# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Validation data models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """Finding severity levels."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ComplianceStatus(str, Enum):
    """Compliance status for a finding.

    Two decision-tree-aware verdict values:

      pass_via_substitution: founding rule failed but a substitution
        rescued it. The finding's via_rule_id points at the
        substitution that passed.
      not_applicable_via_exception: a rule_exception or layer_exception
        marked this finding's rule N/A for this SDD. via_rule_id
        points at the exception that fired.

    The plain PASS / FAIL / WARNING / NOT_APPLICABLE values stay
    valid; findings that only use those render unchanged.

    not_addressed: the document does not discuss this rule at all. This
    is DISTINCT from FAIL (which means the document addresses the rule
    and violates it). Collapsing "not addressed" into FAIL made sparse
    coverage read as total non-compliance and drove scores to 0; scoring
    excludes not_addressed from the denominator so an unmentioned rule
    neither passes nor fails the document."""
    PASS = "pass"  # nosec B105 - verdict label, not a credential
    FAIL = "fail"
    WARNING = "warning"
    NOT_APPLICABLE = "not_applicable"
    # Decision-tree verdicts.
    PASS_VIA_SUBSTITUTION = "pass_via_substitution"  # nosec B105 - verdict label
    NOT_APPLICABLE_VIA_EXCEPTION = "not_applicable_via_exception"
    # The document is silent on this rule. Not a failure; excluded from
    # the score denominator.
    NOT_ADDRESSED = "not_addressed"


# Per-(tier × layer) rollup verdicts. Architecture-level
# COMPLIANT requires every layer verdict to be COMPLIANT* or
# NOT_APPLICABLE*. Distinct enum from finding-level
# ComplianceStatus because layer rollups don't have warnings or
# raw passes — they're either compliant via founding, compliant via
# substitution, N/A, or not compliant.
class LayerVerdict(str, Enum):
    COMPLIANT = "compliant"
    COMPLIANT_VIA_SUBSTITUTION = "compliant_via_substitution"
    NOT_APPLICABLE_VIA_EXCEPTION = "not_applicable_via_exception"
    NOT_COMPLIANT = "not_compliant"


