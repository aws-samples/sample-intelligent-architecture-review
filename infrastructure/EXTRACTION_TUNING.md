# Extraction & Validation tuning (deploy-time)

**There is deliberately no runtime UI for editing extraction/validation
strategy.** In a regulated compliance tool, changes to *how the system decides
what is a rule* — the extraction prompts, the model, the chunking — must be an
auditable, reviewed, reproducible change, not a live admin toggle. So tuning
happens at **deploy time** through version-controlled config, which gives you:

- **Review + provenance** — every change is a PR/commit (who, when, approved).
- **Reproducibility** — a released commit pins the exact prompts + models a
  validation ran against; "which config was live?" is answered by the deploy,
  not reconstructed.
- **No guardrail-removal surface** — the extraction prompt's safety invariants
  (extract nothing from blank templates, never invent rules, tier×layer
  semantics, References-vs-rules split) can't be edited away by a runtime user.

## What you can tune, and how

### 1. Mechanical knobs (models, chunking, batching) — via tfvars, no code change

The backend reads these env vars (all have safe code defaults). Override any of
them by adding to `extra_container_env` in your `<env>.tfvars`, then re-deploy
infra + backend. Empty by default → nothing changes.

```hcl
extra_container_env = {
  # Model routing
  EXTRACTION_UNIT_MODEL_ID    = "us.anthropic.claude-sonnet-4-6"
  EXTRACTION_DIAGRAM_MODEL_ID = "us.anthropic.claude-opus-4-8"
  VALIDATION_MODEL_ID         = "us.anthropic.claude-sonnet-4-6"

  # Chunking / batching
  EXTRACTION_PAGES_PER_CHUNK  = "8"
  VALIDATION_MAX_CHUNK_CHARS  = "50000"
  RULES_PER_DOC_BATCH         = "8"

  # Concurrency
  EXTRACTION_MAX_CONCURRENT   = "8"
  VALIDATION_MAX_CONCURRENT   = "25"

  # Accuracy features
  VALIDATION_VERIFY_FAILS      = "1"          # adversarial verify pass on/off
  VALIDATION_VERIFY_SEVERITIES = "critical,high"

  # Extended thinking
  ENABLE_THINKING_MULTIMODAL = "true"
  THINKING_BUDGET_TOKENS     = "8000"

  # Custom icon legend
  ICON_LEGEND_MAX_IMAGES     = "6"
}
```

The full list of recognised keys is in `variables.tf` (`extra_container_env`
description). `BEDROCK_MODEL_ID` is a first-class tfvars var
(`var.bedrock_model_id`) and can also be set directly.

> **Model IDs are free-text here but should be treated as an allowlist by
> convention** — only set models the deploy account has Bedrock access to; a
> typo'd/unavailable id degrades a run. Keep the set small and reviewed.

### 2. Prompts / chunking logic / unit classification — code change + review

These are module constants (not env), changed via a normal PR:

| What | Where |
|---|---|
| Extraction unit system prompt | `backend/agents/extraction/agent.py` |
| Diagram topology extract/verify prompts | `backend/agents/extraction/topology.py` |
| Unit multimodal message assembly | `backend/agents/extraction/multimodal.py` |
| Validation chunk prompt | `backend/app/services/parallel_validator.py` (`_PROMPT_TEMPLATE`) |
| Validation multimodal / PPTX prompts | `backend/app/services/pdf_multimodal.py`, `parallel_validator.py` |
| How pages/slides become units; is_diagram threshold | `backend/agents/extraction/ingestion.py`, `unit.py` |

Changing any of these is a reviewed code change that ships on the next deploy —
which is the intended control for a compliance system.
