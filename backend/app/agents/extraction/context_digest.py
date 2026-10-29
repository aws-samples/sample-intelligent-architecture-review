# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document-level context digest, used for cross-reference resolution.

Per-unit agents are context-isolated (that's what enables parallelism), so
they can't see a glossary on page 19 while extracting page 9, or resolve a
"see section 2.2.1" pointer. This module builds ONE cheap, document-level
digest from the ingested Units — glossary terms, a section/heading map, and
tier definitions — that can be:

  - prepended to every per-unit agent (shared definitional context), and
  - handed to the cross-reference reasoning agent (to resolve references).

Pure/deterministic (regex + heading heuristics over already-parsed text); no
LLM call, so it's ~free and adds no latency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.extraction.unit import Unit


@dataclass
class DocumentDigest:
    section_map: list[str] = field(default_factory=list)      # "2.2.1 Tier 1", ...
    glossary: dict[str, str] = field(default_factory=dict)    # term -> short def
    tier_definitions: dict[str, str] = field(default_factory=dict)  # "Tier 1" -> desc

    def to_prompt_block(self, max_chars: int = 2000) -> str:
        """Render a compact context block for injection into agent prompts."""
        parts: list[str] = []
        if self.section_map:
            parts.append("SECTION MAP:\n" + "\n".join(f"  {s}" for s in self.section_map[:40]))
        if self.tier_definitions:
            parts.append("TIER DEFINITIONS:\n" + "\n".join(
                f"  {k}: {v}" for k, v in self.tier_definitions.items()))
        if self.glossary:
            parts.append("GLOSSARY:\n" + "\n".join(
                f"  {k}: {v}" for k, v in list(self.glossary.items())[:30]))
        block = "\n\n".join(parts)
        return block[:max_chars]


# "2", "2.2", "2.2.1" style numbered headings, optionally with a title.
_SECTION_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\s+([A-Z][^\n]{2,60})", re.MULTILINE)
# Tier labels.
_TIER_RE = re.compile(r"\bTier\s+([1-5])\b")
# Glossary rows tend to look like "TERM ("ABBR")  Definition..." or "Term — def".
_GLOSSARY_HINT = re.compile(r"^([A-Z][A-Za-z /]{2,40}?)\s*\(?[\"“]?([A-Z]{2,6})?[\"”]?\)?\s*[—:\t]\s*(.{10,200})", re.MULTILINE)


def build_digest(units: list[Unit]) -> DocumentDigest:
    """Build a document digest from the ingested Units. Deterministic."""
    digest = DocumentDigest()
    seen_sections: set[str] = set()

    for u in units:
        text = u.text or ""

        # Section headings.
        for m in _SECTION_RE.finditer(text):
            num, title = m.group(1), m.group(2).strip()
            key = f"{num} {title}"
            if num not in seen_sections:
                seen_sections.add(num)
                digest.section_map.append(key)

        # Glossary-ish rows — only harvest from units whose text mentions
        # "glossary"/"definition"/"term" to avoid false positives elsewhere.
        low = text.lower()
        if any(w in low for w in ("glossary", "definition", "terms")):
            for m in _GLOSSARY_HINT.finditer(text):
                term = m.group(1).strip()
                abbr = (m.group(2) or "").strip()
                definition = m.group(3).strip()
                if len(term.split()) <= 5:
                    label = f"{term} ({abbr})" if abbr else term
                    digest.glossary.setdefault(label, definition[:200])

        # Tier definition sentences ("Tier 1 ... assigned to applications that...").
        for m in _TIER_RE.finditer(text):
            tier = f"Tier {m.group(1)}"
            if tier in digest.tier_definitions:
                continue
            # Grab the sentence containing this tier mention if it looks definitional.
            start = text.rfind(".", 0, m.start()) + 1
            end = text.find(".", m.end())
            sentence = text[start:end if end != -1 else m.end() + 120].strip()
            if any(w in sentence.lower() for w in
                   ("assigned to", "designed to", "typology", "uptime", "critical")):
                digest.tier_definitions[tier] = sentence[:200]

    # Sort section map by dotted number for readability.
    digest.section_map.sort(key=lambda s: [int(x) for x in s.split()[0].split(".")])
    return digest
