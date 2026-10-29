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
  description = "AWS region"
  type        = string
}

variable "ecs_cluster_name" {
  description = "Name of the ECS cluster"
  type        = string
}

variable "ecs_service_name" {
  description = "Name of the ECS service"
  type        = string
}

variable "alb_arn_suffix" {
  description = "ARN suffix of the ALB for CloudWatch metrics"
  type        = string
}

variable "app_log_group_name" {
  description = "Log group the application actually writes to (the ECS task log group). The API-error metric filter attaches to this."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days. One year by default, so a full annual audit cycle stays queryable."
  type        = number
  default     = 365
}

variable "tags" {
  description = "Common tags for all resources"
  type        = map(string)
  default     = {}
}

variable "bedrock_throttle_threshold" {
  description = "Bedrock throttled invocations in a 5-minute period before alarming. The validator retries up to 3 times internally, so a small number is normal under load."
  type        = number
  default     = 25
}

variable "guardrail_intervention_threshold" {
  description = "Guardrail interventions in a 5-minute period before alarming. Redactions are expected, so this is set for a sustained rate rather than any occurrence."
  type        = number
  default     = 50
}

variable "daily_spend_alarm_usd" {
  description = "Estimated daily Bedrock validation spend, in USD, before alarming."
  type        = number
  default     = 100
}

variable "kms_key_arn" {
  description = "ARN of the customer-managed KMS key the alarms topic is encrypted with."
  type        = string
}
