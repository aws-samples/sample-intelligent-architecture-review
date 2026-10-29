# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A validation killed mid-run must not stay PENDING forever.

Validations run as in-process background tasks. When ECS replaced a task during
a run, its row stayed PENDING: the dashboard counted it as active work
indefinitely, and the result cache handed the dead run back to every re-run of
the same document, so it could never be validated again.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models.validation import ValidationResult, ValidationStatus
from app.services import validation_store as vs


def _validation(status: ValidationStatus, age: timedelta) -> ValidationResult:
    return ValidationResult(
        validation_id="val-1",
        document_id="doc-1",
        status=status,
        standards_set="s",
        started_at=datetime.utcnow() - age,
        document_content_hash="dh",
        standards_set_hash="sh",
    )


def test_a_recent_pending_run_is_left_alone():
    v = vs._surface_interrupted(_validation(ValidationStatus.PENDING, timedelta(minutes=10)))
    assert v.status == ValidationStatus.PENDING
    assert v.error_message is None


def test_a_long_dead_pending_run_reads_as_failed():
    v = vs._surface_interrupted(_validation(ValidationStatus.PENDING, timedelta(hours=2)))
    assert v.status == ValidationStatus.FAILED
    assert "run the validation again" in v.error_message.lower()


def test_finished_runs_are_never_rewritten():
    for status in (ValidationStatus.COMPLETED, ValidationStatus.FAILED, ValidationStatus.CANCELLED):
        v = vs._surface_interrupted(_validation(status, timedelta(days=3)))
        assert v.status == status


def test_staleness_reads_dynamodb_iso_strings_with_or_without_offset():
    old = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    assert vs._is_stale_pending("pending", old)
    assert vs._is_stale_pending("pending", old + "+00:00")
    assert not vs._is_stale_pending("pending", datetime.utcnow().isoformat())
    assert not vs._is_stale_pending("pending", "not-a-date")


def test_the_cache_does_not_return_a_dead_run(monkeypatch):
    """Re-running the same document must start a fresh validation, not hand
    back the killed one."""
    store = vs.ValidationStore.__new__(vs.ValidationStore)
    store._available = False
    store._validations = {"val-1": _validation(ValidationStatus.PENDING, timedelta(hours=2))}
    assert store.find_cached_validation("doc-1", "dh", "sh") is None

    store._validations = {"val-1": _validation(ValidationStatus.PENDING, timedelta(minutes=5))}
    assert store.find_cached_validation("doc-1", "dh", "sh") is not None
