# Stage 0.1: dedicated AgentCore execution and SigV4 invocation roles. Both
# remain gated until their corresponding reviewed application plan is approved.
data "aws_iam_policy_document" "agentcore_private_agent_assume_role" {
  statement {
    sid     = "AllowAgentCoreRuntimeFromThisAccount"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = ["<AWS_ACCOUNT_ID>"]
    }

    # The concrete runtime ARN does not exist until a reviewed future apply.
    # This is restricted to AgentCore Runtime resources in this account/region.
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:example:bedrock-agentcore:us-east-1:<AWS_ACCOUNT_ID>:runtime/*"]
    }
  }
}

resource "aws_iam_role" "agentcore_private_agent_execution" {
  count = var.enable_agentcore_private_agent_foundation ? 1 : 0

  name               = var.agentcore_private_agent_execution_role_name
  description        = "Least-privilege execution role for the Texas Energy private AgentCore Runtime."
  assume_role_policy = data.aws_iam_policy_document.agentcore_private_agent_assume_role.json
}

data "aws_iam_policy_document" "agentcore_private_agent_execution" {
  # AgentCore validates and pulls the immutable private image using the
  # runtime execution role during CreateAgentRuntime. ECR does not allow
  # GetAuthorizationToken to be scoped to a repository; the image reads are
  # restricted to this one repository.
  statement {
    sid       = "AcquirePrivateAgentEcrAuthorizationToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = ["us-east-1"]
    }
  }

  statement {
    sid    = "ReadPrivateAgentEcrImage"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = ["arn:example:ecr:us-east-1:<AWS_ACCOUNT_ID>:repository/texas-energy-private-agent"]
  }

  statement {
    sid       = "ReadOnlyPrivateAgentArtifacts"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["arn:example:s3:::texas-energy-private-agent-artifacts-example/private-agent/v1/*"]
  }

  # Four exact, small, SSE-S3 dashboard objects are the only production data
  # evidence an AgentCore request can read. No list, write, Glue, Lambda, or
  # forecasting permission is granted to the private analyst runtime.
  statement {
    sid     = "ReadPublishedDashboardEvidenceOnly"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/dashboard/v1/data/prospective-forecast.json",
      "arn:example:s3:::texas-energy-data-lake-example/dashboard/v1/data/prospective-evaluation-manifest.json",
      "arn:example:s3:::texas-energy-data-lake-example/dashboard/v1/data/rolling-prospective-evaluation.json",
      "arn:example:s3:::texas-energy-data-lake-example/dashboard/v1/data/rolling-historical-replay.json",
    ]
  }

  # The private agent uses Bedrock Converse without streaming. Nova Micro is
  # directly invocable in us-east-1, so no inference-profile permission is needed.
  statement {
    sid       = "InvokeApprovedNovaMicroOnly"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:example:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0"]
  }

  statement {
    sid    = "WriteAgentCoreRuntimeLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:DescribeLogStreams",
    ]
    resources = ["arn:example:logs:us-east-1:<AWS_ACCOUNT_ID>:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }

  statement {
    sid       = "DescribeAgentCoreRuntimeLogGroups"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:example:logs:us-east-1:<AWS_ACCOUNT_ID>:log-group:*"]
  }

  statement {
    sid       = "CreateAgentCoreRuntimeLogPolicy"
    effect    = "Allow"
    actions   = ["logs:PutResourcePolicy"]
    resources = ["arn:example:logs:us-east-1:<AWS_ACCOUNT_ID>:log-group:/aws/bedrock-agentcore/runtimes/texas_energy_private_agent-*"]
  }

  statement {
    sid    = "WriteAgentCoreRuntimeLogEvents"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:example:logs:us-east-1:<AWS_ACCOUNT_ID>:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]
  }

  # X-Ray does not support resource-level permission for these runtime telemetry actions.
  statement {
    sid    = "PublishAgentCoreTelemetry"
    effect = "Allow"
    actions = [
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
      "xray:PutTelemetryRecords",
      "xray:PutTraceSegments",
    ]
    resources = ["*"]
  }

  # CloudWatch PutMetricData does not support resource ARNs; the namespace condition
  # limits the permission to AgentCore telemetry.
  statement {
    sid       = "PublishAgentCoreMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }
}

resource "aws_iam_role_policy" "agentcore_private_agent_execution" {
  count = var.enable_agentcore_private_agent_foundation ? 1 : 0

  name   = "texas-energy-agentcore-private-agent-execution"
  role   = aws_iam_role.agentcore_private_agent_execution[0].id
  policy = data.aws_iam_policy_document.agentcore_private_agent_execution.json
}

data "aws_iam_policy_document" "agentcore_private_agent_invoker_assume_role" {
  statement {
    sid     = "AllowOnlyReviewedTexasEnergySsoRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [var.agentcore_private_agent_sso_principal_arn]
    }
  }
}

resource "aws_iam_role" "agentcore_private_agent_invoker" {
  count = var.enable_agentcore_private_agent_foundation ? 1 : 0

  name               = var.agentcore_private_agent_invoker_role_name
  description        = "SigV4-only invoker for the Texas Energy private AgentCore Runtime."
  assume_role_policy = data.aws_iam_policy_document.agentcore_private_agent_invoker_assume_role.json
}

data "aws_iam_policy_document" "agentcore_private_agent_invoker" {
  # AgentCore generates the final ten-character runtime suffix. The application
  # root adds resource policies with exact runtime and endpoint ARNs as a second,
  # stricter authorization layer.
  statement {
    sid     = "InvokePrivateAgentRuntimeOnly"
    effect  = "Allow"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [
      "arn:example:bedrock-agentcore:us-east-1:<AWS_ACCOUNT_ID>:runtime/texas_energy_private_agent-*",
      "arn:example:bedrock-agentcore:us-east-1:<AWS_ACCOUNT_ID>:runtime/texas_energy_private_agent-*/endpoint/*",
    ]
  }

  statement {
    sid       = "DenyRuntimeUserIdDelegation"
    effect    = "Deny"
    actions   = ["bedrock-agentcore:InvokeAgentRuntimeForUser"]
    resources = ["arn:example:bedrock-agentcore:us-east-1:<AWS_ACCOUNT_ID>:runtime/texas_energy_private_agent-*"]
  }
}

resource "aws_iam_role_policy" "agentcore_private_agent_invoker" {
  count = var.enable_agentcore_private_agent_foundation ? 1 : 0

  name   = "texas-energy-agentcore-private-agent-invoker"
  role   = aws_iam_role.agentcore_private_agent_invoker[0].id
  policy = data.aws_iam_policy_document.agentcore_private_agent_invoker.json
}
