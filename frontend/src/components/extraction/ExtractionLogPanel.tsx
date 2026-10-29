// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Live extraction log panel.
 *
 * Polls /api/standards/extraction-status?document_id=…&since_seq=N
 * every 1.5s and appends new entries. Stops polling once the job
 * completes or fails. Auto-scrolls to bottom unless the user has
 * scrolled up (so they can read history without being yanked back).
 *
 * Filtered per spec: only tool calls + rule writes + reasoning
 * summaries — not every Strands streaming event. Filtering happens
 * server-side in the LiveLogCallbackHandler.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

type StatusResponse = Awaited<ReturnType<typeof api.getExtractionStatus>>;
type LogEntry = NonNullable<StatusResponse["log"]>[number];
type JobSnapshot = NonNullable<StatusResponse["job"]>;

const POLL_MS = 1500;

const LEVEL_STYLE: Record<string, { tag: string; cls: string }> = {
  tool: {
    tag: "TOOL",
    cls: "text-sky-700 dark:text-sky-300",
  },
  rule: {
    tag: "RULE",
    cls: "text-emerald-700 dark:text-emerald-300",
  },
  thought: {
    tag: "THINK",
    cls: "text-slate-500 dark:text-slate-400",
  },
  info: {
    tag: "INFO",
    cls: "text-slate-700 dark:text-slate-200",
  },
  error: {
    tag: "ERROR",
    cls: "text-rose-700 dark:text-rose-300",
  },
};

export function ExtractionLogPanel({
  documentId,
  fullHeight = false,
}: {
  documentId: string;
  /** When true, the panel fills its parent's height and the log body flexes
   *  to fill remaining space instead of the fixed h-72. Used by the
   *  standalone /documents/detail/log view. */
  fullHeight?: boolean;
}) {
  const [job, setJob] = useState<JobSnapshot | null>(null);
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [stickToBottom, setStickToBottom] = useState(true);
  const sinceSeqRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Poll loop — auto-stops once the job leaves an in-flight state.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    async function tick() {
      try {
        const res = await api.getExtractionStatus({
          documentId,
          sinceSeq: sinceSeqRef.current,
        });
        if (cancelled) return;
        if (res.job) setJob(res.job);
        if (res.log && res.log.length > 0) {
          const newLog = res.log;
          setEntries((prev) => [...prev, ...newLog]);
          sinceSeqRef.current = newLog[newLog.length - 1].seq;
        }
        const status = res.job?.status;
        // Terminal — stop. This must NOT be a bare `return`: re-arming in a
        // `finally` would run on the way out and keep the poll alive forever.
        if (status === "completed" || status === "failed") return;
      } catch {
        // swallow — a transient failure shouldn't end the log stream
      }
      if (!cancelled) timer = setTimeout(tick, POLL_MS);
    }
    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [documentId]);

  // Auto-scroll when new entries arrive — but only if the user is
  // already pinned to the bottom.
  useEffect(() => {
    if (!stickToBottom) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries, stickToBottom]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    setStickToBottom(nearBottom);
  }, []);

  // Download the log as a plain-text file for debugging. Each line is
  // "HH:MM:SS  LEVEL  message" — the same shape shown in the panel.
  const downloadLog = useCallback(() => {
    const header = [
      `Extraction log — document ${documentId}`,
      job ? `status: ${job.status}  rules_saved: ${job.rules_saved}` : "",
      job?.started_at ? `started: ${job.started_at}` : "",
      "",
    ]
      .filter(Boolean)
      .join("\n");
    const body = entries
      .map((e) => {
        const t = new Date(e.ts * 1000).toISOString();
        return `${t}  ${e.level.toUpperCase().padEnd(6)}  ${e.message}`;
      })
      .join("\n");
    const blob = new Blob([header + "\n" + body + "\n"], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = window.document.createElement("a");
    a.href = url;
    a.download = `extraction-log-${documentId}.txt`;
    window.document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }, [documentId, entries, job]);

  if (!job && entries.length === 0) {
    // No job for this doc yet — render nothing rather than a blank box.
    return null;
  }

  return (
    <div
      className={
        "rounded-xl border border-slate-200/80 bg-slate-950 text-slate-100 dark:border-white/10" +
        (fullHeight ? " flex h-full flex-col" : "")
      }
    >
      <div className="flex items-center justify-between border-b border-white/10 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-slate-400">
            Extraction log
          </span>
          {job && (
            <span
              className={
                "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium " +
                (job.status === "completed"
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                  : job.status === "failed"
                  ? "border-rose-500/30 bg-rose-500/10 text-rose-300"
                  : "border-sky-500/30 bg-sky-500/10 text-sky-300")
              }
            >
              {(job.status === "extracting" || job.status === "starting") && (
                <svg className="h-2.5 w-2.5 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              )}
              {job.status}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-[10px] text-slate-400">
          {job && job.rules_saved > 0 && (
            <span>
              <span className="font-mono tabular-nums text-slate-200">
                {job.rules_saved}
              </span>{" "}
              rules
            </span>
          )}
          <span className="font-mono">{entries.length} log lines</span>
          {entries.length > 0 && (
            <>
              {/* Open in a new tab — standalone log view for debugging. */}
              <a
                href={`/documents/detail/log/?id=${encodeURIComponent(documentId)}`}
                target="_blank"
                rel="noopener noreferrer"
                title="Open log in a new tab"
                className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-slate-300 transition-colors hover:bg-white/10 hover:text-white"
              >
                <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                </svg>
                Open
              </a>
              {/* Download the log for debugging. */}
              <button
                type="button"
                onClick={downloadLog}
                title="Download log as a text file"
                className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-slate-300 transition-colors hover:bg-white/10 hover:text-white"
              >
                <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                </svg>
                Download
              </button>
            </>
          )}
        </div>
      </div>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className={
          "overflow-y-auto px-4 py-2 font-mono text-[11px] leading-relaxed " +
          (fullHeight ? "min-h-0 flex-1" : "h-72")
        }
      >
        {entries.length === 0 ? (
          <p className="text-slate-500">
            {job?.status === "completed"
              ? `Extraction complete — ${job.rules_saved} rule${job.rules_saved === 1 ? "" : "s"} extracted. No detailed log available for this run.`
              : job?.status === "failed"
              ? `Extraction failed${job.error ? `: ${job.error}` : "."}`
              : "Waiting for the agent to start…"}
          </p>
        ) : (
          entries.map((e) => {
            const style = LEVEL_STYLE[e.level] ?? LEVEL_STYLE.info;
            return (
              <div key={e.seq} className="flex items-start gap-2">
                <span className="w-12 shrink-0 text-slate-500">
                  {new Date(e.ts * 1000).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                  })}
                </span>
                <span className={"w-12 shrink-0 font-semibold " + style.cls}>
                  {style.tag}
                </span>
                <span className="break-words text-slate-300">{e.message}</span>
              </div>
            );
          })
        )}
      </div>
      {!stickToBottom && (
        <button
          type="button"
          onClick={() => setStickToBottom(true)}
          className="w-full border-t border-white/10 bg-white/5 py-1.5 text-[10px] font-medium text-slate-300 transition-colors hover:bg-white/10"
        >
          Jump to live ↓
        </button>
      )}
    </div>
  );
}
