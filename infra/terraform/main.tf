data "aws_iam_policy_document" "github_actions_assume_role" {
  statement {
    sid     = "AllowGitHubActionsFromThisRepository"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:example-org/example-repository:ref:refs/heads/main"
      ]
    }
  }
}

resource "aws_iam_openid_connect_provider" "github_actions" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "github_actions" {
  name               = "github-actions-texas-energy-intelligence"
  description        = "GitHub Actions OIDC role for example-org/texas-energy-intelligence-portfolio."
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume_role.json
}

data "aws_iam_policy_document" "github_actions_terraform_state" {
  statement {
    sid       = "ListTerraformStatePrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:example:s3:::texas-energy-tfstate-<AWS_ACCOUNT_ID>"]

    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values   = ["platform/terraform.tfstate"]
    }
  }

  statement {
    sid    = "ReadWriteTerraformState"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["arn:example:s3:::texas-energy-tfstate-<AWS_ACCOUNT_ID>/platform/terraform.tfstate"]
  }

  statement {
    sid    = "ManageTerraformStateLock"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["arn:example:s3:::texas-energy-tfstate-<AWS_ACCOUNT_ID>/platform/terraform.tfstate.tflock"]
  }
}

resource "aws_iam_policy" "github_actions_terraform_state" {
  name        = "github-actions-texas-energy-terraform-state"
  description = "Allows GitHub Actions to access only the Terraform state object and lock file."
  policy      = data.aws_iam_policy_document.github_actions_terraform_state.json
}

resource "aws_iam_role_policy_attachment" "github_actions_terraform_state" {
  role       = aws_iam_role.github_actions.name
  policy_arn = aws_iam_policy.github_actions_terraform_state.arn
}

data "aws_iam_policy_document" "github_actions_terraform_read" {
  statement {
    sid    = "ReadGitHubOidcProvider"
    effect = "Allow"
    actions = [
      "iam:GetOpenIDConnectProvider",
      "iam:ListOpenIDConnectProviderTags",
    ]
    resources = ["arn:example:iam::<AWS_ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"]
  }

  statement {
    sid    = "ReadProjectRoles"
    effect = "Allow"
    actions = [
      "iam:GetRole",
      "iam:ListAttachedRolePolicies",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
    ]
    resources = [
      "arn:example:iam::<AWS_ACCOUNT_ID>:role/github-actions-texas-energy-intelligence",
      "arn:example:iam::<AWS_ACCOUNT_ID>:role/github-actions-texas-energy-intelligence-deploy",
    ]
  }

  statement {
    sid    = "ReadProjectManagedPolicies"
    effect = "Allow"
    actions = [
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:ListPolicyTags",
    ]
    resources = [
      "arn:example:iam::<AWS_ACCOUNT_ID>:policy/github-actions-texas-energy-terraform-state",
      "arn:example:iam::<AWS_ACCOUNT_ID>:policy/github-actions-texas-energy-terraform-read",
      "arn:example:iam::<AWS_ACCOUNT_ID>:policy/github-actions-texas-energy-application-terraform-state",
      "arn:example:iam::<AWS_ACCOUNT_ID>:policy/github-actions-texas-energy-application-stage-a-deploy",
      "arn:example:iam::<AWS_ACCOUNT_ID>:policy/github-actions-texas-energy-application-stage-b-deploy",
      "arn:example:iam::<AWS_ACCOUNT_ID>:policy/github-actions-texas-energy-ingestion-image-push",
      "arn:example:iam::<AWS_ACCOUNT_ID>:policy/texas-energy-ingestion-execution-boundary",
    ]
  }
}

resource "aws_iam_policy" "github_actions_terraform_read" {
  name        = "github-actions-texas-energy-terraform-read"
  description = "Allows GitHub Actions to read only Terraform-managed IAM resources."
  policy      = data.aws_iam_policy_document.github_actions_terraform_read.json
}

resource "aws_iam_role_policy_attachment" "github_actions_terraform_read" {
  role       = aws_iam_role.github_actions.name
  policy_arn = aws_iam_policy.github_actions_terraform_read.arn
}

