# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Standards management tools for Strands agents."""

from typing import Optional


from strands.tools import tool

# Structured-rule vocabularies — must stay byte-equal to the
# RuleLayer / RuleType Literals declared in app.models.standard.
# A unit test (test_standards_mode_b.py) asserts the equality so
# drift surfaces in CI, not in production.
_VALID_LAYERS = {"datacenter", "data", "web", "application", "load_balancer", "general"}
_VALID_RULE_TYPES = {"founding", "substitution", "exception"}
_VALID_HOSTING_SCOPES = {"on_prem", "cloud", "both"}


def _norm_enum(value: str, allowed: set[str]) -> Optional[str]:
    """Normalise a permissive agent-facing enum string to canonical form.

    The agent prompt may produce 'Web', 'WEB', 'load-balancer', or
    'load balancer' — all of which we accept as equivalent. Out-of-
    vocab values are dropped silently (returned as None) rather than
    raising, so a malformed agent call doesn't lose the whole rule.
    """
    if not value:
        return None
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    return cleaned if cleaned in allowed else None


@tool
def get_all_standards() -> str:
    """Retrieve all configured HA validation standards/rules that documents must comply with."""
    from app.services.validation_store import validation_store

    standards = validation_store.list_standards()
    if not standards:
        return "No standards configured."
    parts = []
    for s in standards:
        parts.append(
            f"[{s.get('id')}] {s.get('name')} (Severity: {s.get('severity')}, Category: {s.get('category')})\n"
            f"  Criteria: {s.get('validation_criteria')}"
        )
    return f"{len(standards)} standards found:\n\n" + "\n\n".join(parts)


_current_source_document: str = "uploaded_document"
# document_id for the in-flight extraction run. Used so
# save_standard can emit a "rule" entry into the live-log registry
# directly, instead of speculatively parsing Strands' event stream
# for the tool result.
_current_document_id: str = ""


def set_source_document(doc_id: str, filename: str):
    """Set the source document context for standards being extracted."""
    global _current_source_document, _current_document_id
    _current_source_document = f"{filename} ({doc_id})"
    _current_document_id = doc_id or ""


