# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document data models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.models.standard import HostingScope


class DocumentStatus(str, Enum):
    """Document processing status."""
    UPLOADED = "uploaded"
    PARSING = "parsing"
    PARSED = "parsed"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    FAILED = "failed"


class DocumentType(str, Enum):
    """Document type classification."""
    SDD = "sdd"
    STANDARDS = "standards"


class SourceFormat(str, Enum):
    """The original file format the user uploaded.

    When a `.pptx` is uploaded, we convert it to PDF behind the
    scenes and treat the PDF as the canonical artifact.
    The Document still surfaces this so the UI can render a
    "Source: PowerPoint" badge and the cache key can anchor on the
    pptx source bytes (LibreOffice output is non-deterministic).
    """
    PDF = "pdf"
    PPTX = "pptx"


class SlideType(str, Enum):
    """Heuristic classification of a slide's dominant content type.

    Thresholds (in classify_slide):
      - title_only: text <= 80 chars AND shape_count <= 3
      - text_heavy: text >= 400 chars AND shape_count <= 6 AND no images
      - diagram_heavy: text < 200 chars AND shape_count >= 8
      - hybrid: anything else (the catch-all)
    """
    TITLE_ONLY = "title_only"
    TEXT_HEAVY = "text_heavy"
    DIAGRAM_HEAVY = "diagram_heavy"
    HYBRID = "hybrid"


class SlideExtractionStatus(str, Enum):
    """Per-slide extraction state, used for failure isolation.

    Mirrors the chunked-validation semantics: a slide that errors
    doesn't fail the whole run; it carries a failure status so the
    UI can list it for manual reprocessing.
    """
    PENDING = "pending"
    EXTRACTED = "extracted"
    SKIPPED = "skipped"
    FAILED = "failed"


class SlideStatus(BaseModel):
    """Per-slide extraction state for a PPTX-derived Document."""

    slide_index: int = Field(
        description="1-based slide number, aligned to the converted PDF's page numbers."
    )
    slide_type: SlideType = Field(description="Heuristic classification.")
    status: SlideExtractionStatus = Field(default=SlideExtractionStatus.PENDING)
    rule_count: int = Field(
        default=0, description="Rules saved from this slide during extraction."
    )
    error: Optional[str] = Field(
        default=None,
        description="Captured error message when status == 'failed'. Surfaced verbatim in the UI.",
    )
    skip_reason: Optional[str] = Field(
        default=None,
        description="Why a slide was skipped — e.g. 'title_only — no rules expected'.",
    )
    image_s3_key: Optional[str] = Field(
        default=None,
        description=(
            "S3 key for the rendered slide PNG. The UI presigns this for "
            "thumbnail rendering in the partial-success drawer."
        ),
    )


class ParsedSection(BaseModel):
    """A section extracted from a PDF document."""
    section_id: str = Field(description="Unique section identifier")
    title: str = Field(description="Section heading/title")
    content: str = Field(description="Text content of the section")
    page_number: int = Field(description="Page number where section starts")
    level: int = Field(default=1, description="Heading level (1=top, 2=sub, etc.)")
    images: list[str] = Field(default_factory=list, description="Base64-encoded images in section")


class ExtractedComponent(BaseModel):
    """A component extracted from the SDD."""
    name: str = Field(description="Component name")
    component_type: str = Field(description="Type: server, database, load_balancer, etc.")
    description: str = Field(default="", description="Component description")
    redundancy: Optional[str] = Field(default=None, description="Redundancy pattern: active-active, active-passive, none")
    failure_domain: Optional[str] = Field(default=None, description="Failure domain: AZ, region, rack")
    dependencies: list[str] = Field(default_factory=list, description="Names of components this depends on")
    rto_minutes: Optional[int] = Field(default=None, description="Recovery Time Objective in minutes")
    rpo_minutes: Optional[int] = Field(default=None, description="Recovery Point Objective in minutes")


