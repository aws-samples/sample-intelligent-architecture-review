// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Validator trace drawer.
 *
 * Renders a finding's trace (the prompt, the document chunk shown to
 * the model, and the raw model response) in a live-log style panel
 * — same monospace + colored tags as the rules-extraction log so the
 * UX feels consistent across "show your work" surfaces.
 *
 * Read-only. The drawer does not re-run the validation; it just lets
 * the reviewer audit how the verdict was reached.
 */

import { useEffect } from "react";
import type { Finding } from "@/lib/mock-data";

interface Props {
  finding: Finding | null;
  onClose: () => void;
}

export function FindingTraceDrawer({ finding, onClose }: Props) {
  // Esc to close — matches ValidationConfigDialog's UX.
  useEffect(() => {
    if (!finding) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [finding, onClose]);

  if (!finding) return null;
  const trace = finding.trace;
  if (!trace) return null;

  return (
    <div className="fixed inset-0 z-50 flex">
      <button
        aria-label="Close"
        className="flex-1 bg-black/40"
        onClick={onClose}
      />
      <aside className="flex h-full w-full max-w-3xl flex-col border-l border-border bg-background shadow-xl animate-in slide-in-from-right duration-200">
        {/* Header */}
        <div className="flex items-start justify-between border-b border-border px-5 py-3.5 shrink-0">
          <div className="min-w-0">
            <p className="text-[10px] font-mono uppercase tracking-normal text-muted-foreground">
              Validator trace
            </p>
            <h3 className="text-sm font-semibold text-[var(--color-ink)] truncate">
              {finding.requirementId} — {finding.title}
            </h3>
            <p className="mt-1 text-[11px] text-muted-foreground">
              <span className="font-mono">{trace.input_tokens.toLocaleString()}</span> in ·{" "}
              <span className="font-mono">{trace.output_tokens.toLocaleString()}</span> out ·{" "}
              <span className="font-mono">{trace.latency_ms}ms</span> · status{" "}
              <span className="font-mono">{finding.status}</span>
              {trace.model_id && (
                <>
                  {" · "}
                  <span className="font-mono">{trace.model_id.split("/").pop()?.split(":")[0]}</span>
                </>
              )}
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close trace"
            className="rounded-md p-1 text-muted-foreground hover:bg-muted/50"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 6l12 12M6 18L18 6" />
            </svg>
          </button>
        </div>

        {/* REASONING leads. The model's verbalized chain
            of thought captured at validation time. Distinct from the
            JSON answer (RESPONSE) and from what the validator saw
            (CHUNK + PROMPT). When reasoning is absent we explain why
            instead of leaving an empty section. */}
        <div className="flex-1 min-h-0 overflow-y-auto bg-slate-950/95 px-5 py-4 font-mono text-[11px] leading-relaxed text-slate-100 dark:bg-black">
          {trace.reasoning ? (
            <>
              <Section
                tag="REASONING"
                tone="text-fuchsia-300"
                body={trace.reasoning}
              />
              <p className="mb-5 -mt-3 text-[10px] italic text-fuchsia-300/60">
                Verbalized reasoning, captured at validation time. Generally
                aligned with the verdict; not a guarantee of internal decision
                logic.
              </p>
            </>
          ) : (
            <div className="mb-5 rounded border border-fuchsia-500/20 bg-fuchsia-500/[0.04] p-3 text-[11px] text-fuchsia-200/80">
              <div className="mb-1 text-[10px] font-bold uppercase tracking-normal text-fuchsia-300">
                ─── REASONING NOT CAPTURED ───
              </div>
              Reasoning capture was not active for this validation. This may be
              because the validation ran before reasoning capture was available,
              the thinking flag was off at runtime, or the model returned no
              reasoning content.
              Re-run validation to capture the model&apos;s chain of thought.
            </div>
          )}
          <Section tag="RESPONSE" tone="text-emerald-300" body={trace.raw_response} />
          <Section tag="CHUNK" tone="text-sky-300" body={trace.chunk_excerpt} />
          <Section tag="PROMPT" tone="text-amber-300" body={trace.prompt} />
        </div>
      </aside>
    </div>
  );
}

function Section({ tag, tone, body }: { tag: string; tone: string; body: string }) {
  if (!body) return null;
  return (
    <div className="mb-5">
      <div className={`mb-1 text-[10px] font-bold uppercase tracking-normal ${tone}`}>
        ─── {tag} ───
      </div>
      <pre className="whitespace-pre-wrap break-words text-slate-200/95">
        {body}
      </pre>
    </div>
  );
}
