# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# Root module variables
#
# This is a REUSABLE root module: nothing about the project is hardcoded.
# Every deploy supplies its own values via a -var-file (see tfvars/). The
# core identity variables (project_name, environment) intentionally have NO
# default so a new project must name itself explicitly rather than silently
# inheriting "arch-compliance".
###############################################################################

variable "project_name" {
  description = "Project name. Prefixes every resource (e.g. <project>-<env>-cluster). Keep it short: OpenSearch Serverless caps names at 32 chars, so <project>-<env>-kb-data must fit."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project_name))
    error_message = "project_name must be lowercase alphanumeric/hyphen, start with a letter, and be <= 21 chars (keeps AOSS/ALB names under their 32-char limit)."
  }
}

variable "environment" {
  description = "Environment name (e.g. dev, test, prod). Part of every resource name and the state key."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,10}$", var.environment))
    error_message = "environment must be lowercase alphanumeric/hyphen, start with a letter, and be <= 11 chars."
  }
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "aws_account_id" {
  description = "AWS account ID. Leave empty to derive from the caller identity (recommended); set only to override for cross-account deploys."
  type        = string
  default     = ""
}

###############################################################################
# Networking
###############################################################################

variable "vpc_cidr" {
  description = "CIDR block for VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets"
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets"
  type        = list(string)
  default     = ["10.0.11.0/24", "10.0.12.0/24"]
}

###############################################################################
# Application / container
###############################################################################

variable "container_image" {
  description = "Docker image for the ECS application. Leave empty to derive as <account>.dkr.ecr.<region>.amazonaws.com/<project>:latest; set to pin a specific tag or digest."
  type        = string
  default     = ""
}

variable "container_port" {
  description = "Port the application container listens on"
  type        = number
  default     = 8000
}

variable "task_cpu" {
  description = "Fargate task CPU units (1024 = 1 vCPU)"
  type        = number
  default     = 2048
}

variable "task_memory" {
  description = "Fargate task memory (MiB)"
  type        = number
  default     = 4096
}

variable "desired_count" {
  description = "Desired number of ECS tasks"
  type        = number
  default     = 1
}

variable "min_capacity" {
  description = "Minimum ECS task count for autoscaling"
  type        = number
  default     = 1
}

variable "max_capacity" {
  description = "Maximum ECS task count for autoscaling"
  type        = number
  default     = 4
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days. One year by default, so a full annual audit cycle stays queryable."
  type        = number
  default     = 365
}

###############################################################################
# Bedrock models
###############################################################################

variable "bedrock_model_id" {
  description = "Bedrock model ID used by the API validators + chat (BEDROCK_MODEL_ID)."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-6"
}

###############################################################################
# RBAC
###############################################################################

variable "bootstrap_admin_email" {
  description = "Comma-separated emails that always resolve to the admin role (seeds the first admin before any role rows exist). BOOTSTRAP_ADMIN_EMAIL."
  type        = string
  default     = ""
}

###############################################################################
# TLS
###############################################################################

variable "certificate_arn" {
  description = "ARN of the ACM certificate for HTTPS on the ALB. Empty = HTTP only."
  type        = string
  default     = ""
}

variable "cloudfront_acm_certificate_arn" {
  description = "ARN of ACM certificate for CloudFront (must be in us-east-1). Empty = default CloudFront certificate."
  type        = string
  default     = ""
}

variable "cors_origins" {
  description = <<-EOT
    Comma-separated allowed CORS origins for the API (backend CORS_ORIGINS).
    Should be the frontend's CloudFront URL, e.g.
    "https://<distribution-id>.cloudfront.net". Cannot be defaulted to the
    CloudFront domain here because that would create an ecs->cloudfront->ecs
    dependency cycle (CloudFront already depends on the ALB). Two-step on a
    fresh account: apply once with the default, then set this to the
    printed cloudfront_domain_name and re-apply. Default "*" is permissive —
    set it in prod.
  EOT
  type        = string
  default     = "*"
}

###############################################################################
# SAML federation (Cognito) — Entra-ready; mocksaml test IdP for now.
###############################################################################

variable "enable_saml_idp" {
  description = "Attach a SAML identity provider to the Cognito pool + web client. Default off; dev opts in."
  type        = bool
  default     = false
}

variable "saml_provider_name" {
  description = "Cognito identity-provider name for the SAML IdP (e.g. MockSAML, EntraID)."
  type        = string
  default     = "MockSAML"
}

variable "saml_metadata_url" {
  description = "HTTPS URL of the SAML IdP federation metadata. mocksaml: https://mocksaml.com/api/saml/metadata. Entra: App Federation Metadata Url."
  type        = string
  default     = ""
}

###############################################################################
# Extraction / validation tuning (deploy-time; NOT a runtime UI)
###############################################################################

