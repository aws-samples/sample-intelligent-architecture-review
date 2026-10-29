// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { signOut } from "aws-amplify/auth";
import { authService } from "./auth/authService";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "";

/**
 * Fetch wrapper that injects the Cognito ID token into the Authorization
 * header. On 401, signs the user out so the AuthGate kicks in and forces
 * a fresh login instead of leaving the UI in a broken "AccessDenied"
 * state. Cognito ID tokens last 1 hour; if the refresh token is also
 * expired (30 days), this is the cleanest recovery path.
 */
async function authFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const idToken = await authService.getIdToken();
  const headers = new Headers(init.headers);
  if (idToken) {
    headers.set("Authorization", `Bearer ${idToken}`);
  }
  const res = await fetch(input, { ...init, headers });

  if (res.status === 401) {
    // Token rejected — sign out so AuthGate shows the login form.
    try {
      await signOut();
    } catch {
      // Ignore — even a failed signOut still clears local state.
    }
  }

  return res;
}

/**
 * Pull a useful error message off a non-OK Response. FastAPI returns
 * `{"detail": "..."}` for HTTPException — surface that. Falls back to
 * statusText, and then to "HTTP <code>" so callers never throw
 * "Failed: " with nothing after.
 *
 * Reads the body destructively, so callers should only invoke this
 * after `res.ok` has been confirmed false.
 */
async function errorDetail(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body === "object" && "detail" in body && body.detail) {
      return String((body as { detail: unknown }).detail);
    }
  } catch {
    // Body wasn't JSON; fall through.
  }
  return res.statusText || `HTTP ${res.status}`;
}

export interface UploadResponse {
  document_id: string;
  filename: string;
  status: string;
  message: string;
}

/** Original upload format. PDF stays canonical for the
 *  validation/extraction pipelines; PPTX is converted to PDF behind
 *  the scenes by the pptx-converter sidecar. The UI uses this for
 *  the "Source: PowerPoint" badge so users know which file the
 *  evidence references back to. */
export type SourceFormat = "pdf" | "pptx";

/** Heuristic classification of a slide's content. Used
 *  by the per-slide extractor to decide whether to skip
 *  decorative/title slides. Mirrors backend SlideType enum. */
export type SlideType =
  | "title"
  | "agenda"
  | "content"
  | "diagram"
  | "table"
  | "appendix";

/** Per-slide extraction state. PPTX docs surface one
 *  entry per slide so the UI can render slide-level progress. */
/** Per-slide extraction state. Field names mirror the backend's SlideStatus
 *  model exactly — this used to declare `rules_saved`, which the API never
 *  sends, so every slide rendered as "no rules" however many it produced. */
export type SlideExtractionStatus =
  | "pending"
  | "extracted"
  | "skipped"
  | "failed";

export interface SlideStatus {
  slide_index: number;
  slide_type: SlideType;
  /** Extraction outcome. Lets the UI tell a failed slide from an empty one. */
  status?: SlideExtractionStatus;
  /** Rules saved from this slide (backend field name: rule_count). */
  rule_count: number;
  /** Set when status === "failed"; surfaced verbatim so the failure is visible
   *  rather than looking like a slide that simply had no rules. */
  error?: string | null;
  skip_reason?: string;
  image_s3_key?: string;
}

export interface DocumentListResponse {
  documents: Array<{
    document_id: string;
    filename: string;
    status: string;
    uploaded_at: string;
    /** Audit trail — email of the uploader. null on older rows. */
    uploaded_by?: string | null;
    file_size_bytes: number;
    page_count: number | null;
    component_count: number;
    document_type?: "sdd" | "standards";
    /** Service tier (1..5). null when not classified or on rows
     *  created before tier support existed. */
    declared_tier?: number | null;
    /** "auto" (LLM classifier) or "user" (manual override). */
    tier_source?: "auto" | "user" | null;
    /** Required field on the backend, marked optional on the wire
     *  shape for graceful render of rows created before this field
     *  existed. */
    hosting_scope?: HostingScope;
    /** Surfaces the "Source: PowerPoint" badge on document
     *  cards. Defaults to "pdf" on the backend so older rows still
     *  render correctly without this field. */
    source_format?: SourceFormat;
    /** Number of slides for PPTX docs (null for PDF). */
    slide_count?: number | null;
  }>;
  total: number;
}

export interface DocumentDetailResponse {
  document_id: string;
  filename: string;
  s3_key: string;
  status: string;
  uploaded_at: string;
  /** Audit trail — email of the uploader. null on older rows. */
  uploaded_by?: string | null;
  parsed_at: string | null;
  file_size_bytes: number;
  page_count: number | null;
  sections: Array<{ title: string; content: string; page_number: number }>;
  components: Array<{ name: string; component_type: string; description: string }>;
  error_message: string | null;
  document_type?: "sdd" | "standards";
  declared_tier?: number | null;
  tier_source?: "auto" | "user" | null;
  /** Backend-required; optional on the wire for graceful render. */
  hosting_scope?: HostingScope;
  /** Original upload format. */
  source_format?: SourceFormat;
  /** Original .pptx S3 key, only set when source_format === "pptx".
   *  Lets the UI offer a "Download original" link alongside the converted PDF. */
  original_s3_key?: string;
  /** Slide count for PPTX docs (null for native PDFs). */
  slide_count?: number | null;
  /** Per-slide extraction state for PPTX docs. */
  slides?: SlideStatus[];
}

export interface ValidationListResponse {
  validations: Array<{
    validation_id: string;
    document_id: string;
    status: string;
    standards_set: string;
    started_at: string;
    completed_at: string | null;
    overall_score: number | null;
    findings_count: number;
    critical_count: number;
    high_count: number;
    evaluated_count?: number;
    total_standards?: number;
  }>;
  total: number;
}

