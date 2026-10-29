# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared multimodal unit-processing primitive.

ONE place that builds the `{text + structured-tables-as-markdown + full-page
image}` message for a Unit. Both extraction and (eventually) validation call
this, so the "how do we show a page/slide to the model" logic can't diverge
again — which is exactly how PDF and PPTX drifted apart in the first place.

Message shape mirrors the working patterns in
app.services.pptx_converter / pdf_multimodal:
  - text content block: {"text": "..."}
  - image content block: {"image": {"format": "png", "source": {"bytes": raw}}}
    RAW bytes — boto3 base64-encodes over the wire; pre-encoding double-encodes
    and Bedrock rejects with "Could not process image".

Model policy: per-unit multimodal work runs on Sonnet 4.6 (strong vision,
~5x cheaper/faster than Opus). Opus is reserved for the cross-unit
synthesis pass only.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.agents.extraction.unit import Unit
from app.bedrock_models import SONNET, model_for

logger = logging.getLogger(__name__)

# Per-unit multimodal model — Sonnet 4.6. Overridable for eval / tuning.
UNIT_MODEL_ID = model_for("EXTRACTION_UNIT_MODEL_ID", SONNET)


def build_unit_message(unit: Unit, image_bytes: Optional[bytes],
                       preamble: Optional[str] = None,
                       topology_block: Optional[str] = None) -> list:
    """Compose the multimodal user-message content blocks for one Unit.

    Order: orientation → preamble → native text → structured tables (markdown)
    → verified topology (diagram units) → image. Tables are rendered as
    markdown grids (not the old TSV blob) so the model sees real column
    structure; the image carries topology that no text field holds.

    ``topology_block``: for diagram units, the reconciled component
    graph from the self-consistency pass, injected so rule extraction is
    grounded in a *verified* reading rather than a fresh (possibly divergent)
    one. Empty/None for text/table units.
    """
    parts: list[str] = [f"[{unit.label} · {unit.classification}]"]
    if preamble:
        parts.append(f"\nPRECEDING CONTEXT (for orientation):\n{preamble.strip()}")
    if unit.text.strip():
        parts.append(f"\nNATIVE TEXT (authoritative for rule wording):\n{unit.text.strip()}")
    else:
        parts.append("\n(no native text on this unit)")
    if unit.has_tables:
        parts.append(
            "\nTABLES (structured — each is a real grid; preserve tier/layer "
            "matrix semantics; a cell with content is a rule candidate):\n"
            + unit.tables_as_markdown()
        )
    if topology_block:
        parts.append("\n" + topology_block)

    # Custom icon legend — for diagram units, tell the model this
    # org's non-standard glyph vocabulary so it doesn't misread proprietary
    # on-prem icons. Text dictionary always applied; icon-image few-shot
    # blocks appended after (capped). Best-effort: no legend → no change.
    legend_text = ""
    legend_image_blocks: list = []
    if image_bytes and getattr(unit, "is_diagram", False):
        try:
            from app.services.icon_legend_inject import (
                build_legend_text,
                build_legend_image_blocks,
            )
            legend_text = build_legend_text()
            if legend_text:
                from app.services.document_store import document_store
                legend_image_blocks = build_legend_image_blocks(doc_store=document_store)
        except Exception:  # noqa: BLE001
            legend_text = ""
            legend_image_blocks = []
    if legend_text:
        parts.append("\n" + legend_text)

    parts.append(
        f"\nExtract HA standards via save_standard. Use '{unit.label}' as "
        f"page_reference."
    )

    blocks: list = [{"text": "\n".join(parts)}]
    # Reference icon crops come BEFORE the page image so they read as a
    # legend the model consults while looking at the diagram.
    blocks.extend(legend_image_blocks)
    if image_bytes:
        blocks.append({"image": {"format": "png", "source": {"bytes": image_bytes}}})
    return blocks


def resolve_image_bytes(unit: Unit, doc_store) -> Optional[bytes]:
    """Fetch the full-page PNG for a Unit (S3 key -> bytes). Best-effort:
    a missing image degrades to text-only rather than failing the unit."""
    if not unit.image_ref:
        return None
    try:
        return doc_store.download_document(unit.image_ref)
    except Exception as exc:
        logger.warning(f"{unit.label}: image fetch failed ({exc}); text-only")
        return None
