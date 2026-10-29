// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from "vitest";

import {
  deriveDashboardInsights,
  type DashboardDocument,
  type DashboardValidation,
} from "@/lib/dashboard-insights";

function document(
  document_id: string,
  filename = `${document_id}.pdf`,
): DashboardDocument {
  return {
    document_id,
    filename,
    status: "parsed",
    uploaded_at: "2026-01-01T00:00:00Z",
  };
}

function validation(
  validation_id: string,
  document_id: string,
  started_at: string,
  overrides: Partial<DashboardValidation> = {},
): DashboardValidation {
  return {
    validation_id,
    document_id,
    status: "completed",
    started_at,
    completed_at: started_at,
    overall_score: 95,
    findings_count: 0,
    critical_count: 0,
    high_count: 0,
    ...overrides,
  };
}

async function dashboardHelpers() {
  return (await import("@/lib/dashboard-insights")) as typeof import("@/lib/dashboard-insights") & {
    formatDashboardScore?: (score: number | null) => string;
    isActiveValidationStatus?: (status: string) => boolean;
  };
}

describe("dashboard insight helpers", () => {
  it("recognizes canonical and legacy active validation statuses", async () => {
    const { isActiveValidationStatus } = await dashboardHelpers();

    expect(isActiveValidationStatus).toBeTypeOf("function");
    expect(isActiveValidationStatus?.("pending")).toBe(true);
    expect(isActiveValidationStatus?.("in_progress")).toBe(true);
    expect(isActiveValidationStatus?.("running")).toBe(true);
    expect(isActiveValidationStatus?.("completed")).toBe(false);
    expect(isActiveValidationStatus?.("failed")).toBe(false);
  });

  it("formats scores without rounding across thresholds", async () => {
    const { formatDashboardScore } = await dashboardHelpers();

    expect(formatDashboardScore).toBeTypeOf("function");
    expect(formatDashboardScore?.(90)).toBe("90%");
    expect(formatDashboardScore?.(89.6)).toBe("89.6%");
    expect(formatDashboardScore?.(69.6)).toBe("69.6%");
    expect(formatDashboardScore?.(null)).toBe("N/A");
  });
});

