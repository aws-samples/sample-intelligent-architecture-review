// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The API client's 60 methods: URLs, query-string construction, request bodies,
 * and error surfacing.
 *
 * api-client.test.ts already covers authFetch's token plumbing and 401 recovery.
 * This file covers the per-method contracts, because that is where a silent
 * mistake changes behaviour without failing: an omitted query parameter falls
 * back to a server default (e.g. dropping `hosting_scope` makes the backend 400,
 * and dropping `include_proposed` silently hides pending rules), and a wrong
 * path 404s in a way the UI reports as an empty list.
 */
import { describe, expect, it, vi, beforeAll, beforeEach } from "vitest";

import { api } from "@/lib/api";

const API = "https://api.test";

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

/** Stub fetch and return the mock so tests can inspect the call. */
function stubFetch(body: unknown = {}, init: ResponseInit = {}) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(body, init));
}

/** The URL string of the Nth fetch call. */
function urlOf(mock: ReturnType<typeof stubFetch>, n = 0): string {
  return String(mock.mock.calls[n][0]);
}

function initOf(mock: ReturnType<typeof stubFetch>, n = 0): RequestInit {
  return (mock.mock.calls[n][1] ?? {}) as RequestInit;
}

function bodyOf(mock: ReturnType<typeof stubFetch>, n = 0): unknown {
  const raw = initOf(mock, n).body;
  return typeof raw === "string" ? JSON.parse(raw) : raw;
}

// jsdom does not implement URL.createObjectURL, so vi.spyOn has nothing to
// replace. Install a stub the blob-URL tests can assert against.
const OBJECT_URL = "blob:stub-url";
beforeAll(() => {
  Object.defineProperty(URL, "createObjectURL", {
    writable: true,
    value: () => OBJECT_URL,
  });
  Object.defineProperty(URL, "revokeObjectURL", { writable: true, value: () => {} });
});

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("error surfacing", () => {
  it("uses FastAPI's detail field on methods that read it", async () => {
    // Not every method uses errorDetail — several report res.statusText. Upload
    // is one that surfaces the body, which is what makes a 400 actionable.
    stubFetch({ detail: "hosting_scope is required" }, { status: 400 });
    const file = new File([new Uint8Array([1])], "f.pdf");
    await expect(api.uploadDocument(file)).rejects.toThrow(/hosting_scope/);
  });

  it("falls back to a status when the body has no detail", async () => {
    // A non-JSON body must not produce "Failed: " with nothing after it.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("gateway exploded", { status: 502, statusText: "Bad Gateway" }),
    );
    const file = new File([new Uint8Array([1])], "f.pdf");
    await expect(api.uploadDocument(file)).rejects.toThrow(/Bad Gateway|502/);
  });

  it("rejects rather than resolving undefined on a server error", async () => {
    stubFetch({ detail: "boom" }, { status: 500 });
    await expect(api.listStandards()).rejects.toThrow();
  });
});

