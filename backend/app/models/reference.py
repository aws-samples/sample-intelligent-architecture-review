# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reference entity.

References are scope-tagged context blocks that are NOT testable as
rules but supply the validator with factual material it needs to
make a correct judgment. The canonical example is page 9 of the v2
Architecture Review process doc — a DC-peer table that says
"VA + TX + CTOS-1 DR" is a sanctioned A-A-DR triple. Without that
table in the validator's prompt, an SDD claiming those datacenters
gets evaluated against the founding pattern alone and the
substitution decision can't be made correctly.

Stored in a separate references DynamoDB
table parallel to standards. Never mixed into the standards table
because every list/filter/curate flow would otherwise have to
learn to ignore them.

Linked to rules by scope tuple at validator-prompt-build time:
References whose (tier, layer, hosting_scope) covers a given rule
are auto-attached to that rule's prompt under a REFERENCES header.
"""

from __future__ import annotations

from typing import Any, Optional, Literal
from pydantic import BaseModel, Field, field_validator

from app.models.standard import (
    ALL_TIERS,
    HostingScope,
    RuleLayer,
    _empty_string_to_none,
)


# Same status taxonomy as Standard so the Curate flow can propose new
# References and the approval/reject lifecycle uses identical code.
ReferenceStatus = Literal["active", "proposed", "archived"]


class CreateReferenceRequest(BaseModel):
    """Body for POST /api/references."""

    name: str
    body: str = Field(
        description=(
            "Markdown body. Tables / lists are preserved verbatim — the "
            "v2 doc's DC peer table is a primary use case."
        ),
    )
    applies_to_tiers: list[int] = Field(default_factory=lambda: list(ALL_TIERS))
    layer: Optional[RuleLayer] = Field(
        default=None,
        description="Architecture layer the Reference applies to. None = applies across layers.",
    )
    hosting_scope: HostingScope
    source_document: Optional[str] = None
    page_reference: Optional[str] = None
    source_quote: Optional[str] = None

    @field_validator("layer", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        return _empty_string_to_none(v)


class UpdateReferenceRequest(BaseModel):
    """PATCH semantics: omit a field to leave it unchanged. Same shape
    as UpdateStandardRequest."""

    name: Optional[str] = None
    body: Optional[str] = None
    applies_to_tiers: Optional[list[int]] = None
    layer: Optional[RuleLayer] = None
    hosting_scope: Optional[HostingScope] = None
    source_document: Optional[str] = None
    page_reference: Optional[str] = None
    source_quote: Optional[str] = None

    @field_validator("layer", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        return _empty_string_to_none(v)


class ReferenceResponse(BaseModel):
    """Response model for a single Reference."""

    id: str = Field(description="HA-R-XXX")
    name: str
    body: str
    applies_to_tiers: list[int] = Field(default_factory=lambda: list(ALL_TIERS))
    layer: Optional[RuleLayer] = None
    hosting_scope: HostingScope
    source_document: Optional[str] = None
    page_reference: Optional[str] = None
    source_quote: Optional[str] = None
    status: ReferenceStatus = "active"
    created_at: str
    updated_at: str
