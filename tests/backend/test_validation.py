# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for validation endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.auth import get_current_user
from app.api.dependencies import get_validation_store
from app.main import app
from app.models.document import Document, DocumentStatus
from app.services.validation_store import ValidationStore


@pytest.fixture
def test_store():
    store = ValidationStore()
    store._available = False
    store._standards["HA-TEST-001"] = {
        "id": "HA-TEST-001",
        "name": "Multi-zone resilience",
        "description": "Critical services use independent failure domains.",
        "severity": "high",
        "category": "availability",
        "validation_criteria": "Confirm deployment across multiple zones.",
        "hosting_scope": "both",
        "status": "active",
        "applies_to_tiers": [1, 2, 3, 4, 5],
    }
    return store


@pytest.fixture
def client(test_store):
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test-user"}
    app.dependency_overrides[get_validation_store] = lambda: test_store
    try:
        with patch(
            "app.api.routes.validation._run_validation",
            new=AsyncMock(),
        ):
            yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_validation_store, None)


@pytest.fixture
def parsed_document(test_store):
    """Create a pre-parsed document in the store for validation tests."""
    from datetime import datetime
    from app.models.document import ExtractedComponent, ParsedSection

    doc = Document(
        document_id="doc-test123",
        filename="test_sdd.pdf",
        s3_key="documents/doc-test123/test_sdd.pdf",
        content_hash="test-content-hash",
        status=DocumentStatus.PARSED,
        hosting_scope="both",
        uploaded_at=datetime.utcnow(),
        parsed_at=datetime.utcnow(),
        file_size_bytes=1024,
        page_count=5,
        sections=[
            ParsedSection(
                section_id="sec-001",
                title="Architecture Overview",
                content="The system uses active-active deployment across two availability zones.",
                page_number=1,
                level=1,
            ),
            ParsedSection(
                section_id="sec-002",
                title="Database Design",
                content="PostgreSQL with synchronous replication and automated failover.",
                page_number=3,
                level=2,
            ),
        ],
        components=[
            ExtractedComponent(
                name="Web Server",
                component_type="server",
                description="Application web server",
                redundancy="active-active",
                failure_domain="az",
            ),
            ExtractedComponent(
                name="PostgreSQL",
                component_type="database",
                description="Primary database",
                redundancy="active-passive",
                failure_domain="az",
            ),
        ],
        ha_patterns=["active-active", "database replication", "multi-az"],
    )

    test_store.save_document(doc)
    return doc


class TestCreateValidation:
    """Tests for creating validations."""

    def test_create_validation_document_not_found(self, client):
        """Test creating validation for non-existent document."""
        response = client.post(
            "/api/validations",
            json={"document_id": "nonexistent", "standards_set": "resilience-review-v1"},
        )
        assert response.status_code == 404

    def test_create_validation_success(self, client, parsed_document):
        """Test creating a validation for a parsed document."""
        response = client.post(
            "/api/validations",
            json={
                "document_id": parsed_document.document_id,
                "standards_set": "resilience-review-v1",
            },
        )
        assert response.status_code == 202
        data = response.json()
        assert "validation_id" in data
        assert data["status"] == "pending"

    def test_create_validation_unparsed_document(self, client, test_store):
        """Test that validation fails for unparsed documents."""
        from datetime import datetime

        doc = Document(
            document_id="doc-unparsed",
            filename="unparsed.pdf",
            s3_key="documents/doc-unparsed/unparsed.pdf",
            status=DocumentStatus.UPLOADED,
            hosting_scope="both",
            uploaded_at=datetime.utcnow(),
            file_size_bytes=1024,
        )
        test_store.save_document(doc)

        response = client.post(
            "/api/validations",
            json={"document_id": "doc-unparsed", "standards_set": "resilience-review-v1"},
        )
        assert response.status_code == 400


class TestListValidations:
    """Tests for listing validations."""

    def test_list_validations(self, client):
        """Test listing all validations."""
        response = client.get("/api/validations")
        assert response.status_code == 200
        data = response.json()
        assert "validations" in data
        assert "total" in data

    def test_list_validations_filter_by_document(self, client, parsed_document):
        """Test filtering validations by document_id."""
        response = client.get(
            f"/api/validations?document_id={parsed_document.document_id}"
        )
        assert response.status_code == 200


class TestGetValidation:
    """Tests for getting a specific validation."""

    def test_get_nonexistent_validation(self, client):
        """Test getting a validation that doesn't exist."""
        response = client.get("/api/validations/nonexistent-id")
        assert response.status_code == 404
