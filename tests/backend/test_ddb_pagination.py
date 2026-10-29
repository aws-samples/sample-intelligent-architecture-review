# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Scans must follow LastEvaluatedKey.

DynamoDB caps a scan response at 1MB and signals the cut-off only through
LastEvaluatedKey — no exception, no marker on the items. An unpaginated scan
therefore returns a silent PREFIX of the table.

That is not a capacity concern here, it is a correctness one:

  * list_standards feeds the validator, so a truncated rule set means rules stop
    being enforced and a document is reported compliant against a rulebook that
    was quietly cut short.
  * A FilterExpression is applied AFTER each page is read, so a filtered scan can
    return zero matches purely because the match sat on a later page — which
    turned the validation cache into a false-miss generator.

These tests use a fake table that ALWAYS pages, so any store that forgets to
paginate sees only the first page and fails.
"""

from __future__ import annotations

import pytest

from app.services.ddb import MAX_SCAN_PAGES, scan_all


class PagingTable:
    """A boto3-Table stand-in that hands out one item per scan call.

    Mirrors the real contract: `LastEvaluatedKey` is present on every response
    except the last, and `ExclusiveStartKey` resumes where the caller left off.
    """

    name = "fake-table"

    def __init__(self, items: list[dict]):
        self.all_items = items
        self.scan_calls = 0

    def scan(self, **kwargs):
        self.scan_calls += 1
        start = 0
        if "ExclusiveStartKey" in kwargs:
            start = int(kwargs["ExclusiveStartKey"]["id"]) + 1

        # Honour a FilterExpression only in the crude sense the tests need:
        # server-side filtering happens per page, which is the point being tested.
        page = self.all_items[start:start + 1]
        resp = {"Items": page}
        if start + 1 < len(self.all_items):
            resp["LastEvaluatedKey"] = {"id": str(start)}
        return resp


def _rows(n: int) -> list[dict]:
    return [{"id": str(i), "name": f"row-{i}"} for i in range(n)]


class TestScanAll:
    def test_returns_every_page_not_just_the_first(self):
        table = PagingTable(_rows(25))
        got = scan_all(table)
        assert len(got) == 25, "pagination dropped rows"
        assert [r["id"] for r in got] == [str(i) for i in range(25)]
        assert table.scan_calls == 25

    def test_single_page_table_scans_once(self):
        table = PagingTable(_rows(1))
        assert len(scan_all(table)) == 1
        assert table.scan_calls == 1

    def test_empty_table(self):
        table = PagingTable([])
        assert scan_all(table) == []

    def test_passes_scan_kwargs_through(self):
        """Callers rely on FilterExpression/ProjectionExpression reaching boto3."""
        seen: list[dict] = []

        class RecordingTable(PagingTable):
            def scan(self, **kwargs):
                seen.append(kwargs)
                return super().scan(**kwargs)

        scan_all(RecordingTable(_rows(3)), FilterExpression="a = :b",
                 ExpressionAttributeValues={":b": "c"})
        assert all(k["FilterExpression"] == "a = :b" for k in seen)
        # The resume key is added by scan_all, never by the caller.
        assert "ExclusiveStartKey" not in seen[0]
        assert seen[1]["ExclusiveStartKey"] == {"id": "0"}

    def test_stops_at_the_page_ceiling_and_says_so(self, caplog):
        """A runaway scan is bounded — but must log, because silence is what made
        the original bug invisible."""
        table = PagingTable(_rows(MAX_SCAN_PAGES + 10))
        with caplog.at_level("ERROR"):
            got = scan_all(table)
        assert len(got) == MAX_SCAN_PAGES
        assert any("INCOMPLETE" in r.message for r in caplog.records)

    def test_respects_an_explicit_lower_page_limit(self):
        table = PagingTable(_rows(20))
        assert len(scan_all(table, page_limit=3)) == 3

    def test_errors_propagate(self):
        """Callers wrap store access in their own ClientError handling and decide
        how to degrade; swallowing here would recreate silent truncation."""
        class Boom:
            name = "boom"

            def scan(self, **kwargs):
                raise RuntimeError("ddb down")

        with pytest.raises(RuntimeError):
            scan_all(Boom())


class TestStoresPaginate:
    """Each store's list path must walk every page.

    The fake table pages after ONE item, so a store that reads only
    `response["Items"]` sees a single row and these fail.
    """

    def test_list_standards_sees_every_rule(self, monkeypatch):
        """The most important one: this list is what the validator evaluates."""
        from app.services.validation_store import validation_store

        rules = [
            {"id": f"HA-{i:03d}", "name": f"rule {i}", "status": "active",
             "hosting_scope": "both"}
            for i in range(30)
        ]
        table = PagingTable(rules)
        monkeypatch.setattr(validation_store, "_available", True)
        monkeypatch.setattr(
            validation_store, "_client",
            type("C", (), {"Table": staticmethod(lambda _n: table)})(),
        )
        got = validation_store.list_standards()
        assert len(got) == 30, (
            "list_standards truncated the rulebook — the validator would score a "
            "document against a partial rule set"
        )

    def test_list_all_references_sees_every_row(self, monkeypatch):
        from app.services.references_store import references_store

        rows = [
            {"id": f"HA-R-{i}", "name": f"ref {i}", "body": "b",
             "status": "active", "hosting_scope": "both", "applies_to_tiers": [1]}
            for i in range(12)
        ]
        # This store talks to DDB unconditionally (no in-memory fallback), so
        # swapping the table is enough.
        monkeypatch.setattr(references_store, "_table", PagingTable(rows))
        assert len(references_store.list_all()) == 12

    def test_list_all_user_roles_sees_every_row(self, monkeypatch):
        from app.services.user_roles_store import user_roles_store

        rows = [{"id": str(i), "sub": f"s{i}", "role": "viewer"} for i in range(9)]
        monkeypatch.setattr(user_roles_store, "_available", True)
        monkeypatch.setattr(user_roles_store, "_table", PagingTable(rows))
        assert len(user_roles_store.list_all()) == 9

    def test_icon_legend_list_sees_every_row(self, monkeypatch):
        """Runs on the diagram-prompt path: a dropped row is an approved icon
        that silently stops being injected."""
        from app.services.icon_legend_store import icon_legend_store

        rows = [
            {"id": f"ICON-{i}", "record_type": "icon", "label": f"g{i}",
             "icon_type": "other", "status": "active", "hosting_scope": "both",
             "created_at": f"2026-01-{i + 1:02d}"}
            for i in range(11)
        ]
        monkeypatch.setattr(icon_legend_store, "_available", True)
        monkeypatch.setattr(icon_legend_store, "_table", PagingTable(rows))
        assert len(icon_legend_store.list_all(status="active")) == 11
