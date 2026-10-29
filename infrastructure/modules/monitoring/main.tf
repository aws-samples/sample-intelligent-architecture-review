# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# Monitoring Module - CloudWatch log groups, alarms, and dashboard
###############################################################################

###############################################################################
# Log Groups
###############################################################################

# NOTE: this module used to declare its own "/app/..." and "/api/..." log groups.
# Nothing ever wrote to either — the tasks log to the ECS module's "/ecs/..."
# group and there is no API Gateway in this stack — so both were removed. The
# API-error metric filter now attaches to var.app_log_group_name (the real one).

###############################################################################
# SNS Topic for Alarms
###############################################################################

resource "aws_sns_topic" "alarms" {
  name = "${var.project_name}-${var.environment}-alarms"

  # The key policy lets cloudwatch.amazonaws.com use the key, which is what
  # allows alarms to keep publishing to an encrypted topic.
  kms_master_key_id = var.kms_key_arn

  tags = var.tags
}

###############################################################################
# ECS CPU Utilization Alarm
###############################################################################

resource "aws_cloudwatch_metric_alarm" "ecs_cpu_high" {
  alarm_name          = "${var.project_name}-${var.environment}-ecs-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "ECS CPU utilization is above 80%"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.ecs_service_name
  }

  tags = var.tags
}

###############################################################################
# ECS Memory Utilization Alarm
###############################################################################

resource "aws_cloudwatch_metric_alarm" "ecs_memory_high" {
  alarm_name          = "${var.project_name}-${var.environment}-ecs-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "MemoryUtilization"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "Average"
  threshold           = 85
  alarm_description   = "ECS memory utilization is above 85%"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.ecs_service_name
  }

  tags = var.tags
}

###############################################################################
# ALB 5xx Error Alarm
###############################################################################

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.project_name}-${var.environment}-alb-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "ALB target 5xx errors exceeded threshold"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }

  tags = var.tags
}

###############################################################################
# ALB Response Time Alarm
###############################################################################

resource "aws_cloudwatch_metric_alarm" "alb_response_time" {
  alarm_name          = "${var.project_name}-${var.environment}-alb-response-time"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "TargetResponseTime"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Average"
  threshold           = 5
  alarm_description   = "ALB average response time is above 5 seconds"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }

  tags = var.tags
}

