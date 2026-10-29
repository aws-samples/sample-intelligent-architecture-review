# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Cognito Module — Architecture Compliance Review
# Adapted from market-surveillance reference implementation.
#
# Provisions a Cognito User Pool plus two app clients:
#   - server client (with secret) for backend service-to-service auth
#   - web app client (no secret) for the browser-facing Next.js app

data "aws_caller_identity" "current" {}

locals {
  user_pool_name = "${var.project_name}-user-pool-${var.environment}"
  client_name    = "${var.project_name}-app-client-${var.environment}"
  domain_prefix  = "${var.project_name}-${var.environment}-${data.aws_caller_identity.current.account_id}"
}

# Cognito User Pool
resource "aws_cognito_user_pool" "this" {
  name = local.user_pool_name

  # Username configuration
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Password policy
  password_policy {
    minimum_length                   = var.password_min_length
    require_lowercase                = var.password_require_lowercase
    require_uppercase                = var.password_require_uppercase
    require_numbers                  = var.password_require_numbers
    require_symbols                  = var.password_require_symbols
    temporary_password_validity_days = 7
  }

  # Account recovery via verified email only
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Email verification template
  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Architecture Compliance Review - Verify your email"
    email_message        = "Your verification code is {####}"
  }

  # No self-registration. This is an internal tool whose users are provisioned
  # by an admin (or federated from the IdP) — leaving signup open would let
  # anyone on the internet create an account, which combined with role
  # resolution from token claims is a privilege-escalation path.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # A role can be resolved from the email claim (the bootstrap-admin list), so
  # a user must not be able to silently re-point their own email at a
  # privileged address. Requiring verification means the change doesn't take
  # effect on the token until the new address is proven.
  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }

  # Email schema
  schema {
    name                     = "email"
    attribute_data_type      = "String"
    required                 = true
    mutable                  = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }

  # Custom attribute the SAML IdP maps its group assertion into (via the
  # identity provider's attribute_mapping). The pre-token Lambda reads
  # custom:saml_groups and emits cognito:groups from it. Additive — does not
  # disturb the email schema. Cognito caps custom string attrs at 2048 chars.
  schema {
    name                     = "saml_groups"
    attribute_data_type      = "String"
    required                 = false
    mutable                  = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 0
      max_length = 2048
    }
  }

  # Pre-token-generation trigger (SAML groups -> cognito:groups). Gated on a
  # static bool (not the ARN) because for_each must be known at plan time —
  # the Lambda ARN is unknown on first apply.
  dynamic "lambda_config" {
    for_each = var.enable_pre_token_trigger ? [1] : []
    content {
      pre_token_generation = var.pre_token_lambda_arn
    }
  }

  mfa_configuration   = "OFF"
  deletion_protection = var.environment == "prod" ? "ACTIVE" : "INACTIVE"

  tags = merge(var.tags, {
    Name = local.user_pool_name
  })
}

# RBAC groups — one per application role. The group NAMES are a contract with
# the backend: app/api/rbac.py resolves a caller's role from the token's
# `cognito:groups` claim by lower-casing each group and matching it against the
# Role enum ({admin, validator, curator, viewer}). They must stay exactly these
# lower-case strings. Membership makes `resolve_role` branch 1 (the groups
# claim) live; it takes priority over the user-roles DDB table. `precedence`
# only orders the claim (lower = earlier); resolve_role does its own
# highest-privilege selection, so correctness does not depend on it.
resource "aws_cognito_user_group" "admin" {
  name         = "admin"
  user_pool_id = aws_cognito_user_pool.this.id
  description  = "Full access incl. user-role management"
  precedence   = 1
}

resource "aws_cognito_user_group" "curator" {
  name         = "curator"
  user_pool_id = aws_cognito_user_pool.this.id
  description  = "Standards library + rule curation"
  precedence   = 2
}

resource "aws_cognito_user_group" "validator" {
  name         = "validator"
  user_pool_id = aws_cognito_user_pool.this.id
  description  = "Upload SDDs + run validation"
  precedence   = 3
}

resource "aws_cognito_user_group" "viewer" {
  name         = "viewer"
  user_pool_id = aws_cognito_user_pool.this.id
  description  = "Read-only + chat"
  precedence   = 4
}

# SAML identity provider (gated). provider_type=SAML federates any SAML 2.0
# IdP via its metadata; for the test phase this is mocksaml.com, and the
# Entra cutover is a metadata-URL + attribute-name swap. The attribute mapping
# lands the IdP's email + group assertion into the pool's email and
# custom:saml_groups attributes; the pre-token Lambda turns the latter into
# cognito:groups. Group names must ultimately match the Role enum (handled in
# the Lambda's GROUP_MAP, not here).
resource "aws_cognito_identity_provider" "saml" {
  count = var.enable_saml_idp ? 1 : 0

  user_pool_id  = aws_cognito_user_pool.this.id
  provider_name = var.saml_provider_name
  provider_type = "SAML"

  provider_details = {
    MetadataURL = var.saml_metadata_url
    IDPSignout  = "false"
  }

  attribute_mapping = {
    email                = var.saml_email_attribute
    "custom:saml_groups" = var.saml_groups_attribute
  }
}

