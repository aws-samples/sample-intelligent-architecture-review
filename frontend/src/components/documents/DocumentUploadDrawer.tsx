// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect } from "react";
import { DocumentUpload } from "./DocumentUpload";

interface DocumentUploadDrawerProps {
  open: boolean;
  onClose: () => void;
  documentType: "sdd" | "standards";
  onUploadComplete?: () => void;
}

export function DocumentUploadDrawer({
  open,
  onClose,
  documentType,
  onUploadComplete,
}: DocumentUploadDrawerProps) {
  // Lock body scroll while open + close on Escape.
  useEffect(() => {
    if (!open) return;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);

  return (
    <div
      aria-hidden={!open}
      className={
        "fixed inset-0 z-40 transition-opacity duration-200 " +
        (open ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none")
      }
    >
      {/* Backdrop */}
      <div
        onClick={onClose}
        className="absolute inset-0 bg-slate-900/30 backdrop-blur-sm dark:bg-slate-950/50"
      />

      {/* Drawer */}
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={
          documentType === "standards"
            ? "Upload standards document"
            : "Upload system design document"
        }
        className={
          "absolute right-0 top-0 flex h-full w-full max-w-[560px] flex-col border-l border-slate-200/80 bg-white shadow-2xl transition-transform duration-200 dark:border-white/10 dark:bg-slate-950 " +
          (open ? "translate-x-0" : "translate-x-full")
        }
      >
        <header className="flex items-center justify-between border-b border-slate-200/60 px-5 py-3.5 dark:border-white/8">
          <div>
            <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
              {documentType === "standards"
                ? "Upload standards document"
                : "Upload system design document"}
            </h2>
            <p className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
              {documentType === "standards"
                ? "AI will extract validation rules into your library."
                : "Will be validated against your standards library."}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-1.5 text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-white/10 dark:hover:text-white"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          <DocumentUpload
            key={documentType /* remount when switching tabs/types */}
            documentType={documentType}
            onUploadComplete={onUploadComplete}
          />
        </div>
      </aside>
    </div>
  );
}
