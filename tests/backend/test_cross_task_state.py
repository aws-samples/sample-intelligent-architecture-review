# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""State that must survive crossing ECS tasks.

The API autoscales to several tasks, so a request routinely lands on a task that
isn't the one doing the work. Two signals were process-local and silently broke
because of it:

  * Validation cancel — POST /{id}/cancel set an in-memory flag on whichever task
    answered. If that wasn't the task running the job, no loop ever read it: the
    run finished and billed the full Bedrock fan-out while the UI said it stopped.
  * Extraction in-flight — the review gate asks "is this document's extraction
    still running?" from a live log held in one task's memory. Elsewhere it
    resolved to "no", so the gate FAILED OPEN and a curator could approve
    proposed rules while dedup and cross-reference wiring were still running over
    that same batch.

Both now read a persisted signal. These tests simulate two tasks by driving the
store directly (task A) and the module-level API (task B).
"""

from __future__ import annotations

import pytest


class FakeTable:
    """In-memory stand-in supporting the update/get patterns these paths use."""

    name = "fake"

    def __init__(self):
        self.items: dict[str, dict] = {}

    def _key(self, key):
        return key["id"]

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, **kw):  # noqa: N803

        item = self.items.setdefault(self._key(Key), {"id": self._key(Key)})
        expr = UpdateExpression.strip()
        values = ExpressionAttributeValues or {}
        if expr.upper().startswith("SET"):
            for assignment in expr[3:].split(","):
                name, _, placeholder = assignment.partition("=")
                item[name.strip()] = values[placeholder.strip()]
        elif expr.upper().startswith("REMOVE"):
            for name in expr[6:].split(","):
                item.pop(name.strip(), None)
        return {}

    def get_item(self, Key, **kw):  # noqa: N803
        found = self.items.get(self._key(Key))
        return {"Item": dict(found)} if found else {}

    def put_item(self, Item):  # noqa: N803
        self.items[Item["id"]] = Item
        return {}


@pytest.fixture
def store(monkeypatch):
    """The validation store backed by a fake DDB table (shared 'database')."""
    from app.services.validation_store import validation_store

    table = FakeTable()
    monkeypatch.setattr(validation_store, "_available", True)
    monkeypatch.setattr(
        validation_store, "_client",
        type("C", (), {"Table": staticmethod(lambda _n: table)})(),
    )
    monkeypatch.setattr(validation_store, "_documents", {})
    monkeypatch.setattr(validation_store, "_validations", {})
    return validation_store


class TestCancelCrossesTasks:
    def test_a_cancel_on_another_task_is_visible_to_the_runner(self, store, monkeypatch):
        """The whole bug: task B answers the cancel, task A is running the job."""
        import app.services.parallel_validator as pv

        # Task A (the runner) has no local knowledge of this cancellation.
        monkeypatch.setattr(pv, "_CANCELLED_VALIDATIONS", set())
        monkeypatch.setattr(pv, "_cancel_cache", {})

        # Task B records the cancel straight into the shared store.
        assert store.request_cancel("val-1") is True

        assert pv.is_cancel_requested("val-1") is True

    def test_an_uncancelled_run_is_not_reported_cancelled(self, store, monkeypatch):
        import app.services.parallel_validator as pv
        monkeypatch.setattr(pv, "_CANCELLED_VALIDATIONS", set())
        monkeypatch.setattr(pv, "_cancel_cache", {})
        assert pv.is_cancel_requested("val-never-touched") is False

    def test_request_cancel_reports_whether_it_persisted(self, store, monkeypatch):
        """The route relies on this to avoid telling the user a cancel landed when
        the write failed — otherwise they stop watching a job that keeps billing."""
        import app.services.parallel_validator as pv
        monkeypatch.setattr(pv, "_CANCELLED_VALIDATIONS", set())
        monkeypatch.setattr(pv, "_cancel_cache", {})
        assert pv.request_cancel("val-2") is True

        from botocore.exceptions import ClientError

        def boom(**kwargs):
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "UpdateItem")

        monkeypatch.setattr(
            store, "_client",
            type("C", (), {"Table": staticmethod(lambda _n: type("T", (), {
                "update_item": staticmethod(boom)})())})(),
        )
        assert pv.request_cancel("val-3") is False

    def test_a_read_failure_does_not_cancel_the_run(self, store, monkeypatch):
        """Fail closed: a DDB hiccup must not abort a run nobody cancelled."""
        import app.services.parallel_validator as pv
        from botocore.exceptions import ClientError

        monkeypatch.setattr(pv, "_CANCELLED_VALIDATIONS", set())
        monkeypatch.setattr(pv, "_cancel_cache", {})

        def boom(**kwargs):
            raise ClientError({"Error": {"Code": "Throttling"}}, "GetItem")

        monkeypatch.setattr(
            store, "_client",
            type("C", (), {"Table": staticmethod(lambda _n: type("T", (), {
                "get_item": staticmethod(boom)})())})(),
        )
        assert pv.is_cancel_requested("val-4") is False

    def test_the_local_flag_short_circuits_without_a_read(self, store, monkeypatch):
        """Same-task cancels must not pay for a round-trip on every checkpoint."""
        import app.services.parallel_validator as pv
        reads = {"n": 0}

        def counting_get(**kwargs):
            reads["n"] += 1
            return {}

        monkeypatch.setattr(pv, "_CANCELLED_VALIDATIONS", {"val-5"})
        monkeypatch.setattr(pv, "_cancel_cache", {})
        monkeypatch.setattr(
            store, "_client",
            type("C", (), {"Table": staticmethod(lambda _n: type("T", (), {
                "get_item": staticmethod(counting_get)})())})(),
        )
        assert pv.is_cancel_requested("val-5") is True
        assert reads["n"] == 0

    def test_reads_are_cached_so_per_rule_checkpoints_stay_cheap(self, store, monkeypatch):
        """The checkpoints run once per rule; an uncached read per rule would add
        hundreds of round-trips to a single run."""
        import app.services.parallel_validator as pv
        reads = {"n": 0}
        real_get = store._client.Table("x").get_item

        def counting_get(**kwargs):
            reads["n"] += 1
            return real_get(**kwargs)

        monkeypatch.setattr(pv, "_CANCELLED_VALIDATIONS", set())
        monkeypatch.setattr(pv, "_cancel_cache", {})
        monkeypatch.setattr(
            store, "_client",
            type("C", (), {"Table": staticmethod(lambda _n: type("T", (), {
                "get_item": staticmethod(counting_get)})())})(),
        )
        for _ in range(20):
            pv.is_cancel_requested("val-6")
        assert reads["n"] == 1, "cancel flag was read once per checkpoint"

    def test_clear_cancel_drops_local_state(self, store, monkeypatch):
        import app.services.parallel_validator as pv
        monkeypatch.setattr(pv, "_CANCELLED_VALIDATIONS", {"val-7"})
        monkeypatch.setattr(pv, "_cancel_cache", {"val-7": (0.0, True)})
        pv.clear_cancel("val-7")
        assert "val-7" not in pv._CANCELLED_VALIDATIONS
        assert "val-7" not in pv._cancel_cache


class TestExtractionGateCrossesTasks:
    def test_an_in_flight_run_is_visible_from_another_task(self, store):
        store.set_extraction_state("doc-1", "extracting")
        assert store.get_extraction_state("doc-1") == "extracting"

    def test_the_review_gate_blocks_mid_run_from_another_task(self, store, monkeypatch):
        """The gate's real job: refuse approval while dedup/cross-ref is pending.

        The live-log lookup is forced to miss, standing in for a task that never
        registered the job.
        """
        import app.api.routes.standards as std

        monkeypatch.setattr(
            "app.services.extraction_log.get_job", lambda _d: None, raising=False
        )
        store.set_extraction_state("doc-2", "enriching")
        rule = {"source_document": "policy.pdf (doc-2)"}
        assert std._extraction_in_flight_for(rule) is True

    def test_the_gate_opens_once_the_run_finishes(self, store, monkeypatch):
        import app.api.routes.standards as std

        monkeypatch.setattr(
            "app.services.extraction_log.get_job", lambda _d: None, raising=False
        )
        store.set_extraction_state("doc-3", "extracting")
        store.set_extraction_state("doc-3", None)  # terminal clears it
        assert std._extraction_in_flight_for({"source_document": "p.pdf (doc-3)"}) is False

    def test_a_stale_stamp_does_not_lock_rules_forever(self, store, monkeypatch):
        """A task killed by a deploy can't clear its own stamp. A permanently-shut
        gate would be worse than a briefly-open one, so an old stamp is ignored."""
        from datetime import datetime, timedelta
        import app.services.validation_store as vs

        store.set_extraction_state("doc-4", "extracting")
        table = store._client.Table("documents")
        old = (datetime.utcnow()
               - timedelta(seconds=vs.EXTRACTION_STATE_STALE_AFTER_SEC + 60))
        table.items["doc-4"]["extraction_state_at"] = old.isoformat()

        assert store.get_extraction_state("doc-4") is None

    def test_a_fresh_stamp_is_still_believed(self, store):
        store.set_extraction_state("doc-5", "extracting")
        assert store.get_extraction_state("doc-5") == "extracting"

    def test_no_document_id_in_source_never_blocks(self, store):
        import app.api.routes.standards as std
        assert std._extraction_in_flight_for({"source_document": "no-parens.pdf"}) is False
        assert std._extraction_in_flight_for({}) is False


class TestIsInFlightStartGuard:
    """is_in_flight also guards the START of a run, not just approve/reject.

    Without it, a standards upload (which auto-triggers extraction) plus a manual
    or double-clicked /extract mint TWO batches for one document — the duplicate
    the reviewer then has to untangle.
    """

    def test_sees_a_run_via_the_cross_task_row_stamp(self, store, monkeypatch):
        # No live job in this task; the document-row stamp is the only signal.
        monkeypatch.setattr("app.services.extraction_log.get_job", lambda _d: None,
                            raising=False)
        from app.services import extraction_log as el
        store.set_extraction_state("doc-x", "extracting")
        assert el.is_in_flight("doc-x") is True

    def test_false_once_the_run_is_terminal(self, store, monkeypatch):
        monkeypatch.setattr("app.services.extraction_log.get_job", lambda _d: None,
                            raising=False)
        from app.services import extraction_log as el
        store.set_extraction_state("doc-y", "extracting")
        store.set_extraction_state("doc-y", None)  # completion clears it
        assert el.is_in_flight("doc-y") is False

    def test_sees_a_run_via_the_same_task_live_log(self, store, monkeypatch):
        # The row stamp is absent; the local live job is the signal.
        from app.services import extraction_log as el
        monkeypatch.setattr(el, "get_job", lambda _d: {"status": "starting"})
        assert el.is_in_flight("doc-z") is True

    def test_empty_document_id_is_never_in_flight(self, store):
        from app.services import extraction_log as el
        assert el.is_in_flight("") is False
        assert el.is_in_flight(None) is False

    def test_a_read_failure_fails_open_not_busy(self, store, monkeypatch):
        # A store hiccup must not fabricate "busy" and block a legitimate run.
        from app.services import extraction_log as el
        monkeypatch.setattr(el, "get_job", lambda _d: None)
        monkeypatch.setattr(
            store, "get_extraction_state",
            lambda _d: (_ for _ in ()).throw(RuntimeError("ddb down")),
        )
        assert el.is_in_flight("doc-err") is False