class ValidationStatus(str, Enum):
    """Validation process status."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    # User requested cancellation. The validator checks a cancel flag
    # between rule batches and stops with this status. Terminal
    # (polling stops), like completed/failed.
    CANCELLED = "cancelled"


class Finding(BaseModel):
    """A single validation finding."""
    finding_id: str = Field(description="Unique finding identifier")
    requirement_id: str = Field(description="HA requirement identifier")
    requirement_title: str = Field(description="Short title of the requirement")
    severity: Severity = Field(description="Finding severity")
    status: ComplianceStatus = Field(description="Compliance status")
    evidence: str = Field(description="Evidence from the document supporting this finding")
    recommendation: str = Field(default="", description="Recommendation if non-compliant")
    affected_components: list[str] = Field(default_factory=list, description="Components affected")
    source_section: Optional[str] = Field(default=None, description="Section where evidence was found")
    # Populated when chunked validation found supporting evidence in
    # multiple chunks. Each entry is a human-readable page reference
    # like "p.3" or "pp.7-12". Empty for single-chunk runs.
    also_seen_pages: list[str] = Field(
        default_factory=list,
        description="Additional page references where this finding's evidence appeared",
    )

    # Decision-tree metadata.
    # rule_type echoes the founding/substitution/exception nature of
    # the rule that produced this finding so the UI can group + badge
    # without joining back to the standards table.
    rule_type: Optional[str] = Field(
        default=None,
        description="Rule type the finding came from: founding | substitution | exception",
    )
    # When status=PASS_VIA_SUBSTITUTION, this is the substitution
    # rule's id. When status=NOT_APPLICABLE_VIA_EXCEPTION, this is
    # the exception rule's id.
    via_rule_id: Optional[str] = Field(
        default=None,
        description="The substitution that rescued, or the exception that overrode, this finding",
    )
    # On FAIL, the substitutions the validator tried before giving up.
    # Empty when no substitutions were configured for the founding rule.
    tried_substitution_ids: list[str] = Field(
        default_factory=list,
        description="Substitution rule ids that were attempted when status=FAIL",
    )

    # Per-finding "show your work" trace. Captured from the winning
    # Bedrock call (the chunk whose verdict drove the merged finding's
    # status). None on findings written before this field existed and
    # on findings produced by paths that don't capture trace yet
    # (e.g. PPTX validator).
    trace: Optional[dict] = Field(
        default=None,
        description=(
            "Per-finding trace: {prompt, chunk_excerpt, raw_response, "
            "input_tokens, output_tokens, latency_ms}"
        ),
    )


class DiagramFinding(BaseModel):
    """A finding from diagram-based visual analysis."""
    diagram_index: int = Field(description="Index of the diagram analyzed")
    image_description: str = Field(description="Description of what the diagram shows")
    components_found: list[str] = Field(default_factory=list, description="Infrastructure components identified")
    ha_patterns_detected: list[str] = Field(default_factory=list, description="HA patterns visible in diagram")
    issues_found: list[str] = Field(default_factory=list, description="Issues or gaps identified")
    redundancy_status: str = Field(default="unknown", description="Overall redundancy assessment")
    spof_risks: list[str] = Field(default_factory=list, description="Single points of failure identified")
    network_redundancy: str = Field(default="unknown", description="Network redundancy assessment")
    confidence_score: float = Field(default=0.0, description="Confidence in analysis 0.0-1.0")


class ValidationRequest(BaseModel):
    """Request to validate a document."""
    document_id: str = Field(description="Document to validate")
    standards_set: str = Field(
        default="resilience-review-v1",
        description="Which standards set to validate against",
    )
    force: bool = Field(
        default=False,
        description="Bypass the result cache and force a fresh validation run",
    )
    standard_ids: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional subset of standard IDs to validate against. "
            "If omitted, all active standards are used."
        ),
    )
    transient_standards: Optional[list[dict]] = Field(
        default=None,
        description=(
            "Optional inline standards used for THIS validation only. "
            "Not saved to the library. Each item should carry name, "
            "description, severity, category, and validation_criteria."
        ),
    )
    # Pre-validation tier override. When set, the
    # parallel validator filters rules by this tier instead of the
    # SDD's declared_tier. Useful for 'what would this look like as
    # T2?' analyses without editing the document. None preserves
    # the existing declared-tier behavior.
    tier_override: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        description="Tier to validate against (1..5). When omitted, uses the document's declared_tier.",
    )


class LayerVerdictRollup(BaseModel):
    """Per-(tier × layer) compliance rollup.

    A validation produces one of these per (tier, layer) pair the
    validator considered. Architecture-level COMPLIANT requires every
    rollup to be COMPLIANT, COMPLIANT_VIA_SUBSTITUTION, or
    NOT_APPLICABLE_VIA_EXCEPTION.
    """

    tier: int = Field(description="Service tier (1..5)")
    layer: str = Field(description="Architecture layer the rollup belongs to")
    verdict: LayerVerdict = Field(description="Layer-level verdict")
    # When verdict=NOT_APPLICABLE_VIA_EXCEPTION, this is the layer-
    # exception rule id that fired. Otherwise None.
    via_rule_id: Optional[str] = Field(
        default=None,
        description="Layer-exception rule id when verdict=NOT_APPLICABLE_VIA_EXCEPTION",
    )
    # The founding-rule findings that fed this rollup. The UI can
    # join back to validation.findings on these ids to render the
    # 'show suppressed checks' disclosure.
    founding_finding_ids: list[str] = Field(
        default_factory=list,
        description="Finding ids of founding rules that contributed to this rollup",
    )


class ValidationResult(BaseModel):
    """Complete validation result."""
    validation_id: str = Field(description="Unique validation identifier")
    document_id: str = Field(description="Document that was validated")
    status: ValidationStatus = Field(default=ValidationStatus.PENDING)
    standards_set: str = Field(description="Standards set used")
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    findings: list[Finding] = Field(default_factory=list)
    # Per-layer rollups computed by the decision-tree validator. Empty
    # on validations written before rollups existed so the UI can fall
    # back to its flat-finding rendering.
    layer_verdicts: list[LayerVerdictRollup] = Field(
        default_factory=list,
        description="Per-(tier × layer) decision-tree rollup",
    )
    # Explicit architecture-level verdict. Mirrors what the UI rollup
    # banner will display. None on validations without rollups.
    architecture_verdict: Optional[LayerVerdict] = Field(
        default=None,
        description="AND of all layer_verdicts. None on legacy rows.",
    )
    # The tier the validator actually ran against. Equals
    # document.declared_tier unless the user supplied tier_override.
    tier_used: Optional[int] = Field(
        default=None,
        description="Tier the validator filtered rules by for this run",
    )
    tier_override: Optional[int] = Field(
        default=None,
        description="If set, the user-supplied tier override that overrode declared_tier",
    )
    diagram_findings: list[DiagramFinding] = Field(default_factory=list, description="Findings from diagram visual analysis")
    overall_score: Optional[float] = Field(default=None, description="Overall compliance score 0-100")
    summary: Optional[str] = Field(default=None, description="Executive summary")
    error_message: Optional[str] = None
    document_content_hash: Optional[str] = Field(
        default=None,
        description="SHA256 of source document bytes at validation time (cache key)",
    )
    standards_set_hash: Optional[str] = Field(
        default=None,
        description="SHA256 of the standards set at validation time (cache key)",
    )
    # Email of the user who started the run. Authorizes cancellation (a run
    # bills Bedrock until it stops, so it must not be cancellable by anyone who
    # merely holds the validator role). None on rows written before this field.
    triggered_by: Optional[str] = Field(
        default=None,
        description="Email of the user who triggered this validation",
    )
    # Issue counts, computed once at save time and stored on the row.
    # List views need them without paying an S3 GET per row for `findings`,
    # which `list_validations` deliberately leaves empty.
    findings_count: int = Field(
        default=0,
        description="Findings that represent an issue (fail or warning). Excludes passes, N/A and not_addressed.",
    )
    critical_count: int = Field(default=0, description="Failing findings at critical severity")
    high_count: int = Field(default=0, description="Failing findings at high severity")
    evaluated_count: int = Field(
        default=0,
        description="Number of standards evaluated so far (live progress)",
    )
    total_standards: int = Field(
        default=0,
        description="Total number of standards being evaluated",
    )
    # Telemetry — populated by the chunked validator. None on older
    # rows so the UI can hide the footnote when it's missing.
    chunks_per_document: Optional[int] = Field(
        default=None,
        description="Number of document chunks the validator fanned out across",
    )
    bedrock_calls: Optional[int] = Field(
        default=None,
        description="Total Bedrock Converse calls made for this validation",
    )
    bedrock_input_tokens: Optional[int] = Field(
        default=None,
        description="Sum of input tokens consumed across all calls",
    )
    bedrock_output_tokens: Optional[int] = Field(
        default=None,
        description="Sum of output tokens generated across all calls",
    )
    estimated_cost_usd: Optional[float] = Field(
        default=None,
        description="Estimated USD cost based on Bedrock pricing for the validator model",
    )
    # Count of rules excluded by tier filtering. evaluated_count +
    # skipped_count + (any error rows) = the size of the active rule
    # library at validation time. Surfaced in the UI so users
    # understand why "47 of 80 evaluated" — the other 33 weren't
    # applicable to the SDD's tier.
    skipped_count: Optional[int] = Field(
        default=None,
        description="Number of rules skipped (sum of all filter dimensions). See skipped_count_breakdown for the per-dimension split.",
    )
    # Per-filter breakdown of the skipped_count total.
    # Keys: "tier" (rule's applies_to_tiers didn't include the SDD's
    # declared_tier), "scope" (rule's hosting_scope didn't match
    # the SDD's). The frontend reads this to render a precise
    # "X tier-mismatched, Y scope-mismatched" tooltip on the
    # validation detail page. Optional + None for any older
    # validation row that didn't track the breakdown.
    skipped_count_breakdown: Optional[dict[str, int]] = Field(
        default=None,
        description="Per-filter breakdown of skipped_count: {'tier': N, 'scope': M}. None on pre-Phase-3 validation rows.",
    )

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class ValidationSummary(BaseModel):
    """Summary of a validation result for list views."""
    validation_id: str
    document_id: str
    status: ValidationStatus
    standards_set: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    overall_score: Optional[float] = None
    findings_count: int = 0
    critical_count: int = 0
    high_count: int = 0
    evaluated_count: int = 0
    total_standards: int = 0
