# Stage 0 backing resources for the private AgentCore package. They are gated
# so the existing application state remains unchanged unless explicitly reviewed.
#
# The AWS provider does not expose AgentCore's mandatory MMDSv2 setting. The
# gated Terraform-managed bridge below applies the documented update immediately
# after every runtime creation or managed runtime configuration change, then
# verifies the setting and READY state before Terraform can create an endpoint.
locals {
  private_agent_tags = {
    Application = "texas-energy-intelligence"
    Component   = "private-agent"
    ManagedBy   = "terraform"
  }

  private_agent_environment = {
    TEXAS_ENERGY_AGENT_ARTIFACT_BUCKET       = var.private_agent_artifact_bucket_name
    TEXAS_ENERGY_AGENT_ARTIFACT_MANIFEST_KEY = var.private_agent_manifest_key
    TEXAS_ENERGY_AGENT_DATA_LAKE_BUCKET      = var.data_lake_bucket_name
    TEXAS_ENERGY_AGENT_MODEL_ID              = var.private_agent_model_id
    TEXAS_ENERGY_AGENT_REGION                = local.region
  }

  private_agent_runtime_artifact = {
    containerConfiguration = {
      containerUri = var.private_agent_image_uri
    }
  }

  private_agent_network_configuration = {
    networkMode = "PUBLIC"
  }

  private_agent_protocol_configuration = {
    serverProtocol = "HTTP"
  }

  private_agent_lifecycle_configuration = {
    idle_runtime_session_timeout = 900
    max_lifetime                 = 3600
  }

  # The control-plane API uses camelCase while the Terraform resource uses
  # snake_case. Keep both explicit so the bridge replays the same values.
  private_agent_lifecycle_configuration_api = {
    idleRuntimeSessionTimeout = local.private_agent_lifecycle_configuration.idle_runtime_session_timeout
    maxLifetime               = local.private_agent_lifecycle_configuration.max_lifetime
  }
}

resource "aws_s3_bucket" "private_agent_artifacts" {
  count = var.enable_private_agent_artifacts ? 1 : 0

  bucket        = var.private_agent_artifact_bucket_name
  force_destroy = false
  tags          = local.private_agent_tags
}