describe("documents", () => {
  it("lists documents", async () => {
    const f = stubFetch({ documents: [], total: 0 });
    await api.listDocuments();
    expect(urlOf(f)).toBe(`${API}/api/documents`);
  });

  it("fetches one document by id", async () => {
    const f = stubFetch({ document_id: "doc-1" });
    await api.getDocument("doc-1");
    expect(urlOf(f)).toBe(`${API}/api/documents/doc-1`);
  });

  it("encodes ids on the endpoints that take user-supplied keys", async () => {
    // roles keys are Cognito subs supplied by the caller, so that path encodes.
    const f = stubFetch({ message: "removed" });
    await api.removeUserRole("sub/with slash");
    expect(urlOf(f)).toContain(encodeURIComponent("sub/with slash"));
  });

  it("sends the tier as a PATCH body", async () => {
    const f = stubFetch({ document_id: "doc-1", declared_tier: 3 });
    await api.setDocumentTier("doc-1", 3);
    expect(initOf(f).method).toBe("PATCH");
    expect(bodyOf(f)).toEqual({ tier: 3 });
  });

  it("sends null to clear a tier override", async () => {
    // null means "revert to auto-detected"; omitting it would leave the
    // override in place.
    const f = stubFetch({ document_id: "doc-1", declared_tier: null });
    await api.setDocumentTier("doc-1", null);
    expect(bodyOf(f)).toEqual({ tier: null });
  });

  it("requests diagrams for a document", async () => {
    const f = stubFetch({ diagrams: [], total: 0 });
    await api.getDocumentDiagrams("doc-1");
    expect(urlOf(f)).toContain("/api/documents/doc-1/diagrams");
  });

  it("fetches a slide image and hands back an object url", async () => {
    // The <img> cannot send a bearer token, so the bytes are fetched here and
    // exposed as a blob URL.
    const f = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(new Blob([new Uint8Array([1])])));
    await expect(api.getDocumentSlideUrl("doc-1", 4)).resolves.toBe(OBJECT_URL);
    expect(urlOf(f)).toBe(`${API}/api/documents/doc-1/slides/4`);
  });

  it("returns null when a slide image is missing", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("", { status: 404 }));
    await expect(api.getDocumentSlideUrl("doc-1", 9)).resolves.toBeNull();
  });

  it("reports pipeline status", async () => {
    const f = stubFetch({ jobs: [] });
    await api.getPipelineStatus();
    expect(urlOf(f)).toContain("/api/documents/pipeline/status");
  });

  it("rejects pipeline status HTTP failures with the response detail", async () => {
    stubFetch(
      { detail: "pipeline status unavailable" },
      { status: 503, statusText: "Service Unavailable" },
    );

    await expect(api.getPipelineStatus()).rejects.toThrow(
      /pipeline status unavailable/,
    );
  });

  it("propagates pipeline status network failures", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new TypeError("network unavailable"),
    );

    await expect(api.getPipelineStatus()).rejects.toThrow(/network unavailable/);
  });
});

describe("uploadDocument", () => {
  function file() {
    return new File([new Uint8Array([1, 2, 3])], "sdd.pdf", {
      type: "application/pdf",
    });
  }

  it("posts multipart form data", async () => {
    const f = stubFetch({ document_id: "doc-1" });
    await api.uploadDocument(file());
    expect(initOf(f).method).toBe("POST");
    expect(initOf(f).body).toBeInstanceOf(FormData);
  });

  it("puts the document type in the query string", async () => {
    const f = stubFetch({ document_id: "doc-1" });
    await api.uploadDocument(file(), "standards");
    expect(urlOf(f)).toContain("document_type=standards");
  });

  it("sends hosting_scope, which the backend requires", async () => {
    const f = stubFetch({ document_id: "doc-1" });
    await api.uploadDocument(file(), "sdd", { hostingScope: "cloud" });
    expect(urlOf(f)).toContain("hosting_scope=cloud");
  });

  it("sends trimmed extraction notes", async () => {
    const f = stubFetch({ document_id: "doc-1" });
    await api.uploadDocument(file(), "standards", {
      extractionNotes: "  focus on storage  ",
    });
    const notes = new URL(urlOf(f)).searchParams.get("extraction_notes");
    expect(notes).toBe("focus on storage");
  });

  it("omits whitespace-only notes entirely", async () => {
    // Blank notes must fall back to unstructured extraction, not send "".
    const f = stubFetch({ document_id: "doc-1" });
    await api.uploadDocument(file(), "standards", { extractionNotes: "   " });
    expect(urlOf(f)).not.toContain("extraction_notes");
  });

  it("uses a bare url when there is nothing to parameterise", async () => {
    const f = stubFetch({ document_id: "doc-1" });
    await api.uploadDocument(file());
    expect(urlOf(f)).toBe(`${API}/api/documents`);
  });

});

