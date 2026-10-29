# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DDB-backed store for async curator chat jobs.

The /api/standards/chat endpoint used to call Bedrock synchronously
inside the request path, which made it timeout-fragile against the
Browser → CloudFront → ALB → ECS chain. This store decouples it:

  1. POST /api/standards/chat creates a job here, returns 202 + job_id
  2. background task runs Bedrock + persists proposals + completes job
  3. frontend polls GET /api/standards/chat/jobs/{job_id} until terminal

Schema is a single hash key on `id`, with status,
reply, proposals (JSON-encoded list of saved standard records), and
optional ttl for 24h auto-cleanup. No GSI; all access is by job_id.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Env var wins; otherwise derive from the shared table prefix so a
# renamed env / fresh account deploy resolves correctly. No hardcoded
# project fallback — DYNAMODB_TABLE_PREFIX is a required setting
# (config.Settings.validate_startup_config fails fast if unset), so an
# empty prefix here means "misconfigured", not a silent wrong table.
_DEFAULT_TABLE = f"{os.getenv('DYNAMODB_TABLE_PREFIX', '')}-chat-jobs"
_TABLE_NAME = os.getenv("CHAT_JOBS_TABLE", _DEFAULT_TABLE)
_TTL_SECONDS = 24 * 60 * 60  # 24h


class ChatJobsStore:
    def __init__(self):
        self._client = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        self._table = self._client.Table(_TABLE_NAME)

    def create(self, messages: list[dict], hosting_scope: str) -> str:
        """Create a job in pending status. Returns the new job_id."""
        job_id = f"chat-{uuid.uuid4()}"
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "id": job_id,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "hosting_scope": hosting_scope,
            "messages": json.dumps(messages),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + _TTL_SECONDS,
        }
        try:
            self._table.put_item(Item=item)
        except ClientError as e:
            logger.error(f"chat_jobs_store create failed: {e}")
            raise
        return job_id

    def mark_running(self, job_id: str) -> None:
        """Transition pending → running when the background task picks
        the job up. Best-effort; failures are logged but don't propagate
        because the job will still complete or fail downstream."""
        try:
            self._table.update_item(
                Key={"id": job_id},
                UpdateExpression="SET #s = :running, updated_at = :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":running": "running",
                    ":now": datetime.now(timezone.utc).isoformat(),
                },
            )
        except ClientError as e:
            logger.warning(f"chat_jobs_store mark_running failed for {job_id}: {e}")

    def complete(self, job_id: str, reply: str, proposals: list[dict]) -> None:
        """Terminal: write reply + proposals + status=completed."""
        try:
            self._table.update_item(
                Key={"id": job_id},
                UpdateExpression=(
                    "SET #s = :completed, reply = :reply, "
                    "proposals = :proposals, updated_at = :now"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":completed": "completed",
                    ":reply": reply,
                    ":proposals": json.dumps(proposals),
                    ":now": datetime.now(timezone.utc).isoformat(),
                },
            )
        except ClientError as e:
            logger.error(f"chat_jobs_store complete failed for {job_id}: {e}")
            raise

    def fail(self, job_id: str, error: str) -> None:
        """Terminal: write error + status=failed."""
        try:
            self._table.update_item(
                Key={"id": job_id},
                UpdateExpression="SET #s = :failed, #e = :error, updated_at = :now",
                ExpressionAttributeNames={"#s": "status", "#e": "error"},
                ExpressionAttributeValues={
                    ":failed": "failed",
                    ":error": error,
                    ":now": datetime.now(timezone.utc).isoformat(),
                },
            )
        except ClientError as e:
            logger.error(f"chat_jobs_store fail failed for {job_id}: {e}")
            raise

    def get(self, job_id: str) -> Optional[dict]:
        """Read a job by id. Returns the inflated record or None."""
        try:
            resp = self._table.get_item(Key={"id": job_id})
        except ClientError as e:
            logger.error(f"chat_jobs_store get failed for {job_id}: {e}")
            return None
        item = resp.get("Item")
        if not item:
            return None
        # Inflate JSON-encoded fields so callers don't have to.
        if "proposals" in item and isinstance(item["proposals"], str):
            try:
                item["proposals"] = json.loads(item["proposals"])
            except json.JSONDecodeError:
                item["proposals"] = []
        if "messages" in item and isinstance(item["messages"], str):
            try:
                item["messages"] = json.loads(item["messages"])
            except json.JSONDecodeError:
                item["messages"] = []
        return item


chat_jobs_store = ChatJobsStore()
