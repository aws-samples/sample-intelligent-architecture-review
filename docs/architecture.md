# Architecture

Architecture Compliance Review runs as two Amazon ECS services on AWS Fargate in a single
cluster, with a static frontend served from Amazon S3 through Amazon CloudFront.

## Solution architecture

<div align="center">
<a href="./assets/architecture.png">
  <img src="./assets/architecture.png" alt="Architecture Compliance Review architecture on AWS: users reach the web app through Amazon CloudFront and an Amazon S3 bucket, and sign in with Amazon Cognito federated with a corporate identity provider, with an AWS Lambda function adding role claims. CloudFront forwards API calls to an Application Load Balancer and the API on Amazon ECS with AWS Fargate in a VPC's private subnets, alongside a PowerPoint converter service. The API calls Amazon Bedrock with Amazon Bedrock Guardrails, stores documents in Amazon S3 and state in Amazon DynamoDB, and answers questions from Amazon Bedrock Knowledge Bases backed by Amazon OpenSearch Serverless. Amazon CloudWatch, AWS X-Ray, AWS CloudTrail, AWS KMS, Amazon SNS and Amazon ECR provide observability, audit and security." width="1100" />
</a>
</div>

1. Users open the web app through Amazon CloudFront, which serves the static
   frontend from an Amazon Simple Storage Service (Amazon S3) bucket.
2. Users sign in with Amazon Cognito, optionally federated with a corporate
   identity provider over SAML. An AWS Lambda function maps directory groups to
   the admin, curator, validator, and viewer roles.
3. CloudFront forwards API calls to an Application Load Balancer that accepts
   only CloudFront traffic. The API runs on Amazon Elastic Container Service
   (Amazon ECS) with AWS Fargate in private subnets across two Availability
   Zones, beside a PowerPoint-to-PDF converter. A NAT gateway provides outbound
   access.
4. Uploaded standards and design documents, their rendered pages and diagrams,
   icon-legend images, and validation findings are stored in Amazon S3.
5. Rule extraction, validation, curation, and question answering call models on
   Amazon Bedrock. An Amazon Bedrock Guardrails policy applies to every call.
6. Amazon DynamoDB holds the rule library, references, icon legend, validation
   metadata, user roles, and the audit trail.
7. Amazon Bedrock Knowledge Bases indexes the documents in an Amazon OpenSearch
   Serverless vector index, so answers cite the passages they draw on.
8. Amazon CloudWatch alarms notify through Amazon Simple Notification Service
   (Amazon SNS), AWS X-Ray traces requests, and AWS CloudTrail records API
   activity. AWS Key Management Service (AWS KMS) keys encrypt data at rest.
   Images come from Amazon Elastic Container Registry (Amazon ECR).

