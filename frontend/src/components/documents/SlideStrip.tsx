// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * SlideStrip — replaces the Parsed Sections panel for PPTX-source
 * documents. The per-slide extraction loop already produced
 * one SlideStatus per slide (slide_type, rule_count, skip reason,
 * image_s3_key); this component renders the matching slide PNGs as
 * a vertical scroll list with that status alongside.
 *
 * Why not Parsed Sections for PPTX:
 *   - Parsed Sections runs PyMuPDF on the LibreOffice-converted PDF
 *     and produces letter-spaced gibberish ("Dat acent er") because
 *     LibreOffice subsets glyphs.
 *   - The LLM never sees that text — it gets the slide PNG +
 *     python-pptx native text — so the panel mis-represents what
 *     the agent actually consumed.
 *   - Slide-by-slide rule counts give a far more useful sanity
 *     check than a section outline that doesn't exist for decks.
 */

import { useEffect, useState } from "react";

import { api, SlideStatus, SlideType } from "@/lib/api";

const SLIDE_TYPE_LABEL: Record<SlideType, string> = {
  title: "Title",
  agenda: "Agenda",
  content: "Content",
  diagram: "Diagram",
  table: "Table",
  appendix: "Appendix",
};

const SLIDE_TYPE_BADGE: Record<SlideType, string> = {
  title: "bg-violet-50 text-violet-700 dark:bg-violet-500/10 dark:text-violet-300",
  agenda: "bg-slate-100 text-slate-700 dark:bg-white/8 dark:text-slate-300",
  content: "bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-300",
  diagram: "bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300",
  table: "bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300",
  appendix: "bg-slate-100 text-slate-500 dark:bg-white/8 dark:text-slate-400",
};

export function SlideStrip({
  documentId,
  slides,
}: {
  documentId: string;
  slides: SlideStatus[];
}) {
  // Lazy-load thumbnails — fetching all 9 PNGs at once would slow
  // the doc-detail page TTFB. Each thumbnail fetches independently
  // when its row mounts; we revoke blob URLs on unmount to avoid
  // leaking memory.
  return (
    <div className="-mx-2 max-h-[650px] space-y-3 overflow-y-auto px-2 pr-1">
      {slides.map((slide) => (
        <SlideRow key={slide.slide_index} documentId={documentId} slide={slide} />
      ))}
    </div>
  );
}

function SlideRow({
  documentId,
  slide,
}: {
  documentId: string;
  slide: SlideStatus;
}) {
  const [thumbUrl, setThumbUrl] = useState<string>("");

  useEffect(() => {
    let revoked = false;
    let url = "";
    api
      .getDocumentSlideUrl(documentId, slide.slide_index)
      .then((u) => {
        if (revoked || !u) return;
        url = u;
        setThumbUrl(u);
      })
      .catch(() => {});
    return () => {
      revoked = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [documentId, slide.slide_index]);

  const badgeCls = SLIDE_TYPE_BADGE[slide.slide_type] || SLIDE_TYPE_BADGE.content;
  const badgeLabel = SLIDE_TYPE_LABEL[slide.slide_type] || slide.slide_type;

  return (
    <div className="rounded-lg border border-slate-200/70 bg-white p-2.5 transition-colors hover:border-slate-300/80 dark:border-white/8 dark:bg-white/[0.02] dark:hover:border-white/12">
      <div className="flex gap-3">
        {/* Thumbnail */}
        <div className="relative aspect-video w-32 flex-shrink-0 overflow-hidden rounded border border-slate-200/60 bg-slate-50 dark:border-white/10 dark:bg-white/5">
          {thumbUrl ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={thumbUrl}
              alt={`Slide ${slide.slide_index}`}
              className="h-full w-full object-contain"
            />
          ) : (
            <div className="flex h-full items-center justify-center text-[10px] text-slate-400">
              loading…
            </div>
          )}
          <span className="absolute left-1 top-1 rounded bg-black/60 px-1.5 py-0.5 text-[9px] font-mono text-white">
            {slide.slide_index}
          </span>
        </div>

        {/* Status */}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={
                "rounded-full px-2 py-0.5 text-[10px] font-medium " + badgeCls
              }
            >
              {badgeLabel}
            </span>
            {slide.rule_count > 0 ? (
              <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300">
                {slide.rule_count} rule{slide.rule_count === 1 ? "" : "s"}
              </span>
            ) : slide.status === "failed" ? (
              /* Distinct from "no rules": this slide errored, so a blank result
                 is a failure to report rather than a slide with nothing in it. */
              <span className="rounded-full bg-rose-50 px-2 py-0.5 text-[10px] font-medium text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">
                failed
              </span>
            ) : (
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500 dark:bg-white/8 dark:text-slate-400">
                no rules
              </span>
            )}
          </div>
          {slide.skip_reason && (
            <p className="mt-1.5 text-[11px] italic text-slate-500 dark:text-slate-400">
              skipped: {slide.skip_reason}
            </p>
          )}
          {slide.error && (
            <p className="mt-1.5 text-[11px] text-rose-600 dark:text-rose-300">
              {slide.error}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
