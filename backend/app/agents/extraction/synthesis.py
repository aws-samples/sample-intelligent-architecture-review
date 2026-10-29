# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis — semantic dedup + cross-reference wiring over extracted rules.

The final extraction stage. Runs AFTER per-unit extraction, over the
full merged rule set, to fix the two cross-unit problems a per-unit pipeline
structurally can't:

  1. SEMANTIC DEDUP — the blunt dedup_rules key (name.lower, tiers, layer)
     misses cross-page restatements ("Tier 1 Web Layer Active-Active Mode" vs
     "Tier 1 – Web Layer – Active-Active Mode with 2N+1 Capacity"). Measured
     ~2x over-extraction on the rulebook (313 rules, ~120-150 distinct).
  2. CROSS-REFERENCE WIRING — substitution/exception rules whose founding
     parent lived in a different unit lose their parent_rule_id; we re-link
     them to the founding rule in the same (tier, layer) cell.

Design: hybrid, cost-bounded. A deterministic pre-bucket by (sorted tiers,
layer) does the cheap grouping; only buckets with >1 candidate go to an LLM
that decides which rules are the SAME requirement and picks the richest
wording to keep. Buckets of 1 never hit the model. Failure is non-fatal —
on any error we fall back to the un-deduped set (never lose rules).

NOTE ON SCOPE: this is the EXTRACTION-side synthesis (consolidate a standards
rulebook into a clean rule library). The holistic "does this design hold
together for HA" reasoning is a VALIDATION-side concern (understanding an SDD
as a system) and is out of scope here.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from app.bedrock_models import SONNET, model_for

logger = logging.getLogger(__name__)

SYNTH_MODEL_ID = model_for("EXTRACTION_SYNTH_MODEL_ID", SONNET)


