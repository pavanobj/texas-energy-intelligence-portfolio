locals {
  account_id                            = "<AWS_ACCOUNT_ID>"
  region                                = "us-east-1"
  lambda_function_arn                   = "arn:example:lambda:${local.region}:${local.account_id}:function:${var.lambda_function_name}"
  schedule_enabled                      = var.enable_forecast_schedule && var.enable_ingestion_lambda
  production_daily_schedule_enabled     = var.enable_production_daily_schedule && var.enable_production_data_pipeline && var.enable_ingestion_lambda
  production_pipeline_state_machine_arn = "arn:example:states:${local.region}:${local.account_id}:stateMachine:${var.production_state_machine_name}"
}
resource "aws_s3_bucket" "bronze" {
  bucket        = var.bronze_bucket_name
  force_destroy = false

  tags = {
    Application = "texas-energy-intelligence"
    DataLayer   = "bronze"
  }
}

resource "aws_s3_bucket_ownership_controls" "bronze" {
  bucket = aws_s3_bucket.bronze.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "bronze" {
  bucket = aws_s3_bucket.bronze.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bronze" {
  bucket = aws_s3_bucket.bronze.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "bronze" {
  bucket = aws_s3_bucket.bronze.id

  versioning_configuration {
    status = "Enabled"
  }
}

data "aws_iam_policy_document" "bronze_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.bronze.arn, "${aws_s3_bucket.bronze.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

}

resource "aws_s3_bucket_policy" "bronze" {
  bucket = aws_s3_bucket.bronze.id
  policy = data.aws_iam_policy_document.bronze_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.bronze]
}

resource "aws_ecr_repository" "ingestion" {
  name                 = var.ecr_repository_name
  image_tag_mutability = "IMMUTABLE"

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "ingestion" {
  repository = aws_ecr_repository.ingestion.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after seven days."
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Keep the ten most recent ingestion images."
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      },
    ]
  })
}

data "aws_iam_policy_document" "ingestion_lambda_ecr" {
  statement {
    sid    = "AllowIngestionLambdaImageRetrieval"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.lambda_function_arn]
    }
  }
}

resource "aws_ecr_repository_policy" "ingestion_lambda" {
  repository = aws_ecr_repository.ingestion.name
  policy     = data.aws_iam_policy_document.ingestion_lambda_ecr.json
}

