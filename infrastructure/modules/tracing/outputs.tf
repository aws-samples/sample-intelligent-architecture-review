# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "spans_log_group" {
  description = "CloudWatch log group Transaction Search indexes spans into. Query it with Logs Insights, or use it as the trace source for the evaluation harness."
  value       = "aws/spans"
}

output "log_resource_policy_name" {
  description = "Name of the CloudWatch Logs resource policy granting X-Ray write access."
  value       = aws_cloudwatch_log_resource_policy.transaction_search.policy_name
}
