# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# AWS CloudTrail
#
# The application maintains its own audit table recording who performed which
# action inside the product. That is not an AWS API audit: it cannot answer who
# invoked a model, who read an object from the documents bucket, or who changed
# an IAM policy. For a compliance review tool, that gap is the more consequential
# one, because the application's own records are written by the component under
# question.
#
# Data events are enabled for Amazon Bedrock InvokeModel specifically. Model
# invocations are the expensive and the sensitive operation here, and management
# events alone do not record them.
###############################################################################

data "aws_caller_identity" "current" {}

locals {
  bucket_name = "${var.project_name}-${var.environment}-cloudtrail-${data.aws_caller_identity.current.account_id}"
}

###############################################################################
# Trail destination bucket
###############################################################################

resource "aws_s3_bucket" "trail" {
  bucket = local.bucket_name

  # The trail is evidence. force_destroy stays false so that `terraform destroy`
  # cannot silently discard the audit history along with the stack.
  force_destroy = false

  tags = merge(var.tags, {
    Name = local.bucket_name
  })
}

resource "aws_s3_bucket_public_access_block" "trail" {
  bucket                  = aws_s3_bucket.trail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "trail" {
  bucket = aws_s3_bucket.trail.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id

  rule {
    id     = "expire-trail-objects"
    status = "Enabled"

    filter {}

    expiration {
      days = var.retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    # Failed multipart uploads are otherwise retained indefinitely as billable
    # storage that does not appear in a bucket listing.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

###############################################################################
# Bucket policy
#
# CloudTrail writes as a service principal, and the two aws:SourceArn conditions
# stop a trail in another account from being pointed at this bucket.
###############################################################################

data "aws_iam_policy_document" "trail_bucket" {
  statement {
    sid    = "AWSCloudTrailAclCheck"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.trail.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudtrail:${var.aws_region}:${data.aws_caller_identity.current.account_id}:trail/${var.project_name}-${var.environment}-trail"]
    }
  }

  statement {
    sid    = "AWSCloudTrailWrite"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.trail.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudtrail:${var.aws_region}:${data.aws_caller_identity.current.account_id}:trail/${var.project_name}-${var.environment}-trail"]
    }
  }
}

resource "aws_s3_bucket_policy" "trail" {
  bucket = aws_s3_bucket.trail.id
  policy = data.aws_iam_policy_document.trail_bucket.json
}

###############################################################################
# CloudWatch Logs destination
#
# The bucket is the durable record; the log group makes events queryable with
# Logs Insights and alarmable, which is what makes the trail useful day to day
# rather than only after an incident.
###############################################################################

resource "aws_cloudwatch_log_group" "trail" {
  name              = "/aws/cloudtrail/${var.project_name}-${var.environment}"
  retention_in_days = var.log_retention_days

  tags = var.tags
}

data "aws_iam_policy_document" "trail_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "trail_to_logs" {
  name               = "${var.project_name}-${var.environment}-cloudtrail-logs"
  assume_role_policy = data.aws_iam_policy_document.trail_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "trail_to_logs" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.trail.arn}:*"]
  }
}

resource "aws_iam_role_policy" "trail_to_logs" {
  name   = "${var.project_name}-${var.environment}-cloudtrail-logs"
  role   = aws_iam_role.trail_to_logs.id
  policy = data.aws_iam_policy_document.trail_to_logs.json
}

###############################################################################
# The trail
###############################################################################

resource "aws_cloudtrail" "main" {
  name                          = "${var.project_name}-${var.environment}-trail"
  s3_bucket_name                = aws_s3_bucket.trail.id
  include_global_service_events = true
  is_multi_region_trail         = true

  # Detects post-hoc modification of delivered log files. Without it a trail
  # records history but cannot demonstrate the history was not altered.
  enable_log_file_validation = true

  cloud_watch_logs_group_arn = "${aws_cloudwatch_log_group.trail.arn}:*"
  cloud_watch_logs_role_arn  = aws_iam_role.trail_to_logs.arn

  advanced_event_selector {
    name = "Management events"

    field_selector {
      field  = "eventCategory"
      equals = ["Management"]
    }
  }

  # Model invocations. Management events do not record these, and they are both
  # the cost driver and the sensitive operation in this application.
  advanced_event_selector {
    name = "Bedrock model invocations"

    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }

    field_selector {
      field  = "resources.type"
      equals = ["AWS::Bedrock::Model"]
    }
  }

  # The bucket policy must exist before the trail validates its destination.
  depends_on = [aws_s3_bucket_policy.trail]

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-trail"
  })
}
