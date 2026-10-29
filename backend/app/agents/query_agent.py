# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Query Agent - Strands agent for natural language queries over documents."""

import os


from strands import Agent
from strands.models import BedrockModel

from app.agents.tools.document_tools import get_document_text, get_document_diagrams_info
from app.agents.tools.kb_tools import kb_search, get_validation_results_summary
from app.agents.tools.standards_tools import get_all_standards
from app.agents.tools.validation_tools import get_past_validations
from app.agents.tools.calculator_tools import calculate, count_findings
from app.bedrock_models import SONNET, model_for
from app.services.bedrock_guardrail import strands_guardrail_kwargs

SYSTEM_PROMPT = """You are an AI assistant for an enterprise resilience review system.

You help architecture teams understand their infrastructure design documents and HA compliance status.

## AVAILABLE TOOLS:
- kb_search: Search across all indexed documents for relevant content
- get_document_text: Get full content of a specific document
- get_validation_results_summary: See all past validation scores and findings
- get_all_standards: See what HA rules are configured
- get_past_validations: See validation history for a specific document
- calculate: Evaluate math expressions (use for percentages, counts, averages — never compute in your head)
- count_findings: Get accurate breakdown of findings by status and severity

## STRATEGY:
- For factual questions ("What is the RTO?"): use kb_search or get_document_text
- For compliance questions ("Does it meet standards?"): check get_validation_results_summary first
- For comparison questions: search multiple documents via kb_search
- For risk questions: combine kb_search + validation results
- Always cite sources with page numbers when possible
- If you can't find the answer, say so clearly

## SYSTEM STATE vs DOCUMENT CONTENT — do not confuse these:
Two different things can both be called a "standard", and they have different
answers:
  - The RULE LIBRARY: the standards configured in THIS system, which validation
    actually runs against. Questions about what is "configured", "active",
    "enabled", "in the library", or "how many rules/standards do we have" are
    about system state. Answer them with **get_all_standards** (and
    get_validation_results_summary for outcomes). Do NOT answer them by counting
    requirements found in a document.
  - DOCUMENT CONTENT: requirements written in an uploaded policy document. A
    document may describe fifty requirements while the library has none active,
    because extracted rules stay as proposals until a curator approves them.
So "how many standards are configured?" and "how many requirements does this
policy document contain?" are different questions — read the question carefully
and pick the matching tool. If get_all_standards reports none configured, say so
plainly rather than substituting a count from a document.

## RULES:
- Use tools to find information - do not guess
- Cite specific pages/sections in your answers
- For complex questions, break them down and search multiple times
- Provide actionable insights, not just data
"""


def create_query_agent(
    callback_handler=None,
    reasoning_budget: int = 0,
    exclude_tools: set[str] | None = None,
    extra_tools: list | None = None,
) -> Agent:
    """Create and return a configured query agent.

    ``callback_handler`` — optional Strands callback. The streaming query
    endpoint passes one that forwards token/tool/reasoning events onto an
    SSE queue; the synchronous path leaves it None (Strands' default).

    ``reasoning_budget`` — when > 0, enable Bedrock extended thinking with
    this token budget so the agent emits reasoningContent deltas. Anthropic
    requires temperature=1 with reasoning on, so we drop the temperature
    override in that mode and ensure max_tokens exceeds the budget.

    ``exclude_tools`` — tool function names to drop; ``extra_tools`` — extra
    tool objects to append. Both default to no-ops, so callers that want the
    standard tool set pass neither. They exist so a caller can compose a
    narrower agent (e.g. a test double, or serving one tool from elsewhere)
    without forking this factory.
    """
    model_kwargs = {
        "model_id": model_for("BEDROCK_MODEL_ID", SONNET),
        "region_name": os.getenv("AWS_REGION", "us-east-1"),
        "max_tokens": 8000,
    }
    if reasoning_budget > 0:
        model_kwargs["max_tokens"] = max(8000, reasoning_budget + 2000)
        model_kwargs["additional_request_fields"] = {
            "reasoning_config": {"type": "enabled", "budget_tokens": reasoning_budget}
        }
        # temperature must be 1 (Anthropic constraint) when thinking is on;
        # omit the override so BedrockModel doesn't send temperature=0.3.
    else:
        model_kwargs["temperature"] = 0.3

    # Strands takes flat guardrail_* keywords rather than the Converse
    # guardrailConfig shape, so this path cannot reuse the client wrapper.
    # Empty when no guardrail is configured.
    model_kwargs.update(strands_guardrail_kwargs())
    model = BedrockModel(**model_kwargs)

    # Tool functions keyed by name so callers can exclude specific ones.
    _tools_by_name = {
        "kb_search": kb_search,
        "get_document_text": get_document_text,
        "get_document_diagrams_info": get_document_diagrams_info,
        "get_validation_results_summary": get_validation_results_summary,
        "get_all_standards": get_all_standards,
        "get_past_validations": get_past_validations,
        "calculate": calculate,
        "count_findings": count_findings,
    }
    excluded = exclude_tools or set()
    tools = [fn for name, fn in _tools_by_name.items() if name not in excluded]
    if extra_tools:
        tools.extend(extra_tools)

    agent_kwargs = {
        "model": model,
        "system_prompt": SYSTEM_PROMPT,
        "tools": tools,
    }
    if callback_handler is not None:
        agent_kwargs["callback_handler"] = callback_handler
    return Agent(**agent_kwargs)
