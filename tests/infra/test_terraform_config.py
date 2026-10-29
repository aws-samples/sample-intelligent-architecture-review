# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Terraform configuration invariants.

These parse the .tf sources as text — no `terraform` binary, no AWS calls, no
state — so they run in CI in milliseconds. They exist to catch the mistakes that
a plan/apply would happily accept but that break the app at runtime:

  - a DynamoDB table the backend reads but Terraform never creates,
  - a table created but never wired into the ECS task environment,
  - a durable table with point-in-time recovery accidentally left off,
  - the icon-legend table losing its TTL (job rows would accumulate and, being
    full-scanned on the prompt hot path, eventually truncate the legend).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.infra

INFRA = Path(__file__).resolve().parents[2] / "infrastructure"
DDB_MAIN = INFRA / "modules" / "dynamodb" / "main.tf"
DDB_OUTPUTS = INFRA / "modules" / "dynamodb" / "outputs.tf"
ROOT_MAIN = INFRA / "main.tf"


def _read(path: Path) -> str:
    assert path.exists(), f"expected terraform file missing: {path}"
    return path.read_text()


def _table_blocks(src: str) -> dict[str, str]:
    """Map DynamoDB resource name -> its HCL block body.

    Comment lines are stripped first: otherwise a commented-out table registers
    as existing, and a commented-out `point_in_time_recovery` satisfies the
    durability check below.
    """
    src = _strip_comments(src)
    blocks: dict[str, str] = {}
    for match in re.finditer(
        r'resource\s+"aws_dynamodb_table"\s+"(\w+)"\s*\{', src
    ):
        name = match.group(1)
        # Walk braces from the opening one to find the block's extent.
        depth, i = 0, match.end() - 1
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blocks[name] = src[match.end():i]
    return blocks


# --- tables exist and are wired ---------------------------------------------

def test_every_table_is_exported():
    """Each table needs a *_table_name output, otherwise the root module can't
    pass it into the ECS task environment."""
    tables = _table_blocks(_read(DDB_MAIN))
    outputs = _read(DDB_OUTPUTS)
    assert tables, "no DynamoDB tables parsed — did the module move?"
    missing = [
        name for name in tables
        if f'output "{name}_table_name"' not in outputs
    ]
    assert not missing, f"tables with no _table_name output: {missing}"


def test_every_table_is_addressable_by_the_backend():
    """A table nothing can address is dead weight. The backend resolves a table
    name one of two ways, and every table must use one of them:

      1. derived from DYNAMODB_TABLE_PREFIX in app/config.py (the original
         tables: "<prefix>-documents" etc.), or
      2. an explicit <NAME>_TABLE env var wired in the root module.
    """
    tables = _table_blocks(_read(DDB_MAIN))
    root = _read(ROOT_MAIN)
    config_src = (
        Path(__file__).resolve().parents[2] / "backend" / "app" / "config.py"
    ).read_text()

    unreachable = []
    for name in tables:
        suffix = name.replace("_", "-")          # user_roles -> user-roles
        wired_explicitly = f"module.dynamodb.{name}_table_name" in root
        derived_from_prefix = f'-{suffix}"' in config_src
        if not (wired_explicitly or derived_from_prefix):
            unreachable.append(name)

    assert not unreachable, (
        f"tables created but unreachable from the app: {unreachable}. "
        "Either derive the name in app/config.py, pass it via "
        "container_environment, or delete the table."
    )


def test_table_names_derive_from_project_and_environment():
    """Names must be interpolated, never literal, so two environments in one
    account can't collide."""
    for name, body in _table_blocks(_read(DDB_MAIN)).items():
        name_line = re.search(r'name\s*=\s*"([^"]+)"', body)
        assert name_line, f"{name}: no name attribute"
        value = name_line.group(1)
        assert "${var.project_name}" in value and "${var.environment}" in value, (
            f"{name}: table name {value!r} is not scoped by project+environment"
        )


# --- durability / lifecycle -------------------------------------------------