###############################################################################
# CloudWatch Dashboard
###############################################################################

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.project_name}-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      # ---- Title banner --------------------------------------------------
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 2
        properties = {
          markdown = "# ${var.project_name} · ${var.environment}\nCompliance-review platform. Top rows are application signal (model cost, guardrail, Bedrock health); the infrastructure widgets follow below."
        }
      },
      # ---- Single-value header -------------------------------------------
      {
        type   = "metric"
        x      = 0
        y      = 2
        width  = 8
        height = 4
        properties = {
          title = "Validations (24h)"
          # ValidationEstimatedCostUSD is emitted exactly once per completed
          # validation, so its sample count is the number of validations.
          metrics   = [["${local.custom_namespace}", "ValidationEstimatedCostUSD", { stat = "SampleCount", label = "validations" }]]
          view      = "singleValue"
          period    = 86400
          region    = var.aws_region
          sparkline = true
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 2
        width  = 8
        height = 4
        properties = {
          title     = "Est. model spend (24h)"
          metrics   = [["${local.custom_namespace}", "ValidationEstimatedCostUSD", { stat = "Sum", label = "USD" }]]
          view      = "singleValue"
          period    = 86400
          region    = var.aws_region
          sparkline = true
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 2
        width  = 8
        height = 4
        properties = {
          title     = "Guardrail interventions (24h)"
          metrics   = [["${local.custom_namespace}", "GuardrailInterventions", { stat = "Sum", label = "interventions" }]]
          view      = "singleValue"
          period    = 86400
          region    = var.aws_region
          sparkline = true
        }
      },
      # ---- Model cost + tokens -------------------------------------------
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title = "Model cost (estimated USD) + calls per period"
          metrics = [
            ["${local.custom_namespace}", "ValidationEstimatedCostUSD", { stat = "Sum", label = "est. USD" }],
            ["${local.custom_namespace}", "ValidationBedrockCalls", { stat = "Sum", label = "Bedrock calls", yAxis = "right" }],
          ]
          period = 3600
          region = var.aws_region
          view   = "timeSeries"
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title = "Tokens (input / output)"
          metrics = [
            ["${local.custom_namespace}", "ValidationInputTokens", { stat = "Sum", label = "input" }],
            ["${local.custom_namespace}", "ValidationOutputTokens", { stat = "Sum", label = "output" }],
          ]
          period = 3600
          region = var.aws_region
          view   = "timeSeries"
        }
      },
      # ---- Guardrail -----------------------------------------------------
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title = "Guardrail: interventions vs blocks"
          # An intervention that only redacts is expected; a block produces no
          # verdict. Plotting both shows whether the policy is over-firing.
          metrics = [
            ["${local.custom_namespace}", "GuardrailInterventions", { stat = "Sum", label = "interventions (incl. redactions)" }],
            ["${local.custom_namespace}", "GuardrailBlocked", { stat = "Sum", label = "blocked" }],
          ]
          period = 3600
          region = var.aws_region
          view   = "timeSeries"
        }
      },
      # ---- Bedrock health ------------------------------------------------
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title = "Bedrock: throttles + server errors"
          # The validator retries throttling and downgrades a PASS resting on
          # too few chunks, so sustained throttling degrades verdict quality
          # before it surfaces as an error.
          metrics = [
            ["AWS/Bedrock", "InvocationThrottles", { stat = "Sum", label = "throttles" }],
            ["AWS/Bedrock", "InvocationServerErrors", { stat = "Sum", label = "server errors" }],
          ]
          period = 300
          region = var.aws_region
          view   = "timeSeries"
        }
      },
      # ---- Validation latency + chunks -----------------------------------
      {
        type   = "metric"
        x      = 0
        y      = 18
        width  = 24
        height = 6
        properties = {
          title = "Validation duration + chunks per run"
          metrics = [
            ["${local.custom_namespace}", "ValidationDurationSeconds", { stat = "Average", label = "avg duration (s)" }],
            ["${local.custom_namespace}", "ValidationDurationSeconds", { stat = "Maximum", label = "max duration (s)" }],
            ["${local.custom_namespace}", "ValidationChunks", { stat = "Average", label = "avg chunks", yAxis = "right" }],
          ]
          period = 3600
          region = var.aws_region
          view   = "timeSeries"
        }
      },

      {
        type   = "metric"
        x      = 0
        y      = 24
        width  = 12
        height = 6
        properties = {
          title   = "ECS CPU Utilization"
          metrics = [["AWS/ECS", "CPUUtilization", "ClusterName", var.ecs_cluster_name, "ServiceName", var.ecs_service_name]]
          period  = 60
          stat    = "Average"
          region  = var.aws_region
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 24
        width  = 12
        height = 6
        properties = {
          title   = "ECS Memory Utilization"
          metrics = [["AWS/ECS", "MemoryUtilization", "ClusterName", var.ecs_cluster_name, "ServiceName", var.ecs_service_name]]
          period  = 60
          stat    = "Average"
          region  = var.aws_region
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 30
        width  = 12
        height = 6
        properties = {
          title   = "ALB Request Count"
          metrics = [["AWS/ApplicationELB", "RequestCount", "LoadBalancer", var.alb_arn_suffix]]
          period  = 60
          stat    = "Sum"
          region  = var.aws_region
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 30
        width  = 12
        height = 6
        properties = {
          title   = "ALB Response Time"
          metrics = [["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", var.alb_arn_suffix]]
          period  = 60
          stat    = "Average"
          region  = var.aws_region
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 36
        width  = 24
        height = 6
        properties = {
          title = "ALB HTTP Status Codes"
          metrics = [
            ["AWS/ApplicationELB", "HTTPCode_Target_2XX_Count", "LoadBalancer", var.alb_arn_suffix],
            ["AWS/ApplicationELB", "HTTPCode_Target_4XX_Count", "LoadBalancer", var.alb_arn_suffix],
            ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", var.alb_arn_suffix],
          ]
          period = 60
          stat   = "Sum"
          region = var.aws_region
        }
      },
      # NOTE: the "Validation Metrics", "Bedrock API Calls", and "Knowledge
      # Base Queries" widgets were removed — they graphed
      # custom metrics (ValidationSuccess/Failures, BedrockInvocations/
      # Throttled, KnowledgeBaseQueries/Failures) emitted only by zero-caller
      # metrics.py methods, so they were always empty.
    ]
  })
}
