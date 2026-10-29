// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Custom Icon Legend.
 *
 * Two authoring modes, cleanly separated:
 *  - TEXT: write a description; optionally "Polish with AI" (before/after) → save
 *    (goes straight to active).
 *  - IMAGE: upload → a multimodal model DERIVES the definition → the user reviews
 *    /edits/approves the draft. The definition comes from the image's pixels, not
 *    an unverified caption, so a mislabeled upload can't assert a false mapping.
 *  - BULK: many images (async interpret job → review queue) or a CSV/JSON of text
 *    rows (→ review queue). Drafts are approved (per-row or all) before going live.
 *
 * The reviewer ALWAYS sees the actual image next to the derived definition, so
 * approving is a real check on the model's reading — not a rubber-stamp of text.
 * Layout matches the References page. Only approved (active) entries are injected
 * into extraction/validation.
 */

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api, ICON_TYPES, IconImageCheck, IconLegendEntry, IconPairResult } from "@/lib/api";
import { useRole } from "@/lib/use-role";

const inputClass =
  "h-9 w-full rounded-md border border-slate-200 bg-white px-3 text-sm outline-none focus:border-[var(--color-brand-primary)]/50 dark:border-white/12 dark:bg-white/5 dark:text-white";

function confidenceTone(conf?: string): string {
  if (conf === "high") return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/20 dark:bg-emerald-500/10 dark:text-emerald-300";
  if (conf === "medium") return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-500/20 dark:bg-amber-500/10 dark:text-amber-300";
  return "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300";
}

/** An entry carries an image if it has an S3 key — independent of source_kind,
 *  so legacy item-5 rows (no source_kind) still show their image. */
function hasImage(e: IconLegendEntry): boolean {
  return Boolean(e.image_s3_key);
}

/** Thumbnail that lazily fetches the icon's stored image (auth-bound blob URL)
 *  and shows it. This is the crux of the review gate: the reviewer sees the
 *  actual pixels the model interpreted. Falls back to a placeholder. */
