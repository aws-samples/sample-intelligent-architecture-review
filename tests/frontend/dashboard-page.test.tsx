// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from "../../frontend/node_modules/react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "../../frontend/node_modules/@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DashboardPage from "@/app/page";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  api: {
    listDocuments: vi.fn(),
    listValidations: vi.fn(),
    listStandards: vi.fn(),
    getPipelineStatus: vi.fn(),
  },
}));

const documentsResponse = {
  documents: [
    {
      document_id: "doc-risk",
      filename: "Customer Data Platform.pdf",
      status: "ready",
      uploaded_at: "2026-07-20T10:00:00Z",
      file_size_bytes: 1024,
      page_count: 12,
      component_count: 4,
      document_type: "sdd" as const,
    },
    {
      document_id: "doc-pass",
      filename: "Payments Gateway.pdf",
      status: "ready",
      uploaded_at: "2026-07-21T10:00:00Z",
      file_size_bytes: 2048,
      page_count: 18,
      component_count: 6,
      document_type: "sdd" as const,
    },
  ],
  total: 2,
};

const validationsResponse = {
  validations: [
    {
      validation_id: "risk-running",
      document_id: "doc-risk",
      status: "running",
      standards_set: "default",
      started_at: "2026-07-29T12:00:00Z",
      completed_at: null,
      overall_score: null,
      findings_count: 0,
      critical_count: 0,
      high_count: 0,
      evaluated_count: 4,
      total_standards: 20,
    },
    {
      validation_id: "risk-current",
      document_id: "doc-risk",
      status: "completed",
      standards_set: "default",
      started_at: "2026-07-28T12:00:00Z",
      completed_at: "2026-07-28T12:30:00Z",
      overall_score: 92,
      findings_count: 3,
      critical_count: 1,
      high_count: 2,
      evaluated_count: 20,
      total_standards: 20,
    },
    {
      validation_id: "pass-current",
      document_id: "doc-pass",
      status: "completed",
      standards_set: "default",
      started_at: "2026-07-27T12:00:00Z",
      completed_at: "2026-07-27T12:30:00Z",
      overall_score: 95,
      findings_count: 0,
      critical_count: 0,
      high_count: 0,
      evaluated_count: 20,
      total_standards: 20,
    },
    {
      validation_id: "risk-historical",
      document_id: "doc-risk",
      status: "completed",
      standards_set: "default",
      started_at: "2026-07-01T12:00:00Z",
      completed_at: "2026-07-01T12:30:00Z",
      overall_score: 45,
      findings_count: 18,
      critical_count: 9,
      high_count: 9,
      evaluated_count: 20,
      total_standards: 20,
    },
  ],
  total: 4,
};

const standardsResponse = {
  standards: [],
  total: 20,
};

const emptyDocumentsResponse = { documents: [], total: 0 };
const emptyValidationsResponse = { validations: [], total: 0 };
const emptyStandardsResponse = { standards: [], total: 0 };

const listDocuments = vi.mocked(api.listDocuments);
const listValidations = vi.mocked(api.listValidations);
const listStandards = vi.mocked(api.listStandards);
const getPipelineStatus = vi.mocked(api.getPipelineStatus);

function mockPopulatedWorkspace() {
  listDocuments.mockResolvedValue(documentsResponse);
  listValidations.mockResolvedValue(validationsResponse);
  listStandards.mockResolvedValue(standardsResponse);
  getPipelineStatus.mockResolvedValue({ active_jobs: 2, jobs: [] });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function setDocumentHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: hidden,
  });
}

async function flushInitialLoad() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

beforeEach(() => {
  listDocuments.mockReset();
  listValidations.mockReset();
  listStandards.mockReset();
  getPipelineStatus.mockReset();
  setDocumentHidden(false);
  mockPopulatedWorkspace();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  setDocumentHidden(false);
});

