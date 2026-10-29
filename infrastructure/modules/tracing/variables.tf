# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

variable "project_name" {
  description = "Project prefix used in resource names."
  type        = string
}

variable "environment" {
  description = "Environment name (dev/test/prod)."
  type        = string
}

variable "aws_region" {
  description = "AWS region. Informational: the module resolves the live region from the provider so it cannot disagree with where it is applied."
  type        = string
  default     = "us-east-1"
}

variable "span_sampling_percentage" {
  description = "Share of trace spans Transaction Search indexes, 0-100. Indexed spans are billed, so lower this for high-traffic accounts. 100 keeps every span for a low-volume compliance-review workload."
  type        = number
  default     = 100

  validation {
    condition     = var.span_sampling_percentage > 0 && var.span_sampling_percentage <= 100
    error_message = "span_sampling_percentage must be between 1 and 100. 0 would silently disable indexing while leaving Transaction Search on, which looks like a broken deployment rather than a disabled one."
  }
}

variable "tags" {
  description = "Tags applied to taggable resources."
  type        = map(string)
  default     = {}
}
