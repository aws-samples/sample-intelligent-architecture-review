// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Per-finding explainer chat.
 *
 * Bottom-right corner panel scoped to a single finding. Sends an
 * opening user message that summarises the rule + verdict so the
 * model has context, then lets the user ask follow-ups. The backend
 * endpoint is read-only — it explains, never re-runs the validation.
 */

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "@/lib/api";
import type { Finding } from "@/lib/mock-data";

interface Props {
  finding: Finding | null;
  validationId: string;
  onClose: () => void;
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export function FindingChatPanel({ finding, validationId, onClose }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Seed with a system-style first turn so the user sees the context
  // they're talking about. The backend prepends its own structured
  // system prompt — this is purely UX scaffolding.
  useEffect(() => {
    if (!finding) {
      const resetTimer = window.setTimeout(() => {
        setMessages([]);
        setInput("");
        setError(null);
      }, 0);
      return () => window.clearTimeout(resetTimer);
    }
    const status = String(finding.status).replace(/_/g, " ");
    const opener = `I'm asking about **${finding.requirementId}** — ${finding.title}. The validator returned **${status.toUpperCase()}**.`;
    const seedTimer = window.setTimeout(() => {
      setMessages([
        {
          role: "assistant",
          content: `Hi — I have the validator's verbalized reasoning at decision time for **${finding.requirementId}** (when reasoning was captured), plus the verdict, evidence, and what the model saw. I can quote the reasoning and explain how it led to the verdict. I can't change the verdict — to challenge it, re-run validation.`,
        },
        { role: "user", content: opener },
      ]);
      setInput("");
      setError(null);
    }, 0);
    const sendTimer = window.setTimeout(() => {
      void send([
        {
          role: "user",
          content: opener,
        },
      ]);
    }, 50);
    return () => {
      window.clearTimeout(seedTimer);
      window.clearTimeout(sendTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finding]);

  // Auto-scroll on new messages.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, sending]);

  async function send(thread: ChatMessage[]) {
    if (!finding) return;
    setSending(true);
    setError(null);

    // Append an empty assistant message we fill in as deltas stream in.
    let assistantIndex = -1;
    setMessages((prev) => {
      assistantIndex = prev.length;
      return [...prev, { role: "assistant", content: "" }];
    });
    const appendToAssistant = (text: string) =>
      setMessages((prev) => {
        const next = [...prev];
        if (assistantIndex >= 0 && next[assistantIndex]) {
          next[assistantIndex] = {
            ...next[assistantIndex],
            content: next[assistantIndex].content + text,
          };
        }
        return next;
      });

    try {
      await api.chatAboutFindingStream(
        validationId,
        finding.requirementId,
        thread,
        {
          onDelta: (t) => appendToAssistant(t),
          // Verdict-flip disclaimer arrives as a trailing frame; render it
          // on its own line so it reads as a distinct caution.
          onWarning: (t) => appendToAssistant(`\n\n${t}`),
        },
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Chat failed");
      // Drop the empty placeholder if nothing streamed in.
      setMessages((prev) =>
        prev.filter((m, i) => !(i === assistantIndex && m.content === "")),
      );
    } finally {
      setSending(false);
    }
  }

  async function handleSubmit() {
    if (!input.trim() || sending || !finding) return;
    const userMsg: ChatMessage = { role: "user", content: input.trim() };
    const next = [...messages, userMsg];
    setMessages(next);
    setInput("");
    await send(next);
  }

  if (!finding) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex h-[560px] w-[420px] flex-col rounded-xl border border-slate-200 bg-white shadow-2xl dark:border-white/10 dark:bg-slate-900">
      <div className="flex items-start justify-between border-b border-slate-200 px-3 py-2.5 dark:border-white/10">
        <div className="min-w-0">
          <p className="text-[10px] font-mono uppercase tracking-normal text-muted-foreground">
            Ask about this finding
          </p>
          <p className="text-xs font-semibold text-slate-900 dark:text-slate-100 truncate">
            {finding.requirementId}
          </p>
        </div>
        <button
          onClick={onClose}
          aria-label="Close chat"
          className="rounded-md p-1 text-muted-foreground hover:bg-muted/50"
        >
          <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 6l12 12M6 18L18 6" />
          </svg>
        </button>
      </div>

      <div ref={scrollRef} className="flex-1 min-h-0 space-y-2.5 overflow-y-auto px-3 py-3">
        {messages.map((m, i) => (
          <div
            key={i}
            className={
              "rounded-lg px-3 py-2 text-xs " +
              (m.role === "user"
                ? "ml-6 bg-[var(--color-brand-primary)]/10 text-slate-900 dark:text-slate-100"
                : "mr-6 bg-slate-100 text-slate-800 dark:bg-white/5 dark:text-slate-200")
            }
          >
            <div className="prose prose-xs max-w-none prose-p:my-1 prose-ul:my-1 prose-ol:my-1 prose-li:my-0 dark:prose-invert">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
            </div>
          </div>
        ))}
        {sending && (
          <div className="mr-6 inline-flex items-center gap-2 rounded-lg bg-slate-100 px-3 py-2 text-xs text-slate-500 dark:bg-white/5 dark:text-slate-400">
            <svg className="h-3 w-3 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            Thinking…
          </div>
        )}
        {error && (
          <div className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-700 dark:text-rose-300">
            {error}
          </div>
        )}
      </div>

      <div className="border-t border-slate-200 p-2.5 dark:border-white/10">
        <div className="flex items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                void handleSubmit();
              }
            }}
            placeholder="Ask why it failed, what the model saw, etc."
            rows={2}
            disabled={sending}
            className="flex-1 resize-none rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/30 disabled:opacity-60 dark:border-white/10 dark:bg-white/5"
          />
          <button
            onClick={() => void handleSubmit()}
            disabled={sending || !input.trim()}
            className="rounded-md bg-[var(--color-brand-primary)] px-3 py-1.5 text-xs font-medium text-white hover:bg-[var(--color-brand-primary-hover)] disabled:opacity-50"
          >
            Send
          </button>
        </div>
        <p className="mt-1 text-[10px] text-muted-foreground">
          ⌘↵ to send · explanations only — verdicts can&apos;t be changed here
        </p>
      </div>
    </div>
  );
}
