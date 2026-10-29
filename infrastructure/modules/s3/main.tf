# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# S3 Module - Documents bucket (private) and Frontend bucket (CloudFront origin)
###############################################################################

###############################################################################
# Documents Bucket - Private with versioning and encryption
###############################################################################

resource "aws_s3_bucket" "documents" {
  bucket = "${var.project_name}-${var.environment}-documents-${var.aws_account_id}"

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-documents"
  })
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket = aws_s3_bucket.documents.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

###############################################################################
# Frontend Bucket - private origin for the CloudFront distribution
###############################################################################

resource "aws_s3_bucket" "frontend" {
  bucket = "${var.project_name}-${var.environment}-frontend-${var.aws_account_id}"

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-frontend"
  })
}

# NOTE: an S3 static-website configuration was removed. The bucket is served
# only through CloudFront with Origin Access Control and public access fully
# blocked, so the S3 website endpoint is unreachable by design; the
# index.html rewrite is handled by a CloudFront viewer-request function.

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

###############################################################################
# Frontend Bucket Policy - Allow CloudFront OAC access
###############################################################################

data "aws_iam_policy_document" "frontend_policy" {
  statement {
    sid    = "AllowCloudFrontOAC"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [var.cloudfront_distribution_arn]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_policy.json
}
