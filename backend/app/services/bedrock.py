# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Amazon Bedrock client wrapper for Claude model invocations."""

import json
import logging
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from app.config import settings

logger = logging.getLogger(__name__)


# AWS exceptions that should trigger retry
RETRYABLE_EXCEPTIONS = (
    ClientError,  # Will filter by error code in retry logic
)


def _is_retryable_error(exception):
    """Check if a ClientError should trigger retry."""
    if not isinstance(exception, ClientError):
        return False

    error_code = exception.response.get("Error", {}).get("Code", "")

    # Retry on throttling and transient errors
    retryable_codes = {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "InternalServerError",
        "RequestTimeout",
        "ServiceQuotaExceededException",
    }

    return error_code in retryable_codes


class BedrockClient:
    """Wrapper around Amazon Bedrock runtime for Claude model invocations."""

    def __init__(self):
        """Initialize Bedrock client."""
        self._client = None

    @property
    def client(self):
        """Lazy-initialize the Bedrock runtime client."""
        if self._client is None:
            try:
                self._client = boto3.client(
                    "bedrock-runtime",
                    region_name=settings.aws_region,
                )
                logger.info("Bedrock client initialized successfully")
            except NoCredentialsError:
                raise RuntimeError(
                    "AWS credentials not found. Cannot connect to Bedrock. "
                    "Please set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables."
                )
            except Exception as e:
                raise RuntimeError(
                    f"Failed to initialize Bedrock client: {e}. "
                    "Ensure AWS credentials and region are configured correctly."
                )
        return self._client

    @retry(
        retry=retry_if_exception_type(ClientError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        retry_error_callback=lambda retry_state: _is_retryable_error(retry_state.outcome.exception()),
    )
    def invoke_model(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: float = 0.1,
    ) -> str:
        """
        Invoke Claude model with a text prompt.

        Automatically retries on throttling and transient errors with exponential backoff.

        Args:
            prompt: User message/prompt
            system_prompt: Optional system prompt for context
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (0-1)

        Returns:
            Model response text
        """

        max_tokens = max_tokens or settings.bedrock_max_tokens

        messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }

        if system_prompt:
            body["system"] = system_prompt

        try:
            response = self.client.invoke_model(
                modelId=settings.bedrock_model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )

            response_body = json.loads(response["body"].read())
            return response_body["content"][0]["text"]

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            # Check if retryable
            if _is_retryable_error(e):
                logger.warning(f"Retryable Bedrock error ({error_code}), will retry")
                raise  # Let tenacity handle retry

            logger.error(f"Non-retryable Bedrock API error ({error_code}): {e}")
            raise RuntimeError(f"Bedrock invocation failed: {error_code}") from e
        except Exception as e:
            logger.error(f"Unexpected error invoking Bedrock: {e}")
            raise RuntimeError(f"Bedrock invocation failed: {e}") from e

    @retry(
        retry=retry_if_exception_type(ClientError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        retry_error_callback=lambda retry_state: _is_retryable_error(retry_state.outcome.exception()),
    )
    def invoke_with_vision(
        self,
        prompt: str,
        image_base64: str,
        system_prompt: Optional[str] = None,
        media_type: str = "image/png",
        max_tokens: Optional[int] = None,
        temperature: float = 0.1,
    ) -> str:
        """
        Invoke Claude model with an image (vision capabilities).

        Automatically retries on throttling and transient errors with exponential backoff.

        Args:
            prompt: Text prompt describing what to analyze
            image_base64: Base64-encoded image data
            system_prompt: Optional system prompt
            media_type: Image MIME type (image/png, image/jpeg, etc.)
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature

        Returns:
            Model response text
        """

        max_tokens = max_tokens or settings.bedrock_max_tokens

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_base64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }

        if system_prompt:
            body["system"] = system_prompt

        try:
            response = self.client.invoke_model(
                modelId=settings.bedrock_model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )

            response_body = json.loads(response["body"].read())
            return response_body["content"][0]["text"]

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            # Check if retryable
            if _is_retryable_error(e):
                logger.warning(f"Retryable Bedrock Vision error ({error_code}), will retry")
                raise  # Let tenacity handle retry

            logger.error(f"Non-retryable Bedrock Vision API error ({error_code}): {e}")
            raise RuntimeError(f"Bedrock vision invocation failed: {error_code}") from e
        except Exception as e:
            logger.error(f"Unexpected error invoking Bedrock vision: {e}")
            raise RuntimeError(f"Bedrock vision invocation failed: {e}") from e


# Singleton instance
bedrock_client = BedrockClient()
