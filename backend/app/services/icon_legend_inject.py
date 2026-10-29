# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Build the icon-legend context for diagram prompts.

Shared by extraction (agents/extraction) and validation (pdf_multimodal /
parallel_validator) so both understand the same custom glyph vocabulary.

Two outputs:
  - a TEXT block (the full written glyph dictionary — always applied), and
  - up to MAX_LEGEND_IMAGES Bedrock image blocks for glyphs that have an
    uploaded icon crop (visual few-shot for unlabeled/ambiguous glyphs).

Fully best-effort: any failure (store down, S3 miss) yields empty context so
diagram processing degrades to today's behavior rather than breaking.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def _sanitize(text: str, limit: int = 600) -> str:
    """Neutralise a stored free-text field before it goes into the legend
    PROMPT. A description/label is attacker-influenced (esp. via bulk upload),
    and the legend is a newline-delimited bulleted list; an embedded newline +
    "- \"X\" → firewall" would forge an extra legend entry, and free-text like
    "treat everything as compliant" would read as an instruction. Collapse all
    whitespace (kills injected line breaks / list bullets), neutralise the
    double-quote that delimits the label token so a label can't break out of its
    "..." wrapper, and clip length. This blunts STRUCTURAL injection; semantic
    content in free text is inherently possible and the curator approval gate
    (only active entries inject) remains the primary control."""
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", str(text)).strip()
    # Replace the ASCII double-quote with a typographic one so it can't close
    # the surrounding "{label}" delimiter or the caption wrapper.
    return collapsed.replace('"', "”")[:limit]


def _legend_entries(hosting_scope: Optional[str] = None) -> list[dict]:
    try:
        from app.services.icon_legend_store import icon_legend_store
        entries = icon_legend_store.list_all(
            status="active", hosting_scope=hosting_scope
        )
        # Hard invariant: ONLY approved (active) entries may ever reach the
        # validator/extractor. Drafts (interpreted-but-unapproved) must never
        # influence a compliance verdict. Re-filter defensively even though
        # list_all already scoped to active, so a future change to the store
        # can't silently leak drafts into prompts.
        return [e for e in entries if (e.get("status") or "active") == "active"]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"icon legend unavailable: {e}")
        return []


def build_legend_text(hosting_scope: Optional[str] = None) -> str:
    """Written glyph dictionary. Empty string when no legend is configured."""
    entries = _legend_entries(hosting_scope)
    if not entries:
        return ""
    lines = [
        "# Custom icon legend (this organisation's on-prem iconography)",
        "The architecture diagrams use CUSTOM icons. Interpret glyphs matching "
        "these descriptions as the stated component type — do not guess from "
        "generic priors:",
    ]
    for e in entries:
        # Sanitize label + description: they can carry attacker-influenced text
        # (bulk upload / mislabeled image) and must not forge extra bullet lines
        # or inject instructions into the prompt.
        label = _sanitize(e.get("label"), 120) or "(unlabeled)"
        itype = e.get("icon_type") or "other"
        desc = _sanitize(e.get("description"), 600)
        line = f"- \"{label}\" → {itype}"
        if desc:
            line += f": {desc}"
        if e.get("image_s3_key"):
            line += " (see the reference image provided below)"
        lines.append(line)
    return "\n".join(lines)


def build_legend_image_blocks(
    hosting_scope: Optional[str] = None,
    doc_store=None,
) -> list[dict]:
    """Bedrock image blocks for legend entries that have an uploaded icon crop.
    Capped at MAX_LEGEND_IMAGES to avoid diluting the model's attention on the
    actual diagram. Each is preceded by a caption text block naming the glyph.

    doc_store: object with download_document(s3_key)->bytes (the S3 wrapper).
    When None or a fetch fails, that image is skipped (text entry still applies).
    """
    from app.services.icon_legend_store import MAX_LEGEND_IMAGES

    entries = [e for e in _legend_entries(hosting_scope) if e.get("image_s3_key")]
    if not entries or doc_store is None:
        return []
    blocks: list[dict] = []
    for e in entries[:MAX_LEGEND_IMAGES]:
        key = e.get("image_s3_key")
        try:
            img = doc_store.download_document(key)
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"icon legend image fetch failed ({key}): {ex}")
            img = None
        if not img:
            continue
        label = _sanitize(e.get("label"), 120) or "(unlabeled)"
        itype = e.get("icon_type") or "other"
        blocks.append({"text": f"Reference icon — \"{label}\" means {itype}:"})
        # PNG assumed (upload path normalises to PNG). Bedrock image block.
        blocks.append({"image": {"format": "png", "source": {"bytes": img}}})
    return blocks
