# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Diagram topology extraction + self-consistency verification.

Architecture diagrams carry the HA topology (redundancy, failover, DR,
load-balancing) that the text never states — connectors aren't in the file
XML, so source→target edges live *only* in the pixels. The per-unit extractor
reads those pixels and can silently misread them.

This module adds a verify-then-trust step for diagram units, on the strongest
available vision model (Opus 4.8):

  1. ``extract_topology(image)`` — Opus reads the page image and returns a
     structured component graph: {nodes, edges, ha_attributes, notes}.
  2. ``verify_topology(image, graph)`` — a SECOND, adversarial Opus pass that
     is shown the same image AND the claimed graph, and asked to find every
     node/edge that is WRONG, MISSING, or HALLUCINATED. It returns a
     confidence (high|medium|low) + a discrepancy list. This is the
     self-consistency check: agreement across two independent readings raises
     confidence; disagreement lowers it and flags the unit for human review.
  3. ``analyze_diagram(image)`` composes them and returns a ``DiagramAnalysis``
     the caller injects into rule extraction and persists onto the resulting
     rules (topology_graph + topology_confidence + needs_review).

Everything here is BEST-EFFORT and NON-FATAL: any Bedrock/parse failure
returns ``None`` (or a low-confidence analysis) so the unit degrades to
today's behavior rather than sinking the extraction run.

Bedrock config (verified in account 577, 2026-07-24):
  - Extended thinking on Opus 4.8 uses ``{"thinking": {"type": "adaptive"}}``
    — NOT ``type: enabled`` (that's Sonnet's shape) and with NO budget_tokens.
  - ``temperature`` is deprecated on Opus and must be omitted.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional
from app.bedrock_models import OPUS_VISION, is_opus, model_for

logger = logging.getLogger(__name__)

# Strongest available vision model for topology — Opus 4.8, verified invokable
# in 577. Overridable so a customer account without Opus access can point this
# at Sonnet (the pipeline still works, just with weaker diagram reading).
DIAGRAM_MODEL_ID = model_for("EXTRACTION_DIAGRAM_MODEL_ID", OPUS_VISION)

# Self-consistency verify pass. On by default — it's the whole point. Set to
# "0" to extract the graph without the second adversarial reading (cheaper;
# every diagram then reports medium confidence).
TOPOLOGY_VERIFY = os.getenv("EXTRACTION_TOPOLOGY_VERIFY", "1") == "1"

_MAX_TOKENS = int(os.getenv("EXTRACTION_TOPOLOGY_MAX_TOKENS", "6000"))

_VALID_CONFIDENCE = {"high", "medium", "low"}


@dataclass
class DiagramAnalysis:
    """Result of analysing one diagram unit."""
    graph: dict = field(default_factory=dict)      # {nodes, edges, ha_attributes, notes}
    confidence: str = "medium"                     # high | medium | low
    discrepancies: list[str] = field(default_factory=list)
    verified: bool = False                         # did the verify pass run + agree

    @property
    def needs_review(self) -> bool:
        """A diagram-derived rule needs human review unless the graph was
        verified at HIGH confidence. Anything less means the two readings
        disagreed (or verify was skipped/failed) — exactly when a misread is
        most likely."""
        return self.confidence != "high"

    def to_prompt_block(self) -> str:
        """Render the verified graph as a context block injected into the
        rule-extraction prompt, so rules are grounded in the reconciled
        topology rather than a fresh (possibly different) reading."""
        if not self.graph:
            return ""
        nodes = self.graph.get("nodes") or []
        edges = self.graph.get("edges") or []
        ha = self.graph.get("ha_attributes") or {}
        lines = ["VERIFIED TOPOLOGY (extracted from this diagram, "
                 f"confidence={self.confidence}):"]
        if nodes:
            lines.append("Components: " + "; ".join(
                f"{n.get('label') or n.get('id')}"
                + (f" [{n.get('type')}]" if n.get('type') else "")
                for n in nodes[:40]
            ))
        if edges:
            lines.append("Connections: " + "; ".join(
                f"{e.get('src')}→{e.get('dst')}"
                + (f" ({e.get('kind')})" if e.get('kind') else "")
                for e in edges[:60]
            ))
        if ha:
            kept = {k: v for k, v in ha.items() if v}
            if kept:
                lines.append("HA attributes: " + json.dumps(kept))
        if self.discrepancies:
            lines.append(
                "CAUTION — the verification pass flagged possible misreadings: "
                + "; ".join(self.discrepancies[:8])
                + ". Treat affected rules as tentative."
            )
        return "\n".join(lines)


