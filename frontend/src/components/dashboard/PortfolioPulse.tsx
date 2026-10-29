// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import type { CSSProperties } from "react";

import type { DocumentPosture } from "@/lib/dashboard-insights";

interface PortfolioPulseProps {
  passRate: number | null;
  coverageRate: number;
  postureCounts: Record<DocumentPosture, number>;
  currentCritical: number;
  currentHigh: number;
  activeValidations: number;
  activeExtractions: number | null;
}

const POSTURES: ReadonlyArray<{
  key: DocumentPosture;
  label: string;
  railClassName: string;
  dotClassName: string;
}> = [
  {
    key: "pass",
    label: "Passing",
    railClassName: "bg-emerald-400",
    dotClassName: "bg-emerald-400",
  },
  {
    key: "warning",
    label: "Warning",
    railClassName: "bg-amber-400",
    dotClassName: "bg-amber-400",
  },
  {
    key: "fail",
    label: "Failing",
    railClassName: "bg-rose-500",
    dotClassName: "bg-rose-500",
  },
  {
    key: "active",
    label: "Active",
    railClassName: "bg-sky-400",
    dotClassName: "bg-sky-400",
  },
  {
    key: "unvalidated",
    label: "Unvalidated",
    railClassName: "bg-slate-400",
    dotClassName: "bg-slate-400",
  },
];

function countLabel(count: number, singular: string, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

export function PortfolioPulse({
  passRate,
  coverageRate,
  postureCounts,
  currentCritical,
  currentHigh,
  activeValidations,
  activeExtractions,
}: PortfolioPulseProps) {
  const validatedCount =
    postureCounts.pass + postureCounts.warning + postureCounts.fail;
  const postureTotal = POSTURES.reduce(
    (total, posture) => total + postureCounts[posture.key],
    0,
  );
  const displayedPassRate =
    passRate !== null && Number.isFinite(passRate) ? `${passRate}%` : "N/A";
  const postureLabel = `Portfolio posture: ${countLabel(postureCounts.pass, "passing", "passing")}, ${countLabel(postureCounts.warning, "warning", "warning")}, ${countLabel(postureCounts.fail, "failing", "failing")}, ${countLabel(postureCounts.active, "active", "active")}, ${countLabel(postureCounts.unvalidated, "unvalidated", "unvalidated")}`;
  const riskTotal = currentCritical + currentHigh;
  const activeWorkKnown = activeExtractions !== null;
  const activeWorkTotal = activeValidations + (activeExtractions ?? 0);
  const activeWorkLabel = activeWorkKnown
    ? `Active work: ${countLabel(activeValidations, "validation")} and ${countLabel(activeExtractions, "extraction job")}, ${activeWorkTotal} total`
    : `Active work: ${countLabel(activeValidations, "validation")}; extraction count unavailable`;

  return (
    <section
      aria-labelledby="portfolio-pulse-heading"
      className="relative isolate overflow-hidden rounded-xl bg-[var(--color-brand-ink)] p-6 text-white shadow-lg shadow-teal-950/15 sm:p-7"
    >
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -right-[15%] -top-[20%] -z-10 aspect-square w-4/5 rotate-12 rounded-[22%] bg-teal-700/15"
      />

      <h2
        id="portfolio-pulse-heading"
        className="text-xs font-semibold lowercase tracking-wide text-teal-200"
      >
        current portfolio posture
      </h2>
      <p className="mt-5 text-6xl font-semibold leading-none tracking-[-0.05em] text-white sm:text-7xl">
        {displayedPassRate}
      </p>
      <p className="mt-2 text-sm text-slate-300">
        {validatedCount === 0
          ? "No validated designs yet"
          : `${postureCounts.pass} of ${validatedCount} validated designs passing`}
      </p>

      <dl className="mt-6 grid grid-cols-1 border-y border-white/10 py-5 sm:grid-cols-3">
        <div
          aria-label={`Coverage: ${coverageRate}%`}
          className="border-white/10 py-2 sm:border-r sm:pr-4"
        >
          <dt className="text-[0.7rem] font-medium uppercase tracking-wide text-slate-400">
            Coverage
          </dt>
          <dd className="mt-1 text-xl font-semibold tabular-nums text-white">
            {coverageRate}%
          </dd>
        </div>

        <div
          aria-label={`Current risk: ${currentCritical} critical and ${currentHigh} high ${riskTotal === 1 ? "finding" : "findings"}`}
          className="border-t border-white/10 py-4 sm:border-r sm:border-t-0 sm:px-4 sm:py-2"
        >
          <dt className="text-[0.7rem] font-medium uppercase tracking-wide text-slate-400">
            Current risk
          </dt>
          <dd className="mt-1">
            <span className="block text-xl font-semibold tabular-nums text-white">
              {currentCritical} critical
            </span>
            <span className="mt-0.5 block text-xs tabular-nums text-orange-200">
              {currentHigh} high
            </span>
          </dd>
        </div>

        <div
          aria-label={activeWorkLabel}
          className="border-t border-white/10 pt-4 sm:border-t-0 sm:pl-4 sm:pt-2"
        >
          <dt className="text-[0.7rem] font-medium uppercase tracking-wide text-slate-400">
            Active work
          </dt>
          <dd className="mt-1">
            <span className="block text-xl font-semibold tabular-nums text-white">
              {activeWorkKnown
                ? countLabel(activeWorkTotal, "job")
                : countLabel(activeValidations, "validation")}
            </span>
            <span className="mt-0.5 block text-xs text-slate-400">
              {activeWorkKnown
                ? `${countLabel(activeValidations, "validation")} + ${countLabel(activeExtractions, "extraction")}`
                : "Extraction status unavailable"}
            </span>
          </dd>
        </div>
      </dl>

      <div
        aria-label={postureLabel}
        className="mt-6 flex h-2 overflow-hidden rounded-full bg-white/10"
        role="img"
      >
        {postureTotal === 0 ? (
          <span className="h-full flex-1 bg-slate-500/70" />
        ) : (
          POSTURES.map((posture) => {
            const count = postureCounts[posture.key];
            return count > 0 ? (
              <span
                aria-hidden="true"
                className={posture.railClassName}
                key={posture.key}
                style={{ flexGrow: count } as CSSProperties}
              />
            ) : null;
          })
        )}
      </div>

      <ul
        aria-label="Portfolio posture legend"
        className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-xs text-slate-300 sm:grid-cols-3"
      >
        {POSTURES.map((posture) => (
          <li className="flex items-center gap-2" key={posture.key}>
            <span
              aria-hidden="true"
              className={`h-2 w-2 shrink-0 rounded-full ${posture.dotClassName}`}
            />
            <span>
              {postureCounts[posture.key]} {posture.label}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
