#!/bin/bash
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Strict mode: -e (exit on error), -u (unset vars are errors), and
# pipefail (a failure anywhere in a pipe fails the whole pipe).
# Without pipefail, `docker push ... | tee` would mask a push
# failure because tee's exit code wins. That's how an earlier silent
# deploy let an unpushed image be reported as "Deployment complete".
set -euo pipefail

# deploy.sh — End-to-end deployment (reusable across projects/accounts)
#
# Usage: ./deploy.sh [all|infra|backend|pptx-converter|frontend]
#        ./deploy.sh create-user <email> [password]
#        ./deploy.sh seed-users
#
# The deploy target is selected by ENVIRONMENT (default: dev), which picks
# infrastructure/tfvars/<env>.tfvars. Project name, region, and resource
# names all come from that file — nothing is hardcoded here. Examples:
#   ENVIRONMENT=dev ./deploy.sh all
#   ENVIRONMENT=test PROJECT=my-proj ./deploy.sh infra
# Overridable env vars: ENVIRONMENT, TFVARS_FILE, PROJECT, AWS_REGION,
# AWS_ACCOUNT_ID, TF_STATE_BUCKET, TF_STATE_KEY.
#
# RBAC per-role user seeding (opt-in):
#   ./deploy.sh seed-users
# Creates one Cognito login per role (admin/validator/curator/viewer),
# sets a permanent password, and writes the matching row to the
# {PROJECT}-{ENVIRONMENT}-user-roles DynamoDB table so the role resolves
# without any manual /admin/roles assignment. Idempotent — existing users
# are tolerated and their password + role row are re-applied. Credentials
# come from env vars:
#   RBAC_ADMIN_EMAIL      (default: admin@example.com)
#   RBAC_VALIDATOR_EMAIL  (default: validator@example.com)
#   RBAC_CURATOR_EMAIL    (default: curator@example.com)
#   RBAC_VIEWER_EMAIL     (default: viewer@example.com)
#   RBAC_<ROLE>_PASSWORD  (no default — a random password is generated and
#                          printed once if you don't supply one)
# The default EMAILS are for dev/demo only — set the env vars (and rotate the
# passwords) for any shared or long-lived environment.

# Requirements:
# - AWS CLI configured with credentials
# - Docker installed and running
# - Terraform installed
# - Node.js 20+ installed

# Resolve the repo root from the script's own location so `cd backend`
# etc. work regardless of the caller's cwd. Earlier deploys silently
# no-op'd when called from inside backend/ — the relative `cd backend`
# tried to descend into backend/backend, set -e tripped before push.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Which deploy target: ENVIRONMENT selects the tfvars file, and
# PROJECT + region are read FROM that file so nothing project-specific is
# hardcoded here (this script is reusable across projects). Override any of
# ENVIRONMENT / TFVARS_FILE / PROJECT / AWS_REGION via the environment.
ENVIRONMENT="${ENVIRONMENT:-dev}"
TF_DIR="infrastructure"
TFVARS_FILE="${TFVARS_FILE:-${TF_DIR}/tfvars/${ENVIRONMENT}.tfvars}"

# Minimal HCL string-var reader: `_tfvar project_name` -> the value.
# Handles `name = "value"` with optional whitespace/trailing comment.
_tfvar() {
    grep -E "^[[:space:]]*$1[[:space:]]*=" "${TFVARS_FILE}" 2>/dev/null \
        | head -1 \
        | sed -E 's/^[^=]*=[[:space:]]*"?([^"#]*)"?.*$/\1/' \
        | sed -E 's/[[:space:]]+$//'
}

# Read identity from the tfvars file (env vars win if set).
_file_project=""
_file_env=""
_file_region=""
if [ -f "${TFVARS_FILE}" ]; then
    _file_project="$(_tfvar project_name)"
    _file_env="$(_tfvar environment)"
    _file_region="$(_tfvar aws_region)"
fi
PROJECT="${PROJECT:-${_file_project}}"
REGION="${AWS_REGION:-${_file_region:-us-east-1}}"


# Set under deploy_frontend() only; used in the post-deploy summary.
# Pre-init here so `set -u` doesn't kill the script when we deployed
# backend-only.
FRONTEND_URL=""

# Account ID is resolved at runtime via STS so this script works
# unchanged in any AWS account. Override by exporting AWS_ACCOUNT_ID
# before running (e.g. for cross-account assume-role flows).
ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")}"

# ECR repo URIs. Frontend has no ECR repo (it deploys to S3/CloudFront).
BACKEND_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${PROJECT}"
PPTX_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${PROJECT}-${ENVIRONMENT}/pptx-converter"

# Remote state (S3) — derived, one bucket per project+env+account so
# state lives in the target account. Same account + different env => same
# bucket, different key. deploy_infra creates the bucket if absent.
STATE_BUCKET="${TF_STATE_BUCKET:-${PROJECT}-${ENVIRONMENT}-tfstate-${ACCOUNT_ID}}"
STATE_KEY="${TF_STATE_KEY:-${ENVIRONMENT}/terraform.tfstate}"

# ALB DNS resolved at runtime from Terraform output rather than
# hardcoded — AWS generates a random suffix per account at ALB
# creation time. Falls back to empty if state isn't initialized
# yet; deploy_backend tolerates that.
ALB_URL=""

