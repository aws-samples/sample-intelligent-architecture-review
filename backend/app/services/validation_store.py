# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DynamoDB-based storage for documents, validations, and queries.

Uses in-memory storage as fallback when DynamoDB is not available.
"""

import hashlib
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError


def _float_to_decimal(obj):
    """Recursively convert floats to Decimals for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: _float_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_float_to_decimal(i) for i in obj]
    return obj


def _status_of(finding) -> str:
    """Finding status as a plain string, whether it's an enum or already str."""
    s = getattr(finding, "status", None)
    return str(getattr(s, "value", s) or "")


def _severity_of(finding) -> str:
    s = getattr(finding, "severity", None)
    return str(getattr(s, "value", s) or "")


# Statuses that represent something the user needs to act on. A passing rule,
# an N/A rule and a rule the document never mentions are not issues, so they
# must not inflate the counts the dashboard shows as "active issues".
_ISSUE_STATUSES = frozenset({"fail", "warning"})


def finding_counts(findings: list) -> dict:
    """Issue counts for a validation, computed once at save time.

    Stored on the DynamoDB row because findings live in S3: a list view would
    otherwise need one S3 GET per row to count them, so it counted an empty
    list instead and every row reported 0.

    ``critical_count``/``high_count`` count FAILING findings only — that is what
    the severity columns and the "active issues" KPI mean. ``findings_count``
    additionally includes warnings, so ``findings_count - critical - high`` (the
    frontend's "medium" bucket) stays non-negative and excludes passes.
    """
    issues = [f for f in findings if _status_of(f) in _ISSUE_STATUSES]
    fails = [f for f in issues if _status_of(f) == "fail"]
    return {
        "findings_count": len(issues),
        "critical_count": sum(1 for f in fails if _severity_of(f) == "critical"),
        "high_count": sum(1 for f in fails if _severity_of(f) == "high"),
    }


def _count_kwargs(item: dict) -> dict:
    """Read the stored issue counts off a row. Absent on rows written before
    they were persisted, where the model defaults (0) take effect."""
    out: dict = {}
    for key in ("findings_count", "critical_count", "high_count"):
        if (v := item.get(key)) is not None:
            out[key] = int(v)
    return out


def _telemetry_kwargs(item: dict) -> dict:
    """Extract chunk/cost telemetry fields from a DynamoDB item.

    Pre-chunked-validator rows don't carry these fields; in that case
    return an empty dict so the ValidationResult defaults (None) take
    effect and the UI can hide the cost footnote.
    """
    out: dict = {}
    if (v := item.get("chunks_per_document")) is not None:
        out["chunks_per_document"] = int(v)
    if (v := item.get("bedrock_calls")) is not None:
        out["bedrock_calls"] = int(v)
    if (v := item.get("bedrock_input_tokens")) is not None:
        out["bedrock_input_tokens"] = int(v)
    if (v := item.get("bedrock_output_tokens")) is not None:
        out["bedrock_output_tokens"] = int(v)
    if (v := item.get("estimated_cost_usd")) is not None:
        out["estimated_cost_usd"] = float(v)
    # Per-filter skipped breakdown. None on rows written before this
    # field existed; the UI falls back to the flat skipped_count then.
    if (v := item.get("skipped_count_breakdown")) is not None:
        out["skipped_count_breakdown"] = {
            k: int(v_) for k, v_ in dict(v).items()
        }
    # Decision-tree rollup + tier provenance. Older rows lack these;
    # the ValidationResult defaults (None / []) take effect and the
    # UI's read-side compat falls back to the flat-finding rendering.
    if (v := item.get("layer_verdicts_json")) is not None:
        import json as _json
        from app.models.validation import LayerVerdictRollup
        try:
            recs = _json.loads(v) if isinstance(v, str) else v
            out["layer_verdicts"] = [LayerVerdictRollup(**rec) for rec in recs]
        except Exception:
            pass
    if (v := item.get("architecture_verdict")) is not None:
        from app.models.validation import LayerVerdict
        try:
            out["architecture_verdict"] = LayerVerdict(str(v))
        except Exception:
            pass
    if (v := item.get("tier_used")) is not None:
        out["tier_used"] = int(v)
    if (v := item.get("tier_override")) is not None:
        out["tier_override"] = int(v)
    return out

from app.config import settings
from app.services.ddb import scan_all
from app.models.document import Document, DocumentStatus
from app.models.validation import ValidationResult, ValidationStatus

logger = logging.getLogger(__name__)

# How long an in-flight extraction stamp is believed. A run that is killed by a
# deploy or scale-in cannot clear its own stamp, and a permanently-shut review
# gate would be worse than a briefly-open one. Comfortably longer than a real
# extraction takes.
EXTRACTION_STATE_STALE_AFTER_SEC = 45 * 60

# How long a PENDING validation is believed. Validations run as in-process
# background tasks, so a task replaced mid-run (deploy, scale-in, failed health
# check) leaves its row PENDING forever: the dashboard counts it as active work,
# and the result cache hands the dead run back to every re-run of the same
# document. A real run takes about 15 minutes at a few hundred rules.
VALIDATION_PENDING_STALE_AFTER_SEC = 60 * 60
INTERRUPTED_MESSAGE = (
    "Interrupted before it finished (the service restarted). Run the "
    "validation again."
)


def _is_stale_pending(status, started_at) -> bool:
    status = status.value if hasattr(status, "value") else str(status or "")
    if status != "pending" or not started_at:
        return False
    if isinstance(started_at, str):
        try:
            started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if started_at.tzinfo is not None:
        started_at = started_at.replace(tzinfo=None) - started_at.utcoffset()
    return (datetime.utcnow() - started_at).total_seconds() > VALIDATION_PENDING_STALE_AFTER_SEC


