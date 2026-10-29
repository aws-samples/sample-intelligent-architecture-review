# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# KMS Module - customer-managed key for application data at rest
#
# One key encrypts the DynamoDB tables, ECR repositories, the alarms SNS topic
# and the SecureString SSM parameter. A customer-managed key, unlike the
# AWS-managed default, has a key policy this stack controls, can be disabled to
# cut off access to every copy of the data at once, and records each use in
# CloudTrail against a key the account owns.
#
# Access is granted through IAM: the key policy delegates to the account, and
# each consumer's role carries its own kms: permissions scoped to this key's
# ARN. The only service principal named here is CloudWatch, because alarm
# notifications are published by CloudWatch itself rather than by a role in
# this account.
###############################################################################

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

resource "aws_kms_key" "main" {
  description             = "${var.project_name}-${var.environment} application data at rest"
  enable_key_rotation     = true
  deletion_window_in_days = var.deletion_window_in_days

  # Inline rather than an aws_iam_policy_document: a key policy's "*" resource
  # means "this key", which IAM-policy linters misread as account-wide.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        # CloudWatch alarms publish to the encrypted SNS topic as the CloudWatch
        # service, so the service needs to generate data keys under this key.
        Sid       = "CloudWatchAlarmsToEncryptedTopic"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource  = "*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
        }
      },
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-data"
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.project_name}-${var.environment}-data"
  target_key_id = aws_kms_key.main.key_id
}
