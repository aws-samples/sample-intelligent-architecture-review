// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ChatMessage } from "@/components/query/ChatMessage";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, HostingScope, HOSTING_SCOPE_VALUES, RuleChatTurn, StandardItem } from "@/lib/api";
import type { ChatMessage as ChatMessageType } from "@/lib/mock-data";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  CHAT_STORAGE_KEY,
  CuratorJobResult,
  forgetJob,
  rememberJob,
  useCuratorJobs,
} from "@/lib/use-curator-jobs";

const SUGGESTION_PROMPT =
  "Review every standard already in the library. Identify gaps in coverage, " +
  "patterns of HA risk that aren't represented, and any rules that look weak or " +
  "redundant. Then propose new rules that would strengthen the library. Up to 5 " +
  "proposals per reply. For each proposal, explain in one sentence what gap it fills.";

// Segmented control labels match the Standards page so the
// vocabulary stays consistent across surfaces.
const HOSTING_SCOPE_LABELS: Record<HostingScope, string> = {
  on_prem: "On-prem",
  cloud: "Cloud",
  both: "Universal",
};

// CHAT_STORAGE_KEY lives in use-curator-jobs (imported above) so the hook can
// land a finished reply into the right scope's transcript even when this page
// isn't mounted — which is what makes a reply survive navigating away.

/**
 * Full-page workspace for AI-driven rule curation.
 *
 * Layout: chat on the left (~3/5), proposal review on the right (~2/5).
 * Both columns are independently scrollable so a long chat thread
 * doesn't squeeze proposals out, and a backlog of proposals doesn't
 * push the input off-screen.
 *
 * Built as a route instead of a drawer because curation is a
 * deliberative workflow (read several proposals, ask follow-ups,
 * approve/reject in batches) — that doesn't fit a side panel.
 */
type KindFilter = "all" | "add" | "edit" | "delete";

