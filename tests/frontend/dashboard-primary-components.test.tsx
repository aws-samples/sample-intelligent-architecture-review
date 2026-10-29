// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { afterEach, describe, expect, it, vi } from "vitest";
import React from "../../frontend/node_modules/react";
import {
  render,
  screen,
  within,
} from "../../frontend/node_modules/@testing-library/react";

import { PortfolioPulse } from "@/components/dashboard/PortfolioPulse";
import { PriorityReviewQueue } from "@/components/dashboard/PriorityReviewQueue";
import type {
  DashboardValidation,
  PriorityQueueItem,
} from "@/lib/dashboard-insights";

function validation(
  validation_id: string,
  document_id: string,
  overrides: Partial<DashboardValidation> = {},
): DashboardValidation {
  return {
    validation_id,
    document_id,
    status: "completed",
    started_at: "2026-07-27T09:00:00Z",
    completed_at: "2026-07-27T10:00:00Z",
    overall_score: 82,
    findings_count: 1,
    critical_count: 0,
    high_count: 0,
    ...overrides,
  };
}

function queueItem(
  document_id: string,
  overrides: Partial<PriorityQueueItem> = {},
): PriorityQueueItem {
  const completed = validation(`val-${document_id}`, document_id);

  return {
    document_id,
    filename: `${document_id}.pdf`,
    posture: "warning",
    active: false,
    score: completed.overall_score,
    critical_count: 0,
    high_count: 0,
    findings_count: 1,
    latestRun: completed,
    latestCompleted: completed,
    reason: "Score needs attention",
    ...overrides,
  };
}

const postureCounts = {
  pass: 5,
  warning: 2,
  fail: 1,
  active: 1,
  unvalidated: 2,
};

afterEach(() => {
  vi.useRealTimers();
});

describe("PortfolioPulse", () => {
  it("renders an unavailable pass rate without leaking null as a percentage", () => {
    const { container } = render(
      <PortfolioPulse
        passRate={null}
        coverageRate={0}
        postureCounts={{
          pass: 0,
          warning: 0,
          fail: 0,
          active: 0,
          unvalidated: 3,
        }}
        currentCritical={0}
        currentHigh={0}
        activeValidations={0}
        activeExtractions={0}
      />,
    );

    expect(screen.getByText("N/A")).toBeTruthy();
    expect(container.textContent).not.toContain("null%");
    expect(screen.getByText("No validated designs yet")).toBeTruthy();
  });

  it("combines known validation and extraction work accurately", () => {
    render(
      <PortfolioPulse
        passRate={72}
        coverageRate={78}
        postureCounts={postureCounts}
        currentCritical={2}
        currentHigh={3}
        activeValidations={2}
        activeExtractions={4}
      />,
    );

    const activeWork = screen.getByLabelText(
      "Active work: 2 validations and 4 extraction jobs, 6 total",
    );
    expect(activeWork.textContent).toContain("6 jobs");
    expect(activeWork.textContent).toContain("2 validations + 4 extractions");
  });

  it("uses singular labels for one validation and one extraction", () => {
    render(
      <PortfolioPulse
        passRate={72}
        coverageRate={78}
        postureCounts={postureCounts}
        currentCritical={2}
        currentHigh={3}
        activeValidations={1}
        activeExtractions={1}
      />,
    );

    const activeWork = screen.getByLabelText(
      "Active work: 1 validation and 1 extraction job, 2 total",
    );
    expect(activeWork.textContent).toContain("1 validation + 1 extraction");
    expect(activeWork.textContent).not.toContain("validations");
    expect(activeWork.textContent).not.toContain("extractions");
  });

  it("reports unknown extraction work without inventing a combined total", () => {
    render(
      <PortfolioPulse
        passRate={72}
        coverageRate={78}
        postureCounts={postureCounts}
        currentCritical={2}
        currentHigh={3}
        activeValidations={2}
        activeExtractions={null}
      />,
    );

    const activeWork = screen.getByLabelText(
      "Active work: 2 validations; extraction count unavailable",
    );
    expect(activeWork.textContent).toContain("2 validations");
    expect(activeWork.textContent).toContain("Extraction status unavailable");
    expect(activeWork.textContent).not.toContain("2 jobs");
  });

  it("labels the open metrics and every posture segment with its count", () => {
    render(
      <PortfolioPulse
        passRate={72}
        coverageRate={78}
        postureCounts={postureCounts}
        currentCritical={2}
        currentHigh={3}
        activeValidations={2}
        activeExtractions={0}
      />,
    );

    expect(screen.getByText("current portfolio posture")).toBeTruthy();
    expect(screen.getByText("5 of 8 validated designs passing")).toBeTruthy();
    expect(screen.getByLabelText("Coverage: 78%")).toBeTruthy();
    expect(
      screen.getByLabelText("Current risk: 2 critical and 3 high findings"),
    ).toBeTruthy();
    expect(
      screen.getByLabelText(
        "Portfolio posture: 5 passing, 2 warning, 1 failing, 1 active, 2 unvalidated",
      ),
    ).toBeTruthy();

    const legend = screen.getByRole("list", {
      name: "Portfolio posture legend",
    });
    for (const label of [
      "5 Passing",
      "2 Warning",
      "1 Failing",
      "1 Active",
      "2 Unvalidated",
    ]) {
      expect(within(legend).getByText(label)).toBeTruthy();
    }
  });
});

