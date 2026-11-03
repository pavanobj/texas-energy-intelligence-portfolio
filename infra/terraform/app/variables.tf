variable "bronze_bucket_name" {
  description = "Globally unique S3 bucket for immutable Bronze ingestion envelopes."
  type        = string
  default     = "texas-energy-bronze-example"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bronze_bucket_name))
    error_message = "bronze_bucket_name must be a valid-looking lowercase S3 bucket name."
  }
}

variable "enable_canonical_data_lake" {
  description = "Create the reviewed canonical S3 Data Lake; the legacy bronze bucket remains unmanaged by this gate."
  type        = bool
  default     = false
}

variable "enable_dashboard_delivery" {
  description = "Publish the read-only dashboard from the canonical Data Lake through CloudFront OAC."
  type        = bool
  default     = false
}

variable "enable_dashboard_public_access" {
  description = "Durable public availability switch for the dashboard CloudFront distribution."
  type        = bool
  default     = false
}

variable "data_lake_bucket_name" {
  description = "Globally unique canonical S3 Data Lake bucket name."
  type        = string
  default     = "texas-energy-data-lake-example"

  validation {
    condition     = var.data_lake_bucket_name == "texas-energy-data-lake-example"
    error_message = "data_lake_bucket_name must remain the reviewed canonical Data Lake bucket name."
  }
}

variable "ecr_repository_name" {
  description = "ECR repository used for the ingestion Lambda container image."
  type        = string
  default     = "texas-energy-ingestion"
}

variable "lambda_function_name" {
  description = "Name of the optional ingestion Lambda function."
  type        = string
  default     = "texas-energy-ingestion"
}

variable "ingestion_execution_role_permissions_boundary_arn" {
  description = "Foundation-managed permissions boundary required for the ingestion Lambda execution role."
  type        = string
  default     = "arn:example:iam::<AWS_ACCOUNT_ID>:policy/texas-energy-ingestion-execution-boundary"

  validation {
    condition     = var.ingestion_execution_role_permissions_boundary_arn == "arn:example:iam::<AWS_ACCOUNT_ID>:policy/texas-energy-ingestion-execution-boundary"
    error_message = "ingestion_execution_role_permissions_boundary_arn must reference the reviewed foundation-managed ingestion boundary."
  }
}

variable "enable_ingestion_lambda" {
  description = "Create the ingestion Lambda from the reviewed immutable ECR image digest."
  type        = bool
  default     = true
}

variable "lambda_image_uri" {
  description = "Reviewed immutable ECR image URI with a sha256 digest; required when enable_ingestion_lambda is true."
  type        = string
  default     = "<AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/texas-energy-ingestion@sha256:a231e1273afa207c5014aba4e6df28979b6301c23eb04b0b4b3e1da870b0a5c0"
  nullable    = true
}

variable "lambda_deployment_revision" {
  description = "Git revision embedded in the immutable ingestion image for runtime provenance."
  type        = string
  default     = "unverified"
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention period for the ingestion function."
  type        = number
  default     = 14

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.log_retention_days)
    error_message = "log_retention_days must be an AWS-supported retention value."
  }
}

variable "enable_forecast_schedule" {
  description = "Enable the optional EventBridge rule for Dallas forecast ingestion. Disabled by default."
  type        = bool
  default     = false
}

variable "forecast_schedule_expression" {
  description = "EventBridge schedule expression used only when enable_forecast_schedule is true."
  type        = string
  default     = "rate(6 hours)"

  validation {
    condition     = can(regex("^(rate|cron)\\(.+\\)$", var.forecast_schedule_expression))
    error_message = "forecast_schedule_expression must use a rate(...) or cron(...) EventBridge expression."
  }
}

variable "enable_production_daily_schedule" {
  description = "Enable the one-per-day EventBridge trigger for the complete production Step Functions workflow."
  type        = bool
  default     = false
}

variable "production_daily_schedule_expression" {
  description = "UTC EventBridge schedule for the full normal production pipeline; 15:00 UTC follows EIA availability and the 00:00 UTC IFS run."
  type        = string
  default     = "cron(0 15 * * ? *)"

  validation {
    condition     = can(regex("^(rate|cron)\\(.+\\)$", var.production_daily_schedule_expression))
    error_message = "production_daily_schedule_expression must use a rate(...) or cron(...) EventBridge expression."
  }
}

variable "enable_private_agent_artifacts" {
  description = "Create the approved private AgentCore artifact bucket and ECR repository."
  type        = bool
  default     = true
}

variable "private_agent_artifact_bucket_name" {
  description = "Private S3 bucket for the manifest-approved private-agent runtime artifacts."
  type        = string
  default     = "texas-energy-private-agent-artifacts-example"

  validation {
    condition     = var.private_agent_artifact_bucket_name == "texas-energy-private-agent-artifacts-example"
    error_message = "private_agent_artifact_bucket_name must remain the reviewed account-scoped bucket name."
  }
}

