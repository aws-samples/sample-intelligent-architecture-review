# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# Root module - Module Orchestration
#
# Single reusable root. Per-deploy values come from a -var-file (tfvars/)
# and remote state from -backend-config (backend-config/). Module paths are
# ./modules/* because this root now lives at infrastructure/ (not
# infrastructure/environments/<name>/).
###############################################################################

data "aws_caller_identity" "current" {}

# Shared secret proving a request reached the ALB via CloudFront. Generated here
# so the stack is locked down by default rather than depending on an operator
# remembering to set a value. Kept in state (already sensitive) and passed to both
# the ALB listener rule and the CloudFront origin.
resource "random_password" "origin_verify" {
  length  = 48
  special = false
}

locals {
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
  }

  # Derive the account id from the caller identity so the deploy works
  # in any account without editing tfvars (a hardcoded id previously
  # drifted from the account actually being deployed into).
  # var.aws_account_id stays as an optional override for cross-account
  # edge cases.
  aws_account_id = var.aws_account_id != "" ? var.aws_account_id : data.aws_caller_identity.current.account_id

  # Compose the ECS image URI from the resolved account + region so the
  # account id isn't baked into a string literal. Overridable via
  # var.container_image (e.g. to pin a specific tag/digest).
  container_image = var.container_image != "" ? var.container_image : "${local.aws_account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.project_name}:latest"

  # Resource name prefix reused across modules + env-var values.
  name_prefix = "${var.project_name}-${var.environment}"
}

###############################################################################
# KMS - customer-managed key for application data at rest
###############################################################################

module "kms" {
  source = "./modules/kms"

  project_name = var.project_name
  environment  = var.environment
  tags         = local.common_tags
}

###############################################################################
# VPC
###############################################################################

module "vpc" {
  source = "./modules/vpc"

  project_name         = var.project_name
  environment          = var.environment
  vpc_cidr             = var.vpc_cidr
  public_subnet_cidrs  = var.public_subnet_cidrs
  private_subnet_cidrs = var.private_subnet_cidrs
  tags                 = local.common_tags
}

###############################################################################
# ECS (Fargate)
###############################################################################

module "ecs" {
  source = "./modules/ecs"

  project_name       = var.project_name
  environment        = var.environment
  aws_region         = var.aws_region
  vpc_id             = module.vpc.vpc_id
  public_subnet_ids  = module.vpc.public_subnet_ids
  private_subnet_ids = module.vpc.private_subnet_ids
  container_image    = local.container_image
  container_port     = var.container_port
  task_cpu           = var.task_cpu
  task_memory        = var.task_memory
  pptx_task_cpu      = var.pptx_task_cpu
  pptx_task_memory   = var.pptx_task_memory
  pptx_container_env = var.pptx_container_env
  desired_count      = var.desired_count
  min_capacity       = var.min_capacity
  max_capacity       = var.max_capacity
  health_check_path  = "/health"
  certificate_arn    = var.certificate_arn
  log_retention_days = var.log_retention_days
  tags               = local.common_tags

  # Only CloudFront-borne requests reach the app (see the ecs module).
  origin_verify_secret       = random_password.origin_verify.result
  verify_origin_header       = var.verify_origin_header
  restrict_alb_to_cloudfront = var.restrict_alb_to_cloudfront
  alb_deletion_protection    = var.alb_deletion_protection

  kms_key_arn = module.kms.key_arn

  # Tracing: the task role needs X-Ray write access to export OTel spans.
  enable_tracing = true

