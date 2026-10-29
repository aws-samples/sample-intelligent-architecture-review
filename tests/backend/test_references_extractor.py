# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Parsing the `## References` section out of a document's extraction notes.

This module was at 0% coverage. It matters because a Reference body is injected
verbatim into every matching validation prompt: a mis-parsed metadata line
silently changes which validations a factual block reaches, and a swallowed
heading means the block never reaches any of them.

All parsing is pure string work. The persistence test replaces the DDB store,
which the module imports lazily for exactly that reason.
"""

from __future__ import annotations

import pytest

from app.services import references_extractor as rx

# The shape the docstring documents, used as the happy path.
NOTES = """\
## Extraction guidance

Prefer the exact wording from the source table.

## References

### DC peer table (v2 doc page 9)
[tier: 1,2; layer: storage; scope: on_prem]
| Primary | Secondary | DR |
|---------|-----------|----|
| VA      | TX        | CTOS-1 |

### Sanctioned A-A-DR triples
[scope: both]
The list of pairings approved by the Architecture Council for 2026.
"""


class TestSliceSection:
    def test_returns_none_without_a_references_heading(self):
        assert rx._slice_references_section("## Rules\nsome guidance") is None

    def test_stops_at_the_next_h2(self):
        notes = "## References\n### A\nbody\n\n## Appendix\nnot a reference"
        section = rx._slice_references_section(notes)
        assert "body" in section
        assert "not a reference" not in section

    def test_h3_headings_do_not_terminate_the_section(self):
        """### is the per-reference delimiter; treating it as a terminator would
        drop every reference after the first."""
        section = rx._slice_references_section(NOTES)
        assert "DC peer table" in section
        assert "Sanctioned A-A-DR triples" in section

    def test_runs_to_end_of_text_when_no_following_heading(self):
        section = rx._slice_references_section("## References\n### A\nlast thing")
        assert "last thing" in section


class TestParseMetadataLine:
    def test_parses_all_three_keys(self):
        got = rx._parse_metadata_line("[tier: 1,2; layer: storage; scope: on_prem]")
        assert got == {
            "applies_to_tiers": [1, 2],
            "layer": "storage",
            "hosting_scope": "on_prem",
        }

    def test_non_metadata_line_returns_none(self):
        """None means "this is body text", not "empty metadata" — the caller
        uses the distinction to decide whether to consume the line."""
        assert rx._parse_metadata_line("| Primary | Secondary |") is None
        assert rx._parse_metadata_line("plain prose") is None

    def test_brackets_must_wrap_the_whole_line(self):
        assert rx._parse_metadata_line("prefix [tier: 1]") is None

    @pytest.mark.parametrize("key", ["tier", "tiers", "applies_to_tiers"])
    def test_tier_key_aliases(self, key):
        assert rx._parse_metadata_line(f"[{key}: 3]")["applies_to_tiers"] == [3]

    @pytest.mark.parametrize("key", ["scope", "hosting_scope"])
    def test_scope_key_aliases(self, key):
        assert rx._parse_metadata_line(f"[{key}: cloud]")["hosting_scope"] == "cloud"

    def test_tiers_are_deduped_and_sorted(self):
        got = rx._parse_metadata_line("[tier: 3,1,3,2]")
        assert got["applies_to_tiers"] == [1, 2, 3]

    def test_out_of_range_tiers_are_dropped_entirely(self):
        """Falling back to the default (all tiers) is safer than trusting a
        partially-valid list that would silently narrow scope."""
        assert "applies_to_tiers" not in rx._parse_metadata_line("[tier: 0,9]")

    def test_non_numeric_tiers_are_ignored(self):
        assert "applies_to_tiers" not in rx._parse_metadata_line("[tier: high]")

    def test_unknown_layer_is_ignored(self):
        assert "layer" not in rx._parse_metadata_line("[layer: teleportation]")

    def test_unknown_scope_is_ignored(self):
        assert "hosting_scope" not in rx._parse_metadata_line("[scope: mars]")

    def test_values_are_case_insensitive(self):
        got = rx._parse_metadata_line("[layer: STORAGE; scope: Both]")
        assert got["layer"] == "storage"
        assert got["hosting_scope"] == "both"

    def test_general_is_a_valid_layer(self):
        assert rx._parse_metadata_line("[layer: general]")["layer"] == "general"

    def test_unknown_keys_are_ignored_not_fatal(self):
        got = rx._parse_metadata_line("[colour: blue; layer: network]")
        assert got == {"layer": "network"}

    def test_parts_without_a_colon_are_skipped(self):
        assert rx._parse_metadata_line("[garbage; layer: data]") == {"layer": "data"}

    def test_empty_brackets_yield_empty_dict(self):
        """Empty dict, not None: the line IS metadata, it just sets nothing, so
        the caller should still consume it rather than treat it as body."""
        assert rx._parse_metadata_line("[]") == {}


class TestParseReferences:
    def test_parses_each_h3_into_one_reference(self):
        refs = rx.parse_references(NOTES)
        assert [r["name"] for r in refs] == [
            "DC peer table (v2 doc page 9)",
            "Sanctioned A-A-DR triples",
        ]

    def test_metadata_line_is_applied_and_removed_from_the_body(self):
        first = rx.parse_references(NOTES)[0]
        assert first["applies_to_tiers"] == [1, 2]
        assert first["layer"] == "storage"
        assert first["hosting_scope"] == "on_prem"
        assert "[tier:" not in first["body"]
        assert first["body"].startswith("| Primary")

    def test_body_keeps_its_markdown_table(self):
        """The body is injected into the prompt verbatim, so table structure has
        to survive parsing."""
        body = rx.parse_references(NOTES)[0]["body"]
        assert "CTOS-1" in body
        assert body.count("\n") >= 2

    def test_defaults_when_no_metadata_line(self):
        refs = rx.parse_references("## References\n### Plain\njust prose")
        assert refs[0]["applies_to_tiers"] == [1, 2, 3, 4, 5]
        assert refs[0]["layer"] is None
        assert refs[0]["hosting_scope"] == "on_prem"

    def test_partial_metadata_keeps_defaults_for_the_rest(self):
        refs = rx.parse_references("## References\n### P\n[layer: network]\nbody")
        assert refs[0]["layer"] == "network"
        assert refs[0]["applies_to_tiers"] == [1, 2, 3, 4, 5]
        assert refs[0]["hosting_scope"] == "on_prem"

    def test_default_tiers_are_not_shared_between_references(self):
        """A mutable default leaking across references would let an edit to one
        reference's tier list silently change another's."""
        refs = rx.parse_references(
            "## References\n### A\nbody a\n\n### B\nbody b"
        )
        refs[0]["applies_to_tiers"].append(99)
        assert refs[1]["applies_to_tiers"] == [1, 2, 3, 4, 5]
        assert rx._DEFAULT_TIERS == [1, 2, 3, 4, 5]

    def test_heading_with_no_body_is_skipped(self):
        refs = rx.parse_references("## References\n### Empty\n\n### Real\nhas body")
        assert [r["name"] for r in refs] == ["Real"]

    def test_metadata_only_reference_is_skipped(self):
        """Metadata with no body carries no factual content to inject."""
        assert rx.parse_references("## References\n### M\n[layer: data]") == []

    @pytest.mark.parametrize("notes", ["", "   ", "\n\n"])
    def test_blank_input_yields_nothing(self, notes):
        assert rx.parse_references(notes) == []

    def test_no_references_section_yields_nothing(self):
        assert rx.parse_references("## Guidance\nsome rules here") == []

    def test_empty_references_section_yields_nothing(self):
        assert rx.parse_references("## References\n\n## Next\nbody") == []

    def test_section_without_h3_headings_yields_nothing(self):
        assert rx.parse_references("## References\nloose prose, no heading") == []

    def test_heading_whitespace_is_trimmed(self):
        refs = rx.parse_references("## References\n###    Padded name   \nbody")
        assert refs[0]["name"] == "Padded name"

    def test_text_after_the_section_is_not_swallowed(self):
        notes = "## References\n### A\nref body\n\n## Other\nother body"
        refs = rx.parse_references(notes)
        assert len(refs) == 1
        assert "other body" not in refs[0]["body"]


# ---------------------------------------------------------------------------
# Persistence — idempotent on re-extraction
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, existing=None):
        self.rows = list(existing or [])
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.fail_on: set[str] = set()

    def list_all(self, status=None):
        return list(self.rows)

    def create(self, **kw):
        if kw.get("name") in self.fail_on:
            raise RuntimeError("DDB write failed")
        self.created.append(kw)
        return {"id": f"ref-{len(self.created)}", **kw}

    def update(self, ref_id, fields):
        self.updated.append((ref_id, fields))
        return {"id": ref_id, **fields}


@pytest.fixture
def store(monkeypatch):
    """Swap the lazily-imported references_store for a recorder."""
    import app.services.references_store as mod

    fake = _FakeStore()
    monkeypatch.setattr(mod, "references_store", fake)
    return fake


class TestSaveReferencesForDocument:
    def test_creates_one_reference_per_parsed_block(self, store):
        summary = rx.save_references_for_document(
            document_id="doc-1", extraction_notes=NOTES
        )
        assert summary == {"created": 2, "updated": 0, "skipped": 0}
        assert len(store.created) == 2
        assert store.created[0]["source_document"] == "doc-1"
        assert store.created[0]["status"] == "active"

    def test_reextraction_updates_in_place_rather_than_duplicating(self, store):
        store.rows = [
            {"id": "ref-a", "name": "DC peer table (v2 doc page 9)",
             "source_document": "doc-1"},
        ]
        summary = rx.save_references_for_document(
            document_id="doc-1", extraction_notes=NOTES
        )
        assert summary["updated"] == 1
        assert summary["created"] == 1
        assert store.updated[0][0] == "ref-a"
        assert "body" in store.updated[0][1]

    def test_matching_is_scoped_to_this_document(self, store):
        """A same-named reference from another document must not be hijacked."""
        store.rows = [
            {"id": "ref-other", "name": "DC peer table (v2 doc page 9)",
             "source_document": "doc-OTHER"},
        ]
        summary = rx.save_references_for_document(
            document_id="doc-1", extraction_notes=NOTES
        )
        assert summary["created"] == 2
        assert store.updated == []

    def test_a_failed_write_is_counted_not_raised(self, store):
        """Reference persistence is explicitly non-fatal — the rest of the
        extraction pipeline must not die because one block failed to save."""
        store.fail_on = {"Sanctioned A-A-DR triples"}
        summary = rx.save_references_for_document(
            document_id="doc-1", extraction_notes=NOTES
        )
        assert summary == {"created": 1, "updated": 0, "skipped": 1}

    @pytest.mark.parametrize("notes", [None, "", "## Guidance\nno refs here"])
    def test_nothing_to_save_touches_no_store(self, notes):
        # No store fixture: proves the DDB import is never reached, which is why
        # the module defers it.
        assert rx.save_references_for_document(
            document_id="doc-1", extraction_notes=notes
        ) == {"created": 0, "updated": 0, "skipped": 0}

    def test_metadata_is_passed_through_to_the_store(self, store):
        rx.save_references_for_document(document_id="doc-1", extraction_notes=NOTES)
        first = store.created[0]
        assert first["applies_to_tiers"] == [1, 2]
        assert first["layer"] == "storage"
        assert first["hosting_scope"] == "on_prem"
