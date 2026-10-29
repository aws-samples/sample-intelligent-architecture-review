// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from "vitest";

import { findQuoteSpans, normalizeQuoteText } from "@/lib/quote-match";

// Text-layer spans as pdf.js emits them for a table: one span per cell line,
// with whitespace-only spans between cells.
const TABLE_PAGE = [
  "2 Service tiers", "", "Tier", " ", "Description", " ", "Maximum outage",
  "Tier 1", " ", "Customer-facing payments and core ledger", " ", "15 minutes", " ", "None (RPO 0)",
  "Tier 2", " ", "Customer-facing, non-payment services", " ", "4 hours", " ", "15 minutes",
  "Tier 3", " ", "Internal and back-office services", " ", "1 business day", " ", "24 hours",
];

describe("normalizeQuoteText", () => {
  it("treats table-cell pipes as whitespace", () => {
    expect(normalizeQuoteText("Tier 2 | Customer-facing |  4 hours")).toBe("tier 2 customer-facing 4 hours");
  });

  it("drops smart quotes and case", () => {
    expect(normalizeQuoteText("“Tier 1 SHALL”")).toBe("tier 1 shall");
  });
});

describe("findQuoteSpans", () => {
  it("highlights a table row quoted with pipe separators", () => {
    const hits = findQuoteSpans(TABLE_PAGE, "Tier 2 | Customer-facing, non-payment services | 4 hours | 15 minutes");
    expect(hits.map((i) => TABLE_PAGE[i])).toEqual([
      "Tier 2", "Customer-facing, non-payment services", "4 hours", "15 minutes",
    ]);
  });

  it("matches a quote that wraps across lines of one cell", () => {
    const spans = ["Every Tier 1 and Tier 2 system SHALL run", "from two owned datacenter sites at least", "30 km apart."];
    expect(findQuoteSpans(spans, "Every Tier 1 and Tier 2 system SHALL run from two owned datacenter sites")).toEqual([0, 1]);
  });

  it("returns nothing when the quote is not on the page", () => {
    expect(findQuoteSpans(TABLE_PAGE, "Tier 4 systems are retired")).toEqual([]);
    expect(findQuoteSpans(TABLE_PAGE, "")).toEqual([]);
  });
});