  container_environment = concat([
    {
      name  = "ENVIRONMENT"
      value = var.environment
    },
    {
      name  = "AWS_REGION"
      value = var.aws_region
    },
    {
      name  = "DYNAMODB_TABLE_PREFIX"
      value = local.name_prefix
    },
    {
      name  = "S3_DOCUMENTS_BUCKET"
      value = module.s3.documents_bucket_id
    },
    {
      name  = "KNOWLEDGE_BASE_ID"
      value = module.knowledge_base.knowledge_base_id
    },
    {
      name  = "DATA_SOURCE_ID"
      value = module.knowledge_base.data_source_id
    },
    {
      name  = "BEDROCK_MODEL_ID"
      value = var.bedrock_model_id
    },
    {
      # OpenTelemetry service name — the identity every span, and therefore the
      # CloudWatch GenAI observability view, is grouped under.
      name  = "OTEL_SERVICE_NAME"
      value = local.name_prefix
    },
    {
      # Puts the ADOT distro in "agent observability" mode, which is what makes
      # tracing work here WITHOUT a collector sidecar. It switches the OTLP trace
      # endpoint to https://xray.<region>.amazonaws.com/v1/traces (signed with
      # SigV4 by the distro's OtlpAwsSpanExporter) and turns on GenAI
      # message-content capture, so prompts, tool calls and token usage appear on
      # the spans. The distro's DEFAULT is OTLP to localhost:4317 — a collector
      # that does not exist in this task, which fails export every few seconds.
      name  = "AGENT_OBSERVABILITY_ENABLED"
      value = "true"
    },
    {
      # REQUIRED, and not optional politeness: several OpenTelemetry
      # distro/configurator entry points are installed once the ADOT distro is
      # present, and the loader picks whichever it resolves first. When the
      # vanilla one wins it logs only
      #   "Configuration of aws_configurator not loaded, configurator already loaded"
      # and the AWS distro's _configure() NEVER RUNS — so
      # AGENT_OBSERVABILITY_ENABLED is ignored, the X-Ray endpoint is never set,
      # and every signal falls back to OTLP on localhost:4317 (no collector
      # exists in this task). Selecting both explicitly is what makes the AWS
      # configuration deterministic. Verified: without these the resolved
      # OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is null; with them it is
      # https://xray.<region>.amazonaws.com/v1/traces.
      name  = "OTEL_PYTHON_DISTRO"
      value = "aws_distro"
    },
    {
      name  = "OTEL_PYTHON_CONFIGURATOR"
      value = "aws_configurator"
    },
    {
      # Logs already reach CloudWatch through the awslogs driver, so the OTLP log
      # exporter would be a redundant second path that also needs its own log
      # group headers to work. Off, rather than left failing in the background.
      name  = "OTEL_LOGS_EXPORTER"
      value = "none"
    },
    {
      # Metrics come from the CloudWatch alarms in ./modules/monitoring. The
      # distro would otherwise default this mode to the EMF exporter, adding a
      # second metrics path with its own permissions to get wrong.
      name  = "OTEL_METRICS_EXPORTER"
      value = "none"
    },
    {
      # Correlate log lines with the active trace/span so a CloudWatch Logs
      # Insights query can pivot straight from an error to its trace.
      name  = "OTEL_PYTHON_LOG_CORRELATION"
      value = "true"
    },
    {
      # Guardrail applied to every Bedrock call. Empty string when the guardrail
      # is disabled; the backend treats empty as "not configured" and omits
      # guardrailConfig rather than failing, so local runs work unchanged.
      name  = "BEDROCK_GUARDRAIL_ID"
      value = var.enable_bedrock_guardrail ? module.bedrock_guardrail[0].guardrail_id : ""
    },
    {
      # A numbered version, never DRAFT: DRAFT is mutable, so invoking against
      # it would change behavior whenever the policy is edited.
      name  = "BEDROCK_GUARDRAIL_VERSION"
      value = var.enable_bedrock_guardrail ? module.bedrock_guardrail[0].guardrail_version : ""
    },
    {
      name  = "PROJECT_NAME"
      value = var.project_name
    },
    {
      name  = "COGNITO_USER_POOL_ID"
      value = module.cognito.user_pool_id
    },
    {
      name  = "COGNITO_WEB_APP_CLIENT_ID"
      value = module.cognito.web_app_client_id
    },
    {
      # Async curator chat job table.
      name  = "CHAT_JOBS_TABLE"
      value = module.dynamodb.chat_jobs_table_name
    },
    {
      # References table.
      name  = "REFERENCES_TABLE"
      value = module.dynamodb.references_table_name
    },
    {
      # User-roles table.
      name  = "USER_ROLES_TABLE"
      value = module.dynamodb.user_roles_table_name
    },
    {
      # Audit trail — append-only who-did-what event log.
      name  = "AUDIT_EVENTS_TABLE"
      value = module.dynamodb.audit_events_table_name
    },
    {
      # Custom on-prem icon legend.
      name  = "ICON_LEGEND_TABLE"
      value = module.dynamodb.icon_legend_table_name
    },
    {
      # Seed admin(s). Comma-separated emails that always
      # resolve to admin, so the first admin exists before any role rows are
      # written. Set via var.bootstrap_admin_email in tfvars.
      name  = "BOOTSTRAP_ADMIN_EMAIL"
      value = var.bootstrap_admin_email
    },
    {
      # pptx-converter sidecar URL via Cloud Map.
      name  = "PPTX_CONVERTER_URL"
      value = "http://pptx-converter.${local.name_prefix}.local:8001"
    },
    {
      # Security: lock CORS to the frontend origin instead of the "*"
      # default. Set var.cors_origins to the CloudFront URL in tfvars
      # (can't reference module.cloudfront here — it depends on the ALB,
      # which would cycle). Two-step apply on a fresh account.
      name  = "CORS_ORIGINS"
      value = var.cors_origins
    },
    # ---- static base env ends here ----
    ],
    # Deploy-time extraction/validation tuning knobs. The backend reads a set
    # of optional env vars (EXTRACTION_*, VALIDATION_*, RULES_PER_*,
    # ENABLE_THINKING_*, *_MODEL_ID, ...) that otherwise fall back to safe code
    # defaults. Rather than a runtime UI (deliberately NOT built — prompt/model
    # changes to a compliance tool should be a reviewed, versioned deploy
    # change, not a live toggle), any of these is overridable here via
    # var.extra_container_env in tfvars. Empty map → no extra entries → nothing
    # changes. See infrastructure/EXTRACTION_TUNING.md.
    [for k, v in var.extra_container_env : { name = k, value = tostring(v) }]
  )
}

