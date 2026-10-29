// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import Link from "next/link";
import { PageHero } from "@/components/layout/PageHero";

const steps = [
  {
    number: 1,
    title: "Upload HA standards document",
    description:
      "Start by uploading your High Availability standards or policy document (PDF). The AI will automatically extract validation rules from it.",
    action: "Go to Standards",
    href: "/standards",
    details: [
      "Click 'Upload Doc' on the Standards page",
      "Select your HA policy PDF (e.g., reference architecture standards)",
      "The AI extracts 10–20 validation rules with severity, category, and criteria",
      "You can also manually add rules with the 'Add Standard' button",
      "Each rule defines what the AI checks when validating SDDs",
    ],
  },
  {
    number: 2,
    title: "Upload system design documents (SDDs)",
    description:
      "Upload the infrastructure design documents you want to validate against the HA standards.",
    action: "Go to Documents",
    href: "/documents",
    details: [
      "Click 'Browse Files' or drag-and-drop PDFs on the Documents page",
      "The system automatically parses text and extracts architecture diagrams",
      "Processing takes 10–30 seconds depending on document size",
      "Once status shows 'Synced', the document is ready for validation",
      "You can upload multiple documents for batch validation",
    ],
  },
  {
    number: 3,
    title: "Run validation",
    description:
      "Validate your SDDs against the configured HA standards. The AI analyzes both text content and architecture diagrams.",
    action: "Go to Documents",
    href: "/documents",
    details: [
      "Open a document detail page and click 'Run Validation'",
      "The AI checks each standard rule against the document content",
      "Architecture diagrams are analyzed via Claude Vision for visual patterns",
      "Validation takes 30–60 seconds (one AI call per standard + per diagram)",
      "Results show pass/fail per rule with evidence citations",
    ],
  },
  {
    number: 4,
    title: "Review validation results",
    description:
      "View detailed findings, compliance scores, and the connection between standards and validation evidence.",
    action: "Go to Validation",
    href: "/validation",
    details: [
      "The Validation page shows all runs with summary stats",
      "Click into a validation to see findings grouped by category",
      "Each finding shows severity, source (text or diagram), and a page reference",
      "The Compliance Matrix tab shows which standards passed or failed",
      "The Diagram Analysis tab shows what was detected in architecture diagrams",
    ],
  },
  {
    number: 5,
    title: "Ask questions (natural-language query)",
    description:
      "Use the AI chat to ask questions about your documents or get insights about HA compliance.",
    action: "Go to Query",
    href: "/query",
    details: [
      "Type natural-language questions about your uploaded documents",
      "The AI retrieves relevant sections and generates grounded answers",
      "Ask about RTO/RPO, redundancy patterns, failover mechanisms, etc.",
      "Answers include source citations from the actual documents",
      "Works across all uploaded documents simultaneously",
    ],
  },
];

const tips = [
  "Run validation multiple times on the same document (e.g., after adding new standards).",
  "Standards can be edited or deleted at any time — changes apply to future validations.",
  "The AI analyzes architecture diagrams separately from text for comprehensive coverage.",
  "Use the Query page to ask specific questions about any uploaded document.",
];

export default function GettingStartedPage() {
  return (
    <div className="max-w-4xl space-y-6 animate-in fade-in duration-300">
      <PageHero
        title="Getting started"
        subtitle="Five steps to validate your infrastructure designs against High Availability standards."
      />

      {/* How it works callout */}
      <div className="overflow-hidden rounded-2xl border border-[var(--color-brand-primary)]/20 bg-gradient-to-br from-[var(--color-brand-primary)]/[0.06] via-white to-white p-5 dark:border-[var(--color-brand-primary)]/30 dark:from-[var(--color-brand-primary)]/[0.10] dark:via-white/[0.02] dark:to-white/[0.02]">
        <div className="flex items-start gap-3.5">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-[var(--color-brand-primary)]/10 ring-1 ring-[var(--color-brand-primary)]/15">
            <svg className="h-4 w-4 text-[var(--color-brand-primary)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </span>
          <div className="space-y-1">
            <p className="text-sm font-semibold text-slate-900 dark:text-slate-50">How it works</p>
            <p className="text-sm leading-relaxed text-slate-600 dark:text-slate-300">
              This tool uses Amazon Bedrock Claude to analyze your System Design Documents against configurable
              HA standards. It checks both text content and architecture diagrams, producing detailed findings
              with evidence citations and compliance scores.
            </p>
          </div>
        </div>
      </div>

      {/* Steps timeline */}
      <ol className="relative space-y-4 border-l border-slate-200/80 pl-6 dark:border-white/10">
        {steps.map((step) => (
          <li key={step.number} className="relative">
            {/* Numbered marker on the timeline rail */}
            <span className="absolute -left-[34px] flex h-7 w-7 items-center justify-center rounded-full border border-slate-200 bg-white font-semibold text-xs text-[var(--color-brand-primary)] shadow-sm dark:border-white/10 dark:bg-slate-900">
              {step.number}
            </span>

            <div className="rounded-xl border border-slate-200/80 bg-white p-5 transition-colors hover:border-slate-300/80 dark:border-white/8 dark:bg-white/[0.02] dark:hover:border-white/12">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 space-y-1">
                  <h3 className="text-sm font-semibold tracking-normal text-slate-900 dark:text-slate-50">
                    {step.title}
                  </h3>
                  <p className="text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                    {step.description}
                  </p>
                </div>
                <Link
                  href={step.href}
                  className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:border-[var(--color-brand-primary)]/30 hover:bg-[var(--color-brand-primary)]/[0.04] hover:text-[var(--color-brand-primary)] dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-[var(--color-brand-primary)]/10"
                >
                  {step.action}
                  <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                  </svg>
                </Link>
              </div>

              <ul className="mt-3.5 space-y-1.5">
                {step.details.map((detail, i) => (
                  <li
                    key={i}
                    className="flex items-start gap-2 text-xs leading-relaxed text-slate-600 dark:text-slate-300"
                  >
                    <svg
                      className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-300"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                    {detail}
                  </li>
                ))}
              </ul>
            </div>
          </li>
        ))}
      </ol>

      {/* Tips */}
      <div className="rounded-xl border border-slate-200/80 bg-white p-5 dark:border-white/8 dark:bg-white/[0.02]">
        <div className="mb-3 flex items-center gap-2">
          <svg className="h-4 w-4 text-[var(--color-brand-primary)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
          </svg>
          <h2 className="text-sm font-semibold tracking-normal text-slate-900 dark:text-slate-50">Tips</h2>
        </div>
        <ul className="space-y-2">
          {tips.map((tip, i) => (
            <li
              key={i}
              className="flex items-start gap-2 text-xs leading-relaxed text-slate-600 dark:text-slate-300"
            >
              <span className="mt-1 inline-block h-1 w-1 shrink-0 rounded-full bg-slate-400 dark:bg-slate-500" />
              {tip}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
