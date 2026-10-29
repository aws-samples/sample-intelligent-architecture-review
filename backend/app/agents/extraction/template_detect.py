# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Template / fill-in-form detection.

The extraction pipeline is for STANDARDS SOURCES (rulebooks/policies). A user
can mistakenly upload a fill-in SDD *template* (the form an architect completes)
as a standards doc, which makes the extractor produce junk rules from blank
form fields. This module detects that case at the document level so
run_extraction can WARN and skip rather than manufacture rules from
instruction text and empty cells.

Deterministic + cheap (no LLM): scores each Unit as template-like from two
signals, then decides at the document level:
  - instruction phrases ("fill out the table for each…", "provide all…"),
  - blank-form tables (many rows whose value cells are empty / placeholder).

A rulebook (filled prose + populated matrices) scores low; a blank SDD
template scores high. The threshold is deliberately conservative — we only
flag when the document is overwhelmingly template-like, so a standards doc
that happens to contain one instruction line still extracts normally.
"""

from __future__ import annotations

import logging
import re

from app.agents.extraction.unit import Unit

logger = logging.getLogger(__name__)

_INSTRUCTION_HINTS = (
    "fill out", "fill in", "provide all", "provide information", "provide a",
    "specify all", "specify information", "please include", "please provide",
    "list of new", "for each new", "enter the", "complete the following",
    "fill the table", "denotes", "populate",
)

# Placeholder cell content that signals an unfilled form field.
_PLACEHOLDER_RE = re.compile(
    r"^\s*(|n/?a|tbd|yes/no|<[^>]*>|iaas/saas/paas|xxx+|\.\.\.|—|-)\s*$",
    re.IGNORECASE,
)


def _table_is_blank_form(table) -> bool:
    """A table looks like a blank form if a large share of its non-header
    cells are empty or placeholder tokens."""
    rows = table.rows if hasattr(table, "rows") else table
    if len(rows) < 2:
        return False
    cells, blank = 0, 0
    for row in rows[1:]:  # skip header row
        for c in row:
            cells += 1
            if _PLACEHOLDER_RE.match((c or "").strip()):
                blank += 1
    return cells > 0 and (blank / cells) >= 0.5


def score_unit(unit: Unit) -> str:
    """Classify a Unit as 'instruction', 'placeholder', or 'filled'."""
    text_low = (unit.text or "").lower()
    has_instruction = any(h in text_low for h in _INSTRUCTION_HINTS)
    blank_tables = sum(1 for t in unit.tables if _table_is_blank_form(t))

    if unit.tables and blank_tables == len(unit.tables) and blank_tables > 0:
        return "placeholder"
    if has_instruction and (not unit.tables or blank_tables >= 1):
        return "instruction"
    return "filled"


def classify_document(units: list[Unit], threshold: float = 0.5) -> dict:
    """Tag each unit's content_kind (in place) and decide if the WHOLE
    document is a fill-in template rather than a standards source.

    Returns {"is_template": bool, "template_ratio": float,
             "instruction_units": int, "placeholder_units": int}.
    """
    inst = placeholder = 0
    considered = 0
    for u in units:
        # Title/near-empty units don't count toward the ratio either way.
        if len((u.text or "").strip()) < 30 and not u.tables:
            u.content_kind = "filled"
            continue
        considered += 1
        kind = score_unit(u)
        u.content_kind = kind
        if kind == "instruction":
            inst += 1
        elif kind == "placeholder":
            placeholder += 1

    template_units = inst + placeholder
    ratio = (template_units / considered) if considered else 0.0
    is_template = ratio >= threshold
    logger.info(
        f"template classification: {template_units}/{considered} template-like "
        f"units (ratio={ratio:.2f}) -> is_template={is_template}"
    )
    return {
        "is_template": is_template,
        "template_ratio": round(ratio, 2),
        "instruction_units": inst,
        "placeholder_units": placeholder,
        "considered_units": considered,
    }