# Tables holding data a user would be upset to lose.
DURABLE_TABLES = {
    "documents", "validations", "standards", "references",
    "user_roles", "audit_events", "icon_legend",
}


def test_durable_tables_have_pitr():
    blocks = _table_blocks(_read(DDB_MAIN))
    for name in sorted(DURABLE_TABLES):
        assert name in blocks, f"expected durable table {name} to exist"
        assert re.search(
            r"point_in_time_recovery\s*\{[^}]*enabled\s*=\s*true",
            blocks[name], re.S,
        ), (
            f"{name} holds durable data but point_in_time_recovery is absent "
            "or not enabled"
        )


def test_icon_legend_table_has_ttl():
    """Bulk-interpret job rows share this table and self-expire via `ttl`.
    Without TTL they accumulate; the table is scanned in full when building
    diagram prompts, so growth eventually costs approved icons their injection."""
    blocks = _table_blocks(_read(DDB_MAIN))
    assert re.search(r"ttl\s*\{[^}]*enabled\s*=\s*true", blocks["icon_legend"], re.S), (
        "icon_legend must keep its ttl block ENABLED so transient job rows are reaped"
    )


def test_chat_jobs_table_has_ttl():
    """Chat jobs are ephemeral request/response state and must not grow forever."""
    blocks = _table_blocks(_read(DDB_MAIN))
    assert re.search(r"ttl\s*\{[^}]*enabled\s*=\s*true", blocks["chat_jobs"], re.S), (
        "chat_jobs must keep TTL enabled so ephemeral job rows don't accumulate"
    )


# --- no environment-specific literals --------------------------------------

def test_no_hardcoded_account_id_in_terraform():
    """An account id baked into config makes the stack non-portable."""
    offenders = []
    for tf in INFRA.rglob("*.tf"):
        if ".terraform" in tf.parts:
            continue
        for lineno, line in enumerate(tf.read_text().splitlines(), 1):
            if line.lstrip().startswith(("#", "//")):
                continue
            # A 12-digit run that isn't part of a longer number (e.g. a timeout
            # in microseconds) and isn't a reference to the account_id variable.
            if re.search(r"(?<![\d.])\d{12}(?![\d.])", line) \
                    and "aws_caller_identity" not in line \
                    and "var.aws_account_id" not in line:
                offenders.append(f"{tf.relative_to(INFRA)}:{lineno}: {line.strip()}")
    assert not offenders, "hardcoded AWS account id(s):\n" + "\n".join(offenders)


# --- Cognito posture --------------------------------------------------------
#
# A review found a remote privilege-escalation chain: self-signup was open and
# the web client could write its own custom:saml_groups, which the pre-token
# Lambda turns into the cognito:groups claim that RBAC trusts first. These pin
# the Terraform side shut.

COGNITO_MAIN = INFRA / "modules" / "cognito" / "main.tf"


def _strip_comments(src: str) -> str:
    """Drop comment lines so a commented-out block can't satisfy a check."""
    return "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith(("#", "//"))
    )


def test_self_signup_is_closed():
    """Users are provisioned by an admin or federated from the IdP. Open signup
    plus role-resolution-from-claims is a path to admin for anyone."""
    src = _strip_comments(_read(COGNITO_MAIN))
    assert "admin_create_user_config" in src, (
        "no admin_create_user_config block — Cognito self-signup defaults to OPEN"
    )
    assert re.search(r"allow_admin_create_user_only\s*=\s*true", src), (
        "allow_admin_create_user_only must be true"
    )


