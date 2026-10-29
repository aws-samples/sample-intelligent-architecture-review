// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Locate an extracted source quote in a PDF page's text layer.
 *
 * Matching is best-effort: extraction truncates source_quote to ~500 chars
 * and normalises whitespace, and the PDF text layer splits text into spans
 * that don't align to sentence boundaries. We normalise both sides and
 * return the run of consecutive spans that the (possibly truncated) quote
 * covers.
 */

/** Normalise text for fuzzy matching: lowercase, collapse whitespace, strip
 *  smart quotes/punctuation noise that differs between extraction + PDF.
 *  Extraction renders a table row as "cell | cell | cell"; the text layer has
 *  no pipes, only the cells, so pipes are treated as whitespace. */
export function normalizeQuoteText(s: string): string {
  return (s || "")
    .toLowerCase()
    .replace(/[“”"’'`]/g, "")
    .replace(/\|/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Indices of the spans (by position in ``spanTexts``) the quote covers, or
 *  an empty list when it can't be found. Matches on a prefix of the quote,
 *  since the stored quote may be truncated and the tail may differ. */
export function findQuoteSpans(spanTexts: string[], quote: string): number[] {
  const needle = normalizeQuoteText(quote).slice(0, 140);
  if (!needle) return [];
  let concat = "";
  const offsets: { index: number; start: number; end: number }[] = [];
  spanTexts.forEach((text, index) => {
    const t = normalizeQuoteText(text);
    if (!t) return;
    const start = concat.length;
    concat += (concat ? " " : "") + t;
    offsets.push({ index, start: start ? start + 1 : 0, end: concat.length });
  });
  let hitStart = concat.indexOf(needle);
  // Fall back to progressively shorter prefixes if the full needle misses.
  if (hitStart === -1) {
    for (const len of [90, 60, 40]) {
      hitStart = concat.indexOf(needle.slice(0, len));
      if (hitStart !== -1) break;
    }
  }
  if (hitStart === -1) return [];
  const hitEnd = hitStart + needle.length;
  return offsets.filter((o) => o.end > hitStart && o.start < hitEnd).map((o) => o.index);
}