def _make_bedrock_client():
    """Bedrock runtime client. Indirection so tests can monkeypatch."""
    from app.services.parallel_validator import _make_bedrock_client as _f
    return _f()


def _parse_first_json_object(text: str) -> Optional[dict]:
    """Extract the first JSON object from model output, tolerating ```json
    fences and trailing prose. Returns None on failure (never raises).

    Mirrors synthesis._parse_first_json_object but non-raising, since a
    topology parse failure must degrade gracefully, not abort the unit."""
    if not text:
        return None
    try:
        decoder = json.JSONDecoder()
        start = text.find("{")
        if start == -1:
            return None
        obj, _ = decoder.raw_decode(text[start:])
        return obj if isinstance(obj, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


def _converse_image_json(client, image_bytes: bytes, prompt: str) -> Optional[dict]:
    """One-shot Opus converse over (prompt + image) → parsed JSON object.

    Uses the verified Opus-4.8 config: adaptive thinking, no temperature. The
    image block passes RAW bytes (boto3 base64-encodes on the wire; pre-
    encoding double-encodes and Bedrock rejects it) — same shape as
    pdf_multimodal._build_page_message. Returns None on any failure."""
    try:
        kwargs = {
            "modelId": DIAGRAM_MODEL_ID,
            "messages": [{"role": "user", "content": [
                {"text": prompt},
                {"image": {"format": "png", "source": {"bytes": image_bytes}}},
            ]}],
            "inferenceConfig": {"maxTokens": _MAX_TOKENS},
        }
        # Opus 4.8 extended thinking: adaptive, no budget. Guarded — if the
        # model id is swapped to one that wants a different shape, the field
        # is additive and a ValidationException just falls through to None.
        if is_opus(DIAGRAM_MODEL_ID):
            kwargs["additionalModelRequestFields"] = {"thinking": {"type": "adaptive"}}
        resp = client.converse(**kwargs)
        blocks = resp.get("output", {}).get("message", {}).get("content", [])
        raw = "".join(b.get("text", "") for b in blocks if "text" in b)
        return _parse_first_json_object(raw)
    except Exception as exc:  # noqa: BLE001 — non-fatal, degrade to None
        logger.warning(f"topology converse failed ({DIAGRAM_MODEL_ID}): {exc}")
        return None


_EXTRACT_PROMPT = """You are an infrastructure architect reading ONE architecture
diagram from a System Design or HA-standards document. Extract its topology as a
structured component graph — the HA-relevant structure the surrounding text does
NOT state (redundancy, failover, replication, DR, load balancing).

Return JSON ONLY (no prose, no fences):
{
  "nodes": [{"id": "<short_id>", "label": "<as drawn>", "type": "<server|database|load_balancer|datacenter|network|storage|service|other>"}],
  "edges": [{"src": "<node id>", "dst": "<node id>", "kind": "<replication|failover|load_balance|network|dependency|data_flow>"}],
  "ha_attributes": {
    "redundancy": "<e.g. active-active, active-passive, N+1, none, or null>",
    "failover": "<automatic|manual|none|null>",
    "replication": "<synchronous|asynchronous|none|null>",
    "dr": "<separate DR site present? yes|no|null>",
    "rto": "<value if stated, else null>",
    "rpo": "<value if stated, else null>"
  },
  "notes": "<one sentence on the dominant HA pattern, or what is ambiguous>"
}

Only describe what is actually drawn. If the image is not an architecture
diagram (a title slide, a table, a photo), return {"nodes": [], "edges": [],
"ha_attributes": {}, "notes": "not an architecture diagram"}."""


def extract_topology(image_bytes: bytes, client=None) -> Optional[dict]:
    """First reading: Opus extracts the component graph from the diagram.
    Returns the graph dict, or None on failure."""
    if not image_bytes:
        return None
    client = client or _make_bedrock_client()
    graph = _converse_image_json(client, image_bytes, _EXTRACT_PROMPT)
    if graph is None:
        return None
    # Normalise shape so downstream code can rely on the keys existing.
    graph.setdefault("nodes", [])
    graph.setdefault("edges", [])
    graph.setdefault("ha_attributes", {})
    graph.setdefault("notes", "")
    return graph


def _verify_prompt(graph: dict) -> str:
    return (
        "You are auditing a claimed reading of an architecture diagram against "
        "the ACTUAL image. Below is a component graph a previous pass extracted. "
        "Look at the image yourself and find every place the graph is WRONG, "
        "MISSING a component/connection that is clearly drawn, or HALLUCINATED "
        "something that is not there. Be strict: a single missed redundancy or a "
        "reversed/invented arrow can flip an HA judgment.\n\n"
        "Claimed graph:\n" + json.dumps({
            "nodes": graph.get("nodes", []),
            "edges": graph.get("edges", []),
            "ha_attributes": graph.get("ha_attributes", {}),
        }, indent=1)[:4000] + "\n\n"
        "Return JSON ONLY:\n"
        '{"faithful": true|false, '
        '"confidence": "high"|"medium"|"low", '
        '"discrepancies": ["<specific wrong/missing/hallucinated item>", ...]}\n'
        "Rules for confidence: 'high' only if the graph matches the image with "
        "no material errors; 'medium' if minor issues that don't change the HA "
        "topology; 'low' if any redundancy/failover/replication/DR edge or node "
        "is wrong, missing, or invented."
    )


def verify_topology(image_bytes: bytes, graph: dict, client=None) -> dict:
    """Second, adversarial reading: is the claimed graph faithful to the image?

    Returns {"faithful": bool, "confidence": high|medium|low,
    "discrepancies": [...]}. On failure returns a conservative MEDIUM (we
    couldn't verify, so we neither trust it as high nor discard it as low)."""
    if not image_bytes or not graph:
        return {"faithful": False, "confidence": "low", "discrepancies": ["no image or graph to verify"]}
    client = client or _make_bedrock_client()
    parsed = _converse_image_json(client, image_bytes, _verify_prompt(graph))
    if parsed is None:
        return {"faithful": False, "confidence": "medium",
                "discrepancies": ["verification pass failed — confidence unknown"]}
    conf = str(parsed.get("confidence", "")).strip().lower()
    if conf not in _VALID_CONFIDENCE:
        # No/'invalid confidence → infer from faithful flag.
        conf = "high" if parsed.get("faithful") is True else "low"
    discrepancies = parsed.get("discrepancies") or []
    if not isinstance(discrepancies, list):
        discrepancies = [str(discrepancies)]
    return {
        "faithful": bool(parsed.get("faithful", conf == "high")),
        "confidence": conf,
        "discrepancies": [str(d) for d in discrepancies][:20],
    }


def analyze_diagram(image_bytes: bytes, client=None) -> Optional[DiagramAnalysis]:
    """Extract the topology graph and (optionally) self-consistency verify it.

    Returns a DiagramAnalysis, or None when extraction produced nothing usable
    (not a diagram, or extraction failed) so the caller can skip topology
    injection for that unit. Fully non-fatal."""
    client = client or _make_bedrock_client()
    graph = extract_topology(image_bytes, client=client)
    if not graph or (not graph.get("nodes") and not graph.get("edges")):
        return None  # nothing to reason about — treat as a non-diagram

    if not TOPOLOGY_VERIFY:
        return DiagramAnalysis(graph=graph, confidence="medium", verified=False)

    v = verify_topology(image_bytes, graph, client=client)
    return DiagramAnalysis(
        graph=graph,
        confidence=v["confidence"],
        discrepancies=v["discrepancies"],
        verified=v["confidence"] == "high",
    )
