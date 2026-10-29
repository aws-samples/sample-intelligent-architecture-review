// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { Finding, Severity } from "@/lib/mock-data";
import type { StandardItem } from "@/lib/api";

interface ComplianceMatrixProps {
  findings: Finding[];
  standards: StandardItem[];
}

type GroupBy = "category" | "tier" | "layer";

/** Pretty label for any category/layer string. Falls back to title-casing
 *  the raw value (replacing underscores) so unknown values like "general"
 *  or "replication" still read like "General" / "Replication". */
function humanize(raw: string | null | undefined): string {
  if (!raw) return "Other";
  // Special-case the multi-word ones we want a specific label for.
  const overrides: Record<string, string> = {
    data: "Data Protection",
    traffic_management: "Traffic Management",
    load_balancer: "Load Balancer",
  };
  const key = raw.toLowerCase();
  if (overrides[key]) return overrides[key];
  return key
    .split(/[_\s-]+/)
    .map((w) => (w.length === 0 ? "" : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}

const severityOrder: Severity[] = ["critical", "high", "medium", "low"];

function getStatusForStandard(
  standard: StandardItem,
  findings: Finding[],
): "pass" | "fail" | "warning" | "not-checked" {
  const matchingFinding = findings.find((f) => {
    const reqLower = f.requirementId.toLowerCase();
    const idLower = standard.id.toLowerCase();
    return reqLower === idLower;
  });

  if (!matchingFinding) return "not-checked";
  if (matchingFinding.status === "pass_via_substitution") return "pass";
  if (matchingFinding.status === "not_applicable_via_exception") return "warning";
  // A rule the document is silent on is a coverage gap, not a pass/fail —
  // render it like an unchecked cell rather than lying about the verdict.
  if (matchingFinding.status === "not_addressed") return "not-checked";
  return matchingFinding.status as "pass" | "fail" | "warning";
}

function StatusIcon({ status }: { status: "pass" | "fail" | "warning" | "not-checked" }) {
  switch (status) {
    case "pass":
      return (
        <div className="flex items-center justify-center w-6 h-6 rounded-full bg-[var(--color-status-pass)]/10">
          <svg className="w-3.5 h-3.5 text-[var(--color-status-pass)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
          </svg>
        </div>
      );
    case "fail":
      return (
        <div className="flex items-center justify-center w-6 h-6 rounded-full bg-[var(--color-status-fail)]/10">
          <svg className="w-3.5 h-3.5 text-[var(--color-status-fail)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </div>
      );
    case "warning":
      return (
        <div className="flex items-center justify-center w-6 h-6 rounded-full bg-[var(--color-status-warning)]/10">
          <svg className="w-3.5 h-3.5 text-[var(--color-status-warning)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M12 9v2m0 4h.01" />
          </svg>
        </div>
      );
    default:
      return (
        <div className="flex items-center justify-center w-6 h-6 rounded-full bg-gray-100">
          <span className="text-gray-400 text-xs font-bold">--</span>
        </div>
      );
  }
}

/** Build the (group key → standards) map for the active grouping. The
 *  key is the raw value used for sorting / equality; we humanize on
 *  render via the heading. */
function groupStandards(
  standards: StandardItem[],
  groupBy: GroupBy,
): Map<string, StandardItem[]> {
  const out = new Map<string, StandardItem[]>();
  for (const s of standards) {
    let key: string;
    if (groupBy === "category") {
      key = s.category || "other";
    } else if (groupBy === "layer") {
      // RuleLayer field; falls back to "general" for legacy rows.
      key = (s as unknown as { layer?: string | null }).layer || "general";
    } else {
      // tier — collapse multi-tier rules under "Universal".
      const tiers = s.applies_to_tiers || [];
      if (!tiers || tiers.length === 0 || tiers.length === 5) {
        key = "universal";
      } else if (tiers.length === 1) {
        key = `t${tiers[0]}`;
      } else {
        key = `multi:${[...tiers].sort((a, b) => a - b).join(",")}`;
      }
    }
    if (!out.has(key)) out.set(key, []);
    out.get(key)!.push(s);
  }
  for (const items of out.values()) {
    items.sort(
      (a, b) =>
        severityOrder.indexOf(a.severity as Severity) -
        severityOrder.indexOf(b.severity as Severity),
    );
  }
  return out;
}

function groupHeading(key: string, groupBy: GroupBy): string {
  if (groupBy === "tier") {
    if (key === "universal") return "All tiers (universal)";
    if (key.startsWith("t")) return `Tier ${key.slice(1)}`;
    if (key.startsWith("multi:")) return `Tiers ${key.slice(6)}`;
    return key;
  }
  return humanize(key);
}

export function ComplianceMatrix({ findings, standards }: ComplianceMatrixProps) {
  const [groupBy, setGroupBy] = useState<GroupBy>("tier");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggle = (k: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  };

  const grouped = groupStandards(standards, groupBy);
  // Sort groups deterministically: tier numeric, otherwise alpha.
  const groupKeys = Array.from(grouped.keys()).sort((a, b) => {
    if (groupBy === "tier") {
      const order = (k: string) =>
        k === "universal" ? 999 : k.startsWith("t") ? Number(k.slice(1)) : 998;
      return order(a) - order(b);
    }
    return a.localeCompare(b);
  });

  const totalStandards = standards.length;
  let passCount = 0;
  let failCount = 0;
  let warningCount = 0;
  let notCheckedCount = 0;
  for (const s of standards) {
    const status = getStatusForStandard(s, findings);
    if (status === "pass") passCount++;
    else if (status === "fail") failCount++;
    else if (status === "warning") warningCount++;
    else notCheckedCount++;
  }

  if (standards.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Compliance Matrix</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-center py-8">
            <svg className="w-10 h-10 mx-auto text-muted-foreground/30 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
            </svg>
            <p className="text-xs text-muted-foreground">No standards available for compliance matrix.</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <CardTitle className="text-sm">Compliance Matrix</CardTitle>
          <div className="flex items-center gap-3">
            {/* Group-by toggle. Tier is the default because that's
                what users compare against; Category and Layer are the
                older lenses. */}
            <div className="inline-flex gap-0.5 rounded-md border border-border p-0.5 text-[11px]">
              {(["tier", "category", "layer"] as const).map((g) => (
                <button
                  key={g}
                  type="button"
                  onClick={() => {
                    setGroupBy(g);
                    setExpanded(new Set());
                  }}
                  className={
                    "rounded px-2 py-0.5 font-medium capitalize transition-colors " +
                    (groupBy === g
                      ? "bg-[var(--color-brand-primary)] text-white"
                      : "text-muted-foreground hover:bg-muted/50")
                  }
                >
                  {g === "tier" ? "By tier" : g === "category" ? "By category" : "By layer"}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
              <span className="flex items-center gap-1">
                <span className="h-2 w-2 rounded-full bg-[var(--color-status-pass)]" /> {passCount} Pass
              </span>
              <span className="flex items-center gap-1">
                <span className="h-2 w-2 rounded-full bg-[var(--color-status-fail)]" /> {failCount} Fail
              </span>
              <span className="flex items-center gap-1">
                <span className="h-2 w-2 rounded-full bg-[var(--color-status-warning)]" /> {warningCount} Warning
              </span>
              <span className="flex items-center gap-1">
                <span className="h-2 w-2 rounded-full bg-gray-300" /> {notCheckedCount} Not Checked
              </span>
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {totalStandards > 0 && (
          <div className="mb-4 space-y-1.5">
            <div className="flex items-center justify-between text-[10px] text-muted-foreground">
              <span>Coverage: {totalStandards - notCheckedCount} of {totalStandards} standards evaluated</span>
              <span>{Math.round(((totalStandards - notCheckedCount) / totalStandards) * 100)}%</span>
            </div>
            <div className="h-2 rounded-full overflow-hidden bg-gray-100 flex">
              {passCount > 0 && (
                <div className="bg-[var(--color-status-pass)] transition-all" style={{ width: `${(passCount / totalStandards) * 100}%` }} />
              )}
              {warningCount > 0 && (
                <div className="bg-[var(--color-status-warning)] transition-all" style={{ width: `${(warningCount / totalStandards) * 100}%` }} />
              )}
              {failCount > 0 && (
                <div className="bg-[var(--color-status-fail)] transition-all" style={{ width: `${(failCount / totalStandards) * 100}%` }} />
              )}
              {notCheckedCount > 0 && (
                <div className="bg-gray-200 transition-all" style={{ width: `${(notCheckedCount / totalStandards) * 100}%` }} />
              )}
            </div>
          </div>
        )}

        <div className="space-y-2">
          {groupKeys.map((key) => {
            const items = grouped.get(key)!;
            const isExpanded = expanded.has(key);
            const groupPass = items.filter((s) => getStatusForStandard(s, findings) === "pass").length;
            const groupFail = items.filter((s) => getStatusForStandard(s, findings) === "fail").length;
            const groupWarn = items.filter((s) => getStatusForStandard(s, findings) === "warning").length;
            return (
              <div key={key} className="rounded-lg border border-border overflow-hidden">
                <button
                  onClick={() => toggle(key)}
                  className="w-full flex items-center justify-between p-2.5 hover:bg-muted/30 transition-colors text-left"
                >
                  <div className="flex items-center gap-2">
                    <svg
                      className={`w-3 h-3 text-muted-foreground transition-transform ${isExpanded ? "rotate-90" : ""}`}
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                    </svg>
                    <span className="text-xs font-semibold text-[var(--color-ink)]">
                      {groupHeading(key, groupBy)}
                    </span>
                    <Badge variant="outline" className="text-[9px]">{items.length}</Badge>
                  </div>
                  <div className="flex items-center gap-2">
                    {groupPass > 0 && <span className="text-[10px] text-[var(--color-status-pass)] font-medium">{groupPass} pass</span>}
                    {groupWarn > 0 && <span className="text-[10px] text-[var(--color-status-warning)] font-medium">{groupWarn} warning</span>}
                    {groupFail > 0 && <span className="text-[10px] text-[var(--color-status-fail)] font-medium">{groupFail} fail</span>}
                  </div>
                </button>

                {isExpanded && (
                  <div className="border-t border-border">
                    <table className="w-full">
                      <thead>
                        <tr className="border-b border-border bg-muted/20">
                          <th className="text-left text-[10px] font-medium text-muted-foreground p-2 w-8">Status</th>
                          <th className="text-left text-[10px] font-medium text-muted-foreground p-2">Standard</th>
                          <th className="text-left text-[10px] font-medium text-muted-foreground p-2 w-20">Severity</th>
                        </tr>
                      </thead>
                      <tbody>
                        {items.map((standard) => {
                          const status = getStatusForStandard(standard, findings);
                          return (
                            <tr key={standard.id} className="border-b border-border last:border-b-0 hover:bg-muted/10">
                              <td className="p-2"><StatusIcon status={status} /></td>
                              <td className="p-2">
                                <div>
                                  <p className="text-xs font-medium text-[var(--color-ink)]">{standard.name}</p>
                                  <p className="text-[10px] text-muted-foreground line-clamp-1 mt-0.5">{standard.description}</p>
                                </div>
                              </td>
                              <td className="p-2">
                                <Badge
                                  variant="outline"
                                  className={`text-[9px] ${
                                    standard.severity === "critical"
                                      ? "bg-[var(--color-severity-critical)]/10 text-[var(--color-severity-critical)] border-[var(--color-severity-critical)]/20"
                                      : standard.severity === "high"
                                      ? "bg-[var(--color-severity-high)]/10 text-[var(--color-severity-high)] border-[var(--color-severity-high)]/20"
                                      : standard.severity === "medium"
                                      ? "bg-[var(--color-severity-medium)]/10 text-[var(--color-severity-medium)] border-[var(--color-severity-medium)]/20"
                                      : "bg-[var(--color-severity-low)]/10 text-[var(--color-severity-low)] border-[var(--color-severity-low)]/20"
                                  }`}
                                >
                                  {standard.severity}
                                </Badge>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