describe("validations", () => {
  it("triggers a validation with the standards set", async () => {
    const f = stubFetch({ validation_id: "val-1" });
    await api.triggerValidation("doc-1", "default");
    expect(initOf(f).method).toBe("POST");
    expect(bodyOf(f)).toMatchObject({ document_id: "doc-1" });
  });

  it("passes the selected rule subset through", async () => {
    const f = stubFetch({ validation_id: "val-1" });
    await api.triggerValidation("doc-1", "default", {
      standardIds: ["HA-1", "HA-2"],
    });
    expect(bodyOf(f)).toMatchObject({ standard_ids: ["HA-1", "HA-2"] });
  });

  it("passes a tier override through", async () => {
    const f = stubFetch({ validation_id: "val-1" });
    await api.triggerValidation("doc-1", "default", { tierOverride: 2 });
    expect(bodyOf(f)).toMatchObject({ tier_override: 2 });
  });

  it("requests a forced re-run when asked", async () => {
    const f = stubFetch({ validation_id: "val-1" });
    await api.triggerValidation("doc-1", "default", { force: true });
    expect(bodyOf(f)).toMatchObject({ force: true });
  });

  it("lists validations", async () => {
    const f = stubFetch({ validations: [], total: 0 });
    await api.listValidations();
    expect(urlOf(f)).toContain("/api/validations");
  });

  it("filters the list by document", async () => {
    const f = stubFetch({ validations: [], total: 0 });
    await api.listValidations("doc-1");
    expect(urlOf(f)).toContain("document_id=doc-1");
  });

  it("fetches one validation", async () => {
    const f = stubFetch({ validation_id: "val-1" });
    await api.getValidation("val-1");
    expect(urlOf(f)).toContain("/api/validations/val-1");
  });

  it("posts a cancellation", async () => {
    const f = stubFetch({ cancelled: true });
    await api.cancelValidation("val-1");
    expect(initOf(f).method).toBe("POST");
    expect(urlOf(f)).toContain("/api/validations/val-1/cancel");
  });

  it("throws when a cancellation is refused", async () => {
    // The backend 403s when the caller does not own the run. This method
    // reports statusText rather than the body, so assert it rejects at all.
    stubFetch({ detail: "not the owner" }, { status: 403, statusText: "Forbidden" });
    await expect(api.cancelValidation("val-1")).rejects.toThrow(/Forbidden|cancel/);
  });
});

describe("standards", () => {
  it("lists active standards by default", async () => {
    const f = stubFetch({ standards: [], total: 0 });
    await api.listStandards();
    expect(urlOf(f)).toContain("/api/standards");
  });

  it("asks for proposals explicitly", async () => {
    // Without this the proposals review page silently shows nothing.
    const f = stubFetch({ standards: [], total: 0 });
    await api.listStandards({ includeProposed: true });
    expect(urlOf(f)).toContain("include_proposed=true");
  });

  it("creates a standard", async () => {
    const f = stubFetch({ id: "HA-U-1" });
    await api.createStandard({
      name: "n",
      description: "d",
      severity: "high",
      category: "availability",
      validation_criteria: "c",
      hosting_scope: "both",
    });
    expect(initOf(f).method).toBe("POST");
    expect(bodyOf(f)).toMatchObject({ name: "n", hosting_scope: "both" });
  });

  it("updates a standard with PUT", async () => {
    const f = stubFetch({ id: "HA-1" });
    await api.updateStandard("HA-1", { severity: "low" });
    expect(initOf(f).method).toBe("PUT");
    expect(bodyOf(f)).toEqual({ severity: "low" });
  });

  it("deletes a standard", async () => {
    const f = stubFetch({ message: "deleted" });
    await api.deleteStandard("HA-1");
    expect(initOf(f).method).toBe("DELETE");
  });

  it("approves a proposal", async () => {
    const f = stubFetch({ id: "P-1", status: "active" });
    await api.approveStandard("P-1");
    expect(urlOf(f)).toContain("/api/standards/P-1/approve");
    expect(initOf(f).method).toBe("POST");
  });

  it("rejects a proposal", async () => {
    const f = stubFetch({ message: "rejected" });
    await api.rejectStandard("P-1");
    expect(urlOf(f)).toContain("/api/standards/P-1/reject");
  });

  it("throws on the 409 raised while extraction is still running", async () => {
    stubFetch({ detail: "still extracting" }, { status: 409, statusText: "Conflict" });
    await expect(api.approveStandard("P-1")).rejects.toThrow(/Conflict|approve/);
  });

  it("curates standards for a document", async () => {
    const f = stubFetch({ existing_standard_ids: [], new_suggestions: [] });
    await api.curateStandards("doc-1", { topN: 20 });
    expect(urlOf(f)).toContain("/api/standards/curate");
    expect(bodyOf(f)).toMatchObject({ document_id: "doc-1", top_n: 20 });
  });

  it("asks for new-rule suggestions when requested", async () => {
    const f = stubFetch({ existing_standard_ids: [], new_suggestions: [] });
    await api.curateStandards("doc-1", { topN: 5, suggestNew: true, maxNew: 3 });
    expect(bodyOf(f)).toMatchObject({ suggest_new: true, max_new: 3 });
  });
});

