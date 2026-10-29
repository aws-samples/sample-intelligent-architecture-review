# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "documents_bucket_id" {
  description = "ID of the documents bucket"
  value       = aws_s3_bucket.documents.id
}

output "documents_bucket_arn" {
  description = "ARN of the documents bucket"
  value       = aws_s3_bucket.documents.arn
}

output "frontend_bucket_id" {
  description = "ID of the frontend bucket"
  value       = aws_s3_bucket.frontend.id
}

output "frontend_bucket_arn" {
  description = "ARN of the frontend bucket"
  value       = aws_s3_bucket.frontend.arn
}

output "frontend_bucket_regional_domain_name" {
  description = "Regional domain name of the frontend bucket"
  value       = aws_s3_bucket.frontend.bucket_regional_domain_name
}
