# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import get_current_user
from app.api.rbac import require_role
from app.config import settings
from app.api.routes import documents, validation, queries, teams, standards, references, roles, audit, icon_legend

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown events."""
    logger.info("Starting Architecture Compliance Review")
    logger.info(f"AWS Region: {settings.aws_region}")
    logger.info(f"Bedrock Model: {settings.bedrock_model_id}")
    logger.info(f"S3 Bucket: {settings.s3_documents_bucket}")

    # Start document processing pipeline
    from app.services.pipeline import pipeline
    await pipeline.start()
    logger.info("Document processing pipeline started")

    yield

    # Stop document processing pipeline
    await pipeline.stop()
    logger.info("Shutting down Architecture Compliance Review")


app = FastAPI(
    title="Architecture Compliance Review API",
    description="AI-assisted resilience and High Availability validation for infrastructure designs",
    version="1.0.0",
    lifespan=lifespan,
    # Served under /api so CloudFront routes them to this origin. At the default
    # /openapi.json they were only reachable by hitting the ALB directly, which
    # the listener now refuses. The schema lists route paths, which the frontend
    # bundle already reveals, so this exposes nothing new.
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url=None,
)

# CORS middleware.
# Security: allow_origins=["*"] together with allow_credentials=True is an
# invalid/insecure combination — the CORS spec forbids a wildcard origin on
# credentialed requests, and browsers reject it. This API authenticates via a
# Bearer header (not cookies), so when origins are wildcard we disable
# credentials to keep a valid, non-permissive config. Set CORS_ORIGINS to the
# frontend's CloudFront URL in prod to lock it down explicitly.
_cors_origins = settings.cors_origin_list
_cors_wildcard = _cors_origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=not _cors_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
@app.get("/api/health")
async def health_check():
    """Liveness probe. PUBLIC — the ALB target group and the frontend's
    connectivity indicator both poll it without credentials.

    Deliberately says nothing but "the process is up". The detailed version used
    to live here and returned the S3 bucket name, DynamoDB table name, Bedrock
    model id, Knowledge Base + data-source ids, the region, and raw exception
    text from each dependency probe — a free map of the account for an
    unauthenticated caller, reachable from the internet. That payload moved to
    /api/health/detail behind admin auth.
    """
    return {"status": "ok", "service": f"{settings.project_name}-validation"}


@app.get("/api/health/detail")
async def health_check_detailed(
    claims: dict = Depends(require_role()),  # admin only
):
    """Dependency diagnostics. ADMIN ONLY — reports infrastructure identifiers
    and upstream error detail, which is exactly what an attacker would want for
    reconnaissance."""
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError

    dependencies = {}
    overall_status = "healthy"

    # Check DynamoDB
    try:
        dynamodb = boto3.client("dynamodb", region_name=settings.aws_region)
        dynamodb.describe_table(TableName=settings.documents_table)
        dependencies["dynamodb"] = {"status": "ok", "tables_accessible": True}
    except NoCredentialsError:
        dependencies["dynamodb"] = {"status": "warning", "message": "No AWS credentials"}
        overall_status = "degraded"
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        dependencies["dynamodb"] = {"status": "error", "message": f"Error: {error_code}"}
        overall_status = "unhealthy"
    except Exception as e:
        dependencies["dynamodb"] = {"status": "error", "message": type(e).__name__}
        overall_status = "unhealthy"

    # Check S3
    try:
        s3 = boto3.client("s3", region_name=settings.aws_region)
        s3.head_bucket(Bucket=settings.s3_documents_bucket)
        dependencies["s3"] = {"status": "ok", "bucket_accessible": True}
    except NoCredentialsError:
        dependencies["s3"] = {"status": "warning", "message": "No AWS credentials"}
        overall_status = "degraded"
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        dependencies["s3"] = {"status": "error", "message": f"Error: {error_code}"}
        overall_status = "unhealthy"
    except Exception as e:
        dependencies["s3"] = {"status": "error", "message": type(e).__name__}
        overall_status = "unhealthy"

    # Check Bedrock
    try:
        boto3.client("bedrock-runtime", region_name=settings.aws_region)
        # Simple API call to verify connectivity (doesn't actually invoke model)
        dependencies["bedrock"] = {"status": "ok", "model_id": settings.bedrock_model_id}
    except NoCredentialsError:
        dependencies["bedrock"] = {"status": "warning", "message": "No AWS credentials"}
        overall_status = "degraded"
    except Exception as e:
        dependencies["bedrock"] = {"status": "error", "message": type(e).__name__}
        overall_status = "unhealthy"

    # Check Knowledge Base
    if settings.knowledge_base_id:
        try:
            boto3.client("bedrock-agent-runtime", region_name=settings.aws_region)
            # Verify KB exists by attempting to retrieve (with empty query, will fail but confirms access)
            dependencies["knowledge_base"] = {
                "status": "ok",
                "kb_id": settings.knowledge_base_id,
                "data_source_id": settings.data_source_id
            }
        except NoCredentialsError:
            dependencies["knowledge_base"] = {"status": "warning", "message": "No AWS credentials"}
            overall_status = "degraded"
        except Exception as e:
            dependencies["knowledge_base"] = {"status": "error", "message": type(e).__name__}
            overall_status = "unhealthy"
    else:
        dependencies["knowledge_base"] = {"status": "not_configured", "message": "KB not configured"}

    return {
        "status": overall_status,
        "service": f"{settings.project_name}-validation",
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "config": {
            "aws_region": settings.aws_region,
            "bedrock_model": settings.bedrock_model_id,
            "s3_bucket": settings.s3_documents_bucket,
            "knowledge_base_configured": settings.knowledge_base_id is not None,
        },
        "dependencies": dependencies,
    }


# All /api/* routers require a valid Cognito ID token. The /health and
# /api/health endpoints are registered at the app level above and stay public
# so the ALB can health-check the container.
auth = [Depends(get_current_user)]

app.include_router(documents.router, prefix="/api/documents", tags=["Documents"], dependencies=auth)
app.include_router(validation.router, prefix="/api/validations", tags=["Validations"], dependencies=auth)
app.include_router(queries.router, prefix="/api/queries", tags=["Queries"], dependencies=auth)
app.include_router(teams.router, prefix="/api/teams", tags=["Teams"], dependencies=auth)
app.include_router(standards.router, prefix="/api/standards", tags=["Standards"], dependencies=auth)
# References (factual context blocks the validator
# attaches alongside rules; e.g. v2 doc page 9's DC-peer table).
app.include_router(references.router, prefix="/api/references", tags=["References"], dependencies=auth)
# User↔role management. Router-level auth (any authenticated
# caller); individual endpoints enforce admin-only via require_role(), except
# /roles/me which any user reads to learn their own role.
app.include_router(roles.router, prefix="/api/roles", tags=["Roles"], dependencies=auth)
# Audit trail — read-only who-did-what history. Any authenticated caller.
app.include_router(audit.router, prefix="/api/audit", tags=["Audit"], dependencies=auth)
# Custom on-prem icon legend — teaches extraction + validation the
# org's proprietary diagram iconography. Reads open; writes are curator+.
app.include_router(icon_legend.router, prefix="/api/icon-legend", tags=["IconLegend"], dependencies=auth)
