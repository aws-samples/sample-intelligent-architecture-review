# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for document upload and management endpoints."""

import io
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.auth import get_current_user
from app.api.dependencies import get_document_store, get_validation_store
from app.main import app
from app.services.document_store import DocumentStore
from app.services.validation_store import ValidationStore


@pytest.fixture
def client():
    store = ValidationStore()
    store._available = False
    document_store = DocumentStore()
    document_store._available = False
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test-user"}
    app.dependency_overrides[get_validation_store] = lambda: store
    app.dependency_overrides[get_document_store] = lambda: document_store
    try:
        with patch("boto3.client"):
            yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_validation_store, None)
        app.dependency_overrides.pop(get_document_store, None)


class TestHealthCheck:
    """Tests for the health check endpoint."""

    def test_health_check(self, client):
        """The public probe reports liveness and nothing else.

        It used to return a `config` block naming the S3 bucket, table, model and
        Knowledge Base ids. Since /health is unauthenticated and the ALB is
        internet-facing, that was reconnaissance data for anyone who found the
        DNS name — it now lives behind admin auth on /api/health/detail.
        See tests/backend/test_health_exposure.py.
        """
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"].endswith("-validation")
        assert "config" not in data
        assert "dependencies" not in data


class TestDocumentUpload:
    """Tests for document upload endpoint."""

    def test_upload_pdf(self, client):
        """Test uploading a valid PDF file."""
        # Create a minimal PDF-like content
        pdf_content = b"%PDF-1.4 fake content for testing"
        file = io.BytesIO(pdf_content)

        response = client.post(
            "/api/documents",
            files={"file": ("test_document.pdf", file, "application/pdf")},
            params={"hosting_scope": "both"},
        )

        assert response.status_code == 201
        data = response.json()
        assert "document_id" in data
        assert data["filename"] == "test_document.pdf"
        assert data["status"] == "uploaded"

    def test_upload_non_pdf_rejected(self, client):
        """Test that non-PDF files are rejected."""
        file = io.BytesIO(b"not a pdf")

        response = client.post(
            "/api/documents",
            files={"file": ("test.txt", file, "text/plain")},
            params={"hosting_scope": "both"},
        )

        assert response.status_code == 400
        assert "Unsupported file type" in response.json()["detail"]

    def test_upload_empty_file_rejected(self, client):
        """Test that empty files are rejected."""
        file = io.BytesIO(b"")

        response = client.post(
            "/api/documents",
            files={"file": ("empty.pdf", file, "application/pdf")},
            params={"hosting_scope": "both"},
        )

        assert response.status_code == 400


class TestDocumentList:
    """Tests for document listing endpoint."""

    def test_list_documents_empty(self, client):
        """Test listing documents when none exist."""
        response = client.get("/api/documents")
        assert response.status_code == 200
        data = response.json()
        assert "documents" in data
        assert "total" in data

    def test_list_documents_after_upload(self, client):
        """Test listing documents after uploading one."""
        # Upload a document first
        pdf_content = b"%PDF-1.4 test content"
        file = io.BytesIO(pdf_content)
        client.post(
            "/api/documents",
            files={"file": ("test.pdf", file, "application/pdf")},
            params={"hosting_scope": "both"},
        )

        response = client.get("/api/documents")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1


class TestDocumentGet:
    """Tests for getting a specific document."""

    def test_get_nonexistent_document(self, client):
        """Test getting a document that doesn't exist."""
        response = client.get("/api/documents/nonexistent-id")
        assert response.status_code == 404

    def test_get_uploaded_document(self, client):
        """Test getting a document that was uploaded."""
        # Upload first
        pdf_content = b"%PDF-1.4 test content for get"
        file = io.BytesIO(pdf_content)
        upload_response = client.post(
            "/api/documents",
            files={"file": ("get_test.pdf", file, "application/pdf")},
            params={"hosting_scope": "both"},
        )
        doc_id = upload_response.json()["document_id"]

        # Get it
        response = client.get(f"/api/documents/{doc_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["document_id"] == doc_id
        assert data["filename"] == "get_test.pdf"
