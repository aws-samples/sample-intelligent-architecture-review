// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

interface ComplianceScoreProps {
  // null = not applicable (no evaluable rules). Rendered as "N/A", NOT 0% —
  // a doc that matched no rules is not 0% compliant.
  score: number | null;
  size?: "sm" | "lg";
}

function getScoreColor(score: number): string {
  if (score >= 90) return "var(--color-status-pass)";
  if (score >= 70) return "var(--color-status-warning)";
  return "var(--color-status-fail)";
}

export function ComplianceScore({ score, size = "sm" }: ComplianceScoreProps) {
  const isNA = score === null || score === undefined;
  const color = isNA ? "var(--color-status-warning)" : getScoreColor(score);
  const radius = size === "lg" ? 70 : 36;
  const strokeWidth = size === "lg" ? 8 : 5;
  const circumference = 2 * Math.PI * radius;
  // N/A shows an empty ring (no progress arc) rather than a full/empty score.
  const offset = isNA ? circumference : circumference - (score / 100) * circumference;
  const svgSize = size === "lg" ? 180 : 90;
  const center = svgSize / 2;

  return (
    <div className="relative inline-flex items-center justify-center">
      <svg width={svgSize} height={svgSize} className="-rotate-90">
        <circle
          cx={center}
          cy={center}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          className="text-muted/50"
        />
        <circle
          cx={center}
          cy={center}
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          className="transition-all duration-700 ease-out"
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span
          className={`font-bold ${isNA ? (size === "lg" ? "text-2xl" : "text-xs") : size === "lg" ? "text-3xl" : "text-sm"}`}
          style={{ color }}
        >
          {isNA ? "N/A" : `${score}%`}
        </span>
        {size === "lg" && (
          <span className="text-xs text-muted-foreground mt-0.5">
            {isNA ? "No applicable rules" : "Compliance"}
          </span>
        )}
      </div>
    </div>
  );
}
