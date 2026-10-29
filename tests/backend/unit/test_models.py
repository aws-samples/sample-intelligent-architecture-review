# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for data models."""

import pytest
from datetime import datetime

from app.models.document import Document, DocumentStatus, DocumentType, ParsedSection
from app.models.validation import (
    ComplianceStatus,
    Finding,
    Severity,
    ValidationResult,
    ValidationStatus,
)
from app.models.standard import CreateStandardRequest, UpdateStandardRequest


class TestDocumentModel:
    """Test Document model validation."""

    @pytest.mark.unit
    def test_document_creation(self):
        """Test creating a valid document."""
        doc = Document(
            document_id="test-123",
            filename="test.pdf",
            s3_key="documents/test-123/test.pdf",
            status=DocumentStatus.UPLOADED,
            document_type=DocumentType.SDD,
            hosting_scope="both",
            uploaded_at=datetime.utcnow().isoformat(),
            file_size_bytes=1024,
        )
        assert doc.document_id == "test-123"
        assert doc.status == DocumentStatus.UPLOADED
        assert doc.document_type == DocumentType.SDD

    @pytest.mark.unit
    def test_document_with_sections(self):
        """Test document with parsed sections."""
        doc = Document(
            document_id="test-123",
            filename="test.pdf",
            s3_key="documents/test-123/test.pdf",
            status=DocumentStatus.PARSED,
            document_type=DocumentType.SDD,
            hosting_scope="both",
            uploaded_at=datetime.utcnow().isoformat(),
            file_size_bytes=1024,
            sections=[
                ParsedSection(
                    section_id="sec-1",
                    title="Introduction",
                    content="This is an introduction",
                    page_number=1,
                )
            ],
        )
        assert len(doc.sections) == 1
        assert doc.sections[0].title == "Introduction"


class TestFindingModel:
    """Test Finding model validation."""

    @pytest.mark.unit
    def test_finding_creation(self):
        """Test creating a valid finding."""
        finding = Finding(
            finding_id="find-123",
            requirement_id="HA-001",
            requirement_title="Multi-AZ",
            severity=Severity.CRITICAL,
            status=ComplianceStatus.PASS,
            evidence="Multi-AZ is enabled",
            recommendation="",
        )
        assert finding.severity == Severity.CRITICAL
        assert finding.status == ComplianceStatus.PASS

    @pytest.mark.unit
    def test_finding_with_affected_components(self):
        """Test finding with affected components list."""
        finding = Finding(
            finding_id="find-123",
            requirement_id="HA-001",
            requirement_title="Multi-AZ",
            severity=Severity.HIGH,
            status=ComplianceStatus.FAIL,
            evidence="Not configured",
            recommendation="Enable Multi-AZ",
            affected_components=["RDS", "Database"],
        )
        assert len(finding.affected_components) == 2
        assert "RDS" in finding.affected_components


class TestValidationResultModel:
    """Test ValidationResult model."""

    @pytest.mark.unit
    def test_validation_result_creation(self):
        """Test creating a validation result."""
        validation = ValidationResult(
            validation_id="val-123",
            document_id="doc-123",
            status=ValidationStatus.COMPLETED,
            standards_set="default",
            overall_score=85.5,
        )
        assert validation.status == ValidationStatus.COMPLETED
        assert validation.overall_score == 85.5

    @pytest.mark.unit
    def test_validation_result_with_findings(self, sample_findings):
        """Test validation result with findings."""
        validation = ValidationResult(
            validation_id="val-123",
            document_id="doc-123",
            status=ValidationStatus.COMPLETED,
            standards_set="default",
            findings=sample_findings,
        )
        assert len(validation.findings) == 2


class TestStandardModels:
    """Test Standard request/response models."""

    @pytest.mark.unit
    def test_create_standard_request(self):
        """Test creating a standard request."""
        request = CreateStandardRequest(
            name="Test Standard",
            description="Test description",
            severity="high",
            category="redundancy",
            validation_criteria="Check for X",
            hosting_scope="both",
        )
        assert request.severity == "high"
        assert request.category == "redundancy"

    @pytest.mark.unit
    def test_update_standard_request_partial(self):
        """Test partial update of standard."""
        request = UpdateStandardRequest(
            name="Updated Name",
            severity="critical",
        )
        assert request.name == "Updated Name"
        assert request.severity == "critical"
        assert request.description is None  # Not provided
