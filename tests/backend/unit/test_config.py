# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for configuration validation."""

import sys
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.config import Settings


class TestConfigValidation:
    """Test configuration validation."""

    @pytest.mark.unit
    def test_default_config_loads(self):
        """Test that default configuration loads successfully."""
        settings = Settings()
        assert settings.aws_region is not None
        assert settings.log_level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    @pytest.mark.unit
    def test_log_level_validation(self):
        """Test log level validation."""
        # Valid log level
        settings = Settings(log_level="INFO")
        assert settings.log_level == "INFO"

        # Invalid log level should raise error
        with pytest.raises(ValidationError):
            Settings(log_level="INVALID")

    @pytest.mark.unit
    def test_max_tokens_validation(self):
        """Test max tokens validation."""
        # Valid max tokens
        settings = Settings(bedrock_max_tokens=4096)
        assert settings.bedrock_max_tokens == 4096

        # Too low
        with pytest.raises(ValidationError):
            Settings(bedrock_max_tokens=0)

        # Too high
        with pytest.raises(ValidationError):
            Settings(bedrock_max_tokens=300000)

    @pytest.mark.unit
    def test_cors_origin_list_parsing(self):
        """Test CORS origin list parsing."""
        # Single wildcard
        settings = Settings(cors_origins="*")
        assert settings.cors_origin_list == ["*"]

        # Multiple origins
        settings = Settings(cors_origins="http://localhost:3000,https://example.com")
        assert len(settings.cors_origin_list) == 2
        assert "http://localhost:3000" in settings.cors_origin_list

    @pytest.mark.unit
    def test_dynamodb_table_names(self):
        """Test DynamoDB table name generation."""
        settings = Settings(dynamodb_table_prefix="test-prefix")
        assert settings.documents_table == "test-prefix-documents"
        assert settings.validations_table == "test-prefix-validations"
        assert settings.standards_table == "test-prefix-standards"

    @pytest.mark.unit
    def test_startup_validation_passes(self):
        """Test startup validation with valid config."""
        settings = Settings(
            s3_documents_bucket="valid-bucket-name",
            dynamodb_table_prefix="test-ha",
            bedrock_model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
        )
        # Should not raise
        settings.validate_startup_config()

    @pytest.mark.unit
    def test_startup_validation_fails_empty_bucket(self):
        """Test startup validation fails with empty bucket name."""
        settings = Settings(s3_documents_bucket="")

        with pytest.raises(SystemExit):
            settings.validate_startup_config()

    @pytest.mark.unit
    def test_startup_validation_warns_on_invalid_model(self):
        """Test startup validation warns on suspicious model ID."""
        settings = Settings(
            bedrock_model_id="invalid-model-format",
            s3_documents_bucket="valid-bucket",
        )
        # Should warn but not fail
        settings.validate_startup_config()
