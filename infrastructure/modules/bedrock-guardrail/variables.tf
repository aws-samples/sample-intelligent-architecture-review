# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

variable "project_name" {
  description = "Name of the project"
  type        = string
}

variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "ecs_task_role_name" {
  description = "Name of the ECS task role that invokes Bedrock, granted bedrock:ApplyGuardrail on this guardrail."
  type        = string
}

variable "pii_entities" {
  description = <<-EOT
    Managed PII entity types to ANONYMIZE in model input and output.

    Deliberately excludes NAME and ADDRESS: a design document names its owning
    team and its datacenter sites, and redacting those removes information the
    validator needs to judge a data-residency or site-pairing rule.
  EOT
  type        = list(string)
  default = [
    "EMAIL",
    "PHONE",
    "CREDIT_DEBIT_CARD_NUMBER",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "PASSWORD",
    "USERNAME",
  ]
}

variable "credential_patterns" {
  description = <<-EOT
    Regular expressions for credential shapes the managed PII types do not
    cover. Anchored deliberately: a broad pattern rewrites ordinary prose, and a
    redaction inside a rule's validation criteria changes what the rule means.
  EOT
  type = list(object({
    name        = string
    description = string
    pattern     = string
  }))
  default = [
    {
      name        = "private-key-block"
      description = "PEM private key header, which should never appear in a design document."
      pattern     = "-----BEGIN [A-Z ]*PRIVATE KEY-----"
    },
    {
      name        = "bearer-token"
      description = "An Authorization bearer token pasted into a document."
      pattern     = "(?i)bearer\\s+[A-Za-z0-9._~+/-]{20,}"
    },
    {
      name        = "connection-string-password"
      description = "A password embedded in a database connection string."
      pattern     = "(?i)(password|pwd)\\s*=\\s*[^\\s;\"']{6,}"
    },
  ]
}

variable "blocked_input_message" {
  description = "Returned when the guardrail blocks a request. Written to be actionable rather than generic, because it surfaces in the application."
  type        = string
  default     = "This request was blocked by the content guardrail. If a document legitimately contains this material, contact an administrator."
}

variable "blocked_output_message" {
  description = "Returned when the guardrail blocks a model response."
  type        = string
  default     = "The model response was blocked by the content guardrail."
}

variable "tags" {
  description = "Common tags for all resources"
  type        = map(string)
  default     = {}
}