variable "private_agent_ecr_repository_name" {
  description = "ECR repository for the ARM64 private AgentCore image."
  type        = string
  default     = "texas-energy-private-agent"

  validation {
    condition     = var.private_agent_ecr_repository_name == "texas-energy-private-agent"
    error_message = "private_agent_ecr_repository_name must remain the reviewed private-agent repository name."
  }
}

variable "enable_private_agent_runtime" {
  description = "Create the private AgentCore Runtime, MMDSv2 bridge, endpoint, and SigV4 resource policies after Stage 0.1 approval."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_private_agent_runtime || (var.enable_private_agent_artifacts && var.private_agent_image_uri != null)
    error_message = "enable_private_agent_runtime requires enable_private_agent_artifacts and an immutable private_agent_image_uri."
  }
}

variable "private_agent_runtime_name" {
  description = "AgentCore Runtime name for the private HTTP agent."
  type        = string
  default     = "texas_energy_private_agent"

  validation {
    condition     = var.private_agent_runtime_name == "texas_energy_private_agent"
    error_message = "private_agent_runtime_name must remain the reviewed private-agent runtime name."
  }
}

variable "private_agent_image_uri" {
  description = "Reviewed immutable ECR image URI for the private AgentCore Runtime; required only when the runtime gate is enabled."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.private_agent_image_uri == null || can(regex("^<AWS_ACCOUNT_ID>\\.dkr\\.ecr\\.us-east-1\\.amazonaws\\.com/texas-energy-private-agent@sha256:[a-f0-9]{64}$", var.private_agent_image_uri))
    error_message = "private_agent_image_uri must be an immutable sha256 URI in the reviewed private-agent ECR repository."
  }
}

variable "private_agent_execution_role_arn" {
  description = "Foundation-managed AgentCore execution role ARN."
  type        = string
  default     = "arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-agentcore-private-agent-execution"

  validation {
    condition     = var.private_agent_execution_role_arn == "arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-agentcore-private-agent-execution"
    error_message = "private_agent_execution_role_arn must reference the reviewed foundation AgentCore execution role."
  }
}

variable "private_agent_invoker_role_arn" {
  description = "Foundation-managed dedicated SigV4 invoker role ARN."
  type        = string
  default     = "arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-agentcore-private-agent-invoker"

  validation {
    condition     = var.private_agent_invoker_role_arn == "arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-agentcore-private-agent-invoker"
    error_message = "private_agent_invoker_role_arn must reference the reviewed foundation AgentCore invoker role."
  }
}

variable "private_agent_model_id" {
  description = "Approved directly invocable Bedrock model ID for the private agent."
  type        = string
  default     = "amazon.nova-micro-v1:0"

  validation {
    condition     = var.private_agent_model_id == "amazon.nova-micro-v1:0"
    error_message = "private_agent_model_id must remain the reviewed directly invocable Nova Micro model ID."
  }
}

variable "private_agent_manifest_key" {
  description = "Immutable private-agent manifest object key."
  type        = string
  default     = "private-agent/v1/manifest.json"

  validation {
    condition     = var.private_agent_manifest_key == "private-agent/v1/manifest.json"
    error_message = "private_agent_manifest_key must remain the reviewed immutable manifest location."
  }
}

variable "enable_glue_smoke_foundation" {
  description = "Acknowledges that the Foundation Glue smoke execution role was separately created."
  type        = bool
  default     = false
}

variable "enable_production_data_pipeline_foundation" {
  description = "Acknowledges that Foundation created the production Glue and orchestration roles."
  type        = bool
  default     = false
}

variable "enable_production_data_pipeline" {
  description = "Create the canonical production Glue job and Standard Step Functions workflow."
  type        = bool
  default     = false
}

variable "production_glue_job_name" {
  description = "Name of the canonical production Bronze-to-Silver-to-Gold Glue job."
  type        = string
  default     = "texas-energy-glue-silver-gold-production"
}

variable "production_glue_execution_role_arn" {
  description = "Foundation-managed production Glue execution-role ARN."
  type        = string
  default     = "arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-glue-silver-gold-production-execution"
}

variable "production_glue_artifact_revision" {
  description = "Immutable production Glue artifact revision published beneath artifacts/glue/production/v1/revisions/."
  type        = string
  default     = "unapproved-default"

  validation {
    condition     = can(regex("^r[0-9]+$", var.production_glue_artifact_revision))
    error_message = "production_glue_artifact_revision must use the reviewed r<integer> form."
  }
}

variable "production_glue_package_sha256" {
  description = "Reviewed SHA-256 of the immutable production Glue Python package."
  type        = string
  default     = ""
}