# Allow the user pool to invoke the pre-token Lambda. Lives in this module
# (not lambda-pretoken) because the pool ARN is local here and the Lambda ARN
# is an input — putting it here breaks what would otherwise be a module cycle.
resource "aws_lambda_permission" "pretoken_invoke" {
  count = var.enable_pre_token_trigger ? 1 : 0

  statement_id  = "AllowCognitoPreTokenInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.pre_token_lambda_arn
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.this.arn
}

# Server-side app client — generates a secret, used by the FastAPI backend
# to obtain a client-credentials token for machine-to-machine calls.
resource "aws_cognito_user_pool_client" "this" {
  name         = local.client_name
  user_pool_id = aws_cognito_user_pool.this.id

  access_token_validity  = var.token_validity_hours
  id_token_validity      = var.id_token_validity_hours
  refresh_token_validity = var.refresh_token_validity_days

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  allowed_oauth_flows                  = ["code", "implicit"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["email", "openid", "profile"]
  supported_identity_providers         = ["COGNITO"]

  # Server flows only need localhost — backend never redirects.
  callback_urls = ["https://localhost:3000/callback"]
  logout_urls   = ["https://localhost:3000/logout"]

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH",
  ]

  prevent_user_existence_errors = "ENABLED"
  generate_secret               = true

  read_attributes  = ["email", "email_verified", "name"]
  write_attributes = ["email", "name"]
}

# Web app client — no secret, used by the browser via Amplify.
# Browsers cannot securely store a client secret.
resource "aws_cognito_user_pool_client" "web_app" {
  name         = "${local.client_name}-web"
  user_pool_id = aws_cognito_user_pool.this.id

  access_token_validity  = var.token_validity_hours
  id_token_validity      = var.id_token_validity_hours
  refresh_token_validity = var.refresh_token_validity_days

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  allowed_oauth_flows                  = ["code", "implicit"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["email", "openid", "profile"]

  # Keep COGNITO (email/password + hosted UI) and add the SAML provider when
  # enabled, so both login paths work: seeded users via password, federated
  # users via "Sign in with SSO". concat keeps COGNITO first.
  supported_identity_providers = concat(
    ["COGNITO"],
    var.enable_saml_idp ? [var.saml_provider_name] : [],
  )

  # Root "/" is added as a signout target so the hosted-UI logout can return
  # to the app home (Amplify redirectSignOut).
  callback_urls = compact([
    var.cloudfront_domain != "" ? "https://${var.cloudfront_domain}/callback" : "",
    "https://localhost:3000/callback",
    "http://localhost:3000/callback",
  ])
  logout_urls = compact([
    var.cloudfront_domain != "" ? "https://${var.cloudfront_domain}/" : "",
    var.cloudfront_domain != "" ? "https://${var.cloudfront_domain}/logout" : "",
    "https://localhost:3000/logout",
    "http://localhost:3000/logout",
  ])

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH",
  ]

  prevent_user_existence_errors = "ENABLED"
  generate_secret               = false

  read_attributes = ["email", "email_verified", "name", "custom:saml_groups"]

  # custom:saml_groups MUST be writable, even though it feeds RBAC's
  # highest-precedence role source. Cognito populates an IdP-mapped attribute by
  # *writing it as this app client*, so per the CreateUserPoolClient contract
  # ("If your app client lacks write access to a mapped attribute, Amazon
  # Cognito throws an error when it attempts to update the attribute") omitting
  # it does not harden anything — it silently prevents the SAML group claim from
  # ever landing. Every federated user then has no groups attribute, the
  # pre-token Lambda emits no cognito:groups, and resolve_role falls through to
  # `viewer`. Group-driven RBAC cannot function without this.
  #
  # Self-assignment is prevented by controls that actually hold:
  #   1. _role_from_groups (app/api/rbac.py) ignores cognito:groups unless the
  #      token carries an `identities` claim, i.e. only on a federated token.
  #      A password user who wrote this attribute is not believed.
  #   2. UpdateUserAttributes needs the aws.cognito.signin.user.admin scope,
  #      which is absent from this client's allowed_oauth_scopes — so a
  #      federated user's OAuth access token cannot call it at all.
  #   3. Cognito overwrites mapped attributes from the assertion on every
  #      federated sign-in, so any locally-set value does not survive.
  #   4. The pre-token Lambda keeps only values naming a real role.
  write_attributes = ["email", "name", "custom:saml_groups"]

  # The client references the SAML provider by name in
  # supported_identity_providers, so the provider must exist first.
  depends_on = [aws_cognito_identity_provider.saml]
}

# Hosted UI domain — provides /login, /token, /logout OAuth2 endpoints.
resource "aws_cognito_user_pool_domain" "this" {
  count = var.enable_hosted_ui ? 1 : 0

  domain       = local.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}
