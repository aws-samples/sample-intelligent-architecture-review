# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "guardrail_id" {
  description = "Guardrail identifier, passed to the application as BEDROCK_GUARDRAIL_ID."
  value       = aws_bedrock_guardrail.main.guardrail_id
}

output "guardrail_arn" {
  description = "Guardrail ARN."
  value       = aws_bedrock_guardrail.main.guardrail_arn
}

output "guardrail_version" {
  description = <<-EOT
    The numbered guardrail version, passed as BEDROCK_GUARDRAIL_VERSION. A
    numbered version rather than DRAFT: DRAFT is mutable, so an invoke against it
    would silently change behavior when the policy is edited.
  EOT
  value       = aws_bedrock_guardrail_version.main.version
}