def test_saml_client_can_write_the_group_attribute():
    """The client that federates MUST be able to write custom:saml_groups.

    This replaces an earlier assertion that the attribute must NOT be writable.
    That reading looked like hardening but was self-defeating: Cognito populates
    an IdP-mapped attribute by writing it *as the app client*, and
    CreateUserPoolClient specifies that a client lacking write access to a
    mapped attribute makes Cognito error when it tries to update it. With the
    attribute omitted, no federated user ever received a groups value, the
    pre-token Lambda emitted no cognito:groups, and every federated user
    resolved to `viewer` — RBAC via federation could not work at all.

    Self-assignment is blocked by controls that actually hold, pinned in
    test_group_claim_is_only_trusted_on_a_federated_token below and in
    tests/backend/test_rbac.py: the groups claim is ignored unless the token
    carries an `identities` claim, the client has no
    aws.cognito.signin.user.admin scope with which to call UpdateUserAttributes,
    and Cognito re-writes mapped attributes from the assertion on every sign-in.
    """
    src = _strip_comments(_read(COGNITO_MAIN))

    web_client = re.search(
        r'resource\s+"aws_cognito_user_pool_client"\s+"web_app"\s*\{(.+?)\n\}',
        src,
        re.S,
    )
    assert web_client, "web_app user pool client not found"
    match = re.search(r"write_attributes\s*=\s*\[([^\]]*)\]", web_client.group(1))
    assert match, "web_app client declares no write_attributes"
    assert "custom:saml_groups" in match.group(1), (
        "custom:saml_groups missing from the web client's write_attributes — "
        "Cognito cannot then persist the IdP's group assertion, so every "
        "federated user silently resolves to viewer"
    )


def test_group_claim_is_only_trusted_on_a_federated_token():
    """The real guard against self-assigned roles.

    Because custom:saml_groups is now writable (it must be, above), the control
    that prevents a non-federated user from choosing their own role lives in the
    resolver: cognito:groups is honoured only when the token also carries an
    `identities` claim, which Cognito sets exclusively for users that signed in
    through an external IdP.
    """
    rbac = _read(INFRA.parent / "backend" / "app" / "api" / "rbac.py")
    assert 'claims.get("identities")' in rbac, (
        "rbac._role_from_groups no longer gates on the `identities` claim — a "
        "password user who can write custom:saml_groups could self-assign a role"
    )


def test_client_lacks_the_scope_to_self_update_attributes():
    """UpdateUserAttributes requires the aws.cognito.signin.user.admin scope.
    Keeping it off the browser client means a federated user's access token
    cannot rewrite their own custom:saml_groups."""
    src = _strip_comments(_read(COGNITO_MAIN))
    for match in re.finditer(r"allowed_oauth_scopes\s*=\s*\[([^\]]*)\]", src):
        assert "signin.user.admin" not in match.group(1), (
            "aws.cognito.signin.user.admin is allowed on a browser client, which "
            f"lets a user call UpdateUserAttributes on themselves: {match.group(0)}"
        )


def test_email_change_requires_verification():
    """A role can be resolved from the email claim (the bootstrap-admin list), so
    a user must not be able to silently repoint their email at that address."""
    src = _strip_comments(_read(COGNITO_MAIN))
    assert "user_attribute_update_settings" in src
    assert re.search(
        r"attributes_require_verification_before_update\s*=\s*\[[^\]]*email",
        src,
    ), "email changes must require verification before taking effect"


# --- ALB exposure -----------------------------------------------------------
#
# The ALB is internet-facing (it has to be, to serve as a CloudFront origin), so
# the only thing stopping a direct hit that bypasses CloudFront is the
# origin-verify header check on the listener. These pin that shut.

ECS_MAIN = INFRA / "modules" / "ecs" / "main.tf"
CLOUDFRONT_MAIN = INFRA / "modules" / "cloudfront" / "main.tf"


def test_alb_listener_rejects_unverified_traffic_by_default():
    """With a secret set, the listener's DEFAULT action must be a rejection —
    forwarding by default would leave the bypass open."""
    src = _strip_comments(_read(ECS_MAIN))
    assert "fixed-response" in src, (
        "no fixed-response default action — the ALB forwards unverified traffic"
    )
    assert 'status_code  = "403"' in src or 'status_code = "403"' in src


