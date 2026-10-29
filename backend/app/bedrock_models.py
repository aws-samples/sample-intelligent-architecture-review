# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock model IDs — the single place a model version is written down.

Every model-using call site takes its default from one of the tier constants
below and stays individually overridable by its own env var, so you can pin one
pipeline stage to a different model without touching code. A model migration is
therefore a one-line edit here rather than a hunt through a dozen files.

Choosing a tier:

  SONNET          the default workhorse. Text extraction, chunked validation,
                  curator chat, query answering — anything where throughput and
                  cost matter more than the last few points of reasoning depth.
  OPUS_REASONING  hard judgement calls on text: tier classification and rule
                  curation, where a wrong answer is expensive.
  OPUS_VISION     diagram work. Topology extraction and its adversarial
                  self-consistency pass, plus icon interpretation — these read
                  pixels, and vision quality is what drives their accuracy.

The IDs are cross-region inference profiles (the ``us.`` prefix). Bedrock IAM is
already scoped to ``inference-profile/us.anthropic.*``, so switching to a
``global.`` profile means widening that policy too.
"""

from __future__ import annotations

import os

# --- model tiers ------------------------------------------------------------

SONNET = "us.anthropic.claude-sonnet-4-6"
OPUS_REASONING = "us.anthropic.claude-opus-4-7"
OPUS_VISION = "us.anthropic.claude-opus-4-8"


def model_for(env_var: str, default: str) -> str:
    """Resolve a per-stage model override, falling back to a tier constant.

    Empty or whitespace-only env values fall back too, so an env var that is
    "set but blank" (a common deploy accident) can't produce an invalid modelId.
    """
    return (os.getenv(env_var) or "").strip() or default


# --- shared agent inference settings ----------------------------------------
#
# Extended-thinking budget for the query agent, resolved here rather than at the
# call site so every inference setting is declared in one file alongside the
# model tiers. > 0 enables Bedrock extended thinking (the streaming query route
# surfaces the reasoning deltas as `thinking` SSE events); 0 disables it.
QUERY_REASONING_BUDGET = int(os.getenv("QUERY_REASONING_BUDGET", "1024"))


# --- capability checks ------------------------------------------------------

# Region prefixes a cross-region inference profile can carry. Used only for
# capability sniffing on an id that may have been overridden by env.
_REGION_PREFIXES = ("us", "global", "eu", "apac")


def is_opus(model_id: str) -> bool:
    """True when the id names an Opus model, whatever region profile it carries.

    Opus takes ``thinking: {"type": "adaptive"}`` with no token budget, while
    Sonnet expects an explicit budget — so a caller sending the wrong shape gets
    a ValidationException. Since any stage's model can be overridden by env, the
    request shape has to be chosen from the *resolved* id rather than assumed.
    """
    return any(
        model_id.startswith(f"{prefix}.anthropic.claude-opus")
        for prefix in _REGION_PREFIXES
    )
