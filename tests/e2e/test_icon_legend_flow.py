# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E2E: the custom icon legend's trust gate, end to end on a live deployment.

This is the flow worth guarding in production: a curator uploads an icon crop, a
multimodal model DERIVES the definition from the pixels, and the entry stays a
DRAFT — invisible to the validator — until a human approves it. The suite proves
each link, including the negative cases (a draft must not appear in the active
legend; a viewer must not see unreviewed AI content).

Costs a real Bedrock vision call per interpret, so it is E2E-only. Every test
cleans up the rows it creates.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
def cleanup_icons(curator):
    """Delete anything the test created, even if it failed mid-way."""
    created: list[str] = []
    yield created
    for icon_id in created:
        curator.delete(f"/api/icon-legend/{icon_id}")


def test_image_upload_derives_definition_and_lands_as_draft(
    curator, viewer, png_bytes, cleanup_icons
):
    """The core trust inversion: the definition comes from the image, and the
    result is quarantined as a draft until a human approves it."""
    resp = curator.post(
        "/api/icon-legend/interpret-image",
        files={"file": ("e2e-icon.png", png_bytes, "image/png")},
    )
    assert resp.status_code == 201, resp.text
    draft = resp.json()
    cleanup_icons.append(draft["id"])

    # The model produced the definition — not a caption we supplied.
    assert draft["status"] == "draft"
    assert draft["ai_generated"] is True
    assert draft["source_kind"] == "image"
    assert draft["description"], "model returned no description"
    assert draft["confidence"] in ("high", "medium", "low")
    assert draft["icon_type"] in (
        "server", "database", "load_balancer", "datacenter", "network",
        "storage", "service", "firewall", "gateway", "other",
    )

    # Quarantine: absent from the active legend the validator reads...
    active = curator.get("/api/icon-legend?status=active").json()["icons"]
    assert all(i["id"] != draft["id"] for i in active), (
        "an unapproved draft appeared in the active legend — trust gate broken"
    )
    # ...but present in the curator's review queue.
    drafts = curator.get("/api/icon-legend?status=draft").json()["icons"]
    assert any(i["id"] == draft["id"] for i in drafts)

    # Unreviewed AI content is not shown to viewers.
    assert viewer.get("/api/icon-legend?status=draft").status_code == 403


def test_reviewer_can_see_the_image_behind_a_draft(curator, viewer, png_bytes,
                                                   cleanup_icons):
    """Approval is only meaningful if the reviewer can see the pixels the model
    read, so the draft's image must be retrievable (and viewer-gated)."""
    draft = curator.post(
        "/api/icon-legend/interpret-image",
        files={"file": ("e2e-icon.png", png_bytes, "image/png")},
    ).json()
    cleanup_icons.append(draft["id"])

    img = curator.get(f"/api/icon-legend/{draft['id']}/image")
    assert img.status_code == 200, img.text
    assert img.headers["content-type"] == "image/png"
    assert img.content[:8] == b"\x89PNG\r\n\x1a\n", "did not serve real PNG bytes"

    # A draft's image is unreviewed content — viewers are refused.
    assert viewer.get(f"/api/icon-legend/{draft['id']}/image").status_code == 403


def test_approval_activates_and_records_who_approved(curator, png_bytes,
                                                     cleanup_icons):
    draft = curator.post(
        "/api/icon-legend/interpret-image",
        files={"file": ("e2e-icon.png", png_bytes, "image/png")},
    ).json()
    cleanup_icons.append(draft["id"])

    resp = curator.post(f"/api/icon-legend/{draft['id']}/approve", json={
        "label": "e2e orange hexagon",
        "icon_type": "firewall",
        "description": "orange hexagon = perimeter firewall",
    })
    assert resp.status_code == 200, resp.text
    approved = resp.json()

    assert approved["status"] == "active"
    assert approved["icon_type"] == "firewall"          # curator's edit applied
    assert approved["approved_by"], "approver identity not recorded on the row"
    assert approved["approved_at"], "approval timestamp not recorded"

    # Now visible to the validator's legend.
    active = curator.get("/api/icon-legend?status=active").json()["icons"]
    assert any(i["id"] == draft["id"] for i in active)


def test_text_authored_icon_skips_the_review_queue(curator, cleanup_icons):
    """A human-written definition needs no AI review — it is trusted on write."""
    resp = curator.post("/api/icon-legend", json={
        "label": "e2e grey cylinder",
        "icon_type": "database",
        "description": "grey cylinder = primary transaction DB",
        "hosting_scope": "on_prem",
    })
    assert resp.status_code == 201, resp.text
    entry = resp.json()
    cleanup_icons.append(entry["id"])
    assert entry["status"] == "active"
    assert entry["ai_generated"] is False


def test_bulk_csv_reports_bad_rows_and_gates_the_good_ones(curator):
    """Bulk text import must reject invalid rows loudly (not silently drop them)
    and still quarantine the valid ones for review."""
    csv_body = (
        "label,icon_type,description\n"
        "e2e-bulk-db,database,primary DB\n"
        "e2e-bulk-bad,not-a-real-type,should be rejected\n"
        ",server,missing label\n"
        "e2e-bulk-lb,load_balancer,active/standby LB\n"
    )
    resp = curator.post(
        "/api/icon-legend/bulk/text",
        files={"file": ("e2e.csv", csv_body.encode(), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] == 2
    assert len(body["errors"]) == 2, f"bad rows not reported: {body['errors']}"

    batch = body["batch_id"]
    try:
        drafts = curator.get(
            f"/api/icon-legend?status=draft&batch={batch}"
        ).json()["icons"]
        assert len(drafts) == 2, "bulk rows should land as drafts, not active"

        approve = curator.post(f"/api/icon-legend/bulk/{batch}/approve-all")
        assert approve.status_code == 200
        assert approve.json()["approved"] == 2
    finally:
        for icon in curator.get(
            f"/api/icon-legend?status=active&batch={batch}"
        ).json()["icons"]:
            curator.delete(f"/api/icon-legend/{icon['id']}")


def test_oversized_canvas_image_is_rejected(curator):
    """A small file that decodes to a huge bitmap is a decompression bomb; it
    must be refused from the header, before anything allocates the canvas."""
    Image = pytest.importorskip("PIL.Image", reason="needs pillow")
    import io
    buf = io.BytesIO()
    Image.new("RGB", (6000, 6000), (1, 2, 3)).save(buf, format="PNG")
    payload = buf.getvalue()
    assert len(payload) < 2 * 1024 * 1024, "fixture must stay under the byte cap"

    resp = curator.post(
        "/api/icon-legend/interpret-image",
        files={"file": ("bomb.png", payload, "image/png")},
    )
    assert resp.status_code == 400, (
        f"decompression bomb accepted ({resp.status_code}) — guard regressed"
    )