describe("extraction batches", () => {
  it("lists batches", async () => {
    const f = stubFetch({ batches: [] });
    await api.listExtractionBatches();
    expect(urlOf(f)).toContain("/api/standards/extraction-batches");
  });

  it("fetches one batch", async () => {
    const f = stubFetch({ batch_id: "b-1" });
    await api.getExtractionBatch("b-1");
    expect(urlOf(f)).toContain("/api/standards/extraction-batches/b-1");
  });

  it("approves a whole batch", async () => {
    const f = stubFetch({ approved: 12 });
    await api.approveExtractionBatch("b-1");
    expect(urlOf(f)).toContain("/approve-all");
    expect(initOf(f).method).toBe("POST");
  });

  it("rejects a whole batch", async () => {
    const f = stubFetch({ rejected: 12 });
    await api.rejectExtractionBatch("b-1");
    expect(urlOf(f)).toContain("/reject-all");
  });

  it("sets the scope for a whole batch, sending the scope in the body", async () => {
    // A wrong hosting_scope must actually reach the server as JSON, not just
    // hit the right path — the point is retargeting every rule.
    const f = stubFetch({ hosting_scope: "cloud", updated: 12, failed: [] });
    await api.setExtractionBatchScope("b-1", "cloud");
    expect(urlOf(f)).toContain("/extraction-batches/b-1/set-scope");
    expect(initOf(f).method).toBe("POST");
    expect(bodyOf(f)).toEqual({ hosting_scope: "cloud" });
  });

  it("surfaces the server's reason when set-scope fails", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "No pending proposals for batch b-9" }, { status: 404 }),
    );
    await expect(api.setExtractionBatchScope("b-9", "on_prem")).rejects.toThrow(
      /No pending proposals for batch b-9/,
    );
  });

  it("reads extraction status for a document", async () => {
    const f = stubFetch({ running: false });
    await api.getExtractionStatus("doc-1");
    expect(urlOf(f)).toContain("extraction-status");
  });
});

describe("references", () => {
  it("lists references", async () => {
    const f = stubFetch({ references: [], total: 0 });
    await api.listReferences();
    expect(urlOf(f)).toContain("/api/references");
  });

  it("creates a reference", async () => {
    const f = stubFetch({ id: "ref-1" });
    await api.createReference({
      name: "DC peers",
      body: "table",
      applies_to_tiers: [1],
      hosting_scope: "on_prem",
    });
    expect(initOf(f).method).toBe("POST");
    expect(bodyOf(f)).toMatchObject({ name: "DC peers" });
  });

  it("updates a reference with PUT", async () => {
    const f = stubFetch({ id: "ref-1" });
    await api.updateReference("ref-1", { body: "new table" });
    expect(initOf(f).method).toBe("PUT");
  });

  it("deletes a reference and accepts a bodiless 204", async () => {
    const f = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(null, { status: 204 }));
    await api.deleteReference("ref-1");
    expect(initOf(f).method).toBe("DELETE");
  });
});

