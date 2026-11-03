// The dashboard is a small, read-only static projection of the canonical lake.
// Forecast source objects stay immutable under forecasts/; only the current
// dashboard projection is refreshed as verified observations arrive.
locals {
  dashboard_prefix = "dashboard/v1"
}

resource "aws_cloudfront_origin_access_control" "dashboard" {
  count                             = var.enable_dashboard_delivery ? 1 : 0
  name                              = "texas-energy-dashboard-data-lake-oac"
  description                       = "CloudFront-only read access to the published Texas Energy dashboard prefix."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_cache_policy" "dashboard_short_ttl" {
  count       = var.enable_dashboard_delivery ? 1 : 0
  name        = "texas-energy-dashboard-short-ttl-v1"
  comment     = "Short cache lifetime so verified actuals become visible without invalidations."
  min_ttl     = 0
  default_ttl = 60
  max_ttl     = 300

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
  }
}

# Immutable dashboard code and styling stay at the edge for up to a day. The
# data behavior below remains short-lived so evaluations can refresh safely.
resource "aws_cloudfront_cache_policy" "dashboard_static_assets" {
  count       = var.enable_dashboard_delivery ? 1 : 0
  name        = "texas-energy-dashboard-static-assets-v1"
  comment     = "Cache immutable dashboard code and styling at the edge."
  min_ttl     = 0
  default_ttl = 3600
  max_ttl     = 86400

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
  }
}

resource "aws_wafv2_web_acl" "dashboard" {
  count       = var.enable_dashboard_delivery ? 1 : 0
  name        = "texas-energy-dashboard-edge-protection-v1"
  description = "Free-plan edge protections for the static Texas Energy dashboard."
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  # This permits normal browsing but blocks sustained cache-busting or floods.
  rule {
    name     = "RateLimitPerIp"
    priority = 0
    action {
      block {}
    }
    statement {
      rate_based_statement {
        aggregate_key_type = "IP"
        limit              = 300
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "DashboardRateLimitPerIp"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "TexasEnergyDashboardWebAcl"
    sampled_requests_enabled   = true
  }

  tags = {
    Project   = "TexasEnergyDashboard"
    Component = "public-dashboard"
  }
}

resource "aws_cloudfront_distribution" "dashboard" {
  count               = var.enable_dashboard_delivery ? 1 : 0
  enabled             = var.enable_dashboard_public_access
  comment             = "Texas Energy Intelligence published dashboard"
  default_root_object = "index.html"
  is_ipv6_enabled     = true
  price_class         = "PriceClass_100"

  origin {
    domain_name              = aws_s3_bucket.data_lake[0].bucket_regional_domain_name
    origin_id                = "canonical-data-lake-dashboard"
    origin_access_control_id = aws_cloudfront_origin_access_control.dashboard[0].id
    origin_path              = "/${local.dashboard_prefix}"
  }

  default_cache_behavior {
    target_origin_id       = "canonical-data-lake-dashboard"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = aws_cloudfront_cache_policy.dashboard_short_ttl[0].id
    compress               = true
  }

  ordered_cache_behavior {
    path_pattern           = "assets/*"
    target_origin_id       = "canonical-data-lake-dashboard"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = aws_cloudfront_cache_policy.dashboard_static_assets[0].id
    compress               = true
  }

  # Query strings and viewer headers are excluded from both cache policies, so
  # arbitrary cache-busting parameters never reach S3 or fragment the cache.
  ordered_cache_behavior {
    path_pattern           = "data/*"
    target_origin_id       = "canonical-data-lake-dashboard"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = aws_cloudfront_cache_policy.dashboard_short_ttl[0].id
    compress               = true
  }

  web_acl_id = aws_wafv2_web_acl.dashboard[0].arn

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = {
    Project   = "TexasEnergyDashboard"
    Component = "public-dashboard"
  }

  depends_on = [aws_s3_bucket_public_access_block.data_lake]
}
