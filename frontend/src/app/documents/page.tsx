// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  ChevronRight,
  FilePlus2,
  Files,
  Grid2X2,
  List,
  LoaderCircle,
  Upload,
} from "lucide-react";
import { DocumentUploadDrawer } from "@/components/documents/DocumentUploadDrawer";
import { ExtractionStatusBanner } from "@/components/extraction/ExtractionStatusBanner";
import { DocumentCard } from "@/components/documents/DocumentCard";
import { ProcessingTracker } from "@/components/documents/ProcessingTracker";
import { Badge } from "@/components/ui/badge";
import { PageHero } from "@/components/layout/PageHero";
import { api } from "@/lib/api";
import type { Document } from "@/lib/mock-data";

type ViewMode = "grid" | "table";
type DocSortKey = "recent" | "oldest" | "name" | "status";
type Tab = "sdd" | "standards";
type ApiDocument =
  Awaited<ReturnType<typeof api.listDocuments>>["documents"][number];

function adaptDocument(d: ApiDocument): Document {
  return {
    id: d.document_id,
    name: d.filename,
    uploadDate: d.uploaded_at,
    uploadedBy: d.uploaded_by ?? null,
    pages: d.page_count ?? 0,
    status:
      d.status === "parsed" || d.status === "analyzed"
        ? "ready"
        : d.status === "parsing" || d.status === "analyzing"
          ? "parsing"
          : d.status === "failed"
            ? "error"
            : "uploaded",
    size: d.file_size_bytes
      ? `${Math.round(d.file_size_bytes / 1024)} KB`
      : "Unknown",
    sourceFormat: d.source_format ?? "pdf",
    slideCount: d.slide_count ?? null,
  };
}

/**
 * Documents page — the source of truth for every document in the system.
 *
 * Splits the list into two distinct purposes that the backend already
 * tracks via document_type:
 *
 *   - "sdd"        → System Design Documents that get validated.
 *   - "standards"  → Policy/standards PDFs that the agent extracts
 *                    rules from and writes into the Standards library.
 *
 * Each tab has its own upload zone scoped to that purpose, with a
 * pointer telling the user when they're in the wrong place.
 */
