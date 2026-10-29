# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Model-output hardening: extraction synthesis and the AI rule curator.

Everything here parses or normalises text an LLM produced, which means every
function is a place where a malformed reply either crashes extraction or, worse,
silently produces a rule the user never intended. The tests pin the tolerant
paths (fences, trailing prose, wrong types) and the fail-closed ones (drop a
suggestion missing a required field rather than persisting a half-rule).

wire_cross_references is the decision-tree repair step: a substitution rule with
no parent is inert, so re-linking orphans is what makes "pass via substitution"
reachable at all.
"""

from __future__ import annotations

import json

import pytest

from app.agents.extraction import synthesis as syn
from app.services import standards_curator as sc


class TestParseFirstJsonObject:
    def test_parses_a_bare_object(self):
        assert syn._parse_first_json_object('{"a": 1}') == {"a": 1}

    def test_ignores_a_leading_fence_and_prose(self):
        raw = 'Sure thing!\n```json\n{"a": 2}\n```'
        assert syn._parse_first_json_object(raw) == {"a": 2}

    def test_trailing_data_after_the_object_is_not_fatal(self):
        """raw_decode is used precisely so "Extra data" does not abort a run
        whose object was already complete."""
        assert syn._parse_first_json_object('{"a": 3} and then some chatter') == {"a": 3}

    def test_nested_objects_survive(self):
        got = syn._parse_first_json_object('{"rules": [{"id": "r1"}]}')
        assert got["rules"][0]["id"] == "r1"

    def test_no_object_raises(self):
        with pytest.raises(ValueError):
            syn._parse_first_json_object("no braces here")

    def test_malformed_json_raises(self):
        with pytest.raises(Exception):
            syn._parse_first_json_object('{"a": }')


class TestBucketKey:
    def test_groups_by_tiers_and_layer(self):
        a = {"applies_to_tiers": [1, 2], "layer": "storage"}
        b = {"applies_to_tiers": [2, 1], "layer": "storage"}
        assert syn._bucket_key(a) == syn._bucket_key(b), "tier order must not matter"

    def test_layer_is_case_insensitive(self):
        assert syn._bucket_key({"layer": "Storage"}) == syn._bucket_key({"layer": "storage"})

    def test_layer_defaults_to_general(self):
        assert syn._bucket_key({}) == syn._bucket_key({"layer": "general"})

    def test_a_null_layer_defaults_to_general(self):
        assert syn._bucket_key({"layer": None}) == syn._bucket_key({"layer": "general"})

    def test_different_layers_are_different_buckets(self):
        assert syn._bucket_key({"layer": "storage"}) != syn._bucket_key({"layer": "network"})

    def test_different_tiers_are_different_buckets(self):
        assert syn._bucket_key({"applies_to_tiers": [1]}) != syn._bucket_key(
            {"applies_to_tiers": [2]}
        )

    def test_unparseable_tiers_fall_back_to_empty(self):
        """A crash here would abort dedupe for the whole extraction batch."""
        assert syn._bucket_key({"applies_to_tiers": ["one", "two"]}) == (
            tuple(),
            "general",
        )

    def test_tiers_are_normalised_from_strings(self):
        assert syn._bucket_key({"applies_to_tiers": ["1", "2"]}) == syn._bucket_key(
            {"applies_to_tiers": [1, 2]}
        )


def _rule(rid, rule_type="founding", layer="storage", tiers=(1,), parent=None) -> dict:
    r = {
        "id": rid,
        "name": f"Rule {rid}",
        "rule_type": rule_type,
        "layer": layer,
        "applies_to_tiers": list(tiers),
    }
    if parent is not None:
        r["parent_rule_id"] = parent
    return r


class TestWireCrossReferences:
    def test_links_an_orphan_to_the_founding_rule_in_its_cell(self):
        """An unparented substitution rule is inert — it can never rescue a
        failing founding rule, so "pass via substitution" never fires."""
        rules = [_rule("f1"), _rule("s1", "substitution")]
        assert syn.wire_cross_references(rules) == 1
        assert rules[1]["parent_rule_id"] == "f1"

    def test_links_exceptions_too(self):
        rules = [_rule("f1"), _rule("e1", "exception")]
        assert syn.wire_cross_references(rules) == 1
        assert rules[1]["parent_rule_id"] == "f1"

    def test_leaves_correctly_linked_rules_alone(self):
        rules = [_rule("f1"), _rule("s1", "substitution", parent="f1")]
        assert syn.wire_cross_references(rules) == 0
        assert rules[1]["parent_rule_id"] == "f1"

    def test_repairs_a_dangling_parent_reference(self):
        """A parent id that is not in the set is as broken as no parent."""
        rules = [_rule("f1"), _rule("s1", "substitution", parent="does-not-exist")]
        assert syn.wire_cross_references(rules) == 1
        assert rules[1]["parent_rule_id"] == "f1"

    def test_resolves_a_parent_written_as_the_documents_label(self):
        """The agent records a parent the way the document names it
        ("substitutes for ACB-REQ-001"), not by library id. Before this step
        every such child read as an orphan and the decision tree never ran."""
        founding = _rule("HA-D-AAA", layer="datacenter")
        founding["name"] = "ACB-REQ-001: Dual owned datacenter sites"
        child = _rule("HA-D-BBB", "substitution", layer="network", parent="ACB-REQ-001")
        child["name"] = "ACB-REQ-002: Colocation as the second site"
        rules = [founding, child]
        assert syn.wire_cross_references(rules) == 1
        assert child["parent_rule_id"] == "HA-D-AAA"

    def test_a_split_requirement_resolves_to_the_tier_that_matches(self):
        """One requirement extracted per tier gives several rules with the same
        label; a Tier 2 substitution belongs to the Tier 2 founding rule."""
        t1 = _rule("HA-D-T1", tiers=(1,))
        t1["name"] = "ACB-REQ-001 – Dual owned datacenter sites (Tier 1)"
        t2 = _rule("HA-D-T2", tiers=(2,))
        t2["name"] = "ACB-REQ-001: Dual owned datacenter sites (Tier 2)"
        child = _rule("HA-D-SUB", "substitution", tiers=(2,), parent="ACB-REQ-001")
        child["name"] = "ACB-REQ-002: Colocation as the second site"
        rules = [t1, t2, child]
        syn.wire_cross_references(rules)
        assert child["parent_rule_id"] == "HA-D-T2"

    def test_a_rule_is_never_its_own_parent(self):
        """A child whose own name carries the label it cites must not link to
        itself, which would make it a founding rule in disguise."""
        child = _rule("HA-D-SELF", "substitution", layer="compute", parent="HA-REQ-011")
        child["name"] = "HA-REQ-011: Active-Active Minimum Node Count"
        rules = [child]
        assert syn.resolve_label_parents(rules) == 0
        assert child["parent_rule_id"] == "HA-REQ-011"

    def test_does_not_cross_layers(self):
        """Wiring a storage substitution onto a network founding rule would make
        the decision tree assert something the source document never said."""
        rules = [_rule("f1", layer="network"), _rule("s1", "substitution", layer="storage")]
        assert syn.wire_cross_references(rules) == 0
        assert "parent_rule_id" not in rules[1]

    def test_does_not_cross_tiers(self):
        rules = [_rule("f1", tiers=(1,)), _rule("s1", "substitution", tiers=(3,))]
        assert syn.wire_cross_references(rules) == 0

    def test_founding_rules_are_never_reparented(self):
        rules = [_rule("f1"), _rule("f2")]
        assert syn.wire_cross_references(rules) == 0
        assert all("parent_rule_id" not in r for r in rules)

    def test_no_founding_rule_means_nothing_to_wire(self):
        rules = [_rule("s1", "substitution"), _rule("e1", "exception")]
        assert syn.wire_cross_references(rules) == 0

    def test_the_first_founding_rule_in_a_cell_wins(self):
        rules = [_rule("f1"), _rule("f2"), _rule("s1", "substitution")]
        syn.wire_cross_references(rules)
        assert rules[2]["parent_rule_id"] == "f1"

    def test_several_orphans_are_all_wired(self):
        rules = [
            _rule("f1"),
            _rule("s1", "substitution"),
            _rule("s2", "substitution"),
            _rule("e1", "exception"),
        ]
        assert syn.wire_cross_references(rules) == 3

    def test_an_empty_set_is_a_no_op(self):
        assert syn.wire_cross_references([]) == 0


class TestSemanticDedupe:
    def test_without_a_bedrock_client_nothing_is_merged(self):
        """Dedupe is LLM-driven; with no client it must pass rules through rather
        than drop them."""
        rules = [_rule("a"), _rule("b")]
        out, notes = syn.semantic_dedupe(rules, bedrock=None)
        assert {r["id"] for r in out} == {"a", "b"}

    def test_a_single_rule_needs_no_merging(self):
        out, _ = syn.semantic_dedupe([_rule("a")], bedrock=None)
        assert len(out) == 1

    def test_an_empty_list_is_handled(self):
        out, notes = syn.semantic_dedupe([], bedrock=None)
        assert out == []


# ---------------------------------------------------------------------------
# AI rule curator
# ---------------------------------------------------------------------------

class TestCoerceNewSuggestion:
    def _raw(self, **over):
        base = {
            "name": "Multi-AZ required",
            "description": "Services span two AZs.",
            "validation_criteria": "Confirm two AZs are named.",
            "severity": "high",
            "category": "availability",
            "rationale": "The doc mentions a single AZ.",
        }
        base.update(over)
        return base

    def test_accepts_a_complete_suggestion(self):
        got = sc._coerce_new_suggestion(self._raw())
        assert got["name"] == "Multi-AZ required"
        assert got["severity"] == "high"
        assert got["category"] == "availability"
        assert got["rationale"].startswith("The doc")

    @pytest.mark.parametrize(
        "field", ["name", "description", "validation_criteria"]
    )
    def test_a_missing_required_field_drops_the_suggestion(self, field):
        """Persisting a half-formed rule is worse than losing it: the rule
        library drives every future verdict."""
        assert sc._coerce_new_suggestion(self._raw(**{field: ""})) is None

    @pytest.mark.parametrize("field", ["name", "description", "validation_criteria"])
    def test_whitespace_only_counts_as_missing(self, field):
        assert sc._coerce_new_suggestion(self._raw(**{field: "   "})) is None

    def test_an_unknown_severity_falls_back_to_medium(self):
        assert sc._coerce_new_suggestion(self._raw(severity="catastrophic"))[
            "severity"
        ] == "medium"

    def test_severity_is_case_insensitive(self):
        assert sc._coerce_new_suggestion(self._raw(severity="HIGH"))["severity"] == "high"

    def test_a_missing_category_defaults_to_general(self):
        got = sc._coerce_new_suggestion(self._raw(category=""))
        assert got["category"] == "general"

    def test_fields_are_stripped(self):
        got = sc._coerce_new_suggestion(self._raw(name="  padded  "))
        assert got["name"] == "padded"

    @pytest.mark.parametrize("raw", [None, "a string", 42, [], True])
    def test_non_dict_input_is_rejected(self, raw):
        assert sc._coerce_new_suggestion(raw) is None

    def test_a_missing_rationale_becomes_empty_not_none(self):
        got = sc._coerce_new_suggestion(self._raw(rationale=""))
        assert got["rationale"] == ""


class TestExtractCuration:
    def test_parses_ids_and_suggestions(self):
        payload = {
            "existing_standard_ids": ["HA-1", "HA-2"],
            "new_suggestions": [
                {
                    "name": "n",
                    "description": "d",
                    "validation_criteria": "c",
                    "severity": "low",
                }
            ],
        }
        ids, new = sc._extract_curation(json.dumps(payload))
        assert ids == ["HA-1", "HA-2"]
        assert len(new) == 1 and new[0]["severity"] == "low"

    def test_tolerates_a_markdown_fence(self):
        ids, _ = sc._extract_curation(
            '```json\n{"existing_standard_ids": ["A"]}\n```'
        )
        assert ids == ["A"]

    def test_tolerates_surrounding_prose(self):
        ids, _ = sc._extract_curation(
            'Here are the most relevant rules:\n{"existing_standard_ids": ["A"]}\nHope this helps!'
        )
        assert ids == ["A"]

    @pytest.mark.parametrize("text", ["", None, "no json at all", "{unclosed"])
    def test_unparseable_output_yields_empty_results(self, text):
        """Curation is a convenience; returning nothing is correct, raising into
        the request is not."""
        assert sc._extract_curation(text) == ([], [])

    def test_a_json_array_at_the_top_level_yields_nothing(self):
        assert sc._extract_curation('{"not": "expected"}') == ([], [])

    def test_non_list_ids_are_ignored(self):
        ids, _ = sc._extract_curation('{"existing_standard_ids": "HA-1"}')
        assert ids == []

    def test_integer_ids_are_stringified(self):
        ids, _ = sc._extract_curation('{"existing_standard_ids": [1, 2]}')
        assert ids == ["1", "2"]

    def test_non_scalar_ids_are_dropped(self):
        ids, _ = sc._extract_curation(
            '{"existing_standard_ids": ["ok", {"nested": 1}, ["list"]]}'
        )
        assert ids == ["ok"]

    def test_non_list_suggestions_are_ignored(self):
        _, new = sc._extract_curation('{"new_suggestions": {"name": "x"}}')
        assert new == []

    def test_invalid_suggestions_are_filtered_out(self):
        payload = {
            "new_suggestions": [
                {"name": "keep", "description": "d", "validation_criteria": "c"},
                {"name": "", "description": "d", "validation_criteria": "c"},
                "not a dict",
            ]
        }
        _, new = sc._extract_curation(json.dumps(payload))
        assert len(new) == 1
        assert new[0]["name"] == "keep"

    def test_missing_keys_yield_empty_lists(self):
        assert sc._extract_curation("{}") == ([], [])

    def test_null_values_are_treated_as_empty(self):
        ids, new = sc._extract_curation(
            '{"existing_standard_ids": null, "new_suggestions": null}'
        )
        assert ids == [] and new == []


class TestSummarizeStandards:
    def test_lists_each_standard(self):
        out = sc._summarize_standards([
            {"id": "HA-1", "name": "Multi-AZ", "description": "d",
             "category": "availability", "severity": "high"},
            {"id": "HA-2", "name": "Backups", "description": "d",
             "category": "data", "severity": "medium"},
        ])
        assert "HA-1" in out and "HA-2" in out
        assert "Multi-AZ" in out and "Backups" in out

    def test_an_empty_library_summarises_without_crashing(self):
        assert isinstance(sc._summarize_standards([]), str)

    def test_missing_fields_do_not_crash(self):
        assert isinstance(sc._summarize_standards([{"id": "x"}]), str)


class _FakeBedrock:
    """Returns a canned grouping, or raises, in place of a model call."""

    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error

    def converse(self, **_):
        if self.error:
            raise self.error
        return {"output": {"message": {"content": [{"text": self.reply}]}}}


class TestDedupeReferences:
    REFS = [
        {"id": "r0", "name": "ACB-STD-04 Diagram Notation Legend", "body": "ACB-01 = Tier 1 node"},
        {"id": "r1", "name": "Diagram notation / icon legend", "body": "ACB-01 = Tier 1 node; ACB-02 = DB"},
        {"id": "r2", "name": "Service tier definitions", "body": "Tier 1: 15 min"},
    ]

    def test_drops_the_restatements_the_model_groups(self):
        """Parallel extraction units each save the context they rely on, so one
        document's notation legend arrived five times."""
        fake = _FakeBedrock('{"groups": [{"keep": 1, "absorb": [0]}, {"keep": 2, "absorb": []}]}')
        assert syn.dedupe_references(self.REFS, bedrock=fake) == ["r0"]

    def test_keeps_everything_when_the_grouping_does_not_cover_every_reference(self):
        fake = _FakeBedrock('{"groups": [{"keep": 1, "absorb": [0]}]}')
        assert syn.dedupe_references(self.REFS, bedrock=fake) == []

    def test_keeps_everything_when_the_model_call_fails(self):
        fake = _FakeBedrock(error=RuntimeError("throttled"))
        assert syn.dedupe_references(self.REFS, bedrock=fake) == []

    def test_a_single_reference_needs_no_call(self):
        assert syn.dedupe_references(self.REFS[:1], bedrock=_FakeBedrock(error=AssertionError())) == []
