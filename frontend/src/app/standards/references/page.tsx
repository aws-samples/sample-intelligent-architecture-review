// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * References tab.
 *
 * References are scope-tagged context blocks the validator attaches
 * alongside rules but never tests an SDD against. They live in their
 * own DDB table and never mix with rules. The canonical example is a
 * datacenter-peer table from a standards document, which lets the
 * validator decide whether an SDD's claimed datacenter triple is
 * sanctioned.
 *
 * UI is intentionally simple: list / detail / edit / delete.
 * Markdown body is rendered with the same react-markdown +
 * remark-gfm pipeline as chat replies so tables in References
 * render as proper tables.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  api,
  CreateReferenceRequest,
  HOSTING_SCOPE_VALUES,
  HostingScope,
  LAYER_VALUES,
  ReferenceItem,
  RuleLayer,
} from "@/lib/api";

const HOSTING_SCOPE_LABELS: Record<HostingScope, string> = {
  on_prem: "On-prem",
  cloud: "Cloud",
  both: "Universal",
};

const ALL_TIERS = [1, 2, 3, 4, 5];

function tierLabel(tiers?: number[]): string {
  if (!tiers || tiers.length === 0) return "T1-5";
  const sorted = [...tiers].sort((a, b) => a - b);
  if (sorted.length === 5 && sorted[0] === 1 && sorted[4] === 5) return "T1-5";
  return "T" + sorted.join(",");
}

