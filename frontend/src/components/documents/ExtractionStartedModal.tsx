// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Post-submit confirmation modal.
 *
 * Aligned with the rest of the app: reuses the project's Dialog
 * primitive (Standards page, ValidationConfigDialog) so styling,
 * dark-mode handling, focus-trap, and overlay behavior are
 * consistent. Brand-red on the primary CTA matches the Submit
 * button in the upload drawer.
 */

import Link from "next/link";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface ExtractionStartedModalProps {
  open: boolean;
  documentId: string;
  filename: string;
  documentType: "sdd" | "standards";
  onClose: () => void;
}

export function ExtractionStartedModal({
  open,
  documentId,
  filename,
  documentType,
  onClose,
}: ExtractionStartedModalProps) {
  const detailHref = `/documents/detail?id=${documentId}`;
  const isStandards = documentType === "standards";

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[var(--color-brand-primary)]/10 ring-1 ring-[var(--color-brand-primary)]/20">
              {isStandards ? (
                <svg className="h-4 w-4 animate-spin text-[var(--color-brand-primary)]" fill="none" viewBox="0 0 24 24" aria-hidden="true">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              ) : (
                <svg className="h-4 w-4 text-[var(--color-brand-primary)]" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
              )}
            </span>
            <div className="min-w-0 flex-1">
              <DialogTitle className="text-sm">
                {isStandards ? "Extraction in progress" : "Upload received"}
              </DialogTitle>
              <p className="mt-1.5 text-xs leading-relaxed text-slate-600 dark:text-slate-400">
                <span className="break-all font-medium text-slate-700 dark:text-slate-200">
                  {filename}
                </span>
                {isStandards
                  ? " is being parsed and the AI agent is extracting rules. Phase A finds rules from the document text; Phase B then attaches diagrams to the right rules. Usually 1–3 minutes."
                  : " is being parsed for validation."}
              </p>
              <p className="mt-2 font-mono text-[11px] text-slate-400 dark:text-slate-500">
                {documentId}
              </p>
            </div>
          </div>
        </DialogHeader>

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>
            Stay here
          </Button>
          <Link
            href={detailHref}
            onClick={onClose}
            className="inline-flex h-7 items-center gap-1 rounded-[min(var(--radius-md),12px)] bg-[var(--color-brand-primary)] px-2.5 text-[0.8rem] font-medium text-white shadow-sm shadow-[var(--color-brand-primary)]/20 transition-all hover:bg-[var(--color-brand-primary-hover)] hover:shadow-md hover:shadow-[var(--color-brand-primary)]/25"
          >
            View live progress
            <svg className="ml-0.5 h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 12h14M13 5l7 7-7 7" />
            </svg>
          </Link>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
