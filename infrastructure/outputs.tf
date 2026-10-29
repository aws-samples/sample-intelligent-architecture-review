# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# Root module outputs
###############################################################################

output "vpc_id" {
  description = "VPC ID"
  value       = module.vpc.vpc_id
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = module.ecs.cluster_name
}

output "ecs_service_name" {
  description = "ECS service name"
  value       = module.ecs.service_name
}

output "alb_dns_name" {
  description = "ALB DNS name"
  value       = module.ecs.alb_dns_name
}

output "cloudfront_domain_name" {
  description = "CloudFront distribution domain name"
  value       = module.cloudfront.distribution_domain_name
}

output "documents_bucket" {
  description = "Documents S3 bucket name"
  value       = module.s3.documents_bucket_id
}

output "frontend_bucket" {
  description = "Frontend S3 bucket name"
  value       = module.s3.frontend_bucket_id
}

output "dynamodb_documents_table" {
  description = "DynamoDB documents table name"
  value       = module.dynamodb.documents_table_name
}

output "dynamodb_validations_table" {
  description = "DynamoDB validations table name"
  value       = module.dynamodb.validations_table_name
}

output "cognito_user_pool_id" {
  description = "Cognito user pool ID — needed to create the first login user"
  value       = module.cognito.user_pool_id
}

output "cognito_web_app_client_id" {
  description = "Cognito web app client ID used by the frontend for login"
  value       = module.cognito.web_app_client_id
}

output "knowledge_base_id" {
  description = "Bedrock Knowledge Base ID"
  value       = module.knowledge_base.knowledge_base_id
}

output "data_source_id" {
  description = "Bedrock Knowledge Base data source ID"
  value       = module.knowledge_base.data_source_id
}

output "monitoring_dashboard" {
  description = "CloudWatch dashboard name"
  value       = module.monitoring.dashboard_name
}

output "sns_alarms_topic" {
  description = "SNS topic ARN for alarms"
  value       = module.monitoring.sns_topic_arn
}

output "bedrock_guardrail_id" {
  description = "Amazon Bedrock guardrail id applied to model calls, or empty when disabled."
  value       = var.enable_bedrock_guardrail ? module.bedrock_guardrail[0].guardrail_id : ""
}

output "bedrock_guardrail_version" {
  description = "Guardrail version the application references."
  value       = var.enable_bedrock_guardrail ? module.bedrock_guardrail[0].guardrail_version : ""
}

output "cloudtrail_bucket" {
  description = "S3 bucket holding CloudTrail objects, or empty when the trail is disabled."
  value       = var.enable_cloudtrail ? module.cloudtrail[0].trail_bucket : ""
}

###############################################################################
# Tracing
###############################################################################

output "spans_log_group" {
  description = "CloudWatch log group where Transaction Search indexes trace spans. Query with Logs Insights to inspect agent behaviour."
  value       = module.tracing.spans_log_group
}
