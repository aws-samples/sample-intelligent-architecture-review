# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pydantic models for HA validation standards."""

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


def _empty_string_to_none(v: Any) -> Any:
    """Pre-validator that maps empty / whitespace strings to None.

    Without this, a payload like ``{"layer": ""}`` from the Edit form
    422s on the ``Optional[RuleLayer]`` Literal because "" isn't in
    the accepted set. The frontend already conditional-spreads to
    avoid sending blanks, but the backend keeps a belt-and-braces
    coercion so a slipped blank doesn't blow up a legitimate edit.
    """
    if isinstance(v, str) and not v.strip():
        return None
    return v

# All five tiers — used as the universal default so rules written
# before tier scoping existed behave as if they applied to every tier
# (their pre-tier-awareness behavior).
ALL_TIERS: list[int] = [1, 2, 3, 4, 5]

# Fixed architectural-layer vocabulary. The review framework groups
# rules by layer; this set is locked and not user-editable. The
# frontend ships a parallel const tuple in lib/api.ts; a unit test
# asserts the two sets are equal.
#
# `general` is the catch-all for cross-cutting rules that don't belong
# to a single architectural layer (security baselines, observability
# rules, RTO/RPO declarations, etc.). Free-form extraction defaults
# newly-saved rules to `general` so the Standards page filters /
# sections render them under a real bucket.
RuleLayer = Literal["datacenter", "data", "web", "application", "load_balancer", "general"]

# Rule-type semantics driving the decision-tree validator's branch
# logic.
RuleType = Literal["founding", "substitution", "exception"]

# Exception scope. Only meaningful when rule_type=exception.
#   "rule"  → exempts a single founding rule (parent_rule_id is set).
#   "layer" → exempts an entire (tier, layer); no parent_rule_id.
# Older exception rows all have parent_rule_id set, so the read-side
# compat default below treats missing exception_scope as "rule".
ExceptionScope = Literal["rule", "layer"]

# Hosting scope. Tags whether a rule applies to on-prem
# workloads, cloud workloads, or both. Required on every rule and
# every Document going forward. Tagging the source at upload time
# keeps validation accurate when standards for different deployment
# models share the same library.
HostingScope = Literal["on_prem", "cloud", "both"]


class StandardSource(str, Enum):
    """Provenance of a standard.

    - DOCUMENT: extracted from an uploaded standards PDF by the extraction agent
    - USER: created/edited by a user through the UI
    - GENERATED: proposed by the rules-curator chat agent. Once a user
      approves the proposal it stays GENERATED so the UI can keep
      showing the "AI-generated" label.
    """

    DOCUMENT = "document"
    USER = "user"
    GENERATED = "generated"


class StandardStatus(str, Enum):
    """Lifecycle state of a standard.

    - ACTIVE: participates in validation runs and the cache hash.
    - PROPOSED: AI suggestion awaiting human approval. Filtered out of
      validation and the cache hash; surfaced in the curate page.
    - ARCHIVED: soft-deleted. Filtered out of validation and listings
      by default but kept for audit so we can see what was once active.
    """

    ACTIVE = "active"
    PROPOSED = "proposed"
    ARCHIVED = "archived"


class ProposalKind(str, Enum):
    """What a proposed standard is asking us to do.

    Only meaningful when status == PROPOSED.

    - ADD: brand-new rule. The proposal row carries the new fields.
    - EDIT: change an existing rule. target_id points at the rule;
      the proposal row carries the full *new* values (not a diff).
    - DELETE: archive an existing rule. target_id points at the rule;
      the proposal row's own fields are decorative (rationale lives in
      its description / criteria).
    """

    ADD = "add"
    EDIT = "edit"
    DELETE = "delete"


