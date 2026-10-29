// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { Bot, FileText, UserRound } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage as ChatMessageType } from "@/lib/mock-data";

export function ChatMessage({ message }: { message: ChatMessageType }) {
  const isUser = message.role === "user";

  return (
    <div className={`flex gap-3 ${isUser ? "justify-end" : "justify-start"}`}>
      {!isUser && (
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-teal-50 text-[var(--color-brand-primary)] dark:bg-teal-300/10 dark:text-teal-300">
          <Bot className="h-4 w-4" />
        </span>
      )}

      <div
        className={
          "max-w-[calc(100%-2.75rem)] rounded-lg px-4 py-3 sm:max-w-[82%] " +
          (isUser
            ? "bg-[var(--color-brand-primary)] text-white"
            : "border border-slate-200/70 bg-slate-50/75 text-[var(--color-ink)] dark:border-white/8 dark:bg-white/[0.035]")
        }
      >
        {isUser ? (
          <div className="whitespace-pre-wrap text-sm leading-relaxed">
            {message.content}
          </div>
        ) : (
          <div className="prose prose-sm max-w-none text-sm leading-relaxed dark:prose-invert prose-headings:mt-3 prose-headings:mb-1 prose-headings:font-semibold prose-headings:text-[var(--color-ink)] prose-p:my-1.5 prose-strong:text-[var(--color-ink)] prose-ul:my-1 prose-li:my-0.5 prose-table:my-2 prose-table:text-xs prose-th:border prose-th:border-slate-200 prose-th:bg-slate-100 prose-th:px-2 prose-th:py-1 prose-th:text-left prose-th:font-semibold prose-td:border prose-td:border-slate-200 prose-td:px-2 prose-td:py-1 prose-td:align-top dark:prose-th:border-slate-700 dark:prose-th:bg-slate-800 dark:prose-td:border-slate-700">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                table: ({ children }) => (
                  <div className="overflow-x-auto">
                    <table className="min-w-full">{children}</table>
                  </div>
                ),
              }}
            >
              {message.content}
            </ReactMarkdown>
          </div>
        )}

        {message.sources && message.sources.length > 0 && (
          <div className="mt-3 border-t border-slate-200/70 pt-2.5 dark:border-white/10">
            <p className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
              <FileText className="h-3 w-3" />
              Sources
            </p>
            <div className="space-y-1">
              {message.sources.map((source, index) => (
                <p
                  key={`${source.title}-${index}`}
                  className="text-[11px] leading-relaxed text-slate-500 dark:text-slate-400"
                >
                  [{index + 1}] {source.title}
                  {source.section ? ` · ${source.section}` : ""}
                </p>
              ))}
            </div>
          </div>
        )}

        <p
          className={
            "mt-2 text-[10px] " +
            (isUser ? "text-white/60" : "text-slate-400 dark:text-slate-500")
          }
        >
          {new Date(message.timestamp).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          })}
        </p>
      </div>

      {isUser && (
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-900 text-white dark:bg-white dark:text-slate-900">
          <UserRound className="h-4 w-4" />
        </span>
      )}
    </div>
  );
}
