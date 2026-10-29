// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import React, { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  api,
  StandardItem,
  CreateStandardData,
  LAYER_VALUES,
  RULE_TYPE_VALUES,
  RuleLayer,
  RuleType,
  HostingScope,
} from "@/lib/api";
import { PageHero } from "@/components/layout/PageHero";
import { ExtractionStatusBanner } from "@/components/extraction/ExtractionStatusBanner";
import { useRole } from "@/lib/use-role";
import Link from "next/link";
import {
  FileTextIcon,
  ListTree,
  LoaderCircle,
  PencilIcon,
  PlusIcon,
  SearchIcon,
  SparklesIcon,
  TrashIcon,
} from "lucide-react";

const severityConfig: Record<string, { label: string; className: string }> = {
  critical: { label: "Critical", className: "bg-[var(--color-severity-critical)]/10 text-[var(--color-severity-critical)] border-[var(--color-severity-critical)]/20" },
  high: { label: "High", className: "bg-[var(--color-severity-high)]/10 text-[var(--color-severity-high)] border-[var(--color-severity-high)]/20" },
  medium: { label: "Medium", className: "bg-[var(--color-severity-medium)]/10 text-[var(--color-severity-medium)] border-[var(--color-severity-medium)]/20" },
  low: { label: "Low", className: "bg-[var(--color-severity-low)]/10 text-[var(--color-severity-low)] border-[var(--color-severity-low)]/20" },
};

const categoryLabels: Record<string, string> = {
  redundancy: "Redundancy",
  availability: "Availability",
  data: "Data Protection",
  traffic_management: "Traffic Management",
  scalability: "Scalability",
  recovery: "Recovery",
  observability: "Observability",
  failover: "Failover",
  resilience: "Resilience",
  security: "Security",
  network: "Network",
};

const categoryOptions = Object.entries(categoryLabels).map(([value, label]) => ({
  value,
  label,
}));

