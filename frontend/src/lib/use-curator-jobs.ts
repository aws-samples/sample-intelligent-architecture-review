// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Track in-flight curator chat jobs across navigation.
 *
 * Polling used to live inside the Curate page's sendTurn closure.
 * Navigating away while the model was still generating dropped the
 * closure and orphaned the reply (the backend
 * still completed and saved proposals, but the chat reply text was
 * lost from the user's view). This hook persists the active job_id
 * per scope to localStorage and resumes polling on every page mount,
 * so the indicator stays accurate everywhere and the reply lands
 * back in the Curate transcript when the user returns.
 *
 * Storage shape — one entry per scope (a user can have at most three
 * concurrent jobs, one per scope tab):
 *
 *   localStorage["curator-active-job-on_prem"] = "chat-<uuid>"
 *   localStorage["curator-active-job-cloud"]   = ...
 *   localStorage["curator-active-job-both"]    = ...
 *
 * The hook returns the running set + a remove callback. The Curate
 * page calls remove(scope) once it has appended the reply to its
 * transcript so the indicator clears.
 */

import { useEffect, useRef, useState } from "react";

import { api, HOSTING_SCOPE_VALUES, HostingScope, StandardItem } from "@/lib/api";

const STORAGE_KEY = (scope: HostingScope) => `curator-active-job-${scope}`;

/** The Curate page's per-scope transcript. Shared so this hook can land a
 *  finished reply even when the mounted consumer isn't the Curate page. */
export const CHAT_STORAGE_KEY = (scope: HostingScope) => `curate-chat-${scope}`;

type StoredMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
};

/**
 * Append an assistant message to a scope's stored transcript.
 *
 * The hook does this itself, before notifying any consumer, because it is
 * mounted app-wide (the header indicator) as well as on the Curate page. When
 * the user navigates away mid-generation the header is the ONLY listener and has
 * no callback, so a reply that was only delivered via callback was dropped —
 * exactly the loss this hook exists to prevent. Writing to storage first makes
 * delivery independent of who happens to be listening.
 *
 * Returns true when the message was newly appended, false when an entry for the
 * same job id was already there (so a racing consumer can't double-append).
 */
export function appendAssistantMessage(
  scope: HostingScope,
  jobId: string,
  content: string,
): boolean {
  if (typeof window === "undefined") return false;
  const key = CHAT_STORAGE_KEY(scope);
  try {
    const raw = window.localStorage.getItem(key);
    const prev = raw ? (JSON.parse(raw) as StoredMessage[]) : [];
    // Job id in the message id keeps this idempotent across tabs/consumers.
    const id = `msg-job-${jobId}`;
    if (prev.some((m) => m.id === id)) return false;
    const next = [
      ...prev,
      { id, role: "assistant" as const, content, timestamp: new Date().toISOString() },
    ];
    window.localStorage.setItem(key, JSON.stringify(next));
    return true;
  } catch {
    // localStorage can be full or blocked; losing the cache is not fatal.
    return false;
  }
}

const POLL_BASELINE_MS = 1500;
const POLL_CEILING_MS = 5000;
const POLL_BACKOFF_AFTER_MS = 30_000;
const HARD_TIMEOUT_MS = 5 * 60 * 1000;

type JobStatus = "pending" | "running" | "completed" | "failed";

export type CuratorJob = {
  jobId: string;
  scope: HostingScope;
  startedAt: number;
};

export type CuratorJobResult =
  | { scope: HostingScope; jobId: string; status: "completed"; reply: string; proposals: StandardItem[] }
  | { scope: HostingScope; jobId: string; status: "failed"; error: string }
  | { scope: HostingScope; jobId: string; status: "timeout" };

function readActiveJobs(): CuratorJob[] {
  if (typeof window === "undefined") return [];
  const out: CuratorJob[] = [];
  for (const scope of HOSTING_SCOPE_VALUES) {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY(scope));
      if (raw) {
        // raw is either a bare "chat-<uuid>" (older format) or
        // {jobId, startedAt}.
        let jobId: string;
        let startedAt = Date.now();
        if (raw.startsWith("{")) {
          const parsed = JSON.parse(raw) as { jobId: string; startedAt: number };
          jobId = parsed.jobId;
          startedAt = parsed.startedAt || Date.now();
        } else {
          jobId = raw;
        }
        out.push({ jobId, scope, startedAt });
      }
    } catch {
      // Corrupt entries get cleared on next remove(); ignore here.
    }
  }
  return out;
}