def test_only_the_verify_header_rule_forwards_to_the_app():
    """A listener rule conditioned on X-Origin-Verify is what lets real traffic
    through; without it nothing would reach the app."""
    src = _strip_comments(_read(ECS_MAIN))
    assert 'resource "aws_lb_listener_rule" "verified_origin_http"' in src
    assert 'http_header_name = "X-Origin-Verify"' in src


def test_cloudfront_sends_the_verify_header():
    """The other half of the pair: if CloudFront stops sending it, every request
    404s/403s — so this must not drift from the listener rule."""
    src = _strip_comments(_read(CLOUDFRONT_MAIN))
    assert "custom_header" in src
    assert '"X-Origin-Verify"' in src


def test_the_secret_is_generated_not_defaulted():
    """A hardcoded or empty-by-default secret would make the check theatre."""
    root = _strip_comments(_read(ROOT_MAIN))
    assert 'resource "random_password" "origin_verify"' in root
    assert "random_password.origin_verify.result" in root


def test_origin_talks_https_when_the_alb_has_a_certificate():
    """Plain HTTP to the origin puts the Cognito ID token on the public AWS
    network in clear text, so the protocol must follow the certificate."""
    src = _strip_comments(_read(CLOUDFRONT_MAIN))
    assert "var.alb_has_certificate ? \"https-only\"" in src


# ---------------------------------------------------------------------------
# Amazon Bedrock guardrail
#
# The guardrail is only protection if it is attached and if its policy stays
# narrow. Both are configuration, so both are asserted here rather than left to
# review: enabling the managed content filters would make the tool intervene on
# the compliance language it exists to read, and referencing DRAFT would let the
# policy change under a deployed environment.
# ---------------------------------------------------------------------------

GUARDRAIL_MAIN = INFRA / "modules" / "bedrock-guardrail" / "main.tf"
GUARDRAIL_OUTPUTS = INFRA / "modules" / "bedrock-guardrail" / "outputs.tf"


def test_guardrail_redacts_sensitive_information():
    src = _read(GUARDRAIL_MAIN)
    assert "sensitive_information_policy_config" in src
    assert 'action = "ANONYMIZE"' in src.replace("  ", " ") or "ANONYMIZE" in src