describe("DashboardPage", () => {
  it("announces the initial loading state as a polite live status", async () => {
    const documents = deferred<typeof documentsResponse>();
    listDocuments.mockReturnValueOnce(documents.promise);

    render(<DashboardPage />);

    const status = screen.getByRole("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.textContent).toContain("Loading review overview");

    documents.resolve(documentsResponse);
    await screen.findByRole("heading", { name: "Architecture assurance" });
  });

  it("starts all core resource calls immediately and requests only SDD documents", async () => {
    render(<DashboardPage />);

    expect(listDocuments).toHaveBeenCalledWith("sdd");
    expect(listValidations).toHaveBeenCalledOnce();
    expect(listStandards).toHaveBeenCalledOnce();
    expect(getPipelineStatus).toHaveBeenCalledOnce();

    await screen.findByRole("heading", { name: "Architecture assurance" });
  });

  it("composes the populated command center from current document insights", async () => {
    render(<DashboardPage />);

    expect(
      await screen.findByRole("heading", { name: "Architecture assurance" }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "1 document needs attention across the current portfolio.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "current portfolio posture" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Priority review queue" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Portfolio insights" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Recent validation activity" }),
    ).toBeTruthy();
    expect(
      screen.getByLabelText("Current risk: 1 critical and 2 high findings"),
    ).toBeTruthy();
    expect(
      screen.queryByLabelText("Current risk: 10 critical and 11 high findings"),
    ).toBeNull();
    expect(
      screen.getAllByRole("row", {
        name: /Validation activity: Customer Data Platform\.pdf/,
      }),
    ).toHaveLength(3);
    expect(
      screen.queryByText("Recommended next action"),
    ).toBeNull();
  });

  it("renders unknown extraction state when pipeline status rejects", async () => {
    getPipelineStatus.mockRejectedValue(new Error("pipeline unavailable"));

    render(<DashboardPage />);

    expect(
      await screen.findByRole("heading", { name: "Architecture assurance" }),
    ).toBeTruthy();
    expect(
      screen.getByLabelText(
        "Active work: 1 validation; extraction count unavailable",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Extraction status unavailable")).toBeTruthy();
  });

  it("shows a retryable initial core error and retries with the same loader", async () => {
    const retryDocuments = deferred<typeof documentsResponse>();
    listDocuments
      .mockRejectedValueOnce(new Error("documents unavailable"))
      .mockReturnValueOnce(retryDocuments.promise);

    render(<DashboardPage />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Dashboard unavailable");
    expect(alert.textContent).toContain("documents unavailable");
    expect(
      screen.getByRole("button", { name: "Retry" }),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    const retryStatus = screen.getByRole("status");
    expect(retryStatus.getAttribute("aria-live")).toBe("polite");
    expect(retryStatus.textContent).toContain("Loading review overview");

    retryDocuments.resolve(documentsResponse);

    expect(
      await screen.findByRole("heading", { name: "Architecture assurance" }),
    ).toBeTruthy();
    expect(listDocuments).toHaveBeenCalledTimes(2);
    expect(listValidations).toHaveBeenCalledTimes(2);
    expect(listStandards).toHaveBeenCalledTimes(2);
    expect(getPipelineStatus).toHaveBeenCalledTimes(2);
  });

  it("shows the foundational onboarding action in an empty workspace", async () => {
    listDocuments.mockResolvedValue(emptyDocumentsResponse);
    listValidations.mockResolvedValue(emptyValidationsResponse);
    listStandards.mockResolvedValue(emptyStandardsResponse);
    getPipelineStatus.mockResolvedValue({ active_jobs: 0, jobs: [] });

    render(<DashboardPage />);

    expect(
      await screen.findByRole("heading", { name: "Architecture assurance" }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "No documents need attention across the current portfolio.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Recommended next action")).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: /Configure your standards/ })
        .getAttribute("href"),
    ).toBe("/standards");
    expect(
      screen.getByRole("heading", { name: "Priority review queue" }),
    ).toBeTruthy();
  });

  it("refreshes core and pipeline resources when the window regains focus", async () => {
    render(<DashboardPage />);
    await screen.findByRole("heading", { name: "Architecture assurance" });

    fireEvent.focus(window);

    await waitFor(() => {
      expect(listDocuments).toHaveBeenCalledTimes(2);
      expect(listValidations).toHaveBeenCalledTimes(2);
      expect(listStandards).toHaveBeenCalledTimes(2);
      expect(getPipelineStatus).toHaveBeenCalledTimes(2);
    });
  });

  it("keeps the newest started refresh when an older response resolves last", async () => {
    render(<DashboardPage />);
    await screen.findByRole("heading", { name: "Architecture assurance" });

    const staleDocuments = deferred<typeof documentsResponse>();
    const newestDocuments = deferred<typeof documentsResponse>();
    listDocuments
      .mockReturnValueOnce(staleDocuments.promise)
      .mockReturnValueOnce(newestDocuments.promise);

    fireEvent.focus(window);
    fireEvent.focus(window);
    expect(listDocuments).toHaveBeenCalledTimes(3);

    newestDocuments.resolve({
      ...documentsResponse,
      documents: documentsResponse.documents.map((document) =>
        document.document_id === "doc-risk"
          ? { ...document, filename: "Newest Portfolio.pdf" }
          : document,
      ),
    });
    expect(await screen.findAllByText("Newest Portfolio.pdf")).toBeTruthy();

    await act(async () => {
      staleDocuments.resolve({
        ...documentsResponse,
        documents: documentsResponse.documents.map((document) =>
          document.document_id === "doc-risk"
            ? { ...document, filename: "Stale Portfolio.pdf" }
            : document,
        ),
      });
      await staleDocuments.promise;
    });

    expect(screen.getAllByText("Newest Portfolio.pdf")).toBeTruthy();
    expect(screen.queryByText("Stale Portfolio.pdf")).toBeNull();
  });

  it("counts parallel active validation records for the same document", async () => {
    listValidations.mockResolvedValue({
      validations: [
        ...validationsResponse.validations,
        {
          ...validationsResponse.validations[0],
          validation_id: "risk-parallel",
          status: "in_progress",
          started_at: "2026-07-29T11:59:00Z",
        },
        {
          ...validationsResponse.validations[0],
          validation_id: "unknown-active",
          document_id: "unknown-document",
          started_at: "2026-07-29T11:58:00Z",
        },
      ],
      total: validationsResponse.total + 2,
    });
    getPipelineStatus.mockResolvedValue({ active_jobs: 0, jobs: [] });

    render(<DashboardPage />);

    expect(
      await screen.findByLabelText(
        "Active work: 2 validations and 0 extraction jobs, 2 total",
      ),
    ).toBeTruthy();
  });

  it("keeps prior dashboard data when a background core refresh fails", async () => {
    render(<DashboardPage />);
    await screen.findByRole("heading", { name: "Architecture assurance" });
    listDocuments.mockRejectedValueOnce(new Error("temporary outage"));

    fireEvent.focus(window);

    await waitFor(() => expect(listDocuments).toHaveBeenCalledTimes(2));
    expect(
      screen.getByRole("heading", { name: "Architecture assurance" }),
    ).toBeTruthy();
    expect(screen.queryByText("Dashboard unavailable")).toBeNull();
  });

  it("does not poll while the document is hidden", async () => {
    vi.useFakeTimers();
    setDocumentHidden(true);

    render(<DashboardPage />);
    await flushInitialLoad();

    expect(
      screen.getByRole("heading", { name: "Architecture assurance" }),
    ).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(listDocuments).toHaveBeenCalledOnce();
    expect(listValidations).toHaveBeenCalledOnce();
    expect(listStandards).toHaveBeenCalledOnce();
    expect(getPipelineStatus).toHaveBeenCalledOnce();
  });

  it("cancels polling and focus refresh after unmount", async () => {
    vi.useFakeTimers();
    const { unmount } = render(<DashboardPage />);
    await flushInitialLoad();

    expect(listDocuments).toHaveBeenCalledOnce();
    unmount();

    fireEvent.focus(window);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(listDocuments).toHaveBeenCalledOnce();
    expect(listValidations).toHaveBeenCalledOnce();
    expect(listStandards).toHaveBeenCalledOnce();
    expect(getPipelineStatus).toHaveBeenCalledOnce();
  });
});
