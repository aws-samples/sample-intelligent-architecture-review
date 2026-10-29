# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# ECS Module - Fargate cluster, service, task definition, ALB, auto-scaling
###############################################################################

###############################################################################
# ECS Cluster
###############################################################################

resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-${var.environment}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-cluster"
  })
}

###############################################################################
# CloudWatch Log Group for ECS
###############################################################################

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${var.project_name}-${var.environment}"
  retention_in_days = var.log_retention_days

  tags = var.tags
}

###############################################################################
# Data sources
###############################################################################

# CloudFront's origin-facing IP ranges, for the optional SG restriction. AWS
# maintains this list, so it tracks CloudFront's fleet without us editing CIDRs.
locals {
  # The port CloudFront reaches the ALB on; see the cloudfront module's
  # origin_protocol_policy, which follows the same certificate test.
  origin_port = var.certificate_arn != "" ? 443 : 80
}

data "aws_ec2_managed_prefix_list" "cloudfront" {
  count = var.restrict_alb_to_cloudfront ? 1 : 0
  name  = "com.amazonaws.global.cloudfront.origin-facing"
}

###############################################################################
# Task Execution Role
###############################################################################

data "aws_iam_policy_document" "ecs_task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${var.project_name}-${var.environment}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

###############################################################################
# Task Role (application permissions)
###############################################################################

resource "aws_iam_role" "task_role" {
  name               = "${var.project_name}-${var.environment}-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json

  tags = var.tags
}

###############################################################################
# Task Role Policies - DynamoDB Access
###############################################################################

data "aws_iam_policy_document" "dynamodb_access" {
  statement {
    sid    = "AllowDynamoDBAccess"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:DescribeTable",
    ]

    resources = [
      "arn:aws:dynamodb:${var.aws_region}:*:table/${var.project_name}-${var.environment}-*",
    ]
  }
}

resource "aws_iam_policy" "dynamodb_access" {
  name        = "${var.project_name}-${var.environment}-dynamodb-access"
  description = "Policy for DynamoDB table access"
  policy      = data.aws_iam_policy_document.dynamodb_access.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "dynamodb_to_task_role" {
  role       = aws_iam_role.task_role.name
  policy_arn = aws_iam_policy.dynamodb_access.arn
}

# The tables are encrypted with the customer-managed key, and DynamoDB uses the
# CALLER's permissions on that key for every read and write. Scoped to the one
# key and to calls DynamoDB makes on the task's behalf.
data "aws_iam_policy_document" "data_key_access" {
  statement {
    sid    = "AllowDataKeyViaDynamoDB"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]

    resources = [var.kms_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "data_key_access" {
  name        = "${var.project_name}-${var.environment}-data-key-access"
  description = "Use the application data key for DynamoDB reads and writes"
  policy      = data.aws_iam_policy_document.data_key_access.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "data_key_to_task_role" {
  role       = aws_iam_role.task_role.name
  policy_arn = aws_iam_policy.data_key_access.arn
}

###############################################################################
# Task Role Policies - S3 Access
###############################################################################

data "aws_iam_policy_document" "s3_access" {
  statement {
    sid    = "AllowS3Access"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]

    resources = [
      "arn:aws:s3:::${var.project_name}-${var.environment}-documents-*",
      "arn:aws:s3:::${var.project_name}-${var.environment}-documents-*/*",
    ]
  }
}

resource "aws_iam_policy" "s3_access" {
  name        = "${var.project_name}-${var.environment}-s3-access"
  description = "Policy for S3 bucket access"
  policy      = data.aws_iam_policy_document.s3_access.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "s3_to_task_role" {
  role       = aws_iam_role.task_role.name
  policy_arn = aws_iam_policy.s3_access.arn
}

###############################################################################
# Task Role Policies - SSM read (Cognito secrets, etc)
###############################################################################

data "aws_iam_policy_document" "ssm_read" {
  statement {
    sid    = "AllowSsmRead"
    effect = "Allow"

    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]

    resources = [
      "arn:aws:ssm:${var.aws_region}:*:parameter/${var.project_name}/${var.environment}/*",
    ]
  }

  statement {
    sid       = "AllowSsmDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "ssm_read" {
  name        = "${var.project_name}-${var.environment}-ssm-read"
  description = "Read project SSM parameters and decrypt SecureString values"
  policy      = data.aws_iam_policy_document.ssm_read.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ssm_read_to_task_role" {
  role       = aws_iam_role.task_role.name
  policy_arn = aws_iam_policy.ssm_read.arn
}

# Tracing: let the task export OpenTelemetry spans to X-Ray and publish the
# derived metrics. None of these X-Ray actions accept a resource ARN, so "*" is
# the only expressible scope (same rationale as the Bedrock ListFoundationModels
# statement in modules/bedrock).
resource "aws_iam_policy" "tracing" {
  count       = var.enable_tracing ? 1 : 0
  name        = "${var.project_name}-${var.environment}-tracing"
  description = "Export OpenTelemetry spans to AWS X-Ray and emit trace metrics"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          # PutSpans is the action behind the OTLP ingestion endpoint
          # (https://xray.<region>.amazonaws.com/v1/traces) the ADOT distro posts
          # to in agent-observability mode. PutTraceSegments is kept for the
          # classic segment API so the task still works if the exporter is
          # repointed at a collector.
          "xray:PutSpans",
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
          "xray:GetSamplingStatisticSummaries",
        ]
        Resource = "*"
      },
      {
        # Application Signals derives service-level metrics from the spans.
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = ["ApplicationSignals", "AWS/ApplicationSignals"] }
        }
      },
    ]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "tracing_to_task_role" {
  count      = var.enable_tracing ? 1 : 0
  role       = aws_iam_role.task_role.name
  policy_arn = aws_iam_policy.tracing[0].arn
}