export interface ValidationDetailResponse {
  validation_id: string;
  document_id: string;
  status: string;
  standards_set: string;
  started_at: string;
  completed_at: string | null;
  overall_score: number | null;
  /** Email of the user who started the run. Only they (or an admin) may
   *  cancel it. Null on runs recorded before ownership was tracked. */
  triggered_by?: string | null;
  findings: Array<{
    requirement_id: string;
    requirement_title: string;
    /** `status` may also be "pass_via_substitution" or
     *  "not_applicable_via_exception" beyond the base pass/fail values. */
    status: string;
    severity: string;
    evidence: string;
    recommendation: string;
    affected_components: string[];
    /** Additional page references where the same verdict was seen
     *  (e.g. "p.7", "pp.12-15"). Empty for single-chunk runs. */
    also_seen_pages?: string[];
    /** Finding's underlying rule type. Drives badge colors. */
    rule_type?: "founding" | "substitution" | "exception" | null;
    /** When status is pass_via_substitution, the rule id
     *  of the substitution that rescued. When status is
     *  not_applicable_via_exception, the rule id of the exception
     *  that fired. */
    via_rule_id?: string | null;
    /** On FAIL, the substitution rule ids the validator
     *  tried before giving up. Empty when no substitutions were
     *  configured for the founding rule. */
    tried_substitution_ids?: string[];
    /** Per-finding "show your work" trace. reasoning, model_id and
     *  reasoning_signature are optional. */
    trace?: {
      prompt: string;
      chunk_excerpt: string;
      raw_response: string;
      input_tokens: number;
      output_tokens: number;
      latency_ms: number;
      /** Verbalized chain of thought from extended thinking.
       *  Null/undefined on older validations or when thinking was off. */
      reasoning?: string | null;
      /** Which Bedrock model produced this verdict. */
      model_id?: string;
      /** Opaque token, reserved for a future tool-use round-trip. */
      reasoning_signature?: string | null;
      /** Reserved for agent tool-call traces; always absent today and
       *  ignored by the trace drawer. */
      tool_calls?: Array<{
        step: number;
        name: string;
        args: Record<string, unknown>;
        result_summary: string;
      }>;
    } | null;
  }>;
  /** Per-(tier × layer) decision-tree rollup. Empty on validations
   *  created before this field existed; the UI falls back to flat
   *  findings. */
  layer_verdicts?: Array<{
    tier: number;
    layer: string;
    verdict: "compliant" | "compliant_via_substitution" | "not_applicable_via_exception" | "not_compliant";
    via_rule_id?: string | null;
    founding_finding_ids?: string[];
  }> | null;
  architecture_verdict?: "compliant" | "compliant_via_substitution" | "not_applicable_via_exception" | "not_compliant" | null;
  tier_used?: number | null;
  tier_override?: number | null;
  summary: string | null;
  error_message: string | null;
  evaluated_count?: number;
  total_standards?: number;
  /** Telemetry — populated by the chunked validator. Optional so
   *  rows created before chunking still load. */
  chunks_per_document?: number | null;
  bedrock_calls?: number | null;
  bedrock_input_tokens?: number | null;
  bedrock_output_tokens?: number | null;
  estimated_cost_usd?: number | null;
  /** Rules excluded by tier filtering. evaluated_count + skipped_count
   *  ≈ size of the active rule library at validation time. */
  skipped_count?: number | null;
  /** Per-filter breakdown of skipped_count. Keys "tier" and "scope"
   *  report rules dropped by tier-mismatch vs hosting-scope-mismatch
   *  respectively. Optional + null on rows created before this field
   *  existed. */
  skipped_count_breakdown?: { tier: number; scope: number } | null;
}

/**
 * Single source of truth for layer / rule_type vocabularies on the
 * frontend. The TS literal unions are derived from these tuples so the
 * values you can write are also the values you can iterate (for filter
 * chips, dropdown <option>s, etc.). Backend has a
 * mirror set in `app/models/standard.py` (`RuleLayer` / `RuleType`
 * Literals) and `agents/tools/standards_tools.py` (`_VALID_LAYERS` /
 * `_VALID_RULE_TYPES`); a backend test asserts those two are equal,
 * but the frontend↔backend sync remains a manual contract for now.
 */
export const LAYER_VALUES = [
  "datacenter",
  "data",
  "web",
  "application",
  "load_balancer",
  "general",
] as const;
export type RuleLayer = (typeof LAYER_VALUES)[number];

export const RULE_TYPE_VALUES = [
  "founding",
  "substitution",
  "exception",
] as const;
export type RuleType = (typeof RULE_TYPE_VALUES)[number];

/**
 * hosting_scope vocabulary. on_prem = traditional
 * datacenter workload, cloud = AWS/Azure/GCP, both = applies
 * to either (security baselines, governance, RTO/RPO). Backend
 * has the same Literal in app/models/standard.py; a backend test
 * (test_hosting_scope_literal_matches_agent_vocab) asserts the
 * three sources stay in sync.
 */
export const HOSTING_SCOPE_VALUES = [
  "on_prem",
  "cloud",
  "both",
] as const;
export type HostingScope = (typeof HOSTING_SCOPE_VALUES)[number];

export interface StandardItem {
  id: string;
  name: string;
  description: string;
  severity: string;
  category: string;
  validation_criteria: string;
  source: "document" | "user" | "generated";
  status?: "active" | "proposed" | "archived";
  source_document?: string | null;
  created_at: string;
  updated_at: string;
  // Proposal-only fields (set when status === "proposed").
  proposal_kind?: "add" | "edit" | "delete";
  target_id?: string;
  rationale?: string;
  /** Tier scope. Older rules without this field default to all
   *  five tiers (universal). Validation only runs rules whose
   *  list contains the SDD's declared_tier. */
  applies_to_tiers?: number[];
  /** Page reference where the rule's source language lives.
   *  Format: "p. 3" or "pp. 4-5". Empty for rules extracted before
   *  provenance tracking landed. */
  page_reference?: string;
  /** Verbatim excerpt from the source document that justified this
   *  rule. Direct quote, max ~500 chars. Empty for older rules. */
  source_quote?: string;
  /** Architectural layer this rule applies to. Fixed vocabulary; see
   *  LAYER_VALUES. Older / flat rules omit this. */
  layer?: RuleLayer;
  /** Rule semantics for the decision-tree validator. founding = the
   *  primary requirement; substitution = an alternative architecture
   *  that satisfies the founding rule; exception = an approved
   *  deviation. Older / flat rules omit this. */
  rule_type?: RuleType;
  /** Id of the founding rule that this substitution or exception
   *  relates to. Free-form string; not FK-checked. No UI input —
   *  auto-populated by structured-rule extraction. */
  parent_rule_id?: string;
  /** Required. Tags whether this rule applies to on-prem
   *  workloads, cloud workloads, or both (universal). The Standards
   *  page segmented control filters by this; the validator only
   *  selects rules where hosting_scope ∈ {sdd_scope, "both"}. */
  hosting_scope: HostingScope;
  /** Review gate — the extraction run this proposed rule belongs to.
   *  Set on rules extracted from a standards document (status="proposed"),
   *  groups them into a reviewable batch on the document-detail page. */
  extraction_batch_id?: string;
  /** Review gate — flagged for human scrutiny. True when the rule was
   *  model-inferred with no verbatim source_quote, or derived from a
   *  low-confidence architecture-diagram reading. */
  needs_review?: boolean;
  /** Self-consistency confidence for a diagram-derived rule. */
  topology_confidence?: "high" | "medium" | "low";
}

/** RBAC — the four roles. */
export type UserRole = "admin" | "validator" | "curator" | "viewer";

export interface UserRoleAssignment {
  sub: string;
  role: UserRole;
  email?: string;
  updated_at?: string;
}

/** A scope-mismatch verdict: the document's content reads as a different
 *  hosting scope than the one declared at upload, which was forced onto every
 *  rule in the batch. Advisory — the reviewer decides. */
export interface ScopeMismatch {
  declared: string;
  inferred: string;
  confidence: string;
  rationale: string;
}

