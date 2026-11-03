# Private AgentCore Runtime package

## Selected runtime

The first cloud demonstration should use **Amazon Bedrock AgentCore Runtime,
HTTP protocol, microVM consumption pricing**, not Lambda or ECS Fargate.

- It preserves the existing bounded Python Bedrock Converse loop and its seven
  read-only tools; no framework migration is required.
- The HTTP container contract is a health route at `/ping` and a bounded
  question route at `/invocations` on port 8080. AgentCore authenticates the
  runtime endpoint; the app itself has no public discovery, docs, or arbitrary
  endpoint.
- Lambda is a reasonable low-cost function platform but is a poorer fit for a
  Python agent which can make multiple model turns and materialize local RAG
  assets. ECS Fargate adds standing task costs and operational work for this
  infrequent private workload.
- The existing stdio MCP server remains a local/private tool interface. A
  separate AgentCore MCP deployment is feasible later, but it exposes tools,
  not the current question-answering agent loop. This deployment therefore
  uses the AgentCore HTTP contract for the agent itself.

No runtime, S3 object, ECR image, IAM role, or identity resource is created by
this directory.

## Read-only private data bundle

The runtime downloads only a manifest allowlist into ephemeral `/tmp` storage:

```
private-agent/v1/manifest.json
private-agent/v1/closeout/historical_demo/heldout_2025-07-01_06z_24h.parquet
private-agent/v1/closeout/comparison/{leaderboard,per_horizon_leaderboard}.json
private-agent/v1/closeout/model_catalog.json
private-agent/v1/rag/{chunks.jsonl,embeddings.npy,manifest.json}
private-agent/v1/rag/embedding-model/*
```

`manifest.json` records object key, byte size, and SHA-256. The runtime
rejects undeclared locations, over-sized objects, and checksum failures. The
embedding model is a clean serving bundle stored alongside the index—not a
local Hugging Face cache and not a forecasting-model checkpoint. No Bronze,
Silver, Gold, prospective forecast outputs, model checkpoints, or source
documents are included.

Build the manifest locally only after an approved clean model export exists:

```bash
PYTHONPATH=src python scripts/build_agentcore_artifact_manifest.py \
  --closeout-root "$TEXAS_ENERGY_CLOSEOUT_ROOT" \
  --rag-index-root "$TEXAS_ENERGY_RAG_INDEX_ROOT" \
  --embedding-model-root "$TEXAS_ENERGY_RAG_EMBEDDING_MODEL_ROOT" \
  --s3-prefix private-agent/v1 \
  --output /tmp/texas-energy-agent-manifest.json
```

The command does **not** upload anything.

## Container and configuration

Build only after explicit approval, on an ARM64 platform as required by
AgentCore Runtime:

```bash
docker buildx build --platform linux/arm64 \
  -f deployment/agentcore/Dockerfile \
  -t texas-energy-private-agent:review .
```

The Dockerfile explicitly installs an official CPU-only PyTorch wheel for
Linux ARM64. It must not resolve CUDA toolkit packages in the AgentCore image.

The runtime needs these non-secret environment variables:

| Variable | Purpose |
| --- | --- |
| `TEXAS_ENERGY_AGENT_ARTIFACT_BUCKET` | private artifact bucket |
| `TEXAS_ENERGY_AGENT_ARTIFACT_MANIFEST_KEY` | immutable manifest key |
| `TEXAS_ENERGY_AGENT_MODEL_ID` | approved Nova Micro model or approved inference-profile ID |
| `AWS_REGION` | AgentCore/Bedrock/S3 region |

Do not pass credentials, tokens, model outputs, or bucket contents through
environment variables. The AgentCore execution role supplies temporary AWS
credentials using the default SDK provider chain.

## IAM and endpoint protection

`iam-execution-policy.json` is a placeholder-substituted least-privilege
execution policy: S3 `GetObject` restricted to one prefix and `bedrock:InvokeModel`
restricted to the selected Nova model. If an inference profile is selected,
the policy must include its profile ARN and the required foundation-model ARN
per the current Bedrock inference-profile requirements.

`iam-trust-policy.json` contains source-account/source-ARN confused-deputy
conditions. Substitute account/region only during a reviewed deployment; do
not store them in this repository.

Create the runtime with a required JWT authorizer (for example, a separately
approved Cognito user pool) and an Authorization-header allowlist. An
authenticated managed endpoint is suitable for the first private demo, but it
is not network-private. A production VPC-only design requires separate review
of AgentCore VPC mode, S3/Bedrock connectivity, and the resulting endpoint or
NAT costs. Never create an unauthenticated endpoint.

## Reviewed deployment and teardown sequence

1. Review the generated manifest and upload exactly its listed objects to the
   approved private prefix.
2. Create/review the execution role from the two JSON templates.
3. Build and push the ARM64 image to a dedicated private ECR repository.
4. Configure AgentCore Runtime HTTP with the execution role, the four
   non-secret variables, a mandatory JWT authorizer, bounded request timeout,
   and CloudWatch retention/KMS configuration.
5. Create an endpoint, wait for `READY`, then make one authenticated bounded
   invocation. Verify seven tools are available through the preserved agent
   loop and that responses are historical/documentary only.
6. Teardown in reverse: disable/delete endpoint, delete runtime, delete the
   private image and the `private-agent/v1/` prefix only after retention
   review, then delete the dedicated role/policies if no runtime remains.

The AgentCore CLI documents `agentcore deploy` for its generated project
layout; this repository intentionally does not run that command before a
reviewed identity, ECR repository, and private artifact location exist.

## Cost guardrails

AgentCore microVM Runtime consumption pricing has no stated minimum standing
fee, bills actual CPU/memory per second, and reclaims idle memory after 120
seconds. ECR and S3 storage, CloudWatch logs, data transfer, and Bedrock tokens
remain billable independently. For an initial one-request demo, use a hard
operational cap of one invocation, four model turns, six tool calls, and 256
output tokens per turn; the earlier local Nova Micro demonstration was roughly
sub-cent, but that observation is not a price guarantee. Keep the first
deployment below a $5 monthly alert threshold, use retention-limited logs, and
delete the ECR image/artifact prefix after validation if the demo is not kept.
AWS Budgets alerts; it does not enforce a hard spending cap.

Recheck the official AgentCore, Bedrock, S3, ECR, and CloudWatch pricing pages
immediately before deployment because service pricing, regional support, and
model availability can change.
