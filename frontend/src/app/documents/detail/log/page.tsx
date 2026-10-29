// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Standalone extraction-log view — opened in a new tab from the doc-detail
 * ExtractionLogPanel ("Open" button). Full-width, self-contained, so a log
 * can be watched or shared for debugging without the rest of the detail page.
 */

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { ExtractionLogPanel } from "@/components/extraction/ExtractionLogPanel";

function LogViewInner() {
  const searchParams = useSearchParams();
  // Robust id resolution at render time (same static-export soft-nav caveat
  // as doc-detail): prefer the hook, fall back to window.location.
  const id =
    searchParams.get("id") ||
    (typeof window !== "undefined"
      ? new URLSearchParams(window.location.search).get("id") || ""
      : "");

  return (
    <div className="flex h-screen flex-col gap-3 p-4 sm:p-6">
      <div className="flex shrink-0 items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
            Extraction log
          </h1>
          <p className="font-mono text-xs text-slate-500 dark:text-slate-400">{id}</p>
        </div>
        <Link
          href={`/documents/detail/?id=${encodeURIComponent(id)}`}
          className="text-xs font-medium text-[var(--color-brand-primary)] hover:underline"
        >
          ← Back to document
        </Link>
      </div>
      {id ? (
        <div className="min-h-0 flex-1">
          <ExtractionLogPanel documentId={id} fullHeight />
        </div>
      ) : (
        <p className="text-sm text-slate-500">No document id provided.</p>
      )}
    </div>
  );
}

export default function ExtractionLogPage() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-slate-500">Loading log…</div>}>
      <LogViewInner />
    </Suspense>
  );
}
