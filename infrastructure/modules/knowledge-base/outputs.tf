# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "knowledge_base_id" {
  description = "ID of the Bedrock Knowledge Base"
  value       = aws_bedrockagent_knowledge_base.main.id
}

output "data_source_id" {
  description = "ID of the Bedrock Knowledge Base data source"
  value       = aws_bedrockagent_data_source.s3.data_source_id
}
