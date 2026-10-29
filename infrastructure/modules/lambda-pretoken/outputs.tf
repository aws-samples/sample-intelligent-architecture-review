# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "function_arn" {
  description = "ARN of the pre-token-generation Lambda (wire to the Cognito user pool's pre_token_generation trigger)."
  value       = aws_lambda_function.pretoken.arn
}

output "function_name" {
  description = "Name of the pre-token-generation Lambda."
  value       = aws_lambda_function.pretoken.function_name
}