###############################################################################
# CloudFront (must be created before S3 for OAC ARN)
###############################################################################

module "cloudfront" {
  source = "./modules/cloudfront"

  project_name                   = var.project_name
  environment                    = var.environment
  s3_bucket_regional_domain_name = module.s3.frontend_bucket_regional_domain_name
  alb_dns_name                   = module.ecs.alb_dns_name
  acm_certificate_arn            = var.cloudfront_acm_certificate_arn
  origin_verify_secret           = random_password.origin_verify.result
  verify_origin_header           = var.verify_origin_header
  alb_has_certificate            = var.certificate_arn != ""
  tags                           = local.common_tags
}

###############################################################################
# S3 Buckets
###############################################################################

module "s3" {
  source = "./modules/s3"

  project_name                = var.project_name
  environment                 = var.environment
  aws_account_id              = local.aws_account_id
  cloudfront_distribution_arn = module.cloudfront.distribution_arn
  tags                        = local.common_tags
}

###############################################################################
# DynamoDB
###############################################################################

module "dynamodb" {
  source = "./modules/dynamodb"

  project_name = var.project_name
  environment  = var.environment
  kms_key_arn  = module.kms.key_arn
  tags         = local.common_tags
}

###############################################################################
# Bedrock IAM
###############################################################################

module "bedrock" {
  source = "./modules/bedrock"

  project_name       = var.project_name
  environment        = var.environment
  aws_region         = var.aws_region
  ecs_task_role_name = module.ecs.task_role_name
  tags               = local.common_tags
}

###############################################################################
# CloudTrail
###############################################################################

module "cloudtrail" {
  source = "./modules/cloudtrail"
  count  = var.enable_cloudtrail ? 1 : 0

  project_name       = var.project_name
  environment        = var.environment
  aws_region         = var.aws_region
  retention_days     = var.cloudtrail_retention_days
  log_retention_days = var.log_retention_days
  tags               = local.common_tags
}

###############################################################################
# Bedrock Guardrail
###############################################################################

module "bedrock_guardrail" {
  source = "./modules/bedrock-guardrail"
  count  = var.enable_bedrock_guardrail ? 1 : 0

  project_name       = var.project_name
  environment        = var.environment
  ecs_task_role_name = module.ecs.task_role_name
  tags               = local.common_tags
}

###############################################################################
# Bedrock Knowledge Base
###############################################################################

module "knowledge_base" {
  source = "./modules/knowledge-base"

  project_name  = var.project_name
  environment   = var.environment
  s3_bucket_arn = module.s3.documents_bucket_arn
  tags          = local.common_tags
}

###############################################################################
# Cognito User Pool (auth provider for the API)
###############################################################################

# Pre-token-generation Lambda: SAML groups -> cognito:groups. Provisioned
# always (cheap, idle when SAML is off); its ARN is fed into the pool trigger.
module "lambda_pretoken" {
  source = "./modules/lambda-pretoken"

  project_name = var.project_name
  environment  = var.environment
  tags         = local.common_tags
}

module "cognito" {
  source = "./modules/cognito"

  project_name      = var.project_name
  environment       = var.environment
  cloudfront_domain = module.cloudfront.distribution_domain_name
  enable_hosted_ui  = true
  tags              = local.common_tags

  # SAML federation (Entra-ready; mocksaml test IdP for now). Gated by
  # enable_saml_idp so non-federated envs are untouched.
  enable_saml_idp          = var.enable_saml_idp
  saml_provider_name       = var.saml_provider_name
  saml_metadata_url        = var.saml_metadata_url
  pre_token_lambda_arn     = module.lambda_pretoken.function_arn
  enable_pre_token_trigger = true
}

