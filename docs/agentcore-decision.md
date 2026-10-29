# Decision record: Amazon Bedrock AgentCore

**Decision — the managed AgentCore runtime was evaluated for the conversational
query agent, built end to end, and then removed.** Distributed tracing and an
evaluation gate were kept and generalised to the whole application. This document
records why, what replaced it, and the conditions under which the decision should
be revisited.

## Why the runtime was removed

The runtime was not a scaling improvement, which was the assumption worth testing:

- The query agent is single-turn, stateless and takes roughly eight seconds.
- The ECS service already target-tracks CPU and memory across `min_capacity` to
  `max_capacity` tasks.
- The genuinely heavy work stays on ECS either way: validation fans out 25
  concurrent Bedrock `Converse` calls, and extraction runs a model call per
  document unit.

So the runtime relocated the *lightest* workload while adding a second container
image, an ARM64 build, roughly five-minute replace-on-change cycles, and a
dependency on preview-stage provider resources.

| Component | Outcome |
|---|---|
| Runtime | Removed — see above. |
| Memory | Removed. It was created and billed but never bound to the agent: the reference session manager requires strands-agents 1.x and the backend is pinned to 0.3.0. Its only purpose was to serve a runtime session. |
| Gateway, Lambda tool targets, machine-to-machine client | Removed. A gateway serves tools *to a runtime over MCP*; with no runtime there is no consumer, and the tools already run in-process. |
| Cedar policy engine | Removed, and it could not have been kept in any form. A policy engine attaches to `aws_bedrockagentcore_gateway.policy_engine_configuration`, so no runtime means no gateway means nothing to attach a policy to. |

The policy layer that remains was already part of the core stack: an Amazon
Bedrock guardrail redacting PII and credentials on every model call, plus Cognito
role-based access control. Per-tool authorization, if it is ever needed, belongs
in the application rather than in a gateway.

## What was kept

| Capability | Where it lives |
|---|---|
| Distributed tracing | The one genuinely portable piece. Instrumentation moved from the runtime container onto the ECS backend (`opentelemetry-instrument` in `backend/Dockerfile`), and `infrastructure/modules/tracing` enables CloudWatch Transaction Search so spans are indexed in `aws/spans`. This traces extraction, validation **and** query — broader coverage than the runtime version, which only ever saw the query agent. |
| Evaluation gate | `backend/evaluation/run_eval.py`. The AgentCore version was runtime-bound: `agent_id` *was* the runtime id and the built-in evaluators read runtime spans. It now drives the deployed API and scores answers with a Bedrock judge, exiting non-zero below a threshold. It also exercises auth, the route, the guardrail and the data plane, which a span-only evaluator does not. |

Both are always on. Neither is behind a feature flag, because a flag defaulting
to off is how a deployment ends up with no traces and no gate.

## Are the built-in AgentCore evaluators usable without a runtime? No

Worth re-checking, since Transaction Search is now enabled and the application's
spans carry `gen_ai.*` attributes in `aws/spans` — which makes it look as though
the built-in evaluators could score the ECS service. Checked against the
installed `bedrock-agentcore-starter-toolkit` rather than assumed:

- The on-demand path fetches spans through
  `ObservabilityClient.query_spans_by_session()`, whose CloudWatch Logs Insights
  query contains:

  ```
  | filter attributes.session.id = '<session_id>'
  | parse resource.attributes.cloud.resource_id "runtime/*/" as parsedAgentId
  | filter parsedAgentId = '<agent_id>'
  ```

  That second filter parses an AgentCore **runtime ARN** out of the
  `cloud.resource_id` resource attribute. A runtime sets it to
  `arn:aws:bedrock-agentcore:…:runtime/<id>/runtime-endpoint/DEFAULT`; an ECS task
  sets it to a task ARN, which has no `runtime/<id>/` segment, so `parsedAgentId`
  is null and the query matches nothing.
- The online path is more tightly coupled still: creating an online evaluation
  config calls `get_agent_runtime()` and derives its log group from the result, so
  it fails outright without a runtime.

Satisfying the filter would mean forging a runtime ARN into `cloud.resource_id`
via `OTEL_RESOURCE_ATTRIBUTES` — publishing telemetry that points at a resource
which does not exist. That would mislead anyone reading the spans and would break
other AgentCore tooling, so it was rejected.

## Engineering findings worth keeping

These cost real debugging time and generalise beyond AgentCore.

1. **Configuration diverges even when code does not.** Both execution paths
   imported the same `create_query_agent`, but the agent's effective configuration
   came from environment variables plus call-site keyword arguments assembled at
   **two independent points**. Three silent regressions followed: the guardrail
   was absent on the managed path, so every model call there ran **unguarded**
   with no PII redaction while the ECS path was guarded; the extended-thinking
   budget was passed by the route but not the runtime entrypoint, changing model
   behaviour and dropping streamed reasoning; and the caller's identity was never
   forwarded, so the actor resolved to `"anonymous"`. None raised an error.
2. **`runtimeUserId` is not readable inside the container.** It reaches the
   control plane and is what `bedrock-agentcore:InvokeAgentRuntimeForUser`
   authorises, but the runtime SDK builds `RequestContext.request_headers` through
   `is_forwardable_header()`, which drops every `x-amzn-*` header that is not
   prefixed `X-Amzn-Bedrock-AgentCore-Runtime-Custom-`. `RequestContext` exposes
   no `runtime_user_id` field either. Identity has to travel in the payload on the
   SigV4 path.
3. **Both invoke actions are required together.** Sending `runtimeUserId` needs
   `InvokeAgentRuntime` *and* `InvokeAgentRuntimeForUser`. Granting only the first
   fails at **request** time with `AccessDeniedException`, so Terraform applies
   cleanly and every query returns 500.
4. **Gate `count` and `for_each` on plan-time-known booleans**, never on an ARN or
   id that comes from a module output. Those are unknown until apply, and a
   fresh-account plan fails with "Invalid count argument".
5. **`aws_bedrockagentcore_gateway_target` was broken in AWS provider 6.56.0.**
   After create it returned a computed `metadata_configuration` block the schema
   did not reconcile, so Terraform reported "Provider produced inconsistent result
   after apply", **tainted** the resource, and destroy/recreated it on every
   subsequent apply. The targets did reach `READY` in AWS — a provider state-read
   defect. `lifecycle`/`ignore_changes` cannot suppress it, because the check
   happens post-apply in Terraform core rather than during diff computation.

## When to revisit

The runtime's real value is **governance**, not scale: gateway-brokered tools over
MCP, per-user tool authorization with Cedar, session isolation for untrusted
documents, and multi-turn memory. Reconsider when at least one of these is true:

- More than one consumer needs the same tools over MCP. Today there is a single
  caller.
- Per-user tool authorization becomes a requirement. This additionally needs a
  dedicated Cognito user pool and a pre-token Lambda carrying a role claim,
  because a pool allows only one pre-token configuration and the existing one is
  already used for federated group mapping.
- The query experience becomes genuinely multi-turn, which needs a strands-agents
  major upgrade — an upgrade that also touches extraction and validation.
