// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Link from "next/link";

import {
  formatDashboardScore,
  isActiveValidationStatus,
  type DashboardValidation,
} from "@/lib/dashboard-insights";

interface RecentActivityProps {
  items: DashboardValidation[];
  documentNames: Readonly<Record<string, string>>;
}

interface StatusPresentation {
  label: string;
  className: string;
}

const STATUS_PRESENTATION: Readonly<Record<string, StatusPresentation>> = {
  pending: {
    label: "Pending",
    className:
      "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-500/20 dark:bg-amber-500/10 dark:text-amber-300",
  },
  completed: {
    label: "Complete",
    className:
      "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/20 dark:bg-emerald-500/10 dark:text-emerald-300",
  },
  failed: {
    label: "Failed",
    className:
      "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300",
  },
  cancelled: {
    label: "Cancelled",
    className:
      "border-slate-200 bg-slate-100 text-slate-600 dark:border-white/15 dark:bg-white/10 dark:text-slate-300",
  },
};

const IN_PROGRESS_STATUS: StatusPresentation = {
  label: "In progress",
  className:
    "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-500/20 dark:bg-sky-500/10 dark:text-sky-300",
};

const UNKNOWN_STATUS: StatusPresentation = {
  label: "Unknown",
  className:
    "border-slate-200 bg-slate-50 text-slate-600 dark:border-white/15 dark:bg-white/5 dark:text-slate-300",
};

function presentationForStatus(status: string): StatusPresentation {
  if (status === "pending") return STATUS_PRESENTATION.pending;
  if (isActiveValidationStatus(status)) return IN_PROGRESS_STATUS;
  return STATUS_PRESENTATION[status] ?? UNKNOWN_STATUS;
}

function relativeStartTime(value: string): string {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "Start time unavailable";

  const elapsedMinutes = Math.max(
    0,
    Math.floor((Date.now() - timestamp) / 60_000),
  );
  if (elapsedMinutes < 1) return "Started just now";
  if (elapsedMinutes < 60) return `Started ${elapsedMinutes}m ago`;

  const elapsedHours = Math.floor(elapsedMinutes / 60);
  if (elapsedHours < 24) return `Started ${elapsedHours}h ago`;

  const elapsedDays = Math.floor(elapsedHours / 24);
  if (elapsedDays < 30) return `Started ${elapsedDays}d ago`;

  const elapsedMonths = Math.floor(elapsedDays / 30);
  if (elapsedMonths < 12) return `Started ${elapsedMonths}mo ago`;

  return `Started ${Math.floor(elapsedMonths / 12)}y ago`;
}

function coverageFor(item: DashboardValidation): string {
  if (
    typeof item.evaluated_count !== "number" ||
    !Number.isFinite(item.evaluated_count) ||
    typeof item.total_standards !== "number" ||
    !Number.isFinite(item.total_standards)
  ) {
    return "N/A";
  }

  return `${item.evaluated_count} / ${item.total_standards}`;
}

function findingsFor(count: number): string {
  return `${count} ${count === 1 ? "finding" : "findings"}`;
}

function scoreClassName(score: number | null): string {
  if (typeof score !== "number" || !Number.isFinite(score)) {
    return "text-slate-400 dark:text-slate-500";
  }
  if (score >= 90) return "text-emerald-700 dark:text-emerald-300";
  if (score >= 70) return "text-amber-700 dark:text-amber-300";
  return "text-rose-600 dark:text-rose-300";
}

