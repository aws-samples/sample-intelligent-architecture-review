// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Always-visible extraction status banner.
 *
 * Polls /api/standards/extraction-status every 5s. When at least one
 * job is in flight (status: starting/extracting), renders a
 * one-line banner:
 *
 *   "Extracting rules from Architecture Rulebook.pdf — 53 saved
 *    so far → View progress"
 *
 * Click → deep-link to /documents/detail?id=… where the live log panel
 * is rendered.
 *
 * Used on the Documents and Standards pages so a user who closed the
 * upload drawer still has a clear signal that the agent is working.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type StatusResponse = Awaited<ReturnType<typeof api.getExtractionStatus>>;

const POLL_MS = 5000;

export function ExtractionStatusBanner() {
  const [data, setData] = useState<StatusResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    async function tick() {
      try {
        const res = await api.getExtractionStatus();
        if (!cancelled) setData(res);
      } catch {
        // ignore — banner just hides on error
      } finally {
        if (!cancelled) timer = setTimeout(tick, POLL_MS);
      }
    }
    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  if (!data || !data.is_extracting || !data.active_jobs || data.active_jobs.length === 0) return null;

  // For now show the first job; multi-job aggregation lands when
  // multi-file uploads do.
  const job = data.active_jobs[0];

  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-[var(--color-brand-primary)]/20 bg-gradient-to-br from-[var(--color-brand-primary)]/[0.06] to-transparent px-4 py-2.5 dark:border-[var(--color-brand-primary)]/30 dark:from-[var(--color-brand-primary)]/[0.10]">
      <div className="flex min-w-0 items-center gap-3">
        <svg className="h-4 w-4 shrink-0 animate-spin text-[var(--color-brand-primary)]" fill="none" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
        <span className="truncate text-xs text-slate-700 dark:text-slate-200">
          Extracting rules from{" "}
          <strong className="font-medium">{job.filename}</strong>
          {job.rules_saved > 0 && (
            <>
              {" — "}
              <span className="font-mono tabular-nums">{job.rules_saved}</span>{" "}
              saved so far
            </>
          )}
        </span>
      </div>
      <Link
        href={`/documents/detail?id=${job.document_id}`}
        className="shrink-0 whitespace-nowrap text-xs font-medium text-[var(--color-brand-primary)] underline-offset-2 transition-colors hover:underline hover:text-[var(--color-brand-primary-hover)]"
      >
        View progress →
      </Link>
    </div>
  );
}
