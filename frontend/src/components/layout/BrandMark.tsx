// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";

export function BrandMark({
  compact = false,
  inverse = false,
  className,
}: {
  compact?: boolean;
  inverse?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 items-center gap-2.5", className)}>
      <span
        className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
          inverse
            ? "bg-teal-300 text-[#10211f]"
            : "bg-[var(--color-brand-primary)] text-white",
        )}
        aria-hidden="true"
      >
        <ShieldCheck className="h-4.5 w-4.5" strokeWidth={2} />
      </span>
      {!compact && (
        <span className="min-w-0">
          <span
            className={cn(
              // Wraps rather than truncates: the full name is wider than the
              // 248px sidebar, and a cut-off product name reads as a bug.
              "block text-sm font-semibold leading-tight tracking-normal",
              inverse ? "text-white" : "text-slate-900 dark:text-slate-50",
            )}
          >
            Architecture Compliance Review
          </span>
          <span
            className={cn(
              "mt-1 block truncate text-[10px] leading-none tracking-normal",
              inverse ? "text-white/45" : "text-slate-500 dark:text-slate-400",
            )}
          >
            Standards assurance
          </span>
        </span>
      )}
    </div>
  );
}