###############################################################################
# Task Definition
###############################################################################

resource "aws_ecs_task_definition" "app" {
  family                   = "${var.project_name}-${var.environment}-app"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task_role.arn

  container_definitions = jsonencode([
    {
      name      = "${var.project_name}-app"
      image     = var.container_image
      essential = true

      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        }
      ]

      environment = var.container_environment

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "app"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:${var.container_port}${var.health_check_path} || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])

  tags = var.tags
}

###############################################################################
# ALB Security Group
###############################################################################

resource "aws_security_group" "alb" {
  name        = "${var.project_name}-${var.environment}-alb-sg"
  description = "Security group for ALB"
  vpc_id      = var.vpc_id

  # Two independent controls keep the ALB behind CloudFront: this security
  # group admits only CloudFront's origin-facing ranges, and the listener
  # rejects any request without the X-Origin-Verify header.
  #
  # Only the ONE port CloudFront actually connects on is opened: 443 when the
  # ALB has a certificate (the origin is https-only), 80 otherwise. That keeps
  # the prefix list — ~55 entries, each counted against the 60-rule security
  # group quota — referenced once rather than twice.
  dynamic "ingress" {
    for_each = var.restrict_alb_to_cloudfront ? [] : [local.origin_port]
    content {
      description = "Public (listener enforces the origin-verify header)"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  dynamic "ingress" {
    for_each = var.restrict_alb_to_cloudfront ? [local.origin_port] : []
    content {
      description     = "CloudFront origin-facing ranges only"
      from_port       = ingress.value
      to_port         = ingress.value
      protocol        = "tcp"
      prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront[0].id]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-alb-sg"
  })
}

###############################################################################
# ECS Service Security Group
###############################################################################

resource "aws_security_group" "ecs_service" {
  name        = "${var.project_name}-${var.environment}-ecs-sg"
  description = "Security group for ECS tasks"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Allow traffic from ALB"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-ecs-sg"
  })
}

###############################################################################
# Application Load Balancer
###############################################################################

resource "aws_lb" "main" {
  name               = "${var.project_name}-${var.environment}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  # Match CloudFront's origin_read_timeout (180s). The PPTX upload
  # route is the slow path: large decks take 30-60s end-to-end after
  # LibreOffice conversion + slide rasterise + KB sync. Default 60s
  # idle_timeout was cutting the connection mid-flight, surfacing
  # as 504 even when the backend completed successfully.
  idle_timeout = 180

  # Strip malformed headers instead of forwarding them. This matters here
  # specifically: the listener authorizes requests on the X-Origin-Verify
  # header, and header-smuggling tricks rely on the LB passing invalid header
  # fields through to the target.
  drop_invalid_header_fields = true

  # Guards the stack's only public entry point against an accidental delete.
  # Set alb_deletion_protection = false and apply before `terraform destroy`.
  enable_deletion_protection = var.alb_deletion_protection

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-alb"
  })
}

###############################################################################
# Target Group
###############################################################################