The diagram's editable source is `assets/architecture.drawio`; open it in
[draw.io](https://www.drawio.com/).

## Observability

Tracing is part of the deployed stack, not an opt-in. The backend container's
`CMD` runs under `opentelemetry-instrument` (`backend/Dockerfile`), so the ADOT
distro auto-instruments FastAPI and boto3 — every Bedrock `Converse`, DynamoDB
read and S3 fetch becomes a span without a line of application code.

`infrastructure/modules/tracing` supplies the two account-level pieces that make
those spans queryable:

| Piece | Why it is needed |
|---|---|
| CloudWatch Logs resource policy for `xray.amazonaws.com` | Lets X-Ray write span records into `aws/spans` |
| `xray:UpdateTraceSegmentDestination = CloudWatchLogs` | Turns Transaction Search on; without it `aws/spans` stays empty and the Application Signals / GenAI views have no source |

Neither has a first-class Terraform resource for the second half, so the module
uses a `null_resource` provisioner. Both settings are **account- and
region-wide**: they affect every X-Ray-traced workload in the account. That is
called out in the module rather than hidden, and destroying the module
deliberately does *not* revert the account setting — flipping it back mid-teardown
would blind any other workload that has come to rely on `aws/spans`.

Span export requires X-Ray permissions on the **task role**
(`modules/ecs`: `aws_iam_policy.tracing`). Missing IAM does not crash the app —
only export fails — so `tests/infra/test_tracing_config.py` guards the whole
chain (wrapper present, IAM attached, Transaction Search wired), because each
link fails silently on its own.

`OTEL_PYTHON_LOG_CORRELATION` is set on the task, so log lines carry the active
trace and span ids and a Logs Insights query can pivot from an error straight to
the trace that produced it.

Three environment variables are load-bearing and each was found the hard way, by
deploying and watching tracing fail silently:

| Variable | Why it is required |
|---|---|
| `AGENT_OBSERVABILITY_ENABLED=true` | Without it the ADOT distro exports OTLP to `localhost:4317`, expecting a collector sidecar. There is none in this task, so the app stays healthy while every span is dropped. Setting it switches to the SigV4-signed X-Ray OTLP endpoint (`https://xray.<region>.amazonaws.com/v1/traces`) and enables GenAI message capture. |
| `OTEL_PYTHON_DISTRO=aws_distro` and `OTEL_PYTHON_CONFIGURATOR=aws_configurator` | Installing the distro registers a second OpenTelemetry distro/configurator pair, and the loader takes whichever resolves first. When the vanilla one wins, the AWS `_configure()` never runs, the variable above is ignored, and the only symptom is one INFO line: *"Configuration of aws_configurator not loaded, configurator already loaded"*. |
| `OTEL_LOGS_EXPORTER=none`, `OTEL_METRICS_EXPORTER=none` | Logs already reach CloudWatch via the `awslogs` driver and metrics via the alarms module. Leaving the distro's defaults for this mode adds two more export paths, each with its own permissions to get wrong. |

Span export needs `xray:PutSpans` (the OTLP ingestion action) in addition to the
classic `xray:PutTraceSegments`.

## Container hardening

Both container images run as a non-root user: the pptx sidecar as `converter`
(uid 1001) and the API as `appuser` (uid 1000). The API previously ran as uid 0,
which left the *more* exposed of the two services the *more* privileged — it
parses attacker-supplied PDF and PowerPoint files through native libraries
(PyMuPDF, python-pptx), which is precisely the code you do not want running as
root. `/app` is chowned so the app can read its code and `/tmp` stays writable
because rasterization and the PowerPoint path need scratch space.

The pptx sidecar runs with `readonlyRootFilesystem`. LibreOffice writes to
exactly two paths, per-request scratch directories in `/tmp` and its user
profile under `/home/converter`, and each is a task-scoped volume the
Dockerfile declares so it starts out owned by the non-root user. The API task
does not yet set the flag: the PDF and PowerPoint paths write scratch files in
several places, and each needs mapping to a volume before the root filesystem can
be made read-only.

Both images are built from the Amazon ECR Public mirror of the Docker official
Python images (`public.ecr.aws/docker/library/python`), not Docker Hub.

## Evaluation

`backend/evaluation/run_eval.py` is a release gate for answer quality. It sends
each item in `golden_set.jsonl` to the **deployed** `/api/queries` and grades the
response with a Bedrock judge on two axes: `correctness` (does it agree with the
reviewer-written expected answer on the facts that matter) and `groundedness`
(is it supported, or does it invent specifics — the hallucination axis). The mean
must clear `--threshold` or the process exits non-zero.

Two deliberate choices:

- It scores **through the API**, not by importing `create_query_agent`. Only the
  API path exercises auth, the route, the agent, its tools, the guardrail and the
  data plane together; a harness that imported the agent would pass while the
  deployed system was broken.
- A **refusal scores well** when the expected answer is itself a refusal.
  Declining to answer what a document does not state is correct behaviour for a
  compliance tool, and an evaluator that punished it would train the system
  toward confident guessing.

Items that fail to run are counted as failures rather than dropped, so a harness
cannot report "pass" because half its items errored.

**Which endpoint an item targets matters.** The two query routes answer
differently, and only one of them uses tools for a cross-document question:

| Item declares | Endpoint | Path taken |
|---|---|---|
| (default) | `POST /api/queries` | Document-scoped → tool-using agent. No document → a single Knowledge Base `retrieve_and_generate`: pure RAG, **no tools** |
| `"path": "stream"` | `POST /api/queries/stream` | **Always** the tool-using agent, document-scoped or not |

So an item asking about system state — "how many rules are configured?" — must
declare `path: "stream"`, because on the default route it would be answered by
RAG that cannot reach `get_all_standards`, and would fail for a reason no prompt
change could fix. This was found the hard way: the item failed twice, once
apparently as an agent hallucination and once as hedging, before the routing
turned out to be the cause.

Golden items reference documents by **filename**, not id. Document ids are
generated per upload, so a hard-coded id works only in the account it came from;
the harness resolves filename → id against `/api/documents` at run time.

## Compute layer

Two ECS services on AWS Fargate share one cluster
(`infrastructure/modules/ecs/main.tf`):

| Service | Purpose | Resources |
|---------|---------|-----------|
| FastAPI backend | API routes, validation fan-out, extraction agents, retrieval-augmented queries | `task_cpu` / `task_memory` (default 2 vCPU / 4 GB) |
| pptx-converter | LibreOffice headless, PowerPoint to PDF conversion | `pptx_task_cpu` / `pptx_task_memory` (default 1 vCPU / 2 GB) |

Only the FastAPI service is registered with the Application Load Balancer. The
converter is internal: it is reachable at
`pptx-converter.<project>-<env>.local:8001` through AWS Cloud Map, its security
group admits traffic only from the API's security group, and its task role holds
no permissions. The API passes presigned Amazon S3 URLs per request instead of
credentials (`infrastructure/modules/ecs/main.tf`).

There is no separate agent runtime: the Strands extraction agents, the query
agent, and the parallel validator all run in-process in the FastAPI worker, using
`asyncio` and FastAPI background tasks so that uploads and validations return
immediately. A managed agent runtime was evaluated and removed — it relieved none
of the real scaling pressure, since that lives in the validation fan-out and
extraction loops which stay on ECS regardless. The rationale is recorded in
[agentcore-decision.md](./agentcore-decision.md).

Auto scaling applies to the API service only (`min_capacity` to `max_capacity`,
with CPU and memory target-tracking policies). Because the service can scale to
several tasks, signals that cross requests are persisted to DynamoDB rather than
held in process memory. See [Cross-task state](#cross-task-state).

## Data plane

| Service | Role |
|---------|------|
| Amazon S3 | Uploaded PDF and PowerPoint files, rendered page and slide PNG images, validation findings, icon-legend images, frontend static assets |
| Amazon DynamoDB | Eight tables: documents, standards, validations, references, chat-jobs, user-roles, audit-events, icon-legend |
| Amazon Bedrock Knowledge Bases | Retrieval for natural-language question answering, backed by an Amazon OpenSearch Serverless vector index and Amazon Titan Text Embeddings V2 |

Validation findings are not stored in DynamoDB. The `validations` table holds
slim metadata — status, score, counts, cost telemetry, cache hashes, and layer
verdicts — while the findings array is written to
`validations/{validation_id}/findings.json` in Amazon S3 and read back on
request (`backend/app/services/validation_store.py`). This keeps a large run
within the DynamoDB 400 KB item limit.

Every DynamoDB table, both ECR repositories, the alarms SNS topic and the
Cognito client-secret parameter are encrypted with one customer-managed KMS key
(`infrastructure/modules/kms`). All eight tables have point-in-time recovery.

All DynamoDB table scans are paginated through
`backend/app/services/ddb.py::scan_all`. A `FilterExpression` is applied after
each 1 MB page, so an unpaginated scan can return no match for a row that exists
on a later page.

## AI layer

Model IDs are declared in one place, `backend/app/bedrock_models.py`, as three
tier constants:

| Tier | Used for |
|------|----------|
| `SONNET` | Chunked and multimodal validation, per-unit extraction, extraction synthesis, curator chat, query answering |
| `OPUS_REASONING` | Service tier classification and rule curation |
| `OPUS_VISION` | Diagram topology extraction and its verification pass, and icon interpretation |

Each call site takes its default from a tier constant through
`model_for(env_var, default)`, so an individual stage can be assigned a
different model with an environment variable and no code change. See
[infrastructure/EXTRACTION_TUNING.md](../infrastructure/EXTRACTION_TUNING.md).

`is_opus(model_id)` exists because the extended-thinking request shape differs
between model families: Opus takes `thinking: {"type": "adaptive"}` with no
budget, Sonnet requires an explicit budget, and the wrong shape returns a
`ValidationException`. Because a stage's model can be overridden by environment
variable, the request shape is selected from the resolved model ID rather than
the default.

The IDs are cross-Region inference profiles (the `us.` prefix), and the Amazon
Bedrock IAM policy is scoped to `inference-profile/us.anthropic.*`. Changing to
a `global.` profile requires widening that policy.

All model calls use the Amazon Bedrock Converse API. There is no model hosting
and no fine-tuning.

## API surface

Every `/api/*` router is mounted with `Depends(get_current_user)` in
`backend/app/main.py`. `/health` and `/api/health` are public so that the load
balancer can check container health; they return liveness only. Diagnostics that
name infrastructure resources are at `/api/health/detail`, which requires the
administrator role.

| Prefix | Covers |
|--------|--------|
| `/api/documents` | Upload (PDF, PowerPoint, image), list, fetch, page and slide images, service tier override, pipeline status |
| `/api/validations` | Create, cancel, read, report export (JSON, CSV, PDF), per-finding chat and its SSE stream |
| `/api/standards` | Rule library CRUD, AI curation, curator chat stream, extraction review batches |
| `/api/references` | Factual context blocks the validator attaches alongside rules |
| `/api/queries` | Natural-language question answering: `POST /api/queries` and the SSE `POST /api/queries/stream` |
| `/api/teams` | Microsoft Teams webhook notifications |
| `/api/roles` | User-to-role management; `/api/roles/me` for self-lookup |
| `/api/audit` | Read-only event history |
| `/api/icon-legend` | Custom icon legend, described below |

The OpenAPI schema is served at `/api/openapi.json` and the interactive docs at
`/api/docs`. Both are under the `/api` prefix so that CloudFront routes them to
the backend origin.

## Custom icon legend

On-premises architecture diagrams often use an organization's own glyph
vocabulary, which a general-purpose model interprets with generic priors. The
icon legend supplies those definitions to both extraction and validation.

The authoring flow derives the definition from the image rather than from a
caption. A curator uploads an icon crop, and a multimodal model examines the
pixels to produce the label, component type, a one-sentence description, and a
confidence value (`backend/app/services/icon_interpret.py`). A mislabeled upload
therefore cannot assert an incorrect mapping. The derived entry is stored with
`status="draft"`.

Draft entries do not affect any result. Only entries with `status="active"` are
included in prompts, and promotion to active requires
`POST /api/icon-legend/{id}/approve`, which records `approved_by` and
`approved_at` on the row (`backend/app/api/routes/icon_legend.py`). The store
fails closed on that field: an unrecognized status value becomes `draft`, never
`active` (`backend/app/services/icon_legend_store.py`). Reviewers can retrieve
the stored image to see what the model read before approving. Draft images are
restricted to curators and administrators.

Injection is shared by both pipelines through
`backend/app/services/icon_legend_inject.py`, so extraction and validation
cannot diverge onto different vocabularies. A divergence would let extraction
interpret an icon that validation cannot, which produces incorrect failures.

Each active entry contributes one line to a written glyph dictionary, which is
always applied. Entries that also have an uploaded crop contribute an Amazon
Bedrock image block as a visual example, capped at `ICON_LEGEND_MAX_IMAGES`
(default 6), because additional embedded images reduce the model's attention on
the diagram under analysis. Labels and descriptions are whitespace-collapsed and
quote-neutralized before entering the prompt so that a stored string cannot
introduce an additional legend line.

Bulk authoring accepts CSV or JSON text rows, or a batch of images interpreted
asynchronously. Entries are created as drafts under a shared batch ID for review
in one pass. Bulk job progress is stored as a row with a TTL in the same table
rather than in process memory, because the API can scale to several tasks and a
status request may reach a task that did not start the job.

A text-authored entry can be given a reference image afterwards, either per row
or for a whole batch: rows declare an optional `image_filename`, and a
multi-file upload pairs each file to the row that named it. Pairing is on the
declared filename rather than the label, because a curator edits labels during
review. Unmatched files and unmatched rows are both reported, since a silent
partial pairing would read as complete.

Attaching an image is the one authoring path where the definition is not derived
from the pixels — a curator writes the text and chooses the file — so nothing
otherwise verifies the two describe the same glyph, and the crop is fed to
diagram prompts as a visual example. Each attach therefore runs one multimodal
consistency check
(`backend/app/services/icon_interpret.py::cross_check_image`), and the verdict
is persisted on the row so a later reviewer sees that the pairing was
questioned. The check reports rather than blocks, because a curator may know
better than the model; the exception is a mismatch on an already-active entry,
which returns it to draft and clears the approval that was granted for the
image-less definition. The check fails open: an unavailable model records
`checked: false`, which the interface presents as unverified rather than as a
pass. The label and description reach that prompt through
`prompt_safety.untrusted_block`, because a CSV row is author-controlled text.

The user interface is at `standards/icon-legend`.

## Guardrails

Every model call carries an Amazon Bedrock guardrail
(`infrastructure/modules/bedrock-guardrail`). The policy is deliberately narrow:

| Policy | Setting |
|---|---|
| Sensitive information | Seven managed PII types plus three credential patterns, all set to `ANONYMIZE` |
| Prompt attack | Not enabled — see below |
| Content filters | Not enabled |
| Denied topics | Not configured |

Prompt-attack detection is excluded for a structural reason rather than as a
tuning preference. The filter classifies the user turn, on the assumption that
system instructions arrive separately and that anything instruction-shaped in the
user turn is an injection attempt. This application sends its own instructions as
the user turn: the validator's per-rule prompt, the topology extractor, and the
icon interpreter all contain directives such as "Return JSON ONLY" and "based
only on what is visually depicted". Measured against a live guardrail, all three
are blocked at `HIGH`, `MEDIUM`, and `LOW`, so there is no strength at which the
filter can be enabled without breaking validation, extraction, and icon
interpretation — each of which degrades to an empty or stub result rather than a
visible error.

Injection protection for the genuinely untrusted strings — rule text, reference
bodies, icon-legend descriptions — is applied in the application by
`backend/app/services/prompt_safety.py`, which fences them in a delimited block
so that a crafted body cannot forge prompt structure. That control targets the
actual threat surface, which is interpolated content rather than the prompt
itself.

The content-filter exclusions are a related decision. This application reads compliance
material that legitimately discusses firewalls, key management, encryption, and
how a control could be bypassed. Bedrock's managed content filters and
denied-topic policies intervene on that language, and an intervention during
validation surfaces as a missing verdict rather than as a recognizable policy
decision. Two tests in `tests/infra/test_terraform_config.py` assert those
filters remain absent, so enabling one is a deliberate change rather than an
accident.

Redaction is preferred to blocking for sensitive information. A design document
containing an operator's email address is ordinary, and refusing to validate it
would be a false positive; removing the address before the call is the correct
outcome. `NAME` and `ADDRESS` are excluded from the PII set because a design
document names its owning team and its datacenter sites, and redacting those
removes information a data-residency or site-pairing rule depends on.

The guardrail is applied by wrapping the Bedrock client rather than by passing an
argument at each call site. `backend/app/services/bedrock_guardrail.py` returns a
proxy that injects `guardrailConfig` on every `converse` and `converse_stream`
call, so a new call site inherits the guardrail by construction. A call that
omitted the configuration would be unguarded with no error to indicate it, which
is why this is structural rather than conventional. The Strands agent paths take
flat `guardrail_*` keywords instead, because Strands does not accept the Converse
shape.

Interventions are recorded on the finding trace and emitted as CloudWatch
metrics. Note that Bedrock reports `stopReason=guardrail_intervened` for a
redaction as well as for a block, so the two are distinguished by the action on
the assessment: `ANONYMIZED` completed with rewritten content, `BLOCKED` produced
no usable response. `GuardrailInterventions` is therefore threshold-alarmed,
while `GuardrailBlocked` alarms on any occurrence.

The published guardrail version embeds a hash of the policy in its description,
so editing the policy forces a new version. Without that dependency Terraform
updates `DRAFT` in place and leaves the numbered version untouched, and the
application — which pins a number, never `DRAFT` — continues running the previous
policy. A guardrail edit would appear to apply while changing nothing.

Set `enable_bedrock_guardrail = false` to omit the guardrail. The application
treats an empty `BEDROCK_GUARDRAIL_ID` as "not configured" and omits the
configuration rather than failing, so local development runs the same code path.

## Auditing

Two independent records exist, and the distinction matters:

| Record | Scope |
|---|---|
| `audit-events` table | Who performed which action inside the application, readable through `/api/audit` |
| AWS CloudTrail | Who called which AWS API, including Amazon Bedrock model invocations |

The application's own table is written by the component under review, so it
cannot serve as the only record. CloudTrail
(`infrastructure/modules/cloudtrail`) delivers to a dedicated bucket with log
file validation enabled, which is what detects modification of delivered log
files after the fact, and mirrors to a CloudWatch log group so events are
queryable with Logs Insights. Management events are recorded, plus data events
for `AWS::Bedrock::Model`, because management events alone do not record model
invocations.

## Authentication

Amazon Cognito issues JSON Web Tokens, and every `/api/*` route validates the
bearer token against the configured user pool. The frontend uses the Cognito
hosted UI. A SAML identity provider and a pre-token-generation Lambda function
are provisioned so that roles can be driven from an external identity provider's
group claim. See [RBAC.md](./RBAC.md).

## Networking

- **CloudFront** — Two origins: Amazon S3 for the static frontend and the
  Application Load Balancer for `/api/*`. The API behavior is uncached (all TTLs
  are 0) and forwards `Authorization`, `Content-Type`, `Origin`, and `Accept`.
  `origin_read_timeout` is 60 seconds, the default account ceiling; a large
  PowerPoint upload can exceed it and requires a Service Quotas increase
  (`infrastructure/modules/cloudfront/main.tf`).
- **Application Load Balancer** — `idle_timeout` is 180 seconds, sized for the
  PowerPoint upload path (LibreOffice conversion, slide rasterization, and the
  Knowledge Base sync trigger). `drop_invalid_header_fields` is enabled.
- **Amazon VPC** — Two Availability Zones, with public subnets for the load
  balancer and NAT gateway, and private subnets for both ECS services. Public
  subnets do not assign public IPs automatically.
- **Access logs** — CloudFront writes every viewer request to a dedicated S3
  bucket, retained for one year.
- **NAT gateway** — Outbound path for tasks reaching Amazon Bedrock, Amazon S3,
  and DynamoDB.

The load balancer is internet-facing, so two controls keep it behind
CloudFront. Its security group admits only CloudFront's managed origin-facing
prefix list, and its listener requires the `X-Origin-Verify` header that
CloudFront adds to every origin request. The listener's default action returns
HTTP 403; only the rule matching that header forwards to the target group. Without this control, the API is reachable at the
load balancer's DNS name, which bypasses CloudFront. The header value is
generated at apply time and is never committed.

No `custom_error_response` is configured on the distribution. That setting is
distribution-wide and would intercept API 404 responses from the load balancer
origin. A CloudFront viewer-request function handles static-export path rewrites
instead.

## Cross-task state

The API service scales to several tasks, so any signal that must be visible to a
request other than the one that created it is persisted rather than held in
memory:

| Signal | Storage | Purpose |
|---|---|---|
| Validation cancellation | `cancel_requested` on the validation row, with a 3-second read cache | A cancel request often reaches a different task than the one running the job |
| Extraction in flight | `extraction_state` and `extraction_state_at` on the document row, with a 45-minute staleness window | The review gate needs to know whether deduplication and cross-reference wiring have finished |
| Bulk icon job progress | Row with a TTL in the icon-legend table | A status request may reach a task that did not start the job |

The staleness window on extraction state exists so that a task terminated
mid-run by a deployment cannot leave rules blocked indefinitely.

## Deployment

`deploy.sh` builds the two container images, pushes them to Amazon ECR, applies
Terraform, and synchronizes the frontend to Amazon S3. Remote state is stored in
an S3 bucket in the target account. All resource names derive from
`project_name` and `environment`.

See [deployment.md](./deployment.md) for commands and configuration.
