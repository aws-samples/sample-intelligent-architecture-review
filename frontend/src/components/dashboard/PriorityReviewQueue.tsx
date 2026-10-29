// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Link from "next/link";
import { ArrowUpRight, CheckCircle2 } from "lucide-react";

import {
  formatDashboardScore,
  isActiveValidationStatus,
  type DocumentPosture,
  type PriorityQueueItem,
} from "@/lib/dashboard-insights";

interface PriorityReviewQueueProps {
  items: PriorityQueueItem[];
}

const DOT_CLASSES: Record<DocumentPosture, string> = {
  pass: "bg-emerald-600",
  warning: "bg-amber-500",
  fail: "bg-rose-600",
  active: "bg-sky-600",
  unvalidated: "bg-slate-500",
};

const SCORE_CLASSES: Record<DocumentPosture, string> = {
  pass: "text-emerald-700 dark:text-emerald-300",
  warning: "text-amber-700 dark:text-amber-300",
  fail: "text-rose-600 dark:text-rose-300",
  active: "text-sky-700 dark:text-sky-300",
  unvalidated: "text-slate-500 dark:text-slate-400",
};

function relativeReviewAge(item: PriorityQueueItem): string {
  const reviewedAt =
    item.latestCompleted?.completed_at ?? item.latestCompleted?.started_at;
  if (!reviewedAt) return "Not reviewed";

  const timestamp = new Date(reviewedAt).getTime();
  if (!Number.isFinite(timestamp)) return "Review date unavailable";

  const elapsedMinutes = Math.max(
    0,
    Math.floor((Date.now() - timestamp) / 60_000),
  );
  if (elapsedMinutes < 1) return "Reviewed just now";
  if (elapsedMinutes < 60) return `Reviewed ${elapsedMinutes}m ago`;

  const elapsedHours = Math.floor(elapsedMinutes / 60);
  if (elapsedHours < 24) return `Reviewed ${elapsedHours}h ago`;

  const elapsedDays = Math.floor(elapsedHours / 24);
  if (elapsedDays < 30) return `Reviewed ${elapsedDays}d ago`;

  const elapsedMonths = Math.floor(elapsedDays / 30);
  if (elapsedMonths < 12) return `Reviewed ${elapsedMonths}mo ago`;

  return `Reviewed ${Math.floor(elapsedMonths / 12)}y ago`;
}

function findingLabel(count: number, severity: "critical" | "high") {
  return `${count} ${severity} ${count === 1 ? "finding" : "findings"}`;
}

function actionFor(item: PriorityQueueItem): "Review" | "Open" | "Start" {
  if (item.active) return "Open";
  if (!item.latestCompleted) return "Start";
  return "Review";
}

function scoreFor(item: PriorityQueueItem): string {
  if (!item.latestCompleted) return "Not reviewed";
  return formatDashboardScore(item.score);
}

function destinationFor(item: PriorityQueueItem): string {
  if (
    item.active &&
    item.latestRun &&
    isActiveValidationStatus(item.latestRun.status)
  ) {
    return `/validation/detail?id=${item.latestRun.validation_id}`;
  }

  return item.latestCompleted
    ? `/validation/detail?id=${item.latestCompleted.validation_id}`
    : `/documents/detail?id=${item.document_id}`;
}

export function PriorityReviewQueue({ items }: PriorityReviewQueueProps) {
  const visibleItems = items.slice(0, 6);

  return (
    <section
      aria-labelledby="priority-review-heading"
      className="overflow-hidden rounded-xl border border-slate-200/80 bg-white px-5 shadow-lg shadow-slate-900/[0.05] dark:border-white/10 dark:bg-white/[0.03] sm:px-6"
    >
      <header className="flex items-center justify-between gap-4 border-b border-slate-200/80 py-5 dark:border-white/10">
        <h2
          id="priority-review-heading"
          className="text-base font-semibold text-slate-950 dark:text-slate-50"
        >
          Priority review queue
        </h2>
        <Link
          className="shrink-0 rounded-md text-xs font-semibold text-[var(--color-brand-primary)] outline-none transition-colors hover:text-[var(--color-brand-primary-hover)] focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/35 focus-visible:ring-offset-2 dark:text-teal-300"
          href="/validation"
        >
          View all validations
        </Link>
      </header>

      {visibleItems.length === 0 ? (
        <div className="flex min-h-52 flex-col items-center justify-center py-10 text-center">
          <CheckCircle2
            aria-hidden="true"
            className="h-7 w-7 text-emerald-600 dark:text-emerald-400"
          />
          <h3 className="mt-3 text-sm font-semibold text-slate-900 dark:text-slate-100">
            Portfolio is clear
          </h3>
          <p className="mt-1 max-w-sm text-sm text-slate-500 dark:text-slate-400">
            No documents currently require priority review.
          </p>
        </div>
      ) : (
        <ul aria-label="Priority review queue items">
          {visibleItems.map((item) => {
            const action = actionFor(item);
            const actionVariant = action === "Start" ? "primary" : "outline";
            const severityDetails = [
              item.critical_count > 0
                ? findingLabel(item.critical_count, "critical")
                : null,
              item.high_count > 0
                ? findingLabel(item.high_count, "high")
                : null,
            ].filter((detail): detail is string => detail !== null);

            return (
              <li
                aria-label={`Priority review: ${item.filename}`}
                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 border-b border-slate-200/80 py-4 last:border-b-0 dark:border-white/10 sm:grid-cols-[minmax(0,1fr)_auto_auto]"
                key={item.document_id}
              >
                <div className="flex min-w-0 items-start gap-3">
                  <span
                    aria-hidden="true"
                    className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${DOT_CLASSES[item.posture]}`}
                  />
                  <div className="min-w-0">
                    <h3
                      className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100"
                      title={item.filename}
                    >
                      {item.filename}
                    </h3>
                    <p className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                      <span>{item.reason}</span>
                      {severityDetails.map((detail) => (
                        <span key={detail}> · {detail}</span>
                      ))}
                      <span> · {relativeReviewAge(item)}</span>
                    </p>
                  </div>
                </div>

                <p
                  className={`text-right text-sm font-semibold tabular-nums ${SCORE_CLASSES[item.posture]}`}
                  data-posture={item.posture}
                >
                  {scoreFor(item)}
                </p>

                <Link
                  aria-label={`${action} ${item.filename}`}
                  className={`col-span-2 mt-3 inline-flex h-8 items-center justify-center gap-1.5 justify-self-end rounded-lg border px-3 text-xs font-semibold outline-none transition-colors focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/35 focus-visible:ring-offset-2 sm:col-span-1 sm:mt-0 sm:min-w-20 ${
                    actionVariant === "primary"
                      ? "border-transparent bg-[var(--color-brand-primary)] text-white shadow-sm shadow-teal-950/15 hover:bg-[var(--color-brand-primary-hover)] dark:bg-teal-600 dark:hover:bg-teal-500"
                      : "border-slate-200 bg-white text-slate-700 hover:border-teal-300 hover:bg-teal-50 hover:text-[var(--color-brand-primary)] dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:border-teal-400/40 dark:hover:bg-teal-400/10 dark:hover:text-teal-200"
                  }`}
                  data-variant={actionVariant}
                  href={destinationFor(item)}
                >
                  {action}
                  <ArrowUpRight
                    aria-hidden="true"
                    className="h-3.5 w-3.5"
                    data-icon="arrow-up-right"
                  />
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
