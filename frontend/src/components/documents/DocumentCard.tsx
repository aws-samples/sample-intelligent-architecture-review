// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Document, DocumentStatus } from "@/lib/mock-data";

const statusConfig: Record<
  DocumentStatus,
  { label: string; className: string; tooltip: string }
> = {
  uploaded: {
    label: "Pending",
    className: "bg-muted text-muted-foreground border-border",
    tooltip: "Document uploaded, awaiting processing",
  },
  parsing: {
    label: "Parsing",
    className: "bg-[var(--color-status-info)]/10 text-[var(--color-status-info)] border-[var(--color-status-info)]/20",
    tooltip: "Document is being parsed and analyzed",
  },
  ready: {
    label: "Synced",
    className: "bg-[var(--color-status-pass)]/10 text-[var(--color-status-pass)] border-[var(--color-status-pass)]/20",
    tooltip: "Document parsed and ready for validation",
  },
  error: {
    label: "Failed",
    className: "bg-[var(--color-status-fail)]/10 text-[var(--color-status-fail)] border-[var(--color-status-fail)]/20",
    tooltip: "Processing failed - check document format",
  },
};

function getTimeSince(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays}d ago`;
  return dateStr;
}

export function DocumentCard({ document }: { document: Document }) {
  const status = statusConfig[document.status];

  return (
    <Link href={`/documents/detail?id=${document.id}`}>
      <Card className="transition-all hover:shadow-md hover:border-border/80 cursor-pointer group">
        <CardHeader className="flex-row items-start justify-between pb-2">
          <div className="flex-1 min-w-0">
            <CardTitle className="truncate text-sm">{document.name}</CardTitle>
            <span
              className="mt-0.5 inline-block font-mono text-[10px] text-slate-400 dark:text-slate-500"
              title={document.id}
            >
              {document.id.length > 12 ? `${document.id.slice(0, 12)}…` : document.id}
            </span>
          </div>
          <div className="flex items-center gap-1.5 flex-shrink-0">
            {/* Source-format badge. PowerPoint uploads
                are converted to PDF behind the scenes; this hint
                lets users link findings back to the original deck.
                Hidden for native PDFs to keep the header tidy. */}
            {document.sourceFormat === "pptx" && (
              <Badge
                variant="outline"
                className="text-[10px] px-2 py-0.5 bg-orange-500/10 text-orange-700 border-orange-500/20 dark:bg-orange-500/15 dark:text-orange-300"
                title={
                  document.slideCount
                    ? `Original PowerPoint deck, ${document.slideCount} slides — converted to PDF for processing`
                    : "Original PowerPoint deck — converted to PDF for processing"
                }
              >
                <svg className="w-2.5 h-2.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5h6m-3 0v14m0 0a4 4 0 01-4-4V9a4 4 0 014-4m0 14a4 4 0 004-4V9a4 4 0 00-4-4" />
                </svg>
                PPTX
              </Badge>
            )}
            <div className="relative group/badge">
              <Badge variant="outline" className={status.className + " text-[10px] px-2 py-0.5"}>
                {document.status === "parsing" && (
                  <svg className="w-2.5 h-2.5 mr-1 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                )}
                {status.label}
              </Badge>
              <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 hidden group-hover/badge:block z-50">
                <div className="bg-[var(--color-ink)] text-white text-[10px] px-2 py-1 rounded whitespace-nowrap">
                  {status.tooltip}
                </div>
              </div>
            </div>
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          {/* Sync status message */}
          {document.status === "ready" && (
            <div className="flex items-center gap-1.5 mb-2 text-[11px] text-[var(--color-status-pass)]">
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
              Ready for validation
            </div>
          )}
          {document.status === "error" && (
            <div className="flex items-center gap-1.5 mb-2 text-[11px] text-[var(--color-status-fail)]">
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              Processing failed - retry upload
            </div>
          )}

          <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
            <span
              className="flex items-center gap-1"
              title={
                document.uploadedBy
                  ? `Uploaded by ${document.uploadedBy}`
                  : "Time since upload"
              }
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              {getTimeSince(document.uploadDate)}
              {document.uploadedBy ? ` · ${document.uploadedBy}` : ""}
            </span>
            <span className="flex items-center gap-1">
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
              {document.pages} pages
            </span>
            <span className="flex items-center gap-1">
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4" />
              </svg>
              {document.size}
            </span>
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}