def _surface_interrupted(validation: ValidationResult) -> ValidationResult:
    """Report a long-dead PENDING run as FAILED, read-side only."""
    if _is_stale_pending(validation.status, validation.started_at):
        validation.status = ValidationStatus.FAILED
        validation.error_message = validation.error_message or INTERRUPTED_MESSAGE
    return validation


class ValidationStore:
    """DynamoDB-based CRUD operations for documents, validations, queries, and standards.

    Falls back to in-memory storage when DynamoDB is unavailable.
    """

    def __init__(self):
        """Initialize DynamoDB client."""
        self._client = None
        self._available = True

        # In-memory write-through cache (not primary source)
        self._documents: dict[str, Document] = {}
        self._validations: dict[str, ValidationResult] = {}
        self._standards: dict[str, dict] = {}

        # Document store for S3 operations
        self._doc_store = None

        self._init_client()

    @property
    def doc_store(self):
        """Lazy-initialize document store for S3 operations."""
        if self._doc_store is None:
            from app.services.document_store import document_store
            self._doc_store = document_store
        return self._doc_store

    def _init_client(self):
        """Initialize DynamoDB client."""
        try:
            self._client = boto3.resource(
                "dynamodb",
                region_name=settings.aws_region,
            )
            # We don't test connection here to avoid errors during import
            logger.info("DynamoDB client initialized")
        except NoCredentialsError:
            logger.warning(
                "AWS credentials not found. Using in-memory storage fallback."
            )
            self._available = False
        except Exception as e:
            logger.warning(f"Failed to initialize DynamoDB: {e}. Using in-memory storage.")
            self._available = False

    # --- Document Operations ---

    def save_document(self, document: Document) -> Document:
        """Save or update a document record."""
        # Always save to in-memory (full content available for processing)
        self._documents[document.document_id] = document

        if not self._available:
            logger.info(f"Saved document {document.document_id} (in-memory)")
            return document

        try:
            table = self._client.Table(settings.documents_table)
            # Build a slim item for DynamoDB (no full section content to avoid 400KB limit)
            item = {
                "id": document.document_id,
                "document_id": document.document_id,
                "filename": document.filename,
                "s3_key": document.s3_key,
                "status": document.status.value if hasattr(document.status, 'value') else str(document.status),
                "document_type": document.document_type.value if hasattr(document.document_type, 'value') else str(document.document_type),
                "uploaded_at": document.uploaded_at.isoformat(),
                "file_size_bytes": document.file_size_bytes or 0,
                "page_count": document.page_count,
                "component_count": len(document.components) if document.components else 0,
                "section_count": len(document.sections) if document.sections else 0,
                "sync_status": getattr(document, 'sync_status', 'pending'),
                "processing_step": getattr(document, 'processing_step', ''),
            }
            if document.content_hash:
                item["content_hash"] = document.content_hash
            if getattr(document, "uploaded_by", None):
                item["uploaded_by"] = document.uploaded_by
            if document.parsed_at:
                item["parsed_at"] = document.parsed_at.isoformat()
            if getattr(document, 'sync_completed_at', None):
                item["sync_completed_at"] = document.sync_completed_at.isoformat()
            if document.error_message:
                item["error_message"] = document.error_message
            # Tier metadata is optional — only persist when set so
            # older rows aren't bloated with null fields.
            if getattr(document, "declared_tier", None) is not None:
                item["declared_tier"] = int(document.declared_tier)
            if getattr(document, "tier_source", None):
                item["tier_source"] = document.tier_source

            # Free-form NLP context the user attached on
            # upload (if any). Persisted as a plain string. Only
            # written when non-empty so older rows stay lean.
            if getattr(document, "extraction_notes", None):
                item["extraction_notes"] = document.extraction_notes
            # Required field; always present on a Document.
            if getattr(document, "hosting_scope", None):
                item["hosting_scope"] = document.hosting_scope
            # PPTX upload support. Only persist these
            # when the upload was actually a .pptx; PDF uploads stay
            # lean (no source_format key, etc.) so those rows look
            # identical to rows written before PPTX was supported.
            sf = getattr(document, "source_format", None)
            if sf is not None:
                # Pydantic enum or raw string — handle both.
                item["source_format"] = sf.value if hasattr(sf, "value") else str(sf)
            if getattr(document, "original_s3_key", None):
                item["original_s3_key"] = document.original_s3_key
            if getattr(document, "source_content_hash", None):
                item["source_content_hash"] = document.source_content_hash
            if getattr(document, "slide_count", None) is not None:
                item["slide_count"] = int(document.slide_count)
            # slides[] is a list of SlideStatus models — JSON-encode
            # to sidestep DDB's nested-list type-coercion gymnastics.
            if getattr(document, "slides", None):
                import json as _json
                item["slides_json"] = _json.dumps(
                    [
                        s.model_dump(mode="json") if hasattr(s, "model_dump") else s
                        for s in document.slides
                    ]
                )

            table.put_item(Item=_float_to_decimal(item))
            logger.info(f"Saved document {document.document_id} to DynamoDB")
            return document
        except ClientError as e:
            logger.error(f"DynamoDB error saving document: {e}")
            return document

    def get_document(self, document_id: str) -> Optional[Document]:
        """Get a document by ID. Prefers in-memory (has full content) over DynamoDB (metadata only)."""
        # In-memory has full content (sections, components)
        if document_id in self._documents:
            return self._documents[document_id]

        if not self._available:
            return None

        try:
            table = self._client.Table(settings.documents_table)
            response = table.get_item(Key={"id": document_id})
            item = response.get("Item")
            if item:
                # DynamoDB has metadata only — reconstruct minimal Document
                doc = Document(
                    document_id=item.get("document_id", document_id),
                    filename=item.get("filename", ""),
                    s3_key=item.get("s3_key", ""),
                    content_hash=item.get("content_hash"),
                    status=item.get("status", "uploaded"),
                    document_type=item.get("document_type", "sdd"),
                    uploaded_at=item.get("uploaded_at", ""),
                    uploaded_by=item.get("uploaded_by"),
                    file_size_bytes=int(item.get("file_size_bytes", 0)),
                    page_count=item.get("page_count"),
                    declared_tier=(
                        int(item["declared_tier"])
                        if item.get("declared_tier") is not None
                        else None
                    ),
                    tier_source=item.get("tier_source"),
                    extraction_notes=item.get("extraction_notes"),
                    # A missing scope on older rows is treated as universal.
                    hosting_scope=item.get("hosting_scope", "both"),
                )
                # Hydrate PPTX-source fields. Defaults
                # match a native-PDF row exactly (source_format=PDF,
                # other fields None) so the rest of the app can stay
                # source-agnostic. Wrapped per-field so a missing
                # column doesn't void the whole document load.
                if item.get("source_format"):
                    doc.source_format = item["source_format"]
                if item.get("original_s3_key"):
                    doc.original_s3_key = item["original_s3_key"]
                if item.get("source_content_hash"):
                    doc.source_content_hash = item["source_content_hash"]
                if item.get("slide_count") is not None:
                    try:
                        doc.slide_count = int(item["slide_count"])
                    except (TypeError, ValueError):
                        pass
                slides_json = item.get("slides_json")
                if slides_json:
                    import json as _json
                    from app.models.document import SlideStatus
                    try:
                        doc.slides = [
                            SlideStatus(**rec) for rec in _json.loads(slides_json)
                        ]
                    except Exception as e:
                        logger.warning(
                            f"Failed to hydrate slides[] for {document_id}: {e}"
                        )

                # Try to load parsed content from S3
                if self._doc_store is None:
                    from app.services.document_store import document_store
                    self._doc_store = document_store

                parsed_content = self._doc_store.load_parsed_content(document_id)
                if parsed_content:
                    from app.models.document import ParsedSection, ExtractedComponent
                    try:
                        doc.sections = [ParsedSection(**s) for s in parsed_content.get("sections", [])]
                        doc.components = [ExtractedComponent(**c) for c in parsed_content.get("components", [])]
                        logger.info(f"Loaded {len(doc.sections)} sections and {len(doc.components)} components from S3 for {document_id}")
                    except Exception as e:
                        logger.warning(f"Failed to reconstruct sections/components from S3: {e}")

                return doc
            return None
        except ClientError as e:
            logger.error(f"DynamoDB error getting document: {e}")
            return None

    def get_document_by_hash(
        self,
        content_hash: str,
        document_type: Optional[str] = None,
        extraction_notes_hash: Optional[str] = None,
    ) -> Optional[Document]:
        """Find an existing document by content + optional extraction
        notes hash.

        Used to deduplicate uploads — re-uploading identical bytes returns
        the existing document instead of creating a duplicate row.

        Dedup also keys on a sha256 of the user's free-form
        extraction_notes when provided, so re-uploading the same PDF
        with different notes creates a new Document row (and a fresh
        extraction run) rather than short-circuiting to the cached
        record. A None kwarg means "don't filter on this dimension"
        (preserves the older behaviour for callers that pass nothing).
        """
        def _dt_value(d: Document) -> str:
            return d.document_type.value if hasattr(d.document_type, "value") else str(d.document_type)

        def _notes_hash(d: Document) -> Optional[str]:
            notes = getattr(d, "extraction_notes", None)
            if not notes:
                return None
            import hashlib as _h
            return _h.sha256(notes.strip().encode("utf-8")).hexdigest()

        # Check in-memory cache first
        for doc in self._documents.values():
            if doc.content_hash != content_hash:
                continue
            if document_type is not None and _dt_value(doc) != document_type:
                continue
            if extraction_notes_hash is not None and _notes_hash(doc) != extraction_notes_hash:
                continue
            return doc

        if not self._available:
            return None

        try:
            from boto3.dynamodb.conditions import Attr
            table = self._client.Table(settings.documents_table)
            filter_expr = Attr("content_hash").eq(content_hash)
            if document_type:
                filter_expr = filter_expr & Attr("document_type").eq(document_type)
            if extraction_notes_hash is not None:
                # extraction_notes is stored as a plain string on the item;
                # we re-derive the hash on items that match content_hash
                # rather than trying to filter on it server-side (DDB scan).
                pass  # filtered in the in-memory loop above OR after fetch below
            # Paginated: a FilterExpression is applied AFTER each 1MB page, so
            # an unpaginated scan can miss a matching row that sits on page 2 —
            # which would defeat upload dedup.
            items = scan_all(table, FilterExpression=filter_expr)
            if not items:
                return None
            # Filter by extraction_notes hash here in Python — DDB scan
            # can't compare a sha256 of a stored attribute, so we
            # rederive the hash on each candidate and match.
            if extraction_notes_hash is not None:
                import hashlib as _h
                def _item_notes_hash(it: dict) -> Optional[str]:
                    notes = it.get("extraction_notes")
                    if not notes:
                        return None
                    return _h.sha256(str(notes).strip().encode("utf-8")).hexdigest()
                items = [it for it in items if _item_notes_hash(it) == extraction_notes_hash]
                if not items:
                    return None
            # Return the oldest match (first uploaded wins)
            items.sort(key=lambda x: x.get("uploaded_at", ""))
            item = items[0]
            return self.get_document(item["id"])
        except ClientError as e:
            logger.error(f"DynamoDB error scanning by content_hash: {e}")
            return None

    def list_documents(self, document_type: Optional[str] = None) -> list[Document]:
        """List all documents. DynamoDB is authoritative source, in-memory is cache.

        Args:
            document_type: Optional filter by document type ('sdd' or 'standards')
        """
        if not self._available:
            # Fallback to in-memory if DynamoDB unavailable
            docs = list(self._documents.values())
            if document_type:
                docs = [d for d in docs if d.document_type.value == document_type or str(d.document_type) == document_type]
            return docs

        try:
            # Always query DynamoDB as authoritative source
            table = self._client.Table(settings.documents_table)
            if document_type:
                items = scan_all(
                    table,
                    FilterExpression="document_type = :dtype",
                    ExpressionAttributeValues={":dtype": document_type},
                )
            else:
                items = scan_all(table)
            documents = []
            for item in items:
                try:
                    doc = Document(
                        document_id=item.get("document_id", item.get("id", "")),
                        filename=item.get("filename", ""),
                        s3_key=item.get("s3_key", ""),
                        status=item.get("status", "uploaded"),
                        document_type=item.get("document_type", "sdd"),
                        uploaded_at=item.get("uploaded_at", ""),
                        file_size_bytes=int(item.get("file_size_bytes", 0)),
                        page_count=item.get("page_count"),
                        # A missing scope on older rows is treated as
                        # universal. Without this kwarg, Pydantic 422s
                        # the ctor and the row was silently dropped from
                        # list views, making scoped docs invisible on
                        # /documents.
                        hosting_scope=item.get("hosting_scope", "both"),
                        declared_tier=(
                            int(item["declared_tier"])
                            if item.get("declared_tier") is not None
                            else None
                        ),
                        tier_source=item.get("tier_source"),
                        extraction_notes=item.get("extraction_notes"),
                    )
                    documents.append(doc)
                except Exception as e:
                    # Surface so a future regression here doesn't
                    # silently kill the list — a swallowed exception
                    # here has cost real debugging time before.
                    logger.warning(
                        f"Skipped document {item.get('id', '?')} during list: {e}"
                    )
                    continue
            return documents
        except ClientError as e:
            logger.error(f"DynamoDB error listing documents: {e}")
            # Fallback to in-memory cache on error
            docs = list(self._documents.values())
            if document_type:
                docs = [d for d in docs if d.document_type.value == document_type or str(d.document_type) == document_type]
            return docs

    def update_document_status(
        self, document_id: str, status: DocumentStatus, error_message: Optional[str] = None
    ) -> Optional[Document]:
        """Update document status."""
        doc = self.get_document(document_id)
        if not doc:
            return None

        doc.status = status
        if error_message:
            doc.error_message = error_message
        if status == DocumentStatus.PARSED:
            doc.parsed_at = datetime.utcnow()

        return self.save_document(doc)

    # --- Validation Operations ---

    def _stored_owner(self, validation_id: str) -> Optional[str]:
        """``triggered_by`` from the DDB row, or None. Projected read: this runs
        on the write path, so it must not pull findings or the whole item."""
        try:
            table = self._client.Table(settings.validations_table)
            response = table.get_item(
                Key={"id": validation_id},
                ProjectionExpression="triggered_by",
            )
            return (response.get("Item") or {}).get("triggered_by")
        except ClientError:
            logger.warning(
                "Could not read prior owner for %s; leaving it unset", validation_id
            )
            return None

    def save_validation(self, validation: ValidationResult) -> ValidationResult:
        """Save or update a validation result. Stores findings in S3, metadata in DynamoDB."""
        # Ownership is write-once. The background validator rewrites this row
        # several times (progress snapshots, then the terminal result) from
        # freshly-built ValidationResult objects, and threading the owner
        # through each construction site would silently drop it the first time
        # someone adds another. Preserving it here makes that impossible: an
        # owner can never be cleared or reassigned by a later save.
        if not validation.triggered_by:
            prior = self._validations.get(validation.validation_id)
            if prior is not None and prior.triggered_by:
                validation.triggered_by = prior.triggered_by
            elif self._available:
                validation.triggered_by = self._stored_owner(validation.validation_id)

        # Stamp the issue counts onto the result itself so list views (which
        # never load findings) and the in-memory fallback both report them.
        counts = finding_counts(validation.findings)
        validation.findings_count = counts["findings_count"]
        validation.critical_count = counts["critical_count"]
        validation.high_count = counts["high_count"]

        # Keep in-memory as hot cache
        self._validations[validation.validation_id] = validation

        if not self._available:
            logger.info(f"Saved validation {validation.validation_id} (in-memory)")
            return validation

        try:
            # Save findings to S3 if present
            if validation.findings:
                self._save_findings_to_s3(validation.validation_id, validation.findings)

            # Store slim metadata in DynamoDB
            table = self._client.Table(settings.validations_table)
            item = {
                "id": validation.validation_id,
                "validation_id": validation.validation_id,
                "document_id": validation.document_id,
                "status": validation.status.value if hasattr(validation.status, 'value') else str(validation.status),
                "standards_set": validation.standards_set,
                "started_at": validation.started_at.isoformat() if validation.started_at else None,
                "completed_at": validation.completed_at.isoformat() if validation.completed_at else None,
                "overall_score": validation.overall_score,
                "triggered_by": validation.triggered_by,
                **counts,
                "summary": validation.summary[:500] if validation.summary else None,
                "error_message": validation.error_message,
                "document_content_hash": validation.document_content_hash,
                "standards_set_hash": validation.standards_set_hash,
                "evaluated_count": getattr(validation, "evaluated_count", 0),
                "total_standards": getattr(validation, "total_standards", 0),
                "chunks_per_document": getattr(validation, "chunks_per_document", None),
                "bedrock_calls": getattr(validation, "bedrock_calls", None),
                "bedrock_input_tokens": getattr(validation, "bedrock_input_tokens", None),
                "bedrock_output_tokens": getattr(validation, "bedrock_output_tokens", None),
                "estimated_cost_usd": getattr(validation, "estimated_cost_usd", None),
                "skipped_count": getattr(validation, "skipped_count", None),
                "skipped_count_breakdown": getattr(validation, "skipped_count_breakdown", None),
            }

            # Decision-tree rollup + tier provenance.
            # layer_verdicts is a list of LayerVerdictRollup models;
            # JSON-encode so DDB doesn't have to deal with nested
            # struct serialization. arch_verdict is an enum -> string.
            lvs = getattr(validation, "layer_verdicts", None)
            if lvs:
                import json as _json
                item["layer_verdicts_json"] = _json.dumps([
                    lv.model_dump(mode="json") if hasattr(lv, "model_dump") else lv
                    for lv in lvs
                ])
            av = getattr(validation, "architecture_verdict", None)
            if av is not None:
                item["architecture_verdict"] = av.value if hasattr(av, "value") else str(av)
            tu = getattr(validation, "tier_used", None)
            if tu is not None:
                item["tier_used"] = int(tu)
            to = getattr(validation, "tier_override", None)
            if to is not None:
                item["tier_override"] = int(to)

            # Remove None values
            item = {k: v for k, v in item.items() if v is not None}
            table.put_item(Item=_float_to_decimal(item))
            logger.info(f"Saved validation {validation.validation_id} to DynamoDB (findings in S3)")
            return validation
        except ClientError as e:
            logger.error(f"DynamoDB error saving validation: {e}")
            return validation

    def find_cached_validation(
        self,
        document_id: str,
        document_content_hash: str,
        standards_set_hash: str,
    ) -> Optional[ValidationResult]:
        """Return the most recent completed/pending validation that matches
        both hashes for this document, or None.

        A pending match is also returned so a duplicate request that arrives
        while a validation is still running short-circuits to the in-flight
        validation_id rather than dispatching a second run.
        """
        if not self._available:
            for v in self._validations.values():
                if (
                    v.document_id == document_id
                    and v.document_content_hash == document_content_hash
                    and v.standards_set_hash == standards_set_hash
                    and (v.status.value if hasattr(v.status, "value") else str(v.status))
                    in ("completed", "pending")
                    and not _is_stale_pending(v.status, v.started_at)
                ):
                    return v
            return None

        try:
            from boto3.dynamodb.conditions import Attr
            table = self._client.Table(settings.validations_table)
            # Paginated: filters run after each page, so a single-page scan
            # could report "no cached run" purely because the match was on a
            # later page — causing duplicate Bedrock spend and breaking the
            # in-flight dedupe that stops two identical runs racing.
            items = scan_all(
                table,
                FilterExpression=(
                    Attr("document_id").eq(document_id)
                    & Attr("document_content_hash").eq(document_content_hash)
                    & Attr("standards_set_hash").eq(standards_set_hash)
                    & Attr("status").is_in(["completed", "pending"])
                ),
            )
            # A dead PENDING run must not be handed back as "in flight", or the
            # document can never be re-validated.
            items = [
                x for x in items
                if not _is_stale_pending(x.get("status"), x.get("started_at"))
            ]
            if not items:
                return None
            # Prefer completed over pending; within each, take most recent.
            items.sort(
                key=lambda x: (
                    x.get("status") != "completed",  # completed first
                    -(self._iso_to_epoch(x.get("completed_at") or x.get("started_at"))),
                )
            )
            return self.get_validation(items[0]["id"])
        except ClientError as e:
            logger.error(f"DynamoDB error scanning validation cache: {e}")
            return None

    @staticmethod
    def _iso_to_epoch(value: Optional[str]) -> float:
        if not value:
            return 0.0
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return 0.0

    # --- extraction in-flight state -----------------------------------
    #
    # Mirrors the cancellation signal below, for the same reason: the extraction
    # live log lives in one task's memory, so another task cannot tell whether a
    # run is in progress. The review gate needs that answer — otherwise it fails
    # open and a curator can approve proposed rules while dedup and
    # cross-reference wiring are still running over the same batch.

    def set_extraction_state(self, document_id: str, state: Optional[str]) -> None:
        """Stamp (or clear) the in-flight extraction state on a document row."""
        if not document_id:
            return
        doc = self._documents.get(document_id)
        if doc is not None:
            setattr(doc, "_extraction_state", state)
        if not self._available:
            return
        try:
            table = self._client.Table(settings.documents_table)
            if state:
                # Timestamped so a task that dies mid-run can't leave the gate
                # shut forever — see get_extraction_state's staleness window.
                table.update_item(
                    Key={"id": document_id},
                    UpdateExpression=(
                        "SET extraction_state = :s, extraction_state_at = :t"
                    ),
                    ExpressionAttributeValues={
                        ":s": state,
                        ":t": datetime.utcnow().isoformat(),
                    },
                )
            else:
                table.update_item(
                    Key={"id": document_id},
                    UpdateExpression="REMOVE extraction_state, extraction_state_at",
                )
        except ClientError as e:
            logger.warning(
                f"DynamoDB error stamping extraction state for {document_id}: {e}"
            )

    def get_extraction_state(self, document_id: str) -> Optional[str]:
        """Read the in-flight extraction state, or None when no run is active.

        Returns None on a read failure. That is deliberate: the gate this feeds
        should not block a curator indefinitely because DynamoDB hiccuped — the
        in-flight window is short, and the same-task check catches the common
        case.
        """
        if not document_id:
            return None
        if not self._available:
            doc = self._documents.get(document_id)
            return getattr(doc, "_extraction_state", None) if doc else None
        try:
            table = self._client.Table(settings.documents_table)
            resp = table.get_item(
                Key={"id": document_id},
                ProjectionExpression="extraction_state, extraction_state_at",
            )
            item = resp.get("Item") or {}
            value = item.get("extraction_state")
            if not value:
                return None
            # A task killed by a deploy or scale-in never clears its stamp. Treat
            # an old stamp as "not running" so the review gate reopens instead of
            # locking those rules permanently.
            stamped_at = self._iso_to_epoch(item.get("extraction_state_at"))
            if stamped_at and (
                datetime.utcnow().timestamp() - stamped_at
            ) > EXTRACTION_STATE_STALE_AFTER_SEC:
                logger.warning(
                    "extraction_state on %s is stale (%s); treating the run as "
                    "no longer in flight", document_id, value,
                )
                return None
            return str(value)
        except ClientError as e:
            logger.warning(
                f"DynamoDB error reading extraction state for {document_id}: {e}"
            )
            return None

    # --- cancellation signal ------------------------------------------
    #
    # Cancellation has to survive crossing tasks. The API autoscales to several
    # ECS tasks, so POST /{id}/cancel frequently lands on a task that is NOT
    # running the job; a process-local flag there is set where no loop reads it,
    # and the run bills the full Bedrock fan-out while the UI claims it stopped.
    # These two methods put the signal on the validation row so any task can see
    # it. Deliberately narrow (one attribute) rather than reusing
    # save_validation, which would rewrite the whole result mid-run.

    def request_cancel(self, validation_id: str) -> bool:
        """Persist a cancel request. Returns False when the write failed, so the
        caller can avoid telling the user a cancel landed when it didn't."""
        if not validation_id:
            return False
        if not self._available:
            row = self._validations.get(validation_id)
            if row is not None:
                setattr(row, "_cancel_requested", True)
            return True
        try:
            table = self._client.Table(settings.validations_table)
            table.update_item(
                Key={"id": validation_id},
                UpdateExpression="SET cancel_requested = :t",
                ExpressionAttributeValues={":t": True},
            )
            return True
        except ClientError as e:
            logger.error(f"DynamoDB error requesting cancel for {validation_id}: {e}")
            return False

    def is_cancel_requested(self, validation_id: str) -> bool:
        """Read the persisted cancel flag. Fails CLOSED (False) on error: a
        transient read failure must not abort a run that nobody cancelled."""
        if not validation_id:
            return False
        if not self._available:
            row = self._validations.get(validation_id)
            return bool(getattr(row, "_cancel_requested", False)) if row else False
        try:
            table = self._client.Table(settings.validations_table)
            resp = table.get_item(
                Key={"id": validation_id},
                ProjectionExpression="cancel_requested",
            )
            return bool((resp.get("Item") or {}).get("cancel_requested", False))
        except ClientError as e:
            logger.warning(f"DynamoDB error reading cancel flag for {validation_id}: {e}")
            return False

    def get_validation(self, validation_id: str) -> Optional[ValidationResult]:
        """Get a validation result by ID. Checks cache first, then DynamoDB+S3."""
        # Check hot cache first — but skip PENDING/RUNNING. The progress
        # writer in the parallel validator background task writes new status
        # to DynamoDB, not back to this process's in-memory cache; a stale
        # PENDING entry would shadow the live progress.
        if validation_id in self._validations:
            cached = self._validations[validation_id]
            cached_status = cached.status.value if hasattr(cached.status, "value") else str(cached.status)
            if cached_status not in ("pending", "running"):
                return cached

        if not self._available:
            return None

        try:
            # Query DynamoDB for metadata
            table = self._client.Table(settings.validations_table)
            response = table.get_item(Key={"id": validation_id})
            item = response.get("Item")
            if item:
                # Load findings from S3
                findings = self._load_findings_from_s3(validation_id)

                # Reconstruct full ValidationResult
                validation = ValidationResult(
                    validation_id=item.get("validation_id", validation_id),
                    document_id=item.get("document_id", ""),
                    status=item.get("status", "completed"),
                    standards_set=item.get("standards_set", ""),
                    started_at=item.get("started_at"),
                    completed_at=item.get("completed_at"),
                    overall_score=float(item["overall_score"]) if item.get("overall_score") is not None else None,
                    summary=item.get("summary"),
                    error_message=item.get("error_message"),
                    findings=findings,
                    document_content_hash=item.get("document_content_hash"),
                    standards_set_hash=item.get("standards_set_hash"),
                    triggered_by=item.get("triggered_by"),
                    evaluated_count=int(item.get("evaluated_count", 0) or 0),
                    total_standards=int(item.get("total_standards", 0) or 0),
                    # Recomputed rather than read off the row: this path has the
                    # findings in hand, so it stays correct for rows written
                    # before the counts were stored.
                    **finding_counts(findings),
                    **_telemetry_kwargs(item),
                )
                # Update cache
                self._validations[validation_id] = validation
                return _surface_interrupted(validation)
            return None
        except ClientError as e:
            logger.error(f"DynamoDB error getting validation: {e}")
            return None

    def list_validations(self, document_id: Optional[str] = None) -> list[ValidationResult]:
        """List validations. DynamoDB is authoritative source."""
        if not self._available:
            # Fallback to in-memory cache if DynamoDB unavailable
            validations = list(self._validations.values())
            if document_id:
                validations = [v for v in validations if v.document_id == document_id]
            return validations

        try:
            # Always query DynamoDB as authoritative source
            table = self._client.Table(settings.validations_table)
            if document_id:
                items = scan_all(
                    table,
                    FilterExpression="document_id = :doc_id",
                    ExpressionAttributeValues={":doc_id": document_id},
                )
            else:
                items = scan_all(table)

            results = []
            for item in items:
                try:
                    # For list operations, return metadata only (no findings)
                    # Findings loaded on-demand via get_validation()
                    results.append(_surface_interrupted(ValidationResult(
                        validation_id=item.get("validation_id", item.get("id", "")),
                        document_id=item.get("document_id", ""),
                        status=item.get("status", "completed"),
                        standards_set=item.get("standards_set", ""),
                        started_at=item.get("started_at"),
                        completed_at=item.get("completed_at"),
                        overall_score=float(item["overall_score"]) if item.get("overall_score") is not None else None,
                        summary=item.get("summary"),
                        error_message=item.get("error_message"),
                        triggered_by=item.get("triggered_by"),
                        evaluated_count=int(item.get("evaluated_count", 0) or 0),
                        total_standards=int(item.get("total_standards", 0) or 0),
                        # Read off the row: `findings` is empty here by design.
                        **_count_kwargs(item),
                        **_telemetry_kwargs(item),
                    )))
                except Exception:
                    continue
            return results
        except ClientError as e:
            logger.error(f"DynamoDB error listing validations: {e}")
            # Fallback to in-memory cache on error
            validations = list(self._validations.values())
            if document_id:
                validations = [v for v in validations if v.document_id == document_id]
            return validations

    # --- Standards Operations ---

    def list_standards(
        self,
        *,
        status: Optional[str] = "active",
        include_proposed: bool = False,
    ) -> list[dict]:
        """List standards, filtered by lifecycle status.

        Defaults to ``status="active"`` so callers (validation, curator,
        cache hash) never pick up proposed or archived rules. Pass
        ``include_proposed=True`` to get every status (used by the
        proposals UI), or ``status=None`` to disable filtering entirely.

        Legacy rows without an explicit ``status`` field are treated as
        ACTIVE — the active-default is checked as
        ``(row.status or "active") == "active"`` so a missing column
        never silently archives an existing rule.
        """
        def _matches(item: dict) -> bool:
            if include_proposed or status is None:
                return True
            row_status = item.get("status") or "active"
            return row_status == status

        if not self._available:
            return [s for s in self._standards.values() if _matches(s)]

        try:
            table = self._client.Table(settings.standards_table)
            # MUST paginate: the validator reads this list, so a truncated scan
            # means rules silently stop being enforced and a document is scored
            # compliant against a rulebook that was cut short.
            items = scan_all(table)
            return [s for s in items if _matches(s)]
        except ClientError as e:
            logger.error(f"DynamoDB error listing standards: {e}")
            return [s for s in self._standards.values() if _matches(s)]

    def get_standard(self, standard_id: str) -> Optional[dict]:
        """Get a standard by ID."""
        if not self._available:
            return self._standards.get(standard_id)

        try:
            table = self._client.Table(settings.standards_table)
            response = table.get_item(Key={"id": standard_id})
            return response.get("Item")
        except ClientError as e:
            logger.error(f"DynamoDB error getting standard: {e}")
            return self._standards.get(standard_id)

    def create_standard(self, standard: dict) -> dict:
        """Create a new standard.

        The structured-rule fields (rule_type / layer / parent_rule_id)
        round-trip transparently via this pass-through write. The OMIT WHEN EMPTY
        contract is enforced upstream (route handler + agent tool), so
        this method never sees empty-string sentinels for the new
        fields — they are either present with a valid literal value or
        absent from the dict entirely.
        """
        if not self._available:
            self._standards[standard["id"]] = standard
            logger.info(f"Saved standard {standard['id']} (in-memory)")
            return standard

        try:
            table = self._client.Table(settings.standards_table)
            table.put_item(Item=standard)
            logger.info(f"Saved standard {standard['id']} to DynamoDB")
            return standard
        except ClientError as e:
            logger.error(f"DynamoDB error creating standard: {e}")
            self._standards[standard["id"]] = standard
            return standard

    def update_standard(self, standard_id: str, updates: dict) -> Optional[dict]:
        """Update an existing standard with standard PUT semantics (replace)."""
        existing = self.get_standard(standard_id)
        if not existing:
            return None

        existing.update(updates)

        if not self._available:
            self._standards[standard_id] = existing
            return existing

        try:
            table = self._client.Table(settings.standards_table)
            table.put_item(Item=existing)
            logger.info(f"Updated standard {standard_id} in DynamoDB")
            return existing
        except ClientError as e:
            logger.error(f"DynamoDB error updating standard: {e}")
            self._standards[standard_id] = existing
            return existing

    def delete_standard(self, standard_id: str) -> bool:
        """Delete a standard by ID."""
        if not self._available:
            if standard_id in self._standards:
                del self._standards[standard_id]
                return True
            return False

        try:
            table = self._client.Table(settings.standards_table)
            table.delete_item(Key={"id": standard_id})
            logger.info(f"Deleted standard {standard_id} from DynamoDB")
            return True
        except ClientError as e:
            logger.error(f"DynamoDB error deleting standard: {e}")
            return False

    # Fields that participate in the standards-set hash. Excludes timestamps
    # and source provenance so metadata churn doesn't invalidate caches.
    # applies_to_tiers IS in the hash because changing a rule's tier
    # scope changes which validations should rerun. alternative_criteria
    # also matters because the validator embeds it in the prompt — a
    # change there changes verdicts.
    # hosting_scope is in for the same reason as applies_to_tiers: the
    # validator selects rules on it (a rule applies when its scope is the
    # document's scope or "both" — see parallel_validator's scope filter), so
    # retargeting a rule from on_prem to cloud changes which rules ran. Without
    # it, that edit left the key unchanged and the next run served the previous
    # verdict, computed against a different rule set.
    _STANDARDS_HASH_FIELDS = (
        "id",
        "name",
        "description",
        "severity",
        "category",
        "validation_criteria",
        "applies_to_tiers",
        "alternative_criteria",
        "hosting_scope",
    )

    def standards_set_hash(
        self,
        selected_ids: Optional[list[str]] = None,
        transient_standards: Optional[list[dict]] = None,
    ) -> str:
        """SHA256 over a canonical JSON projection of standards.

        Used as part of the validation cache key — when this hash changes
        (any standard added, removed, edited, selection changed, or
        transient rules included), prior cached validations are no
        longer valid.

        When selected_ids is provided, only those library standards
        contribute. When transient_standards is provided, those rules
        are folded into the same projection. Both lists merge into the
        canonical representation, sorted by id, so order doesn't matter.
        """
        standards = self.list_standards()
        if selected_ids is not None:
            wanted = set(selected_ids)
            standards = [s for s in standards if s.get("id") in wanted]
        merged = list(standards) + list(transient_standards or [])

        def _project(s: dict) -> dict:
            out: dict = {}
            for k in self._STANDARDS_HASH_FIELDS:
                if k == "applies_to_tiers":
                    # Legacy rows lack this field — default to all
                    # five tiers so the hash matches their actual
                    # behavior. Always sort for determinism.
                    tiers = s.get(k) or [1, 2, 3, 4, 5]
                    try:
                        out[k] = sorted(int(t) for t in tiers)
                    except (TypeError, ValueError):
                        out[k] = [1, 2, 3, 4, 5]
                else:
                    out[k] = s.get(k, "")
            return out

        projected = [_project(s) for s in merged]
        # Sort by id so write order / source order doesn't affect the hash.
        projected.sort(key=lambda x: x["id"])
        # Fold in a validator-algorithm version. When the validator
        # changes how it processes documents (e.g. switches from
        # truncated single-call to chunked fan-out), bump this so old
        # cached results are not served as if they answered the same
        # question as a fresh run.
        # v4: tier-aware + alternative-criteria-aware prompt. Bumped
        # because the prompt template changed (alternatives now folded
        # into a single rule rather than spawning duplicates), and
        # because a v3 hash without applies_to_tiers would collide.
        # v5: the validator's rule-selection code path gains
        # a hosting_scope filter composed with the existing tier
        # filter. Pre-v5 cached verdicts represent "no scope filter"
        # selection and would be inconsistent with v5 reads — bump
        # the version so any stale v4 verdicts cache-miss cleanly.
        # v6: hosting_scope joins _STANDARDS_HASH_FIELDS. v5 added the scope
        # FILTER but left the rule's own scope out of the key, so retargeting a
        # rule between on_prem and cloud did not invalidate the cache and the
        # next run replayed a verdict computed from a different rule set. Bumped
        # because every v5 hash was computed without that field.
        envelope = {"validator_version": "6", "standards": projected}
        canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # --- S3 Findings Storage ---

    def _save_findings_to_s3(self, validation_id: str, findings: list) -> bool:
        """Save validation findings to S3."""
        try:
            # Check if S3 client is available
            if self.doc_store.client is None:
                logger.debug(f"S3 not available, skipping findings save for {validation_id}")
                return False

            key = f"validations/{validation_id}/findings.json"
            content = json.dumps([
                f.model_dump(mode="json") if hasattr(f, 'model_dump') else f
                for f in findings
            ])
            self.doc_store.client.put_object(
                Bucket=settings.s3_documents_bucket,
                Key=key,
                Body=content.encode('utf-8'),
                ContentType='application/json'
            )
            logger.info(f"Saved {len(findings)} findings to S3 for validation {validation_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to save findings to S3: {e}")
            return False

    def _load_findings_from_s3(self, validation_id: str) -> list:
        """Load validation findings from S3."""
        try:
            # Check if S3 client is available
            if self.doc_store.client is None:
                logger.debug(f"S3 not available, cannot load findings for {validation_id}")
                return []

            key = f"validations/{validation_id}/findings.json"
            response = self.doc_store.client.get_object(
                Bucket=settings.s3_documents_bucket,
                Key=key
            )
            findings = json.loads(response['Body'].read().decode('utf-8'))
            logger.info(f"Loaded {len(findings)} findings from S3 for validation {validation_id}")
            return findings
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchKey":
                logger.debug(f"No findings found in S3 for validation {validation_id}")
            else:
                logger.error(f"Failed to load findings from S3: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to load findings from S3: {e}")
            return []


# Singleton instance
validation_store = ValidationStore()