function IconThumb({
  iconId,
  size = 56,
  label,
  onLoadState,
}: {
  iconId: string;
  size?: number;
  label?: string;
  /** Lets a reviewing parent block approval until the image is actually
   *  visible — approving a derived definition you can't see is exactly the
   *  rubber-stamp this review gate exists to prevent. */
  onLoadState?: (state: "loading" | "loaded" | "failed") => void;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  // Held in a ref so a new callback identity doesn't re-trigger the image fetch.
  const onLoadStateRef = useRef(onLoadState);
  useEffect(() => {
    onLoadStateRef.current = onLoadState;
  }, [onLoadState]);

  useEffect(() => {
    let cancelled = false;
    let created: string | null = null;
    (async () => {
      const u = await api.getIconImageUrl(iconId);
      if (cancelled) {
        if (u) URL.revokeObjectURL(u);
        return;
      }
      if (u) {
        created = u;
        setUrl(u);
        onLoadStateRef.current?.("loaded");
      } else {
        setFailed(true);
        onLoadStateRef.current?.("failed");
      }
    })();
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [iconId]);

  const box = `flex shrink-0 items-center justify-center overflow-hidden rounded-md border border-slate-200 bg-slate-50 dark:border-white/10 dark:bg-white/5`;
  const style = { width: size, height: size };

  if (failed) {
    return (
      <div
        className={box}
        style={style}
        title="Image unavailable — cannot verify this icon"
        role="img"
        aria-label="Icon image unavailable"
      >
        <svg className="h-5 w-5 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14M4 6h16v12H4z" />
        </svg>
      </div>
    );
  }
  return (
    <div className={box} style={style}>
      {url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={url}
          alt={label ? `Icon: ${label}` : "Custom legend icon"}
          className="h-full w-full object-contain"
        />
      ) : (
        <div className="h-3 w-3 animate-pulse rounded-full bg-slate-300 dark:bg-white/20" />
      )}
    </div>
  );
}

export default function IconLegendPage() {
  const { isAdmin, role } = useRole();
  const canEdit = isAdmin || role === "curator";

  const [active, setActive] = useState<IconLegendEntry[]>([]);
  const [drafts, setDrafts] = useState<IconLegendEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showText, setShowText] = useState(false);
  const [showImage, setShowImage] = useState(false);
  const [showBulk, setShowBulk] = useState(false);

  // Drafts are curator/admin only on the backend (unreviewed AI content); don't
  // even request them as a viewer.
  async function loadAll(setInitial: boolean) {
    const reqs: [Promise<IconLegendEntry[]>, Promise<IconLegendEntry[]>] = [
      api.listIconLegend("active"),
      canEdit ? api.listIconLegend("draft") : Promise.resolve([]),
    ];
    try {
      const [a, d] = await Promise.all(reqs);
      setActive(a);
      setDrafts(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      if (setInitial) setLoading(false);
    }
  }

  async function reload() {
    await loadAll(false);
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const reqs: [Promise<IconLegendEntry[]>, Promise<IconLegendEntry[]>] = [
        api.listIconLegend("active"),
        canEdit ? api.listIconLegend("draft") : Promise.resolve([]),
      ];
      try {
        const [a, d] = await Promise.all(reqs);
        if (!cancelled) {
          setActive(a);
          setDrafts(d);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [canEdit]);

  // Group drafts by batch so a bulk upload becomes one reviewable group with an
  // "Approve all" action; single interpret drafts (no batch) share a group.
  const batches: Array<{ id: string | null; items: IconLegendEntry[] }> = [];
  const byBatch = new Map<string, IconLegendEntry[]>();
  const singles: IconLegendEntry[] = [];
  for (const d of drafts) {
    if (d.bulk_batch_id) {
      const arr = byBatch.get(d.bulk_batch_id) ?? [];
      arr.push(d);
      byBatch.set(d.bulk_batch_id, arr);
    } else {
      singles.push(d);
    }
  }
  for (const [id, items] of byBatch) batches.push({ id, items });
  if (singles.length) batches.push({ id: null, items: singles });

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      {/* Header — matches the References page layout */}
      <div className="space-y-3">
        <Link
          href="/standards"
          className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 transition-colors hover:text-[var(--color-brand-primary)] dark:text-slate-400"
        >
          <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Standards
        </Link>
        <div className="flex items-end justify-between gap-3">
          <div className="space-y-1">
            <h1 className="text-2xl font-semibold tracking-normal text-slate-900 dark:text-slate-50">
              Custom icon legend
            </h1>
            <p className="max-w-3xl text-sm text-slate-500 dark:text-slate-400">
              Teach extraction and validation your organisation&apos;s on-prem
              diagram icons. Describe an icon in words, or upload it and let AI
              interpret it for your review. Only approved entries are used when
              reading diagrams.
            </p>
          </div>
          {canEdit && (
            <div className="flex shrink-0 items-center gap-2">
              <Button size="sm" variant="outline" onClick={() => setShowText(true)}>
                + Text
              </Button>
              <Button size="sm" variant="outline" onClick={() => setShowImage(true)}>
                + Image
              </Button>
              <Button size="sm" onClick={() => setShowBulk(true)}>
                Bulk upload
              </Button>
            </div>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:border-rose-400/20 dark:bg-rose-400/10 dark:text-rose-200">
          {error}
        </div>
      )}

      {/* Review queue (drafts) — curator/admin only, only when present */}
      {canEdit && drafts.length > 0 && (
        <Card>
          <CardHeader className="border-b border-slate-200/60 px-5 py-3.5 dark:border-white/8">
            <CardTitle className="flex items-center gap-2 text-sm font-semibold">
              Pending review
              <span className="font-mono text-[10px] font-normal text-slate-400 dark:text-slate-500">
                {drafts.length}
              </span>
              <span className="ml-auto text-[11px] font-normal text-slate-500 dark:text-slate-400">
                AI-derived — check the image, then approve
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 p-4">
            {batches.map((b, i) => (
              <BatchGroup
                key={b.id ?? `singles-${i}`}
                batchId={b.id}
                items={b.items}
                onChanged={reload}
              />
            ))}
          </CardContent>
        </Card>
      )}

      {/* Active legend */}
      <Card>
        <CardHeader className="border-b border-slate-200/60 px-5 py-3.5 dark:border-white/8">
          <CardTitle className="text-sm font-semibold">
            Active legend
            {active.length > 0 && (
              <span className="ml-2 font-mono text-[10px] font-normal text-slate-400 dark:text-slate-500">
                {active.length}
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? (
            <div className="flex h-24 items-center justify-center text-sm text-slate-500">Loading…</div>
          ) : active.length === 0 ? (
            <div className="px-5 py-8 text-center text-sm text-slate-500 dark:text-slate-400">
              No active icons yet.
              {canEdit ? " Add one via Text or Image above." : ""}
            </div>
          ) : (
            <div className="divide-y divide-slate-200/60 dark:divide-white/8">
              {active.map((icon) => (
                <ActiveRow key={icon.id} icon={icon} canEdit={canEdit} onChanged={reload} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {showText && (
        <TextDialog onClose={() => setShowText(false)} onSaved={() => { setShowText(false); void reload(); }} />
      )}
      {showImage && (
        <ImageDialog onClose={() => setShowImage(false)} onDrafted={() => { setShowImage(false); void reload(); }} />
      )}
      {showBulk && (
        <BulkDialog onClose={() => setShowBulk(false)} onDone={() => { setShowBulk(false); void reload(); }} />
      )}
    </div>
  );
}

// ---- rows ----

/** File-picker button that attaches (or replaces) an entry's reference image.
 *
 *  A text-authored row has no image until someone attaches one, and without an
 *  image it contributes only a legend line — no visual example — to diagram
 *  prompts. This is the affordance that lets a bulk-uploaded CSV row acquire one.
 */
function AttachImageButton({
  iconId,
  hasExisting,
  disabled,
  onAttached,
  onError,
}: {
  iconId: string;
  hasExisting: boolean;
  disabled?: boolean;
  onAttached: (entry: IconLegendEntry) => void;
  onError: (message: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);

  const pick = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    try {
      onAttached(await api.uploadIconImage(iconId, file));
    } catch (e) {
      onError(e instanceof Error ? e.message : "Image upload failed");
    } finally {
      setBusy(false);
      // Clear the input so re-picking the same file fires onChange again.
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept="image/png,image/jpeg"
        className="hidden"
        onChange={(e) => void pick(e.target.files?.[0])}
      />
      <Button
        variant="outline"
        size="sm"
        disabled={disabled || busy}
        onClick={() => inputRef.current?.click()}
        title={
          hasExisting
            ? "Replace the reference image (re-checked against this definition)"
            : "Attach a reference image used as a visual example in diagram prompts"
        }
      >
        {busy ? "Checking…" : hasExisting ? "Replace image" : "Add image"}
      </Button>
    </>
  );
}

/** Reports the image/definition cross-check.
 *
 *  Shown only when it has something to say: a mismatch, or a check that could
 *  not run. Announcing every pass would train reviewers to ignore the banner,
 *  which is where the mismatch also has to appear.
 */
function ImageCheckNotice({
  check,
  demoted,
  onReplace,
  onDismiss,
}: {
  check: IconImageCheck;
  demoted?: boolean;
  onReplace?: React.ReactNode;
  onDismiss?: () => void;
}) {
  if (check.checked && check.consistent) return null;

  // An unavailable model is not evidence of a mismatch — say "unverified"
  // rather than implying the pairing is wrong.
  const unverified = !check.checked;
  const tone = unverified
    ? "border-slate-200 bg-slate-50 text-slate-600 dark:border-white/10 dark:bg-white/5 dark:text-slate-300"
    : "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-500/20 dark:bg-amber-500/10 dark:text-amber-200";

  return (
    <div className={`mt-2 rounded-md border px-3 py-2 text-xs ${tone}`}>
      {unverified ? (
        <p>
          The image couldn&apos;t be checked against this description right now,
          so the pairing is unverified. Confirm it yourself, or replace the image.
        </p>
      ) : (
        <>
          <p>
            <span className="font-medium">This image may not match the description.</span>{" "}
            {check.observed ? `The image appears to show ${check.observed}. ` : ""}
            {check.reason}
          </p>
          {demoted && (
            <p className="mt-1">
              This entry was returned to draft, because its image is used as a
              visual example in diagram prompts.
            </p>
          )}
        </>
      )}
      {(onReplace || onDismiss) && (
        <div className="mt-2 flex items-center gap-2">
          {onReplace}
          {onDismiss && (
            <Button variant="outline" size="sm" onClick={onDismiss}>
              Keep anyway
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

function TypeBadge({ type }: { type: string }) {
  return (
    <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-600 dark:border-white/10 dark:bg-white/5 dark:text-slate-300">
      {type}
    </span>
  );
}

/** One draft group — a bulk batch (with Approve-all / Reject-all) or the
 *  loose single-interpret drafts. */
function BatchGroup({
  batchId,
  items,
  onChanged,
}: {
  batchId: string | null;
  items: IconLegendEntry[];
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Drafts whose image failed to load. Bulk approve skips these: the review gate
  // exists so a human checks a model-derived definition against the pixels, and
  // approving one nobody could display would defeat it.
  const [unverifiable, setUnverifiable] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<null | "approve" | "reject" | "rejectAll">(null);
  const [pairResult, setPairResult] = useState<IconPairResult | null>(null);
  const pairInputRef = useRef<HTMLInputElement>(null);

  // Rows that declared an image filename in the CSV/JSON. Only these can be
  // paired, so the control stays hidden on a batch that never asked for images.
  const rowsAwaitingImages = items.filter((i) => i.image_filename && !i.image_s3_key);

  const ids = items.map((i) => i.id);
  // Selection is pruned to the rows still present, so a reload that removes a
  // row cannot leave a stale id selected and acted on.
  const selectedIds = ids.filter((id) => selected.has(id));
  const allSelected = ids.length > 0 && selectedIds.length === ids.length;
  const approvableIds = selectedIds.filter((id) => !unverifiable.has(id));
  const skippedCount = selectedIds.length - approvableIds.length;

  const toggleOne = useCallback((id: string, next: boolean) => {
    setSelected((prev) => {
      const copy = new Set(prev);
      if (next) copy.add(id);
      else copy.delete(id);
      return copy;
    });
  }, []);

  const setVerifiability = useCallback((id: string, isUnverifiable: boolean) => {
    setUnverifiable((prev) => {
      if (prev.has(id) === isUnverifiable) return prev;
      const copy = new Set(prev);
      if (isUnverifiable) copy.add(id);
      else copy.delete(id);
      return copy;
    });
  }, []);

  const toggleAll = () => {
    setSelected(allSelected ? new Set() : new Set(ids));
  };

  const pairImages = async (files: FileList | null) => {
    if (!batchId || !files || files.length === 0) return;
    setBusy(true);
    setErr(null);
    setPairResult(null);
    try {
      setPairResult(await api.pairIconBatchImages(batchId, Array.from(files)));
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Pairing failed");
    } finally {
      setBusy(false);
      if (pairInputRef.current) pairInputRef.current.value = "";
    }
  };

  /** Run one action per selected draft. Failures are counted rather than
   *  aborting the run, so one bad row does not strand the rest. */
  const runBulk = async (
    targetIds: string[],
    action: (id: string) => Promise<unknown>,
    verb: string,
  ) => {
    setBusy(true);
    setErr(null);
    let failures = 0;
    for (const id of targetIds) {
      try {
        await action(id);
      } catch {
        failures += 1;
      }
    }
    setSelected(new Set());
    setConfirm(null);
    if (failures > 0) {
      setErr(`${failures} of ${targetIds.length} could not be ${verb}.`);
    }
    // Cleared unconditionally: a partial selection leaves this group mounted, so
    // the flag cannot rely on a reload unmounting the component.
    setBusy(false);
    onChanged();
  };

  const approveSelected = () =>
    runBulk(approvableIds, (id) => api.approveIcon(id, {}), "approved");

  const rejectSelected = () =>
    runBulk(selectedIds, (id) => api.deleteIcon(id), "rejected");

  const approveAll = async () => {
    if (!batchId) return;
    setBusy(true);
    setErr(null);
    try {
      await api.approveIconBatch(batchId);
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Approve-all failed");
    } finally {
      setBusy(false);
    }
  };

  const rejectAll = async () => {
    if (!batchId) return;
    setBusy(true);
    setErr(null);
    try {
      await api.rejectIconBatch(batchId);
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Reject-all failed");
    } finally {
      setConfirm(null);
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="checkbox"
          className="h-4 w-4 accent-[var(--color-brand-primary)]"
          checked={allSelected}
          onChange={toggleAll}
          aria-label={allSelected ? "Clear selection" : "Select all in this group"}
        />
        <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400 dark:text-slate-500">
          {batchId ? `Batch ${batchId.replace(/^iconbatch-/, "").slice(0, 8)}` : "Single uploads"}
        </span>
        <span className="font-mono text-[10px] text-slate-400">{items.length}</span>

        {selectedIds.length > 0 ? (
          <span className="ml-auto flex items-center gap-2">
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {selectedIds.length} selected
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => setConfirm("reject")}
            >
              Reject selected
            </Button>
            <Button
              size="sm"
              disabled={busy || approvableIds.length === 0}
              title={
                approvableIds.length === 0
                  ? "Every selected draft has an image that could not be loaded"
                  : undefined
              }
              onClick={() => setConfirm("approve")}
            >
              Approve selected
            </Button>
          </span>
        ) : (
          batchId && (
            <span className="ml-auto flex items-center gap-2">
              {rowsAwaitingImages.length > 0 && (
                <>
                  <input
                    ref={pairInputRef}
                    type="file"
                    accept="image/png,image/jpeg"
                    multiple
                    className="hidden"
                    onChange={(e) => void pairImages(e.target.files)}
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => pairInputRef.current?.click()}
                    title="Upload the image files named in this batch's rows; each is matched to the row that named it"
                  >
                    {busy ? "Pairing…" : `Add images for ${rowsAwaitingImages.length}`}
                  </Button>
                </>
              )}
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => setConfirm("rejectAll")}
              >
                {`Reject all ${items.length}`}
              </Button>
              <Button variant="outline" size="sm" disabled={busy} onClick={approveAll}>
                {busy ? "…" : `Approve all ${items.length}`}
              </Button>
            </span>
          )
        )}
      </div>

      {confirm === "approve" && (
        <ConfirmBar
          busy={busy}
          message={
            skippedCount > 0
              ? `Approve ${approvableIds.length} draft${approvableIds.length === 1 ? "" : "s"}? ${skippedCount} selected draft${skippedCount === 1 ? " has an image that" : "s have images that"} could not be loaded and will be skipped.`
              : `Approve ${approvableIds.length} selected draft${approvableIds.length === 1 ? "" : "s"}?`
          }
          confirmLabel="Approve"
          onCancel={() => setConfirm(null)}
          onConfirm={approveSelected}
        />
      )}
      {confirm === "reject" && (
        <ConfirmBar
          busy={busy}
          destructive
          message={`Reject and delete ${selectedIds.length} selected draft${selectedIds.length === 1 ? "" : "s"}? This cannot be undone.`}
          confirmLabel="Reject"
          onCancel={() => setConfirm(null)}
          onConfirm={rejectSelected}
        />
      )}
      {confirm === "rejectAll" && (
        <ConfirmBar
          busy={busy}
          destructive
          message={`Reject and delete all ${items.length} draft${items.length === 1 ? "" : "s"} in this batch? This cannot be undone — re-upload to recover.`}
          confirmLabel="Reject all"
          onCancel={() => setConfirm(null)}
          onConfirm={rejectAll}
        />
      )}

      {err && <p className="text-xs text-rose-600 dark:text-rose-300">{err}</p>}
      {pairResult && (
        <PairReport result={pairResult} onDismiss={() => setPairResult(null)} />
      )}
      <div className="space-y-3">
        {items.map((d) => (
          <DraftRow
            key={d.id}
            draft={d}
            onChanged={onChanged}
            selected={selected.has(d.id)}
            onSelectChange={toggleOne}
            onVerifiabilityChange={setVerifiability}
          />
        ))}
      </div>
    </div>
  );
}

/** Outcome of a filename pairing run.
 *
 *  Reports what did NOT pair as prominently as what did: a partial pairing that
 *  only showed successes would read as complete, leaving rows without the visual
 *  example the curator believed they had attached.
 */
function PairReport({
  result,
  onDismiss,
}: {
  result: IconPairResult;
  onDismiss: () => void;
}) {
  const { paired, mismatches, unmatched_files, unmatched_rows, failures } = result;
  const problems =
    mismatches.length + unmatched_files.length + unmatched_rows.length + failures.length;

  return (
    <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs dark:border-white/10 dark:bg-white/5">
      <div className="flex items-center gap-2">
        <span className="font-medium text-slate-700 dark:text-slate-200">
          Paired {paired.length} image{paired.length === 1 ? "" : "s"}
          {problems === 0 ? " — all rows matched" : ""}
        </span>
        <Button variant="outline" size="sm" className="ml-auto" onClick={onDismiss}>
          Dismiss
        </Button>
      </div>
      {mismatches.length > 0 && (
        <div className="mt-2 text-amber-800 dark:text-amber-200">
          <p className="font-medium">
            {mismatches.length} may not match their description — check before approving:
          </p>
          <ul className="mt-1 list-disc pl-4">
            {mismatches.map((m) => (
              <li key={m.icon_id}>
                <span className="font-mono">{m.file}</span> → {m.label}
                {m.observed ? ` (image appears to show ${m.observed})` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
      {unmatched_files.length > 0 && (
        <p className="mt-2 text-slate-600 dark:text-slate-300">
          No row named{" "}
          <span className="font-mono">{unmatched_files.join(", ")}</span> — check the
          image_filename column.
        </p>
      )}
      {unmatched_rows.length > 0 && (
        <p className="mt-2 text-slate-600 dark:text-slate-300">
          Still waiting on an image:{" "}
          {unmatched_rows.map((u) => u.expected_file || u.label).join(", ")}
        </p>
      )}
      {failures.length > 0 && (
        <ul className="mt-2 list-disc pl-4 text-rose-600 dark:text-rose-300">
          {failures.map((f) => (
            <li key={f.file}>
              <span className="font-mono">{f.file}</span>: {f.error}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ConfirmBar({
  message,
  confirmLabel,
  onCancel,
  onConfirm,
  busy,
  destructive = false,
}: {
  message: string;
  confirmLabel: string;
  onCancel: () => void;
  onConfirm: () => void;
  busy: boolean;
  destructive?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 dark:border-white/10 dark:bg-white/5">
      <span className="mr-auto text-xs text-slate-600 dark:text-slate-300">{message}</span>
      <Button variant="outline" size="sm" disabled={busy} onClick={onCancel}>
        Cancel
      </Button>
      <Button
        variant={destructive ? "destructive" : "default"}
        size="sm"
        disabled={busy}
        onClick={onConfirm}
      >
        {busy ? "…" : confirmLabel}
      </Button>
    </div>
  );
}

function ActiveRow({ icon, canEdit, onChanged }: { icon: IconLegendEntry; canEdit: boolean; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(icon.label);
  const [iconType, setIconType] = useState(icon.icon_type);
  const [description, setDescription] = useState(icon.description ?? "");
  const [err, setErr] = useState<string | null>(null);
  // An attach in this session supersedes the row the parent last loaded, so the
  // new thumbnail and any warning appear without a reload.
  const [attached, setAttached] = useState<IconLegendEntry | null>(null);
  const current = attached ?? icon;
  const withImage = hasImage(current);

  const del = async () => {
    setBusy(true);
    try { await api.deleteIcon(icon.id); onChanged(); } catch { setBusy(false); }
  };
  const save = async () => {
    setBusy(true); setErr(null);
    try {
      await api.updateIcon(icon.id, { label, icon_type: iconType, description });
      setEditing(false);
      onChanged();
    } catch (e) { setErr(e instanceof Error ? e.message : "Save failed"); setBusy(false); }
  };
  const reinterpret = async () => {
    setBusy(true); setErr(null);
    try {
      // Re-runs interpretation on the stored image → back to the review queue.
      await api.reinterpretIcon(icon.id);
      onChanged();
    } catch (e) { setErr(e instanceof Error ? e.message : "Re-interpret failed"); setBusy(false); }
  };

  return (
    <div className="flex items-start gap-4 px-5 py-3">
      {withImage && (
        <IconThumb key={current.image_s3_key ?? "none"} iconId={current.id} size={48} />
      )}
      <div className="min-w-0 flex-1">
        {editing ? (
          <div className="space-y-2">
            <div className="grid gap-2 sm:grid-cols-[1fr_140px]">
              <input className={inputClass} value={label} onChange={(e) => setLabel(e.target.value)} />
              <select className={inputClass} value={iconType} onChange={(e) => setIconType(e.target.value)}>
                {ICON_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <textarea
              className="min-h-[56px] w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-[var(--color-brand-primary)]/50 dark:border-white/12 dark:bg-white/5 dark:text-white"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-slate-900 dark:text-slate-100">{icon.label}</span>
              <TypeBadge type={icon.icon_type} />
              {withImage && (
                <span className="rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 text-[10px] font-medium text-sky-700 dark:border-sky-500/20 dark:bg-sky-500/10 dark:text-sky-300" title="Has a reference image used for visual matching">
                  image
                </span>
              )}
              {icon.ai_generated && (
                <span className="text-[10px] text-slate-400 dark:text-slate-500" title={`AI-derived${icon.ai_model ? ` (${icon.ai_model})` : ""}${icon.approved_by ? `, approved by ${icon.approved_by}` : ""}`}>
                  AI-assisted
                </span>
              )}
              {icon.confidence && (
                <span className={`rounded-full border px-1.5 py-0.5 text-[9px] font-medium ${confidenceTone(icon.confidence)}`} title="Model confidence at interpretation time">
                  {icon.confidence}
                </span>
              )}
            </div>
            {icon.description && (
              <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{icon.description}</p>
            )}
          </>
        )}
        {err && <p className="mt-1 text-xs text-rose-600 dark:text-rose-300">{err}</p>}
        {current.image_check && (
          <ImageCheckNotice
            check={current.image_check}
            demoted={current.image_check_demoted}
          />
        )}
      </div>
      {canEdit && (
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          {editing ? (
            <div className="flex gap-2">
              <Button variant="outline" size="sm" disabled={busy} onClick={() => { setEditing(false); setLabel(icon.label); setIconType(icon.icon_type); setDescription(icon.description ?? ""); }}>Cancel</Button>
              <Button size="sm" disabled={busy} onClick={save}>{busy ? "…" : "Save"}</Button>
            </div>
          ) : (
            <div className="flex gap-2">
              <Button variant="outline" size="sm" disabled={busy} onClick={() => setEditing(true)}>Edit</Button>
              <AttachImageButton
                iconId={current.id}
                hasExisting={withImage}
                disabled={busy}
                onAttached={(e) => {
                  setAttached(e);
                  // A mismatch returns the entry to draft, so it no longer
                  // belongs in the active list — reload to move it.
                  if (e.image_check_demoted) onChanged();
                }}
                onError={setErr}
              />
              {withImage && (
                <Button variant="outline" size="sm" disabled={busy} onClick={reinterpret} title="Re-run AI interpretation on the stored image (sends it back to review)">
                  Re-interpret
                </Button>
              )}
              <Button variant="outline" size="sm" disabled={busy} onClick={del}>Delete</Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DraftRow({
  draft,
  onChanged,
  selected,
  onSelectChange,
  onVerifiabilityChange,
}: {
  draft: IconLegendEntry;
  onChanged: () => void;
  selected?: boolean;
  onSelectChange?: (id: string, next: boolean) => void;
  /** Reports whether this draft's image is viewable, so a batch-level approve
   *  can skip the ones a reviewer could not have checked. */
  onVerifiabilityChange?: (id: string, unverifiable: boolean) => void;
}) {
  const [label, setLabel] = useState(draft.label);
  const [iconType, setIconType] = useState(draft.icon_type);
  const [description, setDescription] = useState(draft.description ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [confirmReject, setConfirmReject] = useState(false);
  // An image-derived draft can only be reviewed if the reviewer can SEE the
  // image. Track the thumbnail's state so approval is blocked otherwise.
  const [imageState, setImageState] = useState<"loading" | "loaded" | "failed">("loading");
  // Local copy so an attach updates this row without a full reload, which would
  // discard the reviewer's in-progress edits to the fields below.
  const [attached, setAttached] = useState<IconLegendEntry | null>(null);
  const [checkDismissed, setCheckDismissed] = useState(false);
  const onVerifiabilityChangeRef = useRef(onVerifiabilityChange);
  useEffect(() => {
    onVerifiabilityChangeRef.current = onVerifiabilityChange;
  }, [onVerifiabilityChange]);
  // An attach in this session supersedes the row the parent last loaded.
  const current = attached ?? draft;
  const withImage = hasImage(current);
  const imageUnverifiable = withImage && imageState === "failed";
  const imageCheck = current.image_check;

  // Lift verifiability to the batch. Held in a ref-free effect keyed on the
  // derived boolean so it fires once per real change rather than every render.
  useEffect(() => {
    onVerifiabilityChangeRef.current?.(draft.id, imageUnverifiable);
  }, [draft.id, imageUnverifiable]);

  const approve = async () => {
    setBusy(true); setErr(null);
    try {
      await api.approveIcon(draft.id, { label, icon_type: iconType, description });
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Approve failed");
    } finally {
      // Reset even on success: if the caller's reload leaves this row mounted,
      // it must not stay stuck showing "…" forever.
      setBusy(false);
    }
  };
  const reject = async () => {
    setBusy(true); setErr(null);
    try {
      await api.deleteIcon(draft.id);
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Reject failed");
    } finally {
      setBusy(false);
      setConfirmReject(false);
    }
  };

  return (
    <div className="rounded-lg border border-slate-200/80 p-3 dark:border-white/10">
      <div className="flex gap-3">
        {onSelectChange && (
          <input
            type="checkbox"
            className="mt-1 h-4 w-4 shrink-0 accent-[var(--color-brand-primary)]"
            checked={!!selected}
            onChange={(e) => onSelectChange(draft.id, e.target.checked)}
            aria-label={`Select ${draft.label || "draft"}`}
          />
        )}
        {/* The image the model read — the reviewer checks the derived definition
            against THIS, so approval is a real verification, not a rubber-stamp. */}
        {withImage && (
          <IconThumb
            // Keyed on the S3 key so attaching or replacing an image remounts
            // the thumbnail; it fetches once per icon id and would otherwise
            // keep showing the previous blob.
            key={current.image_s3_key ?? "none"}
            iconId={current.id}
            size={72}
            label={current.label}
            onLoadState={setImageState}
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            {draft.confidence && (
              <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${confidenceTone(draft.confidence)}`} title="Model confidence in this interpretation">
                {draft.confidence} confidence
              </span>
            )}
            {withImage ? (
              <span className="text-[10px] text-slate-400">
                {current.source_kind === "image"
                  ? "definition derived from this image"
                  : "written definition, with an attached example image"}
              </span>
            ) : (
              <span className="text-[10px] text-slate-400">
                text draft — no example image yet
              </span>
            )}
          </div>
          <div className="grid gap-2 sm:grid-cols-[1fr_140px]">
            <input className={inputClass} value={label} onChange={(e) => setLabel(e.target.value)} />
            <select className={inputClass} value={iconType} onChange={(e) => setIconType(e.target.value)}>
              {ICON_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <textarea
            className="mt-2 min-h-[56px] w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-[var(--color-brand-primary)]/50 dark:border-white/12 dark:bg-white/5 dark:text-white"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
      </div>
      {err && <p className="mt-1 text-xs text-rose-600 dark:text-rose-300">{err}</p>}
      {imageUnverifiable && (
        <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
          This icon&apos;s image couldn&apos;t be loaded, so the AI-derived
          definition can&apos;t be checked against it. Reload to try again —
          approving unseen would defeat the review.
        </p>
      )}
      {imageCheck && !checkDismissed && (
        <ImageCheckNotice
          check={imageCheck}
          demoted={current.image_check_demoted}
          onReplace={
            <AttachImageButton
              iconId={current.id}
              hasExisting
              disabled={busy}
              onAttached={(e) => { setAttached(e); setCheckDismissed(false); }}
              onError={setErr}
            />
          }
          onDismiss={() => setCheckDismissed(true)}
        />
      )}
      <div className="mt-2 flex items-center justify-end gap-2">
        {confirmReject ? (
          <>
            <span className="mr-auto text-xs text-slate-600 dark:text-slate-300">
              Reject and delete this draft?
            </span>
            <Button variant="outline" size="sm" disabled={busy}
                    onClick={() => setConfirmReject(false)}>
              Keep
            </Button>
            <Button variant="destructive" size="sm" disabled={busy} onClick={reject}>
              {busy ? "…" : "Reject"}
            </Button>
          </>
        ) : (
          <>
            <AttachImageButton
              iconId={current.id}
              hasExisting={withImage}
              disabled={busy}
              onAttached={(e) => { setAttached(e); setCheckDismissed(false); }}
              onError={setErr}
            />
            <Button variant="outline" size="sm" disabled={busy}
                    onClick={() => setConfirmReject(true)}>
              Reject
            </Button>
            <Button
              size="sm"
              disabled={busy || imageUnverifiable}
              title={imageUnverifiable
                ? "Can't approve while the icon image is unavailable"
                : undefined}
              onClick={approve}
            >
              {busy ? "…" : "Approve"}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

// ---- dialogs ----

function TextDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [label, setLabel] = useState("");
  const [iconType, setIconType] = useState<string>("firewall");
  const [description, setDescription] = useState("");
  const [hostingScope, setHostingScope] = useState("on_prem");
  const [polished, setPolished] = useState<string | null>(null);
  const [polishing, setPolishing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const polish = async () => {
    if (!description.trim()) { setErr("Write a description first"); return; }
    setPolishing(true); setErr(null);
    try {
      const r = await api.polishIconDescription({ label, icon_type: iconType, description });
      setPolished(r.polished);
    } catch (e) { setErr(e instanceof Error ? e.message : "Polish failed"); }
    finally { setPolishing(false); }
  };
  const save = async () => {
    if (!label.trim()) { setErr("Label is required"); return; }
    setSubmitting(true); setErr(null);
    try {
      await api.createIcon({ label: label.trim(), icon_type: iconType, description: description.trim(), hosting_scope: hostingScope });
      onSaved();
    } catch (e) { setErr(e instanceof Error ? e.message : "Failed"); setSubmitting(false); }
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Add icon — text description</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <input className={inputClass} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Glyph label, e.g. orange hexagon / HSM-appliance" />
          <div className="grid grid-cols-2 gap-3">
            <select className={inputClass} value={iconType} onChange={(e) => setIconType(e.target.value)}>
              {ICON_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <select className={inputClass} value={hostingScope} onChange={(e) => setHostingScope(e.target.value)}>
              <option value="on_prem">On-prem</option>
              <option value="cloud">Cloud</option>
              <option value="both">Both</option>
            </select>
          </div>
          <textarea
            className="min-h-[70px] w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-[var(--color-brand-primary)]/50 dark:border-white/12 dark:bg-white/5 dark:text-white"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What it looks like / means, e.g. orange hexagon with a lock = perimeter firewall"
          />
          <div>
            <Button variant="outline" size="sm" onClick={polish} disabled={polishing}>
              {polishing ? "Polishing…" : "Polish with AI"}
            </Button>
          </div>
          {polished !== null && (
            <div className="rounded-md border border-[var(--color-brand-primary)]/20 bg-[var(--color-brand-primary)]/[0.04] p-3 text-sm dark:bg-[var(--color-brand-primary)]/[0.06]">
              <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">AI suggestion</p>
              <p className="text-slate-700 dark:text-slate-200">{polished}</p>
              <div className="mt-2 flex gap-2">
                <Button variant="outline" size="sm" onClick={() => { setDescription(polished); setPolished(null); }}>Use this</Button>
                <Button variant="outline" size="sm" onClick={() => setPolished(null)}>Keep mine</Button>
              </div>
            </div>
          )}
          {err && <p className="text-xs text-rose-600 dark:text-rose-300">{err}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button onClick={save} disabled={submitting}>{submitting ? "Saving…" : "Save"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ImageDialog({ onClose, onDrafted }: { onClose: () => void; onDrafted: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [interpreting, setInterpreting] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [result, setResult] = useState<IconLegendEntry | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Show the picked image immediately (local preview), then interpret. The
  // derived definition renders next to the same image so the user confirms the
  // reading in-dialog before it lands in the review queue.
  const handle = async (file: File) => {
    setErr(null); setResult(null);
    const localUrl = URL.createObjectURL(file);
    setPreview((prev) => { if (prev) URL.revokeObjectURL(prev); return localUrl; });
    setInterpreting(true);
    try {
      const draft = await api.interpretIconImage(file);
      setResult(draft);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Interpretation failed");
    } finally {
      setInterpreting(false);
    }
  };

  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Add icon — from image</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Upload an icon crop. AI reads the image and drafts a definition
            (type + description). The definition comes from the image, not a
            typed label — review it, then approve it in <strong>Pending review</strong>.
          </p>
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg"
            className="hidden"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) void handle(f); e.target.value = ""; }}
          />
          {(preview || result) && (
            <div className="flex gap-3 rounded-md border border-slate-200/80 p-3 dark:border-white/10">
              {preview && (
                <div className="flex h-20 w-20 shrink-0 items-center justify-center overflow-hidden rounded-md border border-slate-200 bg-slate-50 dark:border-white/10 dark:bg-white/5">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={preview} alt="upload preview" className="h-full w-full object-contain" />
                </div>
              )}
              <div className="min-w-0 flex-1 text-sm">
                {interpreting ? (
                  <p className="text-slate-500 dark:text-slate-400">Reading the image…</p>
                ) : result ? (
                  <>
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-slate-900 dark:text-slate-100">{result.label}</span>
                      <TypeBadge type={result.icon_type} />
                      {result.confidence && (
                        <span className={`rounded-full border px-1.5 py-0.5 text-[9px] font-medium ${confidenceTone(result.confidence)}`}>
                          {result.confidence}
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">{result.description}</p>
                    <p className="mt-1 text-[11px] text-slate-400">Added to Pending review — edit/approve it there.</p>
                  </>
                ) : null}
              </div>
            </div>
          )}
          <Button onClick={() => fileRef.current?.click()} disabled={interpreting}>
            {interpreting ? "Interpreting…" : result ? "Choose another" : "Choose image"}
          </Button>
          {err && <p className="text-xs text-rose-600 dark:text-rose-300">{err}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={interpreting}>Close</Button>
          <Button onClick={onDrafted} disabled={interpreting || !result}>Go to review</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const POLL_INTERVAL_MS = 1500;
const POLL_MAX_ATTEMPTS = 400; // ~10 min ceiling so a crashed job can't hang the UI

function BulkDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [tab, setTab] = useState<"images" | "text">("images");
  const imgRef = useRef<HTMLInputElement>(null);
  const txtRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Array<{ row: number; error: string }>>([]);
  const [err, setErr] = useState<string | null>(null);
  const abortRef = useRef(false);

  useEffect(() => () => { abortRef.current = true; }, []);

  const uploadImages = async (files: FileList) => {
    setBusy(true); setErr(null); setRowErrors([]); setStatus("Starting job…");
    abortRef.current = false;
    try {
      const { job_id, skipped } = await api.bulkIconImages(Array.from(files));
      if (skipped?.length) {
        setRowErrors(skipped.map((s, i) => ({ row: i + 1, error: `${s.file}: ${s.error}` })));
      }
      // Poll job to completion, with an attempt ceiling + abort so a crashed or
      // cross-task-missing job can't hang the dialog forever.
      let done = false;
      for (let attempt = 0; attempt < POLL_MAX_ATTEMPTS; attempt++) {
        if (abortRef.current) return;
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        if (abortRef.current) return;  // re-check after the sleep (unmounted?)
        let j;
        try {
          j = await api.iconBulkJobStatus(job_id);
        } catch {
          // Transient (or job row not yet visible) — keep the drafts we can and
          // stop polling after a few misses rather than looping forever.
          if (attempt > 5) {
            setStatus("Job status unavailable — check Pending review below for drafts.");
            done = true;
            break;
          }
          continue;
        }
        if (abortRef.current) return;  // dialog closed during the status fetch
        setStatus(`Interpreting ${j.done + j.failed}/${j.total}…`);
        if (j.status === "completed") {
          setStatus(`Done — ${j.done} drafted${j.failed ? `, ${j.failed} failed` : ""}. Review below.`);
          done = true;
          break;
        }
      }
      if (!done) setStatus("Still running — drafts will appear in Pending review as they finish.");
      onDone();
    } catch (e) { setErr(e instanceof Error ? e.message : "Bulk upload failed"); setBusy(false); }
  };

  const uploadText = async (file: File) => {
    setBusy(true); setErr(null); setRowErrors([]); setStatus("Uploading…");
    try {
      const r = await api.bulkIconText(file);
      setRowErrors(r.errors ?? []);
      setStatus(`Created ${r.created} draft(s)${r.errors.length ? `, ${r.errors.length} row error(s)` : ""}. Review below.`);
      onDone();
    } catch (e) { setErr(e instanceof Error ? e.message : "Bulk text failed"); setBusy(false); }
  };

  // image_filename is optional: name a file here and the review queue offers to
  // upload the images and match each one to the row that named it. A row without
  // it stays text-only and contributes a legend line but no visual example.
  const csvTemplate =
    "label,icon_type,description,image_filename\n" +
    "orange-hex,firewall,perimeter firewall (active/standby),orange-hex.png\n" +
    "grey-cylinder,database,primary transaction DB,grey-cylinder.png\n" +
    "blue-square,server,application server,\n";
  const jsonTemplate = JSON.stringify(
    [
      { label: "orange-hex", icon_type: "firewall", description: "perimeter firewall (active/standby)", image_filename: "orange-hex.png" },
      { label: "grey-cylinder", icon_type: "database", description: "primary transaction DB", image_filename: "grey-cylinder.png" },
      { label: "blue-square", icon_type: "server", description: "application server" },
    ],
    null,
    2,
  );
  const download = (content: string, name: string, mime: string) => {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Bulk upload icons</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="flex gap-2">
            <Button variant={tab === "images" ? "default" : "outline"} size="sm" onClick={() => setTab("images")}>Images</Button>
            <Button variant={tab === "text" ? "default" : "outline"} size="sm" onClick={() => setTab("text")}>CSV / JSON</Button>
          </div>
          {tab === "images" ? (
            <div className="space-y-2">
              <p className="text-sm text-slate-500 dark:text-slate-400">
                Select multiple icon images. Each is interpreted by AI and lands in
                Pending review for approval. (Max 50, 2MB each.)
              </p>
              <input ref={imgRef} type="file" accept="image/png,image/jpeg" multiple className="hidden"
                onChange={(e) => { if (e.target.files?.length) void uploadImages(e.target.files); e.target.value = ""; }} />
              <Button onClick={() => imgRef.current?.click()} disabled={busy}>Choose images</Button>
            </div>
          ) : (
            <div className="space-y-2">
              <p className="text-sm text-slate-500 dark:text-slate-400">
                Upload a CSV or JSON with columns <code>label, icon_type, description</code>.
                Rows land in Pending review.
              </p>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                To give a row an example image, add an optional{" "}
                <code>image_filename</code> column. Pending review then offers to
                upload those files and matches each one to the row that named it.
                You can also attach an image to any row individually.
              </p>
              <div className="flex flex-wrap gap-2">
                <input ref={txtRef} type="file" accept=".csv,.json,application/json,text/csv" className="hidden"
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) void uploadText(f); e.target.value = ""; }} />
                <Button onClick={() => txtRef.current?.click()} disabled={busy}>Choose file</Button>
                <Button variant="outline" onClick={() => download(csvTemplate, "icon-legend-template.csv", "text/csv")}>CSV template</Button>
                <Button variant="outline" onClick={() => download(jsonTemplate, "icon-legend-template.json", "application/json")}>JSON template</Button>
              </div>
            </div>
          )}
          {status && <p className="text-xs text-slate-600 dark:text-slate-300">{status}</p>}
          {rowErrors.length > 0 && (
            <div className="max-h-32 overflow-auto rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800 dark:border-amber-500/20 dark:bg-amber-500/10 dark:text-amber-200">
              <p className="mb-1 font-medium">Skipped rows/files:</p>
              <ul className="space-y-0.5">
                {rowErrors.map((e) => (
                  <li key={e.row}>Row {e.row}: {e.error}</li>
                ))}
              </ul>
            </div>
          )}
          {err && <p className="text-xs text-rose-600 dark:text-rose-300">{err}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
