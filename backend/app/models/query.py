# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Query data models."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SourceReference(BaseModel):
    """Reference to a source section used in answering a query."""
    section_title: str = Field(description="Title of the referenced section")
    page_number: int = Field(description="Page number")
    relevance_score: float = Field(default=0.0, description="Relevance score 0-1")
    excerpt: str = Field(default="", description="Relevant excerpt from the section")


class QueryRequest(BaseModel):
    """Request for a natural language query."""
    question: str = Field(description="Natural language question")
    document_id: Optional[str] = Field(default=None, description="Document to query against (optional - searches all if omitted)")
    include_sources: bool = Field(default=True, description="Include source references")


class QueryResponse(BaseModel):
    """Response to a natural language query."""
    query_id: str = Field(description="Unique query identifier")
    document_id: str = Field(description="Document queried")
    question: str = Field(description="Original question")
    answer: str = Field(description="AI-generated answer")
    sources: list[SourceReference] = Field(default_factory=list)
    confidence: float = Field(default=0.0, description="Confidence score 0-1")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    processing_time_ms: Optional[int] = Field(default=None, description="Processing time in milliseconds")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