/** Review gate — summary of one extraction run's proposed-rule batch. */
export interface ExtractionBatchSummary {
  extraction_batch_id: string;
  source_document?: string | null;
  created_at: string;
  total: number;
  needs_review: number;
  /** The scope every rule in the batch was tagged with (the upload-time
   *  declaration). */
  hosting_scope?: string | null;
  /** Present when the inferred content scope confidently contradicts the
   *  declared one; null otherwise. */
  scope_mismatch?: ScopeMismatch | null;
  /** True while the run that produced this batch is still extracting or
   *  de-duplicating. `total` is the PRE-dedup per-unit count until this clears,
   *  so treat it as provisional and don't surface it as the settled
   *  "awaiting review" number. */
  in_flight?: boolean;
  /** "finalizing" while in-flight (provisional count); "pending" once settled
   *  and ready for review. */
  status: "pending" | "finalizing";
}

// References. Scope-tagged context blocks the validator attaches
// alongside rules but doesn't validate against, e.g. a datacenter-peer
// table from a standards document.
export type ReferenceStatus = "active" | "proposed" | "archived";

export interface ReferenceItem {
  id: string;
  name: string;
  body: string;
  applies_to_tiers: number[];
  layer?: RuleLayer | null;
  hosting_scope: HostingScope;
  source_document?: string | null;
  page_reference?: string | null;
  source_quote?: string | null;
  status: ReferenceStatus;
  created_at: string;
  updated_at: string;
}

export interface CreateReferenceRequest {
  name: string;
  body: string;
  applies_to_tiers: number[];
  layer?: RuleLayer | null;
  hosting_scope: HostingScope;
  source_document?: string;
  page_reference?: string;
  source_quote?: string;
}

export interface RuleChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface RuleChatResponse {
  reply: string;
  proposals: StandardItem[];
}

/** Inline rule the validator should evaluate just for one run, without saving. */
export interface TransientStandard {
  name: string;
  description: string;
  severity: "critical" | "high" | "medium" | "low" | "info";
  category: string;
  validation_criteria: string;
  /** Optional on the wire because the validator only uses
   *  this for one-off transient runs and doesn't filter by scope on
   *  inline rules; AI-suggested rules carry the chosen Curate scope so
   *  promoting them to the library can keep the tag intact. */
  hosting_scope?: HostingScope;
}

/** AI-suggested NEW rule returned by /api/standards/curate. Same shape as
 *  TransientStandard plus a rationale explaining why the library doesn't
 *  already cover it. The frontend assigns a temp client id so the user
 *  can toggle inclusion without confusing it with library rules. */
export interface RuleSuggestion extends TransientStandard {
  rationale: string;
}

export interface CurateResponse {
  existing_standard_ids: string[];
  new_suggestions: RuleSuggestion[];
  total_available: number;
  top_n: number;
}

export interface StandardsListResponse {
  standards: StandardItem[];
  total: number;
}

export interface CreateStandardData {
  name: string;
  description: string;
  severity: "critical" | "high" | "medium" | "low";
  category: string;
  validation_criteria: string;
  /** Structured-rule fields, optional. See StandardItem for semantics.
   *  Caller must omit (not send "") when the user picks no value;
   *  the backend Pydantic Literal would 422 on an empty string. */
  layer?: RuleLayer;
  rule_type?: RuleType;
  /** Backend-accepted but no UI input. Auto-populated by
   *  structured-rule extraction. */
  parent_rule_id?: string;
  /** Required. The Add Standard form must include a
   *  hosting_scope dropdown; the backend 422s on omission. */
  hosting_scope: HostingScope;
}

export interface UpdateStandardData {
  name?: string;
  description?: string;
  severity?: "critical" | "high" | "medium" | "low";
  category?: string;
  validation_criteria?: string;
  applies_to_tiers?: number[];
  /** Structured-rule fields, optional. See StandardItem for semantics.
   *  Same omit-when-empty contract as CreateStandardData. */
  layer?: RuleLayer;
  rule_type?: RuleType;
  parent_rule_id?: string;
  /** Optional on PATCH (omit means "no change"). Backend
   *  uses exclude_none=True. */
  hosting_scope?: HostingScope;
}

export interface DocumentDiagram {
  image_url: string;
  page_number: number;
  caption: string;
}

export interface QueryResponse {
  query_id: string;
  answer: string;
  sources: Array<{ title: string; section: string; uri: string }>;
}