export default function CurateRulesPage() {
  // Chat transcripts are stored per scope so switching doesn't blend
  // conversations across deployment models.
  const [hostingScope, setHostingScope] = useState<HostingScope>("both");
  const [messages, setMessages] = useState<ChatMessageType[]>([]);
  const [input, setInput] = useState("");
  const [proposals, setProposals] = useState<StandardItem[]>([]);
  /** Map of currently-active standards keyed by id, so edit/delete
   *  proposals can render the "current" snapshot next to the proposed
   *  one without an extra fetch per card. */
  const [activeMap, setActiveMap] = useState<Record<string, StandardItem>>({});
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");
  const [sending, setSending] = useState(false);
  const [loadedScope, setLoadedScope] = useState<HostingScope | null>(null);
  const [actingId, setActingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Live streaming state for the in-flight assistant turn. `streamReply`
  // accumulates user-facing deltas; `streamThinking` accumulates the
  // extended-thinking (reasoning) trace shown in a collapsible panel.
  // Both reset to "" when a turn finishes (the final reply is committed
  // to `messages`).
  const [streamReply, setStreamReply] = useState("");
  const [streamThinking, setStreamThinking] = useState("");
  const [streaming, setStreaming] = useState(false);
  // How many proposals the model has started drafting this turn. During
  // that stretch the token stream is silent (proposal JSON is suppressed),
  // so we show a "drafting proposal N" pill. Reset each turn.
  const [proposalProgress, setProposalProgress] = useState(0);
  // Full-screen proposal review: a multi-column overlay for reviewing a
  // large batch at once, with checkbox multi-select + bulk approve/reject.
  const [fullScreen, setFullScreen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkActing, setBulkActing] = useState(false);
  const chatScrollRef = useRef<HTMLDivElement>(null);
  // Abort handle for the in-flight SSE stream so we can cancel on
  // scope-switch or unmount without leaking a reader.
  const streamAbortRef = useRef<AbortController | null>(null);
  // Job ids the stream already committed to the transcript. The durable
  // job stays "remembered" during streaming so a mid-stream refresh can
  // resume it — but if THIS client finishes the stream normally, we must
  // not also let useCuratorJobs' poll re-append the same reply. This set
  // lets handleJobResult skip an already-committed job.
  const committedJobIdsRef = useRef<Set<string>>(new Set());

  // Load existing pending proposals + active standards on mount and on
  // every scope switch. Proposals are filtered server-side by the
  // active scope so each tab shows only proposals it generated.
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.listStandards({ status: "proposed", hostingScope: hostingScope, source: "generated" }),
      api.listStandards(),
    ])
      .then(([proposedRes, activeRes]) => {
        if (cancelled) return;
        setProposals(proposedRes.standards);
        const map: Record<string, StandardItem> = {};
        for (const s of activeRes.standards) map[s.id] = s;
        setActiveMap(map);
      })
      .catch(() => {
        // empty lists are fine
      });
    return () => {
      cancelled = true;
    };
  }, [hostingScope]);

  // Auto-scroll the chat thread when new messages land or the live
  // stream grows, so the newest tokens stay in view.
  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [messages, sending, streamReply, streamThinking, proposalProgress]);

  // Load chat history for the active scope on mount /
  // scope-switch, and persist on every messages change. localStorage
  // is per-origin so switching browsers or clearing storage starts
  // fresh, which is fine for a curation tool.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const kickoff = window.setTimeout(() => {
      try {
        const raw = window.localStorage.getItem(CHAT_STORAGE_KEY(hostingScope));
        setMessages(raw ? (JSON.parse(raw) as ChatMessageType[]) : []);
      } catch {
        setMessages([]);
      }
      setInput("");
      setLoadedScope(hostingScope);
    }, 0);
    return () => window.clearTimeout(kickoff);
  }, [hostingScope]);

  useEffect(() => {
    if (typeof window === "undefined" || loadedScope !== hostingScope) return;
    try {
      window.localStorage.setItem(
        CHAT_STORAGE_KEY(hostingScope),
        JSON.stringify(messages),
      );
    } catch {
      // Quota or serialization errors silently drop persistence —
      // not worth surfacing in a curation flow.
    }
  }, [messages, hostingScope, loadedScope]);

  // Cancel any in-flight stream when the scope changes or the page
  // unmounts. requestScopeSwitch already blocks switching while sending,
  // so this is a safety net (e.g. unmount mid-stream).
  useEffect(() => {
    return () => {
      streamAbortRef.current?.abort();
    };
  }, [hostingScope]);

  // Escape closes the full-screen review.
  useEffect(() => {
    if (!fullScreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullScreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullScreen]);

  // Kick off a chat turn. Streams the reply live over SSE: reasoning
  // deltas feed the thinking trace, reply deltas render token-by-token,
  // and the saved proposals arrive in-band at the end (already persisted,
  // so no read-after-write race — the panel updates immediately).
  //
  // The prior async-job path (startCuratorChatJob + poll) is kept only
  // for RESUMING jobs started before this streaming path shipped — see
  // handleJobResult / useCuratorJobs below. New turns stream directly.
  const sendTurn = async (userText: string) => {
    if (!userText.trim() || sending || streaming) return;
    setError(null);
    const turnScope = hostingScope;
    const userMessage: ChatMessageType = {
      id: `msg-${Date.now()}`,
      role: "user",
      content: userText,
      timestamp: new Date().toISOString(),
    };
    const nextMessages = [...messages, userMessage];
    setMessages(nextMessages);
    setStreamReply("");
    setStreamThinking("");
    setProposalProgress(0);
    setStreaming(true);

    const turns: RuleChatTurn[] = nextMessages.map((m) => ({
      role: m.role as "user" | "assistant",
      content: m.content,
    }));

    // Accumulate outside React state too, so the final commit doesn't
    // depend on a state flush having landed.
    let replyAccum = "";
    let sawProposals = false;
    let jobId: string | null = null;

    const controller = new AbortController();
    streamAbortRef.current = controller;
    try {
      await api.streamCuratorChat(
        turns,
        turnScope,
        {
          onJob: (id) => {
            // Remember the durable job so a mid-stream refresh/navigate
            // resumes it via useCuratorJobs. Mark it committed for THIS
            // page session immediately: scope-switch is blocked while
            // streaming (isSending), so the only mid-stream interruption
            // is a full unmount → a fresh page whose committed set is
            // empty, where resume SHOULD fire. Marking committed now
            // closes the race where the background poll could observe the
            // job as completed (right after the pump persists it) before
            // the stream returns, and double-append the reply.
            jobId = id;
            committedJobIdsRef.current.add(id);
            rememberJob(turnScope, id);
          },
          onThinking: (t) => setStreamThinking((prev) => prev + t),
          onDelta: (t) => {
            replyAccum += t;
            setStreamReply((prev) => prev + t);
          },
          onProposalProgress: (n) => setProposalProgress(n),
          onProposals: (rows) => {
            sawProposals = true;
            if (turnScope !== hostingScope) return;
            if (rows.length > 0) {
              setProposals((prev) => {
                const byId = new Map(prev.map((p) => [p.id, p]));
                for (const p of rows) byId.set(p.id, p);
                return Array.from(byId.values());
              });
            }
          },
        },
        controller.signal,
      );

      // Stream finished normally on THIS client. Forget the durable job
      // (already marked committed in onJob) so the resume poll won't
      // re-append the same reply, then commit the streamed reply as a
      // permanent assistant message.
      if (jobId) {
        forgetJob(turnScope);
      }
      const replyText = replyAccum.trim() || "(no reply)";
      const aiMessage: ChatMessageType = {
        id: `msg-${Date.now() + 1}`,
        role: "assistant",
        content: replyText,
        timestamp: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, aiMessage]);

      // Best-effort reconcile in case a proposals frame never arrived
      // (e.g. the turn produced none, or an older backend without the
      // frame). Only overwrites when the fetch actually returns rows.
      if (!sawProposals && turnScope === hostingScope) {
        api
          .listStandards({ status: "proposed", hostingScope: turnScope, source: "generated" })
          .then((res) => {
            if (res.standards.length > 0) setProposals(res.standards);
          })
          .catch(() => {});
      }
    } catch (err) {
      if ((err as Error)?.name === "AbortError") {
        // User navigated / switched scope mid-stream. Leave the job
        // REMEMBERED (don't forget it) so useCuratorJobs resumes it and
        // handleJobResult lands the reply when the user returns. The pump
        // thread on the backend keeps running and persists the turn.
      } else {
        // Real failure — the backend already marked the job failed, so
        // forget it here to avoid a duplicate error line from the poller.
        // (Already marked committed in onJob.)
        if (jobId) {
          forgetJob(turnScope);
        }
        const errMsg = err instanceof Error ? err.message : "Chat failed";
        // Keep whatever the model had already streamed. A long reasoning turn
        // that dies near the end would otherwise discard everything the user
        // was reading, leaving only the error line.
        const partial = replyAccum.trim();
        setMessages((prev) => [
          ...prev,
          ...(partial
            ? [{
                id: `msg-${Date.now()}-partial`,
                role: "assistant" as const,
                content: partial,
                timestamp: new Date().toISOString(),
              }]
            : []),
          {
            id: `msg-${Date.now() + 1}`,
            role: "assistant",
            content: partial
              ? `_Sorry — the reply above was cut short: ${errMsg}._`
              : `_Sorry — ${errMsg}._`,
            timestamp: new Date().toISOString(),
          },
        ]);
        setError(errMsg);
      }
    } finally {
      streamAbortRef.current = null;
      setStreaming(false);
      setStreamReply("");
      setStreamThinking("");
      setProposalProgress(0);
    }
  };

  // Hook callback when ANY scope's job finishes. We only act on
  // results matching the active scope so a result coming in for a
  // background tab doesn't pollute the visible transcript — but we
  // still need to land the reply in that tab's localStorage so it
  // shows up when the user switches.
  const handleJobResult = (result: CuratorJobResult) => {
    // Skip jobs this client already committed via the live stream — the
    // durable job stays remembered during streaming so a refresh can
    // resume it, but on normal completion the stream both forgets it and
    // records it here, so a racing poll must not re-append the reply.
    if (committedJobIdsRef.current.has(result.jobId)) {
      forgetJob(result.scope);
      return;
    }
    if (result.status === "completed") {
      const aiMessage: ChatMessageType = {
        id: `msg-${Date.now()}`,
        role: "assistant",
        content: result.reply,
        timestamp: new Date().toISOString(),
      };
      // The hook already persisted this reply (idempotently, keyed on job id)
      // before calling us, so there is nothing to write here — only React state
      // to update below for the visible scope.
      // If the result matches the currently visible scope, also
      // update React state so it renders without waiting for a
      // remount.
      if (result.scope === hostingScope) {
        setMessages((prev) => [...prev, aiMessage]);
        setSending(false);
        // Render the proposals the job ALREADY returned immediately, merged
        // by id. Previously we only re-fetched via listStandards, which
        // could race DynamoDB read-after-write (proposals saved but the
        // immediate list read missed them) — leaving the panel empty until
        // a manual page refresh. The job result is authoritative, so seed
        // from it first, then reconcile with a best-effort re-fetch.
        if (result.proposals && result.proposals.length > 0) {
          setProposals((prev) => {
            const byId = new Map(prev.map((p) => [p.id, p]));
            for (const p of result.proposals) byId.set(p.id, p);
            return Array.from(byId.values());
          });
        }
        api
          .listStandards({ status: "proposed", hostingScope, source: "generated" })
          .then((res) => {
            if (res.standards.length > 0) setProposals(res.standards);
          })
          .catch(() => {});
      }
    } else if (result.status === "failed" || result.status === "timeout") {
      const errMsg =
        result.status === "timeout"
          ? "Chat timed out after 5 minutes."
          : (result as { error: string }).error;
      const aiMessage: ChatMessageType = {
        id: `msg-${Date.now()}`,
        role: "assistant",
        content: `_Sorry — ${errMsg}._`,
        timestamp: new Date().toISOString(),
      };
      // Already persisted by the hook (see above).
      if (result.scope === hostingScope) {
        setMessages((prev) => [...prev, aiMessage]);
        setError(errMsg);
        setSending(false);
      }
    }
    forgetJob(result.scope);
  };

  // Subscribe to all in-flight jobs (any scope) so a job from another
  // tab still gets its reply persisted when it finishes.
  const { activeJobs } = useCuratorJobs(handleJobResult);

  // A resumed job is derived from the persisted active set instead of
  // copying that external state into another effect-driven state value.
  const isSending =
    sending ||
    streaming ||
    activeJobs.some((job) => job.scope === hostingScope);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    setInput("");
    sendTurn(text);
  };

  const handleSuggestion = () => sendTurn(SUGGESTION_PROMPT);

  // Switching scopes swaps transcript + drops in-flight
  // typing. Guard with a styled Dialog when the user has draft text
  // to avoid silently losing it. Sending state blocks the switch
  // entirely so we don't strand a pending /api/standards/chat call.
  const [pendingScopeSwitch, setPendingScopeSwitch] = useState<HostingScope | null>(null);
  const [scopeInfoOpen, setScopeInfoOpen] = useState(false);
  // Expand/collapse the proposal panel for complex add/edit
  // proposals where the side-by-side current/proposed view squeezes
  // text. "wide" flips the columns so proposals dominate.
  const [proposalsWide, setProposalsWide] = useState(false);

  // Switch scope + reset the proposal-review UI. Scope swaps the proposal
  // set entirely, so any selection / full-screen state would be stale.
  const switchScope = (next: HostingScope) => {
    setSelectedIds(new Set());
    setFullScreen(false);
    setHostingScope(next);
  };

  const requestScopeSwitch = (next: HostingScope) => {
    if (next === hostingScope || isSending) return;
    if (input.trim().length > 0) {
      setPendingScopeSwitch(next);
      return;
    }
    switchScope(next);
  };

  const confirmScopeSwitch = () => {
    if (pendingScopeSwitch) {
      switchScope(pendingScopeSwitch);
    }
    setPendingScopeSwitch(null);
  };

  // Reconcile the activeMap after an approval lands, given the proposal
  // and the approved row. Shared by single + bulk approve so both keep
  // the current-rule snapshots consistent for later edit/delete cards.
  const reconcileApproval = (proposal: StandardItem, result: StandardItem) => {
    const kind = proposal.proposal_kind || "add";
    const targetId = proposal.target_id;
    if (kind === "add") {
      setActiveMap((prev) => ({ ...prev, [proposal.id]: result }));
    } else if (kind === "edit" && targetId) {
      setActiveMap((prev) => ({ ...prev, [targetId]: result }));
    } else if (kind === "delete" && targetId) {
      setActiveMap((prev) => {
        const next = { ...prev };
        delete next[targetId];
        return next;
      });
    }
  };

  // Drop an id from the selection set (used after any approve/reject so a
  // consumed proposal never lingers as "selected").
  const deselect = (ids: string[]) => {
    setSelectedIds((prev) => {
      if (prev.size === 0) return prev;
      const next = new Set(prev);
      for (const id of ids) next.delete(id);
      return next;
    });
  };

  const approve = async (proposal: StandardItem) => {
    setActingId(proposal.id);
    try {
      const result = await api.approveStandard(proposal.id);
      setProposals((prev) => prev.filter((p) => p.id !== proposal.id));
      reconcileApproval(proposal, result);
      deselect([proposal.id]);
      // Notify the sidebar/standards count that the active set grew.
      if (typeof window !== "undefined")
        window.dispatchEvent(new CustomEvent("standards-changed"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approve failed");
    } finally {
      setActingId(null);
    }
  };

  const reject = async (id: string) => {
    setActingId(id);
    try {
      await api.rejectStandard(id);
      setProposals((prev) => prev.filter((p) => p.id !== id));
      deselect([id]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reject failed");
    } finally {
      setActingId(null);
    }
  };

  // Toggle one proposal's selection in the full-screen review.
  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // Bulk approve/reject the currently-selected proposals. We loop the
  // per-item endpoints (there's no batch API) sequentially so DynamoDB
  // isn't hammered and edit/delete targets reconcile in a stable order.
  // Failures are collected and surfaced; successfully-actioned rows are
  // removed as we go so a partial failure still reflects real progress.
  const bulkApprove = async (proposalsToApprove: StandardItem[]) => {
    if (bulkActing || proposalsToApprove.length === 0) return;
    setBulkActing(true);
    setError(null);
    let failures = 0;
    for (const p of proposalsToApprove) {
      try {
        const result = await api.approveStandard(p.id);
        setProposals((prev) => prev.filter((x) => x.id !== p.id));
        reconcileApproval(p, result);
        deselect([p.id]);
      } catch {
        failures += 1;
      }
    }
    if (failures > 0) {
      setError(`${failures} of ${proposalsToApprove.length} could not be approved.`);
    }
    if (typeof window !== "undefined")
      window.dispatchEvent(new CustomEvent("standards-changed"));
    setBulkActing(false);
  };

  const bulkReject = async (ids: string[]) => {
    if (bulkActing || ids.length === 0) return;
    setBulkActing(true);
    setError(null);
    let failures = 0;
    for (const id of ids) {
      try {
        await api.rejectStandard(id);
        setProposals((prev) => prev.filter((x) => x.id !== id));
        deselect([id]);
      } catch {
        failures += 1;
      }
    }
    if (failures > 0) {
      setError(`${failures} of ${ids.length} could not be rejected.`);
    }
    setBulkActing(false);
  };

  // Proposals visible under the current kind filter — shared by the side
  // panel and the full-screen review so both honor the same chips.
  const filteredProposals = proposals.filter(
    (p) => kindFilter === "all" || (p.proposal_kind || "add") === kindFilter,
  );
  // Selected rows that are still present under the current filter — the
  // set bulk actions operate on (so "Approve 5" matches what's visible).
  const selectedVisible = filteredProposals.filter((p) => selectedIds.has(p.id));
  const allVisibleSelected =
    filteredProposals.length > 0 && selectedVisible.length === filteredProposals.length;

  const toggleSelectAll = () => {
    if (allVisibleSelected) {
      deselect(filteredProposals.map((p) => p.id));
    } else {
      setSelectedIds((prev) => {
        const next = new Set(prev);
        for (const p of filteredProposals) next.add(p.id);
        return next;
      });
    }
  };

  return (
    <div className="flex h-[calc(100vh-7rem)] flex-col gap-5 animate-in fade-in duration-300">
      {/* Header */}
      <div className="space-y-3">
        <Link
          href="/standards"
          className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 transition-colors hover:text-[var(--color-brand-primary)] dark:text-slate-400"
        >
          <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Standards
        </Link>
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-normal text-slate-900 dark:text-slate-50">
            Curate rules with AI
          </h1>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Ask for new rules, or click <em>AI Suggestion</em> to have the AI scan gaps in the library.
          </p>
        </div>

        {/* Hosting scope segmented control. Picking a scope
            filters the library brief sent to the curator (so it
            recommends scope-relevant gaps), tags every proposed rule
            with that scope on save, and swaps the chat transcript
            (one per scope, persisted client-side). */}
        <div className="flex items-center gap-3">
          <div className="inline-flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white p-0.5 dark:border-white/10 dark:bg-white/5">
            {HOSTING_SCOPE_VALUES.map((s) => {
              const isActive = hostingScope === s;
              return (
                <button
                  key={s}
                  type="button"
                  onClick={() => requestScopeSwitch(s)}
                  disabled={isSending}
                  className={
                    "inline-flex items-center rounded-md px-3 py-1.5 text-xs font-medium transition-all disabled:cursor-not-allowed disabled:opacity-60 " +
                    (isActive
                      ? "bg-[var(--color-brand-primary)] text-white shadow-sm"
                      : "text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-white")
                  }
                  aria-pressed={isActive}
                >
                  {HOSTING_SCOPE_LABELS[s]}
                </button>
              );
            })}
          </div>
          <span className="inline-flex items-center gap-1.5 text-[11px] text-slate-500 dark:text-slate-400">
            Curating <strong className="text-slate-700 dark:text-slate-200">{HOSTING_SCOPE_LABELS[hostingScope]}</strong> rules · separate transcript per scope
            <button
              type="button"
              onClick={() => setScopeInfoOpen(true)}
              aria-label="What does each scope see?"
              className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-slate-500 transition-colors hover:border-[var(--color-brand-primary)]/40 hover:text-[var(--color-brand-primary)] dark:border-white/15 dark:text-slate-400 dark:hover:border-[var(--color-brand-primary)]/40"
            >
              <svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </button>
          </span>
        </div>
      </div>

      {/* Two-column workspace. The grid column ratios swap when the
          user expands the proposals panel — useful for dense add/edit
          proposals where the side-by-side view squeezes text. */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-5">
        {/* Chat column */}
        <Card
          className={
            "flex min-h-0 flex-col " +
            (proposalsWide ? "lg:col-span-2" : "lg:col-span-3")
          }
        >
          <CardHeader className="border-b border-slate-200/60 px-5 py-3.5 dark:border-white/8">
            <CardTitle className="text-sm font-semibold">Conversation</CardTitle>
          </CardHeader>

          <CardContent className="flex min-h-0 flex-1 flex-col p-0">
            {/* Thread — scrolls */}
            <div ref={chatScrollRef} className="flex-1 min-h-0 space-y-3 overflow-y-auto px-5 py-4">
              {messages.length === 0 ? (
                <div className="flex h-full flex-col items-center justify-center text-center">
                  <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-[var(--color-brand-primary)]/10 to-[var(--color-brand-primary)]/[0.04] ring-1 ring-[var(--color-brand-primary)]/15">
                    <svg className="h-5 w-5 text-[var(--color-brand-primary)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                    </svg>
                  </div>
                  <p className="mt-4 max-w-md text-sm font-medium text-slate-700 dark:text-slate-200">
                    Tell the curator what you&apos;d like.
                  </p>
                  <p className="mt-1.5 max-w-md text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                    e.g. <em>&quot;We need a rule about RTO targets for Tier-4 systems.&quot;</em>
                    <br />
                    Or click <em>AI Suggestion</em> to scan gaps.
                  </p>
                </div>
              ) : (
                messages.map((m) => <ChatMessage key={m.id} message={m} />)
              )}

              {/* Live reasoning trace — collapsible, shown while the model
                  is thinking. Distinct from the answer so the two don't
                  blur together. */}
              {streaming && streamThinking && (
                <ThinkingTrace text={streamThinking} />
              )}

              {/* Live reply — streamed token-by-token into a real
                  assistant bubble so it reads identically to the committed
                  message it becomes. */}
              {streaming && streamReply && (
                <ChatMessage
                  message={{
                    id: "stream-live",
                    role: "assistant",
                    content: streamReply,
                    timestamp: new Date().toISOString(),
                  }}
                />
              )}

              {/* Proposal-drafting pill. While the model emits <propose>
                  JSON (suppressed from the stream), the reply goes silent —
                  this shows the work isn't stalled. */}
              {streaming && proposalProgress > 0 && (
                <div className="ml-2 inline-flex items-center gap-2 rounded-full border border-[var(--color-brand-primary)]/20 bg-[var(--color-brand-primary)]/[0.06] px-3 py-1.5">
                  <svg className="h-3.5 w-3.5 animate-spin text-[var(--color-brand-primary)]" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  <span className="text-xs font-medium text-[var(--color-brand-primary)]">
                    Drafting proposal {proposalProgress}…
                  </span>
                </div>
              )}

              {/* Spinner only while we're waiting for the first token (or
                  resuming a non-streaming async job). Once text streams in, the
                  live bubble above conveys progress. Suppressed while a
                  proposal is being drafted (the pill covers that). */}
              {isSending && !streamReply && proposalProgress === 0 && (
                <div className="flex items-center gap-2 px-2 py-2">
                  <span className="flex h-7 w-7 items-center justify-center rounded-full bg-[var(--color-brand-primary)]/10 ring-1 ring-[var(--color-brand-primary)]/15">
                    <svg className="h-3.5 w-3.5 animate-spin text-[var(--color-brand-primary)]" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                  </span>
                  <span className="text-xs text-slate-500 dark:text-slate-400">
                    {streamThinking ? "Reasoning…" : "Thinking…"}
                  </span>
                </div>
              )}
            </div>

            {error && (
              <div className="border-t border-rose-200/80 bg-rose-50 px-5 py-2 text-xs font-medium text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300">
                {error}
              </div>
            )}

            {/* Input */}
            <form onSubmit={handleSubmit} className="space-y-2.5 border-t border-slate-200/60 px-5 py-3.5 dark:border-white/8">
              <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-2.5 py-1 transition-all focus-within:border-[var(--color-brand-primary)]/40 focus-within:ring-2 focus-within:ring-[var(--color-brand-primary)]/15 dark:border-white/10 dark:bg-white/5">
                <input
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="Ask for a rule…"
                  disabled={isSending}
                  className="flex-1 bg-transparent px-1.5 py-1.5 text-sm text-slate-900 outline-none placeholder:text-slate-400 disabled:opacity-50 dark:text-slate-100 dark:placeholder:text-slate-500"
                />
                <button
                  type="submit"
                  disabled={isSending || !input.trim()}
                  className="inline-flex h-8 items-center gap-1 rounded-md bg-[var(--color-brand-primary)] px-3 text-xs font-semibold text-white shadow-sm shadow-[var(--color-brand-primary)]/20 transition-all hover:bg-[var(--color-brand-primary-hover)] hover:shadow-md hover:shadow-[var(--color-brand-primary)]/25 active:scale-95 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-[var(--color-brand-primary)]"
                >
                  Send
                  <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 12h14M13 5l7 7-7 7" />
                  </svg>
                </button>
              </div>
              <button
                type="button"
                onClick={handleSuggestion}
                disabled={isSending}
                className="inline-flex items-center gap-1.5 text-[11px] font-medium text-[var(--color-brand-primary)] transition-colors hover:text-[var(--color-brand-primary-hover)] disabled:opacity-50"
              >
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                </svg>
                AI Suggestion — scan the library and propose gaps
              </button>
            </form>
          </CardContent>
        </Card>

        {/* Proposals column */}
        <Card
          className={
            "flex min-h-0 flex-col " +
            (proposalsWide ? "lg:col-span-3" : "lg:col-span-2")
          }
        >
          <CardHeader className="flex flex-row items-center justify-between border-b border-slate-200/60 px-5 py-3.5 dark:border-white/8">
            <CardTitle className="flex items-center gap-2 text-sm font-semibold">
              Pending proposals
              {proposals.length > 0 && (
                <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-[var(--color-brand-primary)] px-1.5 text-[10px] font-semibold text-white">
                  {proposals.length}
                </span>
              )}
            </CardTitle>
            <div className="flex items-center gap-1">
              {proposals.length > 0 && (
                <button
                  type="button"
                  onClick={() => setFullScreen(true)}
                  aria-label="Review proposals full screen"
                  title="Full-screen review (bulk approve/reject)"
                  className="inline-flex h-6 items-center gap-1 rounded-md px-1.5 text-[11px] font-medium text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-white/8 dark:hover:text-slate-200"
                >
                  <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
                  </svg>
                  Full screen
                </button>
              )}
              <button
                type="button"
                onClick={() => setProposalsWide((v) => !v)}
                aria-label={proposalsWide ? "Shrink proposal panel" : "Expand proposal panel"}
                title={proposalsWide ? "Shrink proposal panel" : "Expand proposal panel"}
                className="inline-flex h-6 w-6 items-center justify-center rounded-md text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-white/8 dark:hover:text-slate-200"
              >
                {proposalsWide ? (
                  <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                  </svg>
                ) : (
                  <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
                  </svg>
                )}
              </button>
            </div>
          </CardHeader>

          {/* Filter chips */}
          {proposals.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 border-b border-slate-200/60 px-4 py-2.5 dark:border-white/8">
              {(["all", "add", "edit", "delete"] as const).map((k) => {
                const count =
                  k === "all"
                    ? proposals.length
                    : proposals.filter((p) => (p.proposal_kind || "add") === k).length;
                const active = kindFilter === k;
                const activeTone =
                  k === "add"    ? "bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20"
                  : k === "edit" ? "bg-sky-50 text-sky-700 border-sky-200/80 dark:bg-sky-500/10 dark:text-sky-300 dark:border-sky-500/20"
                  : k === "delete" ? "bg-rose-50 text-rose-700 border-rose-200/80 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20"
                  : "bg-[var(--color-brand-primary)]/10 text-[var(--color-brand-primary)] border-[var(--color-brand-primary)]/20";
                return (
                  <button
                    key={k}
                    onClick={() => setKindFilter(k)}
                    className={
                      "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-all " +
                      (active
                        ? activeTone
                        : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10")
                    }
                  >
                    {k === "all" ? "All" : k === "add" ? "+ New" : k === "edit" ? "~ Edit" : "× Delete"}
                    <span className="opacity-60">{count}</span>
                  </button>
                );
              })}
            </div>
          )}

          <CardContent className="min-h-0 flex-1 overflow-y-auto p-4">
            {proposals.length === 0 ? (
              <div className="flex h-full items-center justify-center text-center">
                <p className="max-w-xs text-sm leading-relaxed text-slate-500 dark:text-slate-400">
                  No pending proposals yet. Chat with the curator on the left or click <em>AI Suggestion</em> to generate some.
                </p>
              </div>
            ) : filteredProposals.length === 0 ? (
              <p className="py-8 text-center text-sm text-slate-500 dark:text-slate-400">
                No proposals match this filter.
              </p>
            ) : (
              <ul className="space-y-3">
                {filteredProposals.map((p) => (
                  <ProposalCard
                    key={p.id}
                    proposal={p}
                    target={p.target_id ? activeMap[p.target_id] : undefined}
                    acting={actingId === p.id}
                    onApprove={() => approve(p)}
                    onReject={() => reject(p.id)}
                  />
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Full-screen proposal review — a multi-column overlay for
          reviewing a large batch at once, with checkbox multi-select and
          bulk approve/reject. Reuses the same ProposalCard + filter chips
          as the side panel so the two stay visually consistent. */}
      {fullScreen && (
        <div className="fixed inset-0 z-50 flex flex-col bg-white dark:bg-slate-950 animate-in fade-in duration-150">
          {/* Header */}
          <div className="flex items-center justify-between gap-3 border-b border-slate-200/80 px-5 py-3.5 dark:border-white/10">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-50">
                Review proposals
              </h2>
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-white/8 dark:text-slate-300">
                {HOSTING_SCOPE_LABELS[hostingScope]}
              </span>
              {filteredProposals.length > 0 && (
                <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-[var(--color-brand-primary)] px-1.5 text-[10px] font-semibold text-white">
                  {filteredProposals.length}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={() => setFullScreen(false)}
              aria-label="Close full-screen review"
              title="Close (Esc)"
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 text-xs font-medium text-slate-600 transition-colors hover:bg-slate-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
            >
              <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
              Close
            </button>
          </div>

          {/* Filter chips */}
          {proposals.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 border-b border-slate-200/60 px-5 py-2.5 dark:border-white/8">
              {(["all", "add", "edit", "delete"] as const).map((k) => {
                const count =
                  k === "all"
                    ? proposals.length
                    : proposals.filter((p) => (p.proposal_kind || "add") === k).length;
                const active = kindFilter === k;
                const activeTone =
                  k === "add"    ? "bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20"
                  : k === "edit" ? "bg-sky-50 text-sky-700 border-sky-200/80 dark:bg-sky-500/10 dark:text-sky-300 dark:border-sky-500/20"
                  : k === "delete" ? "bg-rose-50 text-rose-700 border-rose-200/80 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20"
                  : "bg-[var(--color-brand-primary)]/10 text-[var(--color-brand-primary)] border-[var(--color-brand-primary)]/20";
                return (
                  <button
                    key={k}
                    onClick={() => setKindFilter(k)}
                    className={
                      "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-all " +
                      (active
                        ? activeTone
                        : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10")
                    }
                  >
                    {k === "all" ? "All" : k === "add" ? "+ New" : k === "edit" ? "~ Edit" : "× Delete"}
                    <span className="opacity-60">{count}</span>
                  </button>
                );
              })}
            </div>
          )}

          {/* Multi-select toolbar */}
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/60 bg-slate-50/60 px-5 py-2.5 dark:border-white/8 dark:bg-white/[0.02]">
            <div className="flex items-center gap-3">
              <label className="flex cursor-pointer items-center gap-2 text-xs font-medium text-slate-600 dark:text-slate-300">
                <input
                  type="checkbox"
                  checked={allVisibleSelected}
                  onChange={toggleSelectAll}
                  disabled={filteredProposals.length === 0}
                  aria-label="Select all visible proposals"
                  className="h-4 w-4 cursor-pointer rounded border-slate-300 text-[var(--color-brand-primary)] focus:ring-2 focus:ring-[var(--color-brand-primary)]/30 dark:border-white/20 dark:bg-white/5"
                />
                {allVisibleSelected ? "Deselect all" : "Select all"}
              </label>
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {selectedVisible.length} selected
              </span>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => bulkApprove(selectedVisible)}
                disabled={bulkActing || selectedVisible.length === 0}
                className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm shadow-emerald-600/20 transition-all hover:bg-emerald-700 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-emerald-600"
              >
                {bulkActing && (
                  <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                )}
                Approve {selectedVisible.length > 0 ? selectedVisible.length : ""}
              </button>
              <button
                type="button"
                onClick={() => bulkReject(selectedVisible.map((p) => p.id))}
                disabled={bulkActing || selectedVisible.length === 0}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
              >
                Reject {selectedVisible.length > 0 ? selectedVisible.length : ""}
              </button>
            </div>
          </div>

          {error && (
            <div className="border-b border-rose-200/80 bg-rose-50 px-5 py-2 text-xs font-medium text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300">
              {error}
            </div>
          )}

          {/* Grid of proposals */}
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            {filteredProposals.length === 0 ? (
              <div className="flex h-full items-center justify-center">
                <p className="max-w-xs text-center text-sm text-slate-500 dark:text-slate-400">
                  {proposals.length === 0
                    ? "No pending proposals yet."
                    : "No proposals match this filter."}
                </p>
              </div>
            ) : (
              <ul className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
                {filteredProposals.map((p) => (
                  <ProposalCard
                    key={p.id}
                    proposal={p}
                    target={p.target_id ? activeMap[p.target_id] : undefined}
                    acting={actingId === p.id || bulkActing}
                    onApprove={() => approve(p)}
                    onReject={() => reject(p.id)}
                    selectable
                    selected={selectedIds.has(p.id)}
                    onToggleSelect={() => toggleSelect(p.id)}
                  />
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {/* Scope-switch confirmation. Only opens when the user has
          unsent draft text and tries to flip to a different
          hosting scope. */}
      <Dialog open={pendingScopeSwitch !== null} onOpenChange={(o) => { if (!o) setPendingScopeSwitch(null); }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <div className="flex items-start gap-3">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-amber-500/15 ring-1 ring-amber-500/30">
                <svg className="h-4 w-4 text-amber-600 dark:text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </span>
              <div className="min-w-0 flex-1">
                <DialogTitle className="text-sm">
                  Switch to {pendingScopeSwitch ? HOSTING_SCOPE_LABELS[pendingScopeSwitch] : ""}?
                </DialogTitle>
                <p className="mt-1.5 text-xs leading-relaxed text-slate-600 dark:text-slate-400">
                  Your unsent draft will be cleared. The conversation transcript
                  for the new scope will load.
                </p>
              </div>
            </div>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setPendingScopeSwitch(null)}>
              Cancel
            </Button>
            <Button size="sm" onClick={confirmScopeSwitch}>
              Switch scope
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Scope info dialog. Explains what rules the AI sees in each
          tab and how the per-scope transcript persistence works. */}
      <Dialog open={scopeInfoOpen} onOpenChange={setScopeInfoOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="text-sm">What each scope sees</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
            <p>
              The AI scans only the rules visible in the current tab. The
              same prompt produces different proposals depending on which
              tab you&apos;re in.
            </p>
            <ul className="space-y-1.5 rounded-lg border border-slate-200/60 bg-slate-50/60 p-3 dark:border-white/8 dark:bg-white/[0.03]">
              <li>
                <strong className="text-slate-800 dark:text-slate-100">On-prem</strong> → On-prem rules + Universal rules
              </li>
              <li>
                <strong className="text-slate-800 dark:text-slate-100">Cloud</strong> → Cloud rules + Universal rules
              </li>
              <li>
                <strong className="text-slate-800 dark:text-slate-100">Universal</strong> → Universal rules only
              </li>
            </ul>
            <p>
              Each tab has its own conversation transcript stored in your
              browser. Switching tabs loads a different transcript;
              clearing browser storage starts fresh. Proposals you
              approve are tagged with the scope you were in when they
              were generated.
            </p>
          </div>
          <DialogFooter>
            <Button size="sm" onClick={() => setScopeInfoOpen(false)}>
              Got it
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/**
 * Collapsible live reasoning trace. Renders the model's extended-thinking
 * stream in a muted, monospace-ish panel that reads as "the AI is working"
 * without competing with the answer bubble. Auto-expanded while streaming
 * so the user sees motion; they can collapse it if it gets noisy.
 */
function ThinkingTrace({ text }: { text: string }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="mx-2 rounded-lg border border-slate-200/70 bg-slate-50/60 dark:border-white/8 dark:bg-white/[0.02]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <svg className="h-3.5 w-3.5 animate-pulse text-[var(--color-brand-primary)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
        </svg>
        <span className="text-[11px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
          Reasoning
        </span>
        <svg
          className={"ml-auto h-3 w-3 text-slate-400 transition-transform " + (open ? "rotate-180" : "")}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <div className="max-h-40 overflow-y-auto border-t border-slate-200/60 px-3 py-2 dark:border-white/8">
          <p className="whitespace-pre-wrap text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
            {text}
          </p>
        </div>
      )}
    </div>
  );
}

function severityClass(sev?: string) {
  switch (sev) {
    case "critical":
      return "bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-300";
    case "high":
      return "bg-orange-50 text-orange-700 dark:bg-orange-500/10 dark:text-orange-300";
    case "medium":
      return "bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300";
    case "low":
      return "bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300";
    default:
      return "bg-slate-100 text-slate-600 dark:bg-white/8 dark:text-slate-300";
  }
}

function KindBadge({ kind }: { kind: "add" | "edit" | "delete" }) {
  const cfg = {
    add:    { label: "+ New",    cls: "bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300" },
    edit:   { label: "~ Edit",   cls: "bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-300" },
    delete: { label: "× Delete", cls: "bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-300" },
  }[kind];
  return (
    <span className={"inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-normal " + cfg.cls}>
      {cfg.label}
    </span>
  );
}

const HOSTING_SCOPE_BADGE: Record<HostingScope, string> = {
  on_prem: "On-prem",
  cloud: "Cloud",
  both: "Universal",
};

function tierLabel(tiers?: number[]): string {
  if (!tiers || tiers.length === 0) return "T1-5";
  const sorted = [...tiers].sort((a, b) => a - b);
  if (sorted.length === 5 && sorted[0] === 1 && sorted[4] === 5) return "T1-5";
  return "T" + sorted.join(",");
}

function RuleSummary({
  rule,
  variant,
}: {
  rule: Pick<
    StandardItem,
    | "name"
    | "description"
    | "severity"
    | "category"
    | "validation_criteria"
    | "hosting_scope"
    | "applies_to_tiers"
    | "layer"
  >;
  variant: "current" | "proposed";
}) {
  return (
    <div
      className={
        "rounded-lg border p-3 " +
        (variant === "current"
          ? "border-slate-200/70 bg-slate-50/60 dark:border-white/8 dark:bg-white/[0.03]"
          : "border-sky-200/80 bg-sky-50/40 dark:border-sky-500/20 dark:bg-sky-500/[0.06]")
      }
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span className={"rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-normal " + severityClass(rule.severity)}>
          {rule.severity}
        </span>
        {rule.category && (
          <span className="text-[10px] text-slate-500 dark:text-slate-400">{rule.category}</span>
        )}
        {rule.hosting_scope && (
          <span className="rounded-full bg-violet-50 px-2 py-0.5 text-[10px] font-medium text-violet-700 dark:bg-violet-500/10 dark:text-violet-300">
            {HOSTING_SCOPE_BADGE[rule.hosting_scope]}
          </span>
        )}
        {rule.layer && (
          <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-500/10 dark:text-amber-300">
            {rule.layer}
          </span>
        )}
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-white/8 dark:text-slate-300">
          {tierLabel(rule.applies_to_tiers)}
        </span>
      </div>
      <p className="mt-1.5 text-sm font-medium text-slate-900 dark:text-slate-100">{rule.name}</p>
      <p className="mt-0.5 text-xs leading-relaxed text-slate-600 dark:text-slate-400">{rule.description}</p>
      <p className="mt-1.5 text-[11px] italic leading-relaxed text-slate-500 dark:text-slate-400">{rule.validation_criteria}</p>
    </div>
  );
}

function ProposalCard({
  proposal,
  target,
  acting,
  onApprove,
  onReject,
  selectable = false,
  selected = false,
  onToggleSelect,
}: {
  proposal: StandardItem;
  target?: StandardItem;
  acting: boolean;
  onApprove: () => void;
  onReject: () => void;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
}) {
  const kind = (proposal.proposal_kind || "add") as "add" | "edit" | "delete";

  return (
    <li
      className={
        "rounded-xl border bg-white p-3.5 transition-colors dark:bg-white/[0.02] " +
        (selected
          ? "border-[var(--color-brand-primary)]/60 ring-1 ring-[var(--color-brand-primary)]/30 dark:border-[var(--color-brand-primary)]/50"
          : "border-slate-200/80 hover:border-slate-300/80 dark:border-white/8 dark:hover:border-white/12")
      }
    >
      {/* Header: optional select checkbox + kind + id */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {selectable && (
            <input
              type="checkbox"
              checked={selected}
              onChange={onToggleSelect}
              aria-label={`Select proposal ${proposal.id}`}
              className="h-4 w-4 shrink-0 cursor-pointer rounded border-slate-300 text-[var(--color-brand-primary)] focus:ring-2 focus:ring-[var(--color-brand-primary)]/30 dark:border-white/20 dark:bg-white/5"
            />
          )}
          <KindBadge kind={kind} />
          <span className="font-mono text-[11px] text-slate-400 dark:text-slate-500">
            {kind === "add" ? proposal.id : (proposal.target_id || proposal.id)}
          </span>
        </div>
      </div>

      {proposal.rationale && (
        <p className="mt-2 text-xs italic leading-relaxed text-slate-500 dark:text-slate-400">
          <span className="font-semibold not-italic text-slate-700 dark:text-slate-200">Why:</span>{" "}
          {proposal.rationale}
        </p>
      )}

      <div className="mt-3">
        {kind === "add" && (
          <RuleSummary rule={proposal} variant="proposed" />
        )}

        {kind === "edit" && (
          <div className="space-y-2">
            {target ? (
              <>
                <p className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Current</p>
                <RuleSummary rule={target} variant="current" />
                <p className="text-[10px] font-semibold uppercase tracking-normal text-sky-700 dark:text-sky-300">Proposed</p>
                <RuleSummary rule={proposal} variant="proposed" />
              </>
            ) : (
              <p className="text-xs text-rose-700 dark:text-rose-300">
                Target rule {proposal.target_id} no longer exists. Reject this proposal.
              </p>
            )}
          </div>
        )}

        {kind === "delete" && (
          <div>
            {target ? (
              <>
                <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-normal text-rose-700 dark:text-rose-300">
                  Will be archived
                </p>
                <RuleSummary rule={target} variant="current" />
              </>
            ) : (
              <p className="text-xs text-rose-700 dark:text-rose-300">
                Target rule {proposal.target_id} no longer exists. Reject this proposal.
              </p>
            )}
          </div>
        )}
      </div>

      <div className="mt-3.5 flex gap-2">
        <button
          onClick={onApprove}
          disabled={acting || ((kind === "edit" || kind === "delete") && !target)}
          className="flex-1 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm shadow-emerald-600/20 transition-all hover:bg-emerald-700 hover:shadow-md hover:shadow-emerald-600/25 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-emerald-600"
        >
          Approve
        </button>
        <button
          onClick={onReject}
          disabled={acting}
          className="flex-1 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition-colors hover:border-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
        >
          Reject
        </button>
      </div>
    </li>
  );
}
