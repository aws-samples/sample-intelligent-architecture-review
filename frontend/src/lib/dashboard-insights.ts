// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

export interface DashboardDocument {
  document_id: string;
  filename: string;
  status: string;
  uploaded_at: string;
}

export interface DashboardValidation {
  validation_id: string;
  document_id: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  overall_score: number | null;
  findings_count: number;
  critical_count: number;
  high_count: number;
  evaluated_count?: number;
  total_standards?: number;
}

export type DocumentPosture =
  | "pass"
  | "warning"
  | "fail"
  | "active"
  | "unvalidated";

export interface DocumentInsight {
  document: DashboardDocument;
  runs: DashboardValidation[];
  latestRun: DashboardValidation | null;
  latestCompleted: DashboardValidation | null;
  score: number | null;
  posture: DocumentPosture;
  active: boolean;
  reason: string;
}

export interface PriorityQueueItem {
  document_id: string;
  filename: string;
  posture: DocumentPosture;
  active: boolean;
  score: number | null;
  critical_count: number;
  high_count: number;
  findings_count: number;
  latestRun: DashboardValidation | null;
  latestCompleted: DashboardValidation | null;
  reason: string;
}

export interface DashboardInsights {
  documents: DocumentInsight[];
  documentNames: Readonly<Record<string, string>>;
  passRate: number | null;
  coverageRate: number;
  currentCritical: number;
  currentHigh: number;
  postureCounts: Record<DocumentPosture, number>;
  trend: DashboardValidation[];
  recentActivity: DashboardValidation[];
  priorityQueue: PriorityQueueItem[];
}

export function isActiveValidationStatus(status: string): boolean {
  return (
    status === "pending" ||
    status === "in_progress" ||
    status === "running"
  );
}

export function formatDashboardScore(score: number | null): string {
  if (typeof score !== "number" || !Number.isFinite(score)) return "N/A";
  return `${Object.is(score, -0) ? 0 : score}%`;
}

