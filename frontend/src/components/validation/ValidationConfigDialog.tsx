// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  RuleSuggestion,
  StandardItem,
  TransientStandard,
} from "@/lib/api";
import { useRole } from "@/lib/use-role";

type Mode = "curated" | "manual";

const CATEGORY_LABEL_OVERRIDES: Record<string, string> = {
  data: "Data Protection",
  traffic_management: "Traffic Management",
  load_balancer: "Load Balancer",
};

/** Title-case any category string. Mirrors the helper on the validation
 *  detail page so the dropdown reads like the rest of the UI instead of
 *  showing raw lowercase keys like "general" or "replication". */
function prettyCategory(raw: string): string {
  if (!raw) return "Other";
  const key = raw.toLowerCase();
  if (CATEGORY_LABEL_OVERRIDES[key]) return CATEGORY_LABEL_OVERRIDES[key];
  return key
    .split(/[_\s-]+/)
    .map((w) => (w.length === 0 ? "" : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  documentId: string;
  /** Called once a validation has been kicked off. Receives the validation id. */
  onValidationStarted: (validationId: string) => void;
}

/**
 * Right-side drawer for configuring a validation run.
 *
 * Two modes:
 *
 *   AI-curated  — describe what you want in free text, pick how many
 *                 existing rules to use, hit "Get suggestions". The
 *                 agent returns ranked existing rule ids AND brand-new
 *                 rule suggestions. The user can include each new rule
 *                 in this validation only (transient) or save it to
 *                 the library (active).
 *
 *   Manual      — search/filter the full library and check rules
 *                 individually.
 *
 * Both modes share the bottom action bar that says "Run validation on
 * N standards (M from library + K new)".
 */
export function ValidationConfigDialog({
  open,
  onOpenChange,
  documentId,
  onValidationStarted,
}: Props) {
  const [standards, setStandards] = useState<StandardItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  // Document's declared tier — drives the dimming of out-of-tier rules.
  // Null means we either haven't fetched yet or the doc has no tier set.
  const [documentTier, setDocumentTier] = useState<number | null>(null);
  // Tier override. Null = use the doc's declared tier.
  // Setting this changes which (tier × layer) rules apply for this run
  // and is persisted on the validation so the rollup matches.
  const [tierOverride, setTierOverride] = useState<number | null>(null);
  // Force a fresh run even if an identical validation is cached. Lets a user
  // re-run the same document + rules (e.g. after fixing the doc) and get a
  // new result row instead of the cached one.
  const [forceRerun, setForceRerun] = useState(false);

  const [mode, setMode] = useState<Mode>("curated");
  const [topN, setTopN] = useState<number>(20);
  const [userPrompt, setUserPrompt] = useState<string>("");
  const [curating, setCurating] = useState(false);
  // Writing a suggested rule into the shared library needs curator rights,
  // which a validator running this dialog does not have.
  const { canCurate } = useRole();

  /** AI-suggested NEW rules with ephemeral client ids so users can
   *  toggle inclusion / save without confusing them with library rules. */
  const [suggestions, setSuggestions] = useState<
    Array<RuleSuggestion & { tempId: string; included: boolean; savedId?: string }>
  >([]);
  const [savingId, setSavingId] = useState<string | null>(null);

  // Load standards every time the drawer opens — they may have changed.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const kickoff = window.setTimeout(() => {
      setLoading(true);
      setError(null);
      setSuggestions([]);
      setMode("curated");
      setUserPrompt("");
      setTierOverride(null);
      // Reset the force-rerun opt-in each time the dialog opens, so a
      // previous tick doesn't silently force fresh (uncached) runs later.
      setForceRerun(false);
      void Promise.all([
        api.listStandards(),
        api.getDocument(documentId).catch(() => null),
      ])
        .then(([res, doc]) => {
          if (cancelled) return;
          setStandards(res.standards);
          setSelected(new Set(res.standards.map((s) => s.id)));
          setDocumentTier(doc?.declared_tier ?? null);
        })
        .catch((err) => {
          if (!cancelled) {
            setError(
              err instanceof Error ? err.message : "Failed to load standards",
            );
          }
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(kickoff);
    };
  }, [open, documentId]);

  /** Effective tier for filtering — override takes precedence so the
   *  preview matches what the validator will actually run. */
  const effectiveTier = tierOverride ?? documentTier;

  /** Does this rule apply to the effective tier?
   *  Universal rules (no applies_to_tiers, or all five) always apply.
   *  Returns true when effectiveTier is null (no tier filter active). */
  const ruleApplies = (s: StandardItem): boolean => {
    if (effectiveTier == null) return true;
    const tiers = s.applies_to_tiers;
    if (!tiers || tiers.length === 0 || tiers.length === 5) return true;
    return tiers.includes(effectiveTier);
  };

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onOpenChange(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const s of standards) if (s.category) set.add(s.category);
    return ["all", ...Array.from(set).sort()];
  }, [standards]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return standards.filter((s) => {
      if (categoryFilter !== "all" && s.category !== categoryFilter) return false;
      if (!q) return true;
      return (
        s.id.toLowerCase().includes(q) ||
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q)
      );
    });
  }, [standards, search, categoryFilter]);

  const allFilteredSelected =
    filtered.length > 0 && filtered.every((s) => selected.has(s.id));

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleFiltered = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allFilteredSelected) for (const s of filtered) next.delete(s.id);
      else for (const s of filtered) next.add(s.id);
      return next;
    });
  };

  const includedSuggestions = suggestions.filter((s) => s.included);

  // ────────────────────────────────────────────────────────────────────
  // AI curation
  // ────────────────────────────────────────────────────────────────────
  const handleCurate = async () => {
    setCurating(true);
    setError(null);
    try {
      const res = await api.curateStandards(documentId, {
        topN,
        userPrompt: userPrompt || undefined,
      });
      // Replace selection with the curated existing-id list so the
      // footer count matches "what the AI picked".
      setSelected(new Set(res.existing_standard_ids));
      // Stamp suggestions with client-side temp ids and default-include them.
      setSuggestions(
        res.new_suggestions.map((s, i) => ({
          ...s,
          tempId: `tmp-${Date.now()}-${i}`,
          included: true,
        })),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to curate standards");
    } finally {
      setCurating(false);
    }
  };

  const toggleSuggestion = (tempId: string) => {
    setSuggestions((prev) =>
      prev.map((s) => (s.tempId === tempId ? { ...s, included: !s.included } : s)),
    );
  };

  /** Save an AI-suggested rule to the library as an active standard.
   *  The user has already inspected it in context — that's their approval.
   *  We mark the suggestion with the saved id and disable the button. */
  const saveSuggestion = async (tempId: string) => {
    const sug = suggestions.find((s) => s.tempId === tempId);
    if (!sug || sug.savedId) return;
    setSavingId(tempId);
    try {
      // CreateStandardData rejects "info" — collapse it to "low" since
      // the library only carries the four scoring tiers.
      const severity =
        sug.severity === "info" ? "low" : sug.severity;
      const saved = await api.createStandard({
        name: sug.name,
        description: sug.description,
        severity,
        category: sug.category,
        validation_criteria: sug.validation_criteria,
        // Suggestions without an explicit scope are universal.
        hosting_scope: sug.hosting_scope ?? "both",
      });
      // Add it to the live standards list and select it; drop the suggestion
      // (it now exists in the library, no longer "new").
      setStandards((prev) => [saved, ...prev]);
      setSelected((prev) => new Set(prev).add(saved.id));
      setSuggestions((prev) => prev.filter((s) => s.tempId !== tempId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save rule");
    } finally {
      setSavingId(null);
    }
  };

  // ────────────────────────────────────────────────────────────────────
  // Submit
  // ────────────────────────────────────────────────────────────────────
  const handleStart = async () => {
    const totalCount = selected.size + includedSuggestions.length;
    if (totalCount === 0) {
      setError("Select at least one standard.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      // If everything in the library is selected and no transients,
      // omit standard_ids so the cache key matches a prior full-set run.
      const useAllLibrary =
        selected.size === standards.length && includedSuggestions.length === 0;

      const transients: TransientStandard[] = includedSuggestions.map((s) => ({
        name: s.name,
        description: s.description,
        severity: s.severity,
        category: s.category,
        validation_criteria: s.validation_criteria,
      }));

      const result = await api.triggerValidation(documentId, "default", {
        standardIds: useAllLibrary ? undefined : Array.from(selected),
        transientStandards: transients.length > 0 ? transients : undefined,
        tierOverride: tierOverride ?? undefined,
        force: forceRerun || undefined,
      });
      onValidationStarted(result.validation_id);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start validation");
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  const totalCount = selected.size + includedSuggestions.length;

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Backdrop */}
      <button
        aria-label="Close"
        className="flex-1 bg-black/40"
        onClick={() => onOpenChange(false)}
      />

      {/* Drawer */}
      <aside className="flex h-full w-full max-w-2xl flex-col border-l border-border bg-background shadow-xl animate-in slide-in-from-right duration-200">
        {/* Sticky header */}
        <div className="flex items-start justify-between border-b border-border px-6 py-4 shrink-0">
          <div>
            <h3 className="text-base font-semibold text-[var(--color-ink)]">
              Configure validation
            </h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Pick standards to validate this document against. Press Esc or click
              outside to close.
            </p>
          </div>
          <button
            onClick={() => onOpenChange(false)}
            aria-label="Close"
            className="rounded-md p-1 text-muted-foreground hover:bg-muted/50"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 6l12 12M6 18L18 6" />
            </svg>
          </button>
        </div>

        {/* Tabs */}
        <div className="border-b border-border px-6 pt-3 shrink-0">
          <div className="inline-flex gap-1 rounded-md border border-border p-1 text-xs">
            <button
              onClick={() => setMode("curated")}
              className={
                "rounded px-3 py-1 font-medium " +
                (mode === "curated"
                  ? "bg-[var(--color-brand-primary)] text-white"
                  : "text-muted-foreground hover:bg-muted/50")
              }
            >
              AI-curated
            </button>
            <button
              onClick={() => setMode("manual")}
              className={
                "rounded px-3 py-1 font-medium " +
                (mode === "manual"
                  ? "bg-[var(--color-brand-primary)] text-white"
                  : "text-muted-foreground hover:bg-muted/50")
              }
            >
              Choose specific rules
            </button>
          </div>
        </div>

        {/* Tier picker. Defaults to the doc's declared tier;
            user can override to validate as a different tier without
            changing the document. */}
        <div className="border-b border-border px-6 py-3 shrink-0">
          <div className="flex items-center gap-3">
            <label className="text-xs font-medium text-muted-foreground">
              Validate as tier
            </label>
            <div className="inline-flex gap-1 rounded-md border border-border p-0.5 text-xs">
              {[1, 2, 3, 4, 5].map((t) => {
                const active = (tierOverride ?? documentTier) === t;
                return (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setTierOverride(t)}
                    className={
                      "rounded px-2.5 py-1 font-mono font-medium transition-colors " +
                      (active
                        ? "bg-[var(--color-brand-primary)] text-white"
                        : "text-muted-foreground hover:bg-muted/50")
                    }
                  >
                    T{t}
                  </button>
                );
              })}
            </div>
            {documentTier != null && tierOverride != null && tierOverride !== documentTier && (
              <button
                type="button"
                onClick={() => setTierOverride(null)}
                className="text-[11px] text-[var(--color-brand-primary)] hover:underline"
              >
                Reset to declared (T{documentTier})
              </button>
            )}
            <span className="ml-auto text-[11px] text-muted-foreground">
              {tierOverride != null && tierOverride !== documentTier
                ? `Override — doc declared T${documentTier ?? "?"}`
                : documentTier != null
                ? `Document declared T${documentTier}`
                : "Document tier unknown"}
            </span>
          </div>
        </div>

        {/* Scrollable body */}
        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-4">
          {mode === "curated" ? (
            <div className="space-y-4">
              <div>
                <label className="text-xs font-medium text-muted-foreground">
                  What should the AI focus on for this validation? (optional)
                </label>
                <textarea
                  value={userPrompt}
                  onChange={(e) => setUserPrompt(e.target.value)}
                  onKeyDown={(e) => {
                    // Cmd/Ctrl+Enter submits — the same action as
                    // clicking the button below. Plain Enter still
                    // inserts a newline because this is a multi-line
                    // textarea and people often want a list.
                    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                      e.preventDefault();
                      if (!curating && !loading && standards.length > 0) {
                        handleCurate();
                      }
                    }
                  }}
                  placeholder='e.g. "I care most about RTO targets and failover automation for Tier-1 systems"'
                  rows={3}
                  // Locked while the AI is thinking — typing a
                  // different prompt mid-call would mislead the user
                  // because the result still belongs to the original
                  // prompt. Cursor changes to not-allowed.
                  disabled={curating}
                  className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/40 disabled:cursor-not-allowed disabled:opacity-60"
                />
                <p className="mt-1 text-[11px] text-muted-foreground">
                  Steers two things: which library rules the AI picks first, and
                  what additional rules it proposes if your library is missing
                  anything important. <kbd className="rounded border border-border px-1 py-0.5 font-mono text-[10px]">⌘</kbd>
                  <kbd className="ml-0.5 rounded border border-border px-1 py-0.5 font-mono text-[10px]">↵</kbd> to submit.
                </p>
              </div>

              <div className="flex flex-wrap items-end gap-3">
                <label className="flex flex-col text-xs text-muted-foreground">
                  Library rules to pick from
                  <input
                    type="number"
                    min={1}
                    max={Math.max(1, standards.length)}
                    value={topN}
                    onChange={(e) =>
                      setTopN(
                        Math.max(
                          1,
                          Math.min(standards.length || 200, parseInt(e.target.value) || 1),
                        ),
                      )
                    }
                    disabled={curating}
                    className="mt-1 w-24 rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/40 disabled:cursor-not-allowed disabled:opacity-60"
                  />
                </label>
                <div className="flex gap-1.5">
                  {[10, 20, 50].map((n) => (
                    <button
                      key={n}
                      onClick={() => setTopN(n)}
                      disabled={curating}
                      className={
                        "rounded-md border px-2.5 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-60 " +
                        (topN === n
                          ? "border-[var(--color-brand-primary)] bg-[var(--color-brand-primary)]/10 text-[var(--color-brand-primary)]"
                          : "border-border hover:bg-muted/50")
                      }
                    >
                      Top {n}
                    </button>
                  ))}
                </div>
                <button
                  onClick={handleCurate}
                  disabled={curating || loading || standards.length === 0}
                  className="ml-auto inline-flex items-center gap-1.5 rounded-md bg-[var(--color-brand-primary)] px-4 py-1.5 text-sm font-medium text-white shadow-sm shadow-[var(--color-brand-primary)]/20 transition-all hover:bg-[var(--color-brand-primary-hover)] hover:shadow-md hover:shadow-[var(--color-brand-primary)]/25 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-[var(--color-brand-primary)]"
                >
                  {curating ? (
                    <>
                      <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                      </svg>
                      Asking AI…
                    </>
                  ) : userPrompt.trim() ? (
                    <>
                      <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M5 12h14M13 5l7 7-7 7" />
                      </svg>
                      Submit prompt
                    </>
                  ) : (
                    <>
                      <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                      </svg>
                      Auto-curate
                    </>
                  )}
                </button>
              </div>

              {/* Existing rule picks */}
              {selected.size > 0 && (
                <div className="space-y-2">
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-normal text-muted-foreground">
                      From your library · {selected.size} selected
                    </p>
                    <p className="text-[11px] text-muted-foreground/80">
                      Existing rules the AI thinks apply to this document.
                    </p>
                  </div>
                  <ul className="divide-y divide-border rounded-md border border-border">
                    {Array.from(selected).map((id) => {
                      const s = standards.find((x) => x.id === id);
                      if (!s) return null;
                      return (
                        <li key={s.id} className="flex items-start gap-3 px-3 py-2">
                          <input
                            type="checkbox"
                            checked={true}
                            onChange={() => toggle(s.id)}
                            className="mt-0.5 h-4 w-4 accent-[var(--color-brand-primary)]"
                          />
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <span className="text-[11px] font-mono text-muted-foreground">{s.id}</span>
                              <SeverityBadge severity={s.severity} />
                              {s.category && (
                                <span className="text-[10px] text-muted-foreground">{prettyCategory(s.category)}</span>
                              )}
                            </div>
                            <p className="mt-0.5 text-sm font-medium text-[var(--color-ink)] truncate">
                              {s.name}
                            </p>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}

              {/* New AI suggestions */}
              {suggestions.length > 0 && (
                <div className="space-y-2">
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-normal text-[var(--color-status-info)]">
                      New rule suggestions · {includedSuggestions.length} of {suggestions.length} included
                    </p>
                    <p className="text-[11px] text-muted-foreground/80">
                      Rules the AI thinks are missing from your library, based on the document
                      {userPrompt.trim() && " and your prompt"}. Toggle inclusion for this run, or save to library to keep them.
                    </p>
                  </div>
                  <ul className="space-y-2">
                    {suggestions.map((s) => (
                      <li
                        key={s.tempId}
                        className="rounded-md border border-[var(--color-status-info)]/30 bg-[var(--color-status-info)]/5 p-3"
                      >
                        <div className="flex items-start gap-3">
                          <input
                            type="checkbox"
                            checked={s.included}
                            onChange={() => toggleSuggestion(s.tempId)}
                            className="mt-0.5 h-4 w-4 accent-[var(--color-brand-primary)]"
                          />
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <span className="rounded bg-[var(--color-status-info)]/10 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-normal text-[var(--color-status-info)]">
                                AI suggestion
                              </span>
                              <SeverityBadge severity={s.severity} />
                              <span className="text-[10px] text-muted-foreground">{prettyCategory(s.category)}</span>
                            </div>
                            <p className="mt-1 text-sm font-medium text-[var(--color-ink)]">{s.name}</p>
                            <p className="mt-0.5 text-xs text-muted-foreground">{s.description}</p>
                            <p className="mt-1.5 text-[11px] italic text-muted-foreground">
                              {s.validation_criteria}
                            </p>
                            {s.rationale && (
                              <p className="mt-1.5 text-[11px] text-[var(--color-status-info)]">
                                <span className="font-semibold">Why: </span>{s.rationale}
                              </p>
                            )}
                          </div>
                        </div>

                        <div className="mt-2.5 flex flex-wrap items-center gap-2 pl-7">
                          <span className="text-[11px] text-muted-foreground">
                            {s.included ? "Will run in this validation" : "Not included"}
                          </span>
                          {/* Saving to the shared library is a curator action
                              (POST /api/standards). A validator can still run
                              the suggestion in this validation — offering the
                              button to them only produced a 403. */}
                          {canCurate && (
                            <button
                              onClick={() => saveSuggestion(s.tempId)}
                              disabled={savingId === s.tempId}
                              className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium hover:bg-muted/50 disabled:opacity-50"
                            >
                              {savingId === s.tempId ? "Saving…" : "Save to library"}
                            </button>
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {selected.size === 0 && suggestions.length === 0 && !curating && (
                <div className="rounded-md border border-dashed border-border px-4 py-6 text-center text-xs text-muted-foreground">
                  <p>
                    Click{" "}
                    <strong className="text-[var(--color-ink)]">
                      {userPrompt.trim() ? "Submit prompt" : "Auto-curate"}
                    </strong>{" "}
                    above. The AI will:
                  </p>
                  <ul className="mt-2 inline-block text-left">
                    <li>• Pick the most relevant <strong>existing</strong> rules from your library</li>
                    <li>• Propose <strong>new</strong> rules if it spots gaps in coverage</li>
                  </ul>
                </div>
              )}
            </div>
          ) : (
            // Manual mode
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  placeholder="Search by ID, name, or description"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="flex-1 rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/40"
                />
                <select
                  value={categoryFilter}
                  onChange={(e) => setCategoryFilter(e.target.value)}
                  className="rounded-md border border-border bg-background px-2 py-1.5 text-sm focus:outline-none"
                >
                  {categories.map((c) => (
                    <option key={c} value={c}>
                      {c === "all" ? "All categories" : prettyCategory(c)}
                    </option>
                  ))}
                </select>
              </div>

              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>
                  {selected.size} of {standards.length} selected
                  {filtered.length !== standards.length && ` · ${filtered.length} match filter`}
                </span>
                <button
                  onClick={toggleFiltered}
                  disabled={loading || filtered.length === 0}
                  className="text-[var(--color-brand-primary)] hover:underline disabled:opacity-40"
                >
                  {allFilteredSelected ? "Deselect filtered" : "Select filtered"}
                </button>
              </div>

              <div className="rounded-md border border-border">
                {loading ? (
                  <div className="p-6 text-center text-sm text-muted-foreground">
                    Loading standards…
                  </div>
                ) : filtered.length === 0 ? (
                  <div className="p-6 text-center text-sm text-muted-foreground">
                    No standards match the filter.
                  </div>
                ) : (
                  <ul className="divide-y divide-border">
                    {filtered.map((s) => {
                      const isSelected = selected.has(s.id);
                      const applies = ruleApplies(s);
                      const tiers = s.applies_to_tiers;
                      const tierLabel =
                        !tiers || tiers.length === 5
                          ? "All tiers"
                          : tiers.length === 1
                          ? `Tier ${tiers[0]}`
                          : `Tiers ${[...tiers].sort((a, b) => a - b).join(", ")}`;
                      return (
                        <li key={s.id}>
                          <label
                            className={
                              "flex cursor-pointer items-start gap-3 px-4 py-3 transition-colors hover:bg-muted/40 " +
                              (applies ? "" : "opacity-50")
                            }
                            title={applies ? "" : `This rule does not apply to Tier-${effectiveTier} systems.`}
                          >
                            <input
                              type="checkbox"
                              checked={isSelected}
                              onChange={() => toggle(s.id)}
                              className="mt-0.5 h-4 w-4 accent-[var(--color-brand-primary)]"
                            />
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center gap-2">
                                <span className="text-xs font-mono text-muted-foreground">{s.id}</span>
                                <SeverityBadge severity={s.severity} />
                                {s.category && (
                                  <span className="text-[10px] text-muted-foreground">{prettyCategory(s.category)}</span>
                                )}
                                <span
                                  className={
                                    "rounded-full border px-1.5 py-0.5 font-mono text-[10px] " +
                                    (!tiers || tiers.length === 5
                                      ? "border-slate-200 bg-slate-50 text-slate-500 dark:border-white/10 dark:bg-white/5 dark:text-slate-400"
                                      : "border-[var(--color-brand-primary)]/20 bg-[var(--color-brand-primary)]/[0.08] text-[var(--color-brand-primary)] dark:border-[var(--color-brand-primary)]/30 dark:bg-[var(--color-brand-primary)]/15 dark:text-[#5eead4]")
                                  }
                                >
                                  {tierLabel}
                                </span>
                              </div>
                              <p className="mt-0.5 text-sm font-medium text-[var(--color-ink)] truncate">
                                {s.name}
                              </p>
                              <p className="mt-0.5 text-xs text-muted-foreground line-clamp-2">
                                {s.description}
                              </p>
                              {!applies && effectiveTier != null && (
                                <p className="mt-1 text-[10px] italic text-slate-500 dark:text-slate-400">
                                  Won&apos;t run — out of scope for Tier-{effectiveTier}.
                                </p>
                              )}
                            </div>
                          </label>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>
            </div>
          )}
        </div>

        {error && (
          <div className="border-t border-border bg-[var(--color-status-fail)]/5 px-6 py-2 text-xs text-[var(--color-status-fail)] shrink-0">
            {error}
          </div>
        )}

        {/* Sticky footer */}
        <div className="flex items-center justify-between gap-3 border-t border-border bg-background px-6 py-3 shrink-0">
          <p className="text-xs text-muted-foreground">
            <span className="font-semibold">{totalCount}</span>
            {" total"}
            {includedSuggestions.length > 0 &&
              ` · ${selected.size} library + ${includedSuggestions.length} new`}
            {effectiveTier != null && (() => {
              // Library rules currently selected that actually apply
              // to the effective tier. Out-of-scope rules will be
              // silently filtered server-side; the footer surfaces
              // that count up-front so users aren't surprised.
              const applyingLibrary = standards.filter(
                (s) => selected.has(s.id) && ruleApplies(s),
              ).length;
              const skipped = selected.size - applyingLibrary;
              if (skipped <= 0) return null;
              return (
                <>
                  {" · "}
                  <span className="text-[var(--color-brand-primary)]">
                    {applyingLibrary} apply to Tier-{effectiveTier} ({skipped} skipped)
                  </span>
                </>
              );
            })()}
          </p>
          <div className="flex items-center gap-3">
            <label
              className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground select-none"
              title="Run a fresh validation even if an identical one is cached. Use to re-validate after updating the document."
            >
              <input
                type="checkbox"
                checked={forceRerun}
                onChange={(e) => setForceRerun(e.target.checked)}
                disabled={submitting}
              />
              Force re-run
            </label>
            <button
              onClick={() => onOpenChange(false)}
              className="rounded-md border border-border px-4 py-1.5 text-sm font-medium hover:bg-muted/50"
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              onClick={handleStart}
              disabled={submitting || loading || totalCount === 0}
              className="rounded-md bg-[var(--color-brand-primary)] px-4 py-1.5 text-sm font-medium text-white hover:bg-[var(--color-brand-primary-hover)] disabled:opacity-50"
            >
              {submitting ? "Starting…" : `Run validation on ${totalCount} standard${totalCount === 1 ? "" : "s"}`}
            </button>
          </div>
        </div>
      </aside>
    </div>
  );
}

function SeverityBadge({ severity }: { severity: string }) {
  const cls =
    severity === "critical"
      ? "bg-[var(--color-severity-critical)]/10 text-[var(--color-severity-critical)]"
      : severity === "high"
        ? "bg-[var(--color-severity-high)]/10 text-[var(--color-severity-high)]"
        : severity === "medium"
          ? "bg-[var(--color-severity-medium)]/10 text-[var(--color-severity-medium)]"
          : "bg-muted text-muted-foreground";
  return (
    <span className={"rounded px-1.5 py-0.5 text-[10px] font-medium uppercase " + cls}>
      {severity}
    </span>
  );
}
