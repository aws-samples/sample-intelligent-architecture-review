# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# CloudFront Module - Distribution with OAC for S3 frontend
###############################################################################

###############################################################################
# Origin Access Control
###############################################################################

###############################################################################
# CloudFront Function — rewrite SPA paths to index.html
###############################################################################
# Next.js static export with trailingSlash:true emits index.html inside every
# route folder. S3 returns 403 for "directory" requests, so paths like
# /validation/detail/ break unless we rewrite to /validation/detail/index.html
# at the viewer-request layer before CloudFront forwards to S3.

resource "aws_cloudfront_function" "spa_rewrite" {
  name    = "${var.project_name}-${var.environment}-spa-rewrite"
  runtime = "cloudfront-js-2.0"
  comment = "Rewrite SPA paths to index.html for static-export Next.js"
  publish = true
  code    = file("${path.module}/spa-rewrite.js")
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.project_name}-${var.environment}-frontend-oac"
  description                       = "OAC for frontend S3 bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

###############################################################################
# Access logs
#
# Every viewer request is recorded to a dedicated bucket, which is the only
# record of traffic that CloudFront answers itself (cached assets, rejected
# requests) and so never reaches the ALB or the application log.
#
# CloudFront's standard logging writes with an S3 ACL grant to its log-delivery
# account, so this bucket — unlike the others in the stack — keeps ACLs enabled
# (BucketOwnerPreferred). Public access stays fully blocked; that grant is not
# public.
###############################################################################

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "access_logs" {
  bucket        = "${var.project_name}-${var.environment}-cf-logs-${data.aws_caller_identity.current.account_id}"
  force_destroy = true

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-cf-logs"
  })
}

resource "aws_s3_bucket_ownership_controls" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  # SSE-S3: CloudFront standard logging cannot deliver to a bucket whose default
  # encryption is SSE-KMS.
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.access_log_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

###############################################################################
# CloudFront Distribution
###############################################################################

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  comment             = "${var.project_name} ${var.environment} frontend distribution"

  origin {
    domain_name              = var.s3_bucket_regional_domain_name
    origin_id                = "S3-${var.project_name}-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  dynamic "origin" {
    for_each = var.alb_dns_name != "" ? [1] : []
    content {
      domain_name = var.alb_dns_name
      origin_id   = "ALB-${var.project_name}-backend"

      # Proves to the ALB that a request came through CloudFront. Without it the
      # listener answers 403, so the API can't be reached by hitting the ALB's
      # DNS name directly (which would bypass CloudFront entirely).
      dynamic "custom_header" {
        for_each = var.verify_origin_header ? [1] : []
        content {
          name  = "X-Origin-Verify"
          value = var.origin_verify_secret
        }
      }

      custom_origin_config {
        http_port  = 80
        https_port = 443
        # Encrypt CloudFront -> ALB whenever the ALB has a certificate.
        # Plain HTTP puts the Authorization header (a Cognito ID token) on the
        # public AWS network in clear text, so this follows the cert.
        origin_protocol_policy = var.alb_has_certificate ? "https-only" : "http-only"
        origin_ssl_protocols   = ["TLSv1.2"]
        # PPTX upload route can take 30-60s on large decks (52-slide
        # deck end-to-end: LibreOffice convert + S3 + slide rasterise
        # + KB sync trigger). Default CloudFront origin_read_timeout
        # of 30s cut the connection mid-flight, surfacing as 504 to
        # the user even though the backend completed successfully.
        # AWS default-tier accounts cap at 60s; requesting higher
        # requires a Service Quotas increase. 60s + the existing
        # async background-task pattern covers most cases;
        # truly slow uploads (100+ slide decks) need the quota bump.
        origin_read_timeout      = 60
        origin_keepalive_timeout = 60
      }
    }
  }

  # API routes → ALB backend
  dynamic "ordered_cache_behavior" {
    for_each = var.alb_dns_name != "" ? [1] : []
    content {
      path_pattern           = "/api/*"
      allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
      cached_methods         = ["GET", "HEAD"]
      target_origin_id       = "ALB-${var.project_name}-backend"
      viewer_protocol_policy = "redirect-to-https"

      forwarded_values {
        query_string = true
        headers      = ["Authorization", "Content-Type", "Origin", "Accept"]
        cookies {
          forward = "all"
        }
      }

      min_ttl     = 0
      default_ttl = 0
      max_ttl     = 0
      compress    = true
    }
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "S3-${var.project_name}-frontend"
    viewer_protocol_policy = "redirect-to-https"

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_rewrite.arn
    }

    min_ttl     = 0
    default_ttl = 3600
    max_ttl     = 86400
    compress    = true
  }

  # No custom_error_response: with static export + trailingSlash, all routes
  # have real HTML files in S3. custom_error_response is distribution-wide and
  # would intercept API 404s from the ALB origin, breaking the backend.

  logging_config {
    bucket          = aws_s3_bucket.access_logs.bucket_domain_name
    include_cookies = false
    prefix          = "cloudfront/"
  }

  # The ACL grant CloudFront adds for log delivery needs ownership controls in
  # place first.
  depends_on = [aws_s3_bucket_ownership_controls.access_logs]

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = var.acm_certificate_arn == "" ? true : false
    acm_certificate_arn            = var.acm_certificate_arn != "" ? var.acm_certificate_arn : null
    ssl_support_method             = var.acm_certificate_arn != "" ? "sni-only" : null

    # Only meaningful with a CUSTOM certificate. On the default
    # *.cloudfront.net certificate AWS forces minimum_protocol_version to
    # "TLSv1" and silently ignores any other value — so setting it
    # unconditionally produced a PERPETUAL DIFF
    # ("TLSv1" -> "TLSv1.2_2021") that never converged, making every single
    # apply perform a slow, pointless CloudFront update and burying real
    # changes in the plan. null lets the provider omit the field and match
    # what AWS actually stores. (The same AWS-side pinning is recorded
    # against CKV2_AWS_42 in infrastructure/.checkov.yml.)
    minimum_protocol_version = var.acm_certificate_arn != "" ? "TLSv1.2_2021" : null
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-cdn"
  })
}
