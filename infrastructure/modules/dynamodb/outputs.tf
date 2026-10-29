# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "documents_table_name" {
  description = "Name of the documents DynamoDB table"
  value       = aws_dynamodb_table.documents.name
}

output "documents_table_arn" {
  description = "ARN of the documents DynamoDB table"
  value       = aws_dynamodb_table.documents.arn
}

output "validations_table_name" {
  description = "Name of the validations DynamoDB table"
  value       = aws_dynamodb_table.validations.name
}

output "validations_table_arn" {
  description = "ARN of the validations DynamoDB table"
  value       = aws_dynamodb_table.validations.arn
}

output "standards_table_name" {
  description = "Name of the standards DynamoDB table"
  value       = aws_dynamodb_table.standards.name
}

output "standards_table_arn" {
  description = "ARN of the standards DynamoDB table"
  value       = aws_dynamodb_table.standards.arn
}

output "chat_jobs_table_name" {
  description = "Name of the chat-jobs DynamoDB table (async curator chat)"
  value       = aws_dynamodb_table.chat_jobs.name
}

output "chat_jobs_table_arn" {
  description = "ARN of the chat-jobs DynamoDB table"
  value       = aws_dynamodb_table.chat_jobs.arn
}

output "references_table_name" {
  description = "Name of the References DynamoDB table"
  value       = aws_dynamodb_table.references.name
}

output "references_table_arn" {
  description = "ARN of the References DynamoDB table"
  value       = aws_dynamodb_table.references.arn
}

output "user_roles_table_name" {
  description = "Name of the user-roles DynamoDB table (RBAC)"
  value       = aws_dynamodb_table.user_roles.name
}

output "user_roles_table_arn" {
  description = "ARN of the user-roles DynamoDB table"
  value       = aws_dynamodb_table.user_roles.arn
}

output "audit_events_table_name" {
  description = "Name of the audit-events DynamoDB table (audit trail)"
  value       = aws_dynamodb_table.audit_events.name
}

output "audit_events_table_arn" {
  description = "ARN of the audit-events DynamoDB table"
  value       = aws_dynamodb_table.audit_events.arn
}

output "icon_legend_table_name" {
  description = "Name of the icon-legend DynamoDB table (custom on-prem icons)"
  value       = aws_dynamodb_table.icon_legend.name
}

output "icon_legend_table_arn" {
  description = "ARN of the icon-legend DynamoDB table"
  value       = aws_dynamodb_table.icon_legend.arn
}
