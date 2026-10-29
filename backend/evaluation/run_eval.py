# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Golden-set evaluation gate for the deployed query agent.

Drives ``golden_set.jsonl`` through the **deployed application's own API** and
scores each answer with an LLM judge on Amazon Bedrock, then exits non-zero if
the mean score falls below a threshold — so it works as a release gate in CI.

Why it scores through the API rather than importing the agent: that is the only
path that exercises what users actually hit — auth, the route, the agent, its
tools, the guardrail, and the data plane. A harness that imported
``create_query_agent`` directly would pass while the deployed system was broken.

Judging: each answer is graded against the golden ``expected`` text on two axes
the compliance use case cares about, by a Bedrock model called with a strict
JSON contract:

  * ``correctness``  — does the answer agree with the expected answer on the
    facts that matter? (Wording may differ; substance may not.)
  * ``consistency``  — does the answer CONTRADICT the expected answer, or itself?
    Deliberately not "is every detail present in the expected answer": the
    expected answers are one-line summaries while the source documents contain
    far more (availability targets, RTO/RPO, requirement ids, page refs). An
    earlier version scored that axis against the summary and marked a correct,
    richly-detailed answer as a fabrication — a gate that would have pushed the
    system toward vague replies. Only conflict is penalised.

A refusal ("the document does not state that") scores WELL when the expected
answer is itself a refusal — declining to answer is correct behaviour for a
compliance tool, and an evaluator that punished it would train the system toward
confident guessing.

Usage:
    python backend/evaluation/run_eval.py \
        --base-url https://<cloudfront-domain> \
        --user-pool-client-id <cognito app client id> \
        --username <user> --password <pass> \
        [--golden backend/evaluation/golden_set.jsonl] \
        [--threshold 0.6] [--region us-east-1] [--json-report report.json]

Exit codes: 0 = passed threshold, 1 = below threshold, 2 = harness/setup error.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Judge model. A mid-tier model is deliberate: judging is a short, constrained
# comparison, and using the same top-tier model that produced the answer invites
# self-preference bias.
DEFAULT_JUDGE_MODEL = "us.anthropic.claude-sonnet-4-6"

JUDGE_PROMPT = """You are grading one answer from a compliance-review assistant \
that answers questions about infrastructure design documents.

QUESTION:
{question}

EXPECTED ANSWER (a reviewer-written SUMMARY of the key facts — NOT an exhaustive
list of everything the source document contains):
{expected}

ACTUAL ANSWER (from the system under test):
{actual}

Grade on two axes, each 0.0 to 1.0:

correctness  - Does the actual answer agree with the expected answer on the
               facts that matter? Different wording, ordering or extra relevant
               detail is fine. Contradicting a fact, or omitting the central
               one, is not.
consistency  - Does the actual answer avoid CONTRADICTING the expected answer,
               and stay internally coherent? Score low only for statements that
               conflict with the expected answer, contradict each other, or are
               of a kind the source could not contain at all (e.g. a named
               person's medical history in an architecture document).

CRITICAL — do NOT penalise additional detail:
The expected answer is a short summary. The source document is long and
genuinely contains far more specifics — exact availability targets, RTO/RPO
values, requirement identifiers, page references, tier tables. An actual answer
that supplies such specifics is doing its job well. Judging "anything not in the
expected summary is unsupported" would score a thorough, accurate answer as a
fabrication and push the system toward uselessly vague replies. Penalise only
CONFLICT, never elaboration.

Scoring rules:
- If the EXPECTED answer is itself a refusal or a statement that the
  information is absent, then an actual answer that also declines is CORRECT
  (1.0). Declining when the data is not there is the desired behaviour.
- Judge only the two axes. Do not reward length, tone or formatting.

Return ONLY a JSON object, no prose, no code fences:
{{"correctness": <float>, "consistency": <float>, "reason": "<one sentence>"}}"""


@dataclass
class ItemResult:
    item_id: str
    question: str
    passed: bool
    correctness: float = 0.0
    consistency: float = 0.0
    reason: str = ""
    answer: str = ""
    error: str | None = None

    @property
    def mean(self) -> float:
        return (self.correctness + self.consistency) / 2