const severityOptions = [
  { value: "critical", label: "Critical" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

// Display labels for the fixed layer + rule_type vocabularies. Driven
// by the const tuples in lib/api.ts so adding a new layer there
// automatically requires a label here (TS will error on the
// Record<RuleLayer, string> until you fill it in).
const layerLabels: Record<RuleLayer, string> = {
  datacenter: "Datacenter",
  data: "Data",
  web: "Web",
  application: "Application",
  load_balancer: "Load Balancer",
  general: "General",
};

const ruleTypeLabels: Record<RuleType, string> = {
  founding: "Founding",
  substitution: "Substitution",
  exception: "Exception",
};

// Display labels for the hosting_scope tag.
// Universal = "both"; in segmented-control / pill copy we lean on
// "Universal" because it reads as "applies to either scope" rather
// than the implementation-flavored "both".
const hostingScopeLabels: Record<HostingScope, string> = {
  on_prem: "On-prem",
  cloud: "Cloud",
  both: "Universal",
};

// Segmented-control view key. Adds an "all" pseudo-value on top of
// the three real hosting_scope values; "all" is unfiltered.
type HostingScopeView = HostingScope | "all";

const categoryIcons: Record<string, React.ReactNode> = {
  redundancy: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>,
  availability: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>,
  data: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4" /></svg>,
  traffic_management: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>,
  scalability: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" /></svg>,
  recovery: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>,
  observability: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" /><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" /></svg>,
  failover: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4" /></svg>,
  resilience: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" /></svg>,
  security: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" /></svg>,
  network: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9" /></svg>,
};

const ALL_TIERS = [1, 2, 3, 4, 5];

/** Render a list of tier numbers as a compact label.
 *  - undefined / [1,2,3,4,5] → "All tiers"
 *  - [1] → "Tier 1"
 *  - [1,2,3] → "Tiers 1, 2, 3"
 *  - [2,3] (consecutive 2..3) → "Tiers 2-3" */
function formatTiers(tiers: number[] | undefined | null): string {
  if (!tiers || tiers.length === 0) return "All tiers";
  const sorted = [...tiers].sort((a, b) => a - b);
  if (sorted.length === ALL_TIERS.length && sorted.every((t, i) => t === ALL_TIERS[i])) {
    return "All tiers";
  }
  // Detect a contiguous run for the compact dash form.
  const isContiguous = sorted.length >= 2 && sorted.every((t, i) => i === 0 || t === sorted[i - 1] + 1);
  if (isContiguous) return `Tiers ${sorted[0]}–${sorted[sorted.length - 1]}`;
  return sorted.length === 1 ? `Tier ${sorted[0]}` : `Tiers ${sorted.join(", ")}`;
}

function StandardForm({
  initial,
  onSubmit,
  submitLabel,
}: {
  initial?: Partial<CreateStandardData>;
  onSubmit: (data: CreateStandardData) => void;
  submitLabel: string;
}) {
  const [name, setName] = useState(initial?.name || "");
  const [description, setDescription] = useState(initial?.description || "");
  const [severity, setSeverity] = useState<string>(initial?.severity || "medium");
  const [category, setCategory] = useState(initial?.category || "availability");
  const [validationCriteria, setValidationCriteria] = useState(
    initial?.validation_criteria || ""
  );
  // Both default to "" (no selection). The submit handler below
  // conditionally spreads them into the payload only when non-empty —
  // sending {layer: ""} would 422 against the backend Pydantic
  // Literal.
  const [layer, setLayer] = useState<string>(initial?.layer || "");
  const [ruleType, setRuleType] = useState<string>(initial?.rule_type || "");
  // New manual rules default to universal so the UI does not assume
  // an organization's hosting model.
  const [hostingScope, setHostingScope] = useState<HostingScope>(
    initial?.hosting_scope || "both",
  );

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    // OMIT WHEN EMPTY: never include an empty-string structured-rule
    // field in the payload. A user who opens an existing flat rule and
    // changes only the severity must NOT cause `{layer: ""}` to be
    // sent (it would 422). Conditional spread does the right thing.
    onSubmit({
      name,
      description,
      severity: severity as CreateStandardData["severity"],
      category,
      validation_criteria: validationCriteria,
      hosting_scope: hostingScope,
      ...(layer ? { layer: layer as RuleLayer } : {}),
      ...(ruleType ? { rule_type: ruleType as RuleType } : {}),
    });
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label className="block text-xs font-medium text-muted-foreground mb-1">
          Name
        </label>
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Multi-Region Failover"
          required
        />
      </div>
      <div>
        <label className="block text-xs font-medium text-muted-foreground mb-1">
          Description
        </label>
        <Textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Describe what this standard requires..."
          required
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-medium text-muted-foreground mb-1">
            Severity
          </label>
          <select
            className="h-9 w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 transition-colors hover:border-slate-300 focus:border-[var(--color-brand-primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/15 dark:bg-white/5 dark:text-slate-100"
            value={severity}
            onChange={(e) => setSeverity(e.target.value)}
          >
            {severityOptions.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-muted-foreground mb-1">
            Category
          </label>
          <select
            className="h-9 w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 transition-colors hover:border-slate-300 focus:border-[var(--color-brand-primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/15 dark:bg-white/5 dark:text-slate-100"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            {categoryOptions.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-medium text-muted-foreground mb-1">
            Layer <span className="text-slate-400">(optional)</span>
          </label>
          <select
            className="h-9 w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 transition-colors hover:border-slate-300 focus:border-[var(--color-brand-primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/15 dark:bg-white/5 dark:text-slate-100"
            value={layer}
            onChange={(e) => setLayer(e.target.value)}
          >
            <option value="">— None —</option>
            {LAYER_VALUES.map((v) => (
              <option key={v} value={v}>
                {layerLabels[v]}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-muted-foreground mb-1">
            Rule type <span className="text-slate-400">(optional)</span>
          </label>
          <select
            className="h-9 w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 transition-colors hover:border-slate-300 focus:border-[var(--color-brand-primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/15 dark:bg-white/5 dark:text-slate-100"
            value={ruleType}
            onChange={(e) => setRuleType(e.target.value)}
          >
            <option value="">— None —</option>
            {RULE_TYPE_VALUES.map((v) => (
              <option key={v} value={v}>
                {ruleTypeLabels[v]}
              </option>
            ))}
          </select>
        </div>
      </div>
      <p className="-mt-1 text-[11px] text-slate-500 dark:text-slate-400">
        Substitution and exception rules are auto-linked to a founding rule
        during extraction. Manual linking is not yet supported.
      </p>
      <div>
        <label className="block text-xs font-medium text-muted-foreground mb-1">
          Hosting scope
        </label>
        <select
          className="h-9 w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 transition-colors hover:border-slate-300 focus:border-[var(--color-brand-primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/15 dark:bg-white/5 dark:text-slate-100"
          value={hostingScope}
          onChange={(e) => setHostingScope(e.target.value as HostingScope)}
          required
        >
          <option value="on_prem">On-prem</option>
          <option value="cloud">Cloud</option>
          <option value="both">Universal (both)</option>
        </select>
        <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
          Where this rule applies. The validator only runs rules whose
          scope matches the SDD&apos;s deployment scope (or
          &quot;Universal&quot;).
        </p>
      </div>
      <div>
        <label className="block text-xs font-medium text-muted-foreground mb-1">
          Validation Criteria
        </label>
        <Textarea
          value={validationCriteria}
          onChange={(e) => setValidationCriteria(e.target.value)}
          placeholder="Describe what the AI should check in an SDD to verify compliance..."
          required
        />
      </div>
      <div className="flex justify-end pt-2">
        <Button type="submit">{submitLabel}</Button>
      </div>
    </form>
  );
}

export default function StandardsPage() {
  const { canCurate } = useRole();
  const [standards, setStandards] = useState<StandardItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [extractMessage, setExtractMessage] = useState<string | null>(null);
  const [extractedCount, setExtractedCount] = useState(0);
  const [editingStandard, setEditingStandard] = useState<StandardItem | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [validationInProgress, setValidationInProgress] = useState(false);
  const [pendingProposalsCount, setPendingProposalsCount] = useState(0);
  // Document-extraction review batches awaiting approval (reviewed per-doc on
  // the Documents page — distinct from curator-chat proposals above).
  const [pendingBatchCount, setPendingBatchCount] = useState(0);
  const [pendingBatchRuleCount, setPendingBatchRuleCount] = useState(0);

  // Filter state
  const [searchQuery, setSearchQuery] = useState("");
  const [severityFilter, setSeverityFilter] = useState<string | null>(null);
  const [categoryFilter, setCategoryFilter] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState<"all" | "user" | "document" | "generated">("all");
  // Tier filter — null means no filter. When set, only rules whose
  // applies_to_tiers contains this tier are shown. Universal rules
  // (no applies_to_tiers, or [1..5]) match every tier.
  const [tierFilter, setTierFilter] = useState<number | null>(null);
  // Structured-rule filters. When set, the predicate hides rules
  // whose layer / rule_type is undefined — matching the existing
  // Severity filter behaviour. Default null = no filter.
  const [layerFilter, setLayerFilter] = useState<RuleLayer | null>(null);
  const [ruleTypeFilter, setRuleTypeFilter] = useState<RuleType | null>(null);
  // Filename of the source document; null means "no filter". Only
  // applies to standards whose `source === "document"` and that
  // carry a non-empty `source_document`. Independent from sourceFilter
  // so a user can pick a specific doc without manually flipping the
  // source segmented control to "Document".
  const [sourceDocFilter, setSourceDocFilter] = useState<string | null>(null);
  // Start with the complete library instead of preferring a hosting model.
  const [hostingScopeView, setHostingScopeView] = useState<HostingScopeView>("all");

  const fetchStandards = async () => {
    try {
      const res = await api.listStandards();
      setStandards(res.standards as StandardItem[]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load standards");
    } finally {
      setLoading(false);
    }
  };

  // Refresh the rule list when standards change elsewhere (e.g. a reviewer
  // approves an extraction batch on a document-detail page), so the library
  // reflects newly-active rules without a manual refresh.
  useEffect(() => {
    const onChanged = () => { void fetchStandards(); };
    window.addEventListener("standards-changed", onChanged);
    return () => window.removeEventListener("standards-changed", onChanged);
  }, []);

  useEffect(() => {
    let cancelled = false;

    void api.listStandards()
      .then((res) => {
        if (!cancelled) setStandards(res.standards);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load standards");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    // Curator-chat proposals only (source=generated) — the "Curate with AI"
    // badge. Document-extraction proposals are reviewed per-document, counted
    // separately below.
    void api.listStandards({ status: "proposed", source: "generated" })
      .then((res) => {
        if (!cancelled) setPendingProposalsCount(res.total);
      })
      .catch(() => {});

    // Document-extraction review batches awaiting approval. Exclude batches
    // still in-flight: their `total` is the pre-dedup per-unit count (a doc that
    // restates rules across pages transiently inflates it, then it collapses to
    // the final de-duplicated number), so counting it here would flash a wrong,
    // too-high "awaiting review" figure until the run finishes finalizing.
    void api.listExtractionBatches()
      .then((res) => {
        if (!cancelled) {
          const settled = res.batches.filter((b) => !b.in_flight);
          setPendingBatchCount(settled.length);
          setPendingBatchRuleCount(
            settled.reduce((n, b) => n + b.total, 0),
          );
        }
      })
      .catch(() => {});

    void api.getExtractionStatus()
      .then((status) => {
        if (!cancelled && status.is_extracting) {
          setExtracting(true);
          setExtractedCount(status.total_extracted);
          setExtractMessage(
            `Extraction in progress... ${status.total_extracted} rules found so far`,
          );
        }
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
  }, []);

  // Check for active validations and poll every 10 seconds
  useEffect(() => {
    async function checkActiveValidations() {
      try {
        const res = await api.listValidations();
        const hasActive = res.validations.some((v) => v.status === "pending");
        setValidationInProgress(hasActive);
      } catch {
        // Silently fail - don't block the UI
      }
    }
    checkActiveValidations();
    const interval = setInterval(checkActiveValidations, 10000);
    return () => clearInterval(interval);
  }, []);

  const handleCreate = async (data: CreateStandardData) => {
    try {
      await api.createStandard(data);
      setCreateOpen(false);
      setLoading(true);
      await fetchStandards();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create standard");
    }
  };

  const handleUpdate = async (data: CreateStandardData) => {
    if (!editingStandard) return;
    try {
      await api.updateStandard(editingStandard.id, data);
      setEditingStandard(null);
      setLoading(true);
      await fetchStandards();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update standard");
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.deleteStandard(id);
      setDeleteConfirm(null);
      setLoading(true);
      await fetchStandards();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete standard");
    }
  };

  // Distinct source-document filenames that have at least one rule
  // attached, sorted alphabetically. Used to populate the new filter
  // dropdown.
  const sourceDocOptions = Array.from(
    new Set(
      standards
        .filter((s) => s.source === "document" && s.source_document)
        .map((s) => s.source_document as string),
    ),
  ).sort((a, b) => a.localeCompare(b));

  // Match a rule against the chosen hosting_scope view.
  // Universal rules (hosting_scope === "both") are visible in BOTH
  // On-prem and Cloud views (the rule applies to either scope). When
  // the user picks "Universal", we show ONLY hosting_scope === "both"
  // — that view is the inventory of cross-scope rules. "all" is
  // pass-through.
  const matchesScopeView = (s: StandardItem): boolean => {
    if (hostingScopeView === "all") return true;
    if (hostingScopeView === "both") return s.hosting_scope === "both";
    return s.hosting_scope === hostingScopeView || s.hosting_scope === "both";
  };

  // Apply filters
  const filteredStandards = standards.filter((s) => {
    if (!matchesScopeView(s)) return false;
    if (severityFilter && s.severity !== severityFilter) return false;
    if (categoryFilter && s.category !== categoryFilter) return false;
    if (sourceFilter !== "all" && s.source !== sourceFilter) return false;
    if (tierFilter !== null) {
      // Universal rules (no applies_to_tiers or full [1..5]) match
      // every tier filter; targeted rules must contain the chosen tier.
      const tiers = s.applies_to_tiers;
      if (tiers && tiers.length > 0 && !tiers.includes(tierFilter)) return false;
    }
    // Structured-rule filters: hide rules whose layer / rule_type is
    // unset when a specific value is selected. Same shape as severity.
    if (layerFilter && s.layer !== layerFilter) return false;
    if (ruleTypeFilter && s.rule_type !== ruleTypeFilter) return false;
    if (sourceDocFilter && s.source_document !== sourceDocFilter) return false;
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      return (
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q) ||
        s.validation_criteria.toLowerCase().includes(q)
      );
    }
    return true;
  });

  // Single derived predicate for "any filter is active" — used both
  // by the active-filters summary line and by the empty-state Clear
  // button. Centralising it keeps the JSX readable as more filter
  // axes get added.
  // Note: hostingScopeView is the PRIMARY divider — Clear filters does
  // NOT reset it. The user explicitly chose a scope at the top of the
  // page; clearing inner filters should bring them back to the full
  // scope, not switch them out of it.
  const hasActiveFilter =
    searchQuery !== "" ||
    severityFilter !== null ||
    categoryFilter !== null ||
    sourceFilter !== "all" ||
    sourceDocFilter !== null ||
    tierFilter !== null ||
    layerFilter !== null ||
    ruleTypeFilter !== null;

  const clearAllFilters = () => {
    setSeverityFilter(null);
    setCategoryFilter(null);
    setSourceFilter("all");
    setSourceDocFilter(null);
    setTierFilter(null);
    setLayerFilter(null);
    setRuleTypeFilter(null);
    setSearchQuery("");
  };

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="text-center">
          <LoaderCircle className="mx-auto h-6 w-6 animate-spin text-[var(--color-brand-primary)]" />
          <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">Loading standards…</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="text-center">
          <p className="text-sm font-medium text-rose-700 dark:text-rose-300">Failed to load standards</p>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{error}</p>
        </div>
      </div>
    );
  }

  const criticalCount = standards.filter((s) => s.severity === "critical").length;
  const highCount = standards.filter((s) => s.severity === "high").length;
  const mediumCount = standards.filter((s) => s.severity === "medium").length;
  const userCount = standards.filter((s) => s.source === "user").length;
  const documentCount = standards.filter((s) => s.source === "document").length;

  return (
    <div className="space-y-5 animate-in fade-in duration-300">
      <ExtractionStatusBanner />
      {/* Validation In Progress Banner */}
      {validationInProgress && (
        <div className="flex items-center gap-3 rounded-xl border border-amber-200/80 bg-amber-50 px-4 py-3 dark:border-amber-500/20 dark:bg-amber-500/10">
          <LoaderCircle className="h-4 w-4 shrink-0 animate-spin text-amber-700 dark:text-amber-300" />
          <span className="text-sm text-amber-800 dark:text-amber-200">
            Validation in progress. Standards cannot be modified until validation completes.
          </span>
        </div>
      )}

      {/* Extraction Progress Banner */}
      {extracting && (
        <div className="rounded-xl border border-[var(--color-brand-primary)]/20 bg-gradient-to-br from-[var(--color-brand-primary)]/[0.05] to-transparent px-5 py-4 dark:border-[var(--color-brand-primary)]/30 dark:from-[var(--color-brand-primary)]/[0.10]">
          <div className="mb-2 flex items-center gap-3">
            <LoaderCircle className="h-4 w-4 shrink-0 animate-spin text-[var(--color-brand-primary)]" />
            <span className="text-sm font-semibold text-[var(--color-brand-primary)]">
              AI Extraction In Progress
            </span>
          </div>
          <div className="ml-7">
            <p className="text-xs text-slate-500 dark:text-slate-400">{extractMessage}</p>
            {extractedCount > 0 && (
              <div className="mt-2.5 flex items-center gap-3">
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-200/80 dark:bg-white/10">
                  <div
                    className="h-full rounded-full bg-[var(--color-brand-primary)] transition-all duration-500"
                    style={{ width: `${Math.min((extractedCount / 30) * 100, 95)}%` }}
                  />
                </div>
                <span className="font-mono text-xs font-medium text-slate-700 dark:text-slate-200">
                  {extractedCount} rules
                </span>
              </div>
            )}
          </div>
        </div>
      )}

      <PageHero
        title="Standards"
        subtitle={`${standards.length} ${standards.length === 1 ? "rule" : "rules"} validated against every SDD · ${criticalCount} critical · ${highCount} high`}
        actions={
          <>
            <Link
              href="/standards/references"
              className="inline-flex h-7 items-center gap-1 rounded-[min(var(--radius-md),12px)] border border-slate-200 bg-white px-2.5 text-[0.8rem] font-medium text-slate-700 transition-colors hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10 dark:hover:text-white"
              title="Factual context blocks the validator attaches alongside rules"
            >
              <ListTree className="size-3.5" />
              References
            </Link>
            <Link
              href="/standards/icon-legend"
              className="inline-flex h-7 items-center gap-1 rounded-[min(var(--radius-md),12px)] border border-slate-200 bg-white px-2.5 text-[0.8rem] font-medium text-slate-700 transition-colors hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10 dark:hover:text-white"
              title="Teach extraction + validation your organisation's custom on-prem diagram icons"
            >
              <FileTextIcon className="size-3.5" />
              Icon Legend
            </Link>
            {canCurate && (
              <Link
                href="/standards/curate"
                aria-disabled={validationInProgress}
                className={
                  "inline-flex h-7 items-center gap-1 rounded-[min(var(--radius-md),12px)] border border-slate-200 bg-white px-2.5 text-[0.8rem] font-medium text-slate-700 transition-colors hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10 dark:hover:text-white" +
                  (validationInProgress ? " pointer-events-none opacity-50" : "")
                }
              >
                <SparklesIcon className="size-3.5" />
                Curate with AI
                {pendingProposalsCount > 0 && (
                  <span className="ml-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--color-brand-primary)] px-1 text-[10px] font-semibold text-white">
                    {pendingProposalsCount}
                  </span>
                )}
              </Link>
            )}
            {/* Standards-doc upload was removed in favor of the
                canonical /documents flow. The Standards tab on the
                Documents page is the single place to upload, see
                history, and watch the live extraction summary. */}
            <Link
              href="/documents"
              className={
                "inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-colors " +
                (pendingBatchCount > 0
                  ? "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300"
                  : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10")
              }
              title={
                pendingBatchCount > 0
                  ? `${pendingBatchRuleCount} extracted rule(s) awaiting review across ${pendingBatchCount} document(s)`
                  : undefined
              }
            >
              <FileTextIcon className="size-3.5" />
              {pendingBatchCount > 0 ? "Review extracted rules" : "Manage uploads"}
              {pendingBatchCount > 0 && (
                <span className="ml-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500 px-1 text-[10px] font-semibold text-white">
                  {pendingBatchRuleCount}
                </span>
              )}
            </Link>

            {canCurate && (
              <Dialog open={createOpen} onOpenChange={setCreateOpen}>
                <DialogTrigger
                  render={
                    <Button size="sm" disabled={validationInProgress}>
                      <PlusIcon className="size-3.5 mr-1.5" />
                      Add Standard
                    </Button>
                  }
                />
                <DialogContent className="sm:max-w-lg">
                  <DialogHeader>
                    <DialogTitle>Add New Standard</DialogTitle>
                  </DialogHeader>
                  <StandardForm onSubmit={handleCreate} submitLabel="Create Standard" />
                </DialogContent>
              </Dialog>
            )}
          </>
        }
      />

      {/* Summary KPI tiles — clean grid, brand-tinted accent for total */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <KpiTile label="Total" value={standards.length} accent="brand" />
        <KpiTile label="Critical" value={criticalCount} accent="critical" />
        <KpiTile label="High" value={highCount} accent="high" />
        <KpiTile label="Medium" value={mediumCount} accent="medium" />
        <KpiTile label="User-defined" value={userCount} accent="info" />
        <KpiTile label="From docs" value={documentCount} accent="info" />
      </div>

      {/* Hosting-scope segmented control. Counts come
          from the unfiltered rule library; clicking a segment swaps
          the rule set the rest of the page (chips, filters, sections)
          operates on. */}
      {(() => {
        // Counts are shown as a clean PARTITION so the pills sum to the total
        // and don't mislead: universal (both) is its own bucket, and On-prem /
        // Cloud show only their SCOPE-SPECIFIC rules. The universal rules that
        // ALSO apply to a scope are surfaced as a "+N universal" annotation
        // (and are still included when that scope filter is active), so the
        // annotation matches exactly what the filtered list will show.
        const universalCount = standards.filter(
          (s) => s.hosting_scope === "both",
        ).length;
        const onPremSpecific = standards.filter(
          (s) => s.hosting_scope === "on_prem",
        ).length;
        const cloudSpecific = standards.filter(
          (s) => s.hosting_scope === "cloud",
        ).length;
        const allCount = standards.length;
        const segments: Array<{
          key: HostingScopeView; label: string; count: number; plusUniversal?: number;
        }> = [
          { key: "all", label: "All", count: allCount },
          { key: "both", label: "Universal", count: universalCount },
          { key: "on_prem", label: "On-prem", count: onPremSpecific, plusUniversal: universalCount },
          { key: "cloud", label: "Cloud", count: cloudSpecific, plusUniversal: universalCount },
        ];
        return (
          <div className="space-y-1.5">
            <div className="flex w-fit max-w-full items-center gap-0.5 overflow-x-auto rounded-lg border border-slate-200 bg-white p-0.5 dark:border-white/10 dark:bg-white/5">
              {segments.map((seg) => {
                const isActive = hostingScopeView === seg.key;
                return (
                  <button
                    key={seg.key}
                    onClick={() => setHostingScopeView(seg.key)}
                    className={
                      "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-all " +
                      (isActive
                        ? "bg-[var(--color-brand-primary)] text-white shadow-sm"
                        : "text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-white")
                    }
                    aria-pressed={isActive}
                  >
                    <span>{seg.label}</span>
                    <span className={isActive ? "text-white/75" : "text-slate-400 dark:text-slate-500"}>
                      {seg.count}
                      {seg.plusUniversal ? (
                        <span
                          className={isActive ? "text-white/60" : "text-slate-300 dark:text-slate-600"}
                          title={`${seg.count} ${seg.label}-specific rule(s) + ${seg.plusUniversal} universal rule(s) that also apply`}
                        >
                          {" "}+{seg.plusUniversal}
                        </span>
                      ) : null}
                    </span>
                  </button>
                );
              })}
            </div>
            {(hostingScopeView === "on_prem" || hostingScopeView === "cloud") && (
              <p className="text-[11px] text-slate-500 dark:text-slate-400">
                Showing {standards.filter((s) => s.hosting_scope === (hostingScopeView === "on_prem" ? "on_prem" : "cloud")).length}{" "}
                {hostingScopeView === "on_prem" ? "on-prem" : "cloud"}-specific rule(s)
                {" "}+ {standards.filter((s) => s.hosting_scope === "both").length} universal
                rule(s) that apply to both environments.
              </p>
            )}
          </div>
        );
      })()}

      {/* Category Chip Strip — quick filter by topic */}
      {(() => {
        const categoryCounts = new Map<string, number>();
        standards.forEach((s) => {
          const cat = s.category || "availability";
          categoryCounts.set(cat, (categoryCounts.get(cat) || 0) + 1);
        });
        const sortedCategories = Array.from(categoryCounts.entries()).sort((a, b) => b[1] - a[1]);
        if (sortedCategories.length === 0) return null;
        return (
          <div className="-mx-1 flex gap-2 overflow-x-auto px-1 pb-1">
            {sortedCategories.map(([cat, count]) => {
              const isActive = categoryFilter === cat;
              return (
                <button
                  key={cat}
                  onClick={() => setCategoryFilter(isActive ? null : cat)}
                  className={
                    "group inline-flex shrink-0 items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium transition-all " +
                    (isActive
                      ? "border-[var(--color-brand-primary)]/30 bg-[var(--color-brand-primary)]/8 text-[var(--color-brand-primary)]"
                      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10")
                  }
                >
                  <span className={isActive ? "text-[var(--color-brand-primary)]" : "text-slate-400 group-hover:text-slate-600 dark:text-slate-500"}>
                    {categoryIcons[cat] || categoryIcons.availability}
                  </span>
                  <span>{categoryLabels[cat] || cat}</span>
                  <span className={isActive ? "text-[var(--color-brand-primary)]/60" : "text-slate-400 dark:text-slate-500"}>
                    {count}
                  </span>
                </button>
              );
            })}
          </div>
        );
      })()}

      {/* Filter bar — sectioned by intent so the eye can land on
          the right control fast.

          Three groups, top-to-bottom:
            1. Attributes — what the rule IS (severity)
            2. Provenance — where the rule came FROM (source / doc)
            3. Decision-tree axes — what the rule is ABOUT in the
               review framework (tier × layer × rule type)

          Category lives in its own chip strip above this card; the
          old duplicate Category dropdown was pulling its weight twice
          and making the bar feel cramped, so it's gone. */}
      <div className="rounded-xl border border-slate-200/80 bg-white dark:border-white/8 dark:bg-white/[0.02]">
        <div className="p-3">
          <div className="relative">
            <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-slate-400" />
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search standards by name, description, or criteria…"
              className="pl-9"
            />
          </div>
        </div>

        {/* Section 1: Attributes */}
        <div className="border-t border-slate-200/60 px-3 py-2.5 dark:border-white/8">
          <div className="mb-1.5 text-[9px] font-semibold uppercase tracking-[0.08em] text-slate-400 dark:text-slate-500">
            Attributes
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Severity</span>
            {severityOptions.map((opt) => {
              const count = standards.filter((s) => s.severity === opt.value).length;
              const isActive = severityFilter === opt.value;
              return (
                <button
                  key={opt.value}
                  onClick={() => setSeverityFilter(isActive ? null : opt.value)}
                  className={
                    "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-all " +
                    (isActive
                      ? severityConfig[opt.value].className
                      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10")
                  }
                >
                  {opt.label}
                  <span className="opacity-60">{count}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Section 2: Provenance */}
        <div className="border-t border-slate-200/60 px-3 py-2.5 dark:border-white/8">
          <div className="mb-1.5 text-[9px] font-semibold uppercase tracking-[0.08em] text-slate-400 dark:text-slate-500">
            Provenance
          </div>
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Source</span>
              <div className="inline-flex items-center gap-0.5 rounded-md border border-slate-200 bg-white p-0.5 dark:border-white/10 dark:bg-white/5">
                {(["all", "user", "document", "generated"] as const).map((val) => (
                  <button
                    key={val}
                    onClick={() => setSourceFilter(val)}
                    className={
                      "rounded px-2 py-0.5 text-[11px] font-medium transition-all " +
                      (sourceFilter === val
                        ? "bg-[var(--color-brand-primary)] text-white shadow-sm"
                        : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white")
                    }
                  >
                    {val === "all" ? "All" : val === "user" ? "User" : val === "document" ? "Document" : "AI"}
                  </button>
                ))}
              </div>
            </div>

          {/* Document Filter — only renders when at least one rule was
              extracted from a document. Filenames can be long, so we
              cap the select width and let the truncate ellipsis show
              the rest on hover via the underlying option titles. */}
          {sourceDocOptions.length > 0 && (
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Document</span>
              <select
                className="h-7 max-w-[220px] rounded-md border border-slate-200 bg-white px-2 text-xs text-slate-700 transition-colors hover:border-slate-300 focus:border-[var(--color-brand-primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/15 dark:bg-white/5 dark:text-slate-200"
                value={sourceDocFilter || ""}
                onChange={(e) => setSourceDocFilter(e.target.value || null)}
                title={sourceDocFilter || "All documents"}
              >
                <option value="">All</option>
                {sourceDocOptions.map((doc) => {
                  const count = standards.filter((s) => s.source_document === doc).length;
                  return (
                    <option key={doc} value={doc} title={doc}>
                      {doc} ({count})
                    </option>
                  );
                })}
              </select>
            </div>
          )}
          </div>
        </div>

        {/* Section 3: Decision-tree axes — the review framework dimensions.
            Tier × Layer × Rule Type. Structured-rule extraction
            populates these axes; older rules carry only Tier. Layer +
            Rule Type chips still appear, but their counts stay zero
            until rules carry those fields. */}
        <div className="border-t border-slate-200/60 px-3 py-2.5 dark:border-white/8">
          <div className="mb-1.5 text-[9px] font-semibold uppercase tracking-[0.08em] text-slate-400 dark:text-slate-500">
            Decision-tree axes
          </div>
          <div className="flex flex-col gap-1.5">
          {/* Tier filter. Universal rules always match every tier so
              they remain visible no matter what's picked here. */}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="w-[60px] text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Tier</span>
            {[null, 1, 2, 3, 4, 5].map((t) => {
              const isActive = tierFilter === t;
              const label = t === null ? "All" : `T${t}`;
              const count = t === null
                ? standards.length
                : standards.filter((s) => {
                    const tiers = s.applies_to_tiers;
                    return !tiers || tiers.length === 0 || tiers.includes(t);
                  }).length;
              return (
                <button
                  key={String(t)}
                  onClick={() => setTierFilter(t)}
                  className={
                    "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-all " +
                    (isActive
                      ? "bg-[var(--color-brand-primary)]/[0.08] text-[var(--color-brand-primary)] border-[var(--color-brand-primary)]/20 dark:bg-[var(--color-brand-primary)]/15 dark:text-[#5eead4] dark:border-[var(--color-brand-primary)]/30"
                      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10")
                  }
                >
                  {label}
                  <span className="opacity-60">{count}</span>
                </button>
              );
            })}
          </div>

          {/* Structured-rule filters — Layer + Rule Type. The
              fixed-width label gutter aligns Tier / Layer / Rule type
              vertically so the eye reads them as one cluster. */}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="w-[60px] text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Layer</span>
            {([null, ...LAYER_VALUES] as const).map((v) => {
              const isActive = layerFilter === v;
              const label = v === null ? "All" : layerLabels[v];
              const count = v === null
                ? standards.length
                : standards.filter((s) => s.layer === v).length;
              return (
                <button
                  key={String(v)}
                  onClick={() => setLayerFilter(v as RuleLayer | null)}
                  className={
                    "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-all " +
                    (isActive
                      ? "bg-indigo-50 text-indigo-700 border-indigo-200/80 dark:bg-indigo-500/15 dark:text-indigo-300 dark:border-indigo-500/30"
                      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10")
                  }
                >
                  {label}
                  <span className="opacity-60">{count}</span>
                </button>
              );
            })}
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            <span className="w-[60px] text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">Rule type</span>
            {([null, ...RULE_TYPE_VALUES] as const).map((v) => {
              const isActive = ruleTypeFilter === v;
              const label = v === null ? "All" : ruleTypeLabels[v];
              const count = v === null
                ? standards.length
                : standards.filter((s) => s.rule_type === v).length;
              return (
                <button
                  key={String(v)}
                  onClick={() => setRuleTypeFilter(v as RuleType | null)}
                  className={
                    "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-all " +
                    (isActive
                      ? "bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-500/15 dark:text-emerald-300 dark:border-emerald-500/30"
                      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10")
                  }
                >
                  {label}
                  <span className="opacity-60">{count}</span>
                </button>
              );
            })}
          </div>
          </div>
        </div>

        {/* Active filters summary — its own band so the count + clear
            link don't ride into Section 3's content. Only renders when
            something is actually filtered. */}
        {hasActiveFilter && (
          <div className="flex items-center gap-2 border-t border-slate-200/60 px-3 py-2 dark:border-white/8">
            <span className="text-[11px] text-slate-500 dark:text-slate-400">
              Showing <strong className="text-slate-700 dark:text-slate-200">{filteredStandards.length}</strong> of {standards.length}
            </span>
            <button
              onClick={clearAllFilters}
              className="text-[11px] font-medium text-[var(--color-brand-primary)] transition-colors hover:text-[var(--color-brand-primary-hover)]"
            >
              Clear filters
            </button>
          </div>
        )}
      </div>

      {/* Standards List - Grouped by Layer.
          Canonical order: general → datacenter → data → web →
          application → load_balancer → _legacy. The synthetic
          _legacy bucket holds rules whose `layer` is undefined
          (rows created before the field existed, or hand-crafted
          rules where the user left it blank). Sections with zero
          rules after filtering are hidden. */}
      <div className="space-y-4">
        {(() => {
          type LayerKey = RuleLayer | "_legacy";
          const LAYER_ORDER: LayerKey[] = [
            "general",
            "datacenter",
            "data",
            "web",
            "application",
            "load_balancer",
            "_legacy",
          ];

          // Bucketize. Initialise every key in canonical order so
          // the iteration order stays predictable even when several
          // sections are empty.
          const grouped = new Map<LayerKey, StandardItem[]>();
          LAYER_ORDER.forEach((k) => grouped.set(k, []));
          filteredStandards.forEach((s) => {
            const key: LayerKey = (s.layer as RuleLayer | undefined) ?? "_legacy";
            // The layer vocabulary is a manual backend<->frontend contract, so a
            // rule can arrive with a layer this build doesn't know about. Bucket
            // it rather than dereferencing an absent key — an unknown value must
            // not take the page down, and the rule must still be visible.
            const bucket = grouped.get(key) ?? grouped.get("_legacy")!;
            bucket.push(s);
          });

          // Sort within each section by severity desc, then id asc.
          const sevOrder = ["critical", "high", "medium", "low"];
          grouped.forEach((items) => {
            items.sort((a, b) => {
              const sd = sevOrder.indexOf(a.severity) - sevOrder.indexOf(b.severity);
              if (sd !== 0) return sd;
              return a.id.localeCompare(b.id);
            });
          });

          if (filteredStandards.length === 0 && standards.length > 0) {
            return (
              <Card>
                <CardContent className="py-8">
                  <div className="text-center">
                    <p className="text-sm text-muted-foreground">
                      No standards match your current filters.
                    </p>
                    <button
                      onClick={clearAllFilters}
                      className="mt-2 text-xs text-[var(--color-brand-primary)] hover:underline"
                    >
                      Clear all filters
                    </button>
                  </div>
                </CardContent>
              </Card>
            );
          }

          if (standards.length === 0) {
            return (
              <Card>
                <CardContent className="py-12">
                  <div className="text-center">
                    <p className="text-sm text-muted-foreground">
                      No standards defined yet. Add your first standard or extract from a document.
                    </p>
                    <Button className="mt-4" onClick={() => setCreateOpen(true)}>
                      <PlusIcon className="size-4 mr-1.5" />
                      Add Standard
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          }

          return LAYER_ORDER.filter((k) => (grouped.get(k) || []).length > 0).map((key) => (
            <RuleGroup
              key={key}
              layer={key}
              standards={grouped.get(key) || []}
              allStandards={standards}
              severityConfig={severityConfig}
              ruleTypeFilter={ruleTypeFilter}
              editingStandard={editingStandard}
              setEditingStandard={setEditingStandard}
              deleteConfirm={deleteConfirm}
              setDeleteConfirm={setDeleteConfirm}
              handleUpdate={handleUpdate}
              handleDelete={handleDelete}
              validationInProgress={validationInProgress}
            />
          ));
        })()}
      </div>

    </div>
  );
}

type KpiAccent = "brand" | "critical" | "high" | "medium" | "low" | "info";

const KPI_ACCENT: Record<KpiAccent, { dot: string; value: string }> = {
  brand:    { dot: "bg-[var(--color-brand-primary)]", value: "text-slate-900 dark:text-slate-50" },
  critical: { dot: "bg-rose-500",   value: "text-rose-700 dark:text-rose-300" },
  high:     { dot: "bg-orange-500", value: "text-orange-700 dark:text-orange-300" },
  medium:   { dot: "bg-amber-500",  value: "text-amber-700 dark:text-amber-300" },
  low:      { dot: "bg-blue-500",   value: "text-blue-700 dark:text-blue-300" },
  info:     { dot: "bg-sky-500",    value: "text-sky-700 dark:text-sky-300" },
};

function KpiTile({ label, value, accent }: { label: string; value: number; accent: KpiAccent }) {
  const c = KPI_ACCENT[accent];
  return (
    <div className="rounded-xl border border-slate-200/80 bg-white p-3.5 transition-colors hover:border-slate-300/80 dark:border-white/8 dark:bg-white/[0.02] dark:hover:border-white/12">
      <div className="flex items-center gap-1.5">
        <span className={`inline-block h-1.5 w-1.5 rounded-full ${c.dot}`} aria-hidden />
        <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
          {label}
        </span>
      </div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${c.value}`}>{value}</div>
    </div>
  );
}

// Layer iconography. Each architectural layer + the
// synthetic _legacy bucket gets a distinct lucide-style svg so the
// section header reads at a glance. Inline SVGs to match the
// existing categoryIcons pattern (no extra import).
const layerIcons: Record<RuleLayer | "_legacy", React.ReactNode> = {
  // Datacenter — stacked building
  datacenter: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 21V7l6-4 6 4v14M3 21h18M9 9h2m-2 4h2m-2 4h2m4-4h2m-2 4h2m-2-8V3" /></svg>,
  // Data — cylinder/database
  data: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4" /></svg>,
  // Web — globe
  web: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9" /></svg>,
  // Application — code brackets
  application: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" /></svg>,
  // Load balancer — distribution / split arrows
  load_balancer: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 12h6m0 0l-3-3m3 3l-3 3m12-3a3 3 0 11-6 0 3 3 0 016 0zm-3 5v3m0-16v3" /></svg>,
  // General — stacked layers (catch-all)
  general: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" /></svg>,
  // Legacy — clock (bucket for un-classified rows)
  _legacy: <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>,
};

const layerHeaderLabels: Record<RuleLayer | "_legacy", string> = {
  ...layerLabels,
  _legacy: "Legacy (no layer set)",
};

function RuleGroup({
  layer,
  standards: items,
  allStandards,
  severityConfig,
  ruleTypeFilter,
  editingStandard,
  setEditingStandard,
  deleteConfirm,
  setDeleteConfirm,
  handleUpdate,
  handleDelete,
  validationInProgress,
}: {
  layer: RuleLayer | "_legacy";
  /** Standards in THIS layer section, already filtered + sorted. */
  standards: StandardItem[];
  /** Full unfiltered library — used to resolve parent_rule_id for
   *  nested children even when the parent is filtered out of the
   *  current view. */
  allStandards: StandardItem[];
  severityConfig: Record<string, { label: string; className: string }>;
  ruleTypeFilter: RuleType | null;
  editingStandard: StandardItem | null;
  setEditingStandard: (s: StandardItem | null) => void;
  deleteConfirm: string | null;
  setDeleteConfirm: (id: string | null) => void;
  handleUpdate: (data: CreateStandardData) => void;
  handleDelete: (id: string) => void;
  validationInProgress: boolean;
}) {
  const [expanded, setExpanded] = useState(true);

  // Build a parent-id lookup over the full library so nested children
  // can resolve their parent even if the parent is on a different
  // layer or filtered out.
  const allById = new Map(allStandards.map((s) => [s.id, s]));

  // Partition this section's rules into orphans / foundings / children.
  // Filter override: when ruleTypeFilter is "substitution" or
  // "exception", founding rules are hidden entirely; matching children
  // and orphans render flat at top level. Toggling back to All
  // restores the founding-with-disclosure layout.
  const isChildFilter =
    ruleTypeFilter === "substitution" || ruleTypeFilter === "exception";

  const orphans: StandardItem[] = [];
  const foundings: StandardItem[] = [];
  const childrenByParent = new Map<string, StandardItem[]>();

  for (const s of items) {
    const isChild = s.rule_type === "substitution" || s.rule_type === "exception";
    if (isChild) {
      const parentId = s.parent_rule_id;
      if (parentId && allById.has(parentId)) {
        if (!childrenByParent.has(parentId)) childrenByParent.set(parentId, []);
        childrenByParent.get(parentId)!.push(s);
      } else {
        // Orphan: parent_rule_id missing or doesn't resolve.
        orphans.push(s);
      }
    } else {
      foundings.push(s);
    }
  }

  // When filtering for substitution/exception, the rule list
  // already filtered out foundings — but our partition above still
  // catches any that slipped through. Force-empty in this view.
  const renderFoundings = isChildFilter ? [] : foundings;

  // In the child-filter view, all matching children flatten to top
  // level (orphans go above non-orphans within this group).
  const flatChildren: StandardItem[] = [];
  if (isChildFilter) {
    childrenByParent.forEach((arr) => flatChildren.push(...arr));
  }

  const ruleCount = items.length;
  const foundingCount = items.filter((s) => s.rule_type === "founding").length;
  const altCount = items.filter((s) => s.rule_type === "substitution").length;
  const exceptionCount = items.filter((s) => s.rule_type === "exception").length;

  // Per-section count chip:
  // - _legacy: always show only "{N rules}" (no founding/substitution/
  //   exception breakdown — unclassified rules don't participate in
  //   the decision tree)
  // - real layer with any structured rules: append breakdown of
  //   non-zero counts
  // - section of unstructured rules only: just "{N rules}"
  const showBreakdown =
    layer !== "_legacy" && (foundingCount + altCount + exceptionCount > 0);

  const headerName = layerHeaderLabels[layer];

  return (
    <Card className="overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center justify-between rounded-t-xl px-4 py-3 text-left transition-colors hover:bg-slate-50/60 dark:hover:bg-white/[0.03]"
      >
        <div className="flex items-center gap-3">
          <svg
            className={`h-3.5 w-3.5 text-slate-400 transition-transform ${expanded ? "rotate-90" : ""}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
          <span className="text-[var(--color-brand-primary)]">
            {layerIcons[layer]}
          </span>
          <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            {headerName}
          </span>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-medium text-slate-500 dark:border-white/10 dark:bg-white/5 dark:text-slate-400">
            {ruleCount} {ruleCount === 1 ? "rule" : "rules"}
            {showBreakdown && (
              <>
                {foundingCount > 0 && <span> · {foundingCount}F</span>}
                {altCount > 0 && <span> · {altCount}S</span>}
                {exceptionCount > 0 && <span> · {exceptionCount}E</span>}
              </>
            )}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {layer === "_legacy" && (
            <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
              Assign a layer via Edit
            </span>
          )}
        </div>
      </button>

      {expanded && (
        <div className="border-t border-slate-200/60 dark:border-white/8">
          <div className="divide-y divide-slate-200/60 dark:divide-white/8">
            {/* Render order:
                  1. Orphan substitutions/exceptions (with ⚠ no parent chip)
                  2. Founding rules — each with optional child disclosure
                     (when expanded, children render indented below)
                  3. Filter-override view (rule_type=substitution|exception):
                     foundings hidden; matching children + orphans flat.
                Orphans render first within each section. */}
            {(isChildFilter ? [...orphans, ...flatChildren] : [...orphans, ...renderFoundings]).map((standard) => {
              const sev = severityConfig[standard.severity] || severityConfig.medium;
              const isUserDefined = standard.source === "user";
              const isOrphan =
                (standard.rule_type === "substitution" || standard.rule_type === "exception") &&
                (!standard.parent_rule_id || !allById.has(standard.parent_rule_id));
              const isFounding = standard.rule_type === "founding" || !standard.rule_type;
              const myChildren = isFounding && !isChildFilter
                ? (childrenByParent.get(standard.id) || [])
                : [];
              const sevDot = standard.severity === "critical"
                ? "bg-rose-500"
                : standard.severity === "high"
                ? "bg-orange-500"
                : standard.severity === "low"
                ? "bg-blue-500"
                : "bg-amber-500";
              return (
                <div key={standard.id} className="px-4 py-3.5 transition-colors hover:bg-slate-50/60 dark:hover:bg-white/[0.02]">
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="mb-1 flex items-center gap-2">
                        <span className={`inline-block h-1.5 w-1.5 rounded-full ${sevDot}`} aria-hidden />
                        <span className="font-mono text-[10px] font-medium text-slate-400 dark:text-slate-500">
                          {standard.id}
                        </span>
                        {isOrphan && (
                          <span
                            className="inline-flex items-center gap-1 rounded-full border border-amber-200/80 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300"
                            title={`References parent ${standard.parent_rule_id || "(unset)"} which is not in the library.`}
                          >
                            ⚠ no parent
                          </span>
                        )}
                        <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                          {standard.name}
                        </h3>
                      </div>
                      <p className="mb-2 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                        {standard.description}
                      </p>
                      <div className="rounded-lg border border-slate-200/70 bg-slate-50/60 px-3 py-2 dark:border-white/8 dark:bg-white/[0.03]">
                        <p className="mb-0.5 text-[10px] font-semibold uppercase tracking-normal text-slate-500 dark:text-slate-400">
                          Validation criteria
                        </p>
                        <p className="text-[12px] leading-relaxed text-slate-700 dark:text-slate-200">
                          {standard.validation_criteria}
                        </p>
                      </div>
                      {/* Source info */}
                      {standard.source === "document" && standard.source_document && (
                        <div className="mt-2 flex items-center gap-1.5 text-[10px] text-slate-500 dark:text-slate-400">
                          <FileTextIcon className="size-2.5" />
                          <span>Extracted from: {standard.source_document}</span>
                        </div>
                      )}
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-1.5">
                      <Badge variant="outline" className={sev.className + " text-[10px]"}>
                        {sev.label}
                      </Badge>
                      <Badge
                        variant="outline"
                        className={
                          "text-[10px] " +
                          (isUserDefined
                            ? "bg-sky-50 text-sky-700 border-sky-200/80 dark:bg-sky-500/10 dark:text-sky-300 dark:border-sky-500/20"
                            : "bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20")
                        }
                      >
                        {isUserDefined ? "User" : "Document"}
                      </Badge>
                      {/* Tier scope. Subtle styling — universal rules
                          are the common case so we don't want this
                          chip to scream. Targeted-tier rules use a
                          mild burgundy hint to draw the eye. */}
                      <Badge
                        variant="outline"
                        className={
                          "text-[10px] font-mono " +
                          (!standard.applies_to_tiers || standard.applies_to_tiers.length === 5
                            ? "bg-slate-50 text-slate-500 border-slate-200/80 dark:bg-white/5 dark:text-slate-400 dark:border-white/10"
                            : "bg-[var(--color-brand-primary)]/[0.08] text-[var(--color-brand-primary)] border-[var(--color-brand-primary)]/20 dark:bg-[var(--color-brand-primary)]/15 dark:text-[#5eead4] dark:border-[var(--color-brand-primary)]/30")
                        }
                      >
                        {formatTiers(standard.applies_to_tiers)}
                      </Badge>
                      {/* hosting_scope is required, so every rule
                          renders a pill. Universal rules use a
                          neutral slate; on-prem + cloud get distinct
                          accent colors so the eye separates them in
                          mixed views (e.g. the All segment). */}
                      <Badge
                        variant="outline"
                        className={
                          "text-[10px] " +
                          (standard.hosting_scope === "on_prem"
                            ? "bg-amber-50 text-amber-700 border-amber-200/80 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20"
                            : standard.hosting_scope === "cloud"
                            ? "bg-sky-50 text-sky-700 border-sky-200/80 dark:bg-sky-500/10 dark:text-sky-300 dark:border-sky-500/20"
                            : "bg-slate-100 text-slate-600 border-slate-200/80 dark:bg-white/10 dark:text-slate-300 dark:border-white/15")
                        }
                      >
                        {hostingScopeLabels[standard.hosting_scope]}
                      </Badge>
                      {/* Only render when set; older / flat rules show
                          no Layer or Rule Type pill. */}
                      {standard.layer && (
                        <Badge
                          variant="outline"
                          className="text-[10px] bg-indigo-50 text-indigo-700 border-indigo-200/80 dark:bg-indigo-500/10 dark:text-indigo-300 dark:border-indigo-500/20"
                        >
                          {layerLabels[standard.layer] ?? standard.layer}
                        </Badge>
                      )}
                      {standard.rule_type && (
                        <Badge
                          variant="outline"
                          className={
                            "text-[10px] " +
                            (standard.rule_type === "founding"
                              ? "bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20"
                              : standard.rule_type === "substitution"
                              ? "bg-violet-50 text-violet-700 border-violet-200/80 dark:bg-violet-500/10 dark:text-violet-300 dark:border-violet-500/20"
                              : "bg-amber-50 text-amber-700 border-amber-200/80 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20")
                          }
                        >
                          {ruleTypeLabels[standard.rule_type] ?? standard.rule_type}
                        </Badge>
                      )}
                        {/* Action buttons for user-defined standards */}
                        {(
                          <div className="flex items-center gap-1 mt-0.5">
                            <Dialog
                              open={editingStandard?.id === standard.id}
                              onOpenChange={(open) => {
                                if (!open) setEditingStandard(null);
                              }}
                            >
                              <DialogTrigger
                                render={
                                  <Button
                                    variant="ghost"
                                    size="icon-xs"
                                    disabled={validationInProgress}
                                    onClick={() => setEditingStandard(standard)}
                                  >
                                    <PencilIcon className="size-3" />
                                  </Button>
                                }
                              />
                              <DialogContent className="sm:max-w-lg">
                                <DialogHeader>
                                  <DialogTitle>Edit Standard</DialogTitle>
                                </DialogHeader>
                                {editingStandard && (
                                  <StandardForm
                                    initial={{
                                      name: editingStandard.name,
                                      description: editingStandard.description,
                                      severity: editingStandard.severity as CreateStandardData["severity"],
                                      category: editingStandard.category,
                                      validation_criteria: editingStandard.validation_criteria,
                                      // Preserve current values so a user editing
                                      // only severity doesn't accidentally clear an
                                      // existing layer / rule_type.
                                      layer: editingStandard.layer,
                                      rule_type: editingStandard.rule_type,
                                      hosting_scope: editingStandard.hosting_scope,
                                    }}
                                    onSubmit={handleUpdate}
                                    submitLabel="Save Changes"
                                  />
                                )}
                              </DialogContent>
                            </Dialog>

                            <Dialog
                              open={deleteConfirm === standard.id}
                              onOpenChange={(open) => {
                                if (!open) setDeleteConfirm(null);
                              }}
                            >
                              <DialogTrigger
                                render={
                                  <Button
                                    variant="destructive"
                                    size="icon-xs"
                                    disabled={validationInProgress}
                                    onClick={() => setDeleteConfirm(standard.id)}
                                  >
                                    <TrashIcon className="size-3" />
                                  </Button>
                                }
                              />
                              <DialogContent>
                                <DialogHeader>
                                  <DialogTitle>Delete Standard</DialogTitle>
                                </DialogHeader>
                                <p className="text-sm text-muted-foreground">
                                  Are you sure you want to delete &quot;{standard.name}&quot;? This action cannot be undone.
                                </p>
                                <DialogFooter>
                                  <Button
                                    variant="outline"
                                    onClick={() => setDeleteConfirm(null)}
                                  >
                                    Cancel
                                  </Button>
                                  <Button
                                    variant="destructive"
                                    onClick={() => handleDelete(standard.id)}
                                  >
                                    Delete
                                  </Button>
                                </DialogFooter>
                              </DialogContent>
                            </Dialog>
                          </div>
                        )}
                      </div>
                    </div>

                    {/* Nested children disclosure. Only render
                        when this is a founding rule with at least one
                        substitution/exception child. The native <details>
                        element gives us free accessibility + keyboard
                        nav. Children render indented below with a left
                        border accent so the relationship reads visually. */}
                    {myChildren.length > 0 && (
                      <details className="mt-3 rounded-md border border-slate-200/60 bg-slate-50/50 dark:border-white/8 dark:bg-white/[0.02]">
                        <summary className="cursor-pointer select-none px-3 py-2 text-[11px] font-medium text-slate-600 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white">
                          {(() => {
                            const altCount = myChildren.filter((c) => c.rule_type === "substitution").length;
                            const excCount = myChildren.filter((c) => c.rule_type === "exception").length;
                            const parts: string[] = [];
                            if (altCount > 0) parts.push(`${altCount} alternative${altCount === 1 ? "" : "s"}`);
                            if (excCount > 0) parts.push(`${excCount} exception${excCount === 1 ? "" : "s"}`);
                            return `▶ ${parts.join(" · ")}`;
                          })()}
                        </summary>
                        <div className="border-t border-slate-200/60 dark:border-white/8">
                          <ul className="divide-y divide-slate-200/60 dark:divide-white/8">
                            {myChildren.map((child) => (
                              <li key={child.id} className="border-l-2 border-l-slate-300 px-3 py-2 pl-5 dark:border-l-white/15">
                                <div className="flex items-start justify-between gap-3">
                                  <div className="min-w-0 flex-1">
                                    <div className="flex items-center gap-1.5">
                                      <span className="font-mono text-[10px] font-medium text-slate-400 dark:text-slate-500">
                                        ↳ {child.id}
                                      </span>
                                      <Badge
                                        variant="outline"
                                        className={
                                          "text-[10px] " +
                                          (child.rule_type === "substitution"
                                            ? "bg-violet-50 text-violet-700 border-violet-200/80 dark:bg-violet-500/10 dark:text-violet-300 dark:border-violet-500/20"
                                            : "bg-amber-50 text-amber-700 border-amber-200/80 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20")
                                        }
                                      >
                                        {child.rule_type === "substitution" ? "Substitution" : "Exception"}
                                      </Badge>
                                      <span className="text-[12px] font-medium text-slate-700 dark:text-slate-200">
                                        {child.name}
                                      </span>
                                    </div>
                                    <p className="ml-3 mt-0.5 text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
                                      {child.description}
                                    </p>
                                  </div>
                                  <Dialog
                                    open={editingStandard?.id === child.id}
                                    onOpenChange={(open) => {
                                      if (!open) setEditingStandard(null);
                                    }}
                                  >
                                    <DialogTrigger
                                      render={
                                        <Button
                                          variant="ghost"
                                          size="icon-xs"
                                          disabled={validationInProgress}
                                          onClick={() => setEditingStandard(child)}
                                          aria-label={`Edit child rule ${child.id}`}
                                        >
                                          <PencilIcon className="size-3" />
                                        </Button>
                                      }
                                    />
                                    <DialogContent className="sm:max-w-lg">
                                      <DialogHeader>
                                        <DialogTitle>Edit Standard</DialogTitle>
                                      </DialogHeader>
                                      {editingStandard?.id === child.id && (
                                        <StandardForm
                                          initial={{
                                            name: child.name,
                                            description: child.description,
                                            severity: child.severity as CreateStandardData["severity"],
                                            category: child.category,
                                            validation_criteria: child.validation_criteria,
                                            layer: child.layer,
                                            rule_type: child.rule_type,
                                            hosting_scope: child.hosting_scope,
                                          }}
                                          onSubmit={handleUpdate}
                                          submitLabel="Save Changes"
                                        />
                                      )}
                                    </DialogContent>
                                  </Dialog>
                                </div>
                              </li>
                            ))}
                          </ul>
                        </div>
                      </details>
                    )}
                  </div>
              );
            })}
          </div>
        </div>
      )}
    </Card>
  );
}
