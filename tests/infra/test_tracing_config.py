# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guards for the distributed-tracing wiring (infrastructure/modules/tracing).

Tracing only works when three independent pieces line up, and each fails
SILENTLY on its own — the app keeps serving traffic and you simply get no
traces, which is the hardest kind of regression to notice:

  1. the container runs under `opentelemetry-instrument` (backend/Dockerfile),
  2. the task role may export spans to X-Ray (modules/ecs), and
  3. Transaction Search routes segments into the `aws/spans` log group
     (modules/tracing), which is what the CloudWatch Application Signals /
     GenAI views read.

These are static assertions: no AWS account, no Terraform state, no model calls,
so they run in CI on every push.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TRACING_TF = REPO / "infrastructure" / "modules" / "tracing" / "main.tf"
ECS_TF = REPO / "infrastructure" / "modules" / "ecs" / "main.tf"
ROOT_TF = REPO / "infrastructure" / "main.tf"
BACKEND_DOCKERFILE = REPO / "backend" / "Dockerfile"
BACKEND_REQS = REPO / "backend" / "requirements.txt"


def _read(path: Path) -> str:
    if not path.is_file():
        pytest.fail(f"expected file is missing: {path.relative_to(REPO)}")
    return path.read_text(encoding="utf-8")


def test_container_runs_under_the_otel_wrapper():
    """Without the wrapper nothing is instrumented and no span is ever created."""
    dockerfile = _read(BACKEND_DOCKERFILE)
    assert "opentelemetry-instrument" in dockerfile, (
        "backend/Dockerfile CMD must run under opentelemetry-instrument, or the "
        "app emits no spans and the tracing module has nothing to index"
    )
    reqs = _read(BACKEND_REQS)
    assert "aws-opentelemetry-distro" in reqs, (
        "aws-opentelemetry-distro provides the opentelemetry-instrument wrapper "
        "the Dockerfile CMD invokes; without it the container fails to start"
    )


def test_otel_distro_pins_are_coherent():
    """The distro does not constrain instrumentation-threading, and an
    unpinned resolve picks a version needing a different
    opentelemetry-instrumentation than the distro pins — an incoherent set that
    `pip check` flags. Both must stay pinned together."""
    reqs = _read(BACKEND_REQS)
    assert "opentelemetry-instrumentation-threading==" in reqs, (
        "pin opentelemetry-instrumentation-threading alongside "
        "aws-opentelemetry-distro to keep the dependency set coherent"
    )
    assert "pyyaml==6.0.3" in reqs.lower(), (
        "aws-opentelemetry-distro pins pyyaml exactly; holding an older pin "
        "makes the install unresolvable"
    )


def test_exporter_targets_xray_not_a_missing_collector():
    """The distro must be in agent-observability mode.

    Its DEFAULT is OTLP/gRPC to localhost:4317 — a collector sidecar that does
    not exist in this task. Without AGENT_OBSERVABILITY_ENABLED the app still
    serves traffic but every span export fails against localhost, which is a
    silent loss of all tracing (verified live before this was set). Setting it
    switches the endpoint to the SigV4-signed X-Ray OTLP endpoint and turns on
    GenAI message capture.
    """
    root = _read(ROOT_TF)
    assert "AGENT_OBSERVABILITY_ENABLED" in root, (
        "set AGENT_OBSERVABILITY_ENABLED=true on the task, or the ADOT distro "
        "exports to a nonexistent localhost collector and no trace is recorded"
    )
    # The redundant/unpermitted exporters must be explicitly off, not left to the
    # distro's defaults for this mode (otlp logs + awsemf metrics).
    for var in ("OTEL_LOGS_EXPORTER", "OTEL_METRICS_EXPORTER"):
        assert var in root, f"{var} should be set explicitly to avoid a second failing export path"

    # Installing the ADOT distro registers a second OpenTelemetry
    # distro/configurator pair, and the loader takes whichever it resolves
    # first. If the vanilla configurator wins, the AWS one is skipped with only
    # an INFO line ("Configuration of aws_configurator not loaded, configurator
    # already loaded") and AGENT_OBSERVABILITY_ENABLED is silently ignored —
    # every signal then goes to localhost:4317. Observed live. Selecting both
    # explicitly is the only thing that makes it deterministic.
    for var, expected in (
        ("OTEL_PYTHON_DISTRO", "aws_distro"),
        ("OTEL_PYTHON_CONFIGURATOR", "aws_configurator"),
    ):
        assert var in root, (
            f"{var} must be set to {expected!r}: without it the AWS distro's "
            "_configure() may never run and tracing silently targets a "
            "nonexistent local collector"
        )


