locals {
  glue_smoke_job_name = "texas-energy-glue-silver-gold-smoke"
  glue_smoke_role_arn = "arn:example:iam::<AWS_ACCOUNT_ID>:role/${var.glue_smoke_execution_role_name}"
}

data "aws_iam_policy_document" "glue_smoke_assume_role" {
  statement {
    sid     = "AllowOnlyTheReviewedGlueSmokeJob"
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
      values   = ["arn:example:glue:us-east-1:<AWS_ACCOUNT_ID>:job/${local.glue_smoke_job_name}"]
    }
  }
}

resource "aws_iam_role" "glue_smoke_execution" {
  count = var.enable_glue_smoke_foundation ? 1 : 0

  name               = var.glue_smoke_execution_role_name
  description        = "Least-privilege execution role for the isolated Glue Silver/Gold smoke job."
  assume_role_policy = data.aws_iam_policy_document.glue_smoke_assume_role.json
}

data "aws_iam_policy_document" "glue_smoke_execution" {
  statement {
    sid       = "ListOnlyCanonicalDataLakeGluePrefixes"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        # Delta probes every S3 path ancestor before it creates _delta_log.
        # These exact prefixes retain that traversal without exposing unrelated
        # canonical Data Lake objects.
        "bronze",
        "bronze/",
        "bronze/smoke",
        "bronze/smoke/",
        "bronze/smoke/v2",
        "bronze/smoke/v2/",
        "bronze/smoke/v2/*",
        "artifacts",
        "artifacts/",
        "artifacts/glue",
        "artifacts/glue/",
        "artifacts/glue/v2",
        "artifacts/glue/v2/",
        "artifacts/glue/v2/*",
        "silver",
        "silver/",
        "silver_$folder$",
        "silver/smoke_$folder$",
        "silver/smoke/v2_$folder$",
        "silver/smoke/v2/runs_$folder$",
        "silver/smoke/*",
        "gold",
        "gold/",
        "gold_$folder$",
        "gold/smoke_$folder$",
        "gold/smoke/v2_$folder$",
        "gold/smoke/v2/runs_$folder$",
        "gold/smoke/*",
      ]
    }
  }

  # EMRFS made an observed ListBucket request without a prefix while probing
  # output ancestors.  This permits only the omitted/empty request forms; it
  # does not grant GetObject outside the existing smoke scopes.
  statement {
    sid       = "ListCanonicalDataLakeWhenPrefixIsOmitted"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]

    condition {
      test     = "Null"
      variable = "s3:prefix"
      values   = ["true"]
    }
  }

  statement {
    sid       = "ListCanonicalDataLakeWhenPrefixIsEmpty"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]

    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values   = [""]
    }
  }

  statement {
    sid     = "ReadOnlySmokeFixtureAndGlueArtifacts"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/artifacts/glue/v2/*",
      "arn:example:s3:::texas-energy-data-lake-example/bronze/smoke/v2/*",
    ]
  }

  statement {
    sid    = "ReadWriteSmokeSilverAndGoldOutputs"
    effect = "Allow"
    # Delta/EMRFS writes transaction checksums through temporary objects and
    # must delete those temporary objects after a successful rename.
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/silver/smoke/v2/runs/*",
      "arn:example:s3:::texas-energy-data-lake-example/gold/smoke/v2/runs/*",
    ]
  }

  # EMRFS may materialize legacy folder markers before it writes a descendant
  # Delta table. Each marker is a sibling of the path it represents, so the
  # output descendant wildcards above do not cover any of these exact keys.
  statement {
    sid     = "ReadWriteSmokeTopLevelFolderMarkers"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/silver_$folder$",
      "arn:example:s3:::texas-energy-data-lake-example/silver/smoke_$folder$",
      "arn:example:s3:::texas-energy-data-lake-example/silver/smoke/v2_$folder$",
      "arn:example:s3:::texas-energy-data-lake-example/silver/smoke/v2/runs_$folder$",
      "arn:example:s3:::texas-energy-data-lake-example/gold_$folder$",
      "arn:example:s3:::texas-energy-data-lake-example/gold/smoke_$folder$",
      "arn:example:s3:::texas-energy-data-lake-example/gold/smoke/v2_$folder$",
      "arn:example:s3:::texas-energy-data-lake-example/gold/smoke/v2/runs_$folder$",
    ]
  }

  statement {
    sid       = "LocateOnlyTheCanonicalDataLake"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]
  }

  statement {
    sid    = "WriteGlueJobLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]
    resources = ["arn:example:logs:us-east-1:<AWS_ACCOUNT_ID>:log-group:/aws-glue/jobs/*"]
  }
}

resource "aws_iam_role_policy" "glue_smoke_execution" {
  count = var.enable_glue_smoke_foundation ? 1 : 0

  name   = "${var.glue_smoke_execution_role_name}-scoped"
  role   = aws_iam_role.glue_smoke_execution[0].id
  policy = data.aws_iam_policy_document.glue_smoke_execution.json
}
