data "aws_iam_policy_document" "sagemaker_unified_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sagemaker_unified_execution" {
  count              = var.enable_sagemaker_unified_foundation ? 1 : 0
  name               = "texas-energy-sagemaker-unified-execution"
  description        = "Batch-only least-privilege role for the unified Texas Energy SageMaker runtime."
  assume_role_policy = data.aws_iam_policy_document.sagemaker_unified_assume_role.json
}

data "aws_iam_policy_document" "sagemaker_unified_execution" {
  statement {
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/gold/v1/*",
      "arn:example:s3:::texas-energy-data-lake-example/gold/v2/*",
      "arn:example:s3:::texas-energy-data-lake-example/forecast-inputs/v1/*",
      "arn:example:s3:::texas-energy-data-lake-example/models/v1/*",
      "arn:example:s3:::texas-energy-data-lake-example/training/v1/datasets/*",
    ]
  }
  statement {
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/forecasts/v1/sagemaker-validation/*",
      "arn:example:s3:::texas-energy-data-lake-example/forecasts/v1/approved-selection-verification/*",
      "arn:example:s3:::texas-energy-data-lake-example/evaluation/v1/sagemaker-validation/*",
      "arn:example:s3:::texas-energy-data-lake-example/forecasts/v1/live/*",
      "arn:example:s3:::texas-energy-data-lake-example/models/v2/candidates/*",
    ]
  }
  statement {
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "gold/v1/*",
        "gold/v2/*",
        "forecast-inputs/v1/*",
        "models/v1/*",
        "training/v1/datasets/*",
        "forecasts/v1/sagemaker-validation/*",
        "evaluation/v1/sagemaker-validation/*",
        "forecasts/v1/live/*",
        "models/v2/candidates/*",
      ]
    }
  }
  statement {
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    effect    = "Allow"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = ["arn:example:ecr:us-east-1:<AWS_ACCOUNT_ID>:repository/texas-energy-sagemaker-unified"]
  }
  statement {
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:example:logs:us-east-1:<AWS_ACCOUNT_ID>:log-group:/aws/sagemaker/ProcessingJobs:*",
      "arn:example:logs:us-east-1:<AWS_ACCOUNT_ID>:log-group:/aws/sagemaker/TrainingJobs:*",
    ]
  }
}

resource "aws_iam_role_policy" "sagemaker_unified_execution" {
  count  = var.enable_sagemaker_unified_foundation ? 1 : 0
  name   = "texas-energy-sagemaker-unified-scoped"
  role   = aws_iam_role.sagemaker_unified_execution[0].id
  policy = data.aws_iam_policy_document.sagemaker_unified_execution.json
}
