locals {
  production_glue_artifact_prefix        = "artifacts/glue/production/v1/revisions/${var.production_glue_artifact_revision}"
  production_bronze_prefix               = "bronze/v1"
  production_silver_prefix               = "silver/v1"
  production_gold_prefix                 = "gold/v2/demand-features"
  production_gold_weather_lineage_prefix = "gold/v2/observed-weather-lineage"
  production_live_input_prefix           = "forecast-inputs/v1/live"
  production_live_forecast_prefix        = "forecasts/v1/live"
  dashboard_publication_prefix           = "dashboard/v1"
}

resource "aws_glue_job" "silver_gold_production" {
  count             = var.enable_production_data_pipeline ? 1 : 0
  name              = var.production_glue_job_name
  description       = "Canonical Bronze to Silver/Gold Delta pipeline; run only through the Standard state machine."
  role_arn          = var.production_glue_execution_role_arn
  glue_version      = "5.0"
  worker_type       = "G.1X"
  number_of_workers = 2
  timeout           = 30
  max_retries       = 0
  command {
    name            = "glueetl"
    python_version  = "3"
    script_location = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_glue_artifact_prefix}/run_production_silver_gold.py"
  }
  execution_property { max_concurrent_runs = 1 }
  default_arguments = {
    "--datalake-formats"            = "delta"
    "--extra-py-files"              = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_glue_artifact_prefix}/texas_energy_glue_job.zip"
    "--conf"                        = "spark.sql.extensions=io.delta.sql.DeltaSparkSessionExtension --conf spark.sql.catalog.spark_catalog=org.apache.spark.sql.delta.catalog.DeltaCatalog --conf spark.delta.logStore.class=org.apache.spark.sql.delta.storage.S3SingleDriverLogStore"
    "--bronze-input"                = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_bronze_prefix}"
    "--silver-output"               = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_silver_prefix}"
    "--gold-output"                 = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_gold_prefix}"
    "--gold-weather-lineage-output" = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_gold_weather_lineage_prefix}"
    "--forecast-input-output"       = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_live_input_prefix}"
    "--forecast-output"             = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_live_forecast_prefix}"
    "--dashboard-output"            = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.dashboard_publication_prefix}"
    "--shuffle-partitions"          = "64"
    "--job-language"                = "python"
  }
  lifecycle {
    precondition {
      condition     = var.enable_production_data_pipeline_foundation
      error_message = "Production Glue requires the Foundation execution role gate."
    }
    precondition {
      condition     = var.enable_canonical_data_lake
      error_message = "Production Glue requires canonical Data Lake."
    }
    precondition {
      condition     = can(regex("^[a-f0-9]{64}$", var.production_glue_package_sha256)) && can(regex("^[a-f0-9]{64}$", var.production_glue_entrypoint_sha256))
      error_message = "Production Glue requires reviewed SHA-256 pins for its immutable package and entrypoint."
    }
  }
}