describe("roles", () => {
  it("reads the caller's own role", async () => {
    const f = stubFetch({ role: "curator", email: "c@x.com" });
    const me = await api.getMyRole();
    expect(urlOf(f)).toContain("/api/roles/me");
    expect(me.role).toBe("curator");
  });

  it("lists user roles", async () => {
    const f = stubFetch({ users: [] });
    await api.listUserRoles();
    expect(urlOf(f)).toContain("/api/roles");
  });

  it("assigns a role with PUT", async () => {
    const f = stubFetch({ sub: "s", role: "validator" });
    await api.setUserRole("s", "validator", "u@x.com");
    expect(initOf(f).method).toBe("PUT");
    expect(bodyOf(f)).toEqual({ sub: "s", role: "validator", email: "u@x.com" });
  });

  it("defaults the email to empty when not supplied", async () => {
    const f = stubFetch({ sub: "s", role: "viewer" });
    await api.setUserRole("s", "viewer");
    expect(bodyOf(f)).toMatchObject({ email: "" });
  });

  it("removes a role assignment", async () => {
    const f = stubFetch({ message: "removed" });
    await api.removeUserRole("s");
    expect(initOf(f).method).toBe("DELETE");
  });
});

describe("queries", () => {
  it("submits a question", async () => {
    const f = stubFetch({ answer: "yes" });
    await api.submitQuery("Is it multi-AZ?");
    expect(initOf(f).method).toBe("POST");
    expect(bodyOf(f)).toMatchObject({ question: "Is it multi-AZ?" });
  });

  it("scopes a question to one document when given", async () => {
    const f = stubFetch({ answer: "yes" });
    await api.submitQuery("q", "doc-1");
    expect(bodyOf(f)).toMatchObject({ document_id: "doc-1" });
  });
});

describe("audit", () => {
  it("reads an entity's history", async () => {
    const f = stubFetch({ events: [{ action: "standard.approve" }] });
    const events = await api.getAuditEvents("document", "doc-1");
    const url = urlOf(f);
    expect(url).toContain("entity_type=document");
    expect(url).toContain("entity_id=doc-1");
    expect(events).toHaveLength(1);
  });

  it("encodes both parameters", async () => {
    const f = stubFetch({ events: [] });
    await api.getAuditEvents("doc type", "doc/1");
    expect(urlOf(f)).toContain(encodeURIComponent("doc/1"));
  });

  it("returns an empty list rather than throwing on failure", async () => {
    // The audit panel is supplementary; it must not break the page it sits on.
    stubFetch({ detail: "forbidden" }, { status: 403 });
    await expect(api.getAuditEvents("document", "doc-1")).resolves.toEqual([]);
  });

  it("tolerates a response with no events key", async () => {
    stubFetch({});
    await expect(api.getAuditEvents("document", "doc-1")).resolves.toEqual([]);
  });
});