variable "production_glue_entrypoint_sha256" {
  description = "Reviewed SHA-256 of the immutable production Glue entrypoint."
  type        = string
  default     = ""
}

variable "production_state_machine_name" {
  description = "Standard Step Functions state machine for the production data pipeline."
  type        = string
  default     = "texas-energy-production-data-pipeline"
}

variable "production_pipeline_orchestrator_role_arn" {
  description = "Foundation-managed Step Functions execution-role ARN."
  type        = string
  default     = "arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-production-data-pipeline-orchestrator"
}

variable "enable_sagemaker_unified" {
  description = "Create the immutable unified SageMaker runtime ECR repository; no training job is started by Terraform."
  type        = bool
  default     = false
}

variable "sagemaker_unified_image_uri" {
  description = "Immutable unified SageMaker Processing image used only by the production pipeline."
  type        = string
  default     = ""

  validation {
    condition     = var.sagemaker_unified_image_uri == "" || can(regex("^<AWS_ACCOUNT_ID>\\.dkr\\.ecr\\.us-east-1\\.amazonaws\\.com/texas-energy-sagemaker-unified@sha256:[a-f0-9]{64}$", var.sagemaker_unified_image_uri))
    error_message = "sagemaker_unified_image_uri must be the reviewed immutable unified ECR digest."
  }
}

variable "sagemaker_unified_execution_role_arn" {
  description = "Foundation-managed batch-only role used by the three Processing jobs."
  type        = string
  default     = "arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-sagemaker-unified-execution"
}

variable "enable_approved_model_selection" {
  description = "Mount the approval-gated XGBoost and Bi-LSTM model-selection channels. Defaults false so existing champions remain active until a coordinated approval deploy."
  type        = bool
  default     = false
}

variable "approved_model_artifacts_prefix" {
  description = "Approved immutable trainable-model artifact root, used only when enable_approved_model_selection is true."
  type        = string
  default     = "models/v1/approved/artifacts/"
}

variable "approved_model_selection_prefix" {
  description = "Versioned approved model-selection JSON prefix, used only when enable_approved_model_selection is true."
  type        = string
  default     = "models/v1/approved/selection/"
}

variable "enable_glue_smoke_job" {
  description = "Create the bounded Glue 5 Silver/Gold smoke job after package and fixture review."
  type        = bool
  default     = false
}

variable "glue_smoke_job_name" {
  description = "Name of the isolated Glue Silver/Gold smoke job."
  type        = string
  default     = "texas-energy-glue-silver-gold-smoke"

  validation {
    condition     = var.glue_smoke_job_name == "texas-energy-glue-silver-gold-smoke"
    error_message = "glue_smoke_job_name must remain the reviewed smoke-job name."
  }
}

variable "glue_smoke_execution_role_arn" {
  description = "Foundation-managed Glue smoke execution role ARN."
  type        = string
  default     = "arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-glue-silver-gold-smoke-execution"

  validation {
    condition     = var.glue_smoke_execution_role_arn == "arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-glue-silver-gold-smoke-execution"
    error_message = "glue_smoke_execution_role_arn must reference the reviewed Foundation role."
  }
}

variable "glue_smoke_artifact_version" {
  description = "Immutable, reviewed version label for the Glue script package and sanitized Bronze fixture."
  type        = string
  default     = "v1"

  validation {
    condition     = can(regex("^v[0-9]+$", var.glue_smoke_artifact_version))
    error_message = "glue_smoke_artifact_version must use the reviewed v<integer> form."
  }
}

variable "glue_smoke_entrypoint_revision" {
  description = "Immutable revision label for a corrected Glue entrypoint within the reviewed application version."
  type        = string
  default     = "r1"

  validation {
    condition     = can(regex("^r[0-9]+$", var.glue_smoke_entrypoint_revision))
    error_message = "glue_smoke_entrypoint_revision must use the reviewed r<integer> form."
  }
}

variable "glue_smoke_artifact_sha256" {
  description = "Reviewed SHA-256 of the versioned Glue source ZIP referenced by the smoke job."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.glue_smoke_artifact_sha256 == null || can(regex("^[a-f0-9]{64}$", var.glue_smoke_artifact_sha256))
    error_message = "glue_smoke_artifact_sha256 must be a lowercase SHA-256 hex digest."
  }
}

variable "glue_smoke_entrypoint_sha256" {
  description = "Reviewed SHA-256 of the versioned Glue entrypoint referenced by the smoke job."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.glue_smoke_entrypoint_sha256 == null || can(regex("^[a-f0-9]{64}$", var.glue_smoke_entrypoint_sha256))
    error_message = "glue_smoke_entrypoint_sha256 must be a lowercase SHA-256 hex digest."
  }
}
