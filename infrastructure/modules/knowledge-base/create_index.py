#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Create the Bedrock-required vector index on an OpenSearch Serverless
collection.

Bedrock Knowledge Base will NOT create its vector index for you: the
collection must already have an index named
`bedrock-knowledge-base-default-index` (with the KB's expected field
mapping) before `CreateKnowledgeBase` is called, or it fails with
"no such index [...]" — exactly the 404 a fresh-account deploy hits.

This script is invoked by the `null_resource.kb_index_bootstrap`
local-exec provisioner during `terraform apply`, after the collection
and its data-access policy exist. It is idempotent: an already-present
index is treated as success.

Signing: uses opensearch-py's AWSV4SignerAuth (service "aoss") with the
DEPLOYER's own credentials — the same battle-tested path as the
agentic-knowledge-foundation reference. This means the deploy principal
must (a) be named in the collection's data-access policy — Terraform
adds data.aws_caller_identity.current.arn there — AND (b) hold the IAM
action `aoss:APIAccessAll`. Both are required by AOSS; see the README
"Deploying to a new AWS account" prerequisites.

Requires opensearch-py (see requirements alongside this module).

argv[1] = collection endpoint (https://{id}.{region}.aoss.amazonaws.com)
argv[2] = AWS region
AWS creds come from the standard boto3 chain (same creds Terraform uses).
"""

from __future__ import annotations

import sys
import time

import boto3
from opensearchpy import (
    AWSV4SignerAuth,
    OpenSearch,
    RequestsHttpConnection,
)

INDEX_NAME = "bedrock-knowledge-base-default-index"
SERVICE = "aoss"  # OpenSearch Serverless (NOT "es")

# titan-embed-text-v2:0 emits 1024-dim vectors by default. The field
# names MUST match the KB's storage_configuration.field_mapping in
# main.tf exactly (vector_field / text_field / metadata_field).
EMBED_DIM = 1024
INDEX_BODY = {
    "settings": {"index.knn": True},
    "mappings": {
        "properties": {
            "bedrock-knowledge-base-default-vector": {
                "type": "knn_vector",
                "dimension": EMBED_DIM,
                "method": {
                    "name": "hnsw",
                    "engine": "faiss",
                    "space_type": "l2",
                },
            },
            "AMAZON_BEDROCK_TEXT_CHUNK": {"type": "text"},
            "AMAZON_BEDROCK_METADATA": {"type": "text", "index": False},
        }
    },
}

# AOSS collection + its data-access policy take time to become reachable
# after create — endpoint DNS propagation and IAM data-access
# propagation are the two slow links. Retry generously.
MAX_ATTEMPTS = 12
SLEEP_SECONDS = 15


def _client(endpoint: str, region: str) -> OpenSearch:
    host = endpoint.replace("https://", "").rstrip("/")
    creds = boto3.session.Session().get_credentials()
    if creds is None:
        raise SystemExit("No AWS credentials found for index bootstrap.")
    auth = AWSV4SignerAuth(creds, region, SERVICE)
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        pool_maxsize=20,
        timeout=30,
    )


def create_index(endpoint: str, region: str) -> None:
    client = _client(endpoint, region)

    last_err: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if client.indices.exists(index=INDEX_NAME):
                print(f"Index {INDEX_NAME} already exists; nothing to do.")
                break
            client.indices.create(index=INDEX_NAME, body=INDEX_BODY)
            print(f"Created index {INDEX_NAME}.")
            break
        except Exception as e:  # noqa: BLE001 — 403 while data-policy propagates
            msg = str(e)
            if "resource_already_exists" in msg:
                print(f"Index {INDEX_NAME} already exists; nothing to do.")
                break
            last_err = e
            print(
                f"[attempt {attempt}/{MAX_ATTEMPTS}] index create failed "
                f"({type(e).__name__}); retrying in {SLEEP_SECONDS}s…"
            )
            time.sleep(SLEEP_SECONDS)  # nosemgrep: arbitrary-sleep — deliberate wait between index-create retries
    else:
        raise SystemExit(
            f"Failed to create index {INDEX_NAME} after {MAX_ATTEMPTS} "
            f"attempts: {last_err}. If this is a 403/AuthorizationException, "
            f"the deploy principal is missing the aoss:APIAccessAll IAM "
            f"permission — see README 'Deploying to a new AWS account'."
        )

    # AOSS is eventually consistent; give the index a moment to be
    # queryable before Bedrock validates it during CreateKnowledgeBase.
    print("Waiting 30s for index propagation before KB attach…")
    time.sleep(30)  # nosemgrep: arbitrary-sleep — required wait for AOSS eventual consistency before Bedrock validates the index


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: create_index.py <collection_endpoint> <region>")
    create_index(sys.argv[1], sys.argv[2])
