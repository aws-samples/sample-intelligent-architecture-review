# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# Bedrock Knowledge Base Module - Managed RAG pipeline
###############################################################################

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  kb_name = "${var.project_name}-${var.environment}-kb"

  # OpenSearch Serverless data-access policies only accept IAM principal
  # ARNs (e.g. arn:aws:iam::123:role/Admin). When Terraform runs under an
  # assumed role the caller ARN is an STS *session* ARN
  # (arn:aws:sts::123:assumed-role/Admin/session) which AOSS will not match,
  # causing 403s on index creation. Normalize it back to the IAM role ARN.
  caller_arn = data.aws_caller_identity.current.arn
  caller_principal_arn = replace(
    local.caller_arn,
    "/:sts::(\\d+):assumed-role/([^/]+)/.*/",
    ":iam::$1:role/$2",
  )
}

###############################################################################
# IAM Role for Bedrock Knowledge Base
###############################################################################

data "aws_iam_policy_document" "kb_assume_role" {
  # Bedrock assumes this role at runtime to read S3 + query AOSS.
  statement {
    sid    = "BedrockService"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    actions = ["sts:AssumeRole"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "kb_role" {
  name               = "${var.project_name}-${var.environment}-kb-role"
  assume_role_policy = data.aws_iam_policy_document.kb_assume_role.json

  tags = var.tags
}

data "aws_iam_policy_document" "kb_policy" {
  # S3 access for reading documents
  statement {
    sid    = "AllowS3Access"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:ListBucket",
    ]

    resources = [
      var.s3_bucket_arn,
      "${var.s3_bucket_arn}/*",
    ]
  }

  # Bedrock model invocation for embeddings
  statement {
    sid    = "AllowBedrockEmbeddings"
    effect = "Allow"

    actions = [
      "bedrock:InvokeModel",
    ]

    resources = [
      "arn:aws:bedrock:${data.aws_region.current.region}::foundation-model/amazon.titan-embed-text-v2:0",
    ]
  }

  # OpenSearch Serverless access (managed by KB)
  statement {
    sid    = "AllowOpenSearchServerless"
    effect = "Allow"

    actions = [
      "aoss:APIAccessAll",
    ]

    resources = [
      "arn:aws:aoss:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:collection/*",
    ]
  }
}

resource "aws_iam_role_policy" "kb_policy" {
  name   = "${var.project_name}-${var.environment}-kb-policy"
  role   = aws_iam_role.kb_role.id
  policy = data.aws_iam_policy_document.kb_policy.json
}

###############################################################################
# OpenSearch Serverless Collection (managed by KB)
###############################################################################

resource "aws_opensearchserverless_security_policy" "kb_encryption" {
  name = "${var.project_name}-${var.environment}-kb-enc"
  type = "encryption"

  policy = jsonencode({
    Rules = [
      {
        Resource     = ["collection/${local.kb_name}"]
        ResourceType = "collection"
      }
    ]
    AWSOwnedKey = true
  })
}

resource "aws_opensearchserverless_security_policy" "kb_network" {
  name = "${var.project_name}-${var.environment}-kb-net"
  type = "network"

  policy = jsonencode([
    {
      Rules = [
        {
          Resource     = ["collection/${local.kb_name}"]
          ResourceType = "collection"
        }
      ]
      AllowFromPublic = true
    }
  ])
}

resource "aws_opensearchserverless_access_policy" "kb_data" {
  name = "${var.project_name}-${var.environment}-kb-data"
  type = "data"

  policy = jsonencode([
    {
      Rules = [
        {
          Resource     = ["collection/${local.kb_name}"]
          ResourceType = "collection"
          Permission   = ["aoss:*"]
        },
        {
          Resource     = ["index/${local.kb_name}/*"]
          ResourceType = "index"
          Permission   = ["aoss:*"]
        }
      ]
      Principal = [
        aws_iam_role.kb_role.arn,
        local.caller_principal_arn,
      ]
    }
  ])
}

resource "aws_opensearchserverless_collection" "kb" {
  name = local.kb_name
  type = "VECTORSEARCH"

  depends_on = [
    aws_opensearchserverless_security_policy.kb_encryption,
    aws_opensearchserverless_security_policy.kb_network,
    aws_opensearchserverless_access_policy.kb_data,
  ]

  tags = var.tags
}

###############################################################################
# OpenSearch index bootstrap
#
# Bedrock Knowledge Base requires a vector index to exist on the
# OpenSearch collection BEFORE the KB can be created — Bedrock will
# NOT create it for you, and CreateKnowledgeBase fails with
# "no such index [bedrock-knowledge-base-default-index]" otherwise.
#
# create_index.py signs a PUT to the collection endpoint (SigV4,
# service "aoss") using botocore + stdlib urllib — no extra pip deps
# on the deploy host. It is idempotent and retries while the freshly
# created collection + its data-access policy finish propagating.
#
# Trigger keyed on the collection ARN so a collection replacement
# re-fires the bootstrap.
###############################################################################

resource "null_resource" "kb_index_bootstrap" {
  triggers = {
    collection_arn = aws_opensearchserverless_collection.kb.arn
  }

  provisioner "local-exec" {
    command = "python3 ${path.module}/create_index.py ${aws_opensearchserverless_collection.kb.collection_endpoint} ${data.aws_region.current.region}"
  }

  depends_on = [
    aws_opensearchserverless_collection.kb,
    # The data-access policy names the deploy principal (caller ARN),
    # which is what authorizes the signed index PUT below. Must exist
    # first.
    aws_opensearchserverless_access_policy.kb_data,
  ]
}

###############################################################################
# Bedrock Knowledge Base
###############################################################################

resource "aws_bedrockagent_knowledge_base" "main" {
  name     = local.kb_name
  role_arn = aws_iam_role.kb_role.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${data.aws_region.current.region}::foundation-model/amazon.titan-embed-text-v2:0"
    }
  }

  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"
    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.kb.arn
      vector_index_name = "bedrock-knowledge-base-default-index"
      field_mapping {
        vector_field   = "bedrock-knowledge-base-default-vector"
        text_field     = "AMAZON_BEDROCK_TEXT_CHUNK"
        metadata_field = "AMAZON_BEDROCK_METADATA"
      }
    }
  }

  # The vector index must exist before CreateKnowledgeBase runs, else
  # Bedrock 404s on the index. kb_index_bootstrap creates it.
  depends_on = [null_resource.kb_index_bootstrap]

  tags = var.tags
}

###############################################################################
# Bedrock Data Source (S3)
###############################################################################

resource "aws_bedrockagent_data_source" "s3" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.main.id
  name              = "${var.project_name}-${var.environment}-s3-source"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn = var.s3_bucket_arn
    }
  }
}