data "aws_iam_policy_document" "github_actions_deploy_assume_role" {
  statement {
    sid     = "AllowGitHubActionsFromThisRepositoryMain"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:example-org/example-repository:ref:refs/heads/main"
      ]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  name               = "github-actions-texas-energy-intelligence-deploy"
  description        = "GitHub Actions OIDC role for application Terraform deployments."
  assume_role_policy = data.aws_iam_policy_document.github_actions_deploy_assume_role.json
}

# Separate from the application deployer: this role cannot access Terraform
# state or other AWS services. Its only mutable action is an update to a
# distribution carrying the dashboard Project tag.
resource "aws_iam_role" "github_actions_dashboard_switch" {
  name               = "github-actions-texas-energy-dashboard-switch"
  description        = "GitHub Actions OIDC role for dashboard availability only."
  assume_role_policy = data.aws_iam_policy_document.github_actions_deploy_assume_role.json
}

data "aws_iam_policy_document" "github_actions_application_terraform_state" {
  statement {
    sid       = "ListApplicationTerraformStateAndLock"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:example:s3:::texas-energy-tfstate-<AWS_ACCOUNT_ID>"]

    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values = [
        "application/terraform.tfstate",
        "application/terraform.tfstate.tflock",
      ]
    }
  }

  statement {
    sid    = "ReadWriteApplicationTerraformState"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["arn:example:s3:::texas-energy-tfstate-<AWS_ACCOUNT_ID>/application/terraform.tfstate"]
  }

  statement {
    sid    = "ManageApplicationTerraformStateLock"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["arn:example:s3:::texas-energy-tfstate-<AWS_ACCOUNT_ID>/application/terraform.tfstate.tflock"]
  }
}

resource "aws_iam_policy" "github_actions_application_terraform_state" {
  name        = "github-actions-texas-energy-application-terraform-state"
  description = "Allows the deployment role to access only the application Terraform state object and lock file."
  policy      = data.aws_iam_policy_document.github_actions_application_terraform_state.json
}

resource "aws_iam_role_policy_attachment" "github_actions_application_terraform_state" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.github_actions_application_terraform_state.arn
}

# This permissions boundary is created and maintained in the protected
# foundation root. It limits the application-owned Lambda execution role even
# if a future application configuration changes that role's inline policy.
data "aws_iam_policy_document" "ingestion_lambda_execution_boundary" {
  statement {
    sid    = "WriteBronzeObjectsOnly"
    effect = "Allow"
    actions = [
      "s3:PutObject",
    ]
    # The Application-owned Lambda is pinned to canonical Bronze; keep this
    # boundary aligned so its explicit maximum-permission policy remains usable.
    resources = ["arn:example:s3:::texas-energy-data-lake-example/bronze/v1/*"]

    condition {
      test     = "ArnEquals"
      variable = "lambda:SourceFunctionArn"
      values   = ["arn:example:lambda:us-east-1:<AWS_ACCOUNT_ID>:function:texas-energy-ingestion"]
    }
  }

  statement {
    sid       = "ReadOnlyEiaApiKey"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:example:ssm:us-east-1:<AWS_ACCOUNT_ID>:parameter/texas-energy/eia/api-key"]

    condition {
      test     = "ArnEquals"
      variable = "lambda:SourceFunctionArn"
      values   = ["arn:example:lambda:us-east-1:<AWS_ACCOUNT_ID>:function:texas-energy-ingestion"]
    }
  }

  statement {
    sid    = "WriteFunctionLogsOnly"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:example:logs:us-east-1:<AWS_ACCOUNT_ID>:log-group:/aws/lambda/texas-energy-ingestion:*"]
  }

  # The EventBridge schedule calls this Lambda only to construct the dated
  # normal-pipeline input.  The permissions boundary prevents that scheduler
  # operation from starting any other workflow or paid service directly.
  statement {
    sid       = "StartOnlyProductionPipeline"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = ["arn:example:states:us-east-1:<AWS_ACCOUNT_ID>:stateMachine:texas-energy-production-data-pipeline"]

    condition {
      test     = "ArnEquals"
      variable = "lambda:SourceFunctionArn"
      values   = ["arn:example:lambda:us-east-1:<AWS_ACCOUNT_ID>:function:texas-energy-ingestion"]
    }
  }
}

