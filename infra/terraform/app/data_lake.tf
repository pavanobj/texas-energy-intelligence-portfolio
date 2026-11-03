locals {
  canonical_data_lake_bucket_arn = "arn:example:s3:::${var.data_lake_bucket_name}"
}

# The legacy bronze-named bucket is deliberately retained in main.tf.  This
# bucket is the canonical, versioned S3 Data Lake for all new platform data.
resource "aws_s3_bucket" "data_lake" {
  count = var.enable_canonical_data_lake ? 1 : 0

  bucket        = var.data_lake_bucket_name
  force_destroy = false

  tags = {
    Application = "texas-energy-intelligence"
    DataLayer   = "canonical-data-lake"
    ManagedBy   = "terraform"
  }
}

resource "aws_s3_bucket_ownership_controls" "data_lake" {
  count  = var.enable_canonical_data_lake ? 1 : 0
  bucket = aws_s3_bucket.data_lake[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "data_lake" {
  count  = var.enable_canonical_data_lake ? 1 : 0
  bucket = aws_s3_bucket.data_lake[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data_lake" {
  count  = var.enable_canonical_data_lake ? 1 : 0
  bucket = aws_s3_bucket.data_lake[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "data_lake" {
  count  = var.enable_canonical_data_lake ? 1 : 0
  bucket = aws_s3_bucket.data_lake[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

data "aws_iam_policy_document" "data_lake_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [local.canonical_data_lake_bucket_arn, "${local.canonical_data_lake_bucket_arn}/*"]

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

  dynamic "statement" {
    for_each = var.enable_dashboard_delivery ? [1] : []
    content {
      sid       = "AllowCloudFrontDashboardReadOnly"
      effect    = "Allow"
      actions   = ["s3:GetObject"]
      resources = ["${local.canonical_data_lake_bucket_arn}/${local.dashboard_prefix}/*"]

      principals {
        type        = "Service"
        identifiers = ["cloudfront.amazonaws.com"]
      }

      condition {
        test     = "StringEquals"
        variable = "AWS:SourceArn"
        values   = [aws_cloudfront_distribution.dashboard[0].arn]
      }
    }
  }
}

resource "aws_s3_bucket_policy" "data_lake" {
  count  = var.enable_canonical_data_lake ? 1 : 0
  bucket = aws_s3_bucket.data_lake[0].id
  policy = data.aws_iam_policy_document.data_lake_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.data_lake]
}

# No transition or expiration lifecycle rule is configured.  Bronze envelopes,
# Delta logs, and version history are evidence-bearing inputs/outputs; a future
# retention policy requires a data-governance review rather than a default TTL.
