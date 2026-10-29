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

variable "aws_region" {
  description = "AWS Region, used to build the trail ARN in the bucket policy conditions."
  type        = string
}

variable "retention_days" {
  description = "Days to retain CloudTrail objects in S3 before expiry."
  type        = number
  default     = 90
}

variable "log_retention_days" {
  description = "Days to retain the CloudWatch Logs copy of the trail. One year by default, so a full annual audit cycle stays queryable."
  type        = number
  default     = 365
}

variable "tags" {
  description = "Common tags for all resources"
  type        = map(string)
  default     = {}
}