resource "aws_iam_policy" "ingestion_lambda_execution_boundary" {
  name        = "texas-energy-ingestion-execution-boundary"
  description = "Maximum permissions for the Texas Energy ingestion Lambda execution role."
  policy      = data.aws_iam_policy_document.ingestion_lambda_execution_boundary.json
}

# Stage A grants only the services required to create the Bronze bucket, ECR
# repository, bounded Lambda execution role, and its log group. Lambda and
# EventBridge actions are intentionally excluded until a reviewed Stage B.
data "aws_iam_policy_document" "github_actions_application_stage_a_deploy" {
  # The Application root also owns pre-existing private AgentCore artifact
  # resources.  Terraform must read these controls during a refreshed plan,
  # even when the reviewed change set affects only the canonical Data Lake.
  # This grants no mutation or object-data access to the private artifact
  # bucket.
  statement {
    sid    = "ReadPrivateAgentArtifactControlsForTerraformRefresh"
    effect = "Allow"
    actions = [
      "s3:GetAccelerateConfiguration",
      "s3:GetBucketAcl",
      "s3:GetBucketCORS",
      "s3:GetEncryptionConfiguration",
      "s3:GetBucketLocation",
      "s3:GetBucketLogging",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketOwnershipControls",
      "s3:GetBucketPolicy",
      "s3:GetBucketPublicAccessBlock",
      "s3:GetBucketRequestPayment",
      "s3:GetBucketTagging",
      "s3:GetBucketVersioning",
      "s3:GetBucketWebsite",
      "s3:GetLifecycleConfiguration",
      "s3:GetReplicationConfiguration",
      "s3:ListBucket",
    ]
    resources = ["arn:example:s3:::texas-energy-private-agent-artifacts-example"]
  }

  # The provider refreshes the immutable private-agent repository before it
  # can safely plan unrelated Application resources.  This is inspection-only;
  # image publishing remains governed by its separate policy.
  statement {
    sid    = "ReadPrivateAgentEcrForTerraformRefresh"
    effect = "Allow"
    actions = [
      "ecr:DescribeRepositories",
      "ecr:GetLifecyclePolicy",
      "ecr:GetRepositoryPolicy",
      "ecr:ListTagsForResource",
    ]
    resources = ["arn:example:ecr:us-east-1:<AWS_ACCOUNT_ID>:repository/texas-energy-private-agent"]
  }

  # Read only the existing Terraform-managed AgentCore control plane during a
  # refresh. Runtime/endpoint mutation remains absent from the GitHub role.
  statement {
    sid    = "ReadPrivateAgentControlPlaneForTerraformRefresh"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:GetAgentRuntime",
      "bedrock-agentcore:GetAgentRuntimeEndpoint",
      "bedrock-agentcore:GetResourcePolicy",
      "bedrock-agentcore:ListTagsForResource",
    ]
    resources = [
      "arn:example:bedrock-agentcore:us-east-1:<AWS_ACCOUNT_ID>:runtime/texas_energy_private_agent-*",
      "arn:example:bedrock-agentcore:us-east-1:<AWS_ACCOUNT_ID>:runtime/texas_energy_private_agent-*/endpoint/*",
    ]
  }

  statement {
    sid    = "ManageLegacyBronzeAndCanonicalDataLakeBuckets"
    effect = "Allow"
    actions = [
      "s3:CreateBucket",
      "s3:GetAccelerateConfiguration",
      "s3:GetBucketAcl",
      "s3:GetBucketCORS",
      "s3:GetEncryptionConfiguration",
      "s3:GetBucketLocation",
      "s3:GetBucketLogging",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketOwnershipControls",
      "s3:GetBucketPolicy",
      "s3:GetBucketPublicAccessBlock",
      "s3:GetBucketRequestPayment",
      "s3:GetBucketTagging",
      "s3:GetBucketVersioning",
      "s3:GetBucketWebsite",
      "s3:GetLifecycleConfiguration",
      "s3:GetReplicationConfiguration",
      "s3:ListBucket",
      "s3:PutEncryptionConfiguration",
      "s3:PutBucketOwnershipControls",
      "s3:PutBucketPolicy",
      "s3:PutBucketPublicAccessBlock",
      "s3:PutBucketTagging",
      "s3:PutBucketVersioning",
    ]
    resources = [
      "arn:example:s3:::texas-energy-bronze-example",
      "arn:example:s3:::texas-energy-data-lake-example",
    ]
  }

  statement {
    sid    = "ManageIngestionEcrRepositoryOnly"
    effect = "Allow"
    actions = [
      "ecr:CreateRepository",
      "ecr:DescribeRepositories",
      "ecr:GetLifecyclePolicy",
      "ecr:GetRepositoryPolicy",
      "ecr:ListTagsForResource",
      "ecr:PutImageScanningConfiguration",
      "ecr:PutImageTagMutability",
      "ecr:PutLifecyclePolicy",
      "ecr:SetRepositoryPolicy",
    ]
    resources = ["arn:example:ecr:us-east-1:<AWS_ACCOUNT_ID>:repository/texas-energy-ingestion"]
  }

  statement {
    sid    = "ManageIngestionExecutionRoleOnly"
    effect = "Allow"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
      "iam:PutRolePolicy",
    ]
    resources = ["arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-ingestion-execution"]
  }

  statement {
    sid    = "CreateBoundedIngestionExecutionRoleOnly"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:PutRolePermissionsBoundary",
    ]
    resources = ["arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-ingestion-execution"]

    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      # Keep this known at plan time: the boundary name/ARN is an immutable
      # Foundation contract, while its JSON document may be updated in place.
      values = ["arn:example:iam::<AWS_ACCOUNT_ID>:policy/texas-energy-ingestion-execution-boundary"]
    }
  }

  statement {
    sid    = "ManageIngestionFunctionLogGroupOnly"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:ListTagsForResource",
      "logs:PutRetentionPolicy",
    ]
    resources = ["arn:example:logs:us-east-1:<AWS_ACCOUNT_ID>:log-group:/aws/lambda/texas-energy-ingestion"]
  }

  # CloudWatch Logs does not support resource-level permissions for this
  # account-level list operation. The Terraform provider filters the result to
  # the single configured log group by its exact name.
  statement {
    sid       = "DescribeLogGroupsForTerraformRefresh"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "github_actions_application_stage_a_deploy" {
  name        = "github-actions-texas-energy-application-stage-a-deploy"
  description = "Stage A application Terraform permissions; excludes Lambda and EventBridge mutations."
  policy      = data.aws_iam_policy_document.github_actions_application_stage_a_deploy.json
}

resource "aws_iam_role_policy_attachment" "github_actions_application_stage_a_deploy" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.github_actions_application_stage_a_deploy.arn
}