def test_task_role_can_export_spans():
    """Span export needs X-Ray write permissions on the TASK role.

    Missing IAM does not crash the app — export just fails in the background —
    so this is guarded rather than left to be discovered by absent traces.
    """
    ecs = _read(ECS_TF)
    # PutSpans backs the OTLP ingestion endpoint the distro posts to; without it
    # export is rejected even though the app is healthy.
    for action in ("xray:PutSpans", "xray:PutTraceSegments", "xray:PutTelemetryRecords"):
        assert action in ecs, f"task role is missing {action}; spans cannot be exported"
    assert "aws_iam_role_policy_attachment" in ecs and "tracing_to_task_role" in ecs, (
        "the tracing policy must be ATTACHED to the task role, not merely created"
    )


def test_transaction_search_routes_spans_to_cloudwatch():
    """aws/spans only receives data once the trace destination is CloudWatchLogs
    AND X-Ray holds a log resource policy allowing the write."""
    tracing = _read(TRACING_TF)
    assert "aws_cloudwatch_log_resource_policy" in tracing, (
        "X-Ray needs a CloudWatch Logs resource policy to write span records"
    )
    assert "xray.amazonaws.com" in tracing, "the resource policy must grant the X-Ray service principal"
    assert "update-trace-segment-destination" in tracing, (
        "Transaction Search requires xray:UpdateTraceSegmentDestination=CloudWatchLogs; "
        "there is no native TF resource, hence the provisioner"
    )
    assert "aws/spans" in tracing, "the resource policy must cover the aws/spans log group"


def test_resource_policy_is_scoped_to_this_account():
    """Guard against a confused-deputy: the policy names a service principal, so
    it must constrain SourceAccount/SourceArn to this account's X-Ray."""
    tracing = _read(TRACING_TF)
    assert "aws:SourceAccount" in tracing and "aws:SourceArn" in tracing, (
        "the X-Ray log resource policy must be conditioned on SourceAccount and "
        "SourceArn so another account cannot use it"
    )


def test_tracing_is_always_on_not_flag_gated():
    """Tracing is core, not optional.

    It replaced the AgentCore-runtime-scoped observability wiring; making it
    conditional would recreate the situation where the deployment you shipped
    had no traces because a flag defaulted off.
    """
    root = _read(ROOT_TF)
    assert 'module "tracing"' in root, "the tracing module must be instantiated by the root"
    tracing_block = root[root.index('module "tracing"') :]
    tracing_block = tracing_block[: tracing_block.index("\n}") + 2]
    # Match a count ASSIGNMENT, not the substring: prose in the block's comments
    # legitimately contains words like "account".
    assert not re.search(r"^\s*count\s*=", tracing_block, re.MULTILINE), (
        "the tracing module must not be count-gated — distributed tracing is a "
        "core capability of this stack"
    )


def test_no_agentcore_references_remain_in_infrastructure():
    """The AgentCore runtime/gateway/memory/Cedar stack was removed.

    Only genuinely portable pieces were kept (tracing, and the app-level
    evaluation harness). A lingering reference means a half-removed stack: a
    variable nothing consumes, or IAM for a resource that no longer exists.
    """
    infra = REPO / "infrastructure"
    offenders = []
    for path in infra.rglob("*.tf"):
        if ".terraform" in path.parts:
            continue
        # Comment lines are excluded deliberately: a historical note explaining
        # WHY the AgentCore stack was removed is useful documentation. What must
        # not survive is functional config — a variable, resource or IAM
        # statement for infrastructure that no longer exists.
        config = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        if "agentcore" in config.lower():
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, f"stale AgentCore references remain in: {offenders}"