export default function DocumentsPage() {
  const [tab, setTab] = useState<Tab>("sdd");
  const [sddDocs, setSddDocs] = useState<Document[]>([]);
  const [standardsDocs, setStandardsDocs] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [docSortBy, setDocSortBy] = useState<DocSortKey>("recent");
  const [uploadOpen, setUploadOpen] = useState(false);

  const fetchDocuments = useCallback(async (
    { isInitial = false }: { isInitial?: boolean } = {},
  ) => {
    try {
      // One request per type. We could combine into a single
      // unfiltered listDocuments, but the backend already filters
      // server-side and the responses are small.
      const [sdd, standards] = await Promise.all([
        api.listDocuments("sdd"),
        api.listDocuments("standards"),
      ]);
      setSddDocs(sdd.documents.map(adaptDocument));
      setStandardsDocs(standards.documents.map(adaptDocument));
      setError(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load documents";
      if (isInitial) {
        setError(message);
      } else {
        // Background refresh failures keep the prior list visible —
        // a single transient hiccup shouldn't wipe the page.
        console.warn("Document list refresh failed; keeping prior list:", message);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const kickoff = window.setTimeout(() => {
      void fetchDocuments({ isInitial: true });
    }, 0);
    return () => window.clearTimeout(kickoff);
  }, [fetchDocuments]);

  const activeDocs = tab === "sdd" ? sddDocs : standardsDocs;

  // Sort the visible documents. "recent" is the default (newest upload first).
  const sortedDocs = [...activeDocs].sort((a, b) => {
    switch (docSortBy) {
      case "name":
        return a.name.localeCompare(b.name);
      case "oldest":
        return (a.uploadDate || "").localeCompare(b.uploadDate || "");
      case "status":
        return a.status.localeCompare(b.status);
      case "recent":
      default:
        return (b.uploadDate || "").localeCompare(a.uploadDate || "");
    }
  });

  const syncCounts = {
    pending: activeDocs.filter((d) => d.status === "uploaded").length,
    parsing: activeDocs.filter((d) => d.status === "parsing").length,
    synced: activeDocs.filter((d) => d.status === "ready").length,
    failed: activeDocs.filter((d) => d.status === "error").length,
  };

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="flex h-32 items-center justify-center">
          <div className="text-center">
            <LoaderCircle className="mx-auto h-6 w-6 animate-spin text-[var(--color-brand-primary)]" />
            <p className="mt-3 text-sm text-muted-foreground">Loading documents…</p>
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-6">
        <div className="flex h-32 items-center justify-center">
          <div className="text-center">
            <p className="text-sm text-[var(--color-status-fail)]">Failed to load documents</p>
            <p className="mt-1 text-xs text-muted-foreground">{error}</p>
          </div>
        </div>
      </div>
    );
  }

  const totalCount = activeDocs.length;
  const subtitle = totalCount === 0
    ? tab === "sdd"
      ? "Upload a system design document to validate."
      : "Upload a standards or policy document. The AI will extract validation rules."
    : `${totalCount} document${totalCount === 1 ? "" : "s"} · ${syncCounts.synced} ready · ${syncCounts.parsing} processing`;

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <ExtractionStatusBanner />
      <PageHero
        title="Documents"
        subtitle={subtitle}
        actions={
          totalCount > 0 ? (
            <div className="flex items-center gap-2">
            <select
              value={docSortBy}
              onChange={(e) => setDocSortBy(e.target.value as DocSortKey)}
              title="Sort documents"
              className="h-8 rounded-lg border border-slate-200 bg-white px-2 text-xs text-slate-700 outline-none dark:border-white/10 dark:bg-white/5 dark:text-slate-200"
            >
              <option value="recent">Most recent</option>
              <option value="oldest">Oldest first</option>
              <option value="name">Name (A–Z)</option>
              <option value="status">Status</option>
            </select>
            <div className="inline-flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white p-0.5 dark:border-white/10 dark:bg-white/5">
              <button
                onClick={() => setViewMode("grid")}
                className={`rounded-md px-2.5 py-1.5 transition-all ${
                  viewMode === "grid"
                    ? "bg-[var(--color-brand-primary)] text-white shadow-sm"
                    : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white"
                }`}
                title="Grid view"
                aria-label="Grid view"
                aria-pressed={viewMode === "grid"}
              >
                <Grid2X2 className="h-3.5 w-3.5" />
              </button>
              <button
                onClick={() => setViewMode("table")}
                className={`rounded-md px-2.5 py-1.5 transition-all ${
                  viewMode === "table"
                    ? "bg-[var(--color-brand-primary)] text-white shadow-sm"
                    : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white"
                }`}
                title="Table view"
                aria-label="Table view"
                aria-pressed={viewMode === "table"}
              >
                <List className="h-3.5 w-3.5" />
              </button>
            </div>
            </div>
          ) : null
        }
      />

      {/* Tab bar — clearly distinguishes the two purposes. Counts on
          each tab so users see which library has content without
          flipping between them. */}
      <div className="-mx-1 flex items-center gap-1 overflow-x-auto border-b border-slate-200/80 px-1 dark:border-white/10">
        {(
          [
            { val: "sdd" as const, label: "System designs", count: sddDocs.length, hint: "Validated against HA standards" },
            { val: "standards" as const, label: "Standards documents", count: standardsDocs.length, hint: "Source of validation rules" },
          ]
        ).map(({ val, label, count, hint }) => (
          <button
            key={val}
            onClick={() => setTab(val)}
            className={
              "relative shrink-0 px-4 py-2.5 text-sm font-medium transition-all " +
              (tab === val
                ? "text-slate-900 after:absolute after:inset-x-3 after:bottom-0 after:h-[2px] after:rounded-full after:bg-[var(--color-brand-primary)] dark:text-slate-50"
                : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white")
            }
            title={hint}
          >
            {label}
            <span className="ml-1.5 font-mono text-[11px] text-slate-400 dark:text-slate-500">{count}</span>
          </button>
        ))}
      </div>

      {/* Cross-tab pointer — only renders when a user is on the wrong
          tab for what they're trying to do. The standards tab points
          back at SDDs; the SDD tab points at standards. */}
      <div className="rounded-xl border border-slate-200/80 bg-slate-50/40 px-4 py-2.5 text-xs text-slate-600 dark:border-white/8 dark:bg-white/[0.02] dark:text-slate-400">
        {tab === "sdd" ? (
          <>
            Uploading a <strong>standards or policy document</strong> instead?{" "}
            <button
              onClick={() => setTab("standards")}
              className="inline-flex items-center gap-0.5 font-medium text-[var(--color-brand-primary)] underline-offset-2 transition-colors hover:underline hover:text-[var(--color-brand-primary-hover)]"
            >
              Switch to Standards documents
              <ChevronRight className="h-3 w-3" />
            </button>
          </>
        ) : (
          <>
            Looking to <strong>validate a system design</strong>?{" "}
            <button
              onClick={() => setTab("sdd")}
              className="inline-flex items-center gap-0.5 font-medium text-[var(--color-brand-primary)] underline-offset-2 transition-colors hover:underline hover:text-[var(--color-brand-primary-hover)]"
            >
              Switch to System designs
              <ChevronRight className="h-3 w-3" />
            </button>{" "}
            ·{" "}
            <Link
              href="/standards"
              className="inline-flex items-center gap-0.5 font-medium text-[var(--color-brand-primary)] underline-offset-2 transition-colors hover:underline hover:text-[var(--color-brand-primary-hover)]"
            >
              Manage extracted rules
              <ChevronRight className="h-3 w-3" />
            </Link>
          </>
        )}
      </div>

      {/* Upload trigger — opens the right-side drawer. Scoped to the
          active tab so the drawer renders the right wizard variant
          (Standards has the structured Step 2; SDDs skip it). */}
      <div className="flex flex-col gap-3 rounded-lg border border-slate-200/80 bg-white px-4 py-3 dark:border-white/8 dark:bg-white/[0.025] sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-sm font-medium text-slate-900 dark:text-slate-100">
            {tab === "standards"
              ? "Add a standards document"
              : "Add a system design document"}
          </p>
          <p className="text-[11px] text-slate-500 dark:text-slate-400">
            {tab === "standards"
              ? "AI will extract validation rules into your library."
              : "Will be validated against your standards library."}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setUploadOpen(true)}
          className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg bg-[var(--color-brand-primary)] px-3.5 text-xs font-medium text-white transition-colors hover:bg-[var(--color-brand-primary-hover)]"
        >
          <Upload className="h-3.5 w-3.5" />
          Upload document
        </button>
      </div>

      <DocumentUploadDrawer
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        documentType={tab}
        onUploadComplete={() => fetchDocuments()}
      />

      {/* Real-time Processing Tracker — backend job feed is shared
          across types so we always render it. */}
      <ProcessingTracker />

      {/* Sync status pills */}
      {activeDocs.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {syncCounts.synced > 0 && (
            <Badge variant="outline" className="bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20">
              {syncCounts.synced} Synced
            </Badge>
          )}
          {syncCounts.parsing > 0 && (
            <Badge variant="outline" className="bg-sky-50 text-sky-700 border-sky-200/80 dark:bg-sky-500/10 dark:text-sky-300 dark:border-sky-500/20">
              {syncCounts.parsing} Parsing
            </Badge>
          )}
          {syncCounts.pending > 0 && (
            <Badge variant="secondary">{syncCounts.pending} Pending</Badge>
          )}
          {syncCounts.failed > 0 && (
            <Badge variant="outline" className="bg-rose-50 text-rose-700 border-rose-200/80 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20">
              {syncCounts.failed} Failed
            </Badge>
          )}
        </div>
      )}

      <div>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
            {tab === "sdd" ? "System Design Documents" : "Standards Documents"}
          </h2>
        </div>

        {activeDocs.length === 0 ? (
          <div className="rounded-lg border border-dashed border-slate-200 bg-slate-50/50 px-6 py-10 text-center dark:border-white/10 dark:bg-white/[0.02]">
            <span className="mx-auto flex h-10 w-10 items-center justify-center rounded-lg bg-white text-slate-400 ring-1 ring-slate-200 dark:bg-white/5 dark:ring-white/10">
              <Files className="h-4.5 w-4.5" />
            </span>
            <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
              {tab === "sdd" ? "No system designs yet" : "No standards documents yet"}
            </p>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              {tab === "sdd"
                ? "Use the upload form above to add your first SDD."
                : "Upload a standards/policy PDF and the AI will extract validation rules into the Standards library."}
            </p>
            <button
              type="button"
              onClick={() => setUploadOpen(true)}
              className="mt-4 inline-flex h-8 items-center gap-1.5 rounded-lg bg-[var(--color-brand-primary)] px-3 text-xs font-medium text-white transition-colors hover:bg-[var(--color-brand-primary-hover)]"
            >
              <FilePlus2 className="h-3.5 w-3.5" />
              Add document
            </button>
          </div>
        ) : viewMode === "grid" ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
            {sortedDocs.map((doc) => (
              <DocumentCard key={doc.id} document={doc} />
            ))}
          </div>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-slate-200/80 bg-white shadow-sm shadow-slate-900/[0.02] dark:border-white/8 dark:bg-white/[0.02] dark:shadow-black/20">
            {sortedDocs.map((doc, idx) => {
              const statusMap: Record<string, { label: string; tone: string }> = {
                uploaded: { label: "Pending", tone: "bg-slate-100 text-slate-600 dark:bg-white/8 dark:text-slate-300" },
                parsing: { label: "Parsing", tone: "bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-300" },
                ready: { label: "Synced", tone: "bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300" },
                error: { label: "Failed", tone: "bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-300" },
              };
              const s = statusMap[doc.status] || statusMap.uploaded;

              return (
                <Link
                  key={doc.id}
                  href={`/documents/detail?id=${doc.id}`}
                  className={`group flex min-w-[620px] items-center justify-between gap-4 px-5 py-4 transition-colors hover:bg-slate-50/80 dark:hover:bg-white/[0.03] ${
                    idx > 0 ? "border-t border-slate-200/60 dark:border-white/8" : ""
                  }`}
                >
                  <div className="flex-1 min-w-0">
                    <p className="truncate text-sm font-semibold text-slate-900 transition-colors group-hover:text-[var(--color-brand-primary)] dark:text-slate-100">
                      {doc.name}
                    </p>
                    <p className="mt-0.5 font-mono text-[11px] text-slate-400 dark:text-slate-500">
                      {doc.id}
                    </p>
                  </div>

                  <div className="flex items-center gap-4 shrink-0">
                    <span className={`inline-flex items-center rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-normal ${s.tone}`}>
                      {s.label}
                    </span>
                    <div className="text-right">
                      <p className="text-xs font-medium text-slate-700 dark:text-slate-200">{doc.pages} pages</p>
                      <p className="text-[10px] text-slate-400 dark:text-slate-500">{doc.size}</p>
                    </div>
                    <ChevronRight className="h-4 w-4 text-slate-300 transition-colors group-hover:text-[var(--color-brand-primary)] dark:text-slate-600" />
                  </div>
                </Link>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