# Stage B permits deployment, later image-digest updates, and replacement of
# the one application Lambda. It excludes invocation and all EventBridge
# permissions.
data "aws_iam_policy_document" "github_actions_application_stage_b_deploy" {
  statement {
    sid    = "ManageIngestionLambdaOnly"
    effect = "Allow"
    actions = [
      "lambda:CreateFunction",
      "lambda:DeleteFunction",
      "lambda:GetFunction",
      "lambda:ListVersionsByFunction",
      "lambda:PutFunctionConcurrency",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
    ]
    resources = ["arn:example:lambda:us-east-1:<AWS_ACCOUNT_ID>:function:texas-energy-ingestion"]
  }

  statement {
    sid       = "PassIngestionExecutionRoleToLambdaOnly"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-ingestion-execution"]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "github_actions_application_stage_b_deploy" {
  name        = "github-actions-texas-energy-application-stage-b-deploy"
  description = "Stage B application Terraform permissions for the ingestion Lambda only."
  policy      = data.aws_iam_policy_document.github_actions_application_stage_b_deploy.json
}

resource "aws_iam_role_policy_attachment" "github_actions_application_stage_b_deploy" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.github_actions_application_stage_b_deploy.arn
}

# Dashboard delivery is intentionally separate from the production data-pipeline
# policy.  It can publish only a read-only projection, read the immutable
# forecast/Gold inputs needed to refresh evaluation, and manage the one
# CloudFront distribution that serves that projection.
data "aws_iam_policy_document" "github_actions_dashboard_deploy" {
  statement {
    sid       = "ReadCanonicalDashboardInputsOnly"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "forecast-inputs/v1/live/*",
        "forecasts/v1/live/*",
        "forecast-inputs/v1/historical-replay/*",
        "forecasts/v1/sagemaker-validation/historical-replay/*",
        "gold/v2/*",
        "dashboard/v1/*",
      ]
    }
  }

  statement {
    sid     = "ReadImmutableDashboardInputsOnly"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/forecast-inputs/v1/live/*",
      "arn:example:s3:::texas-energy-data-lake-example/forecasts/v1/live/*",
      "arn:example:s3:::texas-energy-data-lake-example/forecast-inputs/v1/historical-replay/*",
      "arn:example:s3:::texas-energy-data-lake-example/forecasts/v1/sagemaker-validation/historical-replay/*",
      "arn:example:s3:::texas-energy-data-lake-example/gold/v2/*",
      "arn:example:s3:::texas-energy-data-lake-example/dashboard/v1/*",
    ]
  }

  statement {
    sid       = "PublishDashboardProjectionOnly"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example/dashboard/v1/*"]
  }

  statement {
    sid    = "ManageTexasEnergyDashboardCloudFrontOnly"
    effect = "Allow"
    actions = [
      "cloudfront:CreateCachePolicy",
      "cloudfront:CreateDistribution",
      "cloudfront:CreateOriginAccessControl",
      "cloudfront:GetCachePolicy",
      "cloudfront:GetCachePolicyConfig",
      "cloudfront:GetDistribution",
      "cloudfront:GetDistributionConfig",
      "cloudfront:GetOriginAccessControl",
      "cloudfront:GetOriginAccessControlConfig",
      "cloudfront:ListTagsForResource",
      "cloudfront:TagResource",
      "cloudfront:UpdateDistribution",
    ]
    resources = ["*"]
  }

  # WAF CreateWebACL has no resource ARN to scope before creation.  Request
  # tags make it possible only for the dashboard ACL, while every subsequent
  # action is bound to that single named global web ACL.  Deletion is omitted.
  statement {
    sid       = "CreateTaggedDashboardWebAclOnly"
    effect    = "Allow"
    actions   = ["wafv2:CreateWebACL"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = ["TexasEnergyDashboard"]
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = ["Project", "Component"]
    }
  }

  statement {
    sid    = "ManageNamedDashboardWebAclOnly"
    effect = "Allow"
    actions = [
      "wafv2:GetWebACL",
      "wafv2:ListTagsForResource",
      "wafv2:TagResource",
      "wafv2:UpdateWebACL",
    ]
    resources = ["arn:example:wafv2:us-east-1:<AWS_ACCOUNT_ID>:global/webacl/texas-energy-dashboard-edge-protection-v1/*"]
  }
}

resource "aws_iam_policy" "github_actions_dashboard_deploy" {
  name        = "github-actions-texas-energy-dashboard-deploy"
  description = "Allows GitHub Actions to publish and serve only the canonical Texas Energy dashboard."
  policy      = data.aws_iam_policy_document.github_actions_dashboard_deploy.json
}

resource "aws_iam_role_policy_attachment" "github_actions_dashboard_deploy" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.github_actions_dashboard_deploy.arn
}

data "aws_iam_policy_document" "github_actions_dashboard_switch" {
  # Discovery is read-only and the follow-up Get/Update actions are restricted
  # by the Project tag below, so no unrelated distribution can be modified.
  statement {
    sid       = "DiscoverDashboardDistributionReadOnly"
    effect    = "Allow"
    actions   = ["cloudfront:ListDistributions"]
    resources = ["*"]
  }

  statement {
    sid    = "ToggleTaggedDashboardDistributionOnly"
    effect = "Allow"
    actions = [
      "cloudfront:GetDistribution",
      "cloudfront:GetDistributionConfig",
      "cloudfront:ListTagsForResource",
      "cloudfront:UpdateDistribution",
    ]
    resources = ["arn:example:cloudfront::<AWS_ACCOUNT_ID>:distribution/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["TexasEnergyDashboard"]
    }
  }
}

resource "aws_iam_policy" "github_actions_dashboard_switch" {
  name        = "github-actions-texas-energy-dashboard-switch"
  description = "Allows GitHub Actions to toggle only the tagged Texas Energy dashboard distribution."
  policy      = data.aws_iam_policy_document.github_actions_dashboard_switch.json
}

resource "aws_iam_role_policy_attachment" "github_actions_dashboard_switch" {
  role       = aws_iam_role.github_actions_dashboard_switch.name
  policy_arn = aws_iam_policy.github_actions_dashboard_switch.arn
}

data "aws_iam_policy_document" "github_actions_ingestion_image_push" {
  statement {
    sid       = "GetEcrAuthorizationToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "PushAndInspectIngestionImagesOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = ["arn:example:ecr:us-east-1:<AWS_ACCOUNT_ID>:repository/texas-energy-ingestion"]
  }
}

# The existing deployment role may publish only this immutable AgentCore image
# and update only this already-managed runtime and endpoint. It cannot invoke
# the agent, read dashboard data, or modify forecasting resources.
data "aws_iam_policy_document" "github_actions_private_agent_deploy" {
  statement {
    sid       = "GetPrivateAgentEcrAuthorizationToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "PushAndInspectPrivateAgentImagesOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
      "ecr:DescribeImages", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
    ]
    resources = ["arn:example:ecr:us-east-1:<AWS_ACCOUNT_ID>:repository/texas-energy-private-agent"]
  }

  statement {
    sid    = "UpdateExistingPrivateAgentRuntimeOnly"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:PutResourcePolicy",
      "bedrock-agentcore:UpdateAgentRuntime",
      "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
    ]
    resources = [
      "arn:example:bedrock-agentcore:us-east-1:<AWS_ACCOUNT_ID>:runtime/texas_energy_private_agent-*",
      "arn:example:bedrock-agentcore:us-east-1:<AWS_ACCOUNT_ID>:runtime/texas_energy_private_agent-*/endpoint/private",
    ]
  }

  statement {
    sid       = "PassOnlyPrivateAgentExecutionRoleToAgentCore"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-agentcore-private-agent-execution"]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "github_actions_private_agent_deploy" {
  name        = "github-actions-texas-energy-private-agent-deploy"
  description = "Publish the reviewed private AgentCore image and update only its existing runtime."
  policy      = data.aws_iam_policy_document.github_actions_private_agent_deploy.json
}