@tool
def save_standard(
    name: str,
    description: str,
    severity: str,
    category: str,
    validation_criteria: str,
    page_reference: str = "",
    applies_to_tiers: list = None,
    alternative_criteria: str = "",
    source_quote: str = "",
    rule_type: str = "",
    layer: str = "",
    parent_rule_id: str = "",
    hosting_scope: str = "",
) -> str:
    """Save a new HA validation standard extracted from a document.

    Each standard is added to the golden set — it does NOT replace
    existing standards.

    page_reference: The page (or page range, e.g. "p. 3" or "pp. 3-4")
    where the rule's source language lives. Helps users verify
    provenance.

    source_quote: A short *verbatim* excerpt from the source document
    — typically one sentence, max 500 chars. This is the evidence
    that justified the rule. Must be a direct quote (paraphrase is
    not acceptable), so users can compare the AI's interpretation
    against the literal text.

    applies_to_tiers: Service tiers (1..5) this rule applies to. If the
    source document organises requirements by tier (e.g. a 5-row
    matrix where each row is "Tier N"), pass the specific tier(s)
    so validation only runs this rule against SDDs of that tier.
    Omit or pass [1,2,3,4,5] for universal rules.

    alternative_criteria: Substitution-check / alternative architecture
    text. When the source document presents an alternative way to
    satisfy this rule (e.g. "DNS Round Robin instead of dedicated
    load balancer"), fold it into this field instead of creating a
    separate rule. Validation surfaces it as an alternate way the
    rule can pass.

    rule_type: One of 'founding' | 'substitution' | 'exception'.
    Decision-tree metadata. Empty string = unset (a flat rule with no
    layer). Out-of-vocab values are silently dropped. The
    extraction prompt does not yet populate this — leave blank.

    layer: One of 'datacenter' | 'data' | 'web' | 'application' |
    'load_balancer'. Architectural layer this rule constrains.
    Empty string = unset. Hyphens / spaces are normalised to
    underscores ('load-balancer' → 'load_balancer'). Out-of-vocab
    values are silently dropped. The extraction prompt does
    not yet populate this — leave blank.

    parent_rule_id: Optional id of the founding rule that this
    substitution or exception relates to. Free-form string; no FK
    check. The extraction prompt does not yet
    populate this — leave blank.

    hosting_scope: REQUIRED. One of 'on_prem' | 'cloud' | 'both'.
    The system prompt declares the document's hosting_scope at
    the top of every extraction run — pass that value verbatim on
    every save_standard call. If you omit it or pass a value
    outside the three-element vocabulary, the tool raises an
    error and the rule is NOT saved. on_prem = traditional
    datacenter workload; cloud = AWS/Azure/GCP; both = applies
    to either (security baselines, RTO/RPO, etc.).
    """
    import uuid
    from datetime import datetime, timezone

    from app.services.validation_store import validation_store

    # Normalise tiers — accept None / empty / mixed-int-string lists.
    tiers: list[int] = [1, 2, 3, 4, 5]
    if applies_to_tiers:
        try:
            tiers = sorted({int(t) for t in applies_to_tiers if 1 <= int(t) <= 5})
            if not tiers:
                tiers = [1, 2, 3, 4, 5]
        except (TypeError, ValueError):
            # Permissive: bad input falls back to universal so a
            # malformed agent call doesn't lose the rule entirely.
            tiers = [1, 2, 3, 4, 5]

    # Clamp to a sane length. The agent should be sending one
    # sentence; a runaway quote bloats the DDB row and is harder
    # to scan in the UI.
    quote = (source_quote or "").strip()
    if len(quote) > 500:
        quote = quote[:497] + "…"

    # Normalise the structured-rule kwargs (layer / rule_type /
    # parent_rule_id). Empty / out-of-vocab values are coerced to None
    # and then dropped from the persisted dict — OMIT WHEN EMPTY
    # contract — EXCEPT for layer, where the free-form extraction agent
    # typically leaves the kwarg blank. We default to "general" instead
    # of None so those rules land in a real bucket on the Standards
    # page rather than an unlabelled one.
    # User-created rules (POST /api/standards) go through the route
    # handler, NOT this tool, and the route deliberately leaves
    # layer at None — so the asymmetry only affects extraction.
    layer_norm = _norm_enum(layer, _VALID_LAYERS)
    if layer_norm is None:
        layer_norm = "general"
    rule_type_norm = _norm_enum(rule_type, _VALID_RULE_TYPES)
    parent_norm = (parent_rule_id or "").strip() or None

    # hosting_scope is REQUIRED. The system prompt tells
    # the model the document's scope; the model must pass it
    # verbatim. Hard-fail (raise ValueError) when the input is
    # invalid: Strands' @tool decorator catches ValueError and
    # yields a ToolResult with status="error" + the message text,
    # which the model sees as a tool error and can retry on.
    # Verified in strands/tools/decorator.py:429-437.
    hosting_scope_norm = _norm_enum(hosting_scope, _VALID_HOSTING_SCOPES)
    if hosting_scope_norm is None:
        raise ValueError(
            'hosting_scope is required and must be one of "on_prem", '
            '"cloud", or "both". The system prompt for this run '
            "declared the document scope; please pass that value "
            "verbatim on the next call."
        )

    standard = {
        "id": f"HA-D-{uuid.uuid4().hex[:8].upper()}",
        "name": name,
        "description": description,
        "severity": severity,
        "category": category,
        "validation_criteria": validation_criteria,
        "page_reference": page_reference,
        "source_quote": quote,
        "source": "document",
        "source_document": _current_source_document,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "applies_to_tiers": tiers,
    }
    if alternative_criteria:
        standard["alternative_criteria"] = alternative_criteria
    if rule_type_norm:
        standard["rule_type"] = rule_type_norm
    if layer_norm:
        standard["layer"] = layer_norm
    if parent_norm:
        standard["parent_rule_id"] = parent_norm
    # Required, never None at this point (raises above if
    # normalisation rejected the model's input).
    standard["hosting_scope"] = hosting_scope_norm

    # Text extraction never attaches images.
    # evidence_image_refs is set later (after text extraction) by
    # image_enrichment.enrich_images(), which patches each saved rule
    # via update_standard.

    validation_store.create_standard(standard)
    tiers_str = "all tiers" if len(tiers) == 5 else f"tier{'s' if len(tiers) > 1 else ''} {tiers}"
    result_text = f"Saved standard: [{standard['id']}] {name} ({tiers_str})"

    # Emit a "rule" entry into the live-log registry so the
    # frontend log panel sees this rule landing in real-time. Done
    # here instead of via Strands callback parsing because the tool
    # is the canonical source of truth for what was actually saved.
    if _current_document_id:
        try:
            from app.services.extraction_log import append as _log_append
            _log_append(_current_document_id, "rule", result_text)
        except Exception:
            # Live-log is best-effort — never let a logging failure
            # break the rule save.
            pass

    return result_text


