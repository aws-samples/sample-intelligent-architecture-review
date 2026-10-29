# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared API dependencies for dependency injection."""

from app.services.bedrock import bedrock_client, BedrockClient
from app.services.document_store import document_store, DocumentStore
from app.services.validation_store import validation_store, ValidationStore
from app.services.vector_store import vector_store, VectorStore


def get_bedrock() -> BedrockClient:
    """Get Bedrock client instance."""
    return bedrock_client


def get_document_store() -> DocumentStore:
    """Get document store instance."""
    return document_store


def get_validation_store() -> ValidationStore:
    """Get validation store instance."""
    return validation_store


def get_vector_store() -> VectorStore:
    """Get vector store instance."""
    return vector_store