tf_output() {
    (cd "${TF_DIR}" && terraform output -raw "$1" 2>/dev/null) || echo ""
}

resolve_alb_url() {
    # Resolve the ALB DNS from AWS directly (uses the live deploy creds), so
    # this works for `backend`/`frontend` runs even when the local terraform
    # state isn't initialized for this account. The ELB is named
    # <project>-<env>-alb by the ecs module. Fall back to terraform output,
    # then leave empty only if the ALB genuinely doesn't exist yet.
    local dns
    # `|| true`: on a FRESH stack the ALB does not exist yet, and the CLI exits
    # non-zero (LoadBalancerNotFound). Under `set -e` a bare command
    # substitution assignment propagates that status and aborts the whole
    # deploy before `infra` ever runs — the ALB is created BY that apply, so
    # the first deploy of a new account could never succeed. 2>/dev/null hides
    # the message but not the exit code, so it must be tolerated explicitly.
    dns=$(aws elbv2 describe-load-balancers \
            --names "${PROJECT}-${ENVIRONMENT}-alb" \
            --region "${REGION}" \
            --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || true)
    if [ -z "$dns" ] || [ "$dns" = "None" ]; then
        dns=$(tf_output "alb_dns_name")
    fi
    if [ -n "$dns" ] && [ "$dns" != "None" ]; then
        ALB_URL="http://${dns}"
    fi
}

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[DEPLOY]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Check prerequisites
check_prereqs() {
    log "Checking prerequisites..."
    command -v aws >/dev/null 2>&1 || err "AWS CLI not found"
    command -v docker >/dev/null 2>&1 || err "Docker not found"
    command -v terraform >/dev/null 2>&1 || err "Terraform not found"
    command -v node >/dev/null 2>&1 || err "Node.js not found"

    # Verify AWS credentials
    aws sts get-caller-identity >/dev/null 2>&1 || err "AWS credentials not configured"

    # Account id must be resolvable so ECR repo URIs are correct.
    # Empty here means the get-caller-identity above succeeded but
    # ACCOUNT_ID didn't get set — usually a transient issue.
    [ -n "${ACCOUNT_ID}" ] || err "Could not resolve AWS account id (STS returned empty)"

    # The deploy target's tfvars file drives PROJECT/region/state names.
    # Without it we'd build ECR/cluster names with an empty PROJECT.
    [ -f "${TFVARS_FILE}" ] || err "Deploy config not found: ${TFVARS_FILE}. Copy infrastructure/tfvars/dev.example.tfvars to ${TFVARS_FILE} (or set TFVARS_FILE=), then re-run."
    [ -n "${PROJECT}" ] || err "project_name not set in ${TFVARS_FILE} (and PROJECT not exported)."
    # The tfvars environment must match ENVIRONMENT, or resource names
    # created by Terraform won't match the names deploy.sh targets.
    if [ -n "${_file_env}" ] && [ "${_file_env}" != "${ENVIRONMENT}" ]; then
        err "environment mismatch: ENVIRONMENT=${ENVIRONMENT} but ${TFVARS_FILE} has environment=\"${_file_env}\". They must match."
    fi
    log "Prerequisites OK (project ${PROJECT}, env ${ENVIRONMENT}, account ${ACCOUNT_ID}, region ${REGION})"

    # Best-effort ALB DNS lookup so the post-deploy log line is
    # accurate. Returns empty before infra is applied; that's fine.
    resolve_alb_url
}

# Ensure the KB index bootstrap's Python deps are importable. The
# null_resource.kb_index_bootstrap provisioner (run during
# `deploy_infra`) shells out to python3 and imports opensearch-py +
# boto3; if they're missing the provisioner dies mid-apply. Detect and
# install once so `deploy.sh infra` on a fresh host doesn't hang, rather
# than making the operator remember a manual `pip install`.
#
# The deps go into a dedicated virtualenv rather than the system
# interpreter: Homebrew and recent Debian/Ubuntu Pythons are "externally
# managed" (PEP 668) and refuse a plain `pip install`. The venv's bin/ is
# put first on PATH for the rest of this run, so the provisioner's bare
# `python3` resolves to it.
KB_VENV="${SCRIPT_DIR}/infrastructure/modules/knowledge-base/.venv"
ensure_kb_deps() {
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found — required by the KB index bootstrap provisioner"
    fi
    if [ -x "${KB_VENV}/bin/python3" ]; then
        export PATH="${KB_VENV}/bin:${PATH}"
    fi
    if python3 -c 'import opensearchpy, boto3' >/dev/null 2>&1; then
        return 0
    fi
    log "Installing KB index bootstrap deps (opensearch-py, boto3) into ${KB_VENV}..."
    python3 -m venv "${KB_VENV}" \
        || err "Could not create ${KB_VENV}. Install the python3 venv module and retry."
    "${KB_VENV}/bin/python3" -m pip install --quiet \
        -r "${SCRIPT_DIR}/infrastructure/modules/knowledge-base/requirements.txt" \
        || err "Failed to install KB bootstrap deps into ${KB_VENV}"
    export PATH="${KB_VENV}/bin:${PATH}"
    python3 -c 'import opensearchpy, boto3' >/dev/null 2>&1 \
        || err "KB bootstrap deps installed but not importable by python3 on PATH"
}

