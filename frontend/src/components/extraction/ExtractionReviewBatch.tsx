// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Review gate — the extraction review batch.
 *
 * Rules extracted from a standards document land status="proposed" (they do
 * NOT enter the active library or validation) grouped by an
 * extraction_batch_id. This panel renders below the ExtractionLogPanel on the
 * document-detail page and lets a reviewer approve/reject — per rule, in bulk
 * via multi-select, or the whole batch — before any rule goes active.
 *
 * It also provides:
 *  - LIVE POLLING while extraction is in-flight, so newly-extracted rules
 *    appear without a page refresh (stops once the job is terminal).
 *  - a FULL-SCREEN review with checkbox multi-select + bulk approve/reject.
 *  - per-rule detail: rule_type (founding/substitution/exception), the
 *    page_reference location, and the verbatim source_quote so a reviewer
 *    can find and verify the source.
 *  - dispatches "standards-changed" on approve so the sidebar count updates.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  api,
  ExtractionBatchSummary,
  LAYER_VALUES,
  RULE_TYPE_VALUES,
  StandardItem,
  UpdateStandardData,
} from "@/lib/api";
import { PdfSourceViewer } from "@/components/documents/PdfSourceViewer";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type SortKey = "review" | "recent" | "name" | "severity" | "page";

const SEVERITY_RANK: Record<string, number> = {
  critical: 4, high: 3, medium: 2, low: 1, info: 0,
};

/** Parse a "page N" reference to its integer, or a large number so
 *  unref'd rules sort last. */
function pageNum(ref?: string): number {
  const m = (ref || "").match(/\d+/);
  return m ? parseInt(m[0], 10) : 1e9;
}

function sortRules(rules: StandardItem[], key: SortKey): StandardItem[] {
  const out = [...rules];
  switch (key) {
    case "recent":
      return out.sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    case "name":
      return out.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    case "severity":
      return out.sort(
        (a, b) => (SEVERITY_RANK[b.severity] ?? 0) - (SEVERITY_RANK[a.severity] ?? 0),
      );
    case "page":
      return out.sort((a, b) => pageNum(a.page_reference) - pageNum(b.page_reference));
    case "review":
    default:
      // needs_review first, then by page for scan order.
      return out.sort((a, b) => {
        if (!!a.needs_review !== !!b.needs_review) return a.needs_review ? -1 : 1;
        return pageNum(a.page_reference) - pageNum(b.page_reference);
      });
  }
}

const POLL_MS = 3000;

const SEVERITY_CLS: Record<string, string> = {
  critical: "bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-300",
  high: "bg-orange-50 text-orange-700 dark:bg-orange-500/10 dark:text-orange-300",
  medium: "bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300",
  low: "bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300",
};

const KIND_CLS: Record<string, string> = {
  founding: "bg-indigo-50 text-indigo-700 border-indigo-200 dark:bg-indigo-500/10 dark:text-indigo-300 dark:border-indigo-500/20",
  substitution: "bg-sky-50 text-sky-700 border-sky-200 dark:bg-sky-500/10 dark:text-sky-300 dark:border-sky-500/20",
  exception: "bg-fuchsia-50 text-fuchsia-700 border-fuchsia-200 dark:bg-fuchsia-500/10 dark:text-fuchsia-300 dark:border-fuchsia-500/20",
};

/** Fire a cross-component signal that the active standards set changed, so
 *  the sidebar (and any listener) can refresh its count without a refresh. */
function notifyStandardsChanged() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("standards-changed"));
  }
}

type Batch = ExtractionBatchSummary & { rules: StandardItem[] };

