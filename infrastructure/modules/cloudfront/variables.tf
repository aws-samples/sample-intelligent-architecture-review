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

variable "s3_bucket_regional_domain_name" {
  description = "Regional domain name of the S3 frontend bucket"
  type        = string
}

variable "alb_dns_name" {
  description = "DNS name of the backend ALB for API origin"
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "ARN of ACM certificate for custom domain (empty string for default CloudFront cert)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Common tags for all resources"
  type        = map(string)
  default     = {}
}

variable "origin_verify_secret" {
  description = <<-EOT
    Shared secret sent to the ALB origin as the X-Origin-Verify header. The ALB
    listener forwards only requests carrying it, so the API cannot be reached by
    hitting the ALB directly. Empty disables the header (and the ALB check).
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "verify_origin_header" {
  description = "Send the X-Origin-Verify header to the ALB origin. Must match the ecs module's setting."
  type        = bool
  default     = true
}

variable "alb_has_certificate" {
  description = "Whether the ALB has an HTTPS listener. When true, CloudFront talks to the origin over HTTPS instead of plain HTTP."
  type        = bool
  default     = false
}

variable "access_log_retention_days" {
  description = "Days to keep CloudFront access logs before they expire."
  type        = number
  default     = 365
}
