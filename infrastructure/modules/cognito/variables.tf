# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

variable "project_name" {
  description = "Project name (used in resource naming)"
  type        = string
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
}

variable "cloudfront_domain" {
  description = "CloudFront distribution domain. Used for Cognito callback/logout URLs. Empty string disables the production callback."
  type        = string
  default     = ""
}

variable "password_min_length" {
  description = "Minimum password length"
  type        = number
  default     = 8
}

variable "password_require_lowercase" {
  description = "Require lowercase characters in password"
  type        = bool
  default     = true
}

variable "password_require_uppercase" {
  description = "Require uppercase characters in password"
  type        = bool
  default     = true
}

variable "password_require_numbers" {
  description = "Require numbers in password"
  type        = bool
  default     = true
}

variable "password_require_symbols" {
  description = "Require symbols in password"
  type        = bool
  default     = true
}

variable "token_validity_hours" {
  description = "Access token validity in hours"
  type        = number
  default     = 1
}

variable "refresh_token_validity_days" {
  description = "Refresh token validity in days"
  type        = number
  default     = 30
}

variable "id_token_validity_hours" {
  description = "ID token validity in hours"
  type        = number
  default     = 1
}

variable "enable_hosted_ui" {
  description = "Enable Cognito hosted UI domain"
  type        = bool
  default     = true
}

# ---- SAML federation (permanent, Entra-ready) ----

variable "enable_saml_idp" {
  description = "Attach a SAML identity provider to the pool and add it to the web client. Default off so non-federated envs are untouched."
  type        = bool
  default     = false
}

variable "saml_provider_name" {
  description = "Cognito identity-provider name for the SAML IdP (also used as the identity_provider in the hosted-UI authorize call). e.g. MockSAML, EntraID."
  type        = string
  default     = "MockSAML"
}

variable "saml_metadata_url" {
  description = "HTTPS URL of the SAML IdP's federation metadata document. For mocksaml: https://mocksaml.com/api/saml/metadata. For Entra: the App Federation Metadata Url."
  type        = string
  default     = ""
}

variable "saml_email_attribute" {
  description = "SAML assertion attribute name mapped to the Cognito email attribute."
  type        = string
  # mocksaml emits the standard SAML emailaddress claim URI.
  default = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
}

variable "saml_groups_attribute" {
  description = "SAML assertion attribute name mapped to custom:saml_groups (the pre-token Lambda turns this into cognito:groups)."
  type        = string
  default     = "groups"
}

variable "pre_token_lambda_arn" {
  description = "ARN of the pre-token-generation Lambda. Wired to the pool trigger + invoke permission when enable_pre_token_trigger is true."
  type        = string
  default     = ""
}

variable "enable_pre_token_trigger" {
  description = "Wire the pre-token Lambda to the pool (trigger + invoke permission). Kept separate from the ARN because count/for_each must be known at plan time — the ARN is not on first apply."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to Cognito resources"
  type        = map(string)
  default     = {}
}