export function ExtractionReviewBatch({ documentId }: { documentId: string }) {
  const [batch, setBatch] = useState<Batch | null>(null);
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState(false);
  const [confirmRejectAll, setConfirmRejectAll] = useState(false);
  const [actingId, setActingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fullScreen, setFullScreen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [jobInFlight, setJobInFlight] = useState(false);
  // Set-scope control state: which scope is pending, and whether it's applying.
  const [scopeChoice, setScopeChoice] = useState<"on_prem" | "cloud" | "both" | "">("");
  const [settingScope, setSettingScope] = useState(false);
  // Full-screen view controls.
  const [viewMode, setViewMode] = useState<"gallery" | "list">("gallery");
  const [sortBy, setSortBy] = useState<SortKey>("review");
  const [search, setSearch] = useState("");
  // Split-pane document viewer: the rule whose source we're inspecting.
  const [docPaneOpen, setDocPaneOpen] = useState(false);
  const [inspectRule, setInspectRule] = useState<StandardItem | null>(null);
  const [fileUrl, setFileUrl] = useState<string>("");
  // Inline edit: the rule id currently being edited (form shown in-place).
  const [editingId, setEditingId] = useState<string | null>(null);
  // Expanded card popup (accessibility — adjustable font size).
  const [expandedRule, setExpandedRule] = useState<StandardItem | null>(null);

  const load = useCallback(async (): Promise<Batch | null> => {
    try {
      const res = await api.listExtractionBatches(documentId);
      const summary = res.batches[0];
      if (!summary) {
        setBatch(null);
        return null;
      }
      const full = await api.getExtractionBatch(summary.extraction_batch_id);
      setBatch(full);
      return full;
    } catch {
      // No batch / transient error — keep whatever we had.
      return null;
    } finally {
      setLoading(false);
    }
  }, [documentId]);

  // Live-poll while extraction is in-flight so new proposed rules appear
  // without a refresh. We poll the extraction-status job to decide when to
  // stop; once terminal we do one final load and hold. We ALSO keep a slow
  // poll going even when idle-but-nonempty so a batch produced by a
  // background job (started elsewhere) still surfaces.
  useEffect(() => {
    if (!documentId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      if (cancelled) return;
      let inFlight = false;
      try {
        const status = await api.getExtractionStatus({ documentId });
        const s = status.job?.status;
        inFlight = s === "starting" || s === "extracting" || s === "enriching";
      } catch {
        inFlight = false;
      }
      if (cancelled) return;
      setJobInFlight(inFlight);
      const full = await load();
      if (cancelled) return;
      // Keep polling while in-flight (fast). The batch summary can still be
      // `in_flight` after the status job leaves its running states — dedup +
      // cross-reference wiring run in that window — so poll until BOTH the
      // status job and the batch have settled, or the provisional "…" count
      // would stick until a manual refresh.
      if (inFlight || full?.in_flight === true) {
        timer = setTimeout(tick, POLL_MS);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [documentId, load]);

  // Escape closes full-screen.
  useEffect(() => {
    if (!fullScreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullScreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullScreen]);

  // Fetch the source PDF as an auth'd blob URL — only once the doc pane is
  // first opened (avoid downloading the PDF for users who never inspect).
  //
  // IMPORTANT: fileUrl is NOT a dependency, and we do NOT revoke in this
  // effect's cleanup. Earlier both were true, which self-destructed the
  // viewer: setFileUrl() retriggered the effect, whose cleanup then revoked
  // the blob we had just created — so PdfSourceViewer fetched a dead
  // blob: URL ("Unexpected server response (0)"). The blob is revoked once,
  // on unmount, in the separate effect below.
  const fileUrlRef = useRef<string>("");
  useEffect(() => {
    if (!docPaneOpen || fileUrlRef.current) return;
    let cancelled = false;
    api
      .getDocumentBlobUrl(documentId)
      .then((u) => {
        if (cancelled) {
          URL.revokeObjectURL(u); // pane closed before fetch resolved
          return;
        }
        fileUrlRef.current = u;
        setFileUrl(u);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [docPaneOpen, documentId]);

  // Revoke the blob URL only on unmount.
  useEffect(() => {
    return () => {
      if (fileUrlRef.current) URL.revokeObjectURL(fileUrlRef.current);
    };
  }, []);

  // Click a rule → open the doc pane and inspect its source location.
  const inspect = (rule: StandardItem) => {
    setInspectRule(rule);
    setDocPaneOpen(true);
  };

  const deselect = (ids: string[]) =>
    setSelectedIds((prev) => {
      if (prev.size === 0) return prev;
      const next = new Set(prev);
      for (const id of ids) next.delete(id);
      return next;
    });

  const approveOne = async (id: string) => {
    if (jobInFlight) return; // locked until extraction/synthesis finalizes
    setActingId(id);
    setError(null);
    try {
      await api.approveStandard(id);
      deselect([id]);
      notifyStandardsChanged();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Approve failed");
    } finally {
      setActingId(null);
    }
  };

  const rejectOne = async (id: string) => {
    if (jobInFlight) return;
    setActingId(id);
    setError(null);
    try {
      await api.rejectStandard(id);
      deselect([id]);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Reject failed");
    } finally {
      setActingId(null);
    }
  };

  const approveAll = async () => {
    if (!batch || jobInFlight) return;
    setActing(true);
    setError(null);
    try {
      const r = await api.approveExtractionBatch(batch.extraction_batch_id);
      if (r.failed.length) setError(`${r.failed.length} rule(s) could not be approved.`);
      notifyStandardsChanged();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Approve-all failed");
    } finally {
      setActing(false);
    }
  };

  const rejectAll = async () => {
    if (!batch || jobInFlight) return;
    // Deleting a whole batch is unrecoverable without a re-upload + re-extract,
    // so require a deliberate second click.
    if (!confirmRejectAll) {
      setConfirmRejectAll(true);
      return;
    }
    setConfirmRejectAll(false);
    setActing(true);
    setError(null);
    try {
      const r = await api.rejectExtractionBatch(batch.extraction_batch_id);
      if (r.failed.length) setError(`${r.failed.length} rule(s) could not be rejected.`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Reject-all failed");
    } finally {
      setActing(false);
    }
  };

  // Retarget every proposed rule in the batch to one hosting scope — the fix
  // for a wrong upload-time selection, applied before approval.
  const setBatchScope = async (scope: "on_prem" | "cloud" | "both") => {
    if (!batch || jobInFlight) return;
    setSettingScope(true);
    setError(null);
    try {
      const r = await api.setExtractionBatchScope(batch.extraction_batch_id, scope);
      if (r.failed.length) setError(`${r.failed.length} rule(s) could not be retargeted.`);
      setScopeChoice("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Set-scope failed");
    } finally {
      setSettingScope(false);
    }
  };

  // Bulk approve/reject the current selection, sequentially (no batch API for
  // an arbitrary subset). Refresh once at the end.
  const bulkAct = async (mode: "approve" | "reject") => {
    if (!batch || selectedIds.size === 0 || jobInFlight) return;
    setActing(true);
    setError(null);
    const ids = batch.rules.filter((r) => selectedIds.has(r.id)).map((r) => r.id);
    let failures = 0;
    for (const id of ids) {
      try {
        if (mode === "approve") await api.approveStandard(id);
        else await api.rejectStandard(id);
        deselect([id]);
      } catch {
        failures += 1;
      }
    }
    if (failures) setError(`${failures} of ${ids.length} could not be ${mode}d.`);
    if (mode === "approve") notifyStandardsChanged();
    await load();
    setActing(false);
  };

  const toggleSelect = (id: string) =>
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  // Persist an inline edit to a proposed rule, then refresh the batch.
  const saveEdit = async (id: string, data: UpdateStandardData) => {
    if (jobInFlight) return;
    setActingId(id);
    setError(null);
    try {
      await api.updateStandard(id, data);
      setEditingId(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Edit failed");
    } finally {
      setActingId(null);
    }
  };

  if (loading || !batch || batch.total === 0) return null;

  // While extraction is still running, approvals are LOCKED: rules land
  // proposed per-unit, but cross-reference wiring + dedup (synthesis) only run
  // after all units finish, over the whole batch. Approving early would pull a
  // rule out of that set — orphaning parent links + skipping dedup. The
  // backend also enforces this (409), so this is UX, not the guarantee.
  // In-flight if the live status poll says so OR the batch summary is still
  // finalizing (dedup/synthesis running). Either source keeps the provisional
  // pre-dedup count hidden until it settles.
  const finalizing = jobInFlight || batch.in_flight === true;
  const locked = finalizing;
  const busy = acting || actingId !== null || locked;
  const rules = batch.rules;
  // Search filter (name / description / page reference), then sort.
  const q = search.trim().toLowerCase();
  const filteredRules = q
    ? rules.filter(
        (r) =>
          (r.name || "").toLowerCase().includes(q) ||
          (r.description || "").toLowerCase().includes(q) ||
          (r.page_reference || "").toLowerCase().includes(q),
      )
    : rules;
  const sortedRules = sortRules(filteredRules, sortBy);
  // Select-all / counts operate on the currently-VISIBLE (filtered) set.
  const selectedCount = sortedRules.filter((r) => selectedIds.has(r.id)).length;
  const allSelected = sortedRules.length > 0 && selectedCount === sortedRules.length;

  const toggleSelectAll = () => {
    if (allSelected) deselect(sortedRules.map((r) => r.id));
    else
      setSelectedIds((prev) => {
        const next = new Set(prev);
        for (const r of sortedRules) next.add(r.id);
        return next;
      });
  };

  return (
    <>
      <div className="rounded-xl border border-amber-300/60 bg-amber-50/40 dark:border-amber-500/25 dark:bg-amber-500/[0.04]">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-amber-200/70 px-4 py-3 dark:border-amber-500/20">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-amber-700 dark:text-amber-300">
              Pending review
            </span>
            {/* While finalizing, the count is the pre-dedup per-unit total and
                would over-report (a doc that restates rules across pages
                inflates it until dedup runs), so show a dash instead of a
                number that's about to drop. */}
            <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-amber-500 px-1.5 text-[10px] font-semibold text-white">
              {finalizing ? "…" : batch.total}
            </span>
            {finalizing && (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                <svg className="h-3 w-3 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                {jobInFlight ? "extracting…" : "de-duplicating…"}
              </span>
            )}
            {batch.needs_review > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full bg-rose-100 px-2 py-0.5 text-[10px] font-medium text-rose-700 dark:bg-rose-500/15 dark:text-rose-300">
                {batch.needs_review} need scrutiny
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setFullScreen(true)}
              className="inline-flex h-7 items-center gap-1 rounded-md border border-amber-300 bg-white/70 px-2.5 text-[11px] font-medium text-amber-800 transition-colors hover:bg-white dark:border-amber-500/30 dark:bg-white/5 dark:text-amber-200 dark:hover:bg-white/10"
            >
              <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
              </svg>
              Full screen
            </button>
            <button
              type="button"
              onClick={approveAll}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm transition-all hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {acting && (
                <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              )}
              Approve all {batch.total}
            </button>
            <button
              type="button"
              onClick={rejectAll}
              disabled={busy}
              title={confirmRejectAll
                ? "Click again to permanently delete every rule in this batch"
                : undefined}
              className={
                "inline-flex items-center rounded-lg border px-3 py-1.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 " +
                (confirmRejectAll
                  ? "border-rose-300 bg-rose-50 text-rose-700 hover:bg-rose-100 dark:border-rose-400/30 dark:bg-rose-400/10 dark:text-rose-200"
                  : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10")
              }
            >
              {confirmRejectAll ? `Delete all ${batch.total}? Click to confirm` : "Reject all"}
            </button>
            {confirmRejectAll && (
              <button
                type="button"
                onClick={() => setConfirmRejectAll(false)}
                className="text-xs font-medium text-slate-500 underline underline-offset-2 hover:text-slate-700 dark:text-slate-400"
              >
                Cancel
              </button>
            )}
          </div>
        </div>

        <p className="px-4 pt-2.5 text-[11px] leading-relaxed text-amber-800/80 dark:text-amber-200/70">
          These rules were extracted from this document but are <strong>not yet
          active</strong> — they won&apos;t be used in validation until approved.
          {jobInFlight && " More may appear as extraction continues."}
        </p>

        {batch.scope_mismatch && (
          <div className="mx-4 mt-2 rounded-md border border-amber-300 bg-amber-100/60 px-3 py-2.5 dark:border-amber-500/30 dark:bg-amber-500/10">
            <div className="flex items-start gap-2">
              <svg className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M4.93 19h14.14a2 2 0 001.74-3l-7.07-12a2 2 0 00-3.48 0l-7.07 12a2 2 0 001.74 3z" />
              </svg>
              <div className="min-w-0 text-[11px] leading-relaxed text-amber-900 dark:text-amber-100">
                <p>
                  <strong>Check the hosting scope.</strong> You selected{" "}
                  <span className="font-mono">{batch.scope_mismatch.declared}</span> at upload,
                  but this document reads as{" "}
                  <span className="font-mono">{batch.scope_mismatch.inferred}</span>{" "}
                  ({batch.scope_mismatch.confidence} confidence). All {batch.total} rule(s) below
                  are tagged <span className="font-mono">{batch.scope_mismatch.declared}</span>.
                </p>
                {batch.scope_mismatch.rationale && (
                  <p className="mt-1 italic text-amber-800/80 dark:text-amber-200/70">
                    {batch.scope_mismatch.rationale}
                  </p>
                )}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setBatchScope(batch.scope_mismatch!.inferred as "on_prem" | "cloud" | "both")}
                    disabled={busy || settingScope}
                    className="inline-flex items-center gap-1 rounded-md bg-amber-600 px-2.5 py-1 text-[11px] font-semibold text-white transition-colors hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {settingScope && (
                      <svg className="h-3 w-3 animate-spin" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                      </svg>
                    )}
                    Set all {batch.total} to {batch.scope_mismatch.inferred}
                  </button>
                  <span className="text-amber-800/70 dark:text-amber-200/60">or</span>
                  <select
                    value={scopeChoice}
                    onChange={(e) => setScopeChoice(e.target.value as typeof scopeChoice)}
                    disabled={busy || settingScope}
                    aria-label="Choose a different hosting scope for the batch"
                    className="h-7 rounded-md border border-amber-300 bg-white px-2 text-[11px] text-amber-900 outline-none dark:border-amber-500/30 dark:bg-white/5 dark:text-amber-100"
                  >
                    <option value="">choose scope…</option>
                    <option value="on_prem">on_prem</option>
                    <option value="cloud">cloud</option>
                    <option value="both">both</option>
                  </select>
                  <button
                    type="button"
                    onClick={() => scopeChoice && setBatchScope(scopeChoice)}
                    disabled={busy || settingScope || !scopeChoice}
                    className="inline-flex items-center rounded-md border border-amber-300 bg-white px-2.5 py-1 text-[11px] font-semibold text-amber-800 transition-colors hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-amber-500/30 dark:bg-white/5 dark:text-amber-200"
                  >
                    Apply
                  </button>
                </div>
                <p className="mt-1.5 text-amber-800/60 dark:text-amber-200/50">
                  Or leave it as-is if the selection was correct — this is only a warning.
                </p>
              </div>
            </div>
          </div>
        )}

        {locked && (
          <div className="mx-4 mt-2 flex items-center gap-2 rounded-md bg-sky-50 px-3 py-1.5 text-[11px] font-medium text-sky-700 dark:bg-sky-500/10 dark:text-sky-300">
            <svg className="h-3.5 w-3.5 shrink-0 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            Approving is locked until extraction finishes — cross-references and
            duplicates are still being finalized across the whole document.
          </div>
        )}

        {error && (
          <div className="mx-4 mt-2 rounded-md bg-rose-50 px-3 py-1.5 text-xs font-medium text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">
            {error}
          </div>
        )}

        <ul className="max-h-[28rem] space-y-2 overflow-y-auto p-4">
          {rules.map((r) => (
            <RuleRow
              key={r.id}
              rule={r}
              acting={actingId === r.id || acting || locked}
              onApprove={() => approveOne(r.id)}
              onReject={() => rejectOne(r.id)}
              editable={!locked}
              isEditing={editingId === r.id}
              onEdit={() => setEditingId(r.id)}
              onCancelEdit={() => setEditingId(null)}
              onSaveEdit={(data) => saveEdit(r.id, data)}
              onExpand={() => setExpandedRule(r)}
            />
          ))}
        </ul>
      </div>

      {/* Full-screen review with multi-select bulk actions. Portalled to
          <body>: inside this component's space-y stack it inherits a bottom
          margin, which shortens a fixed inset-0 box and leaves the page
          showing through along the bottom edge. */}
      {fullScreen && createPortal(
        <div className="fixed inset-0 z-50 flex flex-col bg-white dark:bg-slate-950 animate-in fade-in duration-150">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/80 px-5 py-3.5 dark:border-white/10">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-50">
                Review extracted rules
              </h2>
              <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-amber-500 px-1.5 text-[10px] font-semibold text-white">
                {finalizing ? "…" : batch.total}
              </span>
              {finalizing && (
                <span className="text-[11px] font-medium text-amber-600 dark:text-amber-300">
                  {jobInFlight ? "extracting…" : "de-duplicating…"}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              {/* View toggle: gallery (grid) vs list. */}
              <div className="inline-flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white p-0.5 dark:border-white/10 dark:bg-white/5">
                <button
                  type="button"
                  onClick={() => setViewMode("gallery")}
                  aria-pressed={viewMode === "gallery"}
                  title="Gallery view"
                  className={
                    "rounded-md px-2 py-1 transition-all " +
                    (viewMode === "gallery"
                      ? "bg-[var(--color-brand-primary)] text-white"
                      : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white")
                  }
                >
                  <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 5a1 1 0 011-1h5a1 1 0 011 1v5a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM13 5a1 1 0 011-1h5a1 1 0 011 1v5a1 1 0 01-1 1h-5a1 1 0 01-1-1V5zM4 14a1 1 0 011-1h5a1 1 0 011 1v5a1 1 0 01-1 1H5a1 1 0 01-1-1v-5zM13 14a1 1 0 011-1h5a1 1 0 011 1v5a1 1 0 01-1 1h-5a1 1 0 01-1-1v-5z" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={() => setViewMode("list")}
                  aria-pressed={viewMode === "list"}
                  title="List view"
                  className={
                    "rounded-md px-2 py-1 transition-all " +
                    (viewMode === "list"
                      ? "bg-[var(--color-brand-primary)] text-white"
                      : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white")
                  }
                >
                  <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
                  </svg>
                </button>
              </div>

              {/* Sort */}
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value as SortKey)}
                title="Sort proposed rules"
                className="h-8 rounded-lg border border-slate-200 bg-white px-2 text-xs text-slate-700 outline-none dark:border-white/10 dark:bg-white/5 dark:text-slate-200"
              >
                <option value="review">Needs review first</option>
                <option value="recent">Most recent</option>
                <option value="name">Name (A–Z)</option>
                <option value="severity">Severity</option>
                <option value="page">Page order</option>
              </select>

              {/* Toggle the document pane. */}
              <button
                type="button"
                onClick={() => setDocPaneOpen((v) => !v)}
                aria-pressed={docPaneOpen}
                className={
                  "inline-flex h-8 items-center gap-1.5 rounded-lg border px-3 text-xs font-medium transition-colors " +
                  (docPaneOpen
                    ? "border-[var(--color-brand-primary)]/40 bg-[var(--color-brand-primary)]/10 text-[var(--color-brand-primary)]"
                    : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10")
                }
              >
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                {docPaneOpen ? "Hide document" : "Show document"}
              </button>

              <button
                type="button"
                onClick={() => setFullScreen(false)}
                className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 text-xs font-medium text-slate-600 transition-colors hover:bg-slate-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
              >
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
                Close
              </button>
            </div>
          </div>

          {/* Multi-select toolbar */}
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/60 bg-slate-50/60 px-5 py-2.5 dark:border-white/8 dark:bg-white/[0.02]">
            <label className="flex cursor-pointer items-center gap-2 text-xs font-medium text-slate-600 dark:text-slate-300">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={toggleSelectAll}
                className="h-4 w-4 cursor-pointer rounded border-slate-300 text-[var(--color-brand-primary)] focus:ring-2 focus:ring-[var(--color-brand-primary)]/30 dark:border-white/20 dark:bg-white/5"
              />
              {allSelected ? "Deselect all" : "Select all"}
              <span className="text-slate-500">· {selectedCount} selected</span>
            </label>

            {/* Search — filters visible rules by name / description / page. */}
            <div className="relative min-w-[200px] flex-1 sm:max-w-xs">
              <svg
                className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" />
              </svg>
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search name, description, or page…"
                className="h-8 w-full rounded-lg border border-slate-200 bg-white pl-8 pr-7 text-xs text-slate-900 outline-none focus:border-[var(--color-brand-primary)]/40 focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/10 dark:bg-white/5 dark:text-slate-100"
              />
              {search && (
                <button
                  type="button"
                  onClick={() => setSearch("")}
                  aria-label="Clear search"
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
                >
                  <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              )}
            </div>

            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => bulkAct("approve")}
                disabled={busy || selectedCount === 0}
                className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white transition-all hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {acting && (
                  <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                )}
                Approve {selectedCount || ""}
              </button>
              <button
                type="button"
                onClick={() => bulkAct("reject")}
                disabled={busy || selectedCount === 0}
                className="inline-flex items-center rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
              >
                Reject {selectedCount || ""}
              </button>
            </div>
          </div>

          {error && (
            <div className="border-b border-rose-200/80 bg-rose-50 px-5 py-2 text-xs font-medium text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300">
              {error}
            </div>
          )}

          {/* Body — rules list, optionally split with the document pane. */}
          <div className="flex min-h-0 flex-1">
            <div
              className={
                "min-h-0 overflow-y-auto px-5 py-4 " +
                (docPaneOpen ? "w-1/2 border-r border-slate-200/60 dark:border-white/8" : "flex-1")
              }
            >
              {sortedRules.length === 0 ? (
                <p className="py-10 text-center text-sm text-slate-500 dark:text-slate-400">
                  {search
                    ? `No rules match “${search}”.`
                    : "No proposed rules."}
                </p>
              ) : (
                // Gallery = multi-column grid of cards; list = the SAME cards
                // stacked full-width (single column). Identical detail, only
                // the layout differs.
                <ul
                  className={
                    viewMode === "gallery"
                      ? "grid grid-cols-1 gap-3 " +
                        (docPaneOpen ? "xl:grid-cols-2" : "md:grid-cols-2 xl:grid-cols-3")
                      : "flex flex-col gap-3"
                  }
                >
                  {sortedRules.map((r) => (
                    <RuleRow
                      key={r.id}
                      rule={r}
                      acting={actingId === r.id || acting || locked}
                      onApprove={() => approveOne(r.id)}
                      onReject={() => rejectOne(r.id)}
                      selectable
                      selected={selectedIds.has(r.id)}
                      onToggleSelect={() => toggleSelect(r.id)}
                      onInspect={() => inspect(r)}
                      isInspected={inspectRule?.id === r.id}
                      editable={!locked}
                      isEditing={editingId === r.id}
                      onEdit={() => setEditingId(r.id)}
                      onCancelEdit={() => setEditingId(null)}
                      onSaveEdit={(data) => saveEdit(r.id, data)}
              onExpand={() => setExpandedRule(r)}
                    />
                  ))}
                </ul>
              )}
            </div>

            {docPaneOpen && (
              <div className="flex w-1/2 min-h-0 flex-col">
                <div className="flex shrink-0 items-center gap-2 border-b border-slate-200/60 px-4 py-2 text-xs dark:border-white/8">
                  <span className="font-medium text-slate-700 dark:text-slate-200">
                    {inspectRule
                      ? `Source: ${inspectRule.page_reference || "document"}`
                      : "Click a rule to jump to its source"}
                  </span>
                  {inspectRule?.source_quote && (
                    <span className="truncate italic text-slate-500 dark:text-slate-400">
                      “{inspectRule.source_quote.slice(0, 80)}…”
                    </span>
                  )}
                </div>
                <div className="min-h-0 flex-1">
                  {fileUrl ? (
                    <PdfSourceViewer
                      fileUrl={fileUrl}
                      page={inspectRule ? pageNum(inspectRule.page_reference) : 1}
                      quote={inspectRule?.source_quote}
                    />
                  ) : (
                    <div className="flex h-full items-center justify-center text-sm text-slate-500">
                      Loading document…
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>,
        document.body,
      )}

      {/* Expanded card popup — full detail at an adjustable font size (a11y). */}
      <RuleDetailDialog
        rule={expandedRule}
        locked={locked}
        acting={acting || actingId === expandedRule?.id}
        onClose={() => setExpandedRule(null)}
        onApprove={() => { if (expandedRule) { approveOne(expandedRule.id); setExpandedRule(null); } }}
        onReject={() => { if (expandedRule) { rejectOne(expandedRule.id); setExpandedRule(null); } }}
        onInspect={() => { if (expandedRule) { inspect(expandedRule); setExpandedRule(null); } }}
        onSave={async (data) => {
          if (!expandedRule) return;
          await saveEdit(expandedRule.id, data);
        }}
      />
    </>
  );
}

function RuleRow({
  rule: r,
  acting,
  onApprove,
  onReject,
  selectable = false,
  selected = false,
  onToggleSelect,
  onInspect,
  isInspected = false,
  editable = false,
  isEditing = false,
  onEdit,
  onCancelEdit,
  onSaveEdit,
  onExpand,
}: {
  rule: StandardItem;
  acting: boolean;
  onApprove: () => void;
  onReject: () => void;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
  onInspect?: () => void;
  isInspected?: boolean;
  editable?: boolean;
  isEditing?: boolean;
  onEdit?: () => void;
  onCancelEdit?: () => void;
  onSaveEdit?: (data: UpdateStandardData) => void;
  onExpand?: () => void;
}) {
  const kind = r.rule_type;

  // Inline edit form replaces the card body while editing.
  if (isEditing && onSaveEdit) {
    return (
      <li className="rounded-lg border border-[var(--color-brand-primary)]/50 bg-white p-3 ring-1 ring-[var(--color-brand-primary)]/20 dark:bg-white/[0.02]">
        <RuleEditForm
          rule={r}
          acting={acting}
          onCancel={() => onCancelEdit?.()}
          onSave={onSaveEdit}
        />
      </li>
    );
  }

  return (
    <li
      className={
        "rounded-lg border bg-white p-3 dark:bg-white/[0.02] " +
        (isInspected
          ? "border-[var(--color-brand-primary)] ring-2 ring-[var(--color-brand-primary)]/40"
          : selected
          ? "border-[var(--color-brand-primary)]/60 ring-1 ring-[var(--color-brand-primary)]/30"
          : r.needs_review
          ? "border-rose-300/60 ring-1 ring-rose-300/30 dark:border-rose-500/30"
          : "border-slate-200/80 dark:border-white/8")
      }
    >
      <div className="flex flex-wrap items-center gap-1.5">
        {selectable && (
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggleSelect}
            aria-label={`Select ${r.id}`}
            className="mr-1 h-4 w-4 shrink-0 cursor-pointer rounded border-slate-300 text-[var(--color-brand-primary)] focus:ring-2 focus:ring-[var(--color-brand-primary)]/30 dark:border-white/20 dark:bg-white/5"
          />
        )}
        <span
          className={
            "rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-normal " +
            (SEVERITY_CLS[r.severity] ||
              "bg-slate-100 text-slate-600 dark:bg-white/8 dark:text-slate-300")
          }
        >
          {r.severity}
        </span>
        {kind && (
          <span
            className={
              "rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-normal " +
              (KIND_CLS[kind] || "bg-slate-100 text-slate-600 border-slate-200")
            }
          >
            {kind}
          </span>
        )}
        {r.layer && (
          <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-500/10 dark:text-amber-300">
            {r.layer}
          </span>
        )}
        {r.needs_review && (
          <span className="ml-auto inline-flex items-center gap-1 rounded-full bg-rose-100 px-2 py-0.5 text-[10px] font-semibold text-rose-700 dark:bg-rose-500/15 dark:text-rose-300">
            {r.topology_confidence
              ? `diagram: ${r.topology_confidence} confidence`
              : "needs review"}
          </span>
        )}
        {onExpand && (
          <button
            type="button"
            onClick={onExpand}
            title="Open in a larger view (adjust font size)"
            aria-label="Expand rule"
            className={
              "inline-flex h-6 w-6 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-white/10 dark:hover:text-slate-200 " +
              (r.needs_review ? "" : "ml-auto")
            }
          >
            <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
            </svg>
          </button>
        )}
      </div>

      <p className="mt-1.5 text-sm font-medium text-slate-900 dark:text-slate-100">{r.name}</p>
      <p className="mt-0.5 text-xs leading-relaxed text-slate-600 dark:text-slate-400">
        {r.description}
      </p>

      {/* Source location — where to find/verify this rule in the document. */}
      <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-slate-500 dark:text-slate-400">
        {r.page_reference && (
          <span className="inline-flex items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 font-medium dark:bg-white/8">
            <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            {r.page_reference}
          </span>
        )}
        {r.parent_rule_id && (
          <span className="font-mono">↳ parent {r.parent_rule_id}</span>
        )}
      </div>

      {r.source_quote ? (
        <p className="mt-1.5 border-l-2 border-slate-200 pl-2 text-[11px] italic leading-relaxed text-slate-500 dark:border-white/10 dark:text-slate-400">
          “{r.source_quote}”
        </p>
      ) : (
        <p className="mt-1.5 text-[11px] italic text-rose-600/80 dark:text-rose-300/70">
          No verbatim source quote — model-inferred; verify against the document.
        </p>
      )}

      <div className="mt-2.5 flex gap-2">
        <button
          type="button"
          onClick={onApprove}
          disabled={acting}
          className="rounded-md bg-emerald-600 px-2.5 py-1 text-[11px] font-semibold text-white transition-colors hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Approve
        </button>
        <button
          type="button"
          onClick={onReject}
          disabled={acting}
          className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
        >
          Reject
        </button>
        {editable && onEdit && (
          <button
            type="button"
            onClick={onEdit}
            disabled={acting}
            className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
            title="Edit this rule before approving"
          >
            Edit
          </button>
        )}
        {onInspect && (
          <button
            type="button"
            onClick={onInspect}
            className="ml-auto inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-semibold text-[var(--color-brand-primary)] transition-colors hover:bg-[var(--color-brand-primary)]/10"
            title="Open the document at this rule's source location"
          >
            <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
            </svg>
            View source
          </button>
        )}
      </div>
    </li>
  );
}

const SEVERITIES = ["critical", "high", "medium", "low"] as const;
const ALL_TIERS = [1, 2, 3, 4, 5];

/** Inline edit form for a proposed rule. Sends only changed fields (PATCH
 *  semantics) so an unchanged blank Mode-B field isn't forced. */
function RuleEditForm({
  rule: r,
  acting,
  onCancel,
  onSave,
}: {
  rule: StandardItem;
  acting: boolean;
  onCancel: () => void;
  onSave: (data: UpdateStandardData) => void;
}) {
  const [name, setName] = useState(r.name || "");
  const [description, setDescription] = useState(r.description || "");
  const [severity, setSeverity] = useState(r.severity || "medium");
  const [category, setCategory] = useState(r.category || "");
  const [criteria, setCriteria] = useState(r.validation_criteria || "");
  const [layer, setLayer] = useState<string>(r.layer || "");
  const [ruleType, setRuleType] = useState<string>(r.rule_type || "");
  const [tiers, setTiers] = useState<number[]>(
    r.applies_to_tiers && r.applies_to_tiers.length ? r.applies_to_tiers : ALL_TIERS,
  );

  const toggleTier = (t: number) =>
    setTiers((prev) => (prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t].sort()));

  const submit = () => {
    const data: UpdateStandardData = {
      name: name.trim(),
      description: description.trim(),
      severity: severity as UpdateStandardData["severity"],
      category: category.trim(),
      validation_criteria: criteria.trim(),
      applies_to_tiers: tiers.length ? tiers : ALL_TIERS,
    };
    // Mode-B fields: only send when set (backend treats empty as no-change).
    if (layer) data.layer = layer as UpdateStandardData["layer"];
    if (ruleType) data.rule_type = ruleType as UpdateStandardData["rule_type"];
    onSave(data);
  };

  // em-relative sizing so the form scales with the popup's base font size
  // (the expanded/accessible view sets a larger px on the container). In the
  // inline-card use the container is the normal document size, so 0.8em ≈ the
  // previous text-xs — visually unchanged there.
  const field = "w-full rounded-md border border-slate-200 bg-white px-2 py-1 text-[0.8em] text-slate-900 outline-none focus:border-[var(--color-brand-primary)]/40 focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/10 dark:bg-white/5 dark:text-slate-100";
  const label = "mb-0.5 block text-[0.68em] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400";

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[0.68em] font-semibold uppercase tracking-normal text-[var(--color-brand-primary)]">
          Editing {r.id}
        </span>
      </div>
      <div>
        <label className={label}>Name</label>
        <input className={field} value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div>
        <label className={label}>Description</label>
        <textarea className={field + " min-h-[48px]"} value={description} onChange={(e) => setDescription(e.target.value)} />
      </div>
      <div>
        <label className={label}>Validation criteria</label>
        <textarea className={field + " min-h-[48px]"} value={criteria} onChange={(e) => setCriteria(e.target.value)} />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className={label}>Severity</label>
          <select className={field} value={severity} onChange={(e) => setSeverity(e.target.value)}>
            {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <div>
          <label className={label}>Category</label>
          <input className={field} value={category} onChange={(e) => setCategory(e.target.value)} />
        </div>
        <div>
          <label className={label}>Layer</label>
          <select className={field} value={layer} onChange={(e) => setLayer(e.target.value)}>
            <option value="">(unset)</option>
            {LAYER_VALUES.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </div>
        <div>
          <label className={label}>Rule type</label>
          <select className={field} value={ruleType} onChange={(e) => setRuleType(e.target.value)}>
            <option value="">(unset)</option>
            {RULE_TYPE_VALUES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
      </div>
      <div>
        <label className={label}>Applies to tiers</label>
        <div className="flex gap-1">
          {ALL_TIERS.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => toggleTier(t)}
              className={
                "flex h-[1.6em] w-[1.6em] items-center justify-center rounded text-[0.75em] font-semibold transition-colors " +
                (tiers.includes(t)
                  ? "bg-[var(--color-brand-primary)] text-white"
                  : "border border-slate-200 bg-white text-slate-500 dark:border-white/15 dark:bg-white/5 dark:text-slate-400")
              }
            >
              {t}
            </button>
          ))}
        </div>
      </div>
      <div className="flex gap-2 pt-1">
        <button
          type="button"
          onClick={submit}
          disabled={acting || !name.trim() || !criteria.trim()}
          className="rounded-md bg-[var(--color-brand-primary)] px-3 py-1 text-[0.78em] font-semibold text-white transition-colors hover:bg-[var(--color-brand-primary-hover)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {acting ? "Saving…" : "Save changes"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={acting}
          className="rounded-md border border-slate-200 bg-white px-3 py-1 text-[0.78em] font-semibold text-slate-700 transition-colors hover:bg-slate-50 disabled:opacity-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

// Font-size preference for the expanded card, persisted so a low-vision user
// sets it once. Clamped to a sensible range.
const FONT_MIN = 12;
const FONT_MAX = 26;
const FONT_DEFAULT = 15;
const FONT_KEY = "review-card-font-size";

function readFontPref(): number {
  if (typeof window === "undefined") return FONT_DEFAULT;
  const raw = parseInt(window.localStorage.getItem(FONT_KEY) || "", 10);
  if (Number.isFinite(raw) && raw >= FONT_MIN && raw <= FONT_MAX) return raw;
  return FONT_DEFAULT;
}

/** Expanded, accessible view of one proposed rule. Renders the full detail
 *  at a user-adjustable base font size (em-relative markup) so the whole card
 *  scales together. Actions mirror the row (approve / reject / edit / view
 *  source), disabled while the batch is locked. */
function RuleDetailDialog({
  rule: r,
  locked,
  acting,
  onClose,
  onApprove,
  onReject,
  onInspect,
  onSave,
}: {
  rule: StandardItem | null;
  locked: boolean;
  acting: boolean;
  onClose: () => void;
  onApprove: () => void;
  onReject: () => void;
  onInspect: () => void;
  onSave: (data: UpdateStandardData) => Promise<void> | void;
}) {
  // Lazy initializer reads the saved preference once (no effect → no
  // cascading-render lint warning; SSR-safe via the typeof guard in readFontPref).
  const [fontSize, setFontSize] = useState<number>(readFontPref);
  // Which rule id is being edited IN this popup. Tracking by id (rather than a
  // bool) means it auto-resets when a different rule is opened — no effect
  // needed. Editing happens inline here so the popup never closes to edit.
  const [editingId, setEditingId] = useState<string | null>(null);
  const editing = r !== null && editingId === r.id;

  const setFont = (next: number) => {
    const clamped = Math.min(FONT_MAX, Math.max(FONT_MIN, next));
    setFontSize(clamped);
    try {
      window.localStorage.setItem(FONT_KEY, String(clamped));
    } catch {
      // preference is best-effort
    }
  };

  const kind = r?.rule_type;

  const close = () => {
    setEditingId(null);
    onClose();
  };

  return (
    <Dialog open={r !== null} onOpenChange={(o) => { if (!o) close(); }}>
      <DialogContent className="sm:max-w-2xl">
        {r && (
          <>
            <DialogHeader>
              <div className="flex items-center justify-between gap-3">
                <DialogTitle className="text-sm">
                  {editing ? "Edit proposed rule" : "Proposed rule"}
                </DialogTitle>
                {/* Font-size controls */}
                <div className="mr-6 flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => setFont(fontSize - 2)}
                    disabled={fontSize <= FONT_MIN}
                    aria-label="Decrease font size"
                    title="Decrease font size"
                    className="inline-flex h-6 w-6 items-center justify-center rounded-md border border-slate-200 bg-white text-[11px] font-semibold text-slate-600 transition-colors hover:bg-slate-50 disabled:opacity-40 dark:border-white/15 dark:bg-white/5 dark:text-slate-300"
                  >
                    A−
                  </button>
                  <span className="w-8 text-center text-[11px] tabular-nums text-slate-500 dark:text-slate-400">
                    {fontSize}px
                  </span>
                  <button
                    type="button"
                    onClick={() => setFont(fontSize + 2)}
                    disabled={fontSize >= FONT_MAX}
                    aria-label="Increase font size"
                    title="Increase font size"
                    className="inline-flex h-6 w-6 items-center justify-center rounded-md border border-slate-200 bg-white text-[13px] font-semibold text-slate-600 transition-colors hover:bg-slate-50 disabled:opacity-40 dark:border-white/15 dark:bg-white/5 dark:text-slate-300"
                  >
                    A+
                  </button>
                </div>
              </div>
            </DialogHeader>

            {/* Editor branch — hosted IN the popup so editing never closes it,
                and it inherits the same adjustable font size. */}
            {editing ? (
              <div
                className="max-h-[68vh] overflow-y-auto pr-1"
                style={{ fontSize: `${fontSize}px` }}
              >
                <RuleEditForm
                  rule={r}
                  acting={acting}
                  onCancel={() => setEditingId(null)}
                  onSave={async (data) => {
                    await onSave(data);
                    close();
                  }}
                />
              </div>
            ) : (
            <>
            {/* Scalable body — base font size drives all em-relative text. */}
            <div
              className="max-h-[60vh] overflow-y-auto pr-1"
              style={{ fontSize: `${fontSize}px`, lineHeight: 1.55 }}
            >
              <div className="flex flex-wrap items-center gap-1.5" style={{ fontSize: "0.7em" }}>
                <span
                  className={
                    "rounded-full px-2 py-0.5 font-semibold uppercase tracking-normal " +
                    (SEVERITY_CLS[r.severity] ||
                      "bg-slate-100 text-slate-600 dark:bg-white/8 dark:text-slate-300")
                  }
                >
                  {r.severity}
                </span>
                {kind && (
                  <span
                    className={
                      "rounded-full border px-2 py-0.5 font-semibold uppercase tracking-normal " +
                      (KIND_CLS[kind] || "bg-slate-100 text-slate-600 border-slate-200")
                    }
                  >
                    {kind}
                  </span>
                )}
                {r.layer && (
                  <span className="rounded-full bg-amber-50 px-2 py-0.5 font-medium text-amber-700 dark:bg-amber-500/10 dark:text-amber-300">
                    {r.layer}
                  </span>
                )}
                {r.category && (
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 font-medium text-slate-600 dark:bg-white/8 dark:text-slate-300">
                    {r.category}
                  </span>
                )}
                {r.applies_to_tiers && r.applies_to_tiers.length > 0 && (
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 font-medium text-slate-600 dark:bg-white/8 dark:text-slate-300">
                    Tiers {r.applies_to_tiers.join(", ")}
                  </span>
                )}
                {r.needs_review && (
                  <span className="rounded-full bg-rose-100 px-2 py-0.5 font-semibold text-rose-700 dark:bg-rose-500/15 dark:text-rose-300">
                    {r.topology_confidence
                      ? `diagram: ${r.topology_confidence} confidence`
                      : "needs review"}
                  </span>
                )}
              </div>

              <p className="mt-3 font-semibold text-slate-900 dark:text-slate-100">{r.name}</p>
              <p className="mt-1 leading-relaxed text-slate-700 dark:text-slate-300">
                {r.description}
              </p>

              <div className="mt-3" style={{ fontSize: "0.85em" }}>
                <p className="font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400" style={{ fontSize: "0.8em" }}>
                  Validation criteria
                </p>
                <p className="mt-0.5 leading-relaxed text-slate-700 dark:text-slate-300">
                  {r.validation_criteria}
                </p>
              </div>

              <div className="mt-3 flex flex-wrap items-center gap-3 text-slate-500 dark:text-slate-400" style={{ fontSize: "0.8em" }}>
                {r.page_reference && (
                  <span className="inline-flex items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 font-medium dark:bg-white/8">
                    {r.page_reference}
                  </span>
                )}
                {r.parent_rule_id && <span className="font-mono">↳ parent {r.parent_rule_id}</span>}
                <span className="font-mono">{r.id}</span>
              </div>

              {r.source_quote ? (
                <div className="mt-3" style={{ fontSize: "0.9em" }}>
                  <p className="font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400" style={{ fontSize: "0.78em" }}>
                    Source quote
                  </p>
                  <p className="mt-0.5 border-l-2 border-slate-200 pl-2 italic leading-relaxed text-slate-600 dark:border-white/10 dark:text-slate-400">
                    “{r.source_quote}”
                  </p>
                </div>
              ) : (
                <p className="mt-3 italic text-rose-600/80 dark:text-rose-300/70" style={{ fontSize: "0.85em" }}>
                  No verbatim source quote — model-inferred; verify against the document.
                </p>
              )}
            </div>

            {/* Actions */}
            <div className="mt-4 flex flex-wrap gap-2 border-t border-slate-200/60 pt-3 dark:border-white/8">
              <button
                type="button"
                onClick={onApprove}
                disabled={acting || locked}
                className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Approve
              </button>
              <button
                type="button"
                onClick={onReject}
                disabled={acting || locked}
                className="rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
              >
                Reject
              </button>
              <button
                type="button"
                onClick={() => r && setEditingId(r.id)}
                disabled={acting || locked}
                className="rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
              >
                Edit
              </button>
              <button
                type="button"
                onClick={onInspect}
                className="ml-auto inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-xs font-semibold text-[var(--color-brand-primary)] transition-colors hover:bg-[var(--color-brand-primary)]/10"
              >
                View source
              </button>
            </div>
            </>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
