// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { afterEach, describe, expect, it, vi } from "vitest";
import React from "../../frontend/node_modules/react";
import {
  render,
  screen,
  within,
} from "../../frontend/node_modules/@testing-library/react";

import { DashboardInsights } from "@/components/dashboard/DashboardInsights";
import { RecentActivity } from "@/components/dashboard/RecentActivity";
import type { DashboardValidation } from "@/lib/dashboard-insights";

function validation(
  validation_id: string,
  document_id: string,
  overrides: Partial<DashboardValidation> = {},
): DashboardValidation {
  return {
    validation_id,
    document_id,
    status: "completed",
    started_at: "2026-07-28T08:00:00Z",
    completed_at: "2026-07-28T09:00:00Z",
    overall_score: 82,
    findings_count: 1,
    critical_count: 0,
    high_count: 0,
    evaluated_count: 18,
    total_standards: 42,
    ...overrides,
  };
}

const postureCounts = {
  pass: 5,
  warning: 2,
  fail: 1,
  active: 3,
  unvalidated: 4,
};

afterEach(() => {
  vi.useRealTimers();
});

describe("DashboardInsights", () => {
  it("renders the five current posture counts with non-color labels", () => {
    render(<DashboardInsights postureCounts={postureCounts} trend={[]} />);

    expect(
      screen.getByRole("heading", { name: "Portfolio insights" }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Current document posture and historical review movement.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Current document posture" }),
    ).toBeTruthy();
    expect(
      screen.getByText("Latest completed review per system design"),
    ).toBeTruthy();

    const legend = screen.getByRole("list", {
      name: "Current document posture counts",
    });
    for (const label of [
      "5 Pass (33%)",
      "2 Warning (13%)",
      "1 Fail (7%)",
      "3 Active (20%)",
      "4 Unvalidated (27%)",
    ]) {
      expect(within(legend).getByText(label)).toBeTruthy();
    }
    expect(legend.className).toContain("grid-cols-2");
    expect(legend.className).toContain("sm:grid-cols-3");
    expect(legend.className).toContain("xl:grid-cols-5");
    expect(legend.className).not.toContain("sm:grid-cols-5");
    expect(within(legend).getByText(/Unvalidated/).className).toContain(
      "break-words",
    );
    expect(
      screen.getByRole("img", {
        name: "Current posture: 5 Pass (33%), 2 Warning (13%), 1 Fail (7%), 3 Active (20%), 4 Unvalidated (27%)",
      }),
    ).toBeTruthy();
  });

  it("keeps the zero-total posture rail finite and accessibly labeled", () => {
    const { container } = render(
      <DashboardInsights
        postureCounts={{
          pass: 0,
          warning: 0,
          fail: 0,
          active: 0,
          unvalidated: 0,
        }}
        trend={[]}
      />,
    );

    const rail = screen.getByRole("img", {
      name: "Current posture: 0 Pass (0%), 0 Warning (0%), 0 Fail (0%), 0 Active (0%), 0 Unvalidated (0%)",
    });
    expect(rail.getAttribute("aria-label")).toBeTruthy();
    expect(container.innerHTML).not.toContain("NaN");
    expect(container.innerHTML).not.toContain("Infinity");
    expect(screen.getByText("No documents in the portfolio yet.")).toBeTruthy();
    const legend = screen.getByRole("list", {
      name: "Current document posture counts",
    });
    for (const label of [
      "0 Pass (0%)",
      "0 Warning (0%)",
      "0 Fail (0%)",
      "0 Active (0%)",
      "0 Unvalidated (0%)",
    ]) {
      expect(within(legend).getByText(label)).toBeTruthy();
    }
  });

  it("renders an N/A trend state when fewer than two scored runs exist", () => {
    render(
      <DashboardInsights
        postureCounts={postureCounts}
        trend={[validation("single", "doc-1", { overall_score: 91 })]}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Review score history" }),
    ).toBeTruthy();
    expect(screen.getByText("Historical completed scored runs")).toBeTruthy();
    expect(screen.getByText("N/A")).toBeTruthy();
    expect(
      screen.getByText("At least two scored runs are needed to show history."),
    ).toBeTruthy();
    expect(
      screen.queryByRole("img", { name: /Review score history from/ }),
    ).toBeNull();
  });

  it("labels a populated fixed-scale chart, thresholds, dates, latest score, and delta", () => {
    const { container } = render(
      <DashboardInsights
        postureCounts={postureCounts}
        trend={[
          validation("oldest", "doc-1", {
            started_at: "2026-07-01T08:00:00Z",
            overall_score: 78,
          }),
          validation("middle", "doc-2", {
            started_at: "2026-07-02T08:00:00Z",
            overall_score: 88,
          }),
          validation("newest", "doc-3", {
            started_at: "2026-07-03T08:00:00Z",
            overall_score: 92,
          }),
        ]}
      />,
    );

    const chart = screen.getByRole("img", {
      name: /Review score history from Jul 1, 2026 to Jul 3, 2026/,
    });
    expect(chart.getAttribute("viewBox")).toBe("0 0 600 180");
    expect(chart.querySelector("title")?.textContent).toBe(
      "Review score history",
    );
    expect(chart.querySelector("desc")?.textContent).toContain(
      "Historical completed scored runs",
    );
    const threshold70 = container.querySelector('[data-threshold="70"]');
    const threshold90 = container.querySelector('[data-threshold="90"]');
    expect(Number(threshold70?.getAttribute("y1"))).toBeCloseTo(56.2, 5);
    expect(Number(threshold70?.getAttribute("y2"))).toBeCloseTo(56.2, 5);
    expect(Number(threshold90?.getAttribute("y1"))).toBeCloseTo(25.4, 5);
    expect(Number(threshold90?.getAttribute("y2"))).toBeCloseTo(25.4, 5);
    expect(
      container
        .querySelector('[data-series="review-score"]')
        ?.getAttribute("vector-effect"),
    ).toBe("non-scaling-stroke");
    expect(screen.getByText("Latest score 92%")).toBeTruthy();
    expect(screen.getByText("+14 pts over 3 runs")).toBeTruthy();
    expect(screen.getByText("Jul 1, 2026")).toBeTruthy();
    expect(screen.getByText("Jul 3, 2026")).toBeTruthy();
  });

  it("clamps out-of-range scores to the fixed zero-to-one-hundred chart scale", () => {
    const { container } = render(
      <DashboardInsights
        postureCounts={postureCounts}
        trend={[
          validation("below-zero", "doc-1", {
            started_at: "2026-07-01T08:00:00Z",
            overall_score: -4,
          }),
          validation("above-one-hundred", "doc-2", {
            started_at: "2026-07-02T08:00:00Z",
            overall_score: 104,
          }),
        ]}
      />,
    );

    const points = container.querySelectorAll("circle");
    expect(points).toHaveLength(2);
    expect(Number(points[0].getAttribute("cy"))).toBe(164);
    expect(Number(points[1].getAttribute("cy"))).toBe(10);
  });

  it("preserves fractional scores in chart copy and coordinates", () => {
    const { container } = render(
      <DashboardInsights
        postureCounts={postureCounts}
        trend={[
          validation("oldest", "doc-1", {
            started_at: "2026-07-01T08:00:00Z",
            overall_score: 70,
          }),
          validation("newest", "doc-2", {
            started_at: "2026-07-02T08:00:00Z",
            overall_score: 89.6,
          }),
        ]}
      />,
    );

    expect(screen.getByText("Latest score 89.6%")).toBeTruthy();
    expect(
      screen.getByRole("img", { name: /latest score 89\.6 percent/ }),
    ).toBeTruthy();
    const latestPoint = container.querySelectorAll("circle")[1];
    expect(Number(latestPoint.getAttribute("cy"))).toBeCloseTo(26.016, 5);
  });
});

describe("RecentActivity", () => {
  it("uses the document lookup and formats coverage, findings, relative time, score, and destination", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-28T10:00:00Z"));

    render(
      <RecentActivity
        documentNames={{ "doc-1": "Core Banking Platform" }}
        items={[
          validation("validation-123", "doc-1", {
            overall_score: null,
            findings_count: 3,
          }),
        ]}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Recent validation activity" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "View all" }).getAttribute("href"),
    ).toBe("/validation");
    expect(screen.getByRole("table")).toBeTruthy();

    const row = screen.getByRole("row", {
      name: "Validation activity: Core Banking Platform",
    });
    expect(row.textContent).toContain("Complete");
    expect(row.textContent).toContain("Core Banking Platform");
    expect(row.textContent).toContain("18 / 42");
    expect(row.textContent).toContain("3 findings");
    expect(row.textContent).toContain("Started 2h ago");
    expect(row.textContent).toContain("N/A");
    expect(
      within(row)
        .getByRole("link", { name: "Open Core Banking Platform validation" })
        .getAttribute("href"),
    ).toBe("/validation/detail?id=validation-123");
  });

  it("keeps status, document, and score visible while hiding secondary mobile columns", () => {
    render(
      <RecentActivity
        documentNames={{ "doc-1": "Core Banking Platform" }}
        items={[validation("validation-123", "doc-1")]}
      />,
    );

    const table = screen.getByRole("table");
    expect(table.className).not.toContain("min-w-");
    for (const heading of ["Status", "Document", "Score"]) {
      expect(
        screen.getByRole("columnheader", { name: heading }).className,
      ).not.toContain("hidden");
    }
    expect(
      screen.getByRole("columnheader", { name: "Evaluated" }).className,
    ).toContain("hidden");
    expect(
      screen.getByRole("columnheader", { name: "Findings" }).className,
    ).toContain("hidden");
    expect(
      screen.getByRole("columnheader", { name: "Started" }).className,
    ).toContain("hidden");
  });

  it("uses explicit labels for backend validation statuses", () => {
    const statuses = [
      ["pending", "Pending"],
      ["in_progress", "In progress"],
      ["completed", "Complete"],
      ["failed", "Failed"],
      ["cancelled", "Cancelled"],
    ] as const;

    render(
      <RecentActivity
        documentNames={Object.fromEntries(
          statuses.map(([status]) => [status, `${status}.pdf`]),
        )}
        items={statuses.map(([status]) =>
          validation(`val-${status}`, status, { status }),
        )}
      />,
    );

    for (const [status, label] of statuses) {
      const row = screen.getByRole("row", {
        name: `Validation activity: ${status}.pdf`,
      });
      expect(within(row).getByText(label).getAttribute("data-status")).toBe(
        status,
      );
    }
  });

  it("preserves exact integer and fractional score values", () => {
    const scores = [
      ["integer", 90, "90%"],
      ["warning", 89.6, "89.6%"],
      ["failing", 69.6, "69.6%"],
    ] as const;

    render(
      <RecentActivity
        documentNames={Object.fromEntries(
          scores.map(([documentId]) => [documentId, `${documentId}.pdf`]),
        )}
        items={scores.map(([documentId, score]) =>
          validation(`val-${documentId}`, documentId, {
            overall_score: score,
          }),
        )}
      />,
    );

    for (const [documentId, , score] of scores) {
      const row = screen.getByRole("row", {
        name: `Validation activity: ${documentId}.pdf`,
      });
      expect(within(row).getByText(score)).toBeTruthy();
    }
  });

  it("keeps running as a legacy-compatible in-progress status", () => {
    render(
      <RecentActivity
        documentNames={{ legacy: "Legacy Running.pdf" }}
        items={[validation("val-running", "legacy", { status: "running" })]}
      />,
    );

    const row = screen.getByRole("row", {
      name: "Validation activity: Legacy Running.pdf",
    });
    expect(
      within(row).getByText("In progress").getAttribute("data-status"),
    ).toBe("running");
  });

  it("never presents an unknown status as complete", () => {
    render(
      <RecentActivity
        documentNames={{ unknown: "Unknown State.pdf" }}
        items={[validation("val-unknown", "unknown", { status: "paused" })]}
      />,
    );

    const row = screen.getByRole("row", {
      name: "Validation activity: Unknown State.pdf",
    });
    expect(within(row).getByText("Unknown")).toBeTruthy();
    expect(within(row).queryByText("Complete")).toBeNull();
  });

  it("defensively limits activity to five newest summaries", () => {
    render(
      <RecentActivity
        documentNames={Object.fromEntries(
          Array.from({ length: 6 }, (_, index) => [
            `doc-${index + 1}`,
            `Document ${index + 1}`,
          ]),
        )}
        items={Array.from({ length: 6 }, (_, index) =>
          validation(`val-${index + 1}`, `doc-${index + 1}`),
        )}
      />,
    );

    expect(
      screen.getAllByRole("row", { name: /Validation activity:/ }),
    ).toHaveLength(5);
    expect(screen.queryByText("Document 6")).toBeNull();
  });

  it("renders a useful linked empty state", () => {
    render(<RecentActivity documentNames={{}} items={[]} />);

    expect(screen.getByText("No validation activity yet")).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: "Browse validations" })
        .getAttribute("href"),
    ).toBe("/validation");
  });
});
