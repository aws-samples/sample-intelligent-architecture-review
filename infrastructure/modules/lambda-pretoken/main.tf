# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Pre-token-generation Lambda module
#
# Packages backend/lambdas/pretoken/handler.py as a zip and provisions the
# Lambda + execution role. The function ARN is returned so the cognito module
# can wire it to the user pool's pre_token_generation trigger AND own the
# aws_lambda_permission (the pool ARN is local there). Keeping the invoke
# permission in the cognito module avoids a module-level dependency cycle
# (this module would otherwise need the pool ARN while cognito needs this
# function's ARN). First Lambda in the repo — minimal (stdlib-only handler,
# no build step, no VPC).

locals {
  function_name = "${var.project_name}-${var.environment}-pretoken"
  # Source lives in the backend package, referenced relative to this module.
  source_dir = "${path.module}/../../../backend/lambdas/pretoken"
}

# Zip the handler at plan/apply time. Content-addressed so a handler edit
# triggers a new deploy (source_code_hash below).
data "archive_file" "pretoken" {
  type        = "zip"
  source_dir  = local.source_dir
  output_path = "${path.module}/build/pretoken.zip"
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "pretoken" {
  name               = "${local.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

# Logs only — the handler makes no AWS calls.
resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.pretoken.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "pretoken" {
  function_name    = local.function_name
  role             = aws_iam_role.pretoken.arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.pretoken.output_path
  source_code_hash = data.archive_file.pretoken.output_base64sha256
  timeout          = 5
  memory_size      = 128

  tags = var.tags
}