@tool
def save_reference(
    name: str,
    body: str,
    applies_to_tiers: list,
    layer: str = "",
    hosting_scope: str = "both",
    page_reference: str = "",
    source_quote: str = "",
) -> str:
    """Save a Reference (factual context block, NOT a testable rule).

    Use this for tables, lists, peer mappings, or any
    context that supports rules but isn't itself something an SDD can
    pass or fail. Examples:

      - Datacenter peer / DR-site mapping tables (e.g. v2 doc page 9
        "VA + TX + CTOS-1 DR")
      - Network zone definitions (Internal Network / Front DMZ /
        Back DMZ definitions)
      - Approved replication-technology lists when used as factual
        catalogs (NOT as substitution rules)
      - Glossary terms / tier-definition tables

    DO NOT use save_reference for:
      - Substitution / exception RULES — those go through
        save_standard with rule_type="substitution" or
        rule_type="exception".
      - Anything an SDD must do or not do — those are rules.

    The validator pulls every Reference whose scope (tier, layer,
    hosting_scope) covers a given rule and prepends it to that rule's
    prompt under a REFERENCES header.

    Args:
      name: short descriptive title (max ~60 chars).
      body: the reference content. Markdown allowed and encouraged —
        tables in particular should be preserved as markdown tables.
      applies_to_tiers: list of tier ints (1..5). Empty / None = all
        tiers (universal Reference).
      layer: one of {datacenter, data, web, application,
        load_balancer, general}. Empty = applies across layers.
      hosting_scope: on_prem | cloud | both (defaults to universal).
      page_reference: page number / range string ("p.9", "pp.6-7").
      source_quote: optional verbatim snippet from the source doc.

    Returns: "Saved reference: [HA-R-XXX] <name>"
    """
    from app.services.references_store import references_store

    norm_layer = _norm_enum(layer, _VALID_LAYERS) or None
    norm_scope = _norm_enum(hosting_scope, _VALID_HOSTING_SCOPES) or "both"

    tiers: list[int] = []
    if applies_to_tiers:
        for t in applies_to_tiers:
            try:
                tiers.append(int(t))
            except (TypeError, ValueError):
                continue
    if not tiers:
        tiers = [1, 2, 3, 4, 5]  # universal

    item = references_store.create(
        name=name.strip(),
        body=body,
        applies_to_tiers=tiers,
        layer=norm_layer,
        hosting_scope=norm_scope,
        source_document=_current_source_document,
        page_reference=page_reference or None,
        source_quote=source_quote or None,
    )

    result_text = f"Saved reference: [{item['id']}] {item['name']}"
    if _current_document_id:
        try:
            from app.services.extraction_log import append as _log_append
            _log_append(_current_document_id, "rule", result_text)
        except Exception:
            pass
    return result_text
