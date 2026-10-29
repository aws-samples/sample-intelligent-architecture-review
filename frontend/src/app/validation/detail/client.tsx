// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ComplianceScore } from "@/components/validation/ComplianceScore";
import { FindingCard } from "@/components/validation/FindingCard";
import { ComplianceMatrix } from "@/components/validation/ComplianceMatrix";
import { FindingTraceDrawer } from "@/components/validation/FindingTraceDrawer";
import { FindingChatPanel } from "@/components/validation/FindingChatPanel";
import { api, HostingScope, ValidationDetailResponse, StandardItem } from "@/lib/api";
import type { Severity, Finding } from "@/lib/mock-data";
import { useRole } from "@/lib/use-role";

const severityOrder: Severity[] = ["critical", "high", "medium", "low"];

// Ceiling on the 2s status poll (~20 min). A run that never reaches a terminal
// state would otherwise keep polling for as long as the tab is open.
const POLL_MAX_TICKS = 600;

const categoryLabels: Record<string, string> = {
  redundancy: "Redundancy",
  availability: "Availability",
  data: "Data Protection",
  traffic_management: "Traffic Management",
  scalability: "Scalability",
  recovery: "Recovery",
  observability: "Observability",
  failover: "Failover",
  resilience: "Resilience",
  security: "Security",
  network: "Network",
  general: "General",
  replication: "Replication",
};

/** Title-case any unknown category/layer key so values like "general"
 *  or "replication" don't render as raw lowercase. Uses categoryLabels
 *  first, then falls back to splitting on _/-/space and capitalizing. */
