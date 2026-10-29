// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { api, HostingScope } from "@/lib/api";
import { ExtractionStartedModal } from "./ExtractionStartedModal";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface DocumentUploadProps {
  onUploadComplete?: () => void;
  documentType?: "sdd" | "standards";
}

const HOSTING_SCOPE_LABELS: Record<HostingScope, string> = {
  on_prem: "On-prem",
  cloud: "Cloud",
  both: "Universal (both)",
};

const HOSTING_SCOPE_HINTS: Record<HostingScope, string> = {
  on_prem: "Rules apply to systems running in privately managed datacenters.",
  cloud: "Rules apply to systems running in cloud (AWS, Azure, GCP).",
  both: "Rules apply regardless of where the system runs.",
};

interface UploadStatus {
  filename: string;
  status: "uploading" | "success" | "error";
  message?: string;
  documentId?: string;
}

type Layout = "table" | "narrative" | "mixed";
const LAYOUT_LABELS: Record<Layout, string> = {
  table: "In a table (rows = rules, columns = tiers/layers)",
  narrative: "Written out as paragraphs",
  mixed: "Some of both",
};

const ALL_TIERS: number[] = [1, 2, 3, 4, 5];

/** Compose the structured Standards-doc answers into the single
 *  free-text extraction_notes string the backend agent already
 *  consumes. Keeping the wire contract unchanged means the wizard is
 *  purely a frontend UX layer. */
function composeStandardsNotes(input: {
  tiers: number[];
  layout: Layout;
  hasRelations: boolean;
  relationsDetail: string;
  extra: string;
}): string {
  const lines: string[] = [];
  if (input.tiers.length === 0 || input.tiers.length === ALL_TIERS.length) {
    lines.push("Tiers covered: all (T1–T5).");
  } else {
    lines.push(`Tiers covered: ${input.tiers.map((t) => `T${t}`).join(", ")}.`);
  }
  lines.push(`Layout: ${LAYOUT_LABELS[input.layout]}.`);
  if (input.hasRelations) {
    const detail = input.relationsDetail.trim();
    lines.push(
      detail
        ? `Exceptions/alternatives: ${detail}`
        : "Exceptions/alternatives: present (location not specified).",
    );
  } else {
    lines.push("Exceptions/alternatives: none — every rule stands alone.");
  }
  const extra = input.extra.trim();
  if (extra) {
    lines.push(`Additional notes: ${extra}`);
  }
  return lines.join("\n");
}