@dataclass
class Report:
    results: list[ItemResult] = field(default_factory=list)

    @property
    def scored(self) -> list[ItemResult]:
        return [r for r in self.results if r.error is None]

    @property
    def mean_score(self) -> float:
        scored = self.scored
        return statistics.fmean(r.mean for r in scored) if scored else 0.0


def _fatal(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def _post_json(url: str, body: dict, token: str | None, timeout: int) -> dict:
    """POST JSON and return the decoded response.

    Raises RuntimeError with the server's message on a non-2xx, because the
    body carries the useful detail (e.g. an RBAC refusal) that the status alone
    does not.
    """
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - operator-supplied https URL
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection failed: {exc.reason}") from exc


def _post_sse(url: str, body: dict, token: str, timeout: int) -> str:
    """POST to the SSE query endpoint and return the concatenated answer.

    Needed because the two query endpoints route differently, and only one of
    them exercises the agent for a cross-document question:

      * ``POST /api/queries`` with no document_id answers via a single Bedrock
        Knowledge Base retrieve_and_generate — pure RAG, NO TOOLS. A question
        about system state ("how many rules are configured?") is unanswerable
        there because get_all_standards is not reachable.
      * ``POST /api/queries/stream`` always runs the tool-using agent.

    So a golden item that needs a tool must target the stream endpoint, or it
    measures the wrong thing and fails for a reason the agent cannot fix.
    """
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    parts: list[str] = []
    event: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - operator-supplied https URL
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if event == "delta" and isinstance(obj.get("text"), str):
                        parts.append(obj["text"])
                    elif event == "error":
                        raise RuntimeError(f"stream error: {obj.get('detail')}")
                    elif event == "done":
                        break
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection failed: {exc.reason}") from exc
    return "".join(parts).strip()


def _get_json(url: str, token: str, timeout: int) -> dict:
    """GET JSON from the app (used to resolve document ids by filename)."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - operator-supplied https URL
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _resolve_document_ids(base_url: str, token: str, timeout: int) -> dict[str, str]:
    """Map filename -> document_id for the target environment.

    Golden items reference documents by FILENAME, not id: ids are generated per
    upload, so a hard-coded id only ever works in the one account it came from —
    which silently breaks the gate everywhere else. Resolving by filename keeps
    the golden set portable across environments.
    """
    listing = _get_json(base_url.rstrip("/") + "/api/documents", token, timeout)
    return {
        d["filename"]: d["document_id"]
        for d in listing.get("documents", [])
        if d.get("filename") and d.get("document_id")
    }


def _get_id_token(region: str, client_id: str, username: str, password: str) -> str:
    """Exchange Cognito credentials for an ID token (the app validates ID tokens)."""
    import boto3

    client = boto3.client("cognito-idp", region_name=region)
    try:
        resp = client.initiate_auth(
            ClientId=client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as a setup error
        _fatal(f"Cognito auth failed: {exc}")
    token = resp.get("AuthenticationResult", {}).get("IdToken")
    if not token:
        _fatal(f"Cognito returned no IdToken (challenge: {resp.get('ChallengeName')})")
    return token


def _judge(client, model_id: str, question: str, expected: str, actual: str) -> tuple[float, float, str]:
    """Score one answer. Returns (correctness, consistency, reason)."""
    prompt = JUDGE_PROMPT.format(question=question, expected=expected, actual=actual)
    resp = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        # temperature 0: grading must be as reproducible as a model allows, or a
        # re-run of the same commit changes the gate's verdict.
        inferenceConfig={"maxTokens": 500, "temperature": 0.0},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    # Tolerate a fenced or prose-wrapped object rather than failing the item:
    # a judge formatting slip is not a fault of the system under test.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"judge returned no JSON object: {text[:200]}")
    verdict = json.loads(text[start : end + 1])
    return (
        float(verdict.get("correctness", 0.0)),
        float(verdict.get("consistency", 0.0)),
        str(verdict.get("reason", "")).strip(),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="Deployed app origin, e.g. https://xxx.cloudfront.net")
    ap.add_argument("--user-pool-client-id", required=True, help="Cognito web app client id")
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--golden", default=str(Path(__file__).with_name("golden_set.jsonl")))
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    ap.add_argument("--threshold", type=float, default=0.6, help="Minimum mean score to pass")
    ap.add_argument("--timeout", type=int, default=120, help="Per-query timeout, seconds")
    ap.add_argument("--json-report", help="Write the full report as JSON to this path")
    args = ap.parse_args()

    golden_path = Path(args.golden)
    if not golden_path.is_file():
        _fatal(f"golden set not found: {golden_path}")
    items = [json.loads(line) for line in golden_path.read_text().splitlines() if line.strip()]
    if not items:
        _fatal(f"golden set is empty: {golden_path}")

    import boto3

    bedrock = boto3.client("bedrock-runtime", region_name=args.region)
    token = _get_id_token(args.region, args.user_pool_client_id, args.username, args.password)
    query_url = args.base_url.rstrip("/") + "/api/queries"
    stream_url = query_url + "/stream"

    # Only needed when an item scopes itself to a document.
    by_filename: dict[str, str] = {}
    if any(i.get("document_filename") for i in items):
        try:
            by_filename = _resolve_document_ids(args.base_url, token, args.timeout)
        except Exception as exc:  # noqa: BLE001
            _fatal(f"could not list documents to resolve golden-set filenames: {exc}")

    print(f"Evaluating {len(items)} golden item(s) against {args.base_url}")
    print(f"Judge: {args.judge_model} | threshold: {args.threshold}\n")

    report = Report()
    for item in items:
        item_id = item.get("id", "<unnamed>")
        question = item["question"]
        body: dict[str, Any] = {"question": question}
        # Prefer a filename (portable); fall back to an explicit id if given.
        wanted = item.get("document_filename")
        if wanted:
            resolved = by_filename.get(wanted)
            if not resolved:
                available = ", ".join(sorted(by_filename)[:5]) or "none"
                report.results.append(
                    ItemResult(
                        item_id=item_id,
                        question=question,
                        passed=False,
                        error=f"document {wanted!r} not uploaded in this environment (present: {available})",
                    )
                )
                print(f"  [{item_id}] SKIP: {wanted!r} not present in this environment")
                continue
            body["document_id"] = resolved
        elif item.get("document_id"):
            body["document_id"] = item["document_id"]

        try:
            # Items that need a tool (system state, arithmetic) declare
            # path="stream", because only the streaming endpoint always runs the
            # tool-using agent. See _post_sse.
            if item.get("path") == "stream":
                answer = _post_sse(stream_url, body, token, args.timeout)
                if not answer:
                    raise RuntimeError("empty answer from /api/queries/stream")
            else:
                resp = _post_json(query_url, body, token, args.timeout)
                answer = (resp.get("answer") or "").strip()
                if not answer:
                    raise RuntimeError("empty answer from /api/queries")
            correctness, consistency, reason = _judge(
                bedrock, args.judge_model, question, item.get("expected", ""), answer
            )
            result = ItemResult(
                item_id=item_id,
                question=question,
                passed=True,
                correctness=correctness,
                consistency=consistency,
                reason=reason,
                answer=answer,
            )
            print(f"  [{item_id}] correctness={correctness:.2f} consistency={consistency:.2f} — {reason}")
        except Exception as exc:  # noqa: BLE001 — one bad item must not abort the run
            result = ItemResult(item_id=item_id, question=question, passed=False, error=str(exc))
            print(f"  [{item_id}] ERROR: {exc}")
        report.results.append(result)

    errored = [r for r in report.results if r.error]
    print(
        f"\nScored {len(report.scored)}/{len(report.results)} item(s); "
        f"mean score {report.mean_score:.3f} (threshold {args.threshold})"
    )
    if errored:
        # Errors are reported and counted as a failure rather than silently
        # dropped: a harness that "passes" because half its items failed to run
        # is worse than one that fails loudly.
        print(f"{len(errored)} item(s) failed to run: {', '.join(r.item_id for r in errored)}")

    if args.json_report:
        Path(args.json_report).write_text(
            json.dumps(
                {
                    "mean_score": report.mean_score,
                    "threshold": args.threshold,
                    "results": [vars(r) for r in report.results],
                },
                indent=2,
            )
        )
        print(f"Report written to {args.json_report}")

    if errored or report.mean_score < args.threshold:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