function categoryLabel(raw: string): string {
  if (categoryLabels[raw]) return categoryLabels[raw];
  if (!raw) return "Other";
  return raw
    .split(/[_\s-]+/)
    .map((w) => (w.length === 0 ? "" : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}

type TabView = "findings" | "diagram" | "compliance";

function ValidationDetailInner() {
  const searchParams = useSearchParams();
  // Static export (output:"export") returns empty useSearchParams() on the
  // first render of a client-side SOFT navigation, so searchParams.get("id")
  // is "" and polling never starts -> infinite "Loading". Fall back to the
  // live URL. (Same fix already applied on documents/detail; this page was
  // left unpatched, which is the freeze the user reported.)
  const id =
    searchParams.get("id") ||
    (typeof window !== "undefined"
      ? new URLSearchParams(window.location.search).get("id") || ""
      : "");

  const [validation, setValidation] = useState<ValidationDetailResponse | null>(null);
  const [documentName, setDocumentName] = useState<string>("");
  const [documentTier, setDocumentTier] = useState<number | null>(null);
  // The SDD's hosting_scope. The validator filtered rules
  // accordingly; surfacing it on the detail header makes the "why
  // were these rules picked" answer obvious without diffing the
  // skipped count.
  const [documentScope, setDocumentScope] = useState<HostingScope | null>(null);
  const [standards, setStandards] = useState<StandardItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TabView>("findings");
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set());
  const [statusFilter, setStatusFilter] = useState<"all" | "pass" | "fail" | "warning" | "not_addressed">("all");
  const [severityFilter, setSeverityFilter] = useState<string | null>(null);
  // Trace drawer + per-finding chat panel.
  const [traceFinding, setTraceFinding] = useState<Finding | null>(null);
  const [chatFinding, setChatFinding] = useState<Finding | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const { email: myEmail, isAdmin } = useRole();
  // Mirrors require_owner_or_admin on POST /api/validations/{id}/cancel: only
  // whoever started the run may stop it, since it bills Bedrock until it does.
  // Runs with no recorded owner predate the control and stay cancellable.
  const runOwner = (validation?.triggered_by || "").trim().toLowerCase();
  const canCancel =
    isAdmin || !runOwner || runOwner === (myEmail || "").trim().toLowerCase();

  const handleCancel = async () => {
    if (!id) return;
    setCancelling(true);
    setCancelError(null);
    try {
      await api.cancelValidation(id);
      // Poll will pick up the terminal "cancelled" status shortly.
    } catch (err) {
      // Shown rather than swallowed: a refused cancel left the button spinning
      // back to idle with no explanation.
      setCancelError(err instanceof Error ? err.message : "Cancel failed");
      setCancelling(false);
    }
  };

  const [exporting, setExporting] = useState<"csv" | "pdf" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const handleExport = async (fmt: "csv" | "pdf") => {
    if (!id) return;
    setExporting(fmt);
    setExportError(null);
    try {
      await api.downloadValidationReport(id, fmt);
    } catch (e) {
      setExportError(e instanceof Error ? e.message : `Failed to export ${fmt}`);
    } finally {
      setExporting(null);
    }
  };

  useEffect(() => {
    if (!id) return;

    let cancelled = false;
    let interval: ReturnType<typeof setInterval> | undefined;
    let ticks = 0;

    async function fetchValidation() {
      try {
        const val = await api.getValidation(id);
        if (cancelled) return;
        // Clear any error from an earlier tick: a transient 5xx must not leave a
        // stale message pinned over a report that is now loading fine.
        setError(null);
        setValidation(val);
        if (val.status === "pending") {
          setElapsedSeconds(
            Math.max(
              0,
              Math.floor(
                (Date.now() - new Date(val.started_at).getTime()) / 1000,
              ),
            ),
          );
        }

        // Fetch document name (only once)
        if (!documentName) {
          try {
            const doc = await api.getDocument(val.document_id);
            if (!cancelled) {
              setDocumentName(doc.filename);
              setDocumentTier(doc.declared_tier ?? null);
              setDocumentScope((doc.hosting_scope as HostingScope) ?? null);
            }
          } catch {
            if (!cancelled) setDocumentName(val.document_id);
          }
        }

        // Stop polling once we hit a terminal state, or once we've waited longer
        // than any real run takes — a run wedged in a non-terminal state would
        // otherwise poll for the life of the tab.
        ticks += 1;
        const gaveUp = ticks > POLL_MAX_TICKS;
        if ((val.status !== "pending" || gaveUp) && interval) {
          clearInterval(interval);
          interval = undefined;
          if (gaveUp && val.status === "pending") {
            setError(
              "Lost track of this run — it has been pending far longer than " +
              "expected. Reload to check again.",
            );
          }
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load validation");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    async function fetchStandards() {
      try {
        const res = await api.listStandards();
        if (!cancelled) setStandards(res.standards);
      } catch {
        if (!cancelled) setStandards([]);
      }
    }

    fetchValidation();
    fetchStandards();
    // Poll every 2s until status leaves "pending".
    interval = setInterval(fetchValidation, 2000);

    return () => {
      cancelled = true;
      if (interval) clearInterval(interval);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const toggleCategory = (cat: string) => {
    setExpandedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(cat)) next.delete(cat);
      else next.add(cat);
      return next;
    });
  };

  const expandAll = () => {
    const allCats = new Set(findings.map((f) => getCategory(f.requirementId)));
    setExpandedCategories(allCats);
  };

  const collapseAll = () => {
    setExpandedCategories(new Set());
  };

  // No id in the URL — show a clear message instead of an infinite spinner
  // (the effect early-returns on empty id, so `loading` would never clear).
  if (!id) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="text-center">
          <p className="text-sm font-medium text-slate-700 dark:text-slate-300">
            No validation ID provided
          </p>
          <Link
            href="/validation"
            className="mt-3 inline-block text-sm font-medium text-[var(--color-brand-primary)] transition-colors hover:text-[var(--color-brand-primary-hover)]"
          >
            Back to validations
          </Link>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="text-center">
          <div className="mx-auto h-7 w-7 animate-spin rounded-full border-[2px] border-slate-200 border-t-[var(--color-brand-primary)] dark:border-white/10 dark:border-t-[var(--color-brand-primary)]" />
          <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">Loading validation results…</p>
        </div>
      </div>
    );
  }

  // Only take over the page when there is genuinely nothing to show. Once a
  // report has loaded, a later error is surfaced as a banner (below) rather than
  // replacing a finished compliance report the user is reading.
  if (!validation) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="text-center">
          <p className="text-sm font-medium text-rose-700 dark:text-rose-300">
            {error || "Validation not found"}
          </p>
          <Link
            href="/validation"
            className="mt-3 inline-block text-sm font-medium text-[var(--color-brand-primary)] transition-colors hover:text-[var(--color-brand-primary-hover)]"
          >
            ← Back to Validations
          </Link>
        </div>
      </div>
    );
  }

  // While the validation is still running, show a progress page.
  if (validation.status === "pending") {
    const evaluated = validation.evaluated_count ?? 0;
    const total = validation.total_standards ?? 0;
    const pct = total > 0 ? Math.round((evaluated / total) * 100) : 0;
    return (
      <div className="space-y-6 animate-in fade-in duration-300">
        <div className="space-y-3">
          <Link
            href="/validation"
            className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 transition-colors hover:text-[var(--color-brand-primary)] dark:text-slate-400"
          >
            <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            Back to Validations
          </Link>
          <div className="space-y-1.5">
            <h1 className="text-2xl font-semibold tracking-normal text-slate-900 dark:text-slate-50">
              {documentName || validation.document_id}
            </h1>
            <p className="font-mono text-[11px] text-slate-400 dark:text-slate-500">{validation.validation_id}</p>
          </div>
        </div>

        <div className="rounded-2xl border border-sky-200/80 bg-sky-50/60 p-6 dark:border-sky-500/20 dark:bg-sky-500/[0.08]">
          <div className="flex items-start gap-4">
            <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-sky-500/15 ring-1 ring-sky-500/20">
              <svg className="h-4 w-4 animate-spin text-sky-600 dark:text-sky-300" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-base font-semibold text-sky-700 dark:text-sky-200">
                Validating against HA standards
              </p>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
                Each standard is evaluated independently in parallel. This page refreshes automatically.
              </p>

              <div className="mt-5 space-y-2">
                <div className="flex items-baseline justify-between text-sm">
                  <span className="font-medium text-slate-700 dark:text-slate-200">
                    {total > 0 ? `${evaluated} of ${total} standards evaluated` : "Preparing standards…"}
                  </span>
                  <span className="font-mono text-xs tabular-nums text-slate-500 dark:text-slate-400">
                    {elapsedSeconds}s elapsed{total > 0 && pct > 0 ? ` · ${pct}%` : ""}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-sky-500/15 dark:bg-sky-500/20">
                  <div
                    className="h-full rounded-full bg-sky-500 transition-all duration-500 ease-out"
                    style={{ width: `${pct}%` }}
                  />
                </div>

                <div className="mt-4">
                  <button
                    type="button"
                    onClick={handleCancel}
                    disabled={cancelling || !canCancel}
                    title={
                      canCancel
                        ? undefined
                        : `Only ${validation?.triggered_by} (or an admin) can cancel this run.`
                    }
                    className="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-white px-3 py-1.5 text-xs font-semibold text-rose-700 transition-all hover:border-rose-300 hover:bg-rose-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400/40 disabled:pointer-events-none disabled:opacity-55 dark:border-rose-400/20 dark:bg-white/5 dark:text-rose-300 dark:hover:bg-rose-500/10"
                  >
                    {cancelling ? "Cancelling…" : "Cancel validation"}
                  </button>
                  {cancelError && (
                    <p className="mt-1.5 text-[11px] text-[var(--color-status-danger)]">
                      {cancelError}
                    </p>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Cancelled runs are terminal — show a clear message + a way to re-run.
  if (validation.status === "cancelled") {
    return (
      <div className="space-y-6 animate-in fade-in duration-300">
        <div className="space-y-3">
          <Link
            href="/validation"
            className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 transition-colors hover:text-[var(--color-brand-primary)] dark:text-slate-400"
          >
            <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            Back to Validations
          </Link>
          <h1 className="text-2xl font-semibold tracking-normal text-slate-900 dark:text-slate-50">
            {documentName || validation.document_id}
          </h1>
        </div>
        <div className="rounded-2xl border border-slate-200/80 bg-slate-50/60 p-6 dark:border-white/8 dark:bg-white/[0.03]">
          <p className="text-base font-semibold text-slate-700 dark:text-slate-200">
            Validation cancelled
          </p>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            This run was cancelled before completing. Start a new validation from
            the document page to try again.
          </p>
        </div>
      </div>
    );
  }

  if (validation.status === "failed") {
    return (
      <div className="space-y-6 animate-in fade-in duration-300">
        <div className="space-y-3">
          <Link
            href="/validation"
            className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 transition-colors hover:text-[var(--color-brand-primary)] dark:text-slate-400"
          >
            <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            Back to Validations
          </Link>
          <h1 className="text-2xl font-semibold tracking-normal text-slate-900 dark:text-slate-50">
            {documentName || validation.document_id}
          </h1>
        </div>
        <div className="rounded-2xl border border-rose-200/80 bg-rose-50/60 p-6 dark:border-rose-500/20 dark:bg-rose-500/[0.08]">
          <div className="flex items-start gap-4">
            <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-rose-500/15 ring-1 ring-rose-500/20">
              <svg className="h-4 w-4 text-rose-600 dark:text-rose-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-base font-semibold text-rose-700 dark:text-rose-200">Validation failed</p>
              <p className="mt-1.5 text-sm text-slate-600 dark:text-slate-400">{validation.error_message || "An unknown error occurred."}</p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Map API findings to the Finding type expected by FindingCard
  const findings: Finding[] = validation.findings.map((f, i) => ({
    id: `f-${i}`,
    requirementId: f.requirement_id,
    title: f.requirement_title,
    description: f.evidence,
    severity: f.severity as Severity,
    status: f.status as Finding["status"],
    evidence: f.evidence,
    recommendation: f.recommendation,
    pattern: f.requirement_id,
    alsoSeenPages: f.also_seen_pages || [],
    ruleType: f.rule_type ?? null,
    viaRuleId: f.via_rule_id ?? null,
    triedSubstitutionIds: f.tried_substitution_ids || [],
    trace: f.trace ?? null,
  }));

  // pass_via_substitution rolls into pass (the layer is
  // compliant); not_applicable_via_exception rolls into warning (it's
  // not a problem, but the user should see why we skipped).
  const findingCounts = {
    critical: findings.filter((f) => f.severity === "critical" && f.status === "fail").length,
    high: findings.filter((f) => f.severity === "high" && f.status === "fail").length,
    medium: findings.filter((f) => f.severity === "medium").length,
    low: findings.filter((f) => f.severity === "low").length,
    pass: findings.filter((f) => f.status === "pass" || f.status === "pass_via_substitution").length,
    fail: findings.filter((f) => f.status === "fail").length,
    warning: findings.filter((f) => f.status === "warning" || f.status === "not_applicable_via_exception").length,
    // Coverage gaps: rules the document is silent on. Shown separately so
    // they aren't mistaken for failures (they don't count against the score).
    notAddressed: findings.filter((f) => f.status === "not_addressed").length,
  };

  // Infer category from requirement_id pattern
  function getCategory(reqId: string): string {
    const lower = reqId.toLowerCase();
    for (const key of Object.keys(categoryLabels)) {
      if (lower.includes(key.replace("_", ""))) return key;
      if (lower.includes(key)) return key;
    }
    // Try partial matches
    if (lower.includes("redund")) return "redundancy";
    if (lower.includes("recov") || lower.includes("rto") || lower.includes("rpo")) return "recovery";
    if (lower.includes("secur") || lower.includes("encrypt") || lower.includes("auth")) return "security";
    if (lower.includes("fail") || lower.includes("switch")) return "failover";
    if (lower.includes("scale") || lower.includes("auto")) return "scalability";
    if (lower.includes("observ") || lower.includes("monitor") || lower.includes("alert")) return "observability";
    if (lower.includes("network") || lower.includes("dns") || lower.includes("lb")) return "network";
    if (lower.includes("data") || lower.includes("backup") || lower.includes("replicat")) return "data";
    if (lower.includes("traffic") || lower.includes("route")) return "traffic_management";
    return "availability";
  }

  // Diagram analysis - findings related to architecture diagrams
  const diagramFindings = findings.filter((f) => {
    const lower = (f.evidence + f.title + f.description).toLowerCase();
    return lower.includes("diagram") || lower.includes("architecture") || lower.includes("topology") || lower.includes("component");
  });

  const patterns = [...new Set(findings.map((f) => f.pattern))];

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      {/* A refresh failed, but the report below is still the last good one —
          say so inline instead of replacing it. */}
      {error && (
        <div className="flex items-start justify-between gap-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-400/20 dark:bg-amber-400/10 dark:text-amber-200">
          <span>Couldn&apos;t refresh this validation: {error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            className="shrink-0 font-medium underline underline-offset-2"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Header */}
      <div className="space-y-3">
        <Link
          href="/validation"
          className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 transition-colors hover:text-[var(--color-brand-primary)] dark:text-slate-400"
        >
          <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Validations
        </Link>
        <div className="flex items-start justify-between gap-4">
        <div className="space-y-1.5">
          <h1 className="break-words text-2xl font-semibold tracking-normal text-slate-900 dark:text-slate-50">
            {documentName}
          </h1>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Validated{" "}
            {validation.started_at
              ? new Date(validation.started_at).toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" })
              : "N/A"}
            {" · "}
            <span className="font-mono text-[11px] text-slate-400 dark:text-slate-500">{validation.validation_id}</span>
          </p>
          {/* SDD scope chip. Render inline next to the
              optional tier chip so the reader sees both axes the
              validator filtered on at a glance. */}
          {(documentScope || documentTier != null) && (
            <div className="flex flex-wrap items-center gap-1.5 pt-1">
              {documentTier != null && (
                <span className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-medium text-slate-600 dark:border-white/10 dark:bg-white/5 dark:text-slate-300">
                  Tier {documentTier}
                </span>
              )}
              {documentScope && (
                <span
                  className={
                    "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium " +
                    (documentScope === "on_prem"
                      ? "border-amber-200/80 bg-amber-50 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300"
                      : documentScope === "cloud"
                      ? "border-sky-200/80 bg-sky-50 text-sky-700 dark:border-sky-500/30 dark:bg-sky-500/10 dark:text-sky-300"
                      : "border-slate-200 bg-slate-50 text-slate-600 dark:border-white/10 dark:bg-white/5 dark:text-slate-300")
                  }
                >
                  {documentScope === "on_prem"
                    ? "On-prem"
                    : documentScope === "cloud"
                    ? "Cloud"
                    : "Universal"}
                </span>
              )}
            </div>
          )}
        </div>
        {/* Export — download the report as CSV or PDF. */}
        <div className="flex shrink-0 flex-col items-end gap-1">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => handleExport("csv")}
              disabled={exporting !== null}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition-all hover:border-slate-300 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/25 disabled:pointer-events-none disabled:opacity-55 dark:border-white/12 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
            >
              {exporting === "csv" ? "Exporting…" : "Export CSV"}
            </button>
            <button
              type="button"
              onClick={() => handleExport("pdf")}
              disabled={exporting !== null}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition-all hover:border-slate-300 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/25 disabled:pointer-events-none disabled:opacity-55 dark:border-white/12 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
            >
              {exporting === "pdf" ? "Exporting…" : "Export PDF"}
            </button>
          </div>
          {exportError && (
            <p className="max-w-[220px] text-right text-[10px] text-rose-600 dark:text-rose-300">
              {exportError}
            </p>
          )}
        </div>
        </div>
      </div>

      {/* Score Overview */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
        <Card className="lg:col-span-1">
          <CardContent className="flex flex-col items-center justify-center py-6">
            <ComplianceScore score={validation.overall_score} size="lg" />
            <p className="mt-3 text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
              Overall score
            </p>
            {/* Coverage context — a high score with unexamined rules is NOT
                full compliance. Make that explicit so the number isn't
                misread (score counts only evaluated rules; not-addressed are
                excluded). */}
            {findingCounts.notAddressed > 0 && (
              <p className="mt-1 max-w-[180px] text-center text-[10px] leading-snug text-slate-500 dark:text-slate-400">
                of {findings.length - findingCounts.notAddressed} evaluated ·{" "}
                <span className="font-medium text-slate-600 dark:text-slate-300">
                  {findingCounts.notAddressed} not addressed
                </span>
              </p>
            )}
            {validation.architecture_verdict && (
              <p className="mt-1 text-center text-[10px] uppercase tracking-wide text-slate-400 dark:text-slate-500">
                Architecture:{" "}
                <span
                  className={
                    validation.architecture_verdict === "not_compliant"
                      ? "font-semibold text-rose-600 dark:text-rose-300"
                      : "font-semibold text-emerald-600 dark:text-emerald-300"
                  }
                >
                  {validation.architecture_verdict.replace(/_/g, " ")}
                </span>
              </p>
            )}
          </CardContent>
        </Card>

        <Card className="lg:col-span-3">
          <CardHeader className="px-5 pt-5 pb-3">
            <CardTitle className="text-sm font-semibold">Findings summary</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 px-5 pb-5">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <FindingTile label="Critical fails" value={findingCounts.critical} tone="rose" />
              <FindingTile label="High fails"     value={findingCounts.high}     tone="orange" />
              <FindingTile label="Passed"         value={findingCounts.pass}     tone="emerald" />
              <FindingTile label="Warnings"       value={findingCounts.warning}  tone="amber" />
            </div>

            {/* Tier + scope filter summary. When the per-dimension
                breakdown is present, surface it so the user
                understands whether rules were skipped because of tier
                mismatch, hosting-scope mismatch, or both. Older rows
                without skipped_count_breakdown fall back to the
                generic "not applicable" copy. */}
            {validation.skipped_count != null && validation.skipped_count > 0 && (
              <div className="rounded-lg border border-[var(--color-brand-primary)]/15 bg-[var(--color-brand-primary)]/[0.04] px-3 py-2 text-xs text-slate-700 dark:border-[var(--color-brand-primary)]/25 dark:bg-[var(--color-brand-primary)]/[0.06] dark:text-slate-300">
                Evaluated{" "}
                <strong className="font-mono tabular-nums">{findings.length}</strong> of{" "}
                <strong className="font-mono tabular-nums">
                  {findings.length + (validation.skipped_count || 0)}
                </strong>{" "}
                rules ·{" "}
                <strong className="font-mono tabular-nums">{validation.skipped_count}</strong>{" "}
                not applicable
                {(() => {
                  const bd = validation.skipped_count_breakdown;
                  if (bd && (bd.tier > 0 || bd.scope > 0)) {
                    const parts: string[] = [];
                    if (bd.tier > 0) parts.push(`${bd.tier} tier-mismatch`);
                    if (bd.scope > 0) parts.push(`${bd.scope} scope-mismatch`);
                    return ` (${parts.join(", ")})`;
                  }
                  return documentTier != null ? ` to Tier-${documentTier}` : "";
                })()}.
              </div>
            )}

            {/* Pass/Fail bar */}
            {findings.length > 0 && (
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
                    Compliance distribution
                  </span>
                  <span className="font-mono text-xs tabular-nums text-slate-500 dark:text-slate-400">
                    {findings.length} checks
                  </span>
                </div>
                <div className="flex h-2 overflow-hidden rounded-full bg-slate-100 dark:bg-white/8">
                  {findingCounts.pass > 0 && (
                    <div
                      className="bg-emerald-500 transition-all"
                      style={{ width: `${(findingCounts.pass / findings.length) * 100}%` }}
                      title={`${findingCounts.pass} passed`}
                    />
                  )}
                  {findingCounts.warning > 0 && (
                    <div
                      className="bg-amber-500 transition-all"
                      style={{ width: `${(findingCounts.warning / findings.length) * 100}%` }}
                      title={`${findingCounts.warning} warnings`}
                    />
                  )}
                  {findingCounts.fail > 0 && (
                    <div
                      className="bg-rose-500 transition-all"
                      style={{ width: `${(findingCounts.fail / findings.length) * 100}%` }}
                      title={`${findingCounts.fail} failed`}
                    />
                  )}
                  {findingCounts.notAddressed > 0 && (
                    <div
                      className="bg-slate-300 transition-all dark:bg-white/20"
                      style={{ width: `${(findingCounts.notAddressed / findings.length) * 100}%` }}
                      title={`${findingCounts.notAddressed} not addressed`}
                    />
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-4 text-[11px] text-slate-500 dark:text-slate-400">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2 w-2 rounded-sm bg-emerald-500" /> Pass
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2 w-2 rounded-sm bg-amber-500" /> Warning
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2 w-2 rounded-sm bg-rose-500" /> Fail
                  </span>
                  {findingCounts.notAddressed > 0 && (
                    <span className="inline-flex items-center gap-1.5">
                      <span className="h-2 w-2 rounded-sm bg-slate-300 dark:bg-white/20" /> Not addressed
                    </span>
                  )}
                </div>
              </div>
            )}

            {/* Standards evaluated */}
            {patterns.length > 0 && (
              <div>
                <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
                  Standards evaluated <span className="font-mono normal-case">{patterns.length}</span>
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {patterns.map((pattern) => (
                    <span
                      key={pattern}
                      className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 font-mono text-[10px] font-medium text-slate-600 dark:border-white/10 dark:bg-white/5 dark:text-slate-300"
                    >
                      {pattern}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Cost / telemetry footnote — only shows for runs that came
          out of the chunked validator (older rows lack these fields). */}
      {validation.bedrock_calls != null && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-1 text-[11px] text-slate-500 dark:text-slate-400">
          <span>
            <span className="font-mono tabular-nums text-slate-700 dark:text-slate-200">
              {validation.bedrock_calls}
            </span>{" "}
            Bedrock {validation.bedrock_calls === 1 ? "call" : "calls"}
          </span>
          {validation.chunks_per_document != null && (
            <span>
              across{" "}
              <span className="font-mono tabular-nums text-slate-700 dark:text-slate-200">
                {validation.chunks_per_document}
              </span>{" "}
              {validation.chunks_per_document === 1 ? "chunk" : "chunks"}
            </span>
          )}
          {validation.bedrock_input_tokens != null &&
            validation.bedrock_output_tokens != null && (
              <span>
                <span className="font-mono tabular-nums text-slate-700 dark:text-slate-200">
                  {(
                    (validation.bedrock_input_tokens || 0) +
                    (validation.bedrock_output_tokens || 0)
                  ).toLocaleString()}
                </span>{" "}
                tokens
              </span>
            )}
          {validation.estimated_cost_usd != null && (
            <span>
              ≈{" "}
              <span className="font-mono tabular-nums text-slate-700 dark:text-slate-200">
                ${validation.estimated_cost_usd.toFixed(4)}
              </span>
            </span>
          )}
        </div>
      )}

      {/* Tab Navigation */}
      <div className="flex items-center gap-1 border-b border-slate-200/80 dark:border-white/10">
        {([
          { val: "findings"   as const, label: "Findings by category", count: findings.length },
          { val: "diagram"    as const, label: "Diagram analysis",     count: diagramFindings.length },
          { val: "compliance" as const, label: "Compliance matrix",    count: null },
        ]).map(({ val, label, count }) => (
          <button
            key={val}
            onClick={() => setActiveTab(val)}
            className={
              "relative px-3.5 py-2.5 text-sm font-medium transition-all " +
              (activeTab === val
                ? "text-slate-900 after:absolute after:inset-x-3 after:bottom-0 after:h-[2px] after:rounded-full after:bg-[var(--color-brand-primary)] dark:text-slate-50"
                : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white")
            }
          >
            {label}
            {count !== null && (
              <span className="ml-1.5 font-mono text-[11px] text-slate-400 dark:text-slate-500">{count}</span>
            )}
          </button>
        ))}
      </div>

      {/* Findings Tab - Grouped by Category */}
      {activeTab === "findings" && (() => {
        // Apply filters. pass_via_substitution rolls under "pass",
        // not_applicable_via_exception rolls under "warning".
        const filteredFindings = findings.filter((f) => {
          if (statusFilter !== "all") {
            const bucket =
              f.status === "pass_via_substitution"
                ? "pass"
                : f.status === "not_applicable_via_exception"
                ? "warning"
                : f.status;
            if (bucket !== statusFilter) return false;
          }
          if (severityFilter && f.severity !== severityFilter) return false;
          return true;
        });

        // Regroup filtered findings by category
        const filteredByCategory = new Map<string, Finding[]>();
        filteredFindings.forEach((f) => {
          const cat = getCategory(f.requirementId);
          if (!filteredByCategory.has(cat)) filteredByCategory.set(cat, []);
          filteredByCategory.get(cat)!.push(f);
        });
        const filteredSortedCategories = Array.from(filteredByCategory.entries()).sort((a, b) => {
          const aCrit = a[1].filter((f) => f.severity === "critical" && f.status === "fail").length;
          const bCrit = b[1].filter((f) => f.severity === "critical" && f.status === "fail").length;
          if (aCrit !== bCrit) return bCrit - aCrit;
          return b[1].length - a[1].length;
        });

        return (
          <div className="space-y-4">
            {/* Filter bar */}
            <div className="rounded-xl border border-slate-200/80 bg-white p-3 dark:border-white/8 dark:bg-white/[0.02]">
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
                {/* Status */}
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Status</span>
                  {(["all", "pass", "fail", "warning", "not_addressed"] as const).map((val) => {
                    const count =
                      val === "all"
                        ? findings.length
                        : val === "pass"
                        ? findings.filter((f) => f.status === "pass" || f.status === "pass_via_substitution").length
                        : val === "warning"
                        ? findings.filter((f) => f.status === "warning" || f.status === "not_applicable_via_exception").length
                        : findings.filter((f) => f.status === val).length;
                    // Hide the Not-addressed chip when there are none, to keep
                    // the bar clean; always show the core pass/fail/warning.
                    if (val === "not_addressed" && count === 0) return null;
                    const isActive = statusFilter === val;
                    const activeTone =
                      val === "all"     ? "bg-slate-100 text-slate-700 border-slate-200 dark:bg-white/10 dark:text-slate-200 dark:border-white/15"
                      : val === "pass"  ? "bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20"
                      : val === "fail"  ? "bg-rose-50 text-rose-700 border-rose-200/80 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20"
                      : val === "warning" ? "bg-amber-50 text-amber-700 border-amber-200/80 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20"
                      : "bg-slate-100 text-slate-600 border-slate-300 dark:bg-white/10 dark:text-slate-300 dark:border-white/15";
                    return (
                      <button
                        key={val}
                        onClick={() => setStatusFilter(val)}
                        className={
                          "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-all " +
                          (isActive
                            ? activeTone
                            : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10")
                        }
                      >
                        {val === "all" ? "All" : val === "pass" ? "Pass" : val === "fail" ? "Fail" : val === "warning" ? "Warning" : "Not addressed"}
                        <span className="opacity-60">{count}</span>
                      </button>
                    );
                  })}
                </div>

                {/* Severity */}
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Severity</span>
                  {severityOrder.map((sev) => {
                    const count = findings.filter(f => f.severity === sev).length;
                    if (count === 0) return null;
                    const isActive = severityFilter === sev;
                    const sevTone: Record<string, string> = {
                      critical: "bg-rose-50 text-rose-700 border-rose-200/80 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20",
                      high:     "bg-orange-50 text-orange-700 border-orange-200/80 dark:bg-orange-500/10 dark:text-orange-300 dark:border-orange-500/20",
                      medium:   "bg-amber-50 text-amber-700 border-amber-200/80 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20",
                      low:      "bg-blue-50 text-blue-700 border-blue-200/80 dark:bg-blue-500/10 dark:text-blue-300 dark:border-blue-500/20",
                    };
                    return (
                      <button
                        key={sev}
                        onClick={() => setSeverityFilter(isActive ? null : sev)}
                        className={
                          "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-all " +
                          (isActive
                            ? sevTone[sev]
                            : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10")
                        }
                      >
                        {sev}
                        <span className="opacity-60">{count}</span>
                      </button>
                    );
                  })}
                </div>

                {(statusFilter !== "all" || severityFilter) && (
                  <button
                    onClick={() => { setStatusFilter("all"); setSeverityFilter(null); }}
                    className="ml-auto text-[11px] font-medium text-[var(--color-brand-primary)] transition-colors hover:text-[var(--color-brand-primary-hover)]"
                  >
                    Clear filters
                  </button>
                )}
              </div>
            </div>

            {/* Expand/Collapse controls */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3 text-[11px]">
                <button onClick={expandAll} className="font-medium text-slate-500 transition-colors hover:text-slate-900 dark:text-slate-400 dark:hover:text-white">
                  Expand all
                </button>
                <span className="text-slate-300 dark:text-slate-600">·</span>
                <button onClick={collapseAll} className="font-medium text-slate-500 transition-colors hover:text-slate-900 dark:text-slate-400 dark:hover:text-white">
                  Collapse all
                </button>
              </div>
              <span className="text-[11px] text-slate-500 dark:text-slate-400">
                Showing <strong className="text-slate-700 dark:text-slate-200">{filteredFindings.length}</strong> of {findings.length} findings
              </span>
            </div>

            {filteredFindings.length === 0 ? (
              <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/50 px-6 py-10 text-center dark:border-white/10 dark:bg-white/[0.02]">
                <p className="text-sm font-medium text-slate-700 dark:text-slate-200">No findings match your filters</p>
                <button
                  onClick={() => { setStatusFilter("all"); setSeverityFilter(null); }}
                  className="mt-2 text-xs font-medium text-[var(--color-brand-primary)] transition-colors hover:text-[var(--color-brand-primary-hover)]"
                >
                  Clear filters
                </button>
              </div>
            ) : (
              filteredSortedCategories.map(([category, catFindings]) => {
                const isExpanded = expandedCategories.has(category);
                const failCount = catFindings.filter((f) => f.status === "fail").length;
                const passCount = catFindings.filter(
                  (f) => f.status === "pass" || f.status === "pass_via_substitution",
                ).length;
                const warnCount = catFindings.filter(
                  (f) => f.status === "warning" || f.status === "not_applicable_via_exception",
                ).length;

                return (
                  <Card key={category} className="overflow-hidden">
                    <button
                      onClick={() => toggleCategory(category)}
                      className="flex w-full items-center justify-between rounded-t-xl px-4 py-3 text-left transition-colors hover:bg-slate-50/60 dark:hover:bg-white/[0.03]"
                    >
                      <div className="flex items-center gap-3">
                        <svg
                          className={`h-3.5 w-3.5 text-slate-400 transition-transform ${isExpanded ? "rotate-90" : ""}`}
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                        >
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                        </svg>
                        <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                          {categoryLabel(category)}
                        </span>
                        <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-medium text-slate-500 dark:border-white/10 dark:bg-white/5 dark:text-slate-400">
                          {catFindings.length} checks
                        </span>
                      </div>
                      <div className="flex items-center gap-3 text-[11px] font-medium">
                        {passCount > 0 && (
                          <span className="text-emerald-700 dark:text-emerald-300">{passCount} pass</span>
                        )}
                        {warnCount > 0 && (
                          <span className="text-amber-700 dark:text-amber-300">{warnCount} warning</span>
                        )}
                        {failCount > 0 && (
                          <span className="text-rose-700 dark:text-rose-300">{failCount} fail</span>
                        )}
                      </div>
                    </button>
                    {isExpanded && (
                      <div className="space-y-3 border-t border-slate-200/60 p-4 dark:border-white/8">
                        {catFindings
                          .sort((a, b) => severityOrder.indexOf(a.severity) - severityOrder.indexOf(b.severity))
                          .map((finding) => (
                            <FindingCard key={finding.id} finding={finding} onOpenTrace={setTraceFinding} onOpenChat={setChatFinding} />
                          ))}
                      </div>
                    )}
                  </Card>
                );
              })
            )}
          </div>
        );
      })()}

      {/* Diagram Analysis Tab */}
      {activeTab === "diagram" && (
        <div className="space-y-4">
          <Card>
            <CardHeader className="px-5 pt-5 pb-3">
              <CardTitle className="text-sm font-semibold">Architecture diagram analysis</CardTitle>
            </CardHeader>
            <CardContent className="px-5 pb-5">
              <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">
                Findings related to architecture diagrams, topology, and component structure identified in the document.
              </p>
              {diagramFindings.length === 0 ? (
                <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/50 px-6 py-10 text-center dark:border-white/10 dark:bg-white/[0.02]">
                  <svg className="mx-auto mb-3 h-9 w-9 text-slate-300 dark:text-slate-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z" />
                  </svg>
                  <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
                    No diagram-specific findings detected
                  </p>
                  <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                    Diagram analysis appears when architecture topology or component diagrams are present in the SDD.
                  </p>
                </div>
              ) : (
                <div className="space-y-3">
                  {diagramFindings.map((finding) => (
                    <FindingCard key={finding.id} finding={finding} onOpenTrace={setTraceFinding} onOpenChat={setChatFinding} />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* Compliance Matrix Tab */}
      {activeTab === "compliance" && (
        <div className="space-y-4">
          <ComplianceMatrix findings={findings} standards={standards} />
        </div>
      )}

      {/* Validator trace drawer (right-side). */}
      <FindingTraceDrawer
        finding={traceFinding}
        onClose={() => setTraceFinding(null)}
      />
      {/* Per-finding chat panel (bottom-right corner). */}
      <FindingChatPanel
        finding={chatFinding}
        validationId={validation.validation_id}
        onClose={() => setChatFinding(null)}
      />
    </div>
  );
}

type FindingTileTone = "rose" | "orange" | "emerald" | "amber";
const FINDING_TILE_TONE: Record<FindingTileTone, { dot: string; value: string }> = {
  rose:    { dot: "bg-rose-500",    value: "text-rose-700 dark:text-rose-300" },
  orange:  { dot: "bg-orange-500",  value: "text-orange-700 dark:text-orange-300" },
  emerald: { dot: "bg-emerald-500", value: "text-emerald-700 dark:text-emerald-300" },
  amber:   { dot: "bg-amber-500",   value: "text-amber-700 dark:text-amber-300" },
};

function FindingTile({ label, value, tone }: { label: string; value: number; tone: FindingTileTone }) {
  const c = FINDING_TILE_TONE[tone];
  return (
    <div className="rounded-xl border border-slate-200/80 bg-white p-3.5 dark:border-white/8 dark:bg-white/[0.02]">
      <div className="flex items-center gap-1.5">
        <span className={`inline-block h-1.5 w-1.5 rounded-full ${c.dot}`} aria-hidden />
        <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
          {label}
        </span>
      </div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${c.value}`}>{value}</div>
    </div>
  );
}

import { Suspense } from "react";
export function ValidationDetailClient() {
  return (
    <Suspense
      fallback={
        <div className="flex h-64 items-center justify-center">
          <div className="mx-auto h-7 w-7 animate-spin rounded-full border-[2px] border-slate-200 border-t-[var(--color-brand-primary)] dark:border-white/10 dark:border-t-[var(--color-brand-primary)]" />
        </div>
      }
    >
      <ValidationDetailInner />
    </Suspense>
  );
}
