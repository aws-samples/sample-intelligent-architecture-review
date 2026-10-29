// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect, useRef, useState } from "react";
import {
  ArrowUpRight,
  ChevronDown,
  LoaderCircle,
  MessageSquareText,
  Search,
  Send,
  Sparkles,
} from "lucide-react";
import { ChatMessage } from "@/components/query/ChatMessage";
import { PageHero } from "@/components/layout/PageHero";
import { suggestedQueries } from "@/lib/mock-data";
import type { ChatMessage as ChatMessageType } from "@/lib/mock-data";
import { api } from "@/lib/api";

export default function QueryPage() {
  const [messages, setMessages] = useState<ChatMessageType[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  // Live streaming state for the in-flight turn: the answer accumulates in
  // `streamAnswer`, extended-thinking in `streamThinking`, and each tool
  // invocation is appended to `streamTools` so the user sees the agent
  // search / calculate as it works.
  const [streamAnswer, setStreamAnswer] = useState("");
  const [streamThinking, setStreamThinking] = useState("");
  const [streamTools, setStreamTools] = useState<string[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, isLoading, streamAnswer, streamThinking, streamTools]);

  // Abort any in-flight stream on unmount so we don't leak a reader.
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  async function handleSend() {
    if (!input.trim() || isLoading) return;

    const question = input.trim();
    setInput("");

    setMessages((current) => [
      ...current,
      {
        id: `msg-${Date.now()}`,
        role: "user",
        content: question,
        timestamp: new Date().toISOString(),
      },
    ]);
    setIsLoading(true);
    setStreamAnswer("");
    setStreamThinking("");
    setStreamTools([]);

    let answerAccum = "";
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await api.streamQuery(
        question,
        undefined,
        {
          onThinking: (t) => setStreamThinking((prev) => prev + t),
          onTool: (t) => setStreamTools((prev) => [...prev, t]),
          onDelta: (t) => {
            answerAccum += t;
            setStreamAnswer((prev) => prev + t);
          },
        },
        controller.signal,
      );

      setMessages((current) => [
        ...current,
        {
          id: `msg-${Date.now() + 1}`,
          role: "assistant",
          content: answerAccum.trim() || "(no answer)",
          timestamp: new Date().toISOString(),
        },
      ]);
    } catch (queryError) {
      if ((queryError as Error)?.name === "AbortError") {
        // Navigated away mid-stream; drop silently.
      } else {
        setMessages((current) => [
          ...current,
          {
            id: `msg-${Date.now() + 1}`,
            role: "assistant",
            content: `The query could not be completed: ${
              queryError instanceof Error ? queryError.message : "Unknown error"
            }. Please try again.`,
            timestamp: new Date().toISOString(),
          },
        ]);
      }
    } finally {
      abortRef.current = null;
      setIsLoading(false);
      setStreamAnswer("");
      setStreamThinking("");
      setStreamTools([]);
    }
  }

  const isEmpty = messages.length === 0;

  return (
    <div className="animate-in space-y-4 fade-in duration-300">
      <PageHero
        title="Ask your documents"
        subtitle="Explore design evidence, validation findings, and resilience posture in one conversation."
      />

      <div className="flex h-[calc(100dvh-13.5rem)] min-h-[520px] flex-col overflow-hidden rounded-lg border border-slate-200/80 bg-white shadow-sm shadow-slate-900/[0.025] dark:border-white/8 dark:bg-white/[0.025] dark:shadow-black/20">
        <div
          className="flex-1 overflow-y-auto px-4 py-5 sm:px-6 sm:py-6"
          aria-live="polite"
        >
          {isEmpty ? (
            <div className="mx-auto flex h-full max-w-3xl flex-col items-center justify-center py-8 text-center">
              <span className="flex h-12 w-12 items-center justify-center rounded-lg border border-teal-200 bg-teal-50 text-[var(--color-brand-primary)] dark:border-teal-300/20 dark:bg-teal-300/10 dark:text-teal-300">
                <MessageSquareText className="h-5 w-5" strokeWidth={1.8} />
              </span>
              <h2 className="mt-5 text-lg font-semibold text-slate-950 dark:text-slate-50">
                What would you like to know?
              </h2>
              <p className="mt-1.5 max-w-md text-sm leading-relaxed text-slate-500 dark:text-slate-400">
                Answers are grounded in indexed documents and validation evidence.
              </p>

              <div className="mt-7 grid w-full gap-2 sm:grid-cols-2">
                {suggestedQueries.slice(0, 6).map((query) => (
                  <button
                    key={query}
                    type="button"
                    onClick={() => setInput(query)}
                    className="group flex min-h-12 items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-left text-xs leading-relaxed text-slate-600 transition-all hover:border-teal-200 hover:bg-teal-50/60 hover:text-slate-900 dark:border-white/10 dark:bg-white/[0.02] dark:text-slate-300 dark:hover:border-teal-300/20 dark:hover:bg-teal-300/5 dark:hover:text-white"
                  >
                    <span>{query}</span>
                    <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-slate-300 transition-colors group-hover:text-[var(--color-brand-primary)] dark:text-slate-600" />
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="mx-auto max-w-4xl space-y-5">
              {messages.map((message) => (
                <ChatMessage key={message.id} message={message} />
              ))}

              {/* Live reasoning trace + tool steps while the agent works. */}
              {isLoading && (streamThinking || streamTools.length > 0) && (
                <AgentTrace thinking={streamThinking} tools={streamTools} />
              )}

              {/* Live answer, streamed token-by-token into a real bubble. */}
              {isLoading && streamAnswer && (
                <ChatMessage
                  message={{
                    id: "stream-live",
                    role: "assistant",
                    content: streamAnswer,
                    timestamp: new Date().toISOString(),
                  }}
                />
              )}

              {/* Spinner only until the first signal arrives. */}
              {isLoading && !streamAnswer && !streamThinking && streamTools.length === 0 && (
                <div className="flex items-center gap-3">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-teal-50 text-[var(--color-brand-primary)] dark:bg-teal-300/10 dark:text-teal-300">
                    <LoaderCircle className="h-4 w-4 animate-spin" />
                  </span>
                  <span className="text-sm text-slate-500 dark:text-slate-400">
                    Reviewing evidence
                  </span>
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            handleSend();
          }}
          className="border-t border-slate-200/70 bg-slate-50/60 p-3 dark:border-white/8 dark:bg-black/10 sm:p-4"
        >
          <div className="mx-auto flex max-w-4xl items-center gap-2 rounded-lg border border-slate-200 bg-white p-1.5 shadow-sm transition-all focus-within:border-[var(--color-brand-primary)]/45 focus-within:ring-3 focus-within:ring-[var(--color-brand-primary)]/10 dark:border-white/12 dark:bg-white/5 dark:focus-within:border-teal-300/35">
            <input
              type="text"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="Ask about risks, standards, recovery targets, or evidence"
              className="h-9 min-w-0 flex-1 bg-transparent px-2 text-sm text-slate-900 outline-none placeholder:text-slate-400 dark:text-slate-100 dark:placeholder:text-slate-500"
              disabled={isLoading}
              aria-label="Question"
            />
            <button
              type="submit"
              disabled={!input.trim() || isLoading}
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[var(--color-brand-primary)] text-white transition-all hover:bg-[var(--color-brand-primary-hover)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/30 active:translate-y-px disabled:pointer-events-none disabled:opacity-40"
              aria-label="Send question"
              title="Send question"
            >
              {isLoading ? (
                <LoaderCircle className="h-4 w-4 animate-spin" />
              ) : (
                <Send className="h-4 w-4" />
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

/**
 * Live agent work trace: a collapsible reasoning panel plus the running
 * list of tool calls (kb_search, calculate, …) so the user can see the
 * agent gather evidence in real time rather than staring at a spinner.
 */
function AgentTrace({ thinking, tools }: { thinking: string; tools: string[] }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="flex gap-3">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-teal-50 text-[var(--color-brand-primary)] dark:bg-teal-300/10 dark:text-teal-300">
        <Sparkles className="h-4 w-4 animate-pulse" />
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        {tools.length > 0 && (
          <div className="space-y-1">
            {tools.map((t, i) => (
              <div
                key={`${t}-${i}`}
                className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400"
              >
                <Search className="h-3 w-3 shrink-0 text-[var(--color-brand-primary)]" />
                <span className="truncate font-mono">{t}</span>
              </div>
            ))}
          </div>
        )}
        {thinking && (
          <div className="rounded-lg border border-slate-200/70 bg-slate-50/60 dark:border-white/8 dark:bg-white/[0.02]">
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left"
            >
              <span className="text-[11px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
                Reasoning
              </span>
              <ChevronDown
                className={
                  "ml-auto h-3 w-3 text-slate-400 transition-transform " +
                  (open ? "rotate-180" : "")
                }
              />
            </button>
            {open && (
              <div className="max-h-36 overflow-y-auto border-t border-slate-200/60 px-3 py-2 dark:border-white/8">
                <p className="whitespace-pre-wrap text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
                  {thinking}
                </p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