export const api = {
  // --- Documents ---

  async uploadDocument(
    file: File,
    documentType?: "sdd" | "standards",
    options?: {
      /** Free-form NLP context describing how this standards document
       *  is organised. The agent uses these notes as authoritative
       *  reasoning input when setting tier / layer / rule_type /
       *  parent_rule_id on extracted rules. Only meaningful for
       *  documentType === "standards"; ignored for SDDs. Empty /
       *  whitespace-only strings are treated as absent, which falls
       *  back to unstructured extraction. */
      extractionNotes?: string;
      /** Required (the upload form's dropdown enforces it).
       *  For SDDs: the deployment target the validator filters
       *  rules by. For standards docs: every extracted rule
       *  inherits this scope. Backend 400s when omitted. */
      hostingScope?: HostingScope;
    }
  ): Promise<UploadResponse> {
    const formData = new FormData();
    formData.append("file", file);

    const params = new URLSearchParams();
    if (documentType) params.set("document_type", documentType);
    if (options?.extractionNotes && options.extractionNotes.trim()) {
      params.set("extraction_notes", options.extractionNotes.trim());
    }
    if (options?.hostingScope) {
      params.set("hosting_scope", options.hostingScope);
    }
    const qs = params.toString();
    const url = qs ? `${API_URL}/api/documents?${qs}` : `${API_URL}/api/documents`;

    const res = await authFetch(url, {
      method: "POST",
      body: formData,
    });

    if (!res.ok) throw new Error(`Upload failed: ${await errorDetail(res)}`);
    return res.json();
  },

  async listDocuments(documentType?: "sdd" | "standards"): Promise<DocumentListResponse> {
    const params = documentType ? `?document_type=${documentType}` : "";
    const res = await authFetch(`${API_URL}/api/documents${params}`);
    if (!res.ok) throw new Error(`Failed to list documents: ${res.statusText}`);
    return res.json();
  },

  /** Override (or clear) an SDD's declared tier. Pass null to revert
   *  to the auto-detected value. Used from the document detail page
   *  when the auto-classifier got it wrong. */
  async setDocumentTier(
    id: string,
    tier: number | null,
  ): Promise<{ document_id: string; declared_tier: number | null; tier_source: "user" | null }> {
    const res = await authFetch(`${API_URL}/api/documents/${id}/tier`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tier }),
    });
    if (!res.ok) throw new Error(`Failed to set document tier: ${res.statusText}`);
    return res.json();
  },

  async getDocument(id: string): Promise<DocumentDetailResponse> {
    // Retry on 404 to handle the upload→detail-page race: POST
    // /api/documents saves the doc to DDB synchronously, but
    // DynamoDB's eventual consistency means a GET from a different
    // connection can hit a replica that hasn't seen the write yet.
    // Three attempts at 0/500ms/1500ms cover the consistency window
    // without making the user wait if the doc truly doesn't exist
    // (e.g. they hand-edited the URL with a bad id).
    const delays = [0, 500, 1500];
    let lastRes: Response | null = null;
    for (const delay of delays) {
      if (delay > 0) await new Promise((r) => setTimeout(r, delay));
      const res = await authFetch(`${API_URL}/api/documents/${id}`);
      if (res.ok) return res.json();
      lastRes = res;
      if (res.status !== 404) break;  // 5xx, 401, etc — don't retry
    }
    throw new Error(
      `Failed to get document: ${lastRes?.statusText || "unknown error"}`,
    );
  },

  async getDocumentDiagrams(id: string): Promise<DocumentDiagram[]> {
    try {
      const res = await authFetch(`${API_URL}/api/documents/${id}/diagrams`);
      if (!res.ok) return [];
      const data = await res.json();
      return data.diagrams || [];
    } catch {
      return [];
    }
  },

  /**
   * Fetch a binary file (PDF/image) with auth and return a blob URL the
   * browser can load in <iframe> / <img> / <a download>. The caller is
   * responsible for revoking the URL when the component unmounts.
   */
  async getDocumentBlobUrl(id: string): Promise<string> {
    const res = await authFetch(`${API_URL}/api/documents/${id}/pdf`);
    if (!res.ok) throw new Error(`Failed to fetch document file: ${res.statusText}`);
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  },

  /** Download a validation report as CSV or PDF. Fetches the file with
   *  auth (the backend sets Content-Disposition) and triggers a browser
   *  download, revoking the object URL afterward. Throws on failure so the
   *  caller can surface an error. */
  async downloadValidationReport(id: string, fmt: "csv" | "pdf"): Promise<void> {
    const res = await authFetch(`${API_URL}/api/validations/${id}/report.${fmt}`);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch {
        /* non-JSON error body */
      }
      throw new Error(`Failed to download ${fmt.toUpperCase()} report: ${detail}`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `validation-${id}.${fmt}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },

  /** Fetch a rasterised slide PNG (1-based) for a PPTX
   *  document and return an auth-bound blob URL the browser can put
   *  on an <img>. Returns null when the slide doesn't exist or the
   *  doc isn't a PPTX. Caller should revoke the URL on unmount. */
  async getDocumentSlideUrl(id: string, slideIndex: number): Promise<string | null> {
    const res = await authFetch(`${API_URL}/api/documents/${id}/slides/${slideIndex}`);
    if (!res.ok) return null;
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  },

  // --- Validations ---

  async triggerValidation(
    documentId: string,
    standardsSet: string = "default",
    options: {
      standardIds?: string[];
      transientStandards?: TransientStandard[];
      force?: boolean;
      /** Pre-validation tier override. When set, the
       *  validator filters rules by this tier instead of the
       *  document's declared_tier. */
      tierOverride?: number;
    } = {},
  ) {
    const body: Record<string, unknown> = {
      document_id: documentId,
      standards_set: standardsSet,
    };
    if (options.standardIds && options.standardIds.length > 0) {
      body.standard_ids = options.standardIds;
    }
    if (options.transientStandards && options.transientStandards.length > 0) {
      body.transient_standards = options.transientStandards;
    }
    if (options.force) {
      body.force = true;
    }
    if (options.tierOverride !== undefined && options.tierOverride !== null) {
      body.tier_override = options.tierOverride;
    }
    const res = await authFetch(`${API_URL}/api/validations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!res.ok) throw new Error(`Validation failed: ${res.statusText}`);
    return res.json();
  },

  async listValidations(documentId?: string): Promise<ValidationListResponse> {
    const url = documentId
      ? `${API_URL}/api/validations?document_id=${documentId}`
      : `${API_URL}/api/validations`;
    const res = await authFetch(url);
    if (!res.ok) throw new Error(`Failed to list validations: ${res.statusText}`);
    return res.json();
  },

  async getValidation(id: string): Promise<ValidationDetailResponse> {
    const res = await authFetch(`${API_URL}/api/validations/${id}`);
    if (!res.ok) throw new Error(`Failed to get validation: ${res.statusText}`);
    return res.json();
  },

  /**
   * Request cancellation of an in-flight validation. The backend sets a
   * cooperative flag; the run stops at its next rule/slide checkpoint and
   * ends with status "cancelled". Idempotent on already-terminal runs.
   */
  async cancelValidation(
    id: string,
  ): Promise<{ validation_id: string; status: string; cancelled: boolean; message: string }> {
    const res = await authFetch(`${API_URL}/api/validations/${id}/cancel`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`Failed to cancel validation: ${res.statusText}`);
    return res.json();
  },

  /**
   * Read-only conversational explainer for a single finding.
   * Backend prepends a structured system prompt with the
   * rule + verdict + trace, so the caller only needs to send the
   * user/assistant message thread. Synchronous; explanations are
   * short so we don't need the async-job machinery.
   */
  async chatAboutFinding(
    validationId: string,
    requirementId: string,
    messages: Array<{ role: "user" | "assistant"; content: string }>,
  ): Promise<{ reply: string }> {
    const res = await authFetch(
      `${API_URL}/api/validations/${validationId}/findings/${encodeURIComponent(requirementId)}/chat`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages }),
      },
    );
    if (!res.ok) throw new Error(`Chat failed: ${await errorDetail(res)}`);
    return res.json();
  },

  /**
   * Streaming (SSE) variant of chatAboutFinding. Uses a fetch ReadableStream
   * rather than EventSource because the request is POST and must carry the
   * Cognito Bearer JWT (EventSource supports neither). Invokes:
   *   onDelta(text)   — for each token chunk as it arrives
   *   onWarning(text) — trailing verdict-flip disclaimer, if any
   * Resolves when the stream completes; rejects on an `error` frame or HTTP
   * failure. Returns an abort function to cancel mid-stream.
   */
  async chatAboutFindingStream(
    validationId: string,
    requirementId: string,
    messages: Array<{ role: "user" | "assistant"; content: string }>,
    handlers: { onDelta: (t: string) => void; onWarning?: (t: string) => void },
    signal?: AbortSignal,
  ): Promise<void> {
    const idToken = await authService.getIdToken();
    const res = await fetch(
      `${API_URL}/api/validations/${validationId}/findings/${encodeURIComponent(requirementId)}/chat/stream`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
        },
        body: JSON.stringify({ messages }),
        signal,
      },
    );
    if (!res.ok || !res.body) {
      throw new Error(`Chat failed: ${await errorDetail(res)}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    // SSE frames are delimited by a blank line; each frame has an `event:`
    // line and a `data:` line. Parse incrementally across chunk boundaries.
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);

        let event = "message";
        let data = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        const parsed = JSON.parse(data);

        if (event === "delta") handlers.onDelta(parsed.text ?? "");
        else if (event === "warning") handlers.onWarning?.(parsed.text ?? "");
        else if (event === "error") throw new Error(parsed.detail ?? "stream error");
        else if (event === "done") return;
      }
    }
  },

  // --- Standards ---

  /**
   * Ask the backend to curate standards for a document. Returns ids of
   * existing library rules to use, plus optionally a list of brand-new
   * rule suggestions (not saved anywhere yet).
   */
  async curateStandards(
    documentId: string,
    options: {
      topN?: number;
      userPrompt?: string;
      suggestNew?: boolean;
      maxNew?: number;
    } = {},
  ): Promise<CurateResponse> {
    const body: Record<string, unknown> = {
      document_id: documentId,
      top_n: options.topN ?? 20,
      suggest_new: options.suggestNew ?? true,
      max_new: options.maxNew ?? 5,
    };
    if (options.userPrompt && options.userPrompt.trim()) {
      body.user_prompt = options.userPrompt.trim();
    }
    const res = await authFetch(`${API_URL}/api/standards/curate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Failed to curate standards: ${await errorDetail(res)}`);
    return res.json();
  },

  async listStandards(
    options: {
      status?: "active" | "proposed";
      includeProposed?: boolean;
      hostingScope?: HostingScope;
      source?: "document" | "generated" | "user";
    } = {},
  ): Promise<StandardsListResponse> {
    const params = new URLSearchParams();
    if (options.status) params.set("status", options.status);
    if (options.includeProposed) params.set("include_proposed", "true");
    if (options.hostingScope) params.set("hosting_scope", options.hostingScope);
    if (options.source) params.set("source", options.source);
    const qs = params.toString();
    const url = qs ? `${API_URL}/api/standards?${qs}` : `${API_URL}/api/standards`;
    const res = await authFetch(url);
    if (!res.ok) throw new Error(`Failed to list standards: ${res.statusText}`);
    return res.json();
  },

  // --- RBAC: roles ---

  /** The caller's own resolved role (admin|validator|curator|viewer). */
  async getMyRole(): Promise<{ sub: string; email: string; role: UserRole }> {
    const res = await authFetch(`${API_URL}/api/roles/me`);
    if (!res.ok) throw new Error(`Failed to load role: ${res.statusText}`);
    return res.json();
  },

  /** Admin only — all explicit role assignments. */
  async listUserRoles(): Promise<{ users: UserRoleAssignment[] }> {
    const res = await authFetch(`${API_URL}/api/roles`);
    if (!res.ok) throw new Error(`Failed to list roles: ${await errorDetail(res)}`);
    return res.json();
  },

  /** Admin only — assign/reassign a user's role. */
  async setUserRole(sub: string, role: UserRole, email = ""): Promise<UserRoleAssignment> {
    const res = await authFetch(`${API_URL}/api/roles`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sub, role, email }),
    });
    if (!res.ok) throw new Error(`Failed to set role: ${await errorDetail(res)}`);
    return res.json();
  },

  /** Admin only — remove a user's explicit role assignment. */
  async removeUserRole(sub: string): Promise<{ message: string }> {
    const res = await authFetch(`${API_URL}/api/roles/${encodeURIComponent(sub)}`, {
      method: "DELETE",
    });
    if (!res.ok) throw new Error(`Failed to remove role: ${await errorDetail(res)}`);
    return res.json();
  },

  // --- Review gate: extraction batches ---

  /** List pending extraction review-batches, optionally scoped to one
   *  document. Each batch groups the proposed rules from one extraction run. */
  async listExtractionBatches(documentId?: string): Promise<{
    batches: ExtractionBatchSummary[];
    total: number;
  }> {
    const qs = documentId ? `?document_id=${encodeURIComponent(documentId)}` : "";
    const res = await authFetch(`${API_URL}/api/standards/extraction-batches${qs}`);
    if (!res.ok) throw new Error(`Failed to list extraction batches: ${res.statusText}`);
    return res.json();
  },

  /** One batch's proposed rules (needs_review sorted first). */
  async getExtractionBatch(batchId: string): Promise<
    ExtractionBatchSummary & { rules: StandardItem[] }
  > {
    const res = await authFetch(
      `${API_URL}/api/standards/extraction-batches/${encodeURIComponent(batchId)}`,
    );
    if (!res.ok) throw new Error(`Failed to load batch: ${await errorDetail(res)}`);
    return res.json();
  },

  async approveExtractionBatch(batchId: string): Promise<{ approved: number; failed: string[] }> {
    const res = await authFetch(
      `${API_URL}/api/standards/extraction-batches/${encodeURIComponent(batchId)}/approve-all`,
      { method: "POST" },
    );
    if (!res.ok) throw new Error(`Failed to approve batch: ${await errorDetail(res)}`);
    return res.json();
  },

  async rejectExtractionBatch(batchId: string): Promise<{ rejected: number; failed: string[] }> {
    const res = await authFetch(
      `${API_URL}/api/standards/extraction-batches/${encodeURIComponent(batchId)}/reject-all`,
      { method: "POST" },
    );
    if (!res.ok) throw new Error(`Failed to reject batch: ${await errorDetail(res)}`);
    return res.json();
  },

  /** Set hosting_scope on every proposed rule in a batch — the one-click fix
   *  for a wrong upload-time scope, done before approval. */
  async setExtractionBatchScope(
    batchId: string,
    hostingScope: "on_prem" | "cloud" | "both",
  ): Promise<{ hosting_scope: string; updated: number; failed: string[] }> {
    const res = await authFetch(
      `${API_URL}/api/standards/extraction-batches/${encodeURIComponent(batchId)}/set-scope`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hosting_scope: hostingScope }),
      },
    );
    if (!res.ok) throw new Error(`Failed to set batch scope: ${await errorDetail(res)}`);
    return res.json();
  },

  /** Start an async curator chat job. Returns immediately with the
   *  job_id. Use getCuratorChatJob (or the useCuratorJobs hook) to
   *  poll for completion. chatWithRulesCurator wraps this with a
   *  polling loop for callers that want a single awaitable. */
  async startCuratorChatJob(
    messages: RuleChatTurn[],
    hostingScope?: HostingScope,
  ): Promise<{ job_id: string }> {
    const body: Record<string, unknown> = { messages };
    if (hostingScope) body.hosting_scope = hostingScope;
    const res = await authFetch(`${API_URL}/api/standards/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Chat failed: ${await errorDetail(res)}`);
    return res.json();
  },

  async getCuratorChatJob(jobId: string): Promise<{
    id: string;
    status: "pending" | "running" | "completed" | "failed";
    hosting_scope: string;
    reply?: string;
    proposals?: StandardItem[];
    error?: string;
  }> {
    const res = await authFetch(
      `${API_URL}/api/standards/chat/jobs/${encodeURIComponent(jobId)}`,
    );
    if (!res.ok) throw new Error(`Chat poll failed: ${await errorDetail(res)}`);
    return res.json();
  },

  /**
   * Streaming (SSE) variant of the curator chat. Uses a fetch
   * ReadableStream (not EventSource — POST + Bearer JWT). Invokes:
   *   onJob(jobId)      — durable job id, first; remember it so a
   *                       mid-stream refresh can resume via the poll path
   *   onThinking(text)  — extended-thinking (reasoning) deltas
   *   onDelta(text)     — user-facing reply chunks (<propose> stripped)
   *   onProposalProgress(n) — the Nth proposal is being drafted (stream
   *                       goes quiet while its JSON is suppressed)
   *   onProposals(rows) — saved proposal rows, once at the end
   * Resolves when the stream completes; rejects on an `error` frame or
   * HTTP failure. Pass an AbortSignal to cancel mid-stream.
   *
   * Unlike the async-job path, proposals arrive in-band already
   * persisted, so the caller renders them with no read-after-write race.
   */
  async streamCuratorChat(
    messages: RuleChatTurn[],
    hostingScope: HostingScope | undefined,
    handlers: {
      onJob?: (jobId: string) => void;
      onThinking?: (t: string) => void;
      onDelta: (t: string) => void;
      onProposalProgress?: (n: number) => void;
      onProposals?: (rows: StandardItem[]) => void;
    },
    signal?: AbortSignal,
  ): Promise<void> {
    const idToken = await authService.getIdToken();
    const body: Record<string, unknown> = { messages };
    if (hostingScope) body.hosting_scope = hostingScope;
    const res = await fetch(`${API_URL}/api/standards/chat/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
      },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok || !res.body) {
      throw new Error(`Chat failed: ${await errorDetail(res)}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);

        let event = "message";
        let data = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        const parsed = JSON.parse(data);

        if (event === "job") handlers.onJob?.(parsed.job_id ?? "");
        else if (event === "thinking") handlers.onThinking?.(parsed.text ?? "");
        else if (event === "delta") handlers.onDelta(parsed.text ?? "");
        else if (event === "proposal_progress")
          handlers.onProposalProgress?.(parsed.n ?? 0);
        else if (event === "proposals")
          handlers.onProposals?.(parsed.proposals ?? []);
        else if (event === "error") throw new Error(parsed.detail ?? "stream error");
        else if (event === "done") return;
      }
    }
  },

  async chatWithRulesCurator(
    messages: RuleChatTurn[],
    hostingScope?: HostingScope,
  ): Promise<RuleChatResponse> {
    // Kept for callers that want the simple awaitable shape.
    // Internally this is start + poll. The Curate page uses
    // startCuratorChatJob directly so the global useCuratorJobs
    // hook can survive navigation.
    const { job_id: jobId } = await this.startCuratorChatJob(messages, hostingScope);

    const startedAt = Date.now();
    const HARD_TIMEOUT_MS = 5 * 60 * 1000;
    let intervalMs = 1500;

    while (true) {
      const elapsed = Date.now() - startedAt;
      if (elapsed > HARD_TIMEOUT_MS) {
        throw new Error(
          "Chat timed out after 5 minutes. The model may still be running; refresh and try again in a moment.",
        );
      }
      const job = await this.getCuratorChatJob(jobId);
      if (job.status === "completed") {
        return {
          reply: job.reply || "(no reply)",
          proposals: job.proposals || [],
        };
      }
      if (job.status === "failed") {
        throw new Error(job.error || "Chat job failed");
      }
      await new Promise((r) => setTimeout(r, intervalMs));
      if (elapsed > 30_000 && intervalMs < 5000) {
        intervalMs = Math.min(intervalMs * 1.3, 5000);
      }
    }
  },

  async approveStandard(id: string): Promise<StandardItem> {
    const res = await authFetch(`${API_URL}/api/standards/${id}/approve`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`Failed to approve standard: ${res.statusText}`);
    return res.json();
  },

  async rejectStandard(id: string): Promise<{ message: string }> {
    const res = await authFetch(`${API_URL}/api/standards/${id}/reject`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`Failed to reject standard: ${res.statusText}`);
    return res.json();
  },

  // References API. Mirrors the standards CRUD shape but lives at
  // /api/references and uses the ReferenceItem type.
  async listReferences(opts: { status?: ReferenceStatus; hostingScope?: HostingScope } = {}): Promise<{ references: ReferenceItem[]; total: number }> {
    const params = new URLSearchParams();
    if (opts.status) params.set("status", opts.status);
    if (opts.hostingScope) params.set("hosting_scope", opts.hostingScope);
    const qs = params.toString();
    const url = qs ? `${API_URL}/api/references?${qs}` : `${API_URL}/api/references`;
    const res = await authFetch(url);
    if (!res.ok) throw new Error(`Failed to list references: ${res.statusText}`);
    return res.json();
  },

  async getReference(id: string): Promise<ReferenceItem> {
    const res = await authFetch(`${API_URL}/api/references/${id}`);
    if (!res.ok) throw new Error(`Failed to fetch reference: ${res.statusText}`);
    return res.json();
  },

  async createReference(body: CreateReferenceRequest): Promise<ReferenceItem> {
    const res = await authFetch(`${API_URL}/api/references`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Failed to create reference: ${await errorDetail(res)}`);
    return res.json();
  },

  async updateReference(id: string, body: Partial<CreateReferenceRequest>): Promise<ReferenceItem> {
    const res = await authFetch(`${API_URL}/api/references/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Failed to update reference: ${await errorDetail(res)}`);
    return res.json();
  },

  async deleteReference(id: string): Promise<void> {
    const res = await authFetch(`${API_URL}/api/references/${id}`, { method: "DELETE" });
    if (!res.ok && res.status !== 204) throw new Error(`Failed to delete reference: ${res.statusText}`);
  },

  async getStandard(id: string): Promise<StandardItem> {
    const res = await authFetch(`${API_URL}/api/standards/${id}`);
    if (!res.ok) throw new Error(`Failed to get standard: ${res.statusText}`);
    return res.json();
  },

  async createStandard(data: CreateStandardData): Promise<StandardItem> {
    const res = await authFetch(`${API_URL}/api/standards`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error(`Failed to create standard: ${res.statusText}`);
    return res.json();
  },

  async updateStandard(id: string, data: UpdateStandardData): Promise<StandardItem> {
    const res = await authFetch(`${API_URL}/api/standards/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error(`Failed to update standard: ${res.statusText}`);
    return res.json();
  },

  async deleteStandard(id: string): Promise<{ message: string }> {
    const res = await authFetch(`${API_URL}/api/standards/${id}`, {
      method: "DELETE",
    });
    if (!res.ok) throw new Error(`Failed to delete standard: ${res.statusText}`);
    return res.json();
  },

  async extractStandards(documentId: string): Promise<{ message: string; job_id: string; status: string }> {
    const res = await authFetch(`${API_URL}/api/standards/extract?document_id=${documentId}`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`Failed to extract standards: ${res.statusText}`);
    return res.json();
  },

  /**
   * Dual-mode extraction status.
   *
   * Default (no opts): snapshot of every active/recent job. Used by
   * the always-visible banner. The Standards page reads
   * `is_extracting` and `total_extracted` from this shape.
   *
   * With `documentId` (+ optional `sinceSeq`): tail-poll one job's
   * live log. Returns one job + the slice of log entries with
   * seq > sinceSeq so the client can incrementally append.
   */
  async getExtractionStatus(opts?: {
    documentId?: string;
    sinceSeq?: number;
  }): Promise<{
    // Snapshot mode (default)
    is_extracting?: boolean;
    active_jobs?: Array<{
      document_id: string;
      filename: string;
      status: string;
      rules_saved: number;
      log_seq: number;
    }>;
    completed_jobs?: Array<unknown>;
    failed_jobs?: Array<unknown>;
    total_extracted: number;
    total_standards?: number;
    // Per-document tail mode
    document_id?: string;
    job?: {
      document_id: string;
      filename: string;
      status: "starting" | "extracting" | "enriching" | "completed" | "failed";
      started_at: string;
      completed_at: string | null;
      error: string | null;
      rules_saved: number;
      log_seq: number;
    } | null;
    log?: Array<{ seq: number; ts: number; level: string; message: string }>;
  }> {
    const params = new URLSearchParams();
    if (opts?.documentId) params.set("document_id", opts.documentId);
    if (opts?.sinceSeq) params.set("since_seq", String(opts.sinceSeq));
    const qs = params.toString();
    const url = `${API_URL}/api/standards/extraction-status${qs ? `?${qs}` : ""}`;
    const res = await authFetch(url);
    if (!res.ok) throw new Error(`Failed to get extraction status: ${res.statusText}`);
    return res.json();
  },

  // --- Queries ---

  async submitQuery(question: string, documentId?: string): Promise<QueryResponse> {
    const res = await authFetch(`${API_URL}/api/queries`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, document_id: documentId }),
    });

    if (!res.ok) throw new Error(`Query failed: ${await errorDetail(res)}`);
    return res.json();
  },

  /**
   * Streaming (SSE) variant of submitQuery. Runs the tool-using query
   * agent and surfaces its work live via a fetch ReadableStream:
   *   onThinking(text) — extended-thinking (reasoning) deltas
   *   onTool(text)     — each tool invocation (kb_search, calculate, …)
   *   onDelta(text)    — answer text, token by token
   * Resolves when the stream completes; rejects on an `error` frame or
   * HTTP failure. Pass an AbortSignal to cancel mid-stream.
   */
  async streamQuery(
    question: string,
    documentId: string | undefined,
    handlers: {
      onThinking?: (t: string) => void;
      onTool?: (t: string) => void;
      onDelta: (t: string) => void;
    },
    signal?: AbortSignal,
  ): Promise<void> {
    const idToken = await authService.getIdToken();
    const res = await fetch(`${API_URL}/api/queries/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
      },
      body: JSON.stringify({ question, document_id: documentId }),
      signal,
    });
    if (!res.ok || !res.body) {
      throw new Error(`Query failed: ${await errorDetail(res)}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);

        let event = "message";
        let data = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        const parsed = JSON.parse(data);

        if (event === "thinking") handlers.onThinking?.(parsed.text ?? "");
        else if (event === "tool") handlers.onTool?.(parsed.text ?? "");
        else if (event === "delta") handlers.onDelta(parsed.text ?? "");
        else if (event === "error") throw new Error(parsed.detail ?? "stream error");
        else if (event === "done") return;
      }
    }
  },

  // --- Pipeline Status ---

  async getPipelineStatus(): Promise<{ active_jobs: number; jobs: unknown[] }> {
    const res = await authFetch(`${API_URL}/api/documents/pipeline/status`);
    if (!res.ok) {
      throw new Error(
        `Failed to get pipeline status: ${await errorDetail(res)}`,
      );
    }
    return res.json();
  },

  // --- Audit trail ---

  /** Fetch the audit history for one entity (newest first). Best-effort:
   *  returns [] on any error so a history drawer degrades gracefully. */
  async getAuditEvents(
    entityType: string,
    entityId: string,
  ): Promise<AuditEvent[]> {
    try {
      const res = await authFetch(
        `${API_URL}/api/audit?entity_type=${encodeURIComponent(entityType)}&entity_id=${encodeURIComponent(entityId)}`,
      );
      if (!res.ok) return [];
      const data = await res.json();
      return data.events ?? [];
    } catch {
      return [];
    }
  },

  // --- Custom icon legend ---

  /** List legend entries. THROWS on failure: the review queue must be able to
   *  tell "nothing to review" from "we couldn't load your review queue" — a
   *  curator with pending drafts seeing an empty page would silently skip the
   *  approval step the whole feature depends on. */
  async listIconLegend(status: string = "active", batch?: string): Promise<IconLegendEntry[]> {
    const q = new URLSearchParams({ status });
    if (batch) q.set("batch", batch);
    const res = await authFetch(`${API_URL}/api/icon-legend?${q.toString()}`);
    if (!res.ok) throw new Error(`Failed to load icon legend: ${await errorDetail(res)}`);
    return (await res.json()).icons ?? [];
  },

  async createIcon(entry: {
    label: string;
    icon_type: string;
    description?: string;
    hosting_scope?: string;
  }): Promise<IconLegendEntry> {
    const res = await authFetch(`${API_URL}/api/icon-legend`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(entry),
    });
    if (!res.ok) {
      let d = res.statusText;
      try { d = (await res.json()).detail || d; } catch { /* noop */ }
      throw new Error(d);
    }
    return res.json();
  },

  /** AI-polish a text description; returns {original, polished} (no persist). */
  async polishIconDescription(entry: {
    label: string;
    icon_type: string;
    description: string;
  }): Promise<{ original: string; polished: string }> {
    const res = await authFetch(`${API_URL}/api/icon-legend/polish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(entry),
    });
    if (!res.ok) throw new Error(`Polish failed: ${res.statusText}`);
    return res.json();
  },

  /** IMAGE mode: upload → model derives the definition → returns a DRAFT. */
  async interpretIconImage(file: File, hostingScope = "on_prem"): Promise<IconLegendEntry> {
    const form = new FormData();
    form.append("file", file);
    form.append("hosting_scope", hostingScope);
    const res = await authFetch(`${API_URL}/api/icon-legend/interpret-image`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) {
      let d = res.statusText;
      try { d = (await res.json()).detail || d; } catch { /* noop */ }
      throw new Error(d);
    }
    return res.json();
  },

  /** Fetch an icon's stored image with auth and return a blob URL the browser
   *  can put on an <img>. Returns null when the icon has no image or the fetch
   *  fails (so the caller renders a placeholder). Caller revokes on unmount. */
  async getIconImageUrl(iconId: string): Promise<string | null> {
    try {
      const res = await authFetch(`${API_URL}/api/icon-legend/${iconId}/image`);
      if (!res.ok) return null;
      const blob = await res.blob();
      return URL.createObjectURL(blob);
    } catch {
      return null;
    }
  },

  /** Re-run interpretation on an entry's existing image → re-drafts it. */
  async reinterpretIcon(iconId: string): Promise<IconLegendEntry> {
    const res = await authFetch(`${API_URL}/api/icon-legend/${iconId}/reinterpret`, {
      method: "POST",
    });
    if (!res.ok) {
      let d = res.statusText;
      try { d = (await res.json()).detail || d; } catch { /* noop */ }
      throw new Error(d);
    }
    return res.json();
  },

  /** Edit an active/draft entry's content (label/type/description). Does not
   *  change approval state — activation flows only through approveIcon. */
  async updateIcon(
    iconId: string,
    fields: { label?: string; icon_type?: string; description?: string; hosting_scope?: string },
  ): Promise<IconLegendEntry> {
    const res = await authFetch(`${API_URL}/api/icon-legend/${iconId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    if (!res.ok) {
      let d = res.statusText;
      try { d = (await res.json()).detail || d; } catch { /* noop */ }
      throw new Error(d);
    }
    return res.json();
  },

  /** Approve a draft → active, optionally applying edits. */
  async approveIcon(
    iconId: string,
    edits?: { label?: string; icon_type?: string; description?: string; hosting_scope?: string },
  ): Promise<IconLegendEntry> {
    const res = await authFetch(`${API_URL}/api/icon-legend/${iconId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(edits ?? {}),
    });
    if (!res.ok) throw new Error(`Approve failed: ${res.statusText}`);
    return res.json();
  },

  async deleteIcon(iconId: string): Promise<void> {
    const res = await authFetch(`${API_URL}/api/icon-legend/${iconId}`, { method: "DELETE" });
    if (!res.ok && res.status !== 204) throw new Error(`Failed to delete icon: ${res.statusText}`);
  },

  async uploadIconImage(iconId: string, file: File): Promise<IconLegendEntry> {
    const form = new FormData();
    form.append("file", file);
    const res = await authFetch(`${API_URL}/api/icon-legend/${iconId}/image`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) {
      let d = res.statusText;
      try { d = (await res.json()).detail || d; } catch { /* noop */ }
      throw new Error(d);
    }
    return res.json();
  },

  /** Pair image files to a text batch's drafts by declared `image_filename`.
   *  Every pair is cross-checked against the row's written definition, and
   *  unmatched files/rows come back so a partial pairing can't read as done. */
  async pairIconBatchImages(batchId: string, files: File[]): Promise<IconPairResult> {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    const res = await authFetch(`${API_URL}/api/icon-legend/bulk/${batchId}/pair-images`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) throw new Error(`Pairing failed: ${await errorDetail(res)}`);
    return res.json();
  },

  /** Bulk text (CSV/JSON) → drafts. Returns {batch_id, created, errors}. */
  async bulkIconText(file: File): Promise<{ batch_id: string; created: number; errors: Array<{ row: number; error: string }> }> {
    const form = new FormData();
    form.append("file", file);
    const res = await authFetch(`${API_URL}/api/icon-legend/bulk/text`, { method: "POST", body: form });
    if (!res.ok) {
      let d = res.statusText;
      try { d = (await res.json()).detail || d; } catch { /* noop */ }
      throw new Error(d);
    }
    return res.json();
  },

  /** Bulk images → async interpret job. Returns {job_id, batch_id, total, skipped}. */
  async bulkIconImages(files: File[], hostingScope = "on_prem"): Promise<{ job_id: string; batch_id: string; total: number; skipped: Array<{ file: string; error: string }> }> {
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    form.append("hosting_scope", hostingScope);
    const res = await authFetch(`${API_URL}/api/icon-legend/bulk/images`, { method: "POST", body: form });
    if (!res.ok) {
      let d = res.statusText;
      try { d = (await res.json()).detail || d; } catch { /* noop */ }
      throw new Error(d);
    }
    return res.json();
  },

  async iconBulkJobStatus(jobId: string): Promise<{ job_id: string; batch_id: string; total: number; done: number; failed: number; status: string }> {
    const res = await authFetch(`${API_URL}/api/icon-legend/bulk/jobs/${jobId}`);
    if (!res.ok) throw new Error(`Job status failed: ${res.statusText}`);
    return res.json();
  },

  async approveIconBatch(batchId: string): Promise<{ batch_id: string; approved: number }> {
    const res = await authFetch(`${API_URL}/api/icon-legend/bulk/${batchId}/approve-all`, { method: "POST" });
    if (!res.ok) throw new Error(`Batch approve failed: ${res.statusText}`);
    return res.json();
  },

  /** Reject (delete) every DRAFT in a batch. Already-approved entries in the
   *  same batch are left alone — they are active and may be shaping prompts. */
  async rejectIconBatch(batchId: string): Promise<{ batch_id: string; rejected: number }> {
    const res = await authFetch(`${API_URL}/api/icon-legend/bulk/${batchId}/reject-all`, { method: "POST" });
    if (!res.ok) throw new Error(`Batch reject failed: ${await errorDetail(res)}`);
    return res.json();
  },

  // --- Health Check ---

  async checkHealth(): Promise<boolean> {
    try {
      const res = await authFetch(`${API_URL}/api/health`);
      return res.ok;
    } catch {
      return false;
    }
  },
};

