# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Bedrock retry logic."""

import pytest
from botocore.exceptions import ClientError
from unittest.mock import MagicMock, patch

from app.services.bedrock import _is_retryable_error, BedrockClient


class TestBedrockRetryLogic:
    """Test Bedrock retry logic and error handling."""

    @pytest.mark.unit
    def test_throttling_exception_is_retryable(self):
        """Test that ThrottlingException is marked as retryable."""
        error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "InvokeModel",
        )
        assert _is_retryable_error(error) is True

    @pytest.mark.unit
    def test_too_many_requests_is_retryable(self):
        """Test that TooManyRequestsException is retryable."""
        error = ClientError(
            {"Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"}},
            "InvokeModel",
        )
        assert _is_retryable_error(error) is True

    @pytest.mark.unit
    def test_service_unavailable_is_retryable(self):
        """Test that ServiceUnavailableException is retryable."""
        error = ClientError(
            {"Error": {"Code": "ServiceUnavailableException", "Message": "Service down"}},
            "InvokeModel",
        )
        assert _is_retryable_error(error) is True

    @pytest.mark.unit
    def test_internal_server_error_is_retryable(self):
        """Test that InternalServerError is retryable."""
        error = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Server error"}},
            "InvokeModel",
        )
        assert _is_retryable_error(error) is True

    @pytest.mark.unit
    def test_validation_exception_not_retryable(self):
        """Test that ValidationException is NOT retryable."""
        error = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Invalid input"}},
            "InvokeModel",
        )
        assert _is_retryable_error(error) is False

    @pytest.mark.unit
    def test_access_denied_not_retryable(self):
        """Test that AccessDeniedException is NOT retryable."""
        error = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "No access"}},
            "InvokeModel",
        )
        assert _is_retryable_error(error) is False

    @pytest.mark.unit
    def test_model_not_found_not_retryable(self):
        """Test that ResourceNotFoundException is NOT retryable."""
        error = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Model not found"}},
            "InvokeModel",
        )
        assert _is_retryable_error(error) is False

    @pytest.mark.unit
    def test_non_client_error_not_retryable(self):
        """Test that non-ClientError exceptions are not retryable."""
        error = ValueError("Some other error")
        assert _is_retryable_error(error) is False

    @pytest.mark.unit
    def test_bedrock_client_lazy_initialization(self):
        """Test that Bedrock client is lazily initialized."""
        with patch("app.services.bedrock.boto3.client") as mock_client:
            bedrock = BedrockClient()
            # Client should not be initialized yet
            assert bedrock._client is None

            # Access client property to trigger initialization
            _ = bedrock.client

            # Now client should be initialized
            mock_client.assert_called_once()
