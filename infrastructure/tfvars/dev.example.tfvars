###############################################################################
# Example deploy config — copy to <your-target>.tfvars and edit.
#
#   cp tfvars/dev.example.tfvars tfvars/dev.tfvars
#   # edit values, then:
#   PROJECT=<project> ENVIRONMENT=<env> ./deploy.sh all
#
# Real per-target tfvars (everything except *.example.tfvars) are gitignored
# so account-specific values never get committed.
###############################################################################

# REQUIRED — no defaults. project_name prefixes every resource; keep <= 21
# chars so <project>-<env>-kb-data stays under the AOSS 32-char name limit.
project_name = "arch-compliance"
environment  = "dev"

aws_region = "us-east-1"

# Account id is derived from the caller identity by default — leave empty
# unless doing a cross-account deploy.
# aws_account_id = ""

# Networking (defaults shown)
vpc_cidr             = "10.0.0.0/16"
public_subnet_cidrs  = ["10.0.1.0/24", "10.0.2.0/24"]
private_subnet_cidrs = ["10.0.11.0/24", "10.0.12.0/24"]

# Application sizing (defaults shown)
# task_cpu      = 2048
# task_memory   = 4096
# desired_count = 1
# min_capacity  = 1
# max_capacity  = 4

# Bedrock models (defaults shown)
# bedrock_model_id = "us.anthropic.claude-sonnet-4-6"

# TLS — empty means HTTP-only ALB + default CloudFront cert.
certificate_arn                = ""
cloudfront_acm_certificate_arn = ""

# CORS: allowed API origins. Two-step on a fresh account — apply once with
# "*", then set this to the printed cloudfront_domain_name and re-apply.
# Locking this to the real frontend origin is strongly recommended in prod.
#   cors_origins = "https://<your-distribution>.cloudfront.net"
cors_origins = "*"

# RBAC — comma-separated emails that always resolve to admin (seeds the first
# admin before any role rows / group membership exist).
bootstrap_admin_email = "admin@example.com"

# SAML federation (Cognito). Off by default. Set enable_saml_idp=true and point
# saml_metadata_url at an IdP's federation metadata to add "Sign in with SSO".
# Test IdP (no groups → federated users resolve to viewer):
#   enable_saml_idp    = true
#   saml_provider_name = "MockSAML"
#   saml_metadata_url  = "https://mocksaml.com/api/saml/metadata"
# Entra cutover (see docs/RBAC.md): set provider_name="EntraID", metadata_url to
# the App Federation Metadata Url, and map Entra group GUIDs in the pre-token
# Lambda's GROUP_MAP. No backend redeploy.
# enable_saml_idp    = false
# saml_provider_name = "MockSAML"
# saml_metadata_url  = ""

