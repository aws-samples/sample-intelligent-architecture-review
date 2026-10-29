# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# Distributed tracing — CloudWatch Transaction Search
#
# The application emits OpenTelemetry spans (the ECS task runs under
# `opentelemetry-instrument`, see backend/Dockerfile). Spans reach X-Ray by
# default, but the CloudWatch Application Signals / GenAI observability views and
# any Logs Insights query over span data read from the `aws/spans` LOG GROUP,
# which only receives data once Transaction Search routes trace segments to
# CloudWatch Logs.
#
# Two pieces are required and neither has a first-class Terraform resource for
# the second half:
#   1. a CloudWatch Logs resource policy letting X-Ray write to aws/spans, and
#   2. xray:UpdateTraceSegmentDestination = CloudWatchLogs.
#
# NOTE ON SCOPE: both settings are ACCOUNT-LEVEL and REGION-WIDE — they affect
# every X-Ray-traced workload in this account/region, not just this stack. That
# is acceptable here because the tracing this enables is what makes the agentic
# pipeline debuggable, and because the destination is additive (segments still
# reach X-Ray). It is called out so a shared-account operator is not surprised.
# Deleting this module does NOT revert the account setting; see the note on
# null_resource.transaction_search below.
###############################################################################

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
}

# Lets the X-Ray service write span records into the CloudWatch log groups that
# Transaction Search uses. Scoped to this account's X-Ray via the source
# conditions so it cannot be used as a confused-deputy path by another account.
resource "aws_cloudwatch_log_resource_policy" "transaction_search" {
  policy_name = "${var.project_name}-${var.environment}-transaction-search"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "TransactionSearchXRayAccess"
      Effect    = "Allow"
      Principal = { Service = "xray.amazonaws.com" }
      Action    = "logs:PutLogEvents"
      Resource = [
        "arn:aws:logs:${local.region}:${local.account_id}:log-group:aws/spans:*",
        "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/application-signals/data:*",
      ]
      Condition = {
        ArnLike      = { "aws:SourceArn" = "arn:aws:xray:${local.region}:${local.account_id}:*" }
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

# Route trace segments to CloudWatch Logs (= Transaction Search ON) and set the
# indexing rate. No Terraform resource covers xray:UpdateTraceSegmentDestination,
# so this shells out to the CLI.
#
# The update call is NOT idempotent: re-issuing it when the destination is
# already CloudWatchLogs returns
#   InvalidRequestException: The destination is already set to CloudWatchLogs
# which under `set -e` fails the whole apply. So read the current destination
# first and only write when it differs — that also makes the provisioner safe to
# run against an account where Transaction Search was enabled by hand.
#
# Intentionally NOT reverted on destroy. Flipping the account's trace destination
# back mid-teardown would silently blind any OTHER workload in the account that
# has come to depend on aws/spans. Turning it off is therefore a deliberate
# operator action:
#   aws xray update-trace-segment-destination --destination XRay --region <region>
resource "null_resource" "transaction_search" {
  triggers = {
    destination = "CloudWatchLogs"
    sampling    = tostring(var.span_sampling_percentage)
    region      = local.region
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      REGION="${local.region}"

      current=$(aws xray get-trace-segment-destination --region "$REGION" \
        --query 'Destination' --output text 2>/dev/null || echo "UNKNOWN")

      if [ "$current" = "CloudWatchLogs" ]; then
        echo "Transaction Search already routing to CloudWatchLogs; leaving as is."
      else
        echo "Routing X-Ray trace segments to CloudWatchLogs (was: $current)..."
        aws xray update-trace-segment-destination \
          --destination CloudWatchLogs --region "$REGION" >/dev/null
      fi

      # The default indexing rule governs what share of spans is indexed (and
      # billed). A failure here must not fail the apply: tracing still works,
      # just at the previous sampling rate.
      aws xray update-indexing-rule --name Default \
        --rule '{"Probabilistic":{"DesiredSamplingPercentage":${var.span_sampling_percentage}}}' \
        --region "$REGION" >/dev/null || \
        echo "WARN: could not set X-Ray indexing rule; tracing is on at the existing rate."
    EOT
  }

  depends_on = [aws_cloudwatch_log_resource_policy.transaction_search]
}