###############################################################################
# Cognito SSM parameters
# Non-secret values are plain String; the server-client secret is SecureString.
# The ECS task reads these at runtime; the frontend build reads the public ones.
###############################################################################

resource "aws_ssm_parameter" "cognito_user_pool_id" {
  name  = "/${var.project_name}/${var.environment}/cognito/user-pool-id"
  type  = "String"
  value = module.cognito.user_pool_id
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "cognito_web_app_client_id" {
  name  = "/${var.project_name}/${var.environment}/cognito/web-app-client-id"
  type  = "String"
  value = module.cognito.web_app_client_id
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "cognito_user_pool_client_id" {
  name  = "/${var.project_name}/${var.environment}/cognito/user-pool-client-id"
  type  = "String"
  value = module.cognito.user_pool_client_id
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "cognito_client_secret" {
  name   = "/${var.project_name}/${var.environment}/cognito/client-secret"
  type   = "SecureString"
  key_id = module.kms.key_arn
  value  = module.cognito.user_pool_client_secret
  tags   = local.common_tags
}

resource "aws_ssm_parameter" "cognito_hosted_ui_url" {
  name  = "/${var.project_name}/${var.environment}/cognito/hosted-ui-url"
  type  = "String"
  value = module.cognito.hosted_ui_url != null ? module.cognito.hosted_ui_url : ""
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "cognito_discovery_url" {
  name  = "/${var.project_name}/${var.environment}/cognito/discovery-url"
  type  = "String"
  value = module.cognito.discovery_url
  tags  = local.common_tags
}

# Hosted-UI domain (bare host, no scheme) + SAML provider name — read at
# frontend build time so Amplify's OAuth redirect config points at the right
# domain and "Sign in with SSO" targets the right identity provider. Empty
# provider means the SSO button is hidden.
resource "aws_ssm_parameter" "cognito_hosted_ui_domain" {
  name  = "/${var.project_name}/${var.environment}/cognito/hosted-ui-domain"
  type  = "String"
  value = module.cognito.hosted_ui_url != null ? replace(module.cognito.hosted_ui_url, "https://", "") : ""
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "cognito_saml_provider" {
  # Skip when SAML is disabled: saml_provider_name is "" then, and SSM rejects an
  # empty String value (min length 1). The frontend build tolerates the
  # parameter's absence (deploy.sh: `|| SAML_PROVIDER=""`), which hides the SSO
  # button — the correct behaviour when there is no IdP.
  count = module.cognito.saml_provider_name != "" ? 1 : 0
  name  = "/${var.project_name}/${var.environment}/cognito/saml-provider"
  type  = "String"
  value = module.cognito.saml_provider_name
  tags  = local.common_tags
}

###############################################################################
# Distributed tracing (always on)
#
# Turns on CloudWatch Transaction Search so the OpenTelemetry spans the ECS task
# emits are indexed in the `aws/spans` log group, which is what powers the
# CloudWatch Application Signals / GenAI observability views and makes the
# agentic work (extraction, validation, query) traceable end to end.
#
# This is core rather than optional: the alarms in ./modules/monitoring tell you
# THAT something regressed, and traces are what tell you WHERE. It replaced the
# AgentCore-runtime-scoped observability wiring, which was the only genuinely
# portable piece of that stack — everything else there existed to serve a managed
# runtime the application does not need (see docs/agentcore-decision.md).
###############################################################################

module "tracing" {
  source = "./modules/tracing"

  project_name = var.project_name
  environment  = var.environment
  aws_region   = var.aws_region
  # Sampling stays configurable because Transaction Search is billed on ingested
  # spans; 100% is right for dev and low-traffic compliance review, not for a
  # high-volume production account.
  span_sampling_percentage = var.trace_sampling_percentage
  tags                     = local.common_tags
}

###############################################################################
# Monitoring
###############################################################################

module "monitoring" {
  source = "./modules/monitoring"

  project_name     = var.project_name
  environment      = var.environment
  aws_region       = var.aws_region
  ecs_cluster_name = module.ecs.cluster_name
  ecs_service_name = module.ecs.service_name
  alb_arn_suffix   = module.ecs.alb_arn_suffix
  # The metric filter for API errors has to watch the log group the tasks
  # actually write to.
  app_log_group_name = module.ecs.log_group_name
  log_retention_days = var.log_retention_days
  kms_key_arn        = module.kms.key_arn
  tags               = local.common_tags
}