resource "aws_lb_target_group" "app" {
  name        = "${var.project_name}-${var.environment}-tg"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    enabled             = true
    healthy_threshold   = 3
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
    path                = var.health_check_path
    protocol            = "HTTP"
    matcher             = "200"
  }

  tags = var.tags
}

###############################################################################
# ALB Listeners
###############################################################################

locals {
  # When enabled, the listener's DEFAULT action rejects and only the rule below
  # — which requires CloudFront's X-Origin-Verify header — forwards to the app.
  # Otherwise the ALB is reachable by anyone who learns its DNS name, bypassing
  # CloudFront (and any WAF or caching in front of it).
  #
  # Gated on a static bool, NOT on the secret's value: the secret is generated at
  # apply time, and a `count` cannot depend on an apply-time value.
  verify_origin = var.verify_origin_header
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    # Priority: redirect to HTTPS when a cert exists; else reject unverified
    # traffic when the origin secret is set; else forward (open, dev-only).
    type = (var.certificate_arn != "" ? "redirect"
    : local.verify_origin ? "fixed-response" : "forward")
    target_group_arn = (
      var.certificate_arn != "" || local.verify_origin
      ? null : aws_lb_target_group.app.arn
    )

    dynamic "redirect" {
      for_each = var.certificate_arn != "" ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }

    dynamic "fixed_response" {
      for_each = var.certificate_arn == "" && local.verify_origin ? [1] : []
      content {
        content_type = "application/json"
        # Deliberately terse: an unverified caller learns nothing about the stack.
        message_body = "{\"detail\":\"Direct access is not permitted.\"}"
        status_code  = "403"
      }
    }
  }

  tags = var.tags
}

# Only requests carrying the shared secret reach the app. CloudFront adds the
# header on every origin request (see the cloudfront module), so normal traffic is
# unaffected while a direct hit on the ALB gets the 403 above.
#
# ALB target-group health checks talk to the task directly, not through the
# listener, so they are unaffected by this rule.
resource "aws_lb_listener_rule" "verified_origin_http" {
  count        = var.certificate_arn == "" && local.verify_origin ? 1 : 0
  listener_arn = aws_lb_listener.http.arn
  priority     = 1

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [var.origin_verify_secret]
    }
  }

  tags = var.tags
}

resource "aws_lb_listener" "https" {
  count             = var.certificate_arn != "" ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type = local.verify_origin ? "fixed-response" : "forward"
    target_group_arn = (
      local.verify_origin ? null : aws_lb_target_group.app.arn
    )

    dynamic "fixed_response" {
      for_each = local.verify_origin ? [1] : []
      content {
        content_type = "application/json"
        message_body = "{\"detail\":\"Direct access is not permitted.\"}"
        status_code  = "403"
      }
    }
  }

  tags = var.tags
}

resource "aws_lb_listener_rule" "verified_origin_https" {
  count        = var.certificate_arn != "" && local.verify_origin ? 1 : 0
  listener_arn = aws_lb_listener.https[0].arn
  priority     = 1

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [var.origin_verify_secret]
    }
  }

  tags = var.tags
}

###############################################################################
# ECS Service
###############################################################################

resource "aws_ecs_service" "app" {
  name            = "${var.project_name}-${var.environment}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 0

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ecs_service.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "${var.project_name}-app"
    container_port   = var.container_port
  }

  depends_on = [aws_lb_listener.https]

  tags = var.tags
}

###############################################################################
# Auto Scaling
###############################################################################

resource "aws_appautoscaling_target" "ecs" {
  max_capacity       = var.max_capacity
  min_capacity       = var.min_capacity
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.project_name}-${var.environment}-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.ecs.resource_id
  scalable_dimension = aws_appautoscaling_target.ecs.scalable_dimension
  service_namespace  = aws_appautoscaling_target.ecs.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

resource "aws_appautoscaling_policy" "memory" {
  name               = "${var.project_name}-${var.environment}-memory-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.ecs.resource_id
  scalable_dimension = aws_appautoscaling_target.ecs.scalable_dimension
  service_namespace  = aws_appautoscaling_target.ecs.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageMemoryUtilization"
    }
    target_value       = 80.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

###############################################################################
# Backend (FastAPI) ECR repository
#
# Originally lived in deploy.sh as a bare-CLI ECR resource; pulled into
# Terraform so a fresh-account `deploy.sh all` runs
# infra → image push → service update without manual `aws ecr
# create-repository` calls.
###############################################################################

