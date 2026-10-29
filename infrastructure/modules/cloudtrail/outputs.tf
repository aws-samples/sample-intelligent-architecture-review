# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "trail_arn" {
  description = "ARN of the CloudTrail trail."
  value       = aws_cloudtrail.main.arn
}

output "trail_bucket" {
  description = "S3 bucket holding delivered trail objects."
  value       = aws_s3_bucket.trail.id
}

output "log_group_name" {
  description = "CloudWatch log group the trail also delivers to, for Logs Insights queries and alarms."
  value       = aws_cloudwatch_log_group.trail.name
}
