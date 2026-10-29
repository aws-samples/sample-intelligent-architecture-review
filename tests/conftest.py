# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Root test configuration shared by every suite.

Two jobs:

  1. Make the test environment hermetic. The backend validates its config at
     import time and boto3 clients are constructed eagerly in some stores, so
     without fake credentials + a disabled IMDS lookup a plain `pytest` run
     either fails config validation or stalls ~170s per client while boto3
     probes the EC2 metadata endpoint. Setting these here means contributors run
     `pytest` with no wrapper script.

  2. Gate the E2E suite. E2E talks to a deployed environment (real AWS, real
     Bedrock spend), so it is deselected unless `--run-e2e` is passed.
"""

from __future__ import annotations

import os

import pytest

# --- hermetic defaults (applied before test modules import the app) ----------
# Only set what isn't already provided, so a deliberate override still wins.
_TEST_ENV = {
    # Never let boto3 reach for instance metadata in a test run.
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    # Required by app.config's validation.
    "S3_DOCUMENTS_BUCKET": "test-documents-bucket",
    "DYNAMODB_TABLE_PREFIX": "test",
}
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)


def pytest_addoption(parser):
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run end-to-end tests against a deployed environment "
             "(needs real AWS credentials and E2E_API_URL).",
    )


def pytest_collection_modifyitems(config, items):
    """Skip anything marked e2e unless --run-e2e was passed."""
    if config.getoption("--run-e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="needs --run-e2e (targets a live deployment)")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