describe("deriveDashboardInsights", () => {
  it("selects latest runs by parsed started_at instead of API order", () => {
    const older = validation("older", "doc-1", "2026-01-02T00:00:00Z");
    const newestCompleted = validation(
      "newest-completed",
      "doc-1",
      "2026-01-04T00:00:00Z",
    );
    const newest = validation(
      "newest",
      "doc-1",
      "2026-01-05T00:00:00Z",
      { status: "running", completed_at: null, overall_score: null },
    );

    const result = deriveDashboardInsights(
      [document("doc-1")],
      [older, newest, newestCompleted],
    );

    expect(result.documents[0].runs.map((run) => run.validation_id)).toEqual([
      "newest",
      "newest-completed",
      "older",
    ]);
    expect(result.documents[0].latestRun).toBe(newest);
    expect(result.documents[0].latestCompleted).toBe(newestCompleted);
  });

  it("orders invalid started_at values consistently across permutations", () => {
    const newest = validation(
      "z-newest",
      "doc-1",
      "2026-01-03T00:00:00Z",
    );
    const older = validation("a-older", "doc-1", "2026-01-02T00:00:00Z");
    const invalid = validation("m-invalid", "doc-1", "not-a-date");
    const permutations = [
      [newest, older, invalid],
      [newest, invalid, older],
      [older, newest, invalid],
      [older, invalid, newest],
      [invalid, newest, older],
      [invalid, older, newest],
    ];

    const outputs = permutations.map((runs) => {
      const result = deriveDashboardInsights([document("doc-1")], runs);
      return {
        runs: result.documents[0].runs.map((run) => run.validation_id),
        recent: result.recentActivity.map((run) => run.validation_id),
        trend: result.trend.map((run) => run.validation_id),
      };
    });
    const expected = {
      runs: ["z-newest", "a-older", "m-invalid"],
      recent: ["z-newest", "a-older", "m-invalid"],
      trend: ["m-invalid", "a-older", "z-newest"],
    };

    expect(outputs).toEqual(Array.from({ length: 6 }, () => expected));
  });

  it("sums current risk from only the latest completed run per document", () => {
    const result = deriveDashboardInsights(
      [document("doc-1"), document("doc-2")],
      [
        validation("doc-1-old", "doc-1", "2026-01-01T00:00:00Z", {
          critical_count: 9,
          high_count: 8,
        }),
        validation("doc-2-current", "doc-2", "2026-01-03T00:00:00Z", {
          critical_count: 1,
          high_count: 2,
        }),
        validation("doc-1-current", "doc-1", "2026-01-04T00:00:00Z", {
          critical_count: 2,
          high_count: 3,
        }),
      ],
    );

    expect(result.currentCritical).toBe(3);
    expect(result.currentHigh).toBe(5);
  });

  it("preserves the completed score while a newer rerun is active", () => {
    const completed = validation(
      "completed",
      "doc-1",
      "2026-01-03T00:00:00Z",
      { overall_score: 84 },
    );
    const pending = validation(
      "pending",
      "doc-1",
      "2026-01-04T00:00:00Z",
      {
        status: "pending",
        completed_at: null,
        overall_score: null,
      },
    );

    const [insight] = deriveDashboardInsights(
      [document("doc-1")],
      [completed, pending],
    ).documents;

    expect(insight.latestRun).toBe(pending);
    expect(insight.latestCompleted).toBe(completed);
    expect(insight.score).toBe(84);
    expect(insight.active).toBe(true);
    expect(insight.posture).toBe("warning");
  });

  it("classifies pass, warning, fail, active, and unvalidated documents", () => {
    const documents = [
      document("pass"),
      document("warning"),
      document("low-score"),
      document("not-applicable"),
      document("active"),
      document("unvalidated"),
      document("failed-run"),
    ];
    const validations = [
      validation("pass", "pass", "2026-01-02T00:00:00Z", {
        overall_score: 90,
      }),
      validation("warning", "warning", "2026-01-02T00:00:00Z", {
        overall_score: 95,
        high_count: 1,
      }),
      validation("low-score", "low-score", "2026-01-02T00:00:00Z", {
        overall_score: 69,
      }),
      validation(
        "not-applicable",
        "not-applicable",
        "2026-01-02T00:00:00Z",
        { overall_score: null },
      ),
      validation("active", "active", "2026-01-02T00:00:00Z", {
        status: "in_progress",
        completed_at: null,
        overall_score: null,
      }),
      validation(
        "failed-completed",
        "failed-run",
        "2026-01-02T00:00:00Z",
      ),
      validation("failed-latest", "failed-run", "2026-01-03T00:00:00Z", {
        status: "failed",
        completed_at: null,
        overall_score: null,
      }),
    ];

    const result = deriveDashboardInsights(documents, validations);
    const postures = Object.fromEntries(
      result.documents.map(
        (insight) => [insight.document.document_id, insight.posture],
      ),
    );

    expect(postures).toEqual({
      pass: "pass",
      warning: "warning",
      "low-score": "fail",
      "not-applicable": "warning",
      active: "active",
      unvalidated: "unvalidated",
      "failed-run": "fail",
    });
    expect(
      result.priorityQueue.find((item) => item.document_id === "active"),
    ).toMatchObject({
      active: true,
      posture: "active",
      latestRun: { status: "in_progress" },
    });
  });

  it("excludes N/A scores from pass rate while counting completed coverage", () => {
    const result = deriveDashboardInsights(
      [
        document("pass"),
        document("warning"),
        document("not-applicable"),
        document("unvalidated"),
      ],
      [
        validation("pass", "pass", "2026-01-02T00:00:00Z"),
        validation("warning", "warning", "2026-01-02T00:00:00Z", {
          overall_score: 95,
          critical_count: 1,
        }),
        validation(
          "not-applicable",
          "not-applicable",
          "2026-01-02T00:00:00Z",
          { overall_score: null },
        ),
      ],
    );

    expect(result.passRate).toBe(50);
    expect(result.coverageRate).toBe(75);
    expect(result.postureCounts).toEqual({
      pass: 1,
      warning: 2,
      fail: 0,
      active: 0,
      unvalidated: 1,
    });

    const empty = deriveDashboardInsights([], []);
    expect(empty.passRate).toBeNull();
    expect(empty.coverageRate).toBe(0);
    expect(empty.postureCounts).toEqual({
      pass: 0,
      warning: 0,
      fail: 0,
      active: 0,
      unvalidated: 0,
    });
  });

  it("builds bounded trend and recent activity from globally sorted runs", () => {
    const scoredRuns = Array.from({ length: 13 }, (_, index) =>
      validation(
        `scored-${index + 1}`,
        "doc-1",
        `2026-01-${String(index + 1).padStart(2, "0")}T00:00:00Z`,
        { overall_score: 70 + index },
      ),
    );
    const pending = validation(
      "pending",
      "doc-1",
      "2026-01-14T00:00:00Z",
      { status: "pending", completed_at: null, overall_score: null },
    );
    const notApplicable = validation(
      "not-applicable",
      "doc-1",
      "2026-01-15T00:00:00Z",
      { overall_score: null },
    );

    const result = deriveDashboardInsights(
      [document("doc-1", "Architecture.pdf")],
      [scoredRuns[5], pending, ...scoredRuns.slice(0, 5), notApplicable, ...scoredRuns.slice(6)],
    );

    expect(result.trend.map((run) => run.validation_id)).toEqual(
      Array.from({ length: 12 }, (_, index) => `scored-${index + 2}`),
    );
    expect(result.recentActivity.map((run) => run.validation_id)).toEqual([
      "not-applicable",
      "pending",
      "scored-13",
      "scored-12",
      "scored-11",
    ]);
    expect(result.documentNames).toEqual({ "doc-1": "Architecture.pdf" });
  });

  it("ranks a latest terminal failure ahead of a low-score failure", () => {
    const result = deriveDashboardInsights(
      [document("terminal-failure"), document("score-failure")],
      [
        validation(
          "terminal-completed",
          "terminal-failure",
          "2026-01-01T00:00:00Z",
        ),
        validation(
          "terminal-failed",
          "terminal-failure",
          "2026-01-03T00:00:00Z",
          {
            status: "failed",
            completed_at: null,
            overall_score: null,
          },
        ),
        validation(
          "score-failure",
          "score-failure",
          "2026-01-02T00:00:00Z",
          { overall_score: 60 },
        ),
      ],
    );

    expect(result.priorityQueue.map((item) => item.document_id)).toEqual([
      "terminal-failure",
      "score-failure",
    ]);
  });

  it("ranks older reviews before lower scores in the same warning band", () => {
    const result = deriveDashboardInsights(
      [document("newer-lower"), document("older-higher")],
      [
        validation(
          "newer-lower",
          "newer-lower",
          "2026-01-02T00:00:00Z",
          { overall_score: 75 },
        ),
        validation(
          "older-higher",
          "older-higher",
          "2026-01-01T00:00:00Z",
          { overall_score: 85 },
        ),
      ],
    );

    expect(result.priorityQueue.map((item) => item.document_id)).toEqual([
      "older-higher",
      "newer-lower",
    ]);
  });

  it("produces a deterministic six-row priority queue", () => {
    const documents = [
      document("critical-2"),
      document("critical-1"),
      document("failed"),
      document("warning-low"),
      document("warning-high"),
      document("pass-active"),
      document("active"),
      document("unvalidated-a"),
      document("unvalidated-b"),
      document("pass"),
    ];
    const validations = [
      validation("critical-2", "critical-2", "2026-01-02T00:00:00Z", {
        overall_score: 85,
        critical_count: 2,
      }),
      validation("critical-1", "critical-1", "2026-01-02T00:00:00Z", {
        overall_score: 85,
        critical_count: 1,
        high_count: 9,
      }),
      validation("failed-completed", "failed", "2026-01-01T00:00:00Z"),
      validation("failed-latest", "failed", "2026-01-08T00:00:00Z", {
        status: "failed",
        completed_at: null,
        overall_score: null,
      }),
      validation("warning-low", "warning-low", "2026-01-02T00:00:00Z", {
        overall_score: 75,
      }),
      validation("warning-high", "warning-high", "2026-01-01T00:00:00Z", {
        overall_score: 85,
      }),
      validation("pass-active-completed", "pass-active", "2026-01-01T00:00:00Z"),
      validation("pass-active-pending", "pass-active", "2026-01-09T00:00:00Z", {
        status: "pending",
        completed_at: null,
        overall_score: null,
      }),
      validation("active", "active", "2026-01-10T00:00:00Z", {
        status: "running",
        completed_at: null,
        overall_score: null,
      }),
      validation("pass", "pass", "2026-01-03T00:00:00Z"),
    ];
    const expected = [
      "critical-2",
      "critical-1",
      "failed",
      "warning-high",
      "warning-low",
      "pass-active",
    ];

    const result = deriveDashboardInsights(documents, validations);
    const reversed = deriveDashboardInsights(
      [...documents].reverse(),
      [...validations].reverse(),
    );

    expect(result.priorityQueue.map((item) => item.document_id)).toEqual(
      expected,
    );
    expect(reversed.priorityQueue.map((item) => item.document_id)).toEqual(
      expected,
    );
    expect(result.priorityQueue).toHaveLength(6);
    expect(result.priorityQueue.some((item) => item.document_id === "pass")).toBe(
      false,
    );

    const stateOrder = deriveDashboardInsights(
      [document("unvalidated"), document("active")],
      [
        validation("active", "active", "2026-01-01T00:00:00Z", {
          status: "pending",
          completed_at: null,
          overall_score: null,
        }),
      ],
    );
    expect(stateOrder.priorityQueue.map((item) => item.document_id)).toEqual([
      "active",
      "unvalidated",
    ]);

    const reviewAgeOrder = deriveDashboardInsights(
      [document("a-new-review"), document("z-old-review")],
      [
        validation(
          "new-review",
          "a-new-review",
          "2026-01-02T00:00:00Z",
          { overall_score: 80 },
        ),
        validation(
          "old-review",
          "z-old-review",
          "2026-01-01T00:00:00Z",
          { overall_score: 80 },
        ),
      ],
    );
    expect(
      reviewAgeOrder.priorityQueue.map((item) => item.document_id),
    ).toEqual(["z-old-review", "a-new-review"]);
  });

  it("does not create document posture from unknown validation document IDs", () => {
    const result = deriveDashboardInsights(
      [document("known")],
      [
        validation("orphan", "missing", "2026-01-02T00:00:00Z", {
          overall_score: 10,
          critical_count: 7,
          high_count: 6,
        }),
      ],
    );

    expect(result.documents).toHaveLength(1);
    expect(result.documents[0].document.document_id).toBe("known");
    expect(result.documents[0].posture).toBe("unvalidated");
    expect(result.currentCritical).toBe(0);
    expect(result.currentHigh).toBe(0);
    expect(result.coverageRate).toBe(0);
    expect(result.trend).toEqual([]);
    expect(result.recentActivity).toEqual([]);
    expect(result.priorityQueue.map((item) => item.document_id)).toEqual([
      "known",
    ]);
    expect(result.postureCounts).toEqual({
      pass: 0,
      warning: 0,
      fail: 0,
      active: 0,
      unvalidated: 1,
    });
  });
});
