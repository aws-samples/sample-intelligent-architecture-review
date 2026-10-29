# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# DynamoDB Module - one table per persisted entity
###############################################################################

###############################################################################
# Documents Table
###############################################################################

resource "aws_dynamodb_table" "documents" {
  name         = "${var.project_name}-${var.environment}-documents"
  billing_mode = "PAY_PER_REQUEST"

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
  hash_key = "id"

  attribute {
    name = "id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-documents"
  })
}

###############################################################################
# Validations Table
###############################################################################

resource "aws_dynamodb_table" "validations" {
  name         = "${var.project_name}-${var.environment}-validations"
  billing_mode = "PAY_PER_REQUEST"

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
  hash_key = "id"

  attribute {
    name = "id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-validations"
  })
}

###############################################################################
# Standards Table
###############################################################################

resource "aws_dynamodb_table" "standards" {
  name         = "${var.project_name}-${var.environment}-standards"
  billing_mode = "PAY_PER_REQUEST"

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
  hash_key = "id"

  attribute {
    name = "id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-standards"
  })
}

###############################################################################
# Chat Jobs Table (async curator chat)
#
# POST /api/standards/chat returns 202+job_id; the FastAPI background
# task writes the in-flight + final state to this table. Frontend
# polls GET /chat/jobs/{job_id} until status is terminal. Records
# auto-expire 24h after creation via the `ttl` attribute.
###############################################################################

resource "aws_dynamodb_table" "chat_jobs" {
  name         = "${var.project_name}-${var.environment}-chat-jobs"
  billing_mode = "PAY_PER_REQUEST"

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
  hash_key = "id"

  attribute {
    name = "id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # Ephemeral request-response state, but PITR stays on like every other table:
  # rows expire after 24h, so the backup cost is negligible.
  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-chat-jobs"
  })
}

###############################################################################
# References Table (factual context blocks)
#
# References are scope-tagged context that the validator attaches
# alongside rules at prompt-build time (e.g. v2 doc page 9's DC-peer
# table). Stored separately from standards so list/filter/curate
# flows don't need to learn to ignore non-rule rows. PITR on — once
# extracted from a doc, References are durable knowledge worth
# restoring.
###############################################################################

resource "aws_dynamodb_table" "references" {
  name         = "${var.project_name}-${var.environment}-references"
  billing_mode = "PAY_PER_REQUEST"

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
  hash_key = "id"

  attribute {
    name = "id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-references"
  })
}

# User↔role assignments, keyed on Cognito sub. Read on every
# authenticated request (role resolution), written only by admins.
resource "aws_dynamodb_table" "user_roles" {
  name         = "${var.project_name}-${var.environment}-user-roles"
  billing_mode = "PAY_PER_REQUEST"

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
  hash_key = "sub"

  attribute {
    name = "sub"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-user-roles"
  })
}

# Audit trail — append-only event log for "who did what, when". One row per
# mutating action (doc upload, rule approve/reject/edit, validation trigger,
# etc.). PK is a unique event_id; two GSIs support the read patterns:
#   - entity_index: per-entity history ("show everything that happened to
#     document X") via entity_key = "<entity_type>#<entity_id>" + ts.
#   - actor_index: per-user activity ("what did user Y do") via actor_sub + ts.
# PITR enabled like the other tables. Append-only by convention (the store
# never updates/deletes rows).
resource "aws_dynamodb_table" "audit_events" {
  name         = "${var.project_name}-${var.environment}-audit-events"
  billing_mode = "PAY_PER_REQUEST"

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
  hash_key = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }
  attribute {
    name = "entity_key"
    type = "S"
  }
  attribute {
    name = "actor_sub"
    type = "S"
  }
  attribute {
    name = "ts"
    type = "S"
  }

  global_secondary_index {
    name            = "entity_index"
    hash_key        = "entity_key"
    range_key       = "ts"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "actor_index"
    hash_key        = "actor_sub"
    range_key       = "ts"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-audit-events"
  })
}

# Custom on-prem icon legend — one row per custom glyph the
# org uses in its architecture diagrams (label -> component type + description,
# plus an optional S3 key for a reference icon crop). Read at diagram
# prompt-build time by extraction + validation; written by curators. Scanned in
# full, so it must stay small — hence TTL below on transient rows.
resource "aws_dynamodb_table" "icon_legend" {
  name         = "${var.project_name}-${var.environment}-icon-legend"
  billing_mode = "PAY_PER_REQUEST"

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
  hash_key = "id"

  attribute {
    name = "id"
    type = "S"
  }

  # Bulk-image interpret jobs persist transient progress rows
  # (record_type="bulk_job", id="JOB#...") here so poll survives multi-task
  # autoscale. They carry a `ttl` epoch so DynamoDB reaps them and they can't
  # accumulate and bloat the full-table scan on the prompt hot path. Icon rows
  # omit `ttl` and are never expired.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-icon-legend"
  })
}
