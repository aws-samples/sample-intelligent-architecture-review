// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";

interface PipelineJob {
  document_id: string;
  filename: string;
  stage?: string;
  status?: string;
  queued_at?: string;
  started_at?: string | null;
  is_standards_doc?: boolean;
  error?: string | null;
}

const stages = [
  { key: "queued", label: "Queued" },
  { key: "parsing", label: "Parsing PDF" },
  { key: "extracting_standards", label: "Extracting Rules" },
  { key: "kb_syncing", label: "Syncing to KB" },
  { key: "completed", label: "Ready" },
];

function getStageIndex(stage: string): number {
  const idx = stages.findIndex((s) => s.key === stage);
  return idx >= 0 ? idx : 0;
}

function StageIcon({ active, done }: { active: boolean; done: boolean }) {
  const baseClass = `w-4 h-4 ${done ? "text-[var(--color-status-pass)]" : active ? "text-[var(--color-brand-primary)] animate-pulse" : "text-muted-foreground/40"}`;

  if (done) return (
    <svg className={baseClass} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
    </svg>
  );
  if (active) return (
    <svg className={`${baseClass} animate-spin`} fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
  return (
    <div className="w-4 h-4 rounded-full border-2 border-muted-foreground/30" />
  );
}

export function ProcessingTracker() {
  const [jobs, setJobs] = useState<PipelineJob[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function poll() {
      try {
        const status = await api.getPipelineStatus();
        setJobs((status.jobs || []) as PipelineJob[]);
      } catch {
        // Silently fail - pipeline status is optional
      } finally {
        setLoading(false);
      }
    }

    poll();
    const interval = setInterval(poll, 3000);
    return () => clearInterval(interval);
  }, []);

  if (loading || jobs.length === 0) return null;

  return (
    <Card className="border-[var(--color-brand-primary)]/20">
      <CardHeader className="pb-2">
        <CardTitle className="text-xs flex items-center gap-2">
          <div className="h-2 w-2 rounded-full bg-[var(--color-brand-primary)] animate-pulse" />
          Processing Documents ({jobs.length})
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {jobs.map((job) => {
          const jobStage = job.stage || job.status || "queued";
          const currentIdx = getStageIndex(jobStage);
          const isFailed = jobStage === "failed";
          const applicableStages = job.is_standards_doc === true
            ? stages
            : stages.filter((s) => s.key !== "extracting_standards");

          return (
            <div key={job.document_id} className="rounded-md border border-border p-3">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-medium text-[var(--color-ink)] truncate max-w-[200px]">
                  {job.filename}
                </span>
                {isFailed && (
                  <span className="text-[10px] text-[var(--color-status-fail)]">Failed</span>
                )}
              </div>

              {/* Progress steps */}
              <div className="flex items-center gap-1">
                {applicableStages.map((stage, idx) => {
                  const stageIdx = stages.findIndex((s) => s.key === stage.key);
                  const isDone = currentIdx > stageIdx;
                  const isActive = currentIdx === stageIdx && !isFailed;

                  return (
                    <div key={stage.key} className="flex items-center gap-1">
                      <div className="flex flex-col items-center">
                        <StageIcon active={isActive} done={isDone} />
                        <span className={`text-[8px] mt-0.5 ${isActive ? "text-[var(--color-brand-primary)] font-medium" : isDone ? "text-[var(--color-status-pass)]" : "text-muted-foreground/50"}`}>
                          {stage.label}
                        </span>
                      </div>
                      {idx < applicableStages.length - 1 && (
                        <div className={`w-4 h-px mt-[-8px] ${isDone ? "bg-[var(--color-status-pass)]" : "bg-muted-foreground/20"}`} />
                      )}
                    </div>
                  );
                })}
              </div>

              {isFailed && job.error && (
                <p className="text-[9px] text-[var(--color-status-fail)] mt-1.5 truncate">
                  {job.error}
                </p>
              )}
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
