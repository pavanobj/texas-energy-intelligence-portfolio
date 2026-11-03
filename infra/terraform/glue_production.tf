locals {
  glue_production_job_name = "texas-energy-glue-silver-gold-production"
  glue_production_role_arn = "arn:example:iam::<AWS_ACCOUNT_ID>:role/${var.glue_production_execution_role_name}"
}

data "aws_iam_policy_document" "glue_production_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = ["<AWS_ACCOUNT_ID>"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = ["arn:example:glue:us-east-1:<AWS_ACCOUNT_ID>:job/${local.glue_production_job_name}"]
    }
  }
}

resource "aws_iam_role" "glue_production_execution" {
  count              = var.enable_production_data_pipeline_foundation ? 1 : 0
  name               = var.glue_production_execution_role_name
  description        = "Least-privilege Glue execution role for canonical Bronze to Silver/Gold Delta processing."
  assume_role_policy = data.aws_iam_policy_document.glue_production_assume_role.json
}

data "aws_iam_policy_document" "glue_production_execution" {
  # EMRFS/Delta root probes use multiple ListBucket request shapes.  A
  # condition-free bucket-level list grant is the smallest reliable allowance:
  # it reveals key names only; all object reads, writes, and deletes stay
  # separately limited to the explicit pipeline prefixes below.
  statement {
    sid       = "ListCanonicalDataLakeForDelta"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]
  }
  statement {
    sid     = "ReadCanonicalBronzeAndApprovedGlueArtifacts"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/bronze/v1/*",
      "arn:example:s3:::texas-energy-data-lake-example/artifacts/glue/production/v1/*",
    ]
  }
  statement {
    sid     = "WriteOnlyCanonicalDerivedDeltaTables"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/silver/v1/*",
      "arn:example:s3:::texas-energy-data-lake-example/gold/v1/*",
      "arn:example:s3:::texas-energy-data-lake-example/gold/v2/*",
    ]
  }
  # A successful bounded input bundle is immutable: Glue writes three new
  # objects and publishes the manifest last.  It never receives delete or
  # read access to forecast-input bundles.
  statement {
    sid     = "PublishImmutableForecastInputBundlesOnly"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/forecast-inputs/v1/live/*",
      "arn:example:s3:::texas-energy-data-lake-example/forecast-inputs/v1/historical-replay/*",
    ]
  }
  # Dashboard evaluation reads immutable SageMaker output and input-weather
  # provenance, then writes only static, SSE-S3 dashboard JSON. It cannot
  # alter forecasts, source data, or any non-dashboard object.
  statement {
    sid     = "ReadImmutableForecastEvaluationInputs"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/forecasts/v1/live/*",
      "arn:example:s3:::texas-energy-data-lake-example/forecast-inputs/v1/live/*",
    ]
  }
  statement {
    sid     = "PublishStaticDashboardEvaluationOnly"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/dashboard/v1/data/prospective-forecast.json",
      "arn:example:s3:::texas-energy-data-lake-example/dashboard/v1/data/rolling-prospective-evaluation.json",
      "arn:example:s3:::texas-energy-data-lake-example/dashboard/v1/data/prospective-evaluation-manifest.json",
      "arn:example:s3:::texas-energy-data-lake-example/dashboard/v1/evaluations/prospective/*",
    ]
  }
  statement {
    sid       = "DecryptImmutableForecastOutputsViaS3Only"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:example:kms:us-east-1:<AWS_ACCOUNT_ID>:key/<KMS_KEY_ID>"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.us-east-1.amazonaws.com"]
    }
  }

  # EMRFS creates root-level legacy folder markers alongside, rather than
  # beneath, the Delta roots. Keep this observed compatibility grant limited
  # to marker creation; table reads, writes, and deletes remain scoped above.
  statement {
    sid     = "CreateCanonicalDeltaRootMarkersOnly"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/silver/v1_$folder$",
      "arn:example:s3:::texas-energy-data-lake-example/gold/v1_$folder$",
      "arn:example:s3:::texas-energy-data-lake-example/gold/v2_$folder$",
    ]
  }
  statement {
    sid       = "LocateCanonicalDataLake"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]
  }
  statement {
    sid       = "WriteGlueLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents"]
    resources = ["arn:example:logs:us-east-1:<AWS_ACCOUNT_ID>:log-group:/aws-glue/jobs/*"]
  }
}

resource "aws_iam_role_policy" "glue_production_execution" {
  count  = var.enable_production_data_pipeline_foundation ? 1 : 0
  name   = "${var.glue_production_execution_role_name}-scoped"
  role   = aws_iam_role.glue_production_execution[0].id
  policy = data.aws_iam_policy_document.glue_production_execution.json
}

data "aws_iam_policy_document" "pipeline_orchestrator_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = ["<AWS_ACCOUNT_ID>"]
    }
  }
}

resource "aws_iam_role" "pipeline_orchestrator" {
  count              = var.enable_production_data_pipeline_foundation ? 1 : 0
  name               = var.pipeline_orchestrator_role_name
  description        = "Least-privilege Step Functions role for the canonical data pipeline."
  assume_role_policy = data.aws_iam_policy_document.pipeline_orchestrator_assume_role.json
}

data "aws_iam_policy_document" "pipeline_orchestrator" {
  statement {
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:example:lambda:us-east-1:<AWS_ACCOUNT_ID>:function:texas-energy-ingestion"]
  }
  statement {
    # The Standard workflow starts only the three bounded CPU Processing jobs
    # after Glue has published a valid immutable input bundle.
    effect = "Allow"
    actions = [
      # Step Functions' optimized .sync integration attaches its managed
      # STARTED_BY_STEP_FUNCTIONS tag when it creates each bounded job.
      "sagemaker:AddTags", "sagemaker:CreateProcessingJob", "sagemaker:DescribeProcessingJob", "sagemaker:StopProcessingJob",
    ]
    resources = ["arn:example:sagemaker:us-east-1:<AWS_ACCOUNT_ID>:processing-job/te-*"]
  }
  statement {
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-sagemaker-unified-execution"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["sagemaker.amazonaws.com"]
    }
  }
  statement {
    effect = "Allow"
    # Glue's optimized .sync integration requires GetJobRuns in addition to
    # StartJobRun/GetJobRun/BatchStopJobRun.  AWS documents these actions with
    # Resource "*" because the Glue job-run authorization model is not limited
    # to one job ARN for each of the status operations.
    actions   = ["glue:StartJobRun", "glue:GetJobRun", "glue:GetJobRuns", "glue:BatchStopJobRun"]
    resources = ["*"]
  }
  statement {
    # Step Functions uses service-managed EventBridge rules to accelerate .sync
    # completion detection.  Each integration has its own fixed rule name.
    effect  = "Allow"
    actions = ["events:DescribeRule", "events:PutRule", "events:PutTargets"]
    resources = [
      "arn:example:events:us-east-1:<AWS_ACCOUNT_ID>:rule/StepFunctionsGetEventsForGlueJobsRule",
      "arn:example:events:us-east-1:<AWS_ACCOUNT_ID>:rule/StepFunctionsGetEventsForSageMakerProcessingJobsRule",
    ]
  }
}

resource "aws_iam_role_policy" "pipeline_orchestrator" {
  count  = var.enable_production_data_pipeline_foundation ? 1 : 0
  name   = "${var.pipeline_orchestrator_role_name}-scoped"
  role   = aws_iam_role.pipeline_orchestrator[0].id
  policy = data.aws_iam_policy_document.pipeline_orchestrator.json
}