export function rememberJob(scope: HostingScope, jobId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      STORAGE_KEY(scope),
      JSON.stringify({ jobId, startedAt: Date.now() }),
    );
    // Notify any other tabs / hooks listening on the same origin.
    window.dispatchEvent(new CustomEvent("curator-jobs-changed"));
  } catch {
    // Quota errors are non-fatal — worst case the indicator misses.
  }
}

export function forgetJob(scope: HostingScope): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY(scope));
    window.dispatchEvent(new CustomEvent("curator-jobs-changed"));
  } catch {
    // ignore
  }
}

/**
 * Hook: live set of in-flight jobs + a callback channel for completion.
 *
 * Polls every active job in parallel. When any one terminates, fires
 * `onResult(result)` so the caller (Curate page) can append the reply
 * to its transcript. The active set is kept in React state so the
 * indicator can re-render reactively.
 */
export function useCuratorJobs(
  onResult?: (result: CuratorJobResult) => void,
): { activeJobs: CuratorJob[] } {
  const [activeJobs, setActiveJobs] = useState<CuratorJob[]>(() => readActiveJobs());
  const onResultRef = useRef(onResult);

  useEffect(() => {
    onResultRef.current = onResult;
  }, [onResult]);

  // Re-read storage on cross-component changes (the Curate page's
  // chatWithRulesCurator dispatches "curator-jobs-changed" when it
  // calls rememberJob/forgetJob).
  useEffect(() => {
    if (typeof window === "undefined") return;
    const handler = () => setActiveJobs(readActiveJobs());
    window.addEventListener("curator-jobs-changed", handler);
    window.addEventListener("storage", handler);
    return () => {
      window.removeEventListener("curator-jobs-changed", handler);
      window.removeEventListener("storage", handler);
    };
  }, []);

  // Spawn one polling loop per known job. The loop self-cancels when
  // the job is no longer in the active set (so a forgetJob from the
  // Curate page tears it down).
  useEffect(() => {
    let cancelled = false;
    const intervals: ReturnType<typeof setTimeout>[] = [];

    for (const job of activeJobs) {
      let intervalMs = POLL_BASELINE_MS;

      const tick = async () => {
        if (cancelled) return;
        // Bail if the job was forgotten by another caller.
        const stillActive = readActiveJobs().some((j) => j.jobId === job.jobId);
        if (!stillActive) return;

        const elapsed = Date.now() - job.startedAt;
        if (elapsed > HARD_TIMEOUT_MS) {
          appendAssistantMessage(
            job.scope, job.jobId, "_Sorry — chat timed out after 5 minutes._",
          );
          forgetJob(job.scope);
          onResultRef.current?.({ scope: job.scope, jobId: job.jobId, status: "timeout" });
          return;
        }

        try {
          const polled = await api.getCuratorChatJob(job.jobId);
          if (cancelled) return;
          const status = polled.status as JobStatus;
          if (status === "completed") {
            // Land the reply in storage BEFORE notifying, so it survives even
            // when the only mounted consumer (the header indicator) has no
            // callback — which is the navigate-away case this hook exists for.
            appendAssistantMessage(
              job.scope, job.jobId, polled.reply || "(no reply)",
            );
            forgetJob(job.scope);
            onResultRef.current?.({
              scope: job.scope,
              jobId: job.jobId,
              status: "completed",
              reply: polled.reply || "(no reply)",
              proposals: polled.proposals || [],
            });
            return;
          }
          if (status === "failed") {
            appendAssistantMessage(
              job.scope, job.jobId,
              `_Sorry — ${polled.error || "Chat job failed"}._`,
            );
            forgetJob(job.scope);
            onResultRef.current?.({
              scope: job.scope,
              jobId: job.jobId,
              status: "failed",
              error: polled.error || "Chat job failed",
            });
            return;
          }
        } catch {
          // Transient network errors during poll are non-fatal —
          // we'll retry on the next tick. If the backend genuinely
          // can't find the job, it'll 404 and we'll bail through
          // catch + the active-set check above on subsequent ticks.
        }

        if (elapsed > POLL_BACKOFF_AFTER_MS && intervalMs < POLL_CEILING_MS) {
          intervalMs = Math.min(intervalMs * 1.3, POLL_CEILING_MS);
        }
        intervals.push(setTimeout(tick, intervalMs));
      };

      intervals.push(setTimeout(tick, intervalMs));
    }

    return () => {
      cancelled = true;
      for (const id of intervals) clearTimeout(id);
    };
  }, [activeJobs]);

  return { activeJobs };
}
