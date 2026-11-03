variable "enable_agentcore_private_agent_foundation" {
  description = "Create the approved AgentCore private-agent execution and invoker IAM roles."
  type        = bool
  default     = true
}

variable "agentcore_private_agent_execution_role_name" {
  description = "Name of the dedicated execution role for the Texas Energy private AgentCore runtime."
  type        = string
  default     = "texas-energy-agentcore-private-agent-execution"

  validation {
    condition     = var.agentcore_private_agent_execution_role_name == "texas-energy-agentcore-private-agent-execution"
    error_message = "agentcore_private_agent_execution_role_name must remain the reviewed private-agent execution role name."
  }
}

variable "agentcore_private_agent_invoker_role_name" {
  description = "Name of the dedicated SigV4 role allowed to invoke only the private AgentCore runtime."
  type        = string
  default     = "texas-energy-agentcore-private-agent-invoker"

  validation {
    condition     = var.agentcore_private_agent_invoker_role_name == "texas-energy-agentcore-private-agent-invoker"
    error_message = "agentcore_private_agent_invoker_role_name must remain the reviewed private-agent invoker role name."
  }
}

variable "agentcore_private_agent_sso_principal_arn" {
  description = "Canonical IAM role ARN behind the reviewed texas-energy AWS SSO profile; only this principal may assume the private-agent invoker role."
  type        = string
  default     = "arn:example:iam::<AWS_ACCOUNT_ID>:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AdministratorAccess_07f81722daeb53f8"

  validation {
    condition     = var.agentcore_private_agent_sso_principal_arn == "arn:example:iam::<AWS_ACCOUNT_ID>:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AdministratorAccess_07f81722daeb53f8"
    error_message = "agentcore_private_agent_sso_principal_arn must remain the reviewed SSO role ARN until a separate invoker-principal review approves a change."
  }
}

variable "enable_glue_smoke_foundation" {
  description = "Create the dedicated, least-privilege Glue smoke-job execution role after explicit approval."
  type        = bool
  default     = false
}

variable "enable_production_data_pipeline_foundation" {
  description = "Create the Foundation IAM roles for the canonical production Glue and Step Functions pipeline."
  type        = bool
  default     = false
}

variable "glue_production_execution_role_name" {
  description = "Foundation-managed execution role for canonical production Glue."
  type        = string
  default     = "texas-energy-glue-silver-gold-production-execution"
}

variable "pipeline_orchestrator_role_name" {
  description = "Foundation-managed role assumed by the canonical Step Functions workflow."
  type        = string
  default     = "texas-energy-production-data-pipeline-orchestrator"
}

variable "enable_sagemaker_unified_foundation" {
  description = "Create the unified batch SageMaker execution role after reviewed deployment approval."
  type        = bool
  default     = false
}

variable "glue_smoke_execution_role_name" {
  description = "Foundation-managed execution role for the isolated Glue Silver/Gold smoke job."
  type        = string
  default     = "texas-energy-glue-silver-gold-smoke-execution"

  validation {
    condition     = var.glue_smoke_execution_role_name == "texas-energy-glue-silver-gold-smoke-execution"
    error_message = "glue_smoke_execution_role_name must remain the reviewed Glue smoke execution role name."
  }
}