export interface AuditEvent {
  event_id: string;
  action: string;
  entity_type: string;
  entity_id: string;
  actor_email: string;
  actor_role: string;
  ts: string;
  detail?: Record<string, unknown>;
}

export interface IconLegendEntry {
  id: string;
  label: string;
  icon_type: string;
  description?: string;
  hosting_scope?: string;
  image_s3_key?: string | null;
  status?: string; // "active" | "draft"
  source_kind?: string; // "text" | "image"
  ai_generated?: boolean;
  ai_model?: string;
  confidence?: string; // "high" | "medium" | "low"
  bulk_batch_id?: string | null;
  created_by?: string;
  /** Who approved this draft → active, and when (self-describing provenance
   *  on the row, not just the audit log). Absent on text-authored entries
   *  and on rows created before these fields existed. */
  approved_by?: string;
  approved_at?: string;
  /** Declared image filename from a bulk text row, used to pair an uploaded
   *  file to this row. */
  image_filename?: string;
  /** Verdict from cross-checking an attached image against this entry's
   *  human-written definition. `checked: false` means the model was
   *  unavailable — unverified, NOT a pass. */
  image_check?: IconImageCheck;
  /** Set on the attach response when a mismatch returned an active entry to
   *  draft. Not persisted on the row. */
  image_check_demoted?: boolean;
}

export interface IconImageCheck {
  consistent: boolean;
  checked: boolean;
  confidence?: string;
  observed?: string;
  reason?: string;
  model?: string;
  checked_at?: string;
}

export interface IconPairResult {
  batch_id: string;
  paired: Array<{ file: string; icon_id: string; label: string }>;
  mismatches: Array<{ file: string; icon_id: string; label: string; reason: string; observed: string }>;
  unmatched_files: string[];
  unmatched_rows: Array<{ icon_id: string; label: string; expected_file: string }>;
  failures: Array<{ file: string; error: string }>;
}

export const ICON_TYPES = [
  "server", "database", "load_balancer", "datacenter", "network",
  "storage", "service", "firewall", "gateway", "other",
] as const;