resource "aws_sfn_state_machine" "production_data_pipeline" {
  count    = var.enable_production_data_pipeline ? 1 : 0
  name     = var.production_state_machine_name
  role_arn = var.production_pipeline_orchestrator_role_arn
  type     = "STANDARD"
  definition = jsonencode({
    Comment = "Canonical Texas Energy ingestion and Delta transformation pipeline"
    StartAt = "ChooseExecutionMode"
    States = {
      # A delayed model-only continuation may use an already immutable forecast
      # input bundle.  It must still carry the original capture timestamps, but
      # must not invoke fresh ingestion or rebuild Gold.
      ChooseExecutionMode = {
        Type = "Choice"
        Choices = [{
          And = [
            { Variable = "$.model_only", IsPresent = true },
            { Variable = "$.model_only", BooleanEquals = true },
          ]
          Next = "ValidateModelOnlyContract"
        }]
        Default = "IngestDemand"
      }
      ValidateModelOnlyContract = {
        Type = "Choice"
        Choices = [{
          And = [
            { Variable = "$.forecast_origin_utc", IsPresent = true },
            { Variable = "$.weather_model_run_utc", IsPresent = true },
            { Variable = "$.forecast_issue_id", IsPresent = true },
          ]
          Next = "RunXGBoost"
        }]
        Default = "ForecastInputContractInvalid"
      }
      IngestDemand = {
        Type           = "Task", Resource = "arn:example:states:::lambda:invoke", ResultPath = "$.demand",
        TimeoutSeconds = 300,
        Parameters     = { FunctionName = aws_lambda_function.ingestion[0].arn, Payload = { operation = "eia_ercot_demand", "start.$" = "$.demand_start_utc", "end.$" = "$.demand_end_utc", page_size = 168 } },
        Next           = "ValidateDemandIngestion"
      }
      ValidateDemandIngestion = {
        Type    = "Choice"
        Choices = [{ Variable = "$.demand.Payload.status", StringEquals = "completed", Next = "IngestHistoricalWeather" }]
        Default = "IngestionFailed"
      }
      IngestHistoricalWeather = {
        Type           = "Task", Resource = "arn:example:states:::lambda:invoke", ResultPath = "$.historical_weather",
        TimeoutSeconds = 300,
        Parameters     = { FunctionName = aws_lambda_function.ingestion[0].arn, Payload = { operation = "texas_historical_weather", "start_date.$" = "$.weather_start_date", "end_date.$" = "$.weather_end_date" } },
        # Core demand Gold is intentionally independent of realized-weather
        # enrichment. Preserve a failed strict enrichment in execution lineage,
        # then continue with issued forecast weather and the core Gold product.
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.historical_weather_enrichment_failure", Next = "IngestIssuedIfsWeather" }],
        Next  = "ValidateHistoricalWeatherIngestion"
      }
      ValidateHistoricalWeatherIngestion = {
        Type    = "Choice"
        Choices = [{ Variable = "$.historical_weather.Payload.status", StringEquals = "completed", Next = "IngestIssuedIfsWeather" }]
        Default = "IngestIssuedIfsWeather"
      }
      # Never substitute the provider's retrieval-time forecast endpoint for
      # the weather-aware model contract. This task captures the requested IFS
      # run and preserves its model-run lineage in canonical Bronze.
      IngestIssuedIfsWeather = {
        Type           = "Task", Resource = "arn:example:states:::lambda:invoke", ResultPath = "$.forecast_weather",
        TimeoutSeconds = 300,
        Parameters     = { FunctionName = aws_lambda_function.ingestion[0].arn, Payload = { operation = "texas_issued_ifs_weather", "model_run_utc.$" = "$.weather_model_run_utc", forecast_hours = 48 } },
        Next           = "ValidateForecastWeatherIngestion"
      }
      ValidateForecastWeatherIngestion = {
        Type    = "Choice"
        Choices = [{ Variable = "$.forecast_weather.Payload.status", StringEquals = "completed", Next = "ChooseTransformation" }]
        Default = "IngestionFailed"
      }
      # Existing historical/replay executions retain the core-demand path. A
      # caller that supplies all three explicit issue fields gets the same Glue
      # job plus manifest-last preparation of one causal SageMaker input bundle.
      ChooseTransformation = {
        Type = "Choice"
        Choices = [{
          And = [
            { Variable = "$.forecast_origin_utc", IsPresent = true },
            { Variable = "$.weather_model_run_utc", IsPresent = true },
            { Variable = "$.forecast_issue_id", IsPresent = true },
          ]
          Next = "TransformAndPrepareForecastInput"
        }]
        Default = "TransformBronzeToGold"
      }
      TransformBronzeToGold = {
        Type       = "Task", Resource = "arn:example:states:::glue:startJobRun.sync", End = true, TimeoutSeconds = 2100,
        Parameters = { JobName = aws_glue_job.silver_gold_production[0].name, Arguments = { "--run-id.$" = "$$.Execution.Name" } }
      }
      TransformAndPrepareForecastInput = {
        Type = "Task", Resource = "arn:example:states:::glue:startJobRun.sync", ResultPath = "$.glue_forecast_input", Next = "RunXGBoost", TimeoutSeconds = 2100,
        Parameters = {
          JobName = aws_glue_job.silver_gold_production[0].name
          Arguments = {
            "--run-id.$"                = "$$.Execution.Name"
            "--forecast-origin-utc.$"   = "$.forecast_origin_utc"
            "--weather-model-run-utc.$" = "$.weather_model_run_utc"
            "--forecast-issue-id.$"     = "$.forecast_issue_id"
            "--forecast-input-output"   = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_live_input_prefix}"
          }
        }
      }
      RunXGBoost = {
        Type = "Task", Resource = "arn:example:states:::sagemaker:createProcessingJob.sync", ResultPath = "$.xgboost_processing", Next = "RunBiLSTM", TimeoutSeconds = 3900,
        Parameters = {
          "ProcessingJobName.$" = "States.Format('te-xgboost-{}-{}', $.forecast_issue_id, $$.Execution.Name)"
          RoleArn               = var.sagemaker_unified_execution_role_arn
          AppSpecification = {
            ImageUri = var.sagemaker_unified_image_uri
            # Persist a deterministic issue/model identifier in the immutable
            # forecast contract rather than letting the container fall back to
            # the local-only runtime default.
            "ContainerArguments.$" = var.enable_approved_model_selection ? "States.Array('--model', 'xgboost', '--input-bundle', '/opt/ml/processing/input', '--artifact-dir', '/opt/ml/processing/model', '--approved-model-selection', '/opt/ml/processing/model-selection/current.json', '--output-dir', '/opt/ml/processing/output', '--run-id', States.Format('{}-xgboost', $.forecast_issue_id), '--generation-type', 'prospective_inference')" : "States.Array('--model', 'xgboost', '--input-bundle', '/opt/ml/processing/input', '--artifact-dir', '/opt/ml/processing/model', '--output-dir', '/opt/ml/processing/output', '--run-id', States.Format('{}-xgboost', $.forecast_issue_id), '--generation-type', 'prospective_inference')"
          }
          ProcessingInputs = concat([
            { InputName = "input", S3Input = { "S3Uri.$" = "States.Format('s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_live_input_prefix}/issue={}/', $.forecast_issue_id)", LocalPath = "/opt/ml/processing/input", S3DataType = "S3Prefix", S3InputMode = "File", S3DataDistributionType = "FullyReplicated", S3CompressionType = "None" } },
            var.enable_approved_model_selection ? { InputName = "model", S3Input = { S3Uri = "s3://${aws_s3_bucket.data_lake[0].bucket}/${var.approved_model_artifacts_prefix}", LocalPath = "/opt/ml/processing/model", S3DataType = "S3Prefix", S3InputMode = "File", S3DataDistributionType = "FullyReplicated", S3CompressionType = "None" } } : { InputName = "model", S3Input = { S3Uri = "s3://${aws_s3_bucket.data_lake[0].bucket}/models/v1/xgboost/final-closeout-653-20261019/", LocalPath = "/opt/ml/processing/model", S3DataType = "S3Prefix", S3InputMode = "File", S3DataDistributionType = "FullyReplicated", S3CompressionType = "None" } },
            ], var.enable_approved_model_selection ? [
            { InputName = "model-selection", S3Input = { S3Uri = "s3://${aws_s3_bucket.data_lake[0].bucket}/${var.approved_model_selection_prefix}", LocalPath = "/opt/ml/processing/model-selection", S3DataType = "S3Prefix", S3InputMode = "File", S3DataDistributionType = "FullyReplicated", S3CompressionType = "None" } },
          ] : [])
          ProcessingOutputConfig = { Outputs = [{ OutputName = "forecast", S3Output = { "S3Uri.$" = "States.Format('s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_live_forecast_prefix}/issue={}/model=xgboost/run={}/', $.forecast_issue_id, States.UUID())", LocalPath = "/opt/ml/processing/output", S3UploadMode = "EndOfJob" } }] }
          ProcessingResources    = { ClusterConfig = { InstanceCount = 1, InstanceType = "ml.m5.xlarge", VolumeSizeInGB = 30 } }
          StoppingCondition      = { MaxRuntimeInSeconds = 3600 }
        }
      }
      RunBiLSTM = {
        Type = "Task", Resource = "arn:example:states:::sagemaker:createProcessingJob.sync", ResultPath = "$.bilstm_processing", Next = "RunChronos2", TimeoutSeconds = 3900,
        Parameters = {
          "ProcessingJobName.$" = "States.Format('te-bilstm-{}-{}', $.forecast_issue_id, $$.Execution.Name)"
          RoleArn               = var.sagemaker_unified_execution_role_arn
          AppSpecification = {
            ImageUri               = var.sagemaker_unified_image_uri
            "ContainerArguments.$" = var.enable_approved_model_selection ? "States.Array('--model', 'bilstm', '--input-bundle', '/opt/ml/processing/input', '--artifact-dir', '/opt/ml/processing/model', '--approved-model-selection', '/opt/ml/processing/model-selection/current.json', '--output-dir', '/opt/ml/processing/output', '--run-id', States.Format('{}-bilstm', $.forecast_issue_id), '--generation-type', 'prospective_inference')" : "States.Array('--model', 'bilstm', '--input-bundle', '/opt/ml/processing/input', '--artifact-dir', '/opt/ml/processing/model', '--output-dir', '/opt/ml/processing/output', '--run-id', States.Format('{}-bilstm', $.forecast_issue_id), '--generation-type', 'prospective_inference')"
          }
          ProcessingInputs = concat([
            { InputName = "input", S3Input = { "S3Uri.$" = "States.Format('s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_live_input_prefix}/issue={}/', $.forecast_issue_id)", LocalPath = "/opt/ml/processing/input", S3DataType = "S3Prefix", S3InputMode = "File", S3DataDistributionType = "FullyReplicated", S3CompressionType = "None" } },
            var.enable_approved_model_selection ? { InputName = "model", S3Input = { S3Uri = "s3://${aws_s3_bucket.data_lake[0].bucket}/${var.approved_model_artifacts_prefix}", LocalPath = "/opt/ml/processing/model", S3DataType = "S3Prefix", S3InputMode = "File", S3DataDistributionType = "FullyReplicated", S3CompressionType = "None" } } : { InputName = "model", S3Input = { S3Uri = "s3://${aws_s3_bucket.data_lake[0].bucket}/models/v1/bilstm/final-closeout-653-20261019/", LocalPath = "/opt/ml/processing/model", S3DataType = "S3Prefix", S3InputMode = "File", S3DataDistributionType = "FullyReplicated", S3CompressionType = "None" } },
            ], var.enable_approved_model_selection ? [
            { InputName = "model-selection", S3Input = { S3Uri = "s3://${aws_s3_bucket.data_lake[0].bucket}/${var.approved_model_selection_prefix}", LocalPath = "/opt/ml/processing/model-selection", S3DataType = "S3Prefix", S3InputMode = "File", S3DataDistributionType = "FullyReplicated", S3CompressionType = "None" } },
          ] : [])
          ProcessingOutputConfig = { Outputs = [{ OutputName = "forecast", S3Output = { "S3Uri.$" = "States.Format('s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_live_forecast_prefix}/issue={}/model=bilstm/run={}/', $.forecast_issue_id, States.UUID())", LocalPath = "/opt/ml/processing/output", S3UploadMode = "EndOfJob" } }] }
          ProcessingResources    = { ClusterConfig = { InstanceCount = 1, InstanceType = "ml.m5.xlarge", VolumeSizeInGB = 30 } }
          StoppingCondition      = { MaxRuntimeInSeconds = 3600 }
        }
      }
      RunChronos2 = {
        Type = "Task", Resource = "arn:example:states:::sagemaker:createProcessingJob.sync", ResultPath = "$.chronos2_processing", Next = "RefreshDashboardEvaluation", TimeoutSeconds = 3900,
        Parameters = {
          "ProcessingJobName.$" = "States.Format('te-chronos2-{}-{}', $.forecast_issue_id, $$.Execution.Name)"
          RoleArn               = var.sagemaker_unified_execution_role_arn
          AppSpecification = {
            ImageUri               = var.sagemaker_unified_image_uri
            "ContainerArguments.$" = "States.Array('--model', 'chronos2', '--input-bundle', '/opt/ml/processing/input', '--artifact-dir', '/opt/ml/processing/model', '--output-dir', '/opt/ml/processing/output', '--run-id', States.Format('{}-chronos2', $.forecast_issue_id), '--generation-type', 'prospective_inference')"
          }
          ProcessingInputs = [
            { InputName = "input", S3Input = { "S3Uri.$" = "States.Format('s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_live_input_prefix}/issue={}/', $.forecast_issue_id)", LocalPath = "/opt/ml/processing/input", S3DataType = "S3Prefix", S3InputMode = "File", S3DataDistributionType = "FullyReplicated", S3CompressionType = "None" } },
            { InputName = "model", S3Input = { S3Uri = "s3://${aws_s3_bucket.data_lake[0].bucket}/models/v1/chronos2/29ec3766d36d6f73f0696f85560a422f50e8498c/", LocalPath = "/opt/ml/processing/model", S3DataType = "S3Prefix", S3InputMode = "File", S3DataDistributionType = "FullyReplicated", S3CompressionType = "None" } },
          ]
          ProcessingOutputConfig = { Outputs = [{ OutputName = "forecast", S3Output = { "S3Uri.$" = "States.Format('s3://${aws_s3_bucket.data_lake[0].bucket}/${local.production_live_forecast_prefix}/issue={}/model=chronos2/run={}/', $.forecast_issue_id, States.UUID())", LocalPath = "/opt/ml/processing/output", S3UploadMode = "EndOfJob" } }] }
          ProcessingResources    = { ClusterConfig = { InstanceCount = 1, InstanceType = "ml.m5.2xlarge", VolumeSizeInGB = 50 } }
          StoppingCondition      = { MaxRuntimeInSeconds = 3600 }
        }
      }
      # The same least-privilege Glue job reads completed immutable forecasts
      # and Gold observations. It never reruns inference or changes Gold. A
      # failed dashboard refresh keeps the forecast execution successful and
      # leaves the previous published dashboard snapshot intact.
      RefreshDashboardEvaluation = {
        Type = "Task", Resource = "arn:example:states:::glue:startJobRun.sync", ResultPath = "$.dashboard_evaluation", Next = "PipelineComplete", TimeoutSeconds = 2100,
        Parameters = {
          JobName = aws_glue_job.silver_gold_production[0].name
          Arguments = {
            "--run-id.$"                  = "$$.Execution.Name"
            "--dashboard-evaluation-only" = "true"
          }
        }
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.dashboard_evaluation_failure", Next = "PipelineComplete" }]
      }
      PipelineComplete = { Type = "Pass", End = true }
      IngestionFailed = {
        Type  = "Fail"
        Error = "TexasEnergy.IngestionValidationFailed"
        Cause = "A Lambda ingestion operation did not return status=completed; Glue was not started."
      }
      ForecastInputContractInvalid = {
        Type  = "Fail"
        Error = "TexasEnergy.ForecastInputContractInvalid"
        Cause = "Model-only execution requires original forecast origin, weather model run, and immutable forecast issue identifier."
      }
    }
  })
  lifecycle {
    precondition {
      condition     = var.enable_production_data_pipeline_foundation
      error_message = "Production state machine requires Foundation IAM."
    }
  }
}

resource "aws_lambda_permission" "allow_pipeline_orchestration" {
  count         = var.enable_production_data_pipeline ? 1 : 0
  statement_id  = "AllowStepFunctionsProductionPipeline"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingestion[0].function_name
  principal     = "states.amazonaws.com"
  source_arn    = aws_sfn_state_machine.production_data_pipeline[0].arn
}