variable "extra_container_env" {
  description = <<-EOT
    Optional extra environment variables merged into the backend container,
    for deploy-time tuning of extraction/validation without a code change.
    The backend reads these (all have safe code defaults); set any to override:
      EXTRACTION_UNIT_MODEL_ID, EXTRACTION_DIAGRAM_MODEL_ID,
      EXTRACTION_SYNTH_MODEL_ID, VALIDATION_MODEL_ID, TIER_CLASSIFIER_MODEL_ID,
      CURATOR_MODEL_ID, RULES_CHAT_MODEL_ID,
      EXTRACTION_PAGES_PER_CHUNK, EXTRACTION_MAX_CONCURRENT,
      EXTRACTION_DIAGRAM_MAX_CONCURRENT, RULES_PER_DOC_BATCH,
      RULES_PER_SLIDE_BATCH, VALIDATION_MAX_CHUNK_CHARS,
      VALIDATION_CHUNK_OVERLAP_CHARS, VALIDATION_MAX_CONCURRENT,
      VALIDATION_MAX_PAGES, VALIDATION_VERIFY_FAILS,
      VALIDATION_VERIFY_SEVERITIES, VALIDATION_VERIFY_MAX_TOKENS,
      ENABLE_THINKING_CHUNKED, ENABLE_THINKING_MULTIMODAL, ENABLE_THINKING_PPTX,
      THINKING_BUDGET_TOKENS, EXTRACTION_TOPOLOGY_VERIFY,
      EXTRACTION_TOPOLOGY_MAX_TOKENS, ENABLE_EXTRACTION_SYNTHESIS,
      ICON_LEGEND_MAX_IMAGES,
      PPTX_VALIDATION_MODEL_ID, MULTIMODAL_VALIDATION_MODEL_ID,
      MULTIMODAL_MAX_CONCURRENT, BEDROCK_READ_TIMEOUT,
      VALIDATION_MAX_DOC_CHARS, VALIDATION_INPUT_USD_PER_MTOK,
      VALIDATION_OUTPUT_USD_PER_MTOK, QUERY_REASONING_BUDGET,
      RULES_CHAT_REASONING_BUDGET, RULES_CHAT_STREAM_MAX_TOKENS,
      TIER_CLASSIFIER_MAX_CHARS, CURATOR_DOCUMENT_TEXT_LIMIT, PPTX_MAX_BYTES,
      ENABLE_TEMPLATE_GUARD, ENABLE_XREF_REASONING.
    The two *_USD_PER_MTOK rates only affect the estimated cost reported on a
    validation; set them to match whichever model VALIDATION_MODEL_ID names, or
    the figure shown in the UI will be wrong.
    Prompt TEXT is intentionally NOT env-tunable — prompt changes are a
    reviewed code change (see infrastructure/EXTRACTION_TUNING.md). Empty by
    default (nothing changes).
  EOT
  type        = map(string)
  default     = {}
}

variable "pptx_task_cpu" {
  description = "Fargate CPU units for the pptx-converter sidecar (LibreOffice conversion is CPU-bound, so it is sized apart from the API task)."
  type        = number
  default     = 1024
}

variable "pptx_task_memory" {
  description = "Fargate memory (MiB) for the pptx-converter sidecar."
  type        = number
  default     = 2048
}

variable "pptx_container_env" {
  description = <<-EOT
    Environment variables for the pptx-converter sidecar. It reads:
      SOFFICE_TIMEOUT_S        per-conversion LibreOffice timeout (default 60).
                               Keep it BELOW the caller's 90s SIDECAR_TIMEOUT_S,
                               or the caller gives up before the sidecar does.
      CONVERTER_TMP_ROOT       scratch directory (default /tmp).
      ALLOWED_URL_HOST_SUFFIX  presigned-URL host allowlist suffix
                               (default .amazonaws.com) — an SSRF guard.
    Empty by default (the sidecar's own defaults apply).
  EOT
  type        = map(string)
  default     = {}
}

variable "enable_bedrock_guardrail" {
  description = <<-EOT
    Create an Amazon Bedrock guardrail and apply it to every model call. On by
    default.

    The policy is deliberately narrow — PII/credential redaction plus
    prompt-attack detection on input, with no content filters and no denied
    topics. This application reads compliance material that legitimately
    discusses firewalls, key management, and how a control could be bypassed, and
    the managed content filters intervene on exactly that language. See
    modules/bedrock-guardrail/main.tf for the reasoning.
  EOT
  type        = bool
  default     = true
}

variable "enable_cloudtrail" {
  description = <<-EOT
    Create a CloudTrail trail with a dedicated S3 bucket, recording management
    events and Amazon Bedrock InvokeModel data events. On by default: the
    application's own audit table records who did what in the app, but not who
    called an AWS API.
  EOT
  type        = bool
  default     = true
}

variable "cloudtrail_retention_days" {
  description = "Days to retain CloudTrail objects before expiry."
  type        = number
  default     = 90
}

variable "verify_origin_header" {
  description = <<-EOT
    Require CloudFront's X-Origin-Verify header at the ALB, so the API cannot be
    reached by hitting the ALB's DNS name directly. On by default. Turn it off
    only to debug the origin directly — the ALB is internet-facing.
  EOT
  type        = bool
  default     = true
}

variable "restrict_alb_to_cloudfront" {
  description = <<-EOT
    Restrict the ALB security group to CloudFront's managed origin-facing prefix
    list, in addition to the origin-verify header. On by default. Only the one
    port CloudFront connects on is opened (443 with an ALB certificate, else 80),
    so the prefix list's ~55 entries fit the default 60-rule security group
    quota. Turn it off only to reach the ALB directly while debugging.
  EOT
  type        = bool
  default     = true
}

variable "alb_deletion_protection" {
  description = "Enable deletion protection on the internet-facing ALB. On by default; set false and apply before terraform destroy."
  type        = bool
  default     = true
}

###############################################################################
# Tracing
#
# Distributed tracing is always on — see ./modules/tracing. Only the indexing
# rate is configurable, because Transaction Search bills per indexed span.
###############################################################################

variable "trace_sampling_percentage" {
  description = "Share of trace spans CloudWatch Transaction Search indexes, 1-100. Indexed spans are billed; 100 suits a low-volume compliance-review workload, lower it for a busy account."
  type        = number
  default     = 100
}