# Preflight the AOSS grant the KB index bootstrap needs. Bedrock won't
# create its vector index, so create_index.py signs an index-create
# call against the collection data plane as the *deployer*. That call
# needs the IAM action aoss:APIAccessAll on the calling principal (the
# data-access-policy membership is handled by Terraform). An account
# admin has it implicitly; a locked-down deploy user does not — and
# without it `terraform apply` HANGS on the bootstrap with repeated
# 403s before failing.
#
# We do NOT auto-grant it: attaching an IAM policy from inside a deploy
# script is a silent privilege escalation, and a principal missing
# aoss:APIAccessAll almost certainly also lacks iam:PutUserPolicy. So we
# detect the principal type and, when we can't confirm the grant, print
# the exact command and let the operator decide. Set
# AOSS_PREFLIGHT_SKIP=1 to bypass (e.g. known-admin runners).
preflight_aoss_grant() {
    [ "${AOSS_PREFLIGHT_SKIP:-0}" = "1" ] && return 0

    local caller_arn
    caller_arn=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo "")

    # Admin/assumed-role principals typically carry the permission
    # implicitly; we can't cheaply prove a deny without a real AOSS
    # collection to call against. Emit guidance keyed on principal type
    # so a fresh-account operator isn't blindsided by a mid-apply hang.
    case "${caller_arn}" in
        *:user/*)
            local user_name="${caller_arn##*/}"
            warn "Deploy principal is IAM user '${user_name}'. The KB index"
            warn "bootstrap needs the IAM action aoss:APIAccessAll on this user."
            warn "If 'terraform apply' hangs on null_resource.kb_index_bootstrap"
            warn "with repeated 403 Forbidden, grant it once and re-run:"
            echo ""
            echo "    aws iam put-user-policy \\"
            echo "      --user-name ${user_name} \\"
            echo "      --policy-name aoss-data-access \\"
            echo "      --policy-document '{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"aoss:APIAccessAll\",\"Resource\":\"*\"}]}'"
            echo ""
            warn "(Set AOSS_PREFLIGHT_SKIP=1 to silence this if already granted.)"
            ;;
        *:assumed-role/*|*:role/*)
            # Assumed role / SSO — the grant belongs on the base role,
            # which this session usually can't name or modify. Admin
            # roles have it implicitly; just note it.
            log "Deploy principal is a role; assuming aoss:APIAccessAll is present."
            log "If the KB index bootstrap 403s, add aoss:APIAccessAll to the role."
            ;;
        *)
            log "Could not determine deploy principal type; if the KB index"
            log "bootstrap 403s, grant aoss:APIAccessAll to the deployer."
            ;;
    esac
}

# ECR Login
ecr_login() {
    log "Logging into ECR..."
    aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com
}

# Ensure the S3 bucket that holds Terraform remote state exists in the
# target account before `terraform init` tries to use it. Idempotent:
# a HEAD that succeeds means it's already there. Versioning + encryption
# are enabled so state is recoverable and encrypted at rest.
ensure_state_backend() {
    if aws s3api head-bucket --bucket "${STATE_BUCKET}" >/dev/null 2>&1; then
        log "State bucket ${STATE_BUCKET} exists."
        return 0
    fi
    log "Creating Terraform state bucket ${STATE_BUCKET}..."
    # us-east-1 rejects a LocationConstraint; every other region requires it.
    if [ "${REGION}" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "${STATE_BUCKET}" --region "${REGION}" >/dev/null \
            || err "Failed to create state bucket ${STATE_BUCKET}"
    else
        aws s3api create-bucket --bucket "${STATE_BUCKET}" --region "${REGION}" \
            --create-bucket-configuration LocationConstraint="${REGION}" >/dev/null \
            || err "Failed to create state bucket ${STATE_BUCKET}"
    fi
    aws s3api put-bucket-versioning --bucket "${STATE_BUCKET}" \
        --versioning-configuration Status=Enabled >/dev/null 2>&1 || true
    aws s3api put-bucket-encryption --bucket "${STATE_BUCKET}" \
        --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null 2>&1 || true
    log "State bucket ready (versioned, encrypted)."
}

# Shared: init the backend + run terraform from the root. Idempotent.
_tf_init() {
    ensure_state_backend
    cd "${SCRIPT_DIR}/${TF_DIR}"
    # Partial backend config: bucket/key/region come from deploy.sh so one
    # root module serves any account/env. -reconfigure keeps switching
    # targets clean across runs.
    terraform init -input=false -reconfigure \
        -backend-config="bucket=${STATE_BUCKET}" \
        -backend-config="key=${STATE_KEY}" \
        -backend-config="region=${REGION}"
    cd "${SCRIPT_DIR}"
}

# Apply the full stack in one pass. ECS references images as plain strings
# and pulls them only when a task launches (no create-time image
# validation), and the services don't wait_for_steady_state, so apply
# returns without a running task. Images are pushed afterward and a
# --force-new-deployment launches the tasks.
#
deploy_infra() {
    log "Applying infrastructure (project ${PROJECT}, env ${ENVIRONMENT})..."

    # The Bedrock KB index bootstrap runs inside this apply. Make sure
    # its deps are importable and warn about the aoss:APIAccessAll grant
    # BEFORE apply, so a fresh account fails fast with guidance instead
    # of hanging on the provisioner.
    ensure_kb_deps
    preflight_aoss_grant
    _tf_init

    local tfvars_abs="${SCRIPT_DIR}/${TFVARS_FILE}"
    cd "${SCRIPT_DIR}/${TF_DIR}"
    terraform plan -input=false -var-file="${tfvars_abs}" -out=tfplan
    terraform apply -input=false tfplan
    rm -f tfplan

    log "Infrastructure deployed!"
    terraform output
    cd "${SCRIPT_DIR}"
}

# Verify the :latest tag in an ECR repo points at the digest we
# just pushed locally. Catches the silent-push class of failures
# where docker push exits non-zero but the script kept going (e.g.
# auth expired, registry typo, account mismatch). Aborts the deploy
# rather than calling update-service with stale image.
verify_push() {
    # Args:
    #   $1 — local docker tag (e.g. "${BACKEND_REPO}:latest")
    #   $2 — ECR repository name (e.g. "resilience-review" or
    #        "resilience-review-dev/pptx-converter"). NOT the URI;
    #        just the repo name as ECR knows it.
    local local_tag="$1"
    local ecr_repo_name="$2"

    # Filter local docker's RepoDigests by the registry prefix
    # we just pushed to. Without this, an image that's been pushed
    # to MULTIPLE registries (e.g. golden + customer-analytics in
    # the same Docker daemon) returns whichever RepoDigest happens
    # to come first — index .RepoDigests 0 is non-deterministic
    # and was silently picking up the golden-account digest while
    # the script was actually pushing to a different account.
    local registry_prefix="${local_tag%%/*}"
    local local_digest remote_digest
    local_digest=$(docker inspect --format='{{range .RepoDigests}}{{println .}}{{end}}' "${local_tag}" 2>/dev/null \
        | grep "^${registry_prefix}/" \
        | head -1 \
        | awk -F@ '{print $2}')
    if [ -z "${local_digest}" ]; then
        err "Could not read local digest for ${local_tag} (registry ${registry_prefix}) — push likely never happened"
    fi

    remote_digest=$(aws ecr describe-images \
        --repository-name "${ecr_repo_name}" \
        --image-ids imageTag=latest \
        --region "${REGION}" \
        --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true)

    if [ "${local_digest}" != "${remote_digest}" ]; then
        err "Push verification FAILED — local digest ${local_digest:0:16} != remote :latest ${remote_digest:0:16}. Aborting before ECS update."
    fi
    log "Push verified: ${ecr_repo_name}:latest = ${local_digest:0:16}…"
}

# --- Backend: build+push and roll are split so `deploy_backend` can push
#     then force a new deployment, and so the roll tolerates a service that
#     doesn't exist yet on a fresh stack (created by deploy_infra). ---
push_backend_image() {
    log "Building + pushing backend image..."
    cd "${SCRIPT_DIR}/backend"
    # --pull forces a fresh fetch of the base image on every build so a
    # stale locally-cached python:3.x-slim can't ship known-CVE layers.
    # A changed base digest cascades cache invalidation to the apt/pip
    # layers, so this keeps the whole image patched (SEV prevention).
    docker build --pull --platform linux/amd64 -t ${PROJECT}:latest .
    docker tag ${PROJECT}:latest ${BACKEND_REPO}:latest
    log "Pushing to ECR (${BACKEND_REPO})..."
    docker push ${BACKEND_REPO}:latest
    verify_push "${BACKEND_REPO}:latest" "${PROJECT}"
    cd "${SCRIPT_DIR}"
}

# Block until the rollout has finished: one deployment left, the old task
# drained and stopped. update-service returns as soon as the rollout is
# requested, so without this "deployed" printed while the OLD task was still
# taking requests — and anything started on it (a validation, an extraction)
# died minutes later when it was stopped.
wait_for_rollout() {
    log "Waiting for ${1} to finish rolling out..."
    aws ecs wait services-stable \
        --cluster ${PROJECT}-${ENVIRONMENT}-cluster \
        --services "${1}" \
        --region ${REGION} \
        || err "${1} did not reach a steady state; check its ECS service events."
}

# Force a new deployment so ECS pulls the freshly-pushed image. Tolerant:
# on a fresh stack the service may not exist yet (created later by the full
# apply, which launches it against the now-present image), so a missing
# service is a warning, not a failure.
roll_backend() {
    log "Rolling backend ECS service..."
    if aws ecs update-service \
        --cluster ${PROJECT}-${ENVIRONMENT}-cluster \
        --service ${PROJECT}-${ENVIRONMENT}-service \
        --force-new-deployment \
        --region ${REGION} >/dev/null 2>&1; then
        wait_for_rollout "${PROJECT}-${ENVIRONMENT}-service"
        log "Backend deployed! URL: ${ALB_URL}"
    else
        warn "Backend ECS service not found yet (will launch on full apply)."
    fi
}

# Standalone: ./deploy.sh backend — push then roll (updates a live stack).
deploy_backend() {
    push_backend_image
    roll_backend
}


# Deploy pptx-converter sidecar
#
# The sidecar lives in its own ECR repo and ECS service. We build
# linux/amd64 (Fargate runs amd64), tag :latest, push, then force a
# new deployment so ECS pulls the new image. Same shape as
# deploy_backend so the operational model stays consistent.
push_pptx_image() {
    log "Building + pushing pptx-converter sidecar image..."
    cd "${SCRIPT_DIR}/backend/services/pptx-converter"
    # --pull: see push_backend_image. Always fetch the newest patched base
    # so the sidecar can't re-ship a stale python:3.x-slim (SEV prevention).
    docker build --pull --platform linux/amd64 -t ${PROJECT}-pptx-converter:latest .
    docker tag ${PROJECT}-pptx-converter:latest ${PPTX_REPO}:latest
    log "Pushing pptx-converter to ECR..."
    docker push ${PPTX_REPO}:latest
    verify_push "${PPTX_REPO}:latest" "${PROJECT}-${ENVIRONMENT}/pptx-converter"
    cd "${SCRIPT_DIR}"
}

roll_pptx() {
    log "Rolling pptx-converter ECS service..."
    if aws ecs update-service \
        --cluster ${PROJECT}-${ENVIRONMENT}-cluster \
        --service ${PROJECT}-${ENVIRONMENT}-pptx-converter \
        --force-new-deployment \
        --region ${REGION} >/dev/null 2>&1; then
        wait_for_rollout "${PROJECT}-${ENVIRONMENT}-pptx-converter"
        log "pptx-converter deployed (internal: pptx-converter.${PROJECT}-${ENVIRONMENT}.local:8001)"
    else
        warn "pptx-converter ECS service not found yet (will launch on full apply)."
    fi
}

# Standalone: ./deploy.sh pptx-converter — push then roll.
deploy_pptx_converter() {
    push_pptx_image
    roll_pptx
}

# Deploy Frontend (S3 + CloudFront)
deploy_frontend() {
    log "Building frontend static site..."
    cd "${SCRIPT_DIR}/frontend"

    # Pull Cognito identifiers from SSM so the static export carries
    # them at build time. They're non-secret — the actual server client
    # secret stays in SSM and is only read by the backend.
    USER_POOL_ID=$(aws ssm get-parameter \
        --name /${PROJECT}/${ENVIRONMENT}/cognito/user-pool-id \
        --region ${REGION} --query "Parameter.Value" --output text 2>/dev/null) || \
        warn "Could not read cognito/user-pool-id from SSM"
    WEB_CLIENT_ID=$(aws ssm get-parameter \
        --name /${PROJECT}/${ENVIRONMENT}/cognito/web-app-client-id \
        --region ${REGION} --query "Parameter.Value" --output text 2>/dev/null) || \
        warn "Could not read cognito/web-app-client-id from SSM"

    # Hosted-UI domain + SAML provider drive the "Sign in with SSO" redirect
    # flow. Both are optional: an empty hosted-UI domain disables the OAuth
    # config, and an empty provider hides the SSO button (password login only).
    # Written by Terraform (aws_ssm_parameter.cognito_hosted_ui_domain /
    # cognito_saml_provider); tolerate absence on older stacks.
    HOSTED_UI_DOMAIN=$(aws ssm get-parameter \
        --name /${PROJECT}/${ENVIRONMENT}/cognito/hosted-ui-domain \
        --region ${REGION} --query "Parameter.Value" --output text 2>/dev/null) || \
        HOSTED_UI_DOMAIN=""
    SAML_PROVIDER=$(aws ssm get-parameter \
        --name /${PROJECT}/${ENVIRONMENT}/cognito/saml-provider \
        --region ${REGION} --query "Parameter.Value" --output text 2>/dev/null) || \
        SAML_PROVIDER=""

    # Install exactly what the lockfile pins, as CI does, rather than trusting
    # whatever node_modules holds. A partial or stale install fails late and
    # obscurely: a missing native binary, for instance, surfaces only as
    # "Turbopack is not supported on this platform".
    log "Installing frontend dependencies (npm ci)..."
    npm ci --prefer-offline --no-audit --no-fund \
        || err "npm ci failed in frontend/"

    NEXT_PUBLIC_API_URL="" \
    NEXT_PUBLIC_AWS_REGION="${REGION}" \
    NEXT_PUBLIC_COGNITO_USER_POOL_ID="${USER_POOL_ID}" \
    NEXT_PUBLIC_COGNITO_WEB_APP_CLIENT_ID="${WEB_CLIENT_ID}" \
    NEXT_PUBLIC_COGNITO_HOSTED_UI_DOMAIN="${HOSTED_UI_DOMAIN}" \
    NEXT_PUBLIC_COGNITO_SAML_PROVIDER="${SAML_PROVIDER}" \
        npm run build

    log "Deploying to S3..."
    # Two passes, because the right caching differs by asset class and the
    # objects carried no Cache-Control at all before this. Without a header a
    # browser caches heuristically: it may reuse a stale index.html — and
    # therefore the old chunk URLs it references — indefinitely, so a deploy
    # goes live yet a returning user never sees it. A CloudFront invalidation
    # does not help, since the request never leaves the browser.
    #
    # Pass 1: everything under _next/static/ is content-hashed by the build, so
    # a changed file always has a new URL and can be cached immutably.
    aws s3 sync out/ s3://${PROJECT}-${ENVIRONMENT}-frontend-${ACCOUNT_ID}/ \
        --delete --region ${REGION} \
        --exclude "*" --include "_next/static/*" \
        --cache-control "public,max-age=31536000,immutable"

    # Pass 2: the HTML entry points keep stable URLs, so they must be
    # revalidated on every navigation or the app pins itself to an old build.
    # --delete runs here so it sees the full local tree (a --delete on the
    # filtered pass above would remove everything outside _next/static).
    aws s3 sync out/ s3://${PROJECT}-${ENVIRONMENT}-frontend-${ACCOUNT_ID}/ \
        --delete --region ${REGION} \
        --exclude "_next/static/*" \
        --cache-control "no-cache,must-revalidate"

    # Get CloudFront distribution ID
    DIST_ID=$(aws cloudfront list-distributions \
        --query "DistributionList.Items[?Comment=='${PROJECT} ${ENVIRONMENT} frontend distribution'].Id" \
        --output text --region ${REGION} 2>/dev/null)

    if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
        log "Invalidating CloudFront cache..."
        aws cloudfront create-invalidation \
            --distribution-id ${DIST_ID} \
            --paths "/*" >/dev/null

        FRONTEND_URL=$(aws cloudfront list-distributions \
            --query "DistributionList.Items[?Id=='${DIST_ID}'].DomainName" \
            --output text)
        log "Frontend deployed! URL: https://${FRONTEND_URL}"
    else
        log "Frontend deployed to S3. Configure CloudFront distribution for HTTPS access."
    fi

    cd ..
}

# Create the first Cognito login user. Every /api/* route requires a
# Cognito JWT, so a fresh deploy has no way to log in until a user
# exists. Reads the pool id from Terraform output (infra must be
# applied first). Idempotent: an already-existing username is treated
# as success so re-runs don't fail.
#
# Generate a random password that satisfies the Cognito policy (upper, lower,
# digit, symbol). Used when no password is supplied, so this script never plants
# a credential that is published in the repo.
random_password() {
    local body
    body=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 20)
    printf 'Aa1!%s' "${body}"
}

# Usage: ./deploy.sh create-user <email> [password]
#   With no password a random one is generated and printed once.
create_user() {
    local username="${1:-}"
    local password="${2:-}"
    local generated=0
    if [ -z "${password}" ]; then
        password=$(random_password)
        generated=1
    fi

    [ -n "${username}" ] || err "Usage: ./deploy.sh create-user <email> [password]"

    local pool_id
    pool_id=$(tf_output "cognito_user_pool_id")
    [ -n "${pool_id}" ] || err "cognito_user_pool_id not found in Terraform output — run './deploy.sh infra' first."

    log "Creating Cognito user '${username}' in pool ${pool_id}..."
    # email_verified is set here by an ADMINISTRATOR; it is not self-asserted by
    # the user. rbac.resolve_role honours BOOTSTRAP_ADMIN_EMAIL only on a
    # VERIFIED email, because an unverified address is self-assertable and
    # matching it grants admin. admin-create-user sets no attributes by default,
    # so without this the created login resolves to `viewer` — and viewer cannot
    # reach /admin/roles to fix its own role, leaving a fresh deploy with no way
    # in. Set it for every admin-created user so the role a deploy intends is
    # the role the token actually carries.
    if aws cognito-idp admin-create-user \
        --user-pool-id "${pool_id}" \
        --username "${username}" \
        --user-attributes Name=email_verified,Value=true \
        --message-action SUPPRESS \
        --region "${REGION}" >/dev/null 2>&1; then
        log "User created."
    else
        # UsernameExistsException (or similar) — keep going so we can
        # still (re)set the password, making the command idempotent.
        warn "admin-create-user did not create a new user (already exists?); continuing to set password."
        # A user created before this flag was added carries no email_verified,
        # so re-apply it on the existing-user path too. Otherwise re-running the
        # command could never repair an account stuck resolving to viewer.
        aws cognito-idp admin-update-user-attributes \
            --user-pool-id "${pool_id}" \
            --username "${username}" \
            --user-attributes Name=email_verified,Value=true \
            --region "${REGION}" >/dev/null 2>&1 \
            || warn "Could not set email_verified on '${username}'; a bootstrap-admin match will be ignored."
    fi

    log "Setting permanent password (skips force-change challenge)..."
    aws cognito-idp admin-set-user-password \
        --user-pool-id "${pool_id}" \
        --username "${username}" \
        --password "${password}" \
        --permanent \
        --region "${REGION}" >/dev/null \
        || err "Failed to set password for '${username}'."

    log "Login user ready: ${username}"
    [ "${generated}" -eq 1 ] && warn "generated password (save it now): ${password}"
    return 0
}

# Seed one Cognito login per RBAC role (admin/validator/curator/viewer) and
# write the matching row to the {PROJECT}-{ENVIRONMENT}-user-roles DDB table so
# the role resolves immediately (resolve_role reads that table) without any
# manual /admin/roles assignment. Opt-in target — never runs as part of `all`.
#
# Idempotent: an existing username is tolerated (we still (re)set the password
# and (re)write the role row). Credentials come from RBAC_<ROLE>_EMAIL /
# RBAC_<ROLE>_PASSWORD env vars; an unset password is randomly generated
# (see the header comment). Defaults are dev/demo-only — override + rotate for
# anything shared.
#
# Uses the SAME resource-name derivation as the rest of this script
# (PROJECT/ENVIRONMENT/REGION from tfvars) and the SAME STS account guard, so
# it never operates outside the account the deploy creds resolve to.
seed_one_role() {
    # $1 = role, $2 = email, $3 = password, $4 = cognito pool id, $5 = ddb table
    local role="$1" email="$2" password="$3" pool_id="$4" table="$5"

    log "Seeding ${role} → ${email}"

    # Create the Cognito user (idempotent — tolerate an existing user so the
    # password + role row below still get (re)applied on re-runs).
    # email_verified: set by an administrator, not self-asserted. Seeded users
    # get their role from the group + role row below, so this is not what makes
    # THEIR role resolve — but leaving it unset means an operator who later
    # points bootstrap_admin_email at a seeded address finds it silently
    # ignored. Keep both create paths consistent (see create_user).
    if aws cognito-idp admin-create-user \
        --user-pool-id "${pool_id}" \
        --username "${email}" \
        --user-attributes Name=email_verified,Value=true \
        --message-action SUPPRESS \
        --region "${REGION}" >/dev/null 2>&1; then
        log "  Cognito user created."
    else
        warn "  admin-create-user did not create a new user (already exists?); continuing."
        aws cognito-idp admin-update-user-attributes \
            --user-pool-id "${pool_id}" \
            --username "${email}" \
            --user-attributes Name=email_verified,Value=true \
            --region "${REGION}" >/dev/null 2>&1 \
            || warn "  Could not set email_verified on '${email}'."
    fi

    # Permanent password so the user can log in without the force-change
    # challenge.
    aws cognito-idp admin-set-user-password \
        --user-pool-id "${pool_id}" \
        --username "${email}" \
        --password "${password}" \
        --permanent \
        --region "${REGION}" >/dev/null \
        || err "  Failed to set password for '${email}'."

    # Resolve the user's Cognito sub — the user-roles table is keyed on sub,
    # not email (matches app.services.user_roles_store).
    local sub
    sub=$(aws cognito-idp admin-get-user \
        --user-pool-id "${pool_id}" \
        --username "${email}" \
        --region "${REGION}" \
        --query 'UserAttributes[?Name==`sub`].Value | [0]' \
        --output text 2>/dev/null || true)
    if [ -z "${sub}" ] || [ "${sub}" = "None" ]; then
        err "  Could not resolve Cognito sub for '${email}'."
    fi

    # Write/overwrite the role assignment row. put-item is an upsert, so this
    # is idempotent on re-runs. Shape matches user_roles_store.set_role:
    # {sub, role, email, updated_at}.
    local now
    now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    aws dynamodb put-item \
        --table-name "${table}" \
        --region "${REGION}" \
        --item "{\"sub\":{\"S\":\"${sub}\"},\"role\":{\"S\":\"${role}\"},\"email\":{\"S\":\"${email}\"},\"updated_at\":{\"S\":\"${now}\"}}" \
        >/dev/null \
        || err "  Failed to write role row for '${email}' to ${table}."

    log "  Role row written: ${role} (sub ${sub:0:8}…)"

    # Also add the user to the matching Cognito group (name == role, created by
    # the cognito Terraform module). Membership surfaces in the token's
    # cognito:groups claim, which resolve_role reads FIRST (ahead of the DDB
    # row) — so this is what exercises the group-based path end to end.
    # Idempotent (re-adding is a no-op) and non-fatal: if the groups haven't
    # been applied yet (`./deploy.sh infra` not run since this change), the DDB
    # row above still makes the role work, so we warn rather than abort.
    if aws cognito-idp admin-add-user-to-group \
        --user-pool-id "${pool_id}" \
        --username "${email}" \
        --group-name "${role}" \
        --region "${REGION}" >/dev/null 2>&1; then
        log "  Added to Cognito group: ${role}"
    else
        warn "  Could not add '${email}' to group '${role}' (group missing? run './deploy.sh infra'). Role still resolves via the user-roles table."
    fi
}

seed_users() {
    local pool_id
    pool_id=$(tf_output "cognito_user_pool_id")
    [ -n "${pool_id}" ] || err "cognito_user_pool_id not found in Terraform output — run './deploy.sh infra' first."

    # user-roles table name is derived the same way Terraform names it
    # (modules/dynamodb: "${project}-${environment}-user-roles").
    local table="${PROJECT}-${ENVIRONMENT}-user-roles"

    log "Seeding per-role users into pool ${pool_id}, roles table ${table}..."

    # Passwords are NOT defaulted to fixed strings: a seed run against any
    # long-lived environment would otherwise plant four logins whose passwords
    # are published in this repo. When RBAC_<ROLE>_PASSWORD is unset we mint a
    # random one and print it once, so the credential exists only here.
    local admin_pw validator_pw curator_pw viewer_pw
    admin_pw="${RBAC_ADMIN_PASSWORD:-$(random_password)}"
    validator_pw="${RBAC_VALIDATOR_PASSWORD:-$(random_password)}"
    curator_pw="${RBAC_CURATOR_PASSWORD:-$(random_password)}"
    viewer_pw="${RBAC_VIEWER_PASSWORD:-$(random_password)}"

    seed_one_role "admin"     "${RBAC_ADMIN_EMAIL:-admin@example.com}"         "${admin_pw}"     "${pool_id}" "${table}"
    seed_one_role "validator" "${RBAC_VALIDATOR_EMAIL:-validator@example.com}" "${validator_pw}" "${pool_id}" "${table}"
    seed_one_role "curator"   "${RBAC_CURATOR_EMAIL:-curator@example.com}"     "${curator_pw}"   "${pool_id}" "${table}"
    seed_one_role "viewer"    "${RBAC_VIEWER_EMAIL:-viewer@example.com}"       "${viewer_pw}"    "${pool_id}" "${table}"

    log "Per-role user seeding complete."
    # Echo only the generated ones — anything supplied via env is already known
    # to the caller and shouldn't be re-printed into logs.
    [ -n "${RBAC_ADMIN_PASSWORD:-}" ]     || warn "admin password (save it now):     ${admin_pw}"
    [ -n "${RBAC_VALIDATOR_PASSWORD:-}" ] || warn "validator password (save it now): ${validator_pw}"
    [ -n "${RBAC_CURATOR_PASSWORD:-}" ]   || warn "curator password (save it now):   ${curator_pw}"
    [ -n "${RBAC_VIEWER_PASSWORD:-}" ]    || warn "viewer password (save it now):    ${viewer_pw}"
}

# Main
main() {
    local target="${1:-all}"

    echo "============================================"
    echo " Architecture Compliance Review - Deploy"
    echo " Target: ${target}"
    echo " Environment: ${ENVIRONMENT}"
    echo " Region: ${REGION}"
    echo "============================================"
    echo ""

    # create-user needs only AWS creds + Terraform output — no Docker
    # build or ECR login. Handle it before the heavier prereq path so it
    # works even when Docker isn't running.
    if [ "${target}" = "create-user" ]; then
        command -v aws >/dev/null 2>&1 || err "AWS CLI not found"
        aws sts get-caller-identity >/dev/null 2>&1 || err "AWS credentials not configured"
        create_user "${2:-}" "${3:-}"
        echo ""
        log "Done."
        return 0
    fi

    # seed-users, like create-user, needs only AWS creds + Terraform output
    # (no Docker). It also needs PROJECT (from tfvars) to name the user-roles
    # table. Keep the SAME STS account guard so it never touches an account
    # the deploy creds don't resolve to.
    if [ "${target}" = "seed-users" ]; then
        command -v aws >/dev/null 2>&1 || err "AWS CLI not found"
        aws sts get-caller-identity >/dev/null 2>&1 || err "AWS credentials not configured"
        [ -n "${ACCOUNT_ID}" ] || err "Could not resolve AWS account id (STS returned empty)"
        [ -n "${PROJECT}" ] || err "project_name not set in ${TFVARS_FILE} (and PROJECT not exported)."
        log "seed-users targeting account ${ACCOUNT_ID}, project ${PROJECT}, env ${ENVIRONMENT}, region ${REGION}"
        seed_users
        echo ""
        log "Done."
        return 0
    fi

    check_prereqs
    ecr_login

    case ${target} in
        all)
            # Single-pass: infra creates ECR repos + ECS services (which
            # don't validate images at create time and don't wait for a
            # running task), then we build+push images and roll the
            # services onto them, then the frontend (needs Cognito SSM
            # from the apply). pptx before backend so the API's
            # PPTX_CONVERTER_URL points at a service already accepting work.
            deploy_infra
            deploy_pptx_converter
            deploy_backend
            deploy_frontend
            ;;
        infra)
            deploy_infra
            ;;
        backend)
            deploy_backend
            ;;
        pptx-converter|sidecar)
            deploy_pptx_converter
            ;;
        frontend)
            deploy_frontend
            ;;
        *)
            err "Unknown target: ${target}. Use: all, infra, backend, pptx-converter, frontend, create-user, seed-users"
            ;;
    esac

    echo ""
    log "Deployment complete!"
    echo ""

    # Re-resolve access info from Terraform output now that infra exists.
    # (check_prereqs' early attempt is empty on a fresh deploy — no state
    # yet at that point.) Fall back to the CloudFront output for the
    # frontend URL when this run didn't invoke deploy_frontend.
    resolve_alb_url
    if [ -z "${FRONTEND_URL}" ]; then
        local cf
        cf=$(tf_output "cloudfront_domain_name")
        [ -n "${cf}" ] && FRONTEND_URL="${cf}"
    fi

    # The ALB answers 403 to anything without CloudFront's origin-verify header,
    # so print it as an internal origin rather than a usable backend URL — the
    # API is reached at https://<frontend>/api/*.
    echo "  ALB origin (internal, 403s direct traffic):  ${ALB_URL:-<run ./deploy.sh infra to create the ALB>}"
    if [ -n "${FRONTEND_URL}" ]; then
        echo "  App + API: https://${FRONTEND_URL}  (API under /api/*)"
    fi
    echo ""
}

main "$@"