resource "aws_ecr_repository" "backend" {
  name                 = var.project_name
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-backend"
  })
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

###############################################################################
# pptx-converter sidecar
#
# LibreOffice headless behind a tiny FastAPI service. Internal-only,
# reachable from the main API via Cloud Map at
# pptx-converter.${project}-${env}.local:8001. The container has no
# AWS credentials — its only S3 access is via per-request presigned
# URLs supplied by the main API. Lives in this module so it shares
# the cluster, task-execution role, and main-service SG with the API.
###############################################################################

resource "aws_ecr_repository" "pptx_converter" {
  name                 = "${var.project_name}-${var.environment}/pptx-converter"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-pptx-converter"
  })
}

resource "aws_ecr_lifecycle_policy" "pptx_converter" {
  repository = aws_ecr_repository.pptx_converter.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "pptx_converter" {
  name              = "/ecs/${var.project_name}-${var.environment}-pptx-converter"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# Cloud Map private DNS namespace — gives the sidecar (and any
# future internal service) a stable name.local domain inside the VPC.
resource "aws_service_discovery_private_dns_namespace" "internal" {
  name        = "${var.project_name}-${var.environment}.local"
  description = "Private DNS namespace for internal services"
  vpc         = var.vpc_id
  tags        = var.tags
}

resource "aws_service_discovery_service" "pptx_converter" {
  name = "pptx-converter"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

# Sidecar SG: only ingress is from the main ECS service SG on the
# container port. No public exposure.
resource "aws_security_group" "pptx_converter" {
  name        = "${var.project_name}-${var.environment}-pptx-converter-sg"
  description = "Sidecar reachable only from the API SG on port 8001"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Allow traffic from API service only"
    from_port       = 8001
    to_port         = 8001
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_service.id]
  }

  egress {
    description = "All egress (HTTPS to S3 via presigned URLs, ECR image pulls)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-pptx-converter-sg"
  })
}

# Dedicated task role with no permissions. We want to make
# the no-creds posture explicit; future narrow permissions can be
# added here without disturbing the main API role.
resource "aws_iam_role" "pptx_converter_task" {
  name               = "${var.project_name}-${var.environment}-pptx-converter-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
  tags               = var.tags
}

resource "aws_ecs_task_definition" "pptx_converter" {
  family                   = "${var.project_name}-${var.environment}-pptx-converter"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.pptx_task_cpu
  memory                   = var.pptx_task_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.pptx_converter_task.arn

  volume {
    name = "pptx-tmp"
  }

  volume {
    name = "pptx-home"
  }

  container_definitions = jsonencode([{
    name      = "pptx-converter"
    image     = "${aws_ecr_repository.pptx_converter.repository_url}:latest"
    essential = true

    # The image is immutable at runtime. LibreOffice needs exactly two writable
    # paths — per-request scratch space in /tmp and its user profile in $HOME —
    # and each is a task-scoped volume (the Dockerfile declares both as VOLUMEs
    # so they start out owned by the non-root user).
    readonlyRootFilesystem = true
    mountPoints = [
      { sourceVolume = "pptx-tmp", containerPath = "/tmp", readOnly = false },
      { sourceVolume = "pptx-home", containerPath = "/home/converter", readOnly = false },
    ]

    portMappings = [{
      containerPort = 8001
      hostPort      = 8001
      protocol      = "tcp"
    }]

    # The sidecar reads SOFFICE_TIMEOUT_S, CONVERTER_TMP_ROOT, and
    # ALLOWED_URL_HOST_SUFFIX. This used to be an empty list, so none of them
    # could be set without a code change.
    environment = [
      for k, v in var.pptx_container_env : {
        name  = k
        value = tostring(v)
      }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.pptx_converter.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "pptx-converter"
      }
    }

    healthCheck = {
      command = [
        "CMD-SHELL",
        "curl -f http://localhost:8001/health || exit 1",
      ]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
  }])

  tags = var.tags
}

resource "aws_ecs_service" "pptx_converter" {
  name            = "${var.project_name}-${var.environment}-pptx-converter"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.pptx_converter.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.pptx_converter.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.pptx_converter.arn
  }

  # deploy.sh forces new-deployment on :latest pushes; don't fight it.
  lifecycle {
    ignore_changes = [task_definition]
  }

  tags = var.tags
}