describe("icon legend", () => {
  it("lists icons", async () => {
    const f = stubFetch({ icons: [] });
    await api.listIconLegend();
    expect(urlOf(f)).toContain("/api/icon-legend");
  });

  it("creates a text icon", async () => {
    const f = stubFetch({ id: "icon-1" });
    await api.createIcon({ name: "DC", description: "datacentre" });
    expect(initOf(f).method).toBe("POST");
  });

  it("approves a draft icon", async () => {
    const f = stubFetch({ id: "icon-1", status: "active" });
    await api.approveIcon("icon-1");
    expect(urlOf(f)).toContain("/api/icon-legend/icon-1/approve");
  });

  it("deletes an icon and accepts a bodiless 204", async () => {
    const f = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(null, { status: 204 }));
    await api.deleteIcon("icon-1");
    expect(initOf(f).method).toBe("DELETE");
  });

  it("fetches an icon image as an object url", async () => {
    const f = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(new Blob([new Uint8Array([1])])));
    await expect(api.getIconImageUrl("icon-1")).resolves.toBe(OBJECT_URL);
    expect(urlOf(f)).toContain("/api/icon-legend/icon-1/image");
  });

  it("returns null rather than throwing when an icon image fails", async () => {
    // The reviewer UI must render a placeholder, not crash the page.
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network"));
    await expect(api.getIconImageUrl("icon-1")).resolves.toBeNull();
  });

  it("reads a bulk job's status", async () => {
    const f = stubFetch({ job_id: "job-1", state: "running" });
    await api.iconBulkJobStatus("job-1");
    expect(urlOf(f)).toContain("/api/icon-legend/bulk/jobs/job-1");
  });

  it("approves a whole batch", async () => {
    const f = stubFetch({ batch_id: "iconbatch-1", approved: 4 });
    await expect(api.approveIconBatch("iconbatch-1")).resolves.toEqual({
      batch_id: "iconbatch-1",
      approved: 4,
    });
    expect(urlOf(f)).toContain("/api/icon-legend/bulk/iconbatch-1/approve-all");
    expect(initOf(f).method).toBe("POST");
  });

  it("rejects a whole batch", async () => {
    const f = stubFetch({ batch_id: "iconbatch-1", rejected: 4 });
    await expect(api.rejectIconBatch("iconbatch-1")).resolves.toEqual({
      batch_id: "iconbatch-1",
      rejected: 4,
    });
    expect(urlOf(f)).toContain("/api/icon-legend/bulk/iconbatch-1/reject-all");
    expect(initOf(f).method).toBe("POST");
  });

  it("pairs images to a batch, sending every file under the same field", async () => {
    // The backend reads a `files` list; appending under per-file names would
    // arrive as no files at all and pair nothing.
    const f = stubFetch({ batch_id: "b1", paired: [], mismatches: [], unmatched_files: [], unmatched_rows: [], failures: [] });
    await api.pairIconBatchImages("b1", [
      new File(["a"], "fw.png", { type: "image/png" }),
      new File(["b"], "db.png", { type: "image/png" }),
    ]);
    expect(urlOf(f)).toContain("/api/icon-legend/bulk/b1/pair-images");
    const body = initOf(f).body as FormData;
    expect(body.getAll("files").map((v) => (v as File).name)).toEqual(["fw.png", "db.png"]);
  });

  it("returns the pairing report including what did not match", async () => {
    stubFetch({
      batch_id: "b1",
      paired: [{ file: "fw.png", icon_id: "ICON-1", label: "fw" }],
      mismatches: [{ file: "fw.png", icon_id: "ICON-1", label: "fw", reason: "database", observed: "cylinder" }],
      unmatched_files: ["stray.png"],
      unmatched_rows: [{ icon_id: "ICON-2", label: "lonely", expected_file: "missing.png" }],
      failures: [],
    });
    const out = await api.pairIconBatchImages("b1", []);
    expect(out.unmatched_files).toEqual(["stray.png"]);
    expect(out.unmatched_rows[0].label).toBe("lonely");
    expect(out.mismatches[0].observed).toBe("cylinder");
  });

  it("surfaces the server's reason when pairing fails", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "No drafts for batch b9" }, { status: 404 }),
    );
    await expect(api.pairIconBatchImages("b9", [])).rejects.toThrow(/No drafts for batch b9/);
  });

  it("surfaces the server's reason when a batch reject fails", async () => {
    // Batch reject deletes rows, so a silent failure would leave the reviewer
    // believing the queue was cleared when it was not.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "No drafts for batch iconbatch-9" }, { status: 404 }),
    );
    await expect(api.rejectIconBatch("iconbatch-9")).rejects.toThrow(
      /No drafts for batch iconbatch-9/,
    );
  });
});

describe("health", () => {
  it("reports true when the health endpoint answers", async () => {
    const f = stubFetch({ status: "ok" });
    await expect(api.checkHealth()).resolves.toBe(true);
    expect(urlOf(f)).toContain("/api/health");
  });

  it("reports false instead of throwing when the API is unreachable", async () => {
    // The connectivity indicator polls this; a throw would surface as an
    // unhandled rejection on every tick.
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("offline"));
    await expect(api.checkHealth()).resolves.toBe(false);
  });
});
