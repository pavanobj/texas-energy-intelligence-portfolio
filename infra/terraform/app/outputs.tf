output "bronze_bucket_name" {
  description = "Name of the private S3 bucket for immutable Bronze ingestion envelopes."
  value       = aws_s3_bucket.bronze.bucket
}

output "canonical_data_lake_bucket_name" {
  description = "Canonical S3 Data Lake bucket for Bronze, Silver, Gold, forecasts, evaluation, and Glue artifacts."
  value       = try(aws_s3_bucket.data_lake[0].bucket, null)
}

output "dashboard_url" {
  description = "HTTPS CloudFront URL for the canonical published dashboard when delivery is enabled."
  value       = try("https://${aws_cloudfront_distribution.dashboard[0].domain_name}", null)
}

output "dashboard_distribution_arn" {
  description = "CloudFront distribution ARN for the public dashboard flat-rate plan."
  value       = try(aws_cloudfront_distribution.dashboard[0].arn, null)
}

output "dashboard_web_acl_arn" {
  description = "CloudFront-scope web ACL ARN protecting the public dashboard."
  value       = try(aws_wafv2_web_acl.dashboard[0].arn, null)
}

output "dashboard_public_access_enabled" {
  description = "Current CloudFront enabled state; false means the public site is offline."
  value       = try(aws_cloudfront_distribution.dashboard[0].enabled, false)
}

output "ingestion_ecr_repository_url" {
  description = "ECR repository URL for the Lambda container image."
  value       = aws_ecr_repository.ingestion.repository_url
}

output "ingestion_lambda_function_arn" {
  description = "ARN of the ingestion Lambda when it is explicitly enabled."
  value       = try(aws_lambda_function.ingestion[0].arn, null)
}

output "private_agent_artifact_bucket_name" {
  description = "Private AgentCore artifact bucket name when the reviewed Stage 0 gate is enabled."
  value       = try(aws_s3_bucket.private_agent_artifacts[0].bucket, null)
}

output "private_agent_ecr_repository_url" {
  description = "Private AgentCore ECR repository URL when the reviewed Stage 0 gate is enabled."
  value       = try(aws_ecr_repository.private_agent[0].repository_url, null)
}

output "private_agent_runtime_arn" {
  description = "Private AgentCore Runtime ARN when the reviewed runtime gate is enabled."
  value       = try(aws_bedrockagentcore_agent_runtime.private_agent[0].agent_runtime_arn, null)
}

output "private_agent_endpoint_arn" {
  description = "Private AgentCore endpoint ARN when the reviewed runtime gate is enabled."
  value       = try(aws_bedrockagentcore_agent_runtime_endpoint.private_agent[0].agent_runtime_endpoint_arn, null)
}
