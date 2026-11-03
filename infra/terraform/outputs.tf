output "github_actions_oidc_provider_arn" {
  description = "ARN of the GitHub Actions OpenID Connect identity provider."
  value       = aws_iam_openid_connect_provider.github_actions.arn
}

output "github_actions_role_arn" {
  description = "ARN GitHub Actions will use to request temporary AWS credentials."
  value       = aws_iam_role.github_actions.arn
}

output "ingestion_execution_role_permissions_boundary_arn" {
  description = "Permissions boundary required on the application ingestion Lambda execution role."
  value       = aws_iam_policy.ingestion_lambda_execution_boundary.arn
}

output "agentcore_private_agent_execution_role_arn" {
  description = "Dedicated AgentCore execution role ARN when the reviewed foundation gate is enabled."
  value       = try(aws_iam_role.agentcore_private_agent_execution[0].arn, null)
}

output "agentcore_private_agent_invoker_role_arn" {
  description = "Dedicated SigV4 private-agent invoker role ARN when the reviewed foundation gate is enabled."
  value       = try(aws_iam_role.agentcore_private_agent_invoker[0].arn, null)
}
