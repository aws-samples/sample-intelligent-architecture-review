// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

export type StandardCategory = "redundancy" | "availability" | "data" | "traffic_management" | "scalability" | "recovery" | "observability" | "failover" | "resilience" | "security" | "network";

export interface HAStandard {
  id: string;
  name: string;
  description: string;
  severity: "critical" | "high" | "medium";
  category: StandardCategory;
  validationCriteria: string;
}

export type DocumentStatus = "uploaded" | "parsing" | "ready" | "error";

export interface Document {
  id: string;
  name: string;
  uploadDate: string;
  /** Audit trail — email of the uploader. undefined/null on older rows. */
  uploadedBy?: string | null;
  pages: number;
  status: DocumentStatus;
  size: string;
  pdfUrl?: string;
  sections?: string[];
  components?: string[];
  /** Original upload format. Drives the "Source: PowerPoint" badge on
   *  the document card. Backend defaults this to "pdf" so older rows
   *  render correctly without the field. */
  sourceFormat?: "pdf" | "pptx";
  /** Slide count for PPTX docs. */
  slideCount?: number | null;
}

export type ValidationStatus =
  | "pass"
  | "fail"
  | "warning"
  | "pass_via_substitution"
  | "not_applicable_via_exception"
  // The document is silent on this rule: a coverage gap, excluded from
  // the compliance score (neither pass nor fail).
  | "not_addressed";
export type Severity = "critical" | "high" | "medium" | "low";

export interface Finding {
  id: string;
  requirementId: string;
  title: string;
  description: string;
  severity: Severity;
  status: ValidationStatus;
  evidence: string;
  recommendation: string;
  pattern: string;
  /** Additional page references (e.g. "p.7", "pp.12-15") where the
   *  same verdict was found by other chunks of a chunked validation
   *  run. Empty for single-chunk validations. */
  alsoSeenPages?: string[];
  /** founding/substitution/exception. Drives badges. */
  ruleType?: "founding" | "substitution" | "exception" | null;
  /** Rule id of the substitution that rescued (when status
   *  is pass_via_substitution) or exception that fired (when status
   *  is not_applicable_via_exception). */
  viaRuleId?: string | null;
  /** Substitution rule ids tried before giving up on FAIL. */
  triedSubstitutionIds?: string[];
  /** Per-finding "show your work" trace. reasoning, model_id and
   *  reasoning_signature are optional. */
  trace?: {
    prompt: string;
    chunk_excerpt: string;
    raw_response: string;
    input_tokens: number;
    output_tokens: number;
    latency_ms: number;
    /** Verbalized chain of thought from extended thinking. */
    reasoning?: string | null;
    /** Bedrock model id for this verdict. */
    model_id?: string;
    /** Opaque token, reserved for a future tool-use round-trip. */
    reasoning_signature?: string | null;
    tool_calls?: Array<{
      step: number;
      name: string;
      args: Record<string, unknown>;
      result_summary: string;
    }>;
  } | null;
}

export interface ValidationResult {
  id: string;
  documentId: string;
  documentName: string;
  date: string;
  score: number;
  status: ValidationStatus;
  findings: Finding[];
  findingCounts: {
    critical: number;
    high: number;
    medium: number;
    low: number;
  };
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  sources?: { title: string; section: string }[];
}

export const suggestedQueries = [
  "What are the most critical HA risks across all documents?",
  "Which systems lack cross-region disaster recovery?",
  "Show me all single points of failure in the payment gateway",
  "What is the RTO compliance status for Tier-1 systems?",
  "Are there any backup verification gaps?",
  "Compare the HA maturity of the order service and customer platform",
];