class Document(BaseModel):
    """A document in the system."""
    document_id: str = Field(description="Unique document identifier")
    filename: str = Field(description="Original filename")
    s3_key: str = Field(description="S3 object key")
    content_hash: Optional[str] = Field(default=None, description="SHA256 of original file bytes (idempotency key)")
    status: DocumentStatus = Field(default=DocumentStatus.UPLOADED)
    document_type: DocumentType = Field(default=DocumentType.SDD, description="Document type: sdd or standards")
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
    # Audit trail — who uploaded this document (Cognito email). Optional so
    # older rows without attribution still deserialize.
    uploaded_by: Optional[str] = Field(default=None, description="Email of the uploader")
    parsed_at: Optional[datetime] = None
    file_size_bytes: int = Field(default=0)
    page_count: int = Field(default=0)
    sections: list[ParsedSection] = Field(default_factory=list)
    components: list[ExtractedComponent] = Field(default_factory=list)
    ha_patterns: list[str] = Field(default_factory=list, description="Identified HA patterns")
    error_message: Optional[str] = None

    # Pipeline status tracking fields
    sync_status: str = Field(default="pending", description="Pipeline sync status: pending, parsing, extracting, syncing, synced, failed")
    processing_step: str = Field(default="", description="Human-readable current processing step")
    sync_error: Optional[str] = Field(default=None, description="Error message if sync failed")
    queued_at: Optional[datetime] = Field(default=None, description="When document was queued for processing")
    sync_started_at: Optional[datetime] = Field(default=None, description="When KB sync started")
    sync_completed_at: Optional[datetime] = Field(default=None, description="When KB sync completed")

    # Tier classification — one of {1,2,3,4,5} or None when unknown.
    # Validation only runs rules whose applies_to_tiers contains the
    # declared_tier. SDDs uploaded before tier-awareness landed will
    # have None and validate against all rules (universal default).
    declared_tier: Optional[int] = Field(
        default=None,
        description="Service tier this SDD targets. 1=Tier-0 critical, 5=lowest. None means unknown / pre-tier-awareness.",
    )
    tier_source: Optional[str] = Field(
        default=None,
        description="How declared_tier was set: 'auto' (LLM classifier) or 'user' (manual override).",
    )

    # Free-form NLP context the user can attach to a
    # standards-document upload to tell the extraction agent how
    # the document is organised (which pages cover which tiers,
    # where substitutions and exceptions live, page-level
    # relationships the agent would otherwise miss).
    #
    # When non-empty, the dispatcher routes to the guided extraction
    # agent which embeds these notes verbatim into the system prompt.
    # When empty / None, the unguided free-form agent runs.
    extraction_notes: Optional[str] = Field(
        default=None,
        description="Optional free-form NLP context describing how the standards document is organised.",
    )
    # Required hosting scope. For SDDs this declares the
    # deployment target (validator filters rules accordingly). For
    # standards docs this tags every extracted rule. Pydantic 422s
    # on omission; the upload form requires the user to choose.
    hosting_scope: HostingScope = Field(
        description="Hosting scope: 'on_prem', 'cloud', or 'both'. For SDDs: the deployment target. For standards docs: the scope every extracted rule inherits.",
    )

    # PPTX upload support. PDF stays canonical; these
    # fields are populated when the user uploaded a .pptx.
    source_format: SourceFormat = Field(
        default=SourceFormat.PDF,
        description="Original file format. PDF is canonical; PPTX is converted-to-PDF behind the scenes.",
    )
    original_s3_key: Optional[str] = Field(
        default=None,
        description="S3 key of the original .pptx (only when source_format == 'pptx'). Lets the UI offer a 'View original' link.",
    )
    source_content_hash: Optional[str] = Field(
        default=None,
        description=(
            "SHA-256 of the ORIGINAL source bytes (the .pptx for PPTX uploads, "
            "the .pdf for native PDF uploads). LibreOffice output is non-"
            "deterministic, so we hash the source — not the converted PDF — "
            "for cache lookups."
        ),
    )
    slide_count: Optional[int] = Field(
        default=None,
        description="Number of slides in the source deck (only when source_format == 'pptx').",
    )
    slides: Optional[list[SlideStatus]] = Field(
        default=None,
        description=(
            "Per-slide extraction state, populated when source_format == 'pptx'. "
            "Lets the UI show 'N of M slides extracted, K errored' instead of "
            "a binary success/fail."
        ),
    )

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class DocumentUploadResponse(BaseModel):
    """Response after uploading a document."""
    document_id: str
    filename: str
    status: DocumentStatus
    message: str
