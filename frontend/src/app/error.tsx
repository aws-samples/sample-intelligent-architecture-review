// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Route-level error boundary.
 *
 * Without this, any exception thrown while rendering a page unmounts the whole
 * app shell and leaves a blank white screen with no way forward — the user can't
 * even navigate away. A stray value in a closed enum (a new rule `layer` added
 * backend-first, say) is enough to trigger it.
 *
 * Keep this deliberately plain: it has to render even when something upstream is
 * broken, so it avoids data fetching and app-specific components.
 */

import Link from "next/link";
import { useEffect } from "react";

export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Surfacing this beats swallowing it — the digest is what correlates a user
    // report with the server-side log entry.
    console.error("Unhandled render error:", error);
  }, [error]);

  return (
    <div className="flex min-h-[60vh] items-center justify-center px-6">
      <div className="max-w-md space-y-4 text-center">
        <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
          Something went wrong on this page
        </h1>
        <p className="text-sm text-slate-600 dark:text-slate-400">
          The rest of the app is still working. Try again, or head back to the
          dashboard.
        </p>
        {error.digest && (
          <p className="font-mono text-[11px] text-slate-400">
            Reference: {error.digest}
          </p>
        )}
        <div className="flex items-center justify-center gap-3 pt-1">
          <button
            type="button"
            onClick={reset}
            className="rounded-lg bg-[var(--color-brand-primary)] px-3 py-1.5 text-sm font-semibold text-white transition-colors hover:bg-[var(--color-brand-primary-hover)]"
          >
            Try again
          </button>
          <Link
            href="/"
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-50 dark:border-white/15 dark:text-slate-200 dark:hover:bg-white/10"
          >
            Back to dashboard
          </Link>
        </div>
      </div>
    </div>
  );
}
