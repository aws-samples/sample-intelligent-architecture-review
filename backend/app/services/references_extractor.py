# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Extract References from a Document's extraction_notes.

Users author the prompt as free-form markdown. To distinguish
"context that should land in the rule library" from "factual material
that should land in References", they author an optional `## References`
section at the bottom of the prompt:

    ## Some other section
    ...rule extraction guidance...

    ## References
    ### DC peer table (v2 doc page 9)
    [tier: 1,2; layer: storage; scope: on_prem]
    | Primary | Secondary | DR |
    |---------|-----------|----|
    | VA      | TX        | CTOS-1 |

    ### Sanctioned A-A-DR triples
    [scope: both]
    The list of pairings approved by the Architecture Council for 2026...

Each `### <name>` becomes one Reference. The optional `[key: value; ...]`
metadata line right after the heading sets applies_to_tiers / layer /
hosting_scope; everything else inside the section is the body.

Idempotency: re-extraction matches existing References by
(name, source_document) and updates body in place rather than creating
duplicates. References that previously parsed but no longer appear in
the prompt are LEFT ALONE — we don't auto-archive on re-extraction
because the user might have manually edited them. The user can delete
explicitly via the References UI.

Failures are non-fatal — log warning, continue. The rest of the
extraction pipeline doesn't depend on References parsing.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# Tier list at extraction time defaults to "all five" so a Reference
# without explicit metadata applies broadly. The validator's
# scope-filter still narrows by layer + hosting_scope.
_DEFAULT_TIERS = [1, 2, 3, 4, 5]
_VALID_LAYERS = {
    "compute",
    "storage",
    "network",
    "data",
    "messaging",
    "monitoring",
    "general",
}
_VALID_SCOPES = {"on_prem", "cloud", "both"}


_REFERENCES_HEADING_RE = re.compile(
    r"^\s*##\s+References\s*$", re.IGNORECASE | re.MULTILINE
)


def _slice_references_section(notes: str) -> Optional[str]:
    """Find the `## References` block and return its body (everything
    until the next `## ` heading or end of text). Returns None when
    no References section is present.
    """
    m = _REFERENCES_HEADING_RE.search(notes)
    if not m:
        return None
    start = m.end()
    # Find the next H2 (or beyond) heading.
    rest = notes[start:]
    next_h2 = re.search(r"^\s*##\s+(?!#)", rest, re.MULTILINE)
    if next_h2:
        return rest[: next_h2.start()]
    return rest


def _parse_metadata_line(line: str) -> Optional[dict]:
    """A line like `[tier: 1,2; layer: storage; scope: on_prem]` →
    `{"applies_to_tiers": [1,2], "layer": "storage", "hosting_scope": "on_prem"}`.

    Returns None if the line isn't a metadata line. Tolerant: unknown
    keys are ignored (logged), bad values for known keys fall back to
    defaults silently.
    """
    s = line.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return None
    inner = s[1:-1]
    out: dict = {}
    for part in inner.split(";"):
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        key = k.strip().lower()
        val = v.strip()
        if key in ("tier", "tiers", "applies_to_tiers"):
            try:
                tiers = sorted(
                    {int(x.strip()) for x in val.split(",") if x.strip()}
                )
                if tiers and all(1 <= t <= 5 for t in tiers):
                    out["applies_to_tiers"] = tiers
            except ValueError:
                logger.debug(f"References: invalid tier spec {val!r}")
        elif key == "layer":
            v_lower = val.lower()
            if v_lower in _VALID_LAYERS:
                out["layer"] = v_lower
        elif key in ("scope", "hosting_scope"):
            v_lower = val.lower()
            if v_lower in _VALID_SCOPES:
                out["hosting_scope"] = v_lower
    return out


def parse_references(notes: str) -> list[dict]:
    """Parse the `## References` section out of *notes* and return a
    list of Reference dicts ready for ReferencesStore.create().

    Each dict carries:
      name (str), body (str), applies_to_tiers (list[int]),
      layer (Optional[str]), hosting_scope (str).

    Empty list when no `## References` section is present, when the
    section is empty, or when no `### <name>` sub-headings appear.
    """
    if not notes or not notes.strip():
        return []
    section = _slice_references_section(notes)
    if section is None:
        return []
    section = section.strip()
    if not section:
        return []

    refs: list[dict] = []
    # Split on `### ` headings while preserving the heading line.
    # `re.split` keeps the matched groups when the pattern uses
    # parentheses; we use a positive lookahead so the heading line
    # stays at the start of each chunk.
    chunks = re.split(r"(?m)^(?=###\s+)", section)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or not chunk.startswith("###"):
            continue
        lines = chunk.splitlines()
        # First line: ### <name>
        heading = lines[0]
        m = re.match(r"^###\s+(.+?)\s*$", heading)
        if not m:
            continue
        name = m.group(1).strip()
        if not name:
            continue
        # Default metadata; overridden if a metadata line follows.
        meta: dict = {
            "applies_to_tiers": list(_DEFAULT_TIERS),
            "layer": None,
            "hosting_scope": "on_prem",
        }
        body_lines = lines[1:]
        if body_lines:
            parsed_meta = _parse_metadata_line(body_lines[0])
            if parsed_meta is not None:
                meta.update(parsed_meta)
                body_lines = body_lines[1:]
        body = "\n".join(body_lines).strip()
        if not body:
            # A heading with no body is not useful as a Reference.
            logger.info(f"References: skipping empty reference {name!r}")
            continue
        refs.append(
            {
                "name": name,
                "body": body,
                "applies_to_tiers": meta["applies_to_tiers"],
                "layer": meta["layer"],
                "hosting_scope": meta["hosting_scope"],
            }
        )
    return refs


def save_references_for_document(
    *,
    document_id: str,
    extraction_notes: Optional[str],
) -> dict:
    """Parse the extraction_notes and persist the References to DDB.

    Idempotent on re-extraction: matches existing References by
    (name, source_document) and updates body + metadata in place.

    Returns:
      {"created": int, "updated": int, "skipped": int}
    """
    summary = {"created": 0, "updated": 0, "skipped": 0}
    if not extraction_notes:
        return summary
    parsed = parse_references(extraction_notes)
    if not parsed:
        return summary

    # Defer the DDB import so unit tests can patch the parser without
    # standing up boto3.
    from app.services.references_store import references_store

    # Build a (name → existing ref) lookup scoped to this document so
    # update-in-place beats create-then-dedupe.
    existing = [
        r
        for r in references_store.list_all(status=None)
        if r.get("source_document") == document_id
    ]
    by_name = {r.get("name"): r for r in existing}

    for spec in parsed:
        try:
            prior = by_name.get(spec["name"])
            if prior:
                fields = {
                    "body": spec["body"],
                    "applies_to_tiers": spec["applies_to_tiers"],
                    "layer": spec["layer"],
                    "hosting_scope": spec["hosting_scope"],
                }
                references_store.update(prior["id"], fields)
                summary["updated"] += 1
            else:
                references_store.create(
                    name=spec["name"],
                    body=spec["body"],
                    applies_to_tiers=spec["applies_to_tiers"],
                    layer=spec["layer"],
                    hosting_scope=spec["hosting_scope"],
                    source_document=document_id,
                    status="active",
                )
                summary["created"] += 1
        except Exception as exc:
            logger.warning(
                f"References: failed to persist {spec.get('name')!r} "
                f"for {document_id}: {exc}"
            )
            summary["skipped"] += 1
    return summary
