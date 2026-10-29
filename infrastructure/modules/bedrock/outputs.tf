# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "bedrock_policy_arn" {
  description = "ARN of the Bedrock access IAM policy"
  value       = aws_iam_policy.bedrock_access.arn
}
