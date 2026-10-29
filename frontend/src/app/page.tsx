// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowRight,
  BookOpen,
  Check,
  LoaderCircle,
  ShieldAlert,
} from "lucide-react";

import { DashboardInsights } from "@/components/dashboard/DashboardInsights";
import { PortfolioPulse } from "@/components/dashboard/PortfolioPulse";
import { PriorityReviewQueue } from "@/components/dashboard/PriorityReviewQueue";
import { RecentActivity } from "@/components/dashboard/RecentActivity";
import { PageHero } from "@/components/layout/PageHero";
import {
  api,
  type DocumentListResponse,
  type StandardsListResponse,
  type ValidationListResponse,
} from "@/lib/api";
import {
  deriveDashboardInsights,
  isActiveValidationStatus,
} from "@/lib/dashboard-insights";

type PipelineStatus = Awaited<ReturnType<typeof api.getPipelineStatus>>;

interface DashboardResources {
  documents: DocumentListResponse;
  validations: ValidationListResponse;
  standards: StandardsListResponse;
  pipeline: PipelineStatus | null;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Failed to load dashboard data";
}

export default function DashboardPage() {
  const [resources, setResources] = useState<DashboardResources | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const hasDataRef = useRef(false);
  const generationRef = useRef(0);
  const requestRef = useRef(0);

  const loadDashboard = useCallback(async (generation: number) => {
    const request = requestRef.current + 1;
    requestRef.current = request;
    const initialRequest = !hasDataRef.current;
    const isCurrentRequest = () =>
      generationRef.current === generation && requestRef.current === request;

    if (initialRequest && isCurrentRequest()) {
      setLoading(true);
      setError(null);
    }

    try {
      const [documents, validations, standards, pipeline] = await Promise.all([
        api.listDocuments("sdd"),
        api.listValidations(),
        api.listStandards(),
        api.getPipelineStatus().catch(() => null),
      ]);

      if (!isCurrentRequest()) return;

      hasDataRef.current = true;
      setResources({ documents, validations, standards, pipeline });
      setError(null);
    } catch (loadError) {
      if (isCurrentRequest() && !hasDataRef.current) {
        setError(errorMessage(loadError));
      }
    } finally {
      if (isCurrentRequest()) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;

    const refresh = () => {
      void loadDashboard(generation);
    };
    const refreshWhileVisible = () => {
      if (!document.hidden) refresh();
    };

    refresh();
    const intervalId = window.setInterval(refreshWhileVisible, 30_000);
    window.addEventListener("focus", refresh);

    return () => {
      generationRef.current += 1;
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refresh);
    };
  }, [loadDashboard]);

  if (loading && !resources) {
    return (
      <div
        aria-live="polite"
        className="flex h-64 items-center justify-center"
        role="status"
      >
        <div className="text-center">
          <LoaderCircle className="mx-auto h-6 w-6 animate-spin text-[var(--color-brand-primary)]" />
          <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
            Loading review overview
          </p>
        </div>
      </div>
    );
  }

  if (error && !resources) {
    return (
      <div
        className="flex h-64 items-center justify-center"
        role="alert"
      >
        <div className="max-w-md text-center">
          <ShieldAlert className="mx-auto h-6 w-6 text-rose-600 dark:text-rose-300" />
          <p className="mt-3 text-sm font-medium text-rose-700 dark:text-rose-300">
            Dashboard unavailable
          </p>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            {error}
          </p>
          <button
            className="mt-4 inline-flex h-8 items-center justify-center rounded-lg bg-[var(--color-brand-primary)] px-4 text-xs font-semibold text-white transition-colors hover:bg-[var(--color-brand-primary-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/35 focus-visible:ring-offset-2"
            onClick={() => void loadDashboard(generationRef.current)}
            type="button"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!resources) return null;

  const insights = deriveDashboardInsights(
    resources.documents.documents,
    resources.validations.validations,
  );
  const attentionCount = insights.documents.filter(
    (insight) => insight.posture !== "pass" || insight.active,
  ).length;
  const knownDocumentIds = new Set(
    resources.documents.documents.map((document) => document.document_id),
  );
  const activeValidations = resources.validations.validations.filter(
    (validation) =>
      knownDocumentIds.has(validation.document_id) &&
      isActiveValidationStatus(validation.status),
  ).length;
  const showOnboarding =
    resources.standards.total === 0 ||
    resources.documents.total === 0 ||
    resources.validations.total === 0;
  const nextAction =
    resources.standards.total === 0
      ? {
          href: "/standards",
          label: "Configure your standards",
          note: "Add or extract the rules that design documents should meet.",
        }
      : resources.documents.total === 0
        ? {
            href: "/documents",
            label: "Add a system design",
            note: "Upload the first document you want to review.",
          }
        : {
            href: "/documents",
            label: "Run the first validation",
            note: "Choose a ready document and evaluate it against the library.",
          };
  const subtitle =
    attentionCount === 0
      ? "No documents need attention across the current portfolio."
      : `${attentionCount} ${attentionCount === 1 ? "document needs" : "documents need"} attention across the current portfolio.`;

  return (
    <div className="animate-in space-y-8 fade-in duration-300">
      <PageHero
        title="Architecture assurance"
        subtitle={subtitle}
        actions={
          <Link
            href="/getting-started"
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 text-xs font-medium text-slate-700 transition-colors hover:border-slate-300 hover:bg-slate-50 dark:border-white/12 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/8"
          >
            <BookOpen className="h-3.5 w-3.5" />
            Workflow guide
          </Link>
        }
      />

      {showOnboarding ? (
        <Link
          href={nextAction.href}
          className="group flex flex-col justify-between gap-5 overflow-hidden rounded-lg border border-teal-200/80 bg-white p-5 shadow-sm shadow-teal-950/[0.03] transition-all hover:border-teal-300 hover:shadow-md dark:border-teal-400/20 dark:bg-white/[0.025] dark:hover:border-teal-300/35 sm:flex-row sm:items-center"
        >
          <div className="flex min-w-0 items-start gap-4">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-teal-50 text-[var(--color-brand-primary)] ring-1 ring-teal-100 dark:bg-teal-400/10 dark:text-teal-300 dark:ring-teal-300/15">
              <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
            </span>
            <div className="min-w-0">
              <p className="text-[10px] font-semibold uppercase tracking-normal text-[var(--color-brand-primary)] dark:text-teal-300">
                Recommended next action
              </p>
              <p className="mt-1 text-base font-semibold text-slate-950 dark:text-slate-50">
                {nextAction.label}
              </p>
              <p className="mt-1 text-sm leading-relaxed text-slate-500 dark:text-slate-400">
                {nextAction.note}
              </p>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-1 sm:pl-4">
            <ReadinessStep
              complete={resources.standards.total > 0}
              label="Rules"
            />
            <span className="h-px w-4 bg-slate-200 dark:bg-white/10" />
            <ReadinessStep
              complete={resources.documents.total > 0}
              label="Docs"
            />
            <span className="h-px w-4 bg-slate-200 dark:bg-white/10" />
            <ReadinessStep
              complete={resources.validations.total > 0}
              label="Reviews"
            />
          </div>
        </Link>
      ) : null}

      <section className="grid gap-5 xl:grid-cols-[minmax(0,5fr)_minmax(420px,7fr)]">
        <PortfolioPulse
          activeExtractions={resources.pipeline?.active_jobs ?? null}
          activeValidations={activeValidations}
          coverageRate={insights.coverageRate}
          currentCritical={insights.currentCritical}
          currentHigh={insights.currentHigh}
          passRate={insights.passRate}
          postureCounts={insights.postureCounts}
        />
        <PriorityReviewQueue items={insights.priorityQueue} />
      </section>

      <DashboardInsights
        postureCounts={insights.postureCounts}
        trend={insights.trend}
      />

      <RecentActivity
        documentNames={insights.documentNames}
        items={insights.recentActivity}
      />
    </div>
  );
}

function ReadinessStep({
  label,
  complete,
}: {
  label: string;
  complete: boolean;
}) {
  return (
    <span className="flex items-center gap-1.5">
      <span
        className={
          "flex h-5 w-5 items-center justify-center rounded-full border " +
          (complete
            ? "border-teal-200 bg-teal-50 text-[var(--color-brand-primary)] dark:border-teal-300/20 dark:bg-teal-300/10 dark:text-teal-300"
            : "border-slate-200 bg-slate-50 text-slate-300 dark:border-white/10 dark:bg-white/5 dark:text-slate-600")
        }
      >
        {complete ? (
          <Check className="h-3 w-3" />
        ) : (
          <span className="h-1 w-1 rounded-full bg-current" />
        )}
      </span>
      <span className="hidden text-[10px] font-medium text-slate-500 dark:text-slate-400 xl:inline">
        {label}
      </span>
    </span>
  );
}