export function DocumentUpload({ onUploadComplete, documentType = "sdd" }: DocumentUploadProps) {
  const [uploading, setUploading] = useState(false);
  const [uploadStatus, setUploadStatus] = useState<UploadStatus | null>(null);
  // Post-submit modal so the user gets a clear CTA to track the
  // extraction live, not just the easy-to-miss banner.
  const [progressModalOpen, setProgressModalOpen] = useState(false);
  // Cloud upload guardrail — replaces window.confirm() so the
  // dialog matches the rest of the app's design system.
  const [cloudConfirmOpen, setCloudConfirmOpen] = useState(false);

  // Step 1 — hosting scope. Universal avoids assuming a deployment model.
  const [hostingScope, setHostingScope] = useState<HostingScope>("both");

  // Step 2 — Standards-only structured questions. Each field has a
  // sensible default so a user who has nothing to add can still
  // submit immediately.
  const [tiers, setTiers] = useState<number[]>([...ALL_TIERS]);
  const [layout, setLayout] = useState<Layout>("mixed");
  const [hasRelations, setHasRelations] = useState<boolean>(false);
  const [relationsDetail, setRelationsDetail] = useState<string>("");
  const [extraNotes, setExtraNotes] = useState<string>("");

  // Step 3 — staged file. Only populated after Choose; the user
  // submits explicitly via the Submit button so they always see what
  // they're committing.
  const [stagedFile, setStagedFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);

  const composedNotes = useMemo(
    () =>
      documentType === "standards"
        ? composeStandardsNotes({
            tiers,
            layout,
            hasRelations,
            relationsDetail,
            extra: extraNotes,
          })
        : "",
    [documentType, tiers, layout, hasRelations, relationsDetail, extraNotes],
  );

  const canSubmit = !uploading && stagedFile !== null && fileError === null;

  function validateFile(file: File): string | null {
    // .pptx is upload-allowed alongside PDF. The backend
    // converts PPTX -> PDF via the pptx-converter sidecar at upload
    // time; from the user's POV it behaves exactly like a PDF
    // upload, just with a "Source: PowerPoint" badge in the UI.
    const allowedExts = [".pdf", ".pptx", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"];
    const ok = allowedExts.some((ext) => file.name.toLowerCase().endsWith(ext));
    if (!ok) return "Supported formats: PDF, PPTX, PNG, JPG, TIFF, BMP";
    if (file.size > 100 * 1024 * 1024) return "File exceeds the 100 MB limit";
    return null;
  }

  function handleChooseFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    const err = validateFile(f);
    setFileError(err);
    setStagedFile(err ? null : f);
  }

  async function handleSubmit() {
    if (!stagedFile || uploading) return;
    // Cloud uploads need explicit confirmation — gate via the
    // styled Dialog (replaces window.confirm). Skipping the gate
    // for non-cloud is unchanged.
    if (hostingScope === "cloud") {
      setCloudConfirmOpen(true);
      return;
    }
    await runUpload();
  }

  async function runUpload() {
    if (!stagedFile || uploading) return;
    setCloudConfirmOpen(false);

    setUploading(true);
    setUploadStatus({ filename: stagedFile.name, status: "uploading" });
    try {
      const result = await api.uploadDocument(stagedFile, documentType, {
        extractionNotes: documentType === "standards" ? composedNotes : undefined,
        hostingScope,
      });
      setUploadStatus({
        filename: stagedFile.name,
        status: "success",
        documentId: result.document_id,
        message:
          documentType === "standards"
            ? "Uploaded. The agent is now extracting rules — open the document to watch progress."
            : "Uploaded. The document is being parsed for validation.",
      });
      // Open the post-submit modal so the user gets a clear CTA to
      // view live progress.
      setProgressModalOpen(true);
      // Clear the staged file so the form resets for the next upload.
      setStagedFile(null);
      onUploadComplete?.();
    } catch (error) {
      setUploadStatus({
        filename: stagedFile.name,
        status: "error",
        message: error instanceof Error ? error.message : "Upload failed",
      });
    } finally {
      setUploading(false);
    }
  }

  function toggleTier(t: number) {
    setTiers((prev) => (prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t].sort()));
  }

  return (
    <div className="space-y-3">
      {/* Purpose-aware caption */}
      <div className="flex items-baseline gap-2 px-1">
        <span className="inline-flex items-center gap-1 rounded-full border border-[var(--color-brand-primary)]/20 bg-[var(--color-brand-primary)]/[0.08] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-normal text-[var(--color-brand-primary)] dark:border-[var(--color-brand-primary)]/30 dark:bg-[var(--color-brand-primary)]/15 dark:text-[#5eead4]">
          {documentType === "standards" ? "Standards" : "SDD"}
        </span>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          {documentType === "standards"
            ? "Upload a policy/standards document. AI will extract validation rules into your library."
            : "Upload a system design document to validate against your standards library."}
        </span>
      </div>

      {/* Step 1 — Hosting scope */}
      <StepCard step={1} title="Hosting scope">
        <div className="grid gap-2 sm:grid-cols-3">
          {(Object.keys(HOSTING_SCOPE_LABELS) as HostingScope[]).map((s) => {
            const active = hostingScope === s;
            return (
              <button
                key={s}
                type="button"
                onClick={() => setHostingScope(s)}
                disabled={uploading}
                className={
                  "flex flex-col items-start gap-1 rounded-lg border px-3 py-2 text-left transition-all disabled:opacity-60 " +
                  (active
                    ? "border-[var(--color-brand-primary)] bg-[var(--color-brand-primary)]/[0.06] dark:border-[var(--color-brand-primary)]/60"
                    : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:hover:bg-white/10")
                }
              >
                <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                  {HOSTING_SCOPE_LABELS[s]}
                </span>
                <span className="text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
                  {HOSTING_SCOPE_HINTS[s]}
                </span>
              </button>
            );
          })}
        </div>
        <p className="mt-2 text-[11px] text-slate-500 dark:text-slate-400">
          {documentType === "sdd"
            ? "Where this system is deployed. The validator only runs rules whose scope matches (or are Universal)."
            : "The agent will tag every extracted rule with this scope. You can curate exceptions later."}
        </p>
      </StepCard>

      {/* Step 2 — Standards-only document context */}
      {documentType === "standards" && (
        <StepCard step={2} title="Tell us about this document">
          {/* Tiers covered */}
          <FieldRow label="Tiers covered" hint="Default: all 5">
            <div className="flex flex-wrap items-center gap-1.5">
              {ALL_TIERS.map((t) => {
                const active = tiers.includes(t);
                return (
                  <button
                    key={t}
                    type="button"
                    onClick={() => toggleTier(t)}
                    disabled={uploading}
                    className={
                      "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-all disabled:opacity-60 " +
                      (active
                        ? "border-[var(--color-brand-primary)]/30 bg-[var(--color-brand-primary)]/8 text-[var(--color-brand-primary)] dark:border-[var(--color-brand-primary)]/40 dark:bg-[var(--color-brand-primary)]/15 dark:text-[#5eead4]"
                        : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10")
                    }
                  >
                    T{t}
                  </button>
                );
              })}
              <button
                type="button"
                onClick={() => setTiers([...ALL_TIERS])}
                disabled={uploading || tiers.length === ALL_TIERS.length}
                className="ml-2 text-[11px] font-medium text-[var(--color-brand-primary)] transition-colors hover:text-[var(--color-brand-primary-hover)] disabled:opacity-50"
              >
                Use default
              </button>
            </div>
          </FieldRow>

          {/* Layout */}
          <FieldRow label="How are rules laid out?" hint="Default: Some of both">
            <div className="space-y-1.5">
              {(Object.keys(LAYOUT_LABELS) as Layout[]).map((l) => (
                <label key={l} className="flex cursor-pointer items-start gap-2 text-xs text-slate-700 dark:text-slate-200">
                  <input
                    type="radio"
                    name="layout"
                    value={l}
                    checked={layout === l}
                    onChange={() => setLayout(l)}
                    disabled={uploading}
                    className="mt-0.5 accent-[var(--color-brand-primary)]"
                  />
                  <span>{LAYOUT_LABELS[l]}</span>
                </label>
              ))}
            </div>
          </FieldRow>

          {/* Exceptions / alternatives */}
          <FieldRow
            label="Does this document have exceptions or alternatives to its main rules?"
            hint="Default: No"
          >
            <div className="space-y-1.5">
              <label className="flex cursor-pointer items-start gap-2 text-xs text-slate-700 dark:text-slate-200">
                <input
                  type="radio"
                  name="relations"
                  checked={!hasRelations}
                  onChange={() => setHasRelations(false)}
                  disabled={uploading}
                  className="mt-0.5 accent-[var(--color-brand-primary)]"
                />
                <span>No — every rule stands alone</span>
              </label>
              <label className="flex cursor-pointer items-start gap-2 text-xs text-slate-700 dark:text-slate-200">
                <input
                  type="radio"
                  name="relations"
                  checked={hasRelations}
                  onChange={() => setHasRelations(true)}
                  disabled={uploading}
                  className="mt-0.5 accent-[var(--color-brand-primary)]"
                />
                <span>Yes — describe below</span>
              </label>
              {hasRelations && (
                <textarea
                  value={relationsDetail}
                  onChange={(e) => setRelationsDetail(e.target.value)}
                  disabled={uploading}
                  rows={3}
                  placeholder='e.g. "Page 7–9 list alternatives if the Tier-1 rule on p.3 cannot be met. Page 12 lists explicit exceptions for legacy systems."'
                  className="mt-1 w-full resize-y rounded-md border border-slate-200 bg-white px-3 py-2 text-xs leading-relaxed text-slate-900 transition-colors placeholder:text-slate-400 hover:border-slate-300 focus:border-[var(--color-brand-primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 disabled:opacity-60 dark:border-white/15 dark:bg-white/5 dark:text-slate-100 dark:placeholder:text-slate-500"
                />
              )}
            </div>
          </FieldRow>

          {/* Extra notes */}
          <FieldRow label="Anything specific the agent should know?" hint="Optional">
            <textarea
              value={extraNotes}
              onChange={(e) => setExtraNotes(e.target.value)}
              disabled={uploading}
              rows={3}
              placeholder='e.g. "Skip the executive summary on pp.1-2"'
              className="w-full resize-y rounded-md border border-slate-200 bg-white px-3 py-2 text-xs leading-relaxed text-slate-900 transition-colors placeholder:text-slate-400 hover:border-slate-300 focus:border-[var(--color-brand-primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 disabled:opacity-60 dark:border-white/15 dark:bg-white/5 dark:text-slate-100 dark:placeholder:text-slate-500"
            />
          </FieldRow>
        </StepCard>
      )}

      {/* Step 3 — Choose file */}
      <StepCard
        step={documentType === "standards" ? 3 : 2}
        title="Choose file"
      >
        <label className="flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed border-slate-200 bg-white px-4 py-6 text-center transition-colors hover:border-[var(--color-brand-primary)]/50 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:hover:bg-white/10">
          <svg className="h-6 w-6 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
          </svg>
          <span className="mt-2 text-sm font-medium text-[var(--color-ink)]">
            {stagedFile ? stagedFile.name : "Click to choose a file"}
          </span>
          <span className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
            PDF, PNG, JPG, TIFF, BMP — up to 50 MB
          </span>
          <input
            type="file"
            accept=".pdf,.pptx,application/vnd.openxmlformats-officedocument.presentationml.presentation,.png,.jpg,.jpeg,.tiff,.bmp"
            className="hidden"
            onChange={handleChooseFile}
            disabled={uploading}
          />
        </label>
        {fileError && (
          <p className="mt-2 text-xs text-rose-700 dark:text-rose-300">{fileError}</p>
        )}
      </StepCard>

      {/* Review + Submit */}
      <div className="rounded-xl border border-slate-200/80 bg-slate-50/60 p-3 dark:border-white/10 dark:bg-white/[0.02]">
        <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-slate-400 dark:text-slate-500">
          Review
        </div>
        <ul className="space-y-1 text-xs text-slate-700 dark:text-slate-200">
          <li>
            <span className="text-slate-500 dark:text-slate-400">Hosting scope:</span>{" "}
            <strong>{HOSTING_SCOPE_LABELS[hostingScope]}</strong>
          </li>
          {documentType === "standards" && (
            <>
              <li>
                <span className="text-slate-500 dark:text-slate-400">Tiers covered:</span>{" "}
                <strong>
                  {tiers.length === ALL_TIERS.length
                    ? "All (T1–T5)"
                    : tiers.length === 0
                    ? "(none — defaulting to all)"
                    : tiers.map((t) => `T${t}`).join(", ")}
                </strong>
              </li>
              <li>
                <span className="text-slate-500 dark:text-slate-400">Layout:</span>{" "}
                <strong>{LAYOUT_LABELS[layout]}</strong>
              </li>
              <li>
                <span className="text-slate-500 dark:text-slate-400">Exceptions/alternatives:</span>{" "}
                <strong>
                  {hasRelations
                    ? relationsDetail.trim()
                      ? "yes (described)"
                      : "yes (no detail)"
                    : "none"}
                </strong>
              </li>
              <li>
                <span className="text-slate-500 dark:text-slate-400">Additional notes:</span>{" "}
                <strong>{extraNotes.trim() ? "provided" : "(none)"}</strong>
              </li>
            </>
          )}
          <li>
            <span className="text-slate-500 dark:text-slate-400">File:</span>{" "}
            <strong>
              {stagedFile
                ? `${stagedFile.name} (${(stagedFile.size / 1024 / 1024).toFixed(2)} MB)`
                : "(not chosen yet)"}
            </strong>
          </li>
        </ul>
        <div className="mt-3 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="inline-flex items-center gap-1.5 rounded-md bg-[var(--color-brand-primary)] px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-[var(--color-brand-primary-hover)] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {uploading ? (
              <>
                <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                Uploading…
              </>
            ) : (
              "Submit upload"
            )}
          </button>
        </div>
      </div>

      {/* Status banner */}
      {uploadStatus && !uploading && (
        <div
          className={
            "flex items-start justify-between gap-3 rounded-md px-3 py-2.5 text-xs " +
            (uploadStatus.status === "success"
              ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300"
              : "bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-300")
          }
        >
          <div className="flex min-w-0 items-start gap-2">
            {uploadStatus.status === "success" ? (
              documentType === "standards" ? (
                <svg className="mt-0.5 h-4 w-4 shrink-0 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              ) : (
                <svg className="mt-0.5 h-4 w-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
              )
            ) : (
              <svg className="mt-0.5 h-4 w-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            )}
            <span className="break-words">
              <strong>{uploadStatus.filename}</strong>
              {uploadStatus.documentId && (
                <span className="ml-1.5 font-mono text-[10px] opacity-75">
                  {uploadStatus.documentId}
                </span>
              )}
              {uploadStatus.message && ` — ${uploadStatus.message}`}
            </span>
          </div>
          {uploadStatus.status === "success" && uploadStatus.documentId && (
            <Link
              href={`/documents/detail?id=${uploadStatus.documentId}`}
              className="shrink-0 whitespace-nowrap font-medium underline-offset-2 transition-opacity hover:underline"
            >
              {documentType === "standards" ? "View extraction →" : "View document →"}
            </Link>
          )}
        </div>
      )}

      {/* Post-submit modal. Clearer than the banner alone for users
          who closed the drawer too quickly. */}
      {uploadStatus?.status === "success" && uploadStatus.documentId && (
        <ExtractionStartedModal
          open={progressModalOpen}
          documentId={uploadStatus.documentId}
          filename={uploadStatus.filename}
          documentType={documentType}
          onClose={() => setProgressModalOpen(false)}
        />
      )}

      {/* Cloud-scope confirmation dialog. Replaces window.confirm()
          so the prompt matches the rest of the app's design. */}
      <Dialog open={cloudConfirmOpen} onOpenChange={setCloudConfirmOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <div className="flex items-start gap-3">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-amber-500/15 ring-1 ring-amber-500/30">
                <svg className="h-4 w-4 text-amber-600 dark:text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </span>
              <div className="min-w-0 flex-1">
                <DialogTitle className="text-sm">
                  Confirm cloud scope
                </DialogTitle>
                <p className="mt-1.5 text-xs leading-relaxed text-slate-600 dark:text-slate-400">
                  {documentType === "sdd"
                    ? "Validate this SDD as a CLOUD deployment? It will be checked against cloud + universal rules only — on-prem rules will be skipped."
                    : "Tag every rule extracted from this document as CLOUD scope? Switch hosting scope first if this isn't a cloud standards doc."}
                </p>
              </div>
            </div>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setCloudConfirmOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" onClick={runUpload}>
              Confirm cloud scope
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function StepCard({
  step,
  title,
  children,
}: {
  step: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-slate-200/80 bg-white p-3 dark:border-white/8 dark:bg-white/[0.02]">
      <div className="mb-2 flex items-center gap-2">
        <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-[var(--color-brand-primary)] px-1.5 text-[10px] font-semibold text-white">
          {step}
        </span>
        <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">{title}</span>
      </div>
      {children}
    </div>
  );
}

function FieldRow({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="border-t border-slate-200/60 pt-2.5 first:border-0 first:pt-0 dark:border-white/8">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <label className="text-xs font-medium text-slate-700 dark:text-slate-200">{label}</label>
        {hint && (
          <span className="text-[10px] text-slate-400 dark:text-slate-500">{hint}</span>
        )}
      </div>
      {children}
    </div>
  );
}
