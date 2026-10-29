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

variable "vpc_id" {
  description = "ID of the VPC"
  type        = string
}

variable "public_subnet_ids" {
  description = "IDs of public subnets for ALB"
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "IDs of private subnets for ECS tasks"
  type        = list(string)
}

variable "container_image" {
  description = "Docker image for the application"
  type        = string
}

variable "container_port" {
  description = "Port the container listens on"
  type        = number
  default     = 8000
}

variable "task_cpu" {
  description = "CPU units for the task (1024 = 1 vCPU)"
  type        = number
  default     = 2048
}

variable "task_memory" {
  description = "Memory in MiB for the task"
  type        = number
  default     = 4096
}

variable "desired_count" {
  description = "Desired number of tasks"
  type        = number
  default     = 1
}

variable "min_capacity" {
  description = "Minimum number of tasks for auto-scaling"
  type        = number
  default     = 1
}

variable "max_capacity" {
  description = "Maximum number of tasks for auto-scaling"
  type        = number
  default     = 4
}

variable "health_check_path" {
  description = "Path for health check"
  type        = string
  default     = "/health"
}

variable "certificate_arn" {
  description = "ARN of the ACM certificate for HTTPS"
  type        = string
}

variable "container_environment" {
  description = "Environment variables for the container"
  type = list(object({
    name  = string
    value = string
  }))
  default = []
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

variable "pptx_task_cpu" {
  description = "Fargate CPU units for the pptx-converter sidecar. LibreOffice conversion is CPU-bound, so this is sized independently of the API task."
  type        = number
  default     = 1024
}

variable "pptx_task_memory" {
  description = "Fargate memory (MiB) for the pptx-converter sidecar."
  type        = number
  default     = 2048
}

variable "pptx_container_env" {
  description = <<-EOT
    Environment variables for the pptx-converter sidecar. It reads:
      SOFFICE_TIMEOUT_S      per-conversion LibreOffice timeout (default 60).
                             Must stay BELOW the caller's SIDECAR_TIMEOUT_S (90)
                             or the caller gives up before the sidecar does.
      CONVERTER_TMP_ROOT     scratch directory (default /tmp).
      ALLOWED_URL_HOST_SUFFIX  presigned-URL host allowlist suffix
                             (default .amazonaws.com) — an SSRF guard.
    Empty by default (the sidecar's own defaults apply).
  EOT
  type        = map(string)
  default     = {}
}

variable "origin_verify_secret" {
  description = <<-EOT
    Shared secret CloudFront attaches as the X-Origin-Verify header. When set,
    the ALB's listener answers 403 to anything WITHOUT the header, so the API can
    only be reached through CloudFront — otherwise anyone who learns the ALB's
    DNS name reaches it directly, bypassing CloudFront entirely.
    Empty disables the check (the listener forwards everything).
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "verify_origin_header" {
  description = <<-EOT
    Enable the X-Origin-Verify check on the ALB listener. Kept as a separate bool
    from origin_verify_secret because the secret is generated at apply time, and
    Terraform cannot use an apply-time value in a `count`.
  EOT
  type        = bool
  default     = true
}

variable "restrict_alb_to_cloudfront" {
  description = <<-EOT
    Restrict the ALB security group to CloudFront's managed origin-facing prefix
    list, on top of the header check. The list counts ~55 entries against the
    security group's 60-rule quota, which fits because only the one port
    CloudFront connects on is opened. false opens that port to 0.0.0.0/0 and
    leaves the header check as the only control — use it only to reach the ALB
    directly while debugging.
  EOT
  type        = bool
  default     = true
}

variable "enable_tracing" {
  description = "Grant the task role X-Ray span export + Application Signals metric permissions. The container runs under opentelemetry-instrument, so without this spans are dropped at export with an AccessDenied visible only in the task log."
  type        = bool
  default     = true
}

variable "alb_deletion_protection" {
  description = "Enable deletion protection on the ALB. Set false and apply before running terraform destroy."
  type        = bool
  default     = true
}

variable "kms_key_arn" {
  description = "ARN of the customer-managed KMS key encrypting the ECR repositories and DynamoDB tables."
  type        = string
}
