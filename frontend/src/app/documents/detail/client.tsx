// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { DiagramGallery, DiagramItem } from "@/components/documents/DiagramGallery";
import { SlideStrip } from "@/components/documents/SlideStrip";
import { ExtractionLogPanel } from "@/components/extraction/ExtractionLogPanel";
import { ExtractionReviewBatch } from "@/components/extraction/ExtractionReviewBatch";
import { ValidationConfigDialog } from "@/components/validation/ValidationConfigDialog";
import { api, DocumentDetailResponse, StandardItem } from "@/lib/api";
import { useRole } from "@/lib/use-role";

function DocumentDetailInner() {
  const searchParams = useSearchParams();
  // Resolve the document id robustly. Under `output: "export"` (static
  // export), useSearchParams() can return a stale-empty value on a client-
  // side (soft) navigation — e.g. arriving here straight from the upload
  // flow — which left `id=""`, so the fetch effect early-returned and the
  // page froze at "Loading document…" until a hard refresh. Fall back to
  // window.location.search (always correct in the browser) and keep it in
  // state so a late searchParams update or a popstate still re-resolves.
  // Resolve at RENDER time (not in an effect): prefer the hook value, fall
  // back to window.location.search. Under `output: "export"`, the hook can
  // lag on a soft navigation, leaving id="" and freezing the page at
  // "Loading document…"; the location fallback is always correct in the
  // browser, and useSearchParams already re-renders when the hook catches up.
  const id =
    searchParams.get("id") ||
    (typeof window !== "undefined"
      ? new URLSearchParams(window.location.search).get("id") || ""
      : "");

  const [document, setDocument] = useState<DocumentDetailResponse | null>(null);
  const [diagrams, setDiagrams] = useState<DiagramItem[]>([]);
  const [diagramsLoading, setDiagramsLoading] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [validating, setValidating] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [validationSuccess, setValidationSuccess] = useState<string | null>(null);
  const [activeValidationId, setActiveValidationId] = useState<string | null>(null);
  const [validationStatus, setValidationStatus] = useState<string | null>(null);
  const [validationProgress, setValidationProgress] = useState<{ evaluated: number; total: number } | null>(null);
  const [validationStartedAt, setValidationStartedAt] = useState<number | null>(null);
  const [validationElapsed, setValidationElapsed] = useState<number>(0);
  const [configOpen, setConfigOpen] = useState(false);
  const [fileUrl, setFileUrl] = useState<string>("");
  const [savingTier, setSavingTier] = useState(false);
  const [tierError, setTierError] = useState<string | null>(null);
  const { email: myEmail, isAdmin } = useRole();
  // Mirrors require_owner_or_admin on PATCH /api/documents/{id}/tier: only the
  // uploader (or an admin) may retier, because the tier selects which rules
  // this SDD is judged against and so changes the verdict. Documents with no
  // recorded uploader predate the control and stay editable.
  const docOwner = (document?.uploaded_by || "").trim().toLowerCase();
  const canEditTier =
    isAdmin || !docOwner || docOwner === (myEmail || "").trim().toLowerCase();
  // Rules extracted FROM this document — only fetched when this doc is
  // a standards document. Drives the "Extraction summary" panel that
  // gives users post-hoc visibility into what the agent produced.
  const [extractedRules, setExtractedRules] = useState<StandardItem[]>([]);
  // Proposed rules still awaiting review. They are not in the library yet, so
  // listStandards() omits them; counting only active rules made a fresh
  // extraction read "0 rules — the agent didn't produce any" right below its
  // own pending batch.
  const [pendingRuleCount, setPendingRuleCount] = useState(0);
  const [extractionLoading, setExtractionLoading] = useState(false);
  const [extractionInProgress, setExtractionInProgress] = useState(false);

  useEffect(() => {
    if (!id) return;
    async function fetchDocument() {
      try { const doc = await api.getDocument(id); setDocument(doc); }
      catch (err) { setError(err instanceof Error ? err.message : "Failed to load document"); }
      finally { setLoading(false); }
    }
    async function fetchDiagrams() {
      try { const diags = await api.getDocumentDiagrams(id); setDiagrams(diags); }
      catch { setDiagrams([]); }
      finally { setDiagramsLoading(false); }
    }
    fetchDocument();
    fetchDiagrams();
  }, [id]);

  // Persist a tier override and reflect the change locally so the
  // status row updates without a full refetch.
  const handleTierChange = async (next: number | null) => {
    if (!document) return;
    setSavingTier(true);
    setTierError(null);
    try {
      const result = await api.setDocumentTier(document.document_id, next);
      setDocument({
        ...document,
        declared_tier: result.declared_tier,
        tier_source: result.tier_source,
      });
    } catch (err) {
      // Surfaced, not just logged: the backend refuses a tier change on someone
      // else's document, and a silently-ignored click looks like a broken page.
      setTierError(err instanceof Error ? err.message : "Tier change failed");
    } finally {
      setSavingTier(false);
    }
  };

  // Fetch the document binary with auth, expose as a blob URL for <iframe>/<a>.
  // Plain `<iframe src=…/api/documents/x/pdf>` doesn't carry the bearer token.
  useEffect(() => {
    if (!id) return;
    let revoked = false;
    let url = "";
    api.getDocumentBlobUrl(id)
      .then((u) => { if (!revoked) { url = u; setFileUrl(u); } })
      .catch(() => {});
    return () => { revoked = true; if (url) URL.revokeObjectURL(url); };
  }, [id]);

  // Fetch rules extracted from THIS standards document. Polls while
  // the agent is plausibly still running. Crucially we keep polling
  // even after the document reaches `parsed`, because the standards
  // extractor runs AFTER parsing completes — the doc reaches parsed
  // ~10s after upload but rules may keep appearing for another minute
  // or two. Stops once we see the rule count hold steady for several
  // polls in a row (a heuristic: the agent is done emitting), or after
  // a hard 5-minute ceiling.
  // Drive polling off the live extraction-status job registry, not
  // document.status. The doc transitions to status="parsed" ~10s after
  // upload but extraction + enrichment can run for another 60-180s;
  // gating on document.status alone freezes the rule list mid-
  // enrichment until the user refreshes.
  //
  // Strategy:
  // 1. Poll BOTH the document itself AND the extraction-status job
  //    every 3s.
  // 2. Refresh the rule list every 3s while the job is in-flight
  //    (status ∈ {starting, extracting, enriching}).
  // 3. After the job lands on completed/failed, do one final rule
  //    fetch and stop polling.
  // 4. If no job exists for this doc (e.g. an older upload from before
  //    the live log existed), do a single rule fetch and stop.
  useEffect(() => {
    if (!id) return;
    const isStandards = document?.document_type === "standards";

    let cancelled = false;
    let interval: ReturnType<typeof setInterval> | undefined;
    const POLL_MS = 3000;
    const MAX_TICKS = 120; // 120 × 3s = 6 min hard ceiling
    let totalTicks = 0;
    let jobSawTerminal = false;

    const fetchRules = async () => {
      try {
        const res = await api.listStandards();
        if (cancelled) return;
        const docId = document?.document_id ?? id;
        const mine = res.standards.filter(
          (s) =>
            s.source === "document" &&
            s.source_document &&
            s.source_document.includes(docId),
        );
        setExtractedRules(mine);
        const batches = await api.listExtractionBatches(docId);
        if (cancelled) return;
        setPendingRuleCount(
          batches.batches
            .filter((b) => b.status === "pending")
            .reduce((n, b) => n + (b.total || 0), 0),
        );
      } catch {
        // best-effort
      } finally {
        if (!cancelled) setExtractionLoading(false);
      }
    };

    const refreshDocument = async () => {
      try {
        const fresh = await api.getDocument(id);
        if (cancelled) return;
        setDocument(fresh);
      } catch {
        // best-effort — stale state is acceptable, missing doc is
        // surfaced via the initial fetch's error path
      }
    };

    const tick = async () => {
      if (cancelled) return;
      totalTicks += 1;
      if (totalTicks >= MAX_TICKS) {
        if (interval) clearInterval(interval);
        interval = undefined;
        setExtractionInProgress(false);
        return;
      }
      try {
        const status = await api.getExtractionStatus({ documentId: id });
        if (cancelled) return;
        const job = status.job;
        const inFlight =
          job &&
          (job.status === "starting" ||
            job.status === "extracting" ||
            job.status === "enriching");
        const terminal =
          job && (job.status === "completed" || job.status === "failed");

        setExtractionInProgress(Boolean(inFlight));

        // Always refresh rules while in flight, plus exactly once
        // when transitioning to terminal so the final attachment
        // batch from Phase B lands.
        if (inFlight) {
          await fetchRules();
        } else if (terminal && !jobSawTerminal) {
          jobSawTerminal = true;
          await fetchRules();
          await refreshDocument();
          if (interval) clearInterval(interval);
          interval = undefined;
          return;
        } else if (!job) {
          // No job for this doc — single fetch already happened in
          // the kick-off below. Stop the loop.
          if (interval) clearInterval(interval);
          interval = undefined;
          return;
        }
      } catch {
        // best-effort; keep polling
      }
    };

    // Defer the kick-off so this effect only coordinates the external
    // polling lifecycle; state updates happen from an async callback.
    const kickoff = setTimeout(() => {
      if (cancelled) return;
      setExtractionInProgress(isStandards);
      setExtractionLoading(true);
      void fetchRules();

      if (isStandards) {
        void tick();
        interval = setInterval(tick, POLL_MS);
      } else {
        setExtractionInProgress(false);
        setExtractionLoading(false);
      }
    }, 0);

    return () => {
      cancelled = true;
      if (kickoff) clearTimeout(kickoff);
      if (interval) clearInterval(interval);
    };
  }, [id, document?.document_type, document?.document_id]);

  useEffect(() => {
    if (!activeValidationId) return;

    const poll = async () => {
      try {
        const val = await api.getValidation(activeValidationId);
        setValidationStatus(val.status);
        if (val.evaluated_count !== undefined && val.total_standards !== undefined) {
          setValidationProgress({
            evaluated: val.evaluated_count,
            total: val.total_standards,
          });
        }
        if (val.status !== "pending") {
          setValidating(false);
        }
      } catch {}
    };
    void poll();
    const interval = setInterval(poll, 2000);
    return () => clearInterval(interval);
  }, [activeValidationId]);

  // Tick the elapsed-time display once per second while a validation is pending.
  useEffect(() => {
    if (validationStatus !== "pending" || !validationStartedAt) return;
    const tick = setInterval(() => {
      setValidationElapsed(Math.floor((Date.now() - validationStartedAt) / 1000));
    }, 1000);
    return () => clearInterval(tick);
  }, [validationStatus, validationStartedAt]);

  if (!id) return (
    <div className="flex h-64 items-center justify-center">
      <p className="text-sm text-slate-500 dark:text-slate-400">No document ID provided</p>
    </div>
  );
  if (loading) return (
    <div className="flex h-64 items-center justify-center">
      <div className="text-center">
        <div className="mx-auto h-7 w-7 animate-spin rounded-full border-[2px] border-slate-200 border-t-[var(--color-brand-primary)] dark:border-white/10 dark:border-t-[var(--color-brand-primary)]" />
        <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">Loading document…</p>
      </div>
    </div>
  );
  if (error || !document) return (
    <div className="flex h-64 items-center justify-center">
      <div className="text-center">
        <p className="text-sm font-medium text-rose-700 dark:text-rose-300">{error || "Document not found"}</p>
        <Link href="/documents" className="mt-3 inline-block text-sm font-medium text-[var(--color-brand-primary)] transition-colors hover:text-[var(--color-brand-primary-hover)]">
          ← Back to Documents
        </Link>
      </div>
    </div>
  );

  const statusLabel = document.status === "parsed" || document.status === "analyzed" ? "Ready" : document.status === "parsing" || document.status === "analyzing" ? "Parsing" : document.status === "failed" ? "Error" : "Uploaded";
  const statusTone = statusLabel === "Ready"
    ? "bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20"
    : statusLabel === "Parsing"
    ? "bg-sky-50 text-sky-700 border-sky-200/80 dark:bg-sky-500/10 dark:text-sky-300 dark:border-sky-500/20"
    : statusLabel === "Error"
    ? "bg-rose-50 text-rose-700 border-rose-200/80 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20"
    : "bg-slate-100 text-slate-600 border-slate-200 dark:bg-white/8 dark:text-slate-300 dark:border-white/10";
  const isSDDDocument = !document.document_type || document.document_type === "sdd";
  // PPTX uploads have a converted .pdf at the same
  // /api/documents/{id}/pdf endpoint as native PDF uploads, so the
  // preview iframe works for both. Gate on filename OR
  // source_format so we don't accidentally hide the preview for
  // a .pptx-named row.
  const isPDF =
    /\.pdf$/i.test(document.filename) ||
    /\.pptx$/i.test(document.filename) ||
    document.source_format === "pdf" ||
    document.source_format === "pptx";
  const sizeLabel = document.file_size_bytes
    ? document.file_size_bytes / 1024 > 1024
      ? (document.file_size_bytes / 1048576).toFixed(1) + " MB"
      : Math.round(document.file_size_bytes / 1024) + " KB"
    : "N/A";

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      {/* Header */}
      <div className="space-y-3">
        <Link
          href="/documents"
          className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 transition-colors hover:text-[var(--color-brand-primary)] dark:text-slate-400"
        >
          <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Documents
        </Link>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 space-y-1.5">
            <h1 className="break-words text-2xl font-semibold tracking-normal text-slate-900 dark:text-slate-50">
              {document.filename}
            </h1>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline" className={statusTone + " text-[10px] font-semibold uppercase tracking-normal"}>
                {statusLabel}
              </Badge>
              {/* Tier control — SDDs only. The validator only runs
                  rules whose applies_to_tiers contains this value, so
                  it's the most consequential field on the page. */}
              {isSDDDocument && (
                <div className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2 py-0.5 text-[11px] dark:border-white/15 dark:bg-white/5">
                  <span className="font-medium text-slate-500 dark:text-slate-400">Tier</span>
                  <select
                    value={document.declared_tier ?? ""}
                    onChange={(e) => {
                      const v = e.target.value;
                      handleTierChange(v === "" ? null : parseInt(v, 10));
                    }}
                    disabled={savingTier || !canEditTier}
                    title={
                      canEditTier
                        ? undefined
                        : "Only the person who uploaded this document (or an admin) can change its tier."
                    }
                    className="bg-transparent text-[11px] font-semibold text-slate-700 outline-none dark:text-slate-200 disabled:opacity-50"
                    aria-label="Service tier"
                  >
                    <option value="">Unknown</option>
                    <option value="1">1 — Critical</option>
                    <option value="2">2</option>
                    <option value="3">3</option>
                    <option value="4">4</option>
                    <option value="5">5 — Lowest</option>
                  </select>
                  {document.tier_source === "auto" && (
                    <span
                      className="text-[9px] uppercase tracking-normal text-slate-400 dark:text-slate-500"
                      title="Auto-detected by the AI classifier. Override above if incorrect."
                    >
                      auto
                    </span>
                  )}
                  {document.tier_source === "user" && (
                    <span
                      className="text-[9px] uppercase tracking-normal text-[var(--color-brand-primary)]"
                      title="Manually set."
                    >
                      manual
                    </span>
                  )}
                </div>
              )}
              <span className="font-mono text-[11px] text-slate-400 dark:text-slate-500">{document.document_id}</span>
            </div>
            {tierError && (
              <p className="text-[11px] text-[var(--color-status-danger)]">{tierError}</p>
            )}
            {/* Audit provenance — who uploaded this + when. */}
            <p className="text-[11px] text-slate-500 dark:text-slate-400">
              Uploaded
              {document.uploaded_by ? (
                <> by <span className="font-medium text-slate-700 dark:text-slate-300">{document.uploaded_by}</span></>
              ) : null}
              {document.uploaded_at ? (
                <> · {new Date(document.uploaded_at).toLocaleString()}</>
              ) : null}
            </p>
          </div>
          {isSDDDocument && (
            <button
              onClick={() => {
                setValidationError(null);
                setConfigOpen(true);
              }}
              disabled={validating || validationStatus === "pending"}
              className="inline-flex shrink-0 items-center gap-2 rounded-lg bg-[var(--color-brand-primary)] px-4 py-2 text-sm font-semibold text-white shadow-sm shadow-[var(--color-brand-primary)]/20 transition-all hover:bg-[var(--color-brand-primary-hover)] hover:shadow-md hover:shadow-[var(--color-brand-primary)]/25 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-[var(--color-brand-primary)]"
            >
              <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              {validating ? "Validating…" : "Run Validation"}
            </button>
          )}
        </div>
      </div>

      {/* Live extraction log. Renders for any document
          that has an in-flight or recently-completed extraction job.
          Hides itself when no job exists for this id. */}
      <ExtractionLogPanel documentId={id} />

      {/* Review gate — proposed rules from this document's extraction,
          awaiting approval before they enter the active library. Hides
          itself when there's no pending batch. */}
      <ExtractionReviewBatch documentId={id} />

      {/* Validation Status Banners */}
      {validationSuccess && validationStatus === "pending" && (
        <div className="rounded-xl border border-sky-200/80 bg-sky-50/60 px-5 py-4 dark:border-sky-500/20 dark:bg-sky-500/[0.08]">
          <div className="flex items-start justify-between gap-4">
            <div className="flex min-w-0 items-start gap-3">
              <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-sky-500/15 ring-1 ring-sky-500/20">
                <svg className="h-3.5 w-3.5 animate-spin text-sky-600 dark:text-sky-300" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              </span>
              <div className="min-w-0 space-y-0.5">
                <p className="text-sm font-semibold text-sky-700 dark:text-sky-200">
                  Validating against HA standards
                  {validationProgress && validationProgress.total > 0 && (
                    <span className="ml-2 font-mono text-xs font-normal tabular-nums text-sky-600 dark:text-sky-300">
                      {validationProgress.evaluated} / {validationProgress.total}
                    </span>
                  )}
                </p>
                <p className="text-xs text-slate-600 dark:text-slate-400">
                  Typically completes in ~30 seconds · {validationElapsed}s elapsed · safe to leave this page.
                </p>
              </div>
            </div>
            <Link
              href={"/validation/detail?id=" + validationSuccess}
              className="shrink-0 text-xs font-medium text-sky-700 transition-colors hover:text-sky-900 dark:text-sky-300 dark:hover:text-sky-100"
            >
              View progress →
            </Link>
          </div>
          {validationProgress && validationProgress.total > 0 && (
            <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-sky-500/15 dark:bg-sky-500/20">
              <div
                className="h-full rounded-full bg-sky-500 transition-all duration-500 ease-out"
                style={{ width: `${Math.round((validationProgress.evaluated / validationProgress.total) * 100)}%` }}
              />
            </div>
          )}
        </div>
      )}
      {validationSuccess && validationStatus === "completed" && (
        <div className="flex items-center justify-between gap-4 rounded-xl border border-emerald-200/80 bg-emerald-50/60 px-5 py-3.5 dark:border-emerald-500/20 dark:bg-emerald-500/[0.08]">
          <div className="flex items-center gap-3">
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-emerald-500/15 ring-1 ring-emerald-500/20">
              <svg className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
            </span>
            <span className="text-sm font-semibold text-emerald-700 dark:text-emerald-200">Validation complete</span>
          </div>
          <Link
            href={"/validation/detail?id=" + validationSuccess}
            className="text-xs font-medium text-emerald-700 transition-colors hover:text-emerald-900 dark:text-emerald-300 dark:hover:text-emerald-100"
          >
            View results →
          </Link>
        </div>
      )}
      {validationSuccess && validationStatus === "failed" && (
        <div className="flex items-center justify-between gap-4 rounded-xl border border-rose-200/80 bg-rose-50/60 px-5 py-3.5 dark:border-rose-500/20 dark:bg-rose-500/[0.08]">
          <div className="flex items-center gap-3">
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-rose-500/15 ring-1 ring-rose-500/20">
              <svg className="h-3.5 w-3.5 text-rose-600 dark:text-rose-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </span>
            <span className="text-sm font-semibold text-rose-700 dark:text-rose-200">Validation failed. Try running it again.</span>
          </div>
          <Link
            href={"/validation/detail?id=" + validationSuccess}
            className="text-xs font-medium text-rose-700 transition-colors hover:text-rose-900 dark:text-rose-300 dark:hover:text-rose-100"
          >
            View details →
          </Link>
        </div>
      )}
      {validationError && (
        <div className="rounded-xl border border-rose-200/80 bg-rose-50/60 px-5 py-3.5 text-sm font-medium text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/[0.08] dark:text-rose-300">
          {validationError}
        </div>
      )}

      {/* Document Overview metadata strip — for standards docs we
          replace "Diagrams" with "Rules extracted" since that's the
          actionable signal here. */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-4">
        <MetaTile label="Pages" value={document.page_count?.toString() || "N/A"} />
        <MetaTile label="File size" value={sizeLabel} />
        <MetaTile label="Sections" value={(document.sections?.length || 0).toString()} />
        {isSDDDocument ? (
          <MetaTile label="Diagrams" value={diagrams.length.toString()} />
        ) : (
          <MetaTile label="Rules extracted" value={(extractedRules.length + pendingRuleCount).toString()} />
        )}
      </div>

      {/* Extraction Summary — standards docs only. Shows what the AI
          produced from this document so the user can verify the
          translation from raw text → rules looks right. The parsed
          sections panel below this acts as the "what the agent saw"
          source-of-truth side. */}
      {!isSDDDocument && (
        <ExtractionSummary
          rules={extractedRules}
          pendingCount={pendingRuleCount}
          loading={extractionLoading}
          inProgress={extractionInProgress}
        />
      )}

      {/* Extracted Diagrams — SDDs only. */}
      {isSDDDocument && (
        <DiagramGallery diagrams={diagrams} loading={diagramsLoading} documentStatus={document.status} pageCount={document.page_count || 0} />
      )}

      {/* Main Content: PDF gets side-by-side with sections; non-PDF just shows sections */}
      {isPDF ? (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* PDF Viewer - 2/3 width */}
          <Card className="lg:col-span-2">
            <CardHeader className="flex flex-row items-center justify-between px-5 pt-4 pb-3">
              <CardTitle className="text-sm font-semibold">
                Document preview
                {document.source_format === "pptx" && (
                  <span className="ml-2 text-[10px] font-normal text-slate-500 dark:text-slate-400">
                    (PowerPoint converted to PDF)
                  </span>
                )}
              </CardTitle>
              <a
                href={fileUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-[11px] font-medium text-[var(--color-brand-primary)] transition-colors hover:text-[var(--color-brand-primary-hover)]"
              >
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                </svg>
                Open in new tab
              </a>
            </CardHeader>
            <CardContent className="p-0">
              <iframe
                src={fileUrl}
                className="h-[700px] w-full rounded-b-xl border-t border-slate-200/60 dark:border-white/8"
                title={document.source_format === "pptx" ? "Slide preview (converted PDF)" : "PDF Preview"}
              />
            </CardContent>
          </Card>

          {/* Right rail (1/3 width):
              - PPTX uploads: Slide Strip with thumbnails + per-slide
                rule counts. Used instead of Parsed Sections, which
                for PPTX showed letter-spaced gibberish from PyMuPDF
                reading LibreOffice's font subsets.
              - PDF uploads: Parsed Sections outline (debugging aid). */}
          {document.source_format === "pptx" &&
          document.slides &&
          document.slides.length > 0 ? (
            <Card className="lg:col-span-1">
              <CardHeader className="px-5 pt-4 pb-3">
                <CardTitle className="text-sm font-semibold">
                  Slides
                  <span className="ml-1.5 font-mono text-[10px] font-normal text-slate-400 dark:text-slate-500">
                    {document.slides.length}
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent className="px-5 pb-5">
                <SlideStrip documentId={document.document_id} slides={document.slides} />
              </CardContent>
            </Card>
          ) : (
            document.sections && document.sections.length > 0 && (
              <Card className="lg:col-span-1">
                <CardHeader className="px-5 pt-4 pb-3">
                  <CardTitle className="text-sm font-semibold">
                    Parsed sections
                    <span className="ml-1.5 font-mono text-[10px] font-normal text-slate-400 dark:text-slate-500">
                      {document.sections.length}
                    </span>
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-5 pb-5">
                  <div className="-mx-2 max-h-[650px] space-y-1 overflow-y-auto pr-1">
                    {document.sections.map((section, i) => (
                      <div
                        key={i}
                        className="rounded-lg px-2 py-2 transition-colors hover:bg-slate-50/80 dark:hover:bg-white/[0.03]"
                      >
                        <div className="flex items-start gap-2.5">
                          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-slate-100 font-mono text-[9px] font-medium text-slate-500 dark:bg-white/8 dark:text-slate-400">
                            {section.page_number || i + 1}
                          </span>
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-xs font-medium text-slate-900 dark:text-slate-100">
                              {section.title}
                            </p>
                            {section.content && (
                              <p className="mt-0.5 line-clamp-2 text-[10px] leading-snug text-slate-500 dark:text-slate-400">
                                {section.content.slice(0, 120)}…
                              </p>
                            )}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )
          )}
        </div>
      ) : (
        /* Non-PDF: just show parsed sections full width */
        document.sections && document.sections.length > 0 && (
          <Card>
            <CardHeader className="px-5 pt-4 pb-3">
              <CardTitle className="text-sm font-semibold">
                Parsed content
                <span className="ml-1.5 font-mono text-[10px] font-normal text-slate-400 dark:text-slate-500">
                  {document.sections.length} sections
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="px-5 pb-5">
              <div className="-mx-2 max-h-[500px] space-y-1 overflow-y-auto pr-1">
                {document.sections.map((section, i) => (
                  <div
                    key={i}
                    className="rounded-lg px-3 py-2.5 transition-colors hover:bg-slate-50/80 dark:hover:bg-white/[0.03]"
                  >
                    <div className="flex items-start gap-3">
                      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-slate-100 font-mono text-[9px] font-medium text-slate-500 dark:bg-white/8 dark:text-slate-400">
                        {section.page_number || i + 1}
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="text-xs font-medium text-slate-900 dark:text-slate-100">
                          {section.title}
                        </p>
                        {section.content && (
                          <p className="mt-1 line-clamp-4 whitespace-pre-wrap text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
                            {section.content.slice(0, 300)}{section.content.length > 300 ? "…" : ""}
                          </p>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        )
      )}

      <ValidationConfigDialog
        open={configOpen}
        onOpenChange={setConfigOpen}
        documentId={id}
        onValidationStarted={(validationId) => {
          setValidationStartedAt(Date.now());
          setValidationElapsed(0);
          setValidating(true);
          setValidationStatus(null);
          setValidationError(null);
          setValidationSuccess(validationId);
          setActiveValidationId(validationId);
        }}
      />
    </div>
  );
}

function MetaTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-slate-200/80 bg-white p-3.5 dark:border-white/8 dark:bg-white/[0.02]">
      <p className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
        {label}
      </p>
      <p className="mt-1 text-lg font-semibold tabular-nums text-slate-900 dark:text-slate-50">{value}</p>
    </div>
  );
}

/**
 * Post-hoc visibility into what the AI extracted from a standards
 * document. Shows the rules the agent produced from this specific
 * upload, with severity, tier scope, category, and the validation
 * criteria — the same fields the validator will eventually feed
 * into a Bedrock prompt.
 *
 * Live updating: the parent polls every 4s while the document is
 * still parsing/extracting, so rules appear as the agent saves
 * them. Once the document hits a terminal state, polling stops
 * and this becomes a static history view.
 */
function ExtractionSummary({
  rules,
  pendingCount,
  loading,
  inProgress,
}: {
  rules: StandardItem[];
  pendingCount: number;
  loading: boolean;
  inProgress: boolean;
}) {
  const sevTone = (sev: string): string => {
    switch (sev) {
      case "critical":
        return "bg-rose-50 text-rose-700 border-rose-200/80 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20";
      case "high":
        return "bg-orange-50 text-orange-700 border-orange-200/80 dark:bg-orange-500/10 dark:text-orange-300 dark:border-orange-500/20";
      case "medium":
        return "bg-amber-50 text-amber-700 border-amber-200/80 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20";
      case "low":
        return "bg-blue-50 text-blue-700 border-blue-200/80 dark:bg-blue-500/10 dark:text-blue-300 dark:border-blue-500/20";
      default:
        return "bg-slate-100 text-slate-600 border-slate-200 dark:bg-white/8 dark:text-slate-300 dark:border-white/10";
    }
  };

  const formatTiers = (tiers: number[] | undefined): string => {
    if (!tiers || tiers.length === 0 || tiers.length === 5) return "All tiers";
    const sorted = [...tiers].sort((a, b) => a - b);
    const isContiguous = sorted.length >= 2 && sorted.every((t, i) => i === 0 || t === sorted[i - 1] + 1);
    if (isContiguous) return `Tiers ${sorted[0]}–${sorted[sorted.length - 1]}`;
    return sorted.length === 1 ? `Tier ${sorted[0]}` : `Tiers ${sorted.join(", ")}`;
  };

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h2 className="text-[11px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
          Extraction summary
        </h2>
        <div className="flex items-center gap-2 text-[11px] text-slate-500 dark:text-slate-400">
          {inProgress && (
            <span className="inline-flex items-center gap-1.5 text-sky-600 dark:text-sky-300">
              <svg className="h-3 w-3 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Extracting…
            </span>
          )}
          <span className="font-mono tabular-nums">
            <strong className="text-slate-700 dark:text-slate-200">{rules.length}</strong>{" "}
            {rules.length === 1 ? "rule" : "rules"}
          </span>
        </div>
      </div>

      {rules.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/50 px-6 py-10 text-center dark:border-white/10 dark:bg-white/[0.02]">
          {loading || inProgress ? (
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {inProgress
                ? "Agent is reading the document and extracting rules. Rules will appear here as they're saved."
                : "Loading extracted rules…"}
            </p>
          ) : pendingCount > 0 ? (
            <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
              {pendingCount} {pendingCount === 1 ? "rule is" : "rules are"} awaiting review above. Approved
              rules join the library and appear here.
            </p>
          ) : (
            <>
              <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
                No rules extracted yet
              </p>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                The agent didn&apos;t produce any rules from this document. Check the parsed sections below
                to verify the document was readable, or re-upload the file.
              </p>
            </>
          )}
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-slate-200/80 bg-white shadow-sm shadow-slate-900/[0.02] dark:border-white/8 dark:bg-white/[0.02] dark:shadow-black/20">
          {rules.map((rule, idx) => (
            <div
              key={rule.id}
              className={
                "px-5 py-3.5 transition-colors hover:bg-slate-50/60 dark:hover:bg-white/[0.02] " +
                (idx > 0 ? "border-t border-slate-200/60 dark:border-white/8" : "")
              }
            >
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="mb-1 flex flex-wrap items-center gap-2">
                    <span className="font-mono text-[10px] font-medium text-slate-400 dark:text-slate-500">
                      {rule.id}
                    </span>
                    <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                      {rule.name}
                    </h3>
                  </div>
                  <p className="mb-2 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                    {rule.description}
                  </p>
                  <div className="rounded-lg border border-slate-200/70 bg-slate-50/60 px-3 py-2 dark:border-white/8 dark:bg-white/[0.03]">
                    <p className="mb-0.5 text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
                      Validation criteria
                    </p>
                    <p className="text-[12px] leading-relaxed text-slate-700 dark:text-slate-200">
                      {rule.validation_criteria}
                    </p>
                  </div>

                  {/* Source provenance — verbatim quote + page ref so
                      users can verify the AI's interpretation against
                      the literal source text. Only renders when the
                      agent captured a quote — rules created before
                      this field existed have none. */}
                  {(rule.source_quote || rule.page_reference) && (
                    <div className="mt-2 rounded-lg border-l-2 border-l-[var(--color-brand-primary)]/40 bg-[var(--color-brand-primary)]/[0.03] px-3 py-2 dark:bg-[var(--color-brand-primary)]/[0.06]">
                      <div className="mb-0.5 flex items-center justify-between gap-2">
                        <p className="text-[10px] font-semibold uppercase tracking-normal text-[var(--color-brand-primary)]">
                          Source
                        </p>
                        {rule.page_reference && (
                          <span className="font-mono text-[10px] text-slate-500 dark:text-slate-400">
                            {rule.page_reference}
                          </span>
                        )}
                      </div>
                      {rule.source_quote ? (
                        <p className="text-[12px] italic leading-relaxed text-slate-700 dark:text-slate-200">
                          &ldquo;{rule.source_quote}&rdquo;
                        </p>
                      ) : (
                        <p className="text-[11px] text-slate-500 dark:text-slate-400">
                          No verbatim quote captured for this rule.
                        </p>
                      )}
                    </div>
                  )}
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1.5">
                  <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-normal ${sevTone(rule.severity)}`}>
                    {rule.severity}
                  </span>
                  <span
                    className={
                      "inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] " +
                      (!rule.applies_to_tiers || rule.applies_to_tiers.length === 5
                        ? "border-slate-200 bg-slate-50 text-slate-500 dark:border-white/10 dark:bg-white/5 dark:text-slate-400"
                        : "border-[var(--color-brand-primary)]/20 bg-[var(--color-brand-primary)]/[0.08] text-[var(--color-brand-primary)] dark:border-[var(--color-brand-primary)]/30 dark:bg-[var(--color-brand-primary)]/15 dark:text-[#5eead4]")
                    }
                  >
                    {formatTiers(rule.applies_to_tiers)}
                  </span>
                  {rule.category && (
                    <span className="text-[10px] text-slate-400 dark:text-slate-500">
                      {rule.category}
                    </span>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {rules.length > 0 && (
        <p className="px-1 text-[11px] text-slate-500 dark:text-slate-400">
          Verify these against the parsed sections below. If something looks wrong, edit individual rules
          on the{" "}
          <Link
            href="/standards"
            className="font-medium text-[var(--color-brand-primary)] underline-offset-2 transition-colors hover:underline hover:text-[var(--color-brand-primary-hover)]"
          >
            Standards page
          </Link>{" "}
          or re-upload the source document.
        </p>
      )}
    </div>
  );
}

export function DocumentDetailClient() {
  return (
    <Suspense
      fallback={
        <div className="flex h-64 items-center justify-center">
          <div className="mx-auto h-7 w-7 animate-spin rounded-full border-[2px] border-slate-200 border-t-[var(--color-brand-primary)] dark:border-white/10 dark:border-t-[var(--color-brand-primary)]" />
        </div>
      }
    >
      <DocumentDetailInner />
    </Suspense>
  );
}