resource "aws_s3_bucket_ownership_controls" "private_agent_artifacts" {
  count = var.enable_private_agent_artifacts ? 1 : 0

  bucket = aws_s3_bucket.private_agent_artifacts[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "private_agent_artifacts" {
  count = var.enable_private_agent_artifacts ? 1 : 0

  bucket = aws_s3_bucket.private_agent_artifacts[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "private_agent_artifacts" {
  count = var.enable_private_agent_artifacts ? 1 : 0

  bucket = aws_s3_bucket.private_agent_artifacts[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "private_agent_artifacts" {
  count = var.enable_private_agent_artifacts ? 1 : 0

  bucket = aws_s3_bucket.private_agent_artifacts[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

data "aws_iam_policy_document" "private_agent_artifacts_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      "arn:example:s3:::${var.private_agent_artifact_bucket_name}",
      "arn:example:s3:::${var.private_agent_artifact_bucket_name}/*",
    ]

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

resource "aws_s3_bucket_policy" "private_agent_artifacts" {
  count = var.enable_private_agent_artifacts ? 1 : 0

  bucket = aws_s3_bucket.private_agent_artifacts[0].id
  policy = data.aws_iam_policy_document.private_agent_artifacts_bucket.json
}

resource "aws_ecr_repository" "private_agent" {
  count = var.enable_private_agent_artifacts ? 1 : 0

  name                 = var.private_agent_ecr_repository_name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = local.private_agent_tags
}

resource "aws_ecr_lifecycle_policy" "private_agent" {
  count = var.enable_private_agent_artifacts ? 1 : 0

  repository = aws_ecr_repository.private_agent[0].name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain the newest three immutable private-agent images for rollback."
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = {
        type = "expire"
      }
    }]
  })
}

resource "aws_bedrockagentcore_agent_runtime" "private_agent" {
  count = var.enable_private_agent_runtime ? 1 : 0

  agent_runtime_name    = var.private_agent_runtime_name
  description           = "Private, read-only Texas Energy Intelligence HTTP agent."
  role_arn              = var.private_agent_execution_role_arn
  environment_variables = local.private_agent_environment

  agent_runtime_artifact {
    container_configuration {
      container_uri = var.private_agent_image_uri
    }
  }

  network_configuration {
    # SigV4 authenticates callers; this intentionally does not introduce VPC,
    # NAT, or additional network infrastructure for the bounded private demo.
    network_mode = local.private_agent_network_configuration.networkMode
  }

  protocol_configuration {
    server_protocol = local.private_agent_protocol_configuration.serverProtocol
  }

  lifecycle_configuration = [local.private_agent_lifecycle_configuration]
  tags                    = local.private_agent_tags

  depends_on = [
    aws_ecr_repository.private_agent,
    aws_s3_bucket.private_agent_artifacts,
  ]
}

# The command replays every Terraform-owned runtime setting required by
# UpdateAgentRuntime, then waits until the service reports both MMDSv2=true and
# READY. No endpoint is created until this provisioner exits successfully.
resource "terraform_data" "private_agent_mmdsv2" {
  count = var.enable_private_agent_runtime ? 1 : 0

  triggers_replace = {
    runtime_id       = aws_bedrockagentcore_agent_runtime.private_agent[0].agent_runtime_id
    image_uri        = var.private_agent_image_uri
    execution_role   = var.private_agent_execution_role_arn
    environment_hash = sha256(jsonencode(local.private_agent_environment))
    lifecycle_hash   = sha256(jsonencode(local.private_agent_lifecycle_configuration))
    network_hash     = sha256(jsonencode(local.private_agent_network_configuration))
    protocol_hash    = sha256(jsonencode(local.private_agent_protocol_configuration))
  }

  provisioner "local-exec" {
    # PowerShell Core is available on the reviewed Windows workstation and
    # GitHub's Ubuntu runner. A nonzero exit fails closed before endpoint use.
    interpreter = ["pwsh", "-NoProfile", "-NonInteractive", "-Command"]
    command     = <<-EOT
      $ErrorActionPreference = "Stop"
      # Construct JSON in PowerShell. Passing jsonencode() values through a
      # Windows process environment removes their embedded quotes before AWS
      # CLI parses them.
      $artifact = @{
        containerConfiguration = @{
          containerUri = $env:AGENTCORE_IMAGE_URI
        }
      } | ConvertTo-Json -Compress
      $network_configuration = @{
        networkMode = $env:AGENTCORE_NETWORK_MODE
      } | ConvertTo-Json -Compress
      $protocol_configuration = @{
        serverProtocol = $env:AGENTCORE_SERVER_PROTOCOL
      } | ConvertTo-Json -Compress
      $lifecycle_configuration = @{
        idleRuntimeSessionTimeout = [int]$env:AGENTCORE_IDLE_TIMEOUT
        maxLifetime               = [int]$env:AGENTCORE_MAX_LIFETIME
      } | ConvertTo-Json -Compress
      $environment_variables = @{
        TEXAS_ENERGY_AGENT_ARTIFACT_BUCKET       = $env:AGENTCORE_ARTIFACT_BUCKET
        TEXAS_ENERGY_AGENT_ARTIFACT_MANIFEST_KEY = $env:AGENTCORE_ARTIFACT_MANIFEST_KEY
        TEXAS_ENERGY_AGENT_DATA_LAKE_BUCKET      = $env:AGENTCORE_DATA_LAKE_BUCKET
        TEXAS_ENERGY_AGENT_MODEL_ID              = $env:AGENTCORE_MODEL_ID
        TEXAS_ENERGY_AGENT_REGION                = $env:AGENTCORE_APPLICATION_REGION
      } | ConvertTo-Json -Compress
      # Native Windows command-line parsing strips JSON quote characters.
      # Use ephemeral AWS CLI file:// parameters instead, then clean them up.
      $temporary_directory = Join-Path ([System.IO.Path]::GetTempPath()) ("texas-energy-agentcore-" + [System.Guid]::NewGuid().ToString("N"))
      $utf8_without_bom = [System.Text.UTF8Encoding]::new($false)
      New-Item -ItemType Directory -Path $temporary_directory -Force | Out-Null
      try {
        $artifact_file = Join-Path $temporary_directory "artifact.json"
        $network_file = Join-Path $temporary_directory "network.json"
        $protocol_file = Join-Path $temporary_directory "protocol.json"
        $lifecycle_file = Join-Path $temporary_directory "lifecycle.json"
        $environment_file = Join-Path $temporary_directory "environment.json"
        $metadata_file = Join-Path $temporary_directory "metadata.json"
        [System.IO.File]::WriteAllText($artifact_file, $artifact, $utf8_without_bom)
        [System.IO.File]::WriteAllText($network_file, $network_configuration, $utf8_without_bom)
        [System.IO.File]::WriteAllText($protocol_file, $protocol_configuration, $utf8_without_bom)
        [System.IO.File]::WriteAllText($lifecycle_file, $lifecycle_configuration, $utf8_without_bom)
        [System.IO.File]::WriteAllText($environment_file, $environment_variables, $utf8_without_bom)
        [System.IO.File]::WriteAllText($metadata_file, '{"requireMMDSV2":true}', $utf8_without_bom)

        $update_arguments = @(
          "bedrock-agentcore-control", "update-agent-runtime",
          "--region", "$env:AGENTCORE_REGION",
          "--agent-runtime-id", "$env:AGENTCORE_RUNTIME_ID",
          "--agent-runtime-artifact", "file://$artifact_file",
          "--role-arn", "$env:AGENTCORE_ROLE_ARN",
          "--network-configuration", "file://$network_file",
          "--protocol-configuration", "file://$protocol_file",
          "--lifecycle-configuration", "file://$lifecycle_file",
          "--environment-variables", "file://$environment_file",
          "--description", "$env:AGENTCORE_DESCRIPTION",
          "--metadata-configuration", "file://$metadata_file",
          "--no-cli-pager"
        )
        & aws @update_arguments | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "UpdateAgentRuntime failed." }

        $deadline = (Get-Date).AddMinutes(15)
        while ($true) {
          $enabled = (& aws bedrock-agentcore-control get-agent-runtime --region "$env:AGENTCORE_REGION" --agent-runtime-id "$env:AGENTCORE_RUNTIME_ID" --query "metadataConfiguration.requireMMDSV2" --output text --no-cli-pager).Trim()
          if ($LASTEXITCODE -ne 0) { throw "GetAgentRuntime MMDSv2 verification failed." }

          $status = (& aws bedrock-agentcore-control get-agent-runtime --region "$env:AGENTCORE_REGION" --agent-runtime-id "$env:AGENTCORE_RUNTIME_ID" --query "status" --output text --no-cli-pager).Trim()
          if ($LASTEXITCODE -ne 0) { throw "GetAgentRuntime status verification failed." }

          if ($enabled -eq "True" -and $status -eq "READY") {
            Write-Output "AgentCore runtime MMDSv2 verification passed."
            break
          }
          if ($status -in @("FAILED", "DELETE_FAILED")) {
            throw "AgentCore runtime did not become READY after the MMDSv2 update."
          }
          if ((Get-Date) -ge $deadline) {
            throw "Timed out waiting for AgentCore MMDSv2 and READY verification."
          }
          Start-Sleep -Seconds 10
        }
      }
      finally {
        Remove-Item -LiteralPath $temporary_directory -Recurse -Force -ErrorAction SilentlyContinue
      }
    EOT

    environment = {
      AGENTCORE_REGION                = local.region
      AGENTCORE_RUNTIME_ID            = aws_bedrockagentcore_agent_runtime.private_agent[0].agent_runtime_id
      AGENTCORE_IMAGE_URI             = var.private_agent_image_uri
      AGENTCORE_ROLE_ARN              = var.private_agent_execution_role_arn
      AGENTCORE_NETWORK_MODE          = local.private_agent_network_configuration.networkMode
      AGENTCORE_SERVER_PROTOCOL       = local.private_agent_protocol_configuration.serverProtocol
      AGENTCORE_IDLE_TIMEOUT          = tostring(local.private_agent_lifecycle_configuration.idle_runtime_session_timeout)
      AGENTCORE_MAX_LIFETIME          = tostring(local.private_agent_lifecycle_configuration.max_lifetime)
      AGENTCORE_ARTIFACT_BUCKET       = local.private_agent_environment.TEXAS_ENERGY_AGENT_ARTIFACT_BUCKET
      AGENTCORE_ARTIFACT_MANIFEST_KEY = local.private_agent_environment.TEXAS_ENERGY_AGENT_ARTIFACT_MANIFEST_KEY
      AGENTCORE_DATA_LAKE_BUCKET      = local.private_agent_environment.TEXAS_ENERGY_AGENT_DATA_LAKE_BUCKET
      AGENTCORE_MODEL_ID              = local.private_agent_environment.TEXAS_ENERGY_AGENT_MODEL_ID
      AGENTCORE_APPLICATION_REGION    = local.private_agent_environment.TEXAS_ENERGY_AGENT_REGION
      AGENTCORE_DESCRIPTION           = "Private, read-only Texas Energy Intelligence HTTP agent."
    }
  }
}

resource "aws_bedrockagentcore_agent_runtime_endpoint" "private_agent" {
  count = var.enable_private_agent_runtime ? 1 : 0

  agent_runtime_id = aws_bedrockagentcore_agent_runtime.private_agent[0].agent_runtime_id
  # Pin the endpoint to the verified runtime version rather than letting the
  # service retain a previously live runtime version after an image update.
  agent_runtime_version = aws_bedrockagentcore_agent_runtime.private_agent[0].agent_runtime_version
  name                  = "private"
  description           = "SigV4-only endpoint for the Texas Energy private agent."
  tags                  = local.private_agent_tags

  depends_on = [terraform_data.private_agent_mmdsv2]
}

# UpdateAgentRuntime in the mandatory MMDSv2 bridge creates a later runtime
# version than the provider-managed runtime update. Promote the endpoint only
# after that bridge has verified READY so every pinned image deployment serves
# the final MMDSv2-enabled runtime version.
resource "terraform_data" "private_agent_endpoint_promotion" {
  count = var.enable_private_agent_runtime ? 1 : 0

  triggers_replace = {
    runtime_id       = aws_bedrockagentcore_agent_runtime.private_agent[0].agent_runtime_id
    endpoint_name    = aws_bedrockagentcore_agent_runtime_endpoint.private_agent[0].name
    image_uri        = var.private_agent_image_uri
    mmdsv2_bridge_id = terraform_data.private_agent_mmdsv2[0].id
  }

  provisioner "local-exec" {
    interpreter = ["pwsh", "-NoProfile", "-NonInteractive", "-Command"]
    command     = <<-EOT
      $ErrorActionPreference = "Stop"
      $runtime_version = (& aws bedrock-agentcore-control get-agent-runtime --region "$env:AGENTCORE_REGION" --agent-runtime-id "$env:AGENTCORE_RUNTIME_ID" --query "agentRuntimeVersion" --output text --no-cli-pager).Trim()
      if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($runtime_version)) { throw "GetAgentRuntime version lookup failed." }

      & aws bedrock-agentcore-control update-agent-runtime-endpoint --region "$env:AGENTCORE_REGION" --agent-runtime-id "$env:AGENTCORE_RUNTIME_ID" --endpoint-name "$env:AGENTCORE_ENDPOINT_NAME" --agent-runtime-version "$runtime_version" --description "$env:AGENTCORE_ENDPOINT_DESCRIPTION" --no-cli-pager | Out-Null
      if ($LASTEXITCODE -ne 0) { throw "UpdateAgentRuntimeEndpoint failed." }

      $deadline = (Get-Date).AddMinutes(15)
      while ($true) {
        $endpoint = & aws bedrock-agentcore-control get-agent-runtime-endpoint --region "$env:AGENTCORE_REGION" --agent-runtime-id "$env:AGENTCORE_RUNTIME_ID" --endpoint-name "$env:AGENTCORE_ENDPOINT_NAME" --output json --no-cli-pager | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw "GetAgentRuntimeEndpoint verification failed." }
        if ($endpoint.status -eq "READY" -and "$($endpoint.liveVersion)" -eq "$runtime_version") {
          Write-Output "AgentCore endpoint promotion verification passed for runtime version $runtime_version."
          break
        }
        if ($endpoint.status -in @("FAILED", "DELETE_FAILED")) { throw "AgentCore endpoint did not become READY after promotion." }
        if ((Get-Date) -ge $deadline) { throw "Timed out waiting for AgentCore endpoint promotion." }
        Start-Sleep -Seconds 10
      }
    EOT

    environment = {
      AGENTCORE_REGION               = local.region
      AGENTCORE_RUNTIME_ID           = aws_bedrockagentcore_agent_runtime.private_agent[0].agent_runtime_id
      AGENTCORE_ENDPOINT_NAME        = aws_bedrockagentcore_agent_runtime_endpoint.private_agent[0].name
      AGENTCORE_ENDPOINT_DESCRIPTION = "SigV4-only endpoint for the Texas Energy private agent."
    }
  }

  depends_on = [aws_bedrockagentcore_agent_runtime_endpoint.private_agent]
}