export function RecentActivity({
  items,
  documentNames,
}: RecentActivityProps) {
  const visibleItems = items.slice(0, 5);

  return (
    <section aria-labelledby="recent-activity-heading">
      <header className="flex items-center justify-between gap-4">
        <h2
          id="recent-activity-heading"
          className="text-xl font-semibold tracking-tight text-slate-950 dark:text-slate-50"
        >
          Recent validation activity
        </h2>
        <Link
          className="shrink-0 rounded-md text-xs font-semibold text-[var(--color-brand-primary)] outline-none transition-colors hover:text-[var(--color-brand-primary-hover)] focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/35 focus-visible:ring-offset-2 dark:text-teal-300"
          href="/validation"
        >
          View all
        </Link>
      </header>

      {visibleItems.length === 0 ? (
        <div className="mt-4 rounded-xl border border-dashed border-slate-200 bg-white px-6 py-10 text-center dark:border-white/10 dark:bg-white/[0.02]">
          <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            No validation activity yet
          </p>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Completed and in-progress reviews will appear here.
          </p>
          <Link
            className="mt-4 inline-flex rounded-md text-xs font-semibold text-[var(--color-brand-primary)] outline-none hover:text-[var(--color-brand-primary-hover)] focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/35 focus-visible:ring-offset-2 dark:text-teal-300"
            href="/validation"
          >
            Browse validations
          </Link>
        </div>
      ) : (
        <div className="mt-4 overflow-x-auto rounded-xl border border-slate-200/80 bg-white shadow-sm shadow-slate-900/[0.03] dark:border-white/10 dark:bg-white/[0.03]">
          <table className="w-full table-fixed border-collapse text-left sm:table-auto">
            <thead>
              <tr className="border-b border-slate-200/80 text-[0.7rem] font-semibold uppercase tracking-wide text-slate-500 dark:border-white/10 dark:text-slate-400">
                <th className="w-32 px-3 py-4 sm:px-5" scope="col">
                  Status
                </th>
                <th className="px-3 py-4 sm:px-4" scope="col">
                  Document
                </th>
                <th className="hidden px-4 py-4 sm:table-cell" scope="col">
                  Evaluated
                </th>
                <th className="hidden px-4 py-4 md:table-cell" scope="col">
                  Findings
                </th>
                <th className="hidden px-4 py-4 lg:table-cell" scope="col">
                  Started
                </th>
                <th
                  className="w-16 px-3 py-4 text-right sm:w-20 sm:px-5"
                  scope="col"
                >
                  Score
                </th>
              </tr>
            </thead>
            <tbody>
              {visibleItems.map((item) => {
                const documentName =
                  documentNames[item.document_id] ?? item.document_id;
                const status = presentationForStatus(item.status);

                return (
                  <tr
                    aria-label={`Validation activity: ${documentName}`}
                    className="group relative border-b border-slate-200/80 last:border-b-0 dark:border-white/10"
                    key={item.validation_id}
                  >
                    <td className="whitespace-nowrap px-3 py-4 sm:px-5">
                      <span
                        className={`inline-flex rounded-full border px-2.5 py-1 text-[0.7rem] font-semibold ${status.className}`}
                        data-status={item.status}
                      >
                        {status.label}
                      </span>
                    </td>
                    <th
                      className="max-w-64 px-3 py-4 text-sm font-semibold text-slate-900 dark:text-slate-100 sm:px-4"
                      scope="row"
                    >
                      <Link
                        aria-label={`Open ${documentName} validation`}
                        className="block truncate rounded-sm outline-none transition-colors after:absolute after:inset-0 group-hover:text-[var(--color-brand-primary)] focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/35 focus-visible:ring-offset-2 dark:group-hover:text-teal-300"
                        href={`/validation/detail?id=${item.validation_id}`}
                        title={documentName}
                      >
                        {documentName}
                      </Link>
                    </th>
                    <td className="hidden whitespace-nowrap px-4 py-4 text-xs tabular-nums text-slate-600 dark:text-slate-300 sm:table-cell">
                      {coverageFor(item)}
                    </td>
                    <td className="hidden whitespace-nowrap px-4 py-4 text-xs tabular-nums text-slate-600 dark:text-slate-300 md:table-cell">
                      {findingsFor(item.findings_count)}
                    </td>
                    <td className="hidden whitespace-nowrap px-4 py-4 text-xs text-slate-500 dark:text-slate-400 lg:table-cell">
                      {relativeStartTime(item.started_at)}
                    </td>
                    <td
                      className={`whitespace-nowrap px-3 py-4 text-right text-sm font-semibold tabular-nums sm:px-5 ${scoreClassName(item.overall_score)}`}
                    >
                      {formatDashboardScore(item.overall_score)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
