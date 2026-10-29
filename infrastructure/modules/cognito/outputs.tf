# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

data "aws_region" "current" {}

output "user_pool_id" {
  description = "The ID of the Cognito User Pool"
  value       = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  description = "The ARN of the Cognito User Pool"
  value       = aws_cognito_user_pool.this.arn
}

output "user_pool_endpoint" {
  description = "The endpoint of the Cognito User Pool"
  value       = aws_cognito_user_pool.this.endpoint
}

output "user_pool_client_id" {
  description = "The ID of the server app client (with secret) — for backend service auth"
  value       = aws_cognito_user_pool_client.this.id
}

output "user_pool_client_secret" {
  description = "Secret of the server app client. Sensitive — store in SSM SecureString."
  value       = aws_cognito_user_pool_client.this.client_secret
  sensitive   = true
}

output "web_app_client_id" {
  description = "The ID of the web app client (no secret) — for browser/Amplify"
  value       = aws_cognito_user_pool_client.web_app.id
}

output "saml_provider_name" {
  description = "The SAML identity-provider name to pass to the hosted UI (empty when SAML is disabled). The frontend uses this as the signInWithRedirect provider."
  value       = var.enable_saml_idp ? var.saml_provider_name : ""
}

output "rbac_group_names" {
  description = "The RBAC Cognito group names, keyed by role. Names match the backend Role enum; membership drives the cognito:groups claim resolve_role reads."
  value = {
    admin     = aws_cognito_user_group.admin.name
    curator   = aws_cognito_user_group.curator.name
    validator = aws_cognito_user_group.validator.name
    viewer    = aws_cognito_user_group.viewer.name
  }
}

output "user_pool_domain" {
  description = "The Cognito User Pool domain (only if hosted UI enabled)"
  value       = var.enable_hosted_ui ? aws_cognito_user_pool_domain.this[0].domain : null
}

output "hosted_ui_url" {
  description = "Base URL of the Cognito hosted UI (only if hosted UI enabled)"
  value = var.enable_hosted_ui ? format(
    "https://%s.auth.%s.amazoncognito.com",
    aws_cognito_user_pool_domain.this[0].domain,
    data.aws_region.current.region,
  ) : null
}

output "discovery_url" {
  description = "OIDC discovery URL — for any consumer that validates this pool's JWTs"
  value       = "https://cognito-idp.${data.aws_region.current.region}.amazonaws.com/${aws_cognito_user_pool.this.id}/.well-known/openid-configuration"
}
