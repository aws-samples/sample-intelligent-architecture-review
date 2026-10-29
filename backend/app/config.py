# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Application configuration using Pydantic Settings."""

import sys
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings
from app import bedrock_models


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Project identity. Terraform injects PROJECT_NAME into the ECS task;
    # used for the health-check service label so it isn't hardcoded.
    project_name: str = "compliance-validation"

    # AWS Configuration
    aws_region: str = "us-east-1"
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None

    # S3 Configuration. Empty default so a deploy without
    # S3_DOCUMENTS_BUCKET env set fails the validator below loudly
    # rather than silently pointing at a bucket that doesn't exist
    # in the deploy account. Terraform wires this from
    # module.s3.documents_bucket_id at deploy time.
    s3_documents_bucket: str = ""

    # DynamoDB Configuration. Same rationale — empty default so a
    # cross-account deploy without DYNAMODB_TABLE_PREFIX set fails
    # the validator instead of silently using the wrong table name.
    dynamodb_table_prefix: str = ""

    # Bedrock Configuration
    bedrock_model_id: str = bedrock_models.SONNET
    bedrock_max_tokens: int = 4096

    # Bedrock Knowledge Base Configuration
    knowledge_base_id: Optional[str] = None
    data_source_id: Optional[str] = None

    # Teams Webhook
    teams_webhook_url: Optional[str] = None

    # Application Settings
    log_level: str = "INFO"
    cors_origins: str = "*"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level is valid."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"Invalid log level: {v}. Must be one of {valid_levels}")
        return v_upper

    @field_validator("bedrock_max_tokens")
    @classmethod
    def validate_max_tokens(cls, v: int) -> int:
        """Validate max tokens is reasonable."""
        if v < 1 or v > 200000:
            raise ValueError(f"bedrock_max_tokens must be between 1 and 200000, got {v}")
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        """Parse CORS origins into a list."""
        if self.cors_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",")]

    @property
    def documents_table(self) -> str:
        return f"{self.dynamodb_table_prefix}-documents"

    @property
    def validations_table(self) -> str:
        return f"{self.dynamodb_table_prefix}-validations"

    @property
    def standards_table(self) -> str:
        return f"{self.dynamodb_table_prefix}-standards"

    @property
    def chat_jobs_table(self) -> str:
        return f"{self.dynamodb_table_prefix}-chat-jobs"

    @property
    def references_table(self) -> str:
        return f"{self.dynamodb_table_prefix}-references"


    def validate_startup_config(self) -> None:
        """Validate critical configuration on startup and fail fast with clear messages."""
        errors = []
        warnings = []

        # Check AWS region
        valid_regions = [
            "us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1",
            "us-east-2", "eu-central-1", "ap-southeast-1"
        ]
        if self.aws_region not in valid_regions:
            warnings.append(
                f"AWS region '{self.aws_region}' may not support all Bedrock models. "
                f"Recommended regions: {', '.join(valid_regions)}"
            )

        # Check S3 bucket name format
        if not self.s3_documents_bucket:
            errors.append("S3_DOCUMENTS_BUCKET is required but not set")
        elif len(self.s3_documents_bucket) < 3 or len(self.s3_documents_bucket) > 63:
            errors.append(
                f"S3 bucket name must be 3-63 characters, got {len(self.s3_documents_bucket)}"
            )

        # Check DynamoDB table prefix
        if not self.dynamodb_table_prefix:
            errors.append("DYNAMODB_TABLE_PREFIX is required but not set")

        # Check Bedrock model ID format
        if not self.bedrock_model_id:
            errors.append("BEDROCK_MODEL_ID is required but not set")
        elif not (
            self.bedrock_model_id.startswith("anthropic.") or
            self.bedrock_model_id.startswith("us.anthropic.") or
            self.bedrock_model_id.startswith("eu.anthropic.")
        ):
            warnings.append(
                f"BEDROCK_MODEL_ID '{self.bedrock_model_id}' does not match expected format. "
                "Expected: 'anthropic.*' or 'us.anthropic.*' or 'eu.anthropic.*'"
            )

        # Check Knowledge Base configuration (optional but recommended)
        if not self.knowledge_base_id:
            warnings.append(
                "KNOWLEDGE_BASE_ID not set. Query functionality will be limited."
            )
        if not self.data_source_id:
            warnings.append(
                "DATA_SOURCE_ID not set. Knowledge Base sync will not work."
            )

        # Print warnings
        if warnings:
            print("\n⚠️  Configuration Warnings:")
            for warning in warnings:
                print(f"  - {warning}")

        # Fail fast on errors
        if errors:
            print("\n❌ Configuration Errors:")
            for error in errors:
                print(f"  - {error}")
            print("\nPlease fix the configuration errors above and restart the application.")
            sys.exit(1)

        print("✅ Configuration validation passed")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

# Validate configuration on import
settings.validate_startup_config()