export default function ReferencesPage() {
  const [refs, setRefs] = useState<ReferenceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<ReferenceItem | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  async function reload() {
    setLoading(true);
    try {
      const res = await api.listReferences({ status: "active" });
      setRefs(res.references);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Load failed");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    void api.listReferences({ status: "active" })
      .then((res) => {
        if (!cancelled) setRefs(res.references);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Load failed");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
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
        <div className="flex items-end justify-between gap-3">
          <div className="space-y-1">
            <h1 className="text-2xl font-semibold tracking-normal text-slate-900 dark:text-slate-50">
              References
            </h1>
            <p className="text-sm text-slate-500 dark:text-slate-400 max-w-3xl">
              Factual context the validator attaches alongside rules — datacenter peer
              tables, network zone definitions, glossary entries. Not testable as rules
              themselves, but help the AI judge whether an SDD&apos;s claims (e.g. a specific
              DC triple) are valid.
            </p>
          </div>
          <Button size="sm" onClick={() => setShowCreate(true)}>
            + New reference
          </Button>
        </div>
      </div>

      {/* List */}
      <Card>
        <CardHeader className="border-b border-slate-200/60 px-5 py-3.5 dark:border-white/8">
          <CardTitle className="text-sm font-semibold">
            All references
            {refs.length > 0 && (
              <span className="ml-2 font-mono text-[10px] font-normal text-slate-400 dark:text-slate-500">
                {refs.length}
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? (
            <div className="flex h-24 items-center justify-center text-sm text-slate-500">Loading…</div>
          ) : error ? (
            <div className="px-5 py-4 text-sm text-rose-700 dark:text-rose-300">{error}</div>
          ) : refs.length === 0 ? (
            <div className="flex h-32 flex-col items-center justify-center text-center">
              <p className="text-sm text-slate-500 dark:text-slate-400 max-w-md">
                No references yet. They&apos;re extracted automatically when standards
                documents are uploaded — or you can create one manually.
              </p>
            </div>
          ) : (
            <ul className="divide-y divide-slate-200/60 dark:divide-white/8">
              {refs.map((r) => (
                <li
                  key={r.id}
                  className="flex items-start justify-between gap-3 px-5 py-3 transition-colors hover:bg-slate-50/80 dark:hover:bg-white/[0.03] cursor-pointer"
                  onClick={() => setEditing(r)}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="font-mono text-[10px] text-slate-400 dark:text-slate-500">{r.id}</span>
                      <span className="rounded-full bg-violet-50 px-2 py-0.5 text-[10px] font-medium text-violet-700 dark:bg-violet-500/10 dark:text-violet-300">
                        {HOSTING_SCOPE_LABELS[r.hosting_scope]}
                      </span>
                      {r.layer && (
                        <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-500/10 dark:text-amber-300">
                          {r.layer}
                        </span>
                      )}
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-white/8 dark:text-slate-300">
                        {tierLabel(r.applies_to_tiers)}
                      </span>
                    </div>
                    <p className="mt-1 text-sm font-medium text-slate-900 dark:text-slate-100">{r.name}</p>
                    <p className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-slate-500 dark:text-slate-400">
                      {r.body.slice(0, 200)}
                      {r.body.length > 200 ? "…" : ""}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* Edit dialog */}
      {editing && (
        <ReferenceEditDialog
          reference={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await reload();
          }}
          onDeleted={async () => {
            setEditing(null);
            await reload();
          }}
        />
      )}

      {/* Create dialog */}
      {showCreate && (
        <ReferenceEditDialog
          reference={null}
          onClose={() => setShowCreate(false)}
          onSaved={async () => {
            setShowCreate(false);
            await reload();
          }}
        />
      )}
    </div>
  );
}

function ReferenceEditDialog({
  reference,
  onClose,
  onSaved,
  onDeleted,
}: {
  reference: ReferenceItem | null;
  onClose: () => void;
  onSaved: () => Promise<void>;
  onDeleted?: () => Promise<void>;
}) {
  const isCreate = reference === null;
  const [name, setName] = useState(reference?.name || "");
  const [body, setBody] = useState(reference?.body || "");
  const [layer, setLayer] = useState<RuleLayer | "">((reference?.layer as RuleLayer) || "");
  const [hostingScope, setHostingScope] = useState<HostingScope>(
    reference?.hosting_scope || "both",
  );
  const [tiers, setTiers] = useState<number[]>(reference?.applies_to_tiers || [...ALL_TIERS]);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    setSaving(true);
    setErr(null);
    try {
      if (isCreate) {
        const body_: CreateReferenceRequest = {
          name: name.trim(),
          body,
          applies_to_tiers: tiers,
          layer: layer || null,
          hosting_scope: hostingScope,
        };
        await api.createReference(body_);
      } else {
        await api.updateReference(reference!.id, {
          name: name.trim(),
          body,
          applies_to_tiers: tiers,
          layer: layer || null,
          hosting_scope: hostingScope,
        });
      }
      await onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!reference) return;
    if (!window.confirm(`Delete reference ${reference.id}?`)) return;
    setSaving(true);
    try {
      await api.deleteReference(reference.id);
      await onDeleted?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Delete failed");
      setSaving(false);
    }
  }

  function toggleTier(t: number) {
    setTiers((prev) => (prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t].sort()));
  }

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="sm:max-w-3xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="text-sm">
            {isCreate ? "New reference" : `Edit ${reference!.id}`}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          {/* Name */}
          <label className="block">
            <span className="text-xs font-medium text-slate-700 dark:text-slate-300">Name</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1 block w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm dark:border-white/10 dark:bg-white/5"
              placeholder="e.g. Tier 1 datacenter peer table"
            />
          </label>

          {/* Tiers */}
          <div>
            <span className="text-xs font-medium text-slate-700 dark:text-slate-300">Applies to tiers</span>
            <div className="mt-1 flex gap-1">
              {ALL_TIERS.map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => toggleTier(t)}
                  className={
                    "rounded-md border px-2.5 py-1 text-xs font-medium transition-all " +
                    (tiers.includes(t)
                      ? "bg-[var(--color-brand-primary)] text-white border-[var(--color-brand-primary)]"
                      : "bg-white text-slate-600 border-slate-200 hover:border-slate-300 dark:bg-white/5 dark:text-slate-300 dark:border-white/10")
                  }
                >
                  T{t}
                </button>
              ))}
            </div>
          </div>

          {/* Layer + scope */}
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-xs font-medium text-slate-700 dark:text-slate-300">Layer</span>
              <select
                value={layer}
                onChange={(e) => setLayer(e.target.value as RuleLayer | "")}
                className="mt-1 block w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm dark:border-white/10 dark:bg-white/5"
              >
                <option value="">(any)</option>
                {LAYER_VALUES.map((l) => (
                  <option key={l} value={l}>{l}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-xs font-medium text-slate-700 dark:text-slate-300">Hosting scope</span>
              <select
                value={hostingScope}
                onChange={(e) => setHostingScope(e.target.value as HostingScope)}
                className="mt-1 block w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm dark:border-white/10 dark:bg-white/5"
              >
                {HOSTING_SCOPE_VALUES.map((s) => (
                  <option key={s} value={s}>{HOSTING_SCOPE_LABELS[s]}</option>
                ))}
              </select>
            </label>
          </div>

          {/* Body — split-pane raw + preview */}
          <div>
            <span className="text-xs font-medium text-slate-700 dark:text-slate-300">Body (markdown — tables preserved)</span>
            <div className="mt-1 grid grid-cols-2 gap-3">
              <textarea
                value={body}
                onChange={(e) => setBody(e.target.value)}
                className="h-72 rounded-md border border-slate-200 bg-white p-2 font-mono text-xs dark:border-white/10 dark:bg-white/5"
                placeholder="| Datacenters | A-A peer | DR site |\n| --- | --- | --- |\n| CTOS-1 | CTOS-2 | TX DC |"
              />
              <div className="h-72 overflow-y-auto rounded-md border border-slate-200 bg-slate-50 p-3 dark:border-white/10 dark:bg-white/[0.02]">
                <div className="prose prose-sm max-w-none dark:prose-invert prose-table:text-xs prose-th:bg-slate-100 prose-th:px-2 prose-th:py-1 prose-td:px-2 prose-td:py-1 prose-td:border prose-th:border prose-th:border-slate-200 prose-td:border-slate-200 dark:prose-th:bg-slate-800">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{body || "_preview_"}</ReactMarkdown>
                </div>
              </div>
            </div>
          </div>

          {err && <p className="text-xs text-rose-700 dark:text-rose-300">{err}</p>}
        </div>

        <DialogFooter>
          {!isCreate && (
            <Button variant="outline" size="sm" onClick={remove} disabled={saving}>
              Delete
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button size="sm" onClick={save} disabled={saving || !name.trim() || !body.trim()}>
            {saving ? "Saving…" : isCreate ? "Create" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