data "aws_iam_policy_document" "private_agent_runtime_invocation" {
  count = var.enable_private_agent_runtime ? 1 : 0

  statement {
    sid     = "AllowOnlyDedicatedSigV4Invoker"
    effect  = "Allow"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [
      aws_bedrockagentcore_agent_runtime.private_agent[0].agent_runtime_arn,
    ]

    principals {
      type        = "AWS"
      identifiers = [var.private_agent_invoker_role_arn]
    }
  }

  statement {
    sid     = "DenyOtherSigV4Principals"
    effect  = "Deny"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [
      aws_bedrockagentcore_agent_runtime.private_agent[0].agent_runtime_arn,
    ]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = [var.private_agent_invoker_role_arn]
    }
  }
}

resource "aws_bedrockagentcore_resource_policy" "private_agent_runtime_invocation" {
  count = var.enable_private_agent_runtime ? 1 : 0

  resource_arn = aws_bedrockagentcore_agent_runtime.private_agent[0].agent_runtime_arn
  policy       = data.aws_iam_policy_document.private_agent_runtime_invocation[0].json

  depends_on = [terraform_data.private_agent_mmdsv2]
}

data "aws_iam_policy_document" "private_agent_endpoint_invocation" {
  count = var.enable_private_agent_runtime ? 1 : 0

  statement {
    sid       = "AllowOnlyDedicatedSigV4Invoker"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [aws_bedrockagentcore_agent_runtime_endpoint.private_agent[0].agent_runtime_endpoint_arn]

    principals {
      type        = "AWS"
      identifiers = [var.private_agent_invoker_role_arn]
    }
  }

  statement {
    sid       = "DenyOtherSigV4Principals"
    effect    = "Deny"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [aws_bedrockagentcore_agent_runtime_endpoint.private_agent[0].agent_runtime_endpoint_arn]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = [var.private_agent_invoker_role_arn]
    }
  }
}

resource "aws_bedrockagentcore_resource_policy" "private_agent_endpoint_invocation" {
  count = var.enable_private_agent_runtime ? 1 : 0

  resource_arn = aws_bedrockagentcore_agent_runtime_endpoint.private_agent[0].agent_runtime_endpoint_arn
  policy       = data.aws_iam_policy_document.private_agent_endpoint_invocation[0].json

  depends_on = [aws_bedrockagentcore_resource_policy.private_agent_runtime_invocation]
}
