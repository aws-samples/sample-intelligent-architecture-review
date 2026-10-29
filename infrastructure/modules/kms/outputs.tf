# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "key_arn" {
  description = "ARN of the customer-managed key encrypting application data at rest."
  value       = aws_kms_key.main.arn
}

output "key_id" {
  description = "ID of the customer-managed key."
  value       = aws_kms_key.main.key_id
}