function startedAt(validation: DashboardValidation): number {
  const parsed = new Date(validation.started_at).getTime();
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function newestFirst(
  left: DashboardValidation,
  right: DashboardValidation,
): number {
  return (
    startedAt(right) - startedAt(left) ||
    left.validation_id.localeCompare(right.validation_id)
  );
}

function classifyPosture(
  latestRun: DashboardValidation | null,
  latestCompleted: DashboardValidation | null,
  active: boolean,
): Pick<DocumentInsight, "posture" | "reason"> {
  if (!latestCompleted) {
    return active
      ? { posture: "active", reason: "First validation in progress" }
      : { posture: "unvalidated", reason: "No completed validation" };
  }

  if (
    latestRun?.status === "failed" &&
    startedAt(latestRun) >= startedAt(latestCompleted)
  ) {
    return { posture: "fail", reason: "Latest validation failed" };
  }

  const score = latestCompleted.overall_score;
  if (
    score !== null &&
    score >= 90 &&
    latestCompleted.critical_count === 0 &&
    latestCompleted.high_count === 0
  ) {
    return { posture: "pass", reason: "Meets passing threshold" };
  }

  if (score !== null && score < 70) {
    return { posture: "fail", reason: "Score below 70" };
  }

  if (score === null) {
    return { posture: "warning", reason: "Score not available" };
  }

  if (latestCompleted.critical_count > 0) {
    return { posture: "warning", reason: "Critical findings remain" };
  }

  if (latestCompleted.high_count > 0) {
    return { posture: "warning", reason: "High-risk findings remain" };
  }

  return { posture: "warning", reason: "Score needs attention" };
}

function queueStateRank(item: PriorityQueueItem): number {
  if (item.latestRun?.status === "failed") return 0;
  if (
    typeof item.score === "number" &&
    Number.isFinite(item.score) &&
    item.score < 70
  ) {
    return 1;
  }
  if (item.posture === "warning") return 2;
  if (item.active || item.posture === "active") return 3;
  if (item.posture === "unvalidated") return 4;
  return 5;
}

function numericScoreRank(score: number | null): number {
  return typeof score === "number" && Number.isFinite(score)
    ? score
    : Number.POSITIVE_INFINITY;
}

function completedReviewTime(item: PriorityQueueItem): number {
  const reviewedAt =
    item.latestCompleted?.completed_at ?? item.latestCompleted?.started_at;
  if (!reviewedAt) return Number.POSITIVE_INFINITY;

  const parsed = new Date(reviewedAt).getTime();
  return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY;
}

function comparePriority(
  left: PriorityQueueItem,
  right: PriorityQueueItem,
): number {
  return (
    right.critical_count - left.critical_count ||
    right.high_count - left.high_count ||
    queueStateRank(left) - queueStateRank(right) ||
    completedReviewTime(left) - completedReviewTime(right) ||
    numericScoreRank(left.score) - numericScoreRank(right.score) ||
    left.document_id.localeCompare(right.document_id)
  );
}

export function deriveDashboardInsights(
  documents: DashboardDocument[],
  validations: DashboardValidation[],
): DashboardInsights {
  const knownDocumentIds = new Set(
    documents.map((document) => document.document_id),
  );
  const runsByDocument = new Map<string, DashboardValidation[]>(
    documents.map((item) => [item.document_id, []]),
  );
  const sortedValidations = validations
    .filter((validation) => knownDocumentIds.has(validation.document_id))
    .sort(newestFirst);

  for (const validation of sortedValidations) {
    runsByDocument.get(validation.document_id)?.push(validation);
  }

  const documentInsights = documents.map((item) => {
    const runs = runsByDocument.get(item.document_id)!;

    const latestRun = runs[0] ?? null;
    const latestCompleted =
      runs.find((validation) => validation.status === "completed") ?? null;
    const active = latestRun
      ? isActiveValidationStatus(latestRun.status)
      : false;
    const classification = classifyPosture(latestRun, latestCompleted, active);

    return {
      document: item,
      runs,
      latestRun,
      latestCompleted,
      score: latestCompleted?.overall_score ?? null,
      ...classification,
      active,
    };
  });
  const scoredDocuments = documentInsights.filter(
    (insight) =>
      typeof insight.latestCompleted?.overall_score === "number" &&
      Number.isFinite(insight.latestCompleted.overall_score),
  );
  const postureCounts: Record<DocumentPosture, number> = {
    pass: 0,
    warning: 0,
    fail: 0,
    active: 0,
    unvalidated: 0,
  };

  for (const insight of documentInsights) {
    postureCounts[insight.posture] += 1;
  }
  const priorityQueue = documentInsights
    .filter((insight) => insight.posture !== "pass" || insight.active)
    .map(
      (insight): PriorityQueueItem => ({
        document_id: insight.document.document_id,
        filename: insight.document.filename,
        posture: insight.posture,
        active: insight.active,
        score: insight.score,
        critical_count: insight.latestCompleted?.critical_count ?? 0,
        high_count: insight.latestCompleted?.high_count ?? 0,
        findings_count: insight.latestCompleted?.findings_count ?? 0,
        latestRun: insight.latestRun,
        latestCompleted: insight.latestCompleted,
        reason: insight.reason,
      }),
    )
    .sort(comparePriority)
    .slice(0, 6);

  return {
    documents: documentInsights,
    documentNames: Object.fromEntries(
      documents.map((item) => [item.document_id, item.filename]),
    ),
    passRate:
      scoredDocuments.length === 0
        ? null
        : Math.round(
            (scoredDocuments.filter((insight) => insight.posture === "pass")
              .length /
              scoredDocuments.length) *
              100,
          ),
    coverageRate:
      documents.length === 0
        ? 0
        : Math.round(
            (documentInsights.filter((insight) => insight.latestCompleted)
              .length /
              documents.length) *
              100,
          ),
    currentCritical: documentInsights.reduce(
      (total, insight) =>
        total + (insight.latestCompleted?.critical_count ?? 0),
      0,
    ),
    currentHigh: documentInsights.reduce(
      (total, insight) => total + (insight.latestCompleted?.high_count ?? 0),
      0,
    ),
    postureCounts,
    trend: sortedValidations
      .filter(
        (validation) =>
          validation.status === "completed" &&
          typeof validation.overall_score === "number" &&
          Number.isFinite(validation.overall_score),
      )
      .slice(0, 12)
      .reverse(),
    recentActivity: sortedValidations.slice(0, 5),
    priorityQueue,
  };
}
