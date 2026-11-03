locals {
  glue_smoke_artifact_prefix   = "artifacts/glue/${var.glue_smoke_artifact_version}"
  glue_smoke_entrypoint_prefix = "${local.glue_smoke_artifact_prefix}/revisions/${var.glue_smoke_entrypoint_revision}"
  glue_smoke_input_prefix      = "bronze/smoke/${var.glue_smoke_artifact_version}/fixtures"
  glue_smoke_silver_prefix     = "silver/smoke/${var.glue_smoke_artifact_version}"
  glue_smoke_gold_prefix       = "gold/smoke/${var.glue_smoke_artifact_version}"
}

# This job is deliberately disabled by default. The package and sanitized Bronze
# fixture must be uploaded to the reviewed prefixes before an approved apply.
resource "aws_glue_job" "silver_gold_smoke" {
  count = var.enable_glue_smoke_job ? 1 : 0

  name              = var.glue_smoke_job_name
  description       = "Bounded Glue 5 Bronze-to-Silver-to-Gold Delta smoke test."
  role_arn          = var.glue_smoke_execution_role_arn
  glue_version      = "5.0"
  worker_type       = "G.1X"
  number_of_workers = 2
  timeout           = 10
  max_retries       = 0

  command {
    name            = "glueetl"
    python_version  = "3"
    script_location = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.glue_smoke_entrypoint_prefix}/run_silver_gold.py"
  }

  execution_property {
    max_concurrent_runs = 1
  }

  default_arguments = {
    "--datalake-formats"        = "delta"
    "--extra-py-files"          = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.glue_smoke_artifact_prefix}/texas_energy_glue_job.zip"
    "--conf"                    = "spark.sql.extensions=io.delta.sql.DeltaSparkSessionExtension --conf spark.sql.catalog.spark_catalog=org.apache.spark.sql.delta.catalog.DeltaCatalog --conf spark.delta.logStore.class=org.apache.spark.sql.delta.storage.S3SingleDriverLogStore"
    "--bronze-input"            = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.glue_smoke_input_prefix}"
    "--silver-output"           = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.glue_smoke_silver_prefix}/unapproved-default"
    "--gold-output"             = "s3://${aws_s3_bucket.data_lake[0].bucket}/${local.glue_smoke_gold_prefix}/unapproved-default"
    "--shuffle-partitions"      = "4"
    "--job-language"            = "python"
    "--artifact-package-sha256" = var.glue_smoke_artifact_sha256
    "--artifact-script-sha256"  = var.glue_smoke_entrypoint_sha256
  }

  lifecycle {
    precondition {
      condition     = var.enable_glue_smoke_foundation
      error_message = "enable_glue_smoke_job requires the separately approved Glue smoke execution role in the Foundation root."
    }

    precondition {
      condition     = var.enable_canonical_data_lake
      error_message = "enable_glue_smoke_job requires the canonical Data Lake bucket gate."
    }

    precondition {
      condition     = !var.enable_glue_smoke_job || can(regex("^[a-f0-9]{64}$", var.glue_smoke_artifact_sha256))
      error_message = "enable_glue_smoke_job requires a reviewed SHA-256 for the versioned Glue source package."
    }

    precondition {
      condition     = !var.enable_glue_smoke_job || can(regex("^[a-f0-9]{64}$", var.glue_smoke_entrypoint_sha256))
      error_message = "enable_glue_smoke_job requires a reviewed SHA-256 for the versioned Glue entrypoint."
    }
  }
}
