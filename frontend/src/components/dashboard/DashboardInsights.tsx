// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import type { CSSProperties } from "react";

import {
  formatDashboardScore,
  type DashboardValidation,
  type DocumentPosture,
} from "@/lib/dashboard-insights";

interface DashboardInsightsProps {
  postureCounts: Record<DocumentPosture, number>;
  trend: DashboardValidation[];
}

const POSTURES: ReadonlyArray<{
  key: DocumentPosture;
  label: string;
  railClassName: string;
  dotClassName: string;
}> = [
  {
    key: "pass",
    label: "Pass",
    railClassName: "bg-emerald-600",
    dotClassName: "bg-emerald-600",
  },
  {
    key: "warning",
    label: "Warning",
    railClassName: "bg-amber-500",
    dotClassName: "bg-amber-500",
  },
  {
    key: "fail",
    label: "Fail",
    railClassName: "bg-rose-600",
    dotClassName: "bg-rose-600",
  },
  {
    key: "active",
    label: "Active",
    railClassName: "bg-sky-600",
    dotClassName: "bg-sky-600",
  },
  {
    key: "unvalidated",
    label: "Unvalidated",
    railClassName: "bg-slate-400",
    dotClassName: "bg-slate-400",
  },
];

const DATE_FORMATTER = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

type ScoredValidation = DashboardValidation & { overall_score: number };

function isScored(validation: DashboardValidation): validation is ScoredValidation {
  return (
    typeof validation.overall_score === "number" &&
    Number.isFinite(validation.overall_score)
  );
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime())
    ? DATE_FORMATTER.format(parsed)
    : "Date unavailable";
}

function formatDelta(delta: number): string {
  const displayDelta = Math.round(delta * 100) / 100;
  if (displayDelta > 0) return `+${displayDelta}`;
  return `${displayDelta}`;
}

function posturePercentage(count: number, total: number): number {
  return total === 0 ? 0 : Math.round((count / total) * 100);
}

function PostureDistribution({
  postureCounts,
}: Pick<DashboardInsightsProps, "postureCounts">) {
  const total = POSTURES.reduce(
    (sum, posture) => sum + postureCounts[posture.key],
    0,
  );
  const postureEntries = POSTURES.map((posture) => {
    const count = postureCounts[posture.key];
    return {
      ...posture,
      count,
      percentage: posturePercentage(count, total),
    };
  });
  const railLabel = `Current posture: ${postureEntries
    .map(
      (posture) =>
        `${posture.count} ${posture.label} (${posture.percentage}%)`,
    )
    .join(", ")}`;

  return (
    <article className="rounded-xl border border-slate-200/80 bg-white p-6 shadow-sm shadow-slate-900/[0.03] dark:border-white/10 dark:bg-white/[0.03]">
      <h3 className="text-sm font-semibold text-slate-950 dark:text-slate-50">
        Current document posture
      </h3>
      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
        Latest completed review per system design
      </p>

      <div
        aria-label={railLabel}
        className="mt-7 flex h-3 overflow-hidden rounded-full bg-slate-100 dark:bg-white/10"
        role="img"
      >
        {total === 0 ? (
          <span
            aria-hidden="true"
            className="h-full w-full bg-slate-200 dark:bg-slate-700"
          />
        ) : (
          postureEntries.map((posture) =>
            posture.count > 0 ? (
              <span
                aria-hidden="true"
                className={posture.railClassName}
                key={posture.key}
                style={
                  {
                    width: `${(posture.count / total) * 100}%`,
                  } as CSSProperties
                }
              />
            ) : null,
          )
        )}
      </div>

      {total === 0 ? (
        <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
          No documents in the portfolio yet.
        </p>
      ) : null}

      <ul
        aria-label="Current document posture counts"
        className="mt-6 grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 xl:grid-cols-5"
      >
        {postureEntries.map((posture) => (
          <li className="min-w-0" key={posture.key}>
            <span
              aria-hidden="true"
              className={`mb-2 block h-2 w-2 rounded-full ${posture.dotClassName}`}
            />
            <span className="break-words text-sm font-semibold tabular-nums text-slate-900 dark:text-slate-100">
              {posture.count} {posture.label} ({posture.percentage}%)
            </span>
          </li>
        ))}
      </ul>
    </article>
  );
}