class CreateStandardRequest(BaseModel):
    """Request body for creating a new user-defined standard."""

    name: str
    description: str
    severity: Literal["critical", "high", "medium", "low"]
    category: str
    validation_criteria: str
    # Which service tiers this rule applies to. Defaults to all five
    # so a user-created rule without explicit tier scope behaves
    # universally (preserves the old behavior).
    applies_to_tiers: list[int] = Field(default_factory=lambda: list(ALL_TIERS))
    # Optional structured-rule metadata. The route handler enforces the
    # OMIT WHEN EMPTY contract — these are only persisted to DDB when
    # non-empty after normalisation.
    rule_type: Optional[RuleType] = None
    layer: Optional[RuleLayer] = None
    parent_rule_id: Optional[str] = None
    # Exception scope. Only persisted (and only meaningful) when
    # rule_type=exception. "rule" is the default for older rows since
    # they all have parent_rule_id set.
    exception_scope: Optional[ExceptionScope] = None
    # Required hosting scope. No Optional; Pydantic 422s
    # on omission. The frontend's segmented control / dropdown
    # always emits one of the three real values, so this is safe
    # to be required.
    hosting_scope: HostingScope
    @field_validator("rule_type", "layer", "parent_rule_id", "exception_scope", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        return _empty_string_to_none(v)


class UpdateStandardRequest(BaseModel):
    """Partial-update payload for an existing standard.

    Note: ``exclude_none=True`` semantics mean sending ``null`` for a
    field is treated as "no change" — the route never forwards null
    values to the store. Empty strings on the structured-rule fields
    (``layer``, ``rule_type``, ``parent_rule_id``) are also stripped
    server-side before reaching the store. To clear a previously-set
    ``layer`` or ``rule_type``, delete and recreate the rule. Cleaner
    three-state PATCH semantics are not implemented yet.
    """

    name: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[Literal["critical", "high", "medium", "low"]] = None
    category: Optional[str] = None
    validation_criteria: Optional[str] = None
    applies_to_tiers: Optional[list[int]] = None
    rule_type: Optional[RuleType] = None
    layer: Optional[RuleLayer] = None
    parent_rule_id: Optional[str] = None
    # PATCH semantics same as the rest of the structured-rule fields:
    # omit to leave unchanged, send empty string to clear, send
    # "rule"/"layer" to set.
    exception_scope: Optional[ExceptionScope] = None
    # PATCH semantics: hosting_scope IS Optional on the Update
    # request, because omitting it means "don't change this
    # field." The route handler's `model_dump(exclude_unset=True)`
    # / `exclude_none=True` filtering preserves the existing value.
    hosting_scope: Optional[HostingScope] = None

    @field_validator("rule_type", "layer", "parent_rule_id", "exception_scope", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        return _empty_string_to_none(v)


class StandardResponse(BaseModel):
    """Response model for a single standard."""

    id: str
    name: str
    description: str
    severity: str
    category: str
    validation_criteria: str
    source: StandardSource
    status: StandardStatus = StandardStatus.ACTIVE
    source_document: Optional[str] = None
    created_at: str
    updated_at: str
    # Proposal-only metadata. Set when status == PROPOSED.
    proposal_kind: Optional[ProposalKind] = None
    target_id: Optional[str] = None
    rationale: Optional[str] = None
    # Tier scope. Rows that pre-date tier awareness are backfilled to
    # ALL_TIERS at load time so they continue to validate against
    # every tier.
    applies_to_tiers: list[int] = Field(default_factory=lambda: list(ALL_TIERS))
    # Structured-rule metadata. Optional — flat rules omit these.
    rule_type: Optional[RuleType] = None
    layer: Optional[RuleLayer] = None
    parent_rule_id: Optional[str] = None
    # Exception scope. Only meaningful when rule_type=exception. Older
    # exception rows have parent_rule_id set, so the read-side compat
    # in validation_store defaults missing exception_scope to "rule"
    # for those.
    exception_scope: Optional[ExceptionScope] = None
    # Required. Every persisted standard has a scope tag.
    hosting_scope: HostingScope