def test_guardrail_does_not_enable_prompt_attack_detection():
    """PROMPT_ATTACK classifies the USER turn on the assumption that anything
    instruction-shaped there is an injection. This application sends its own
    instructions AS the user turn, so measured against a live guardrail the
    icon-interpretation prompt, the topology-extraction prompt, and the
    validator's chunk template are all blocked at HIGH, MEDIUM *and* LOW. There
    is no strength at which it can be enabled without breaking validation,
    extraction, and icon interpretation.

    Injection protection for the genuinely untrusted strings lives in
    app/services/prompt_safety.py instead."""
    config = "\n".join(
        line for line in _read(GUARDRAIL_MAIN).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "PROMPT_ATTACK" not in config, (
        "PROMPT_ATTACK blocks this application's own prompts at every strength; "
        "re-enabling it silently breaks validation, extraction, and icon "
        "interpretation"
    )
    assert "content_policy_config" not in config


def test_guardrail_does_not_enable_content_filters_that_break_compliance_reading():
    """VIOLENCE, MISCONDUCT and similar fire on legitimate design-document
    language such as how a control could be bypassed. An intervention there
    surfaces as a missing verdict, not as a recognizable policy decision."""
    # Strip comments first: the module explains WHY these filters are absent, so
    # a plain substring search matches its own rationale.
    config = "\n".join(
        line for line in _read(GUARDRAIL_MAIN).splitlines()
        if not line.lstrip().startswith("#")
    )
    for filter_type in ("VIOLENCE", "HATE", "SEXUAL", "MISCONDUCT", "INSULTS"):
        assert filter_type not in config, (
            f"{filter_type} content filter is enabled; it will intervene on "
            f"compliance material this application must be able to read"
        )


def test_guardrail_declares_no_denied_topics():
    config = "\n".join(
        line for line in _read(GUARDRAIL_MAIN).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "topic_policy_config" not in config, (
        "a denied-topic policy will block the security subject matter this "
        "application reviews"
    )


def test_guardrail_publishes_a_numbered_version():
    """DRAFT is mutable, so invoking against it changes behavior whenever the
    policy is edited."""
    assert "aws_bedrock_guardrail_version" in _read(GUARDRAIL_MAIN)
    assert "guardrail_version" in _read(GUARDRAIL_OUTPUTS)


def test_guardrail_grants_apply_permission_to_the_task_role():
    src = _read(GUARDRAIL_MAIN)
    assert "bedrock:ApplyGuardrail" in src
    assert "aws_iam_role_policy_attachment" in src


def test_guardrail_reaches_the_backend_environment():
    """The backend reads these two variables; without them every Converse call
    is unguarded and nothing reports it."""
    src = _read(ROOT_MAIN)
    assert "BEDROCK_GUARDRAIL_ID" in src
    assert "BEDROCK_GUARDRAIL_VERSION" in src


def test_guardrail_is_enabled_by_default():
    src = _read(INFRA / "variables.tf")
    block = src[src.index('variable "enable_bedrock_guardrail"'):]
    block = block[: block.index("\n}")]
    assert "default     = true" in block or "default = true" in block


# ---------------------------------------------------------------------------
# CloudTrail
# ---------------------------------------------------------------------------

TRAIL_MAIN = INFRA / "modules" / "cloudtrail" / "main.tf"


def test_cloudtrail_validates_log_files():
    """Without validation the trail records history but cannot show the history
    was not altered afterwards."""
    assert "enable_log_file_validation = true" in _read(TRAIL_MAIN)


def test_cloudtrail_records_bedrock_data_events():
    """Management events do not record model invocations, which are both the
    cost driver and the sensitive operation here."""
    src = _read(TRAIL_MAIN)
    assert "AWS::Bedrock::Model" in src
    assert '"Data"' in src


def test_cloudtrail_bucket_is_private_and_encrypted():
    src = _read(TRAIL_MAIN)
    assert "aws_s3_bucket_public_access_block" in src
    assert "aws_s3_bucket_server_side_encryption_configuration" in src
    assert "aws_s3_bucket_versioning" in src


def test_cloudtrail_bucket_is_not_force_destroyed():
    """The trail is evidence; `terraform destroy` must not discard it silently."""
    assert "force_destroy = false" in _read(TRAIL_MAIN)


def test_cloudtrail_bucket_policy_is_scoped_to_this_trail():
    """Without the aws:SourceArn conditions another account's trail could be
    pointed at this bucket."""
    src = _read(TRAIL_MAIN)
    assert "aws:SourceArn" in src
    assert src.count("aws:SourceArn") >= 2


# ---------------------------------------------------------------------------
# Bedrock and guardrail alarms
# ---------------------------------------------------------------------------

ALARMS = INFRA / "modules" / "monitoring" / "alarms.tf"


def test_bedrock_throttling_is_alarmed():
    """The validator retries throttling internally and downgrades a PASS that
    rests on too few chunks, so sustained throttling degrades verdict quality
    before it ever surfaces as an error."""
    src = _read(ALARMS)
    assert "InvocationThrottles" in src
    assert 'namespace           = "AWS/Bedrock"' in src


def test_guardrail_interventions_are_alarmed():
    src = _read(ALARMS)
    assert "GuardrailInterventions" in src
    assert "GuardrailBlocked" in src


def test_blocked_calls_alarm_on_any_occurrence():
    """A redaction is expected; a block produces no verdict at all."""
    src = _read(ALARMS)
    block = src[src.index('"guardrail_blocked"'):]
    block = block[: block.index("\n}")]
    assert "threshold           = 0" in block


def test_model_spend_is_alarmed():
    assert "ValidationEstimatedCostUSD" in _read(ALARMS)


# --- data at rest and network exposure ---------------------------------------
#
# Each of these was a finding in an external security scan. They are guarded
# here because the fix is a default or a single attribute, which a later edit
# can undo silently: the stack still deploys and nothing fails until an audit.

ECS_MAIN = INFRA / "modules" / "ecs" / "main.tf"
ECS_VARS = INFRA / "modules" / "ecs" / "variables.tf"
KMS_MAIN = INFRA / "modules" / "kms" / "main.tf"
PPTX_DOCKERFILE = INFRA.parent / "backend" / "services" / "pptx-converter" / "Dockerfile"


def test_every_table_uses_the_customer_managed_key():
    blocks = _table_blocks(_read(DDB_MAIN))
    for name, body in sorted(blocks.items()):
        assert re.search(
            r"server_side_encryption\s*\{[^}]*kms_key_arn\s*=\s*var\.kms_key_arn",
            body, re.S,
        ), f"{name} must be encrypted with the customer-managed key"


def test_every_table_has_pitr():
    """Including chat_jobs: its rows expire after a day, so backing it up is
    almost free, and an exception is one more thing to justify in every audit."""
    blocks = _table_blocks(_read(DDB_MAIN))
    for name, body in sorted(blocks.items()):
        assert re.search(
            r"point_in_time_recovery\s*\{[^}]*enabled\s*=\s*true", body, re.S
        ), f"{name} must have point_in_time_recovery enabled"


def test_data_key_rotates():
    src = _strip_comments(_read(KMS_MAIN))
    assert re.search(r"enable_key_rotation\s*=\s*true", src)


def test_ecr_repositories_use_the_customer_managed_key():
    src = _strip_comments(_read(ECS_MAIN))
    repos = re.findall(r'resource\s+"aws_ecr_repository"\s+"(\w+)"', src)
    assert len(repos) == 2, repos
    assert len(re.findall(r'encryption_type\s*=\s*"KMS"', src)) == len(repos), (
        "every ECR repository must use KMS encryption with the data key"
    )


def test_alb_only_admits_cloudfront_by_default():
    src = _strip_comments(_read(ECS_VARS))
    block = re.search(
        r'variable\s+"restrict_alb_to_cloudfront"\s*\{.*?\n\}', src, re.S
    ).group(0)
    assert re.search(r"default\s*=\s*true", block), (
        "the ALB security group must default to CloudFront's prefix list; open "
        "to 0.0.0.0/0, the origin-verify header is the only control left"
    )


def test_alb_opens_a_single_origin_port():
    """CloudFront's prefix list counts ~55 rules against a 60-rule quota, so it
    can be referenced on one port only. Opening both 80 and 443 fails to apply."""
    src = _strip_comments(_read(ECS_MAIN))
    assert "[80, 443]" not in src
    assert "local.origin_port" in src


def test_pptx_sidecar_root_filesystem_is_read_only():
    src = _strip_comments(_read(ECS_MAIN))
    assert re.search(r"readonlyRootFilesystem\s*=\s*true", src)
    # The writable paths must exist as volumes, or LibreOffice fails on the
    # first conversion rather than at deploy time.
    for path in ("/tmp", "/home/converter"):
        assert f'containerPath = "{path}"' in src, f"{path} must be a mounted volume"
    assert 'VOLUME ["/tmp", "/home/converter"]' in _read(PPTX_DOCKERFILE), (
        "the Dockerfile must declare the volumes so they inherit the non-root "
        "user's ownership"
    )


def test_log_groups_keep_a_year_by_default():
    for path in [INFRA / "variables.tf", ECS_VARS,
                 INFRA / "modules" / "cloudtrail" / "variables.tf",
                 INFRA / "modules" / "monitoring" / "variables.tf"]:
        src = _strip_comments(_read(path))
        block = re.search(
            r'variable\s+"log_retention_days"\s*\{.*?\n\}', src, re.S
        ).group(0)
        days = int(re.search(r"default\s*=\s*(\d+)", block).group(1))
        assert days >= 365, f"{path.name}: log_retention_days default is {days}"