describe("PriorityReviewQueue", () => {
  it("routes reviewed and unreviewed documents to their exact destinations", () => {
    const reviewed = queueItem("reviewed", {
      latestCompleted: validation("completed-123", "reviewed"),
    });
    const unreviewed = queueItem("unreviewed", {
      posture: "unvalidated",
      score: null,
      latestRun: null,
      latestCompleted: null,
      reason: "No completed validation",
    });

    render(<PriorityReviewQueue items={[reviewed, unreviewed]} />);

    expect(
      screen
        .getByRole("link", { name: /Review reviewed\.pdf/ })
        .getAttribute("href"),
    ).toBe("/validation/detail?id=completed-123");
    expect(
      screen
        .getByRole("link", { name: /Start unreviewed\.pdf/ })
        .getAttribute("href"),
    ).toBe("/documents/detail?id=unreviewed");
  });

  it("routes a first-time active validation to its latest run", () => {
    const latestRun = validation("active-first-run", "active-first", {
      status: "in_progress",
      completed_at: null,
      overall_score: null,
    });
    const active = queueItem("active-first", {
      posture: "active",
      active: true,
      score: null,
      latestRun,
      latestCompleted: null,
      reason: "First validation in progress",
    });

    render(<PriorityReviewQueue items={[active]} />);

    expect(
      screen
        .getByRole("link", { name: "Open active-first.pdf" })
        .getAttribute("href"),
    ).toBe("/validation/detail?id=active-first-run");
  });

  it("routes an active rerun to the latest run instead of the older review", () => {
    const active = queueItem("active-rerun", {
      active: true,
      latestRun: validation("current-running", "active-rerun", {
        status: "running",
        completed_at: null,
        overall_score: null,
      }),
      latestCompleted: validation("older-completed", "active-rerun"),
      reason: "Validation in progress",
    });

    render(<PriorityReviewQueue items={[active]} />);

    expect(
      screen
        .getByRole("link", { name: "Open active-rerun.pdf" })
        .getAttribute("href"),
    ).toBe("/validation/detail?id=current-running");
  });

  it("defensively limits the queue to six rows", () => {
    render(
      <PriorityReviewQueue
        items={Array.from({ length: 8 }, (_, index) =>
          queueItem(`document-${index + 1}`),
        )}
      />,
    );

    expect(
      screen.getAllByRole("listitem", { name: /Priority review:/ }),
    ).toHaveLength(6);
    expect(screen.queryByText("document-7.pdf")).toBeNull();
  });

  it("shows semantic reasons, severity counts, relative age, and N/A scores", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-28T10:00:00Z"));

    const critical = queueItem("critical", {
      filename: "Customer Data Platform.pdf",
      score: null,
      critical_count: 2,
      findings_count: 2,
      latestCompleted: validation("critical-review", "critical", {
        completed_at: "2026-07-28T07:00:00Z",
        overall_score: null,
        critical_count: 2,
      }),
      reason: "Critical findings remain",
    });
    const high = queueItem("high", {
      filename: "Payment Gateway.pdf",
      high_count: 3,
      findings_count: 3,
      latestCompleted: validation("high-review", "high", {
        completed_at: "2026-07-27T10:00:00Z",
        high_count: 3,
      }),
      reason: "High-risk findings remain",
    });

    render(<PriorityReviewQueue items={[critical, high]} />);

    const criticalRow = screen.getByRole("listitem", {
      name: "Priority review: Customer Data Platform.pdf",
    });
    expect(criticalRow.textContent).toContain("Critical findings remain");
    expect(criticalRow.textContent).toContain("2 critical findings");
    expect(criticalRow.textContent).toContain("Reviewed 3h ago");
    expect(criticalRow.textContent).toContain("N/A");

    const highRow = screen.getByRole("listitem", {
      name: "Priority review: Payment Gateway.pdf",
    });
    expect(highRow.textContent).toContain("High-risk findings remain");
    expect(highRow.textContent).toContain("3 high findings");
    expect(highRow.textContent).toContain("Reviewed 1d ago");
  });

  it("marks scores by posture and presents Start as the primary action", () => {
    const activeCompleted = validation("active-completed", "active", {
      overall_score: 43,
    });
    const items: PriorityQueueItem[] = [
      queueItem("pass", { posture: "pass", score: 90 }),
      queueItem("warning", { posture: "warning", score: 89.6 }),
      queueItem("fail", { posture: "fail", score: 69.6 }),
      queueItem("active", {
        posture: "active",
        active: true,
        score: 43,
        latestRun: validation("active-running", "active", {
          status: "running",
          completed_at: null,
          overall_score: null,
        }),
        latestCompleted: activeCompleted,
      }),
      queueItem("unvalidated", {
        posture: "unvalidated",
        score: null,
        latestRun: null,
        latestCompleted: null,
      }),
    ];

    render(<PriorityReviewQueue items={items} />);

    for (const [documentId, posture, score] of [
      ["pass", "pass", "90%"],
      ["warning", "warning", "89.6%"],
      ["fail", "fail", "69.6%"],
      ["active", "active", "43%"],
      ["unvalidated", "unvalidated", "Not reviewed"],
    ] as const) {
      const row = screen.getByRole("listitem", {
        name: `Priority review: ${documentId}.pdf`,
      });
      expect(within(row).getByText(score).getAttribute("data-posture")).toBe(
        posture,
      );
    }

    const start = screen.getByRole("link", {
      name: "Start unvalidated.pdf",
    });
    expect(start.getAttribute("data-variant")).toBe("primary");

    for (const action of screen.getAllByRole("link", {
      name: /^(Review|Open|Start) /,
    })) {
      expect(action.querySelector('[data-icon="arrow-up-right"]')).toBeTruthy();
    }
  });

  it("uses Open for active work and distinguishes an unreviewed score", () => {
    const active = queueItem("active", {
      active: true,
      reason: "Validation in progress",
    });
    const unreviewed = queueItem("fresh", {
      posture: "unvalidated",
      score: null,
      latestRun: null,
      latestCompleted: null,
      reason: "No completed validation",
    });

    render(<PriorityReviewQueue items={[active, unreviewed]} />);

    expect(screen.getByRole("link", { name: /Open active\.pdf/ })).toBeTruthy();
    const freshRow = screen.getByRole("listitem", {
      name: "Priority review: fresh.pdf",
    });
    expect(freshRow.textContent).toContain("Not reviewed");
    expect(freshRow.textContent).toContain("Start");
  });

  it("renders an all-clear state with a link to all validations", () => {
    render(<PriorityReviewQueue items={[]} />);

    expect(screen.getByText("Portfolio is clear")).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: "View all validations" })
        .getAttribute("href"),
    ).toBe("/validation");
  });
});
