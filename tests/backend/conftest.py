# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test configuration and fixtures."""

import uuid
from datetime import datetime
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.document import Document, DocumentStatus, DocumentType, ParsedSection
from app.models.validation import (
    ComplianceStatus,
    Finding,
    Severity,
    ValidationResult,
    ValidationStatus,
)
from app.services.validation_store import ValidationStore


@pytest.fixture(autouse=True)
def _rbac_test_admin():
    """Ensure tests that override get_current_user resolve to admin (not viewer)
    so existing tests don't silently 403 after RBAC enforcement was added.

    Uses the user_roles_store in-memory fallback — no DynamoDB needed. Seeds
    admin rows for the common test sub values used across the suite."""
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    prev = dict(user_roles_store._mem)
    for sub in ("test", "test-user", "test-sub"):
        user_roles_store._mem[sub] = {"sub": sub, "role": "admin", "email": "test@example.com"}
    yield
    user_roles_store._mem = prev


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
def mock_store() -> ValidationStore:
    """Create a fresh in-memory validation store for testing."""
    store = ValidationStore()
    store._available = False
    store._documents = {}
    store._validations = {}
    store._standards = {}
    return store


@pytest.fixture
def mock_bedrock_client():
    """Mock Bedrock client for testing without AWS calls."""
    with patch("app.services.bedrock.boto3.client") as mock_client:
        mock_bedrock = MagicMock()
        mock_client.return_value = mock_bedrock
        yield mock_bedrock


@pytest.fixture
def mock_s3_client():
    """Mock S3 client for testing without AWS calls."""
    with patch("app.services.document_store.boto3.client") as mock_client:
        mock_s3 = MagicMock()
        mock_client.return_value = mock_s3
        yield mock_s3


@pytest.fixture
def mock_dynamodb_resource():
    """Mock DynamoDB resource for testing without AWS calls."""
    with patch("app.services.validation_store.boto3.resource") as mock_resource:
        mock_dynamodb = MagicMock()
        mock_resource.return_value = mock_dynamodb
        yield mock_dynamodb


@pytest.fixture
def sample_document() -> Document:
    """Create a sample document for testing."""
    return Document(
        document_id="test-doc-123",
        filename="test-sdd.pdf",
        s3_key="documents/test-doc-123/test-sdd.pdf",
        status=DocumentStatus.PARSED,
        document_type=DocumentType.SDD,
        hosting_scope="both",
        uploaded_at=datetime.utcnow().isoformat(),
        file_size_bytes=1024,
        page_count=5,
        sections=[
            ParsedSection(
                section_id="section-1",
                title="System Architecture",
                content="The system uses AWS RDS with Multi-AZ deployment.",
                page_number=1,
            ),
            ParsedSection(
                section_id="section-2",
                title="High Availability",
                content="We implement failover with ALB across two availability zones.",
                page_number=2,
            ),
        ],
    )


@pytest.fixture
def sample_standards() -> list[dict]:
    """Create sample HA standards for testing."""
    return [
        {
            "id": "HA-001",
            "name": "Multi-AZ Deployment",
            "description": "Database must be deployed across multiple availability zones",
            "severity": "critical",
            "category": "redundancy",
            "validation_criteria": "Check for Multi-AZ configuration in RDS settings",
            "hosting_scope": "both",
            "source": "document",
            "created_at": datetime.utcnow().isoformat(),
        },
        {
            "id": "HA-002",
            "name": "Load Balancer Health Checks",
            "description": "Application Load Balancer must have health checks configured",
            "severity": "high",
            "category": "availability",
            "validation_criteria": "Verify ALB health check settings are enabled",
            "hosting_scope": "both",
            "source": "document",
            "created_at": datetime.utcnow().isoformat(),
        },
    ]


@pytest.fixture
def sample_findings() -> list[Finding]:
    """Create sample validation findings for testing."""
    return [
        Finding(
            finding_id="find-001",
            requirement_id="HA-001",
            requirement_title="Multi-AZ Deployment",
            severity=Severity.CRITICAL,
            status=ComplianceStatus.PASS,
            evidence="RDS is configured with Multi-AZ on page 1",
            recommendation="",
            affected_components=["RDS Database"],
            source_section="System Architecture, page 1",
        ),
        Finding(
            finding_id="find-002",
            requirement_id="HA-002",
            requirement_title="Load Balancer Health Checks",
            severity=Severity.HIGH,
            status=ComplianceStatus.FAIL,
            evidence="No health check configuration found",
            recommendation="Configure health checks on ALB with /health endpoint",
            affected_components=["Application Load Balancer"],
            source_section="High Availability, page 2",
        ),
    ]


@pytest.fixture
def sample_validation(sample_document: Document, sample_findings: list[Finding]) -> ValidationResult:
    """Create a sample validation result for testing."""
    return ValidationResult(
        validation_id="val-test-123",
        document_id=sample_document.document_id,
        status=ValidationStatus.COMPLETED,
        standards_set="default",
        started_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
        overall_score=75.0,
        summary="Validation completed with 1 critical issue",
        findings=sample_findings,
    )


@pytest.fixture
def sample_pdf_bytes():
    """Create minimal valid PDF bytes for testing."""
    # Minimal valid PDF that pymupdf can parse
    pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj

2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj

3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]
   /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj

4 0 obj
<< /Length 44 >>
stream
BT /F1 12 Tf 100 700 Td (Hello World) Tj ET
endstream
endobj

5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj

xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000266 00000 n
0000000360 00000 n

trailer
<< /Size 6 /Root 1 0 R >>
startxref
441
%%EOF"""
    return pdf_content
