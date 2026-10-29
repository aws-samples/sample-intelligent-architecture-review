# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "cluster_id" {
  description = "ID of the ECS cluster"
  value       = aws_ecs_cluster.main.id
}

output "cluster_name" {
  description = "Name of the ECS cluster"
  value       = aws_ecs_cluster.main.name
}

output "service_name" {
  description = "Name of the ECS service"
  value       = aws_ecs_service.app.name
}

output "task_definition_arn" {
  description = "ARN of the task definition"
  value       = aws_ecs_task_definition.app.arn
}

output "alb_dns_name" {
  description = "DNS name of the ALB"
  value       = aws_lb.main.dns_name
}

output "alb_zone_id" {
  description = "Zone ID of the ALB"
  value       = aws_lb.main.zone_id
}

output "alb_arn" {
  description = "ARN of the ALB"
  value       = aws_lb.main.arn
}

# CloudWatch's LoadBalancer dimension wants the "app/<name>/<id>" suffix, NOT the
# full ARN. Passing the ARN yields alarms and widgets that silently never receive
# a datapoint, so expose the suffix explicitly.
output "alb_arn_suffix" {
  description = "ARN suffix of the ALB, for CloudWatch metric dimensions"
  value       = aws_lb.main.arn_suffix
}

output "log_group_name" {
  description = "Name of the ECS task log group (where application logs land)"
  value       = aws_cloudwatch_log_group.ecs.name
}

output "target_group_arn" {
  description = "ARN of the target group"
  value       = aws_lb_target_group.app.arn
}

output "task_execution_role_arn" {
  description = "ARN of the task execution role"
  value       = aws_iam_role.task_execution.arn
}

output "task_role_arn" {
  description = "ARN of the task role"
  value       = aws_iam_role.task_role.arn
}

output "task_role_name" {
  description = "Name of the task role"
  value       = aws_iam_role.task_role.name
}

output "ecs_security_group_id" {
  description = "ID of the ECS service security group"
  value       = aws_security_group.ecs_service.id
}

output "alb_security_group_id" {
  description = "ID of the ALB security group"
  value       = aws_security_group.alb.id
}
