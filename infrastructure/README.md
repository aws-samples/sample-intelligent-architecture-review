# Architecture Compliance Review - Infrastructure

Terraform infrastructure for the AI-based HA (High Availability) Policy Validation Tool deployed on AWS.

## Architecture

- **VPC**: Multi-AZ network with public/private subnets, NAT Gateway, Internet Gateway
- **ECS Fargate**: Containerized API backend with auto-scaling (1-4 tasks)
- **ALB**: Application Load Balancer with HTTPS termination
- **S3**: Document storage (private, encrypted) and frontend static hosting
- **DynamoDB**: Documents, validations, and queries tables (on-demand billing)
- **Bedrock**: IAM permissions for Claude model invocation
- **OpenSearch Serverless**: Vector search collection for RAG
- **CloudFront**: CDN with OAC for frontend distribution
- **Monitoring**: CloudWatch dashboards, alarms, and log groups

## Directory Structure

```
infrastructure/
├── main.tf           # Root module — orchestrates all modules
├── variables.tf      # Input variables (project_name/environment required, no default)
├── outputs.tf        # Stack outputs
├── providers.tf      # AWS provider + version pins
├── backend.tf        # S3 remote-state backend (partial config)
├── tfvars/           # Per-target var-files (only *.example committed)
│   └── dev.example.tfvars
├── backend-config/   # Per-target S3 backend configs (only *.example committed)
│   └── dev.example.s3.tfbackend
├── modules/          # Reusable Terraform modules
│   ├── vpc/          # Network infrastructure
│   ├── ecs/          # Container orchestration + ALB + pptx-converter sidecar
│   ├── s3/           # Object storage
│   ├── dynamodb/     # NoSQL database tables
│   ├── bedrock/      # AI model access
│   ├── knowledge-base/ # Bedrock KB + OpenSearch Serverless vector index
│   ├── cognito/      # Auth (user pool + hosted UI)
│   ├── cloudfront/   # CDN
│   ├── tracing/      # CloudWatch Transaction Search (OTel span indexing)
│   └── monitoring/   # Alarms, dashboard, metric filters
└── README.md
```

This is a **single reusable root**: one `infrastructure/` deployed to any
account/project by supplying a different `tfvars/<target>.tfvars`. There are
no per-environment `.tf` copies.

## Observability and evaluation

Both are part of the single deployed stack — there is no flag to remember:

- **`modules/tracing`** turns on CloudWatch Transaction Search so the
  OpenTelemetry spans the backend emits (its container runs under
  `opentelemetry-instrument`) are indexed into the `aws/spans` log group behind
  the Application Signals / GenAI observability views. Only the sampling rate is
  configurable (`trace_sampling_percentage`), since indexed spans are billed.
  Note this is an **account- and region-wide** setting; see the module header.
- **`backend/evaluation/run_eval.py`** scores the deployed API against a golden
  set with a Bedrock judge and exits non-zero below a threshold, so it can gate a
  release. Opt-in, because it incurs model usage.

A managed agent runtime (Amazon Bedrock AgentCore) was evaluated for the query
agent and removed: it relieved none of the real scaling pressure, which lives in
the validation fan-out and extraction loops that run on ECS regardless. The
tracing and evaluation capabilities above are what survived that evaluation. The
full rationale is in
[../docs/agentcore-decision.md](../docs/agentcore-decision.md).

## Prerequisites

- Terraform >= 1.5.0
- AWS CLI configured with appropriate credentials
- ACM certificate for HTTPS (ALB)
- ECR repository with application Docker image

## Quick Start

The easiest path is `./deploy.sh` from the repo root (it creates the state
bucket, wires the backend config, and runs plan/apply). To run Terraform
directly:

```bash
cd infrastructure

# 1. Create a deploy config (once per target).
cp tfvars/dev.example.tfvars tfvars/dev.tfvars           # then edit values
cp backend-config/dev.example.s3.tfbackend backend-config/dev.s3.tfbackend

# 2. Init with the S3 backend (partial config) + apply with the var-file.
terraform init -backend-config=backend-config/dev.s3.tfbackend
terraform plan  -var-file=tfvars/dev.tfvars
terraform apply -var-file=tfvars/dev.tfvars
```

The KB index bootstrap provisioner needs `opensearch-py`/`boto3`:
`pip install -r modules/knowledge-base/requirements.txt`.

## Configuration

Edit your `tfvars/<target>.tfvars` (copied from `dev.example.tfvars`).
Required (no defaults):

- `project_name` — prefixes every resource; ≤ 21 chars (AOSS name limit)
- `environment` — e.g. dev/test/prod; ≤ 11 chars

Common optional overrides (see `variables.tf` for the full list):

- `aws_region` - defaults to us-east-1
- `aws_account_id` - derived from caller identity if empty
- `container_image` - ECR image URI; derived if empty
- `bedrock_model_id` - model selection
- `task_cpu` / `task_memory` / `desired_count` / `min_capacity` / `max_capacity`
- `certificate_arn` / `cloudfront_acm_certificate_arn` - HTTPS certs

## Security

- All S3 buckets block public access
- ECS tasks run in private subnets
- Security groups follow least-privilege principle
- Encryption at rest enabled for S3 and DynamoDB
- OpenSearch collection uses VPC-only network access
- HTTPS enforced on ALB and CloudFront

## Tagging

All resources are tagged with:
- `Project = <project_name>`
- `Environment = <environment>`
- `ManagedBy = "terraform"`
