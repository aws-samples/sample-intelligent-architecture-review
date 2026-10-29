# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# Additional CloudWatch Alarms for Application Metrics
###############################################################################

###############################################################################
# Custom Metrics Namespace
###############################################################################

locals {
  # Derived from project_name so the namespace isn't pinned to one project.
  # Must match backend metrics.py MetricsService.namespace ("<project>-metrics").
  custom_namespace = "${var.project_name}-metrics"
}

# NOTE: the validation_failures + bedrock_throttling alarms were removed.
# They watched custom metrics (ValidationFailures /
# BedrockThrottled) emitted only by metrics.py methods that had zero callers,
# so the alarms never had data. Removed with those dead emitters.
# The api_error_rate alarm below stays: it is fed by a log metric filter, which
# now watches the ECS task log group (it previously watched a log group nothing
# wrote to, so it could never fire).

###############################################################################
# DynamoDB Error Rate Alarm
###############################################################################

resource "aws_cloudwatch_metric_alarm" "dynamodb_errors" {
  alarm_name          = "${var.project_name}-${var.environment}-dynamodb-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "UserErrors"
  namespace           = "AWS/DynamoDB"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "DynamoDB user errors exceeded threshold"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    TableName = "${var.project_name}-${var.environment}-documents"
  }

  tags = var.tags
}

# NOTE: an S3 4xx-error alarm was removed. AWS/S3 4xxErrors is only published
# when the bucket has a request-metrics configuration, which it does not, so
# the alarm could never receive a datapoint.

###############################################################################
# ECS Task Count Alarm (Service Degradation)
###############################################################################

resource "aws_cloudwatch_metric_alarm" "ecs_task_count_low" {
  alarm_name          = "${var.project_name}-${var.environment}-ecs-task-count-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "RunningTaskCount"
  namespace           = "ECS/ContainerInsights"
  period              = 60
  statistic           = "Average"
  threshold           = 1
  alarm_description   = "ECS running task count below minimum (service degraded)"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "breaching"

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.ecs_service_name
  }

  tags = var.tags
}

# NOTE: the kb_query_failures + extraction_failures alarms were removed for the
# same reason: their custom metrics
# (KnowledgeBaseQueryFailures / ExtractionFailures) were emitted only by
# zero-caller metrics.py methods, so the alarms never had data.

###############################################################################
# API Error Rate Alarm (from logs)
###############################################################################

resource "aws_cloudwatch_log_metric_filter" "api_errors" {
  name           = "${var.project_name}-${var.environment}-api-errors"
  log_group_name = var.app_log_group_name
  pattern        = "[time, request_id, level=ERROR*, ...]"

  metric_transformation {
    name      = "APIErrors"
    namespace = local.custom_namespace
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "api_error_rate" {
  alarm_name          = "${var.project_name}-${var.environment}-api-error-rate"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "APIErrors"
  namespace           = local.custom_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 20
  alarm_description   = "API error rate exceeded threshold (>20 errors in 5 minutes)"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "notBreaching"

  tags = var.tags
}

###############################################################################
# Amazon Bedrock
#
# Model calls are the cost driver and the failure mode most likely to degrade a
# result silently. The validator retries throttling internally and downgrades a
# PASS that rests on too few successful chunks, so sustained throttling shows up
# as lower-quality verdicts rather than as errors — which is precisely why it
# needs an alarm rather than only a log line.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "bedrock_throttles" {
  alarm_name          = "${var.project_name}-${var.environment}-bedrock-throttles"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "InvocationThrottles"
  namespace           = "AWS/Bedrock"
  period              = 300
  statistic           = "Sum"
  threshold           = var.bedrock_throttle_threshold
  alarm_description   = "Bedrock is throttling invocations. Validation retries internally, so sustained throttling degrades verdict quality before it surfaces as an error."
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "notBreaching"

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "bedrock_server_errors" {
  alarm_name          = "${var.project_name}-${var.environment}-bedrock-server-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "InvocationServerErrors"
  namespace           = "AWS/Bedrock"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = "Bedrock is returning server errors."
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "notBreaching"

  tags = var.tags
}

###############################################################################
# Guardrail interventions
#
# An intervention is not inherently a fault: redacting an email address from a
# design document is the guardrail working as intended. A sustained rate is the
# signal, because it usually means the policy is intervening on legitimate
# compliance language, which shows up to a user as an incomplete verdict.
#
# The metric comes from the application, which records interventions from the
# guardrail trace on each Bedrock response. AWS/Bedrock does not publish a
# per-guardrail intervention count that distinguishes redaction from blocking.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "guardrail_interventions" {
  alarm_name          = "${var.project_name}-${var.environment}-guardrail-interventions"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "GuardrailInterventions"
  namespace           = local.custom_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = var.guardrail_intervention_threshold
  alarm_description   = "Guardrail interventions are elevated. Check whether the policy is firing on legitimate compliance language rather than on real sensitive data."
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "notBreaching"

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "guardrail_blocked" {
  alarm_name          = "${var.project_name}-${var.environment}-guardrail-blocked"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "GuardrailBlocked"
  namespace           = local.custom_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "A request or response was BLOCKED by the guardrail, not merely redacted. A blocked call produces no verdict, so any occurrence is worth seeing."
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "notBreaching"

  tags = var.tags
}

###############################################################################
# Model spend
#
# The application already persists an estimated cost per validation and emits it
# as a metric. This alarms on the daily total, which is the figure that matters
# when a misconfigured run fans out further than intended.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "daily_model_spend" {
  alarm_name          = "${var.project_name}-${var.environment}-daily-model-spend"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ValidationEstimatedCostUSD"
  namespace           = local.custom_namespace
  period              = 86400
  statistic           = "Sum"
  threshold           = var.daily_spend_alarm_usd
  alarm_description   = "Estimated Bedrock spend on validations exceeded the daily threshold. The figure depends on VALIDATION_INPUT_USD_PER_MTOK / VALIDATION_OUTPUT_USD_PER_MTOK matching the model in use."
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "notBreaching"

  tags = var.tags
}

###############################################################################
# Data Sources
###############################################################################

data "aws_caller_identity" "current" {}
