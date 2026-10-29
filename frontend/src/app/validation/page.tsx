// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ChevronRight, Grid2X2, List, LoaderCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { PageHero } from "@/components/layout/PageHero";
import { api } from "@/lib/api";

type StatusFilter = "all" | "completed" | "pending" | "failed" | "cancelled";
type SortOption = "date-desc" | "date-asc" | "score-desc" | "score-asc";

interface ValidationItem {
  id: string;
  documentId: string;
  documentName: string;
  date: string;
  dateRaw: string;
  // null = not applicable (no evaluable rules). Rendered as "N/A", not 0.
  score: number | null;
  status: string;
  findingCounts: {
    critical: number;
    high: number;
    medium: number;
    low: number;
  };
  evaluatedCount: number;
  totalStandards: number;
}

type ViewMode = "grid" | "table";

export default function ValidationPage() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [documentFilter, setDocumentFilter] = useState<string>("all");
  const [sortOption, setSortOption] = useState<SortOption>("date-desc");
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [validations, setValidations] = useState<ValidationItem[]>([]);
  const [documents, setDocuments] = useState<{ id: string; name: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [initialLoad, setInitialLoad] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Guard the poll: without a cancelled flag it sets state after unmount, and
    // an idle tick after the token expires trips authFetch's 401 handler and
    // signs the user out of the whole app.
    let cancelled = false;

    async function fetchData() {
      try {
        const [valsRes, docsRes] = await Promise.all([
          api.listValidations(),
          api.listDocuments(),
        ]);
        if (cancelled) return;

        const docList = docsRes.documents.map((d) => ({
          id: d.document_id,
          name: d.filename,
        }));
        setDocuments(docList);

        const items: ValidationItem[] = valsRes.validations.map((v) => {
          const doc = docsRes.documents.find((d) => d.document_id === v.document_id);
          return {
            id: v.validation_id,
            documentId: v.document_id,
            documentName: doc?.filename || v.document_id,
            date: v.started_at ? new Date(v.started_at).toLocaleDateString() : "",
            dateRaw: v.started_at || "",
            score: v.overall_score,
            status: v.status,
            findingCounts: {
              critical: v.critical_count,
              high: v.high_count,
              medium: v.findings_count - v.critical_count - v.high_count,
              low: 0,
            },
            evaluatedCount: v.evaluated_count ?? 0,
            totalStandards: v.total_standards ?? 0,
          };
        });

        setValidations(items);
        // A later success clears an earlier transient failure rather than
        // leaving a stale message over good data.
        setError(null);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load validations");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
          setInitialLoad(false);
        }
      }
    }

    fetchData();
    const interval = setInterval(() => {
      // Only poll a visible tab. A background tab doesn't need fresh rows, and
      // this list refetches every validation on each tick.
      if (typeof document !== "undefined" && document.hidden) return;
      fetchData();
    }, 10000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  // Apply filters and sorting
  let filtered = validations;
  if (statusFilter !== "all") {
    filtered = filtered.filter((v) => v.status === statusFilter);
  }
  if (documentFilter !== "all") {
    filtered = filtered.filter((v) => v.documentId === documentFilter);
  }

  // Sort
  filtered = [...filtered].sort((a, b) => {
    switch (sortOption) {
      case "date-desc": return new Date(b.dateRaw).getTime() - new Date(a.dateRaw).getTime();
      case "date-asc": return new Date(a.dateRaw).getTime() - new Date(b.dateRaw).getTime();
      // Null (N/A) scores sort to the bottom in both directions.
      case "score-desc": return (b.score ?? -1) - (a.score ?? -1);
      case "score-asc": return (a.score ?? Infinity) - (b.score ?? Infinity);
      default: return 0;
    }
  });

  // Group by document to show history
  const documentHistory = new Map<string, number>();
  validations.forEach((v) => {
    documentHistory.set(v.documentId, (documentHistory.get(v.documentId) || 0) + 1);
  });

  const statusCounts = {
    all: validations.length,
    completed: validations.filter((v) => v.status === "completed").length,
    pending: validations.filter((v) => v.status === "pending").length,
    failed: validations.filter((v) => v.status === "failed").length,
    cancelled: validations.filter((v) => v.status === "cancelled").length,
  };

  if (initialLoad && loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="text-center">
          <LoaderCircle className="mx-auto h-6 w-6 animate-spin text-[var(--color-brand-primary)]" />
          <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">Loading validations…</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="text-center">
          <p className="text-sm font-medium text-rose-700 dark:text-rose-300">Failed to load validations</p>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{error}</p>
        </div>
      </div>
    );
  }

  // Average over completed runs that have a numeric score (exclude N/A /
  // null-score runs so they don't skew the average as if they were 0%).
  const scoredRuns = validations.filter(
    (v): v is ValidationItem & { score: number } =>
      v.status === "completed" && typeof v.score === "number",
  );
  const avgScore =
    scoredRuns.length > 0
      ? Math.round(scoredRuns.reduce((sum, v) => sum + v.score, 0) / scoredRuns.length)
      : 0;
  const totalCritical = validations.reduce((sum, v) => sum + v.findingCounts.critical, 0);
  const uniqueDocs = new Set(validations.map((v) => v.documentId)).size;

  const subtitle = validations.length === 0
    ? "Run a validation against an SDD to see results here."
    : `${validations.length} run${validations.length === 1 ? "" : "s"} · ${statusCounts.completed} completed · ${avgScore}% avg score`;

  return (
    <div className="space-y-5 animate-in fade-in duration-300">
      <PageHero title="Validations" subtitle={subtitle} />

      {/* KPI Tiles */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
        <ValTile label="Total" value={validations.length} accent="brand" />
        <ValTile label="Completed" value={statusCounts.completed} accent="pass" />
        <ValTile label="In progress" value={statusCounts.pending} accent="info" />
        <ValTile label="Avg score" value={`${avgScore}%`} accent="brand" />
        <ValTile label="Critical issues" value={totalCritical} accent="critical" />
        <ValTile label="Docs validated" value={uniqueDocs} accent="info" />
      </div>

      {/* Filter bar */}
      <div className="rounded-xl border border-slate-200/80 bg-white p-3 dark:border-white/8 dark:bg-white/[0.02]">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          {/* Status pills */}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Status</span>
            {(["all", "completed", "pending", "failed", "cancelled"] as StatusFilter[]).map((val) => {
              // Hide the Cancelled pill entirely when there are none, to keep
              // the bar uncluttered; the other pills always show.
              if (val === "cancelled" && statusCounts.cancelled === 0) return null;
              const isActive = statusFilter === val;
              const activeTone =
                val === "completed" ? "bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20"
                : val === "failed"  ? "bg-rose-50 text-rose-700 border-rose-200/80 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20"
                : val === "pending" ? "bg-amber-50 text-amber-700 border-amber-200/80 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20"
                : val === "cancelled" ? "bg-slate-100 text-slate-600 border-slate-300 dark:bg-white/10 dark:text-slate-300 dark:border-white/15"
                : "bg-[var(--color-brand-primary)]/10 text-[var(--color-brand-primary)] border-[var(--color-brand-primary)]/20";
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
                  {val.charAt(0).toUpperCase() + val.slice(1)}
                  <span className="opacity-60">{statusCounts[val]}</span>
                </button>
              );
            })}
          </div>

          {/* Document */}
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Document</span>
            <select
              className="h-7 max-w-[200px] truncate rounded-md border border-slate-200 bg-white px-2 text-xs text-slate-700 transition-colors hover:border-slate-300 focus:border-[var(--color-brand-primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/15 dark:bg-white/5 dark:text-slate-200"
              value={documentFilter}
              onChange={(e) => setDocumentFilter(e.target.value)}
            >
              <option value="all">All</option>
              {documents.map((d) => (
                <option key={d.id} value={d.id}>{d.name}</option>
              ))}
            </select>
          </div>

          {/* Sort */}
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Sort</span>
            <select
              className="h-7 rounded-md border border-slate-200 bg-white px-2 text-xs text-slate-700 transition-colors hover:border-slate-300 focus:border-[var(--color-brand-primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/15 dark:bg-white/5 dark:text-slate-200"
              value={sortOption}
              onChange={(e) => setSortOption(e.target.value as SortOption)}
            >
              <option value="date-desc">Newest first</option>
              <option value="date-asc">Oldest first</option>
              <option value="score-desc">Highest score</option>
              <option value="score-asc">Lowest score</option>
            </select>
          </div>

          {/* View toggle */}
          <div className="ml-auto inline-flex items-center gap-0.5 rounded-md border border-slate-200 bg-white p-0.5 dark:border-white/10 dark:bg-white/5">
            <button
              onClick={() => setViewMode("grid")}
              className={
                "rounded px-2.5 py-1.5 transition-all " +
                (viewMode === "grid"
                  ? "bg-[var(--color-brand-primary)] text-white shadow-sm"
                  : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white")
              }
              title="Card view"
              aria-label="Card view"
            >
              <Grid2X2 className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setViewMode("table")}
              className={
                "rounded px-2.5 py-1.5 transition-all " +
                (viewMode === "table"
                  ? "bg-[var(--color-brand-primary)] text-white shadow-sm"
                  : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white")
              }
              title="Table view"
              aria-label="Table view"
            >
              <List className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>

      {/* Validation List */}
      {filtered.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/50 px-6 py-10 text-center dark:border-white/10 dark:bg-white/[0.02]">
          <p className="text-sm font-medium text-slate-700 dark:text-slate-200">No validations match your filters</p>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Adjust the filters above or run a new validation from the document detail page.
          </p>
        </div>
      ) : viewMode === "grid" ? (
        <div className="overflow-hidden rounded-xl border border-slate-200/80 bg-white shadow-sm shadow-slate-900/[0.02] dark:border-white/8 dark:bg-white/[0.02] dark:shadow-black/20">
          {filtered.map((validation, idx) => {
            const isPending = validation.status === "pending";
            const divider = idx > 0 ? "border-t border-slate-200/60 dark:border-white/8" : "";

            if (isPending) {
              const pct = validation.totalStandards > 0
                ? Math.round((validation.evaluatedCount / validation.totalStandards) * 100)
                : 0;
              return (
                <Link
                  key={validation.id}
                  href={`/validation/detail?id=${validation.id}`}
                  className={`block px-5 py-4 transition-colors hover:bg-sky-50/40 dark:hover:bg-sky-500/5 ${divider}`}
                >
                  <div className="flex items-center justify-between gap-4">
                    <div className="flex flex-1 items-center gap-4 min-w-0">
                      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sky-50 dark:bg-sky-500/10">
                        <svg className="h-4 w-4 animate-spin text-sky-600 dark:text-sky-300" fill="none" viewBox="0 0 24 24">
                          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                        </svg>
                      </span>
                      <div className="flex-1 min-w-0">
                        <p className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
                          {validation.documentName}
                        </p>
                        <p className="mt-0.5 font-mono text-[11px] text-slate-400 dark:text-slate-500">
                          {validation.id}
                        </p>
                      </div>
                    </div>
                    <div className="shrink-0 text-right">
                      <p className="text-[10px] font-semibold uppercase tracking-normal text-sky-600 dark:text-sky-300">
                        Validating
                      </p>
                      <p className="mt-0.5 font-mono text-xs tabular-nums text-slate-500 dark:text-slate-400">
                        {validation.totalStandards > 0
                          ? `${validation.evaluatedCount} / ${validation.totalStandards}`
                          : "Starting…"}
                      </p>
                    </div>
                  </div>
                  {validation.totalStandards > 0 && (
                    <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-sky-100 dark:bg-sky-500/15">
                      <div
                        className="h-full rounded-full bg-sky-500 transition-all duration-500 ease-out"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  )}
                </Link>
              );
            }

            const scoreTone =
              validation.score === null ? "text-slate-400 dark:text-slate-500"
              : validation.score >= 90 ? "text-emerald-600 dark:text-emerald-300"
              : validation.score >= 70 ? "text-amber-600 dark:text-amber-300"
              : "text-rose-600 dark:text-rose-300";
            const statusTone =
              validation.status === "completed"
                ? "bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20"
                : validation.status === "cancelled"
                ? "bg-slate-100 text-slate-600 border-slate-300 dark:bg-white/10 dark:text-slate-300 dark:border-white/15"
                : "bg-rose-50 text-rose-700 border-rose-200/80 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20";

            return (
              <Link
                key={validation.id}
                href={`/validation/detail?id=${validation.id}`}
                className={`group flex items-center justify-between gap-4 px-5 py-4 transition-colors hover:bg-slate-50/80 dark:hover:bg-white/[0.03] ${divider}`}
              >
                <div className="flex flex-1 items-center gap-4 min-w-0">
                  <div className="w-12 text-right">
                    <p className={`text-2xl font-bold tabular-nums ${scoreTone}`}>
                      {validation.score === null ? "N/A" : validation.score}
                    </p>
                    <p className="text-[9px] uppercase tracking-normal text-slate-400 dark:text-slate-500">Score</p>
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="truncate text-sm font-semibold text-slate-900 transition-colors group-hover:text-[var(--color-brand-primary)] dark:text-slate-100">
                      {validation.documentName}
                    </p>
                    <p className="mt-0.5 font-mono text-[11px] text-slate-400 dark:text-slate-500">
                      {validation.id}
                    </p>
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-4">
                  <Badge variant="outline" className={statusTone + " text-[10px] font-semibold uppercase tracking-normal"}>
                    {validation.status}
                  </Badge>

                  {(validation.findingCounts.critical > 0 || validation.findingCounts.high > 0) && (
                    <div className="flex items-center gap-2">
                      {validation.findingCounts.critical > 0 && (
                        <span className="text-xs font-semibold text-rose-700 dark:text-rose-300">
                          {validation.findingCounts.critical} critical
                        </span>
                      )}
                      {validation.findingCounts.high > 0 && (
                        <span className="text-xs font-semibold text-orange-700 dark:text-orange-300">
                          {validation.findingCounts.high} high
                        </span>
                      )}
                    </div>
                  )}

                  <p className="text-[10px] text-slate-400 dark:text-slate-500">{validation.date}</p>

                  <ChevronRight className="h-4 w-4 text-slate-300 transition-colors group-hover:text-[var(--color-brand-primary)] dark:text-slate-600" />
                </div>
              </Link>
            );
          })}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-200/80 bg-white shadow-sm shadow-slate-900/[0.02] dark:border-white/8 dark:bg-white/[0.02] dark:shadow-black/20">
          <table className="w-full min-w-[760px] text-xs">
            <thead>
              <tr className="bg-slate-50/60 dark:bg-white/[0.03]">
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Score</th>
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Document</th>
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Status</th>
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Date</th>
                <th className="px-4 py-2.5 text-right text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Critical</th>
                <th className="px-4 py-2.5 text-right text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">High</th>
                <th className="px-4 py-2.5 text-right text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Medium</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200/60 dark:divide-white/8">
              {filtered.map((v) => {
                const isPending = v.status === "pending";
                const scoreTone =
                  v.score === null ? "text-slate-400 dark:text-slate-500"
                  : v.score >= 90 ? "text-emerald-600 dark:text-emerald-300"
                  : v.score >= 70 ? "text-amber-600 dark:text-amber-300"
                  : "text-rose-600 dark:text-rose-300";
                return (
                  <tr
                    key={v.id}
                    className="cursor-pointer transition-colors hover:bg-slate-50/60 dark:hover:bg-white/[0.03]"
                    onClick={() => { window.location.href = `/validation/detail?id=${v.id}`; }}
                  >
                    <td className="px-4 py-2.5">
                      {isPending ? (
                        <span className="inline-flex items-center gap-2 text-sky-600 dark:text-sky-300">
                          <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                          </svg>
                          <span className="font-mono tabular-nums">
                            {v.totalStandards > 0 ? `${v.evaluatedCount}/${v.totalStandards}` : "…"}
                          </span>
                        </span>
                      ) : (
                        <span className={`font-semibold tabular-nums ${scoreTone}`}>{v.score === null ? "N/A" : `${v.score}%`}</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 font-medium text-slate-700 dark:text-slate-200">{v.documentName}</td>
                    <td className="px-4 py-2.5">
                      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-normal ${
                        v.status === "completed" ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300"
                        : v.status === "failed" ? "bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-300"
                        : v.status === "cancelled" ? "bg-slate-100 text-slate-600 dark:bg-white/10 dark:text-slate-300"
                        : "bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300"
                      }`}>
                        {isPending ? "Validating" : v.status}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-slate-500 dark:text-slate-400">{isPending ? "In progress…" : v.date}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-rose-700 dark:text-rose-300">{isPending ? "—" : (v.findingCounts.critical || "—")}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-orange-700 dark:text-orange-300">{isPending ? "—" : (v.findingCounts.high || "—")}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-amber-700 dark:text-amber-300">{isPending ? "—" : (v.findingCounts.medium || "—")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

type ValAccent = "brand" | "pass" | "info" | "critical";
const VAL_ACCENT: Record<ValAccent, { dot: string; value: string }> = {
  brand:    { dot: "bg-[var(--color-brand-primary)]", value: "text-slate-900 dark:text-slate-50" },
  pass:     { dot: "bg-emerald-500", value: "text-emerald-700 dark:text-emerald-300" },
  info:     { dot: "bg-sky-500",     value: "text-sky-700 dark:text-sky-300" },
  critical: { dot: "bg-rose-500",    value: "text-rose-700 dark:text-rose-300" },
};

function ValTile({ label, value, accent }: { label: string; value: number | string; accent: ValAccent }) {
  const c = VAL_ACCENT[accent];
  return (
    <div className="rounded-xl border border-slate-200/80 bg-white p-3.5 transition-colors hover:border-slate-300/80 dark:border-white/8 dark:bg-white/[0.02] dark:hover:border-white/12">
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