def _parse_first_json_object(text: str) -> dict:
    """Extract the first valid JSON object from model output, tolerating
    ```json fences and trailing prose. Uses raw_decode so 'Extra data'
    after a complete object is ignored rather than fatal."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in model output")
    obj, _ = decoder.raw_decode(text[start:])
    return obj


def _bucket_key(rule: dict) -> tuple:
    tiers = rule.get("applies_to_tiers") or []
    try:
        tiers_key = tuple(sorted(int(t) for t in tiers))
    except (TypeError, ValueError):
        tiers_key = tuple()
    return (tiers_key, (rule.get("layer") or "general").strip().lower())


def _merge_bucket_llm(bedrock, bucket: list[dict]) -> list[dict]:
    """Ask the model to cluster same-requirement rules within one
    (tier, layer) bucket and return the surviving rules (richest wording),
    each tagged with the ids it absorbed. Deterministic fallback on error:
    return the bucket unchanged."""
    if len(bucket) <= 1:
        return bucket

    numbered = [
        {"n": i, "id": r["id"], "name": r.get("name", ""),
         "criteria": (r.get("validation_criteria") or "")[:200],
         "quote": (r.get("source_quote") or "")[:150]}
        for i, r in enumerate(bucket)
    ]
    prompt = (
        "These HA rules all target the SAME tier(s) and architectural layer. "
        "Some are the SAME requirement restated on different pages of the "
        "standards document (e.g. a detailed section and a summary matrix). "
        "Group rules that state the SAME underlying requirement. Two rules are "
        "the same ONLY if an auditor would check the exact same thing; "
        "different thresholds, modes (A-A vs A-P), or components are DIFFERENT "
        "rules — keep them separate. For each group, pick the index whose "
        "wording is most complete/specific to KEEP.\n\n"
        "Return JSON only: {\"groups\": [{\"keep\": <n>, \"absorb\": [<n>...]}]}\n"
        "Every input index must appear exactly once (as a keep or an absorb).\n\n"
        f"Rules:\n{json.dumps(numbered, indent=2)}"
    )
    try:
        resp = bedrock.converse(
            modelId=SYNTH_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 2000, "temperature": 0},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        groups = _parse_first_json_object(text)["groups"]
    except Exception as exc:
        logger.warning(f"synthesis bucket merge failed ({exc}); keeping bucket as-is")
        return bucket

    # Validate coverage — if the model dropped/duplicated indices, bail safe.
    covered: set[int] = set()
    kept: list[dict] = []
    ok = True
    for g in groups:
        keep_n = g.get("keep")
        absorb = g.get("absorb", []) or []
        if keep_n is None or keep_n >= len(bucket):
            ok = False
            break
        idxs = [keep_n, *[a for a in absorb if isinstance(a, int)]]
        if any(i in covered or i >= len(bucket) for i in idxs):
            ok = False
            break
        covered.update(idxs)
        keeper = dict(bucket[keep_n])
        absorbed_ids = [bucket[a]["id"] for a in absorb if a < len(bucket)]
        if absorbed_ids:
            keeper["absorbed_rule_ids"] = absorbed_ids
        kept.append(keeper)
    if not ok or covered != set(range(len(bucket))):
        logger.warning("synthesis merge coverage invalid; keeping bucket as-is")
        return bucket
    return kept


def dedupe_references(refs: list[dict], bedrock=None) -> list[str]:
    """Return the ids of references that restate another one.

    Several extraction units read the same document in parallel, and each
    saves the context it relies on: the tier table, the rule-type glossary, the
    notation legend. Rules get a dedup pass; references had none, so a short
    standard produced five copies of its notation legend. One model call groups
    references covering the same content and keeps the most complete of each.
    Never raises: on any failure nothing is dropped."""
    if len(refs) <= 1:
        return []
    if bedrock is None:
        from app.services.parallel_validator import _make_bedrock_client
        bedrock = _make_bedrock_client()
    numbered = [
        {"n": i, "name": r.get("name", ""), "body": (r.get("body") or "")[:300]}
        for i, r in enumerate(refs)
    ]
    prompt = (
        "These context references were extracted from ONE standards document by "
        "several readers working in parallel, so many restate the same "
        "reference (a tier table, a glossary of rule types, a diagram notation "
        "legend). Group references that convey the SAME content, even when "
        "named differently. References about different content stay separate. "
        "For each group, pick the index with the most complete body to KEEP.\n\n"
        "Return JSON only: {\"groups\": [{\"keep\": <n>, \"absorb\": [<n>...]}]}\n"
        "Every input index must appear exactly once (as a keep or an absorb).\n\n"
        f"References:\n{json.dumps(numbered, indent=2)}"
    )
    try:
        resp = bedrock.converse(
            modelId=SYNTH_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 1500, "temperature": 0},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        groups = _parse_first_json_object(text)["groups"]
    except Exception as exc:
        logger.warning(f"reference dedup failed ({exc}); keeping all references")
        return []

    covered: set[int] = set()
    dropped: list[str] = []
    for g in groups:
        keep = g.get("keep")
        absorb = [a for a in (g.get("absorb") or []) if isinstance(a, int)]
        idxs = [keep, *absorb]
        if keep is None or any(not isinstance(i, int) or i in covered or not 0 <= i < len(refs) for i in idxs):
            logger.warning("reference dedup coverage invalid; keeping all references")
            return []
        covered.update(idxs)
        dropped.extend(refs[a]["id"] for a in absorb)
    if covered != set(range(len(refs))):
        logger.warning("reference dedup coverage incomplete; keeping all references")
        return []
    return dropped


def semantic_dedupe(rules: list[dict], bedrock=None) -> tuple[list[dict], list[str]]:
    """Return (kept_rules, dropped_ids). Buckets by (tiers, layer); LLM-merges
    only multi-candidate buckets. Never raises — worst case returns input."""
    if not rules:
        return rules, []
    if bedrock is None:
        from app.services.parallel_validator import _make_bedrock_client
        bedrock = _make_bedrock_client()

    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for r in rules:
        buckets[_bucket_key(r)].append(r)

    kept: list[dict] = []
    dropped: list[str] = []
    multi = sum(1 for b in buckets.values() if len(b) > 1)
    logger.info(f"semantic_dedupe: {len(rules)} rules, {len(buckets)} buckets, "
                f"{multi} need LLM merge")

    for bucket in buckets.values():
        survivors = _merge_bucket_llm(bedrock, bucket)
        surviving_ids = {s["id"] for s in survivors}
        kept.extend(survivors)
        for r in bucket:
            if r["id"] not in surviving_ids:
                dropped.append(r["id"])
    return kept, dropped


def reason_cross_references(rules: list[dict], digest_block: str = "",
                            bedrock=None) -> dict:
    """LLM cross-reference reasoning over the FULL rule set.

    Goes beyond the deterministic (tier,layer) parent heuristic: given every
    extracted rule (compact form) + a document context digest (glossary,
    section map, tier defs), the model reasons about:
      - parent links: which substitution/exception rule attaches to which
        founding rule, INCLUDING cross-page cases the bucket heuristic can't
        see (e.g. a p.13 substitution for a p.11 founding rule);
      - gaps/contradictions: flags conflicting or missing rules.

    Returns {"parent_fixes": {rule_id: parent_id}, "flags": [str]}. Applies
    parent_fixes in place. Fully non-fatal: any error yields empty result and
    leaves the deterministic wiring intact.
    """
    subs = [r for r in rules if r.get("rule_type") in ("substitution", "exception")]
    if not subs:
        return {"parent_fixes": {}, "flags": []}
    if bedrock is None:
        from app.services.parallel_validator import _make_bedrock_client
        bedrock = _make_bedrock_client()

    founding = [r for r in rules if r.get("rule_type") == "founding"]
    def compact(r):
        return {"id": r["id"], "name": r.get("name", ""),
                "tiers": r.get("applies_to_tiers"), "layer": r.get("layer"),
                "type": r.get("rule_type"), "parent": r.get("parent_rule_id")}
    prompt = (
        "You are reconciling an extracted HA standards rule library. Each "
        "substitution/exception rule should link (parent_rule_id) to the "
        "FOUNDING rule it modifies — often stated on a DIFFERENT page, so a "
        "naive same-tier/same-layer match can be wrong or miss it.\n\n"
        "Using the founding rules and the document context, decide the correct "
        "parent for each substitution/exception rule. If none fits, leave it "
        "null. Also flag any contradictions or obvious gaps.\n\n"
        "Return JSON only:\n"
        '{"parent_fixes": {"<sub_or_exc_id>": "<founding_id_or_null>"}, '
        '"flags": ["<short note>"]}\n\n'
        f"DOCUMENT CONTEXT:\n{digest_block[:1500]}\n\n"
        f"FOUNDING RULES:\n{json.dumps([compact(r) for r in founding], indent=1)}\n\n"
        f"SUBSTITUTION/EXCEPTION RULES:\n{json.dumps([compact(r) for r in subs], indent=1)}"
    )
    try:
        resp = bedrock.converse(
            modelId=SYNTH_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 3000, "temperature": 0},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        result = _parse_first_json_object(text)
    except Exception as exc:
        logger.warning(f"cross-reference reasoning failed ({exc}); keeping heuristic links")
        return {"parent_fixes": {}, "flags": []}

    fixes = result.get("parent_fixes", {}) or {}
    ids = {r["id"] for r in rules}
    by_id = {r["id"]: r for r in rules}
    applied = 0
    for sub_id, parent_id in fixes.items():
        if sub_id in by_id and parent_id and parent_id in ids:
            by_id[sub_id]["parent_rule_id"] = parent_id
            applied += 1
    logger.info(f"cross-reference reasoning: applied {applied} parent fix(es), "
                f"{len(result.get('flags', []))} flag(s)")
    return {"parent_fixes": fixes, "flags": result.get("flags", []), "applied": applied}


# A requirement label as standards documents write it: "ACB-REQ-001",
# "HA-REQ-011", "SEC-12". Rule names start with the label of the requirement
# they were extracted from.
_LABEL = re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d+[a-z]?)\b")


def _label_of(rule: dict) -> str | None:
    match = _LABEL.match((rule.get("name") or "").strip())
    return match.group(1) if match else None


def _tiers(rule: dict) -> set[int]:
    try:
        return {int(t) for t in rule.get("applies_to_tiers") or []}
    except (TypeError, ValueError):
        return set()


def resolve_label_parents(rules: list[dict]) -> int:
    """Turn parents written as document labels into library rule ids.

    The extraction agent records a child's parent the way the document names
    it ("substitutes for ACB-REQ-001"), so parent_rule_id holds the label, not
    the id of the founding rule extracted from that requirement. Nothing else
    maps one to the other, so every such child read as an orphan and the
    decision tree never applied it.

    A label can name several founding rules when a requirement was split per
    tier; prefer the one whose tiers overlap the child's. Returns the number of
    rules re-linked. Mutates rules in place."""
    ids = {r["id"] for r in rules}
    by_label: dict[str, list[dict]] = defaultdict(list)
    for r in rules:
        label = _label_of(r)
        if label and r.get("rule_type") == "founding":
            by_label[label].append(r)

    resolved = 0
    for r in rules:
        if r.get("rule_type") not in ("substitution", "exception"):
            continue
        parent = (r.get("parent_rule_id") or "").strip()
        if not parent or parent in ids:
            continue
        candidates = [c for c in by_label.get(parent, []) if c["id"] != r["id"]]
        if not candidates:
            continue
        overlapping = [c for c in candidates if _tiers(c) & _tiers(r)]
        r["parent_rule_id"] = (overlapping or candidates)[0]["id"]
        resolved += 1
    return resolved


def wire_cross_references(rules: list[dict]) -> int:
    """Link substitution/exception rules to their founding rule. Returns the
    number of rules newly wired.

    First resolve parents written as document labels (resolve_label_parents).
    Any rule still orphaned (no parent_rule_id, or one pointing at an id not in
    the set) is matched to the founding rule in the SAME (tier, layer) cell.
    Mutates rules in place."""
    wired = resolve_label_parents(rules)
    by_bucket_founding: dict[tuple, str] = {}
    ids = {r["id"] for r in rules}
    for r in rules:
        if r.get("rule_type") == "founding":
            by_bucket_founding.setdefault(_bucket_key(r), r["id"])

    for r in rules:
        if r.get("rule_type") not in ("substitution", "exception"):
            continue
        parent = r.get("parent_rule_id")
        if parent and parent in ids:
            continue  # already correctly linked
        founding = by_bucket_founding.get(_bucket_key(r))
        if founding:
            r["parent_rule_id"] = founding
            wired += 1
    return wired