data "aws_iam_policy_document" "ingestion_lambda_assume_role" {
  statement {
    sid     = "AllowLambdaServiceToAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingestion_lambda" {
  name                 = "${var.lambda_function_name}-execution"
  description          = "Least-privilege execution role for Texas Energy ingestion Lambda."
  assume_role_policy   = data.aws_iam_policy_document.ingestion_lambda_assume_role.json
  permissions_boundary = var.ingestion_execution_role_permissions_boundary_arn
}

resource "aws_cloudwatch_log_group" "ingestion_lambda" {
  name              = "/aws/lambda/${var.lambda_function_name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "ingestion_lambda_execution" {
  statement {
    sid    = "WriteBronzeObjectsOnly"
    effect = "Allow"
    actions = [
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.data_lake[0].arn}/bronze/v1/*"]

    condition {
      test     = "ArnEquals"
      variable = "lambda:SourceFunctionArn"
      values   = [local.lambda_function_arn]
    }
  }

  statement {
    sid       = "ReadOnlyEiaApiKey"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:example:ssm:${local.region}:${local.account_id}:parameter/texas-energy/eia/api-key"]

    condition {
      test     = "ArnEquals"
      variable = "lambda:SourceFunctionArn"
      values   = [local.lambda_function_arn]
    }
  }

  statement {
    sid    = "WriteFunctionLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.ingestion_lambda.arn}:*"]
  }

  # EventBridge invokes the existing Lambda only to construct a causal daily
  # issue input and start this exact Standard workflow.  It cannot invoke any
  # other state machine or directly run Glue/SageMaker.
  statement {
    sid       = "StartOnlyProductionPipeline"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = [local.production_pipeline_state_machine_arn]

    condition {
      test     = "ArnEquals"
      variable = "lambda:SourceFunctionArn"
      values   = [local.lambda_function_arn]
    }
  }
}

resource "aws_iam_role_policy" "ingestion_lambda_execution" {
  name   = "${var.lambda_function_name}-execution"
  role   = aws_iam_role.ingestion_lambda.id
  policy = data.aws_iam_policy_document.ingestion_lambda_execution.json
}

resource "aws_lambda_function" "ingestion" {
  count = var.enable_ingestion_lambda ? 1 : 0

  function_name = var.lambda_function_name
  description   = "Ingests bounded Texas energy and weather data into immutable Bronze S3 objects."
  package_type  = "Image"
  image_uri     = var.lambda_image_uri
  role          = aws_iam_role.ingestion_lambda.arn
  architectures = ["x86_64"]
  memory_size   = 512
  timeout       = 300

  environment {
    variables = {
      BRONZE_BUCKET_NAME                    = aws_s3_bucket.data_lake[0].bucket
      DEPLOYMENT_REVISION                   = var.lambda_deployment_revision
      PRODUCTION_PIPELINE_STATE_MACHINE_ARN = local.production_pipeline_state_machine_arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.ingestion_lambda]

  lifecycle {
    precondition {
      condition     = var.lambda_image_uri != null && can(regex("@sha256:[0-9a-f]{64}$", var.lambda_image_uri))
      error_message = "lambda_image_uri must be an immutable ECR image digest when enable_ingestion_lambda is true."
    }
  }
}

resource "aws_cloudwatch_event_rule" "forecast" {
  count = local.schedule_enabled ? 1 : 0

  name                = "${var.lambda_function_name}-forecast"
  description         = "Optional Dallas forecast ingestion schedule. Disabled unless explicitly enabled."
  schedule_expression = var.forecast_schedule_expression
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "forecast" {
  count = local.schedule_enabled ? 1 : 0

  rule      = aws_cloudwatch_event_rule.forecast[0].name
  target_id = "ingestion-lambda"
  arn       = aws_lambda_function.ingestion[0].arn
  input = jsonencode({
    operation = "dallas_forecast_weather"
  })
}

resource "aws_lambda_permission" "allow_eventbridge_forecast" {
  count = local.schedule_enabled ? 1 : 0

  statement_id  = "AllowEventBridgeForecast"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingestion[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.forecast[0].arn
}

resource "aws_cloudwatch_event_rule" "production_pipeline_daily" {
  count = local.production_daily_schedule_enabled ? 1 : 0

  name                = "${var.production_state_machine_name}-daily"
  description         = "Starts one causal 15:00 UTC normal Texas Energy production pipeline issue each day."
  schedule_expression = var.production_daily_schedule_expression
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "production_pipeline_daily" {
  count = local.production_daily_schedule_enabled ? 1 : 0

  rule      = aws_cloudwatch_event_rule.production_pipeline_daily[0].name
  target_id = "production-pipeline-scheduler"
  arn       = aws_lambda_function.ingestion[0].arn

  input_transformer {
    input_paths = { scheduled_at = "$.time" }
    input_template = jsonencode({
      operation        = "start_production_pipeline"
      scheduled_at_utc = "<scheduled_at>"
    })
  }

  # EventBridge normalizes only the JSON whitespace in this template. Preserve
  # the deployed daily trigger during unrelated application deployments; the
  # operation and input path remain Terraform-owned configuration above.
  lifecycle {
    ignore_changes = [input_transformer[0].input_template]
  }
}

resource "aws_lambda_permission" "allow_eventbridge_production_pipeline_daily" {
  count = local.production_daily_schedule_enabled ? 1 : 0

  statement_id  = "AllowEventBridgeProductionPipelineDaily"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingestion[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.production_pipeline_daily[0].arn
}
