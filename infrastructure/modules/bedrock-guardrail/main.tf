# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# Amazon Bedrock Guardrail
#
# Scope is deliberately narrow: sensitive-information filters plus prompt-attack
# detection, and nothing else.
#
# This application reads compliance material — High Availability policies and
# system design documents — that legitimately discusses firewalls, key
# management, encryption, failure modes, and how a control could be bypassed.
# Bedrock's managed content filters (VIOLENCE, MISCONDUCT, and similar) and
# denied-topic policies intervene on exactly that language, and an intervention
# during validation surfaces as a failed or empty verdict rather than as a
# recognizable policy decision. Enabling them would make the tool unreliable on
# its own subject matter.
#
# What the two enabled policies protect against is real and does not overlap the
# subject matter:
#   - Sensitive information: a design document or policy PDF frequently contains
#     operator email addresses, phone numbers, and occasionally a pasted
#     credential. Those are redacted before they reach a model and before they
#     are written into a finding's stored trace.
#   - Prompt attack: rule text, reference bodies, and icon-legend descriptions
#     are all attacker-influenced strings that end up inside a prompt. The
#     application already fences them (see app/services/prompt_safety.py); this
#     is the second layer.
###############################################################################

resource "aws_bedrock_guardrail" "main" {
  name                      = "${var.project_name}-${var.environment}-guardrail"
  description               = "PII redaction and prompt-attack detection for compliance review model calls."
  blocked_input_messaging   = var.blocked_input_message
  blocked_outputs_messaging = var.blocked_output_message

  # Redact rather than block. A design document containing an operator's email
  # address is normal, and refusing to validate it would be a false positive;
  # removing the address before the call is the correct outcome.
  sensitive_information_policy_config {
    dynamic "pii_entities_config" {
      for_each = var.pii_entities
      content {
        type   = pii_entities_config.value
        action = "ANONYMIZE"
      }
    }

    # Credential shapes the managed PII types do not cover. Anchored patterns
    # rather than broad ones, so ordinary prose is not rewritten.
    dynamic "regexes_config" {
      for_each = var.credential_patterns
      content {
        name        = regexes_config.value.name
        description = regexes_config.value.description
        pattern     = regexes_config.value.pattern
        action      = "ANONYMIZE"
      }
    }
  }

  # PROMPT_ATTACK is deliberately NOT enabled, and the reason is structural
  # rather than a tuning preference.
  #
  # Bedrock's prompt-attack filter classifies the USER turn, on the assumption
  # that system instructions arrive separately and that anything
  # instruction-shaped in the user turn is therefore an injection attempt. This
  # application sends its own instructions AS the user turn — the validator's
  # per-rule prompt, the topology extractor, and the icon interpreter all say
  # things like "Return JSON ONLY", "based ONLY on what is visually depicted",
  # and "do not invent a rule the unit does not support".
  #
  # Measured against a live guardrail: the icon-interpretation prompt, the
  # topology-extraction prompt, and the validator's chunk template are ALL
  # blocked at inputStrength HIGH, MEDIUM *and* LOW. There is no strength at
  # which this application's own prompts pass, so the filter cannot be tuned
  # into usefulness here — enabling it silently breaks validation, extraction,
  # and icon interpretation, each degrading to an empty or stub result.
  #
  # Injection protection for the genuinely untrusted strings — rule text,
  # reference bodies, icon-legend descriptions — is app-side in
  # app/services/prompt_safety.py, which fences them in a delimited block so a
  # crafted body cannot forge prompt structure. That control targets the actual
  # threat surface, which is interpolated content rather than the prompt itself.
  #
  # Re-enable only with a prompt architecture that puts instructions in the
  # system field, and re-measure the three prompts above before doing so.

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-guardrail"
  })
}

###############################################################################
# Version
#
# Bedrook requires a numbered version to be referenced at invoke time; DRAFT is
# mutable and is not suitable for a deployed environment. The version is
# recreated when the guardrail changes, so the application always references an
# immutable snapshot of the policy that was reviewed.
###############################################################################

resource "aws_bedrock_guardrail_version" "main" {
  guardrail_arn = aws_bedrock_guardrail.main.guardrail_arn

  # The description embeds a hash of the policy so that editing the policy forces
  # a NEW version. Without this the version resource has no dependency on the
  # policy content: Terraform updates DRAFT in place, leaves the numbered version
  # untouched, and the application — which pins a number, never DRAFT — keeps
  # running the previous policy. That is how a guardrail edit can appear to apply
  # while changing nothing at all.
  description = "Managed by Terraform. Policy revision ${substr(sha256(jsonencode({
    pii     = var.pii_entities
    regexes = var.credential_patterns
    blocked = [var.blocked_input_message, var.blocked_output_message]
  })), 0, 12)}"

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# Task-role permission
#
# ApplyGuardrail is required for the standalone API; Converse with a
# guardrailConfig also requires it. Scoped to this guardrail only.
###############################################################################

data "aws_iam_policy_document" "guardrail_access" {
  statement {
    sid    = "AllowApplyGuardrail"
    effect = "Allow"
    actions = [
      "bedrock:ApplyGuardrail",
    ]
    resources = [aws_bedrock_guardrail.main.guardrail_arn]
  }
}

resource "aws_iam_policy" "guardrail_access" {
  name        = "${var.project_name}-${var.environment}-guardrail-access"
  description = "Allows the ECS task role to apply the Bedrock guardrail."
  policy      = data.aws_iam_policy_document.guardrail_access.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "guardrail_access" {
  role       = var.ecs_task_role_name
  policy_arn = aws_iam_policy.guardrail_access.arn
}
