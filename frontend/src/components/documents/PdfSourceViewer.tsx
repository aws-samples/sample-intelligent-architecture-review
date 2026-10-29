// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * PdfSourceViewer — a react-pdf viewer that jumps to a target page and
 * highlights a source quote in the page's text layer.
 *
 * Used by the full-screen extraction review split-pane: clicking a proposed
 * rule sets {page, quote} here, and the viewer navigates to that page and
 * paints a highlight over the matching text so the reviewer can verify the
 * rule against the document without hunting.
 *
 * Matching is best-effort (see lib/quote-match). If nothing matches we
 * still land on the page.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/TextLayer.css";
import "react-pdf/dist/Page/AnnotationLayer.css";
import { findQuoteSpans, normalizeQuoteText } from "@/lib/quote-match";

// Worker is served from /public/pdf.worker.min.mjs, auto-synced from the
// pdfjs-dist that react-pdf ACTUALLY resolves (its own nested copy) by the
// `sync-pdf-worker` prebuild/predev script — react-pdf pins a different
// pdfjs-dist minor than the top-level one, and a version mismatch throws
// "API version does not match the Worker version". Required under
// output:export where we can't rely on a CDN/bundler worker URL.
pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";

export function PdfSourceViewer({
  fileUrl,
  page,
  quote,
}: {
  fileUrl: string;
  /** 1-based target page. null = show page 1, no highlight. */
  page: number | null;
  /** Source text to highlight on that page (best-effort). */
  quote?: string | null;
}) {
  const [numPages, setNumPages] = useState(0);
  const [pageWidth, setPageWidth] = useState(680);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const pageWrapRef = useRef<HTMLDivElement>(null);

  const targetPage = page && page >= 1 ? page : 1;
  const normQuote = useMemo(() => normalizeQuoteText(quote || ""), [quote]);

  // Size the page to the container width (responsive within the split pane).
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => setPageWidth(Math.max(320, el.clientWidth - 24));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // After the text layer renders, find + highlight the quote and scroll to it.
  const highlightAndScroll = () => {
    const wrap = pageWrapRef.current;
    if (!wrap) return;
    // Clear any prior highlight.
    wrap.querySelectorAll<HTMLElement>("[data-quote-hit]").forEach((n) => {
      n.style.backgroundColor = "";
      n.style.borderRadius = "";
      n.removeAttribute("data-quote-hit");
    });
    if (!normQuote) {
      // No quote — just scroll the page into view.
      wrap.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const spans = Array.from(
      wrap.querySelectorAll<HTMLElement>(".react-pdf__Page__textContent span"),
    );
    if (spans.length === 0) return;

    const hits = findQuoteSpans(spans.map((s) => s.textContent || ""), normQuote);
    if (hits.length === 0) {
      // No text match — land on the page top.
      wrap.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    for (const i of hits) {
      spans[i].style.backgroundColor = "rgba(250, 204, 21, 0.45)"; // amber-400/45
      spans[i].style.borderRadius = "2px";
      spans[i].setAttribute("data-quote-hit", "1");
    }
    spans[hits[0]].scrollIntoView({ behavior: "smooth", block: "center" });
  };

  return (
    <div ref={containerRef} className="h-full overflow-auto bg-slate-100 dark:bg-slate-900">
      {error ? (
        <div className="p-6 text-sm text-rose-600 dark:text-rose-300">{error}</div>
      ) : (
        <Document
          file={fileUrl}
          onLoadSuccess={({ numPages: n }) => setNumPages(n)}
          onLoadError={(e) => setError(`Could not load PDF: ${e.message}`)}
          loading={<div className="p-6 text-sm text-slate-500">Loading document…</div>}
        >
          <div ref={pageWrapRef} className="flex justify-center py-3">
            <Page
              key={`${targetPage}-${normQuote.slice(0, 20)}`}
              pageNumber={Math.min(targetPage, numPages || targetPage)}
              width={pageWidth}
              renderAnnotationLayer={false}
              renderTextLayer
              onRenderTextLayerSuccess={highlightAndScroll}
              className="shadow-lg"
            />
          </div>
          {numPages > 0 && (
            <p className="pb-3 text-center text-[11px] text-slate-500 dark:text-slate-400">
              Page {Math.min(targetPage, numPages)} of {numPages}
            </p>
          )}
        </Document>
      )}
    </div>
  );
}