resource "aws_iam_role_policy_attachment" "github_actions_private_agent_deploy" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.github_actions_private_agent_deploy.arn
}

# This policy is intentionally separate from the historical smoke policy.  It
# authorizes only the reviewed production Lambda/Glue/Step Functions/SageMaker
# resources and immutable production-Glue artifact prefix.
data "aws_iam_policy_document" "github_actions_production_pipeline_deploy" {
  statement {
    sid       = "ListAndPublishProductionGlueArtifactsOnly"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["artifacts/glue/production/v1/*"]
    }
  }
  statement {
    sid     = "PublishAndVerifyProductionGlueArtifactsOnly"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/artifacts/glue/production/v1/*",
    ]
  }
  statement {
    sid    = "ManageProductionGlueJobOnly"
    effect = "Allow"
    actions = [
      "glue:CreateJob", "glue:GetJob", "glue:GetTags", "glue:UpdateJob",
    ]
    resources = ["arn:example:glue:us-east-1:<AWS_ACCOUNT_ID>:job/texas-energy-glue-silver-gold-production"]
  }
  statement {
    sid       = "ReadGlueUsageProfileForProductionRefresh"
    effect    = "Allow"
    actions   = ["glue:GetUsageProfile"]
    resources = ["arn:example:glue:us-east-1:<AWS_ACCOUNT_ID>:usageProfile/*"]
  }
  statement {
    sid       = "PassOnlyProductionGlueExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-glue-silver-gold-production-execution"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["glue.amazonaws.com"]
    }
  }
  statement {
    sid    = "ManageProductionStateMachineOnly"
    effect = "Allow"
    actions = [
      "states:CreateStateMachine", "states:DescribeStateMachine", "states:ListStateMachineVersions", "states:ListTagsForResource", "states:TagResource", "states:UpdateStateMachine",
    ]
    resources = ["arn:example:states:us-east-1:<AWS_ACCOUNT_ID>:stateMachine:texas-energy-production-data-pipeline"]
  }

  # ValidateStateMachineDefinition has no resource type in the AWS IAM service
  # authorization reference, so it must be granted separately with Resource="*".
  statement {
    sid       = "ValidateProductionStateMachineDefinition"
    effect    = "Allow"
    actions   = ["states:ValidateStateMachineDefinition"]
    resources = ["*"]
  }
  statement {
    sid       = "PassOnlyPipelineOrchestratorRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-production-data-pipeline-orchestrator"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["states.amazonaws.com"]
    }
  }
  statement {
    sid       = "ManageProductionLambdaPermissionOnly"
    effect    = "Allow"
    actions   = ["lambda:AddPermission", "lambda:GetPolicy"]
    resources = ["arn:example:lambda:us-east-1:<AWS_ACCOUNT_ID>:function:texas-energy-ingestion"]
  }
  statement {
    sid    = "ManageProductionDailyScheduleOnly"
    effect = "Allow"
    actions = [
      "events:DeleteRule", "events:DescribeRule", "events:ListTargetsByRule",
      "events:ListTagsForResource", "events:PutRule", "events:PutTargets",
      "events:RemoveTargets",
    ]
    resources = ["arn:example:events:us-east-1:<AWS_ACCOUNT_ID>:rule/texas-energy-production-data-pipeline-daily"]
  }
  statement {
    sid    = "ManageUnifiedSageMakerRepositoryOnly"
    effect = "Allow"
    actions = [
      "ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:GetLifecyclePolicy", "ecr:ListTagsForResource", "ecr:PutImageScanningConfiguration", "ecr:PutImageTagMutability", "ecr:PutLifecyclePolicy",
    ]
    resources = ["arn:example:ecr:us-east-1:<AWS_ACCOUNT_ID>:repository/texas-energy-sagemaker-unified"]
  }
  statement {
    sid       = "GetEcrAuthorizationForUnifiedSageMakerImage"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid    = "PushAndInspectUnifiedSageMakerImagesOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload", "ecr:DescribeImages", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
    ]
    resources = ["arn:example:ecr:us-east-1:<AWS_ACCOUNT_ID>:repository/texas-energy-sagemaker-unified"]
  }
  statement {
    sid    = "RunBoundedUnifiedSageMakerProcessingJobsOnly"
    effect = "Allow"
    actions = [
      "sagemaker:AddTags", "sagemaker:CreateProcessingJob", "sagemaker:DescribeProcessingJob",
    ]
    resources = ["arn:example:sagemaker:us-east-1:<AWS_ACCOUNT_ID>:processing-job/te-*"]
  }
  statement {
    sid       = "CreateTaggedCandidateTrainingJobsOnly"
    effect    = "Allow"
    actions   = ["sagemaker:AddTags", "sagemaker:CreateTrainingJob"]
    resources = ["arn:example:sagemaker:us-east-1:<AWS_ACCOUNT_ID>:training-job/tei-*-smoke-*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/project"
      values   = ["texas-energy-intelligence"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/run-kind"
      values   = ["candidate-training"]
    }
  }
  statement {
    sid       = "InspectOrStopCandidateTrainingJobsOnly"
    effect    = "Allow"
    actions   = ["sagemaker:DescribeTrainingJob", "sagemaker:StopTrainingJob"]
    resources = ["arn:example:sagemaker:us-east-1:<AWS_ACCOUNT_ID>:training-job/tei-*-smoke-*"]
  }
  statement {
    sid       = "PassOnlyUnifiedSageMakerExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-sagemaker-unified-execution"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["sagemaker.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "github_actions_production_pipeline_deploy" {
  name        = "github-actions-texas-energy-production-pipeline-deploy"
  description = "Manual GitHub deployment permissions for the reviewed canonical production pipeline only."
  policy      = data.aws_iam_policy_document.github_actions_production_pipeline_deploy.json
}

resource "aws_iam_role_policy_attachment" "github_actions_production_pipeline_deploy" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.github_actions_production_pipeline_deploy.arn
}

# Candidate promotion is deliberately separate from pipeline deployment. It can
# read candidate evidence and write only immutable approved artifacts plus the
# one versioned selection pointer. It cannot delete models or start workloads.
data "aws_iam_policy_document" "github_actions_model_promotion" {
  statement {
    sid       = "ListCandidateAndApprovedModelPrefixesOnly"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["models/v2/candidates/*", "models/v1/approved/*"]
    }
  }
  statement {
    sid     = "ReadCandidateEvidenceAndApprovedSelectionOnly"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/models/v2/candidates/*",
      "arn:example:s3:::texas-energy-data-lake-example/models/v1/approved/*",
    ]
  }
  statement {
    sid       = "PublishApprovedArtifactsAndSelectionOnly"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example/models/v1/approved/*"]
  }
}

resource "aws_iam_policy" "github_actions_model_promotion" {
  name        = "github-actions-texas-energy-model-promotion"
  description = "Manual approved-model promotion read/write access restricted to candidate evidence and versioned approved-model prefixes."
  policy      = data.aws_iam_policy_document.github_actions_model_promotion.json
}

resource "aws_iam_role_policy_attachment" "github_actions_model_promotion" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.github_actions_model_promotion.arn
}

resource "aws_iam_policy" "github_actions_ingestion_image_push" {
  name        = "github-actions-texas-energy-ingestion-image-push"
  description = "Allows GitHub Actions to publish immutable ingestion images to one ECR repository."
  policy      = data.aws_iam_policy_document.github_actions_ingestion_image_push.json
}

resource "aws_iam_role_policy_attachment" "github_actions_ingestion_image_push" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.github_actions_ingestion_image_push.arn
}

# The normal CI role remains read-only. This separate policy is attached only
# to the protected manual-deployment role and is limited to the canonical
# Data Lake's reviewed Glue smoke prefixes. It intentionally omits glue:DeleteJob
# and S3 delete permissions.
data "aws_iam_policy_document" "github_actions_glue_smoke_deploy" {
  statement {
    sid       = "ListCanonicalGlueSmokeArtifactPrefixesOnly"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:example:s3:::texas-energy-data-lake-example"]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "artifacts/glue/*",
        "bronze/smoke/*",
      ]
    }
  }

  statement {
    sid    = "PublishAndVerifyCanonicalGlueSmokeArtifactsOnly"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = [
      "arn:example:s3:::texas-energy-data-lake-example/artifacts/glue/*",
      "arn:example:s3:::texas-energy-data-lake-example/bronze/smoke/*",
    ]
  }

  statement {
    sid    = "ManageAndRunOnlyTheGlueSmokeJob"
    effect = "Allow"
    actions = [
      "glue:CreateJob",
      "glue:GetJob",
      "glue:GetJobRun",
      "glue:GetJobRuns",
      "glue:GetTags",
      "glue:StartJobRun",
      "glue:UpdateJob",
    ]
    resources = ["arn:example:glue:us-east-1:<AWS_ACCOUNT_ID>:job/texas-energy-glue-silver-gold-smoke"]
  }

  statement {
    sid       = "ReadGlueUsageProfileForProviderRefresh"
    effect    = "Allow"
    actions   = ["glue:GetUsageProfile"]
    resources = ["arn:example:glue:us-east-1:<AWS_ACCOUNT_ID>:usageProfile/*"]
  }

  statement {
    sid       = "PassOnlyGlueSmokeExecutionRoleToGlue"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-glue-silver-gold-smoke-execution"]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["glue.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "github_actions_glue_smoke_deploy" {
  name        = "github-actions-texas-energy-glue-smoke-deploy"
  description = "Manual GitHub deployment permissions for only the isolated Glue Silver/Gold smoke job."
  policy      = data.aws_iam_policy_document.github_actions_glue_smoke_deploy.json
}

resource "aws_iam_role_policy_attachment" "github_actions_glue_smoke_deploy" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.github_actions_glue_smoke_deploy.arn
}