function ReviewTrend({ trend }: Pick<DashboardInsightsProps, "trend">) {
  const points = trend.filter(isScored);

  if (points.length < 2) {
    return (
      <article className="rounded-xl border border-slate-200/80 bg-white p-6 shadow-sm shadow-slate-900/[0.03] dark:border-white/10 dark:bg-white/[0.03]">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="text-sm font-semibold text-slate-950 dark:text-slate-50">
              Review score history
            </h3>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              Historical completed scored runs
            </p>
          </div>
          <p className="text-sm font-semibold tabular-nums text-slate-400">
            N/A
          </p>
        </div>
        <div className="mt-7 flex min-h-36 items-center justify-center rounded-lg border border-dashed border-slate-200 bg-slate-50/60 px-6 text-center text-xs text-slate-500 dark:border-white/10 dark:bg-white/[0.02] dark:text-slate-400">
          At least two scored runs are needed to show history.
        </div>
      </article>
    );
  }

  const latest = points.at(-1)!;
  const oldest = points[0];
  const oldestDate = formatDate(oldest.started_at);
  const newestDate = formatDate(latest.started_at);
  const latestScore = formatDashboardScore(latest.overall_score);
  const delta = latest.overall_score - oldest.overall_score;
  const left = 16;
  const right = 584;
  const top = 10;
  const bottom = 164;
  const chartHeight = bottom - top;
  const positions = points.map((point, index) => {
    const score = Math.min(100, Math.max(0, point.overall_score));
    return {
      x: left + (index / (points.length - 1)) * (right - left),
      y: bottom - (score / 100) * chartHeight,
    };
  });
  const linePath = positions
    .map(({ x, y }, index) => `${index === 0 ? "M" : "L"} ${x} ${y}`)
    .join(" ");
  const areaPath = `M ${positions[0].x} ${bottom} ${positions
    .map(({ x, y }) => `L ${x} ${y}`)
    .join(" ")} L ${positions.at(-1)!.x} ${bottom} Z`;
  const chartLabel = `Review score history from ${oldestDate} to ${newestDate}; latest score ${latestScore.replace("%", " percent")}; ${formatDelta(delta)} point change over ${points.length} runs`;

  return (
    <article className="rounded-xl border border-slate-200/80 bg-white p-6 shadow-sm shadow-slate-900/[0.03] dark:border-white/10 dark:bg-white/[0.03]">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold text-slate-950 dark:text-slate-50">
            Review score history
          </h3>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Historical completed scored runs
          </p>
        </div>
        <div className="shrink-0 text-right">
          <p className="text-sm font-semibold tabular-nums text-slate-900 dark:text-slate-100">
            Latest score {latestScore}
          </p>
          <p
            className={`mt-1 text-xs font-semibold tabular-nums ${
              delta > 0
                ? "text-emerald-700 dark:text-emerald-300"
                : delta < 0
                  ? "text-rose-600 dark:text-rose-300"
                  : "text-slate-500 dark:text-slate-400"
            }`}
          >
            {formatDelta(delta)} pts over {points.length} runs
          </p>
        </div>
      </div>

      <svg
        aria-label={chartLabel}
        className="mt-5 aspect-[10/3] w-full overflow-visible"
        role="img"
        viewBox="0 0 600 180"
      >
        <title>Review score history</title>
        <desc>
          Historical completed scored runs on a fixed zero to one hundred scale,
          with thresholds at 70 and 90.
        </desc>
        {[70, 90].map((threshold) => {
          const y = bottom - (threshold / 100) * chartHeight;
          return (
            <g key={threshold}>
              <line
                className="stroke-slate-300 dark:stroke-slate-600"
                data-threshold={threshold}
                strokeDasharray="5 6"
                vectorEffect="non-scaling-stroke"
                x1={left}
                x2={right}
                y1={y}
                y2={y}
              />
              <text
                className="fill-slate-400 text-[10px] dark:fill-slate-500"
                x={right - 3}
                y={y - 5}
              >
                {threshold}
              </text>
            </g>
          );
        })}
        <path
          className="fill-teal-600/10 dark:fill-teal-300/10"
          d={areaPath}
        />
        <path
          className="fill-none stroke-teal-700 dark:stroke-teal-300"
          d={linePath}
          data-series="review-score"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="2.5"
          vectorEffect="non-scaling-stroke"
        />
        {positions.map(({ x, y }, index) => (
          <circle
            className="fill-white stroke-teal-700 dark:fill-slate-900 dark:stroke-teal-300"
            cx={x}
            cy={y}
            key={points[index].validation_id}
            r={index === positions.length - 1 ? 4 : 2.5}
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>

      <div className="mt-1 flex justify-between gap-4 text-xs text-slate-500 dark:text-slate-400">
        <time dateTime={oldest.started_at}>{oldestDate}</time>
        <time dateTime={latest.started_at}>{newestDate}</time>
      </div>
    </article>
  );
}

export function DashboardInsights({
  postureCounts,
  trend,
}: DashboardInsightsProps) {
  return (
    <section aria-labelledby="portfolio-insights-heading">
      <header>
        <h2
          id="portfolio-insights-heading"
          className="text-xl font-semibold tracking-tight text-slate-950 dark:text-slate-50"
        >
          Portfolio insights
        </h2>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Current document posture and historical review movement.
        </p>
      </header>

      <div className="mt-5 grid gap-5 lg:grid-cols-2">
        <PostureDistribution postureCounts={postureCounts} />
        <ReviewTrend trend={trend} />
      </div>
    </section>
  );
}
