# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudWatch custom metrics service."""

import logging
from datetime import datetime
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)


class MetricsService:
    """Service for emitting custom CloudWatch metrics."""

    def __init__(self):
        """Initialize CloudWatch client."""
        self._client = None
        # Derived from the project name so the metrics namespace isn't
        # hardcoded to one project. Must match the terraform monitoring
        # module's local.custom_namespace ("${project_name}-metrics").
        self.namespace = f"{settings.project_name}-metrics"

    @property
    def client(self):
        """Lazy-initialize CloudWatch client."""
        if self._client is None:
            try:
                self._client = boto3.client("cloudwatch", region_name=settings.aws_region)
                logger.info("CloudWatch metrics client initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize CloudWatch client: {e}")
                self._client = None
        return self._client

    def emit_metric(
        self,
        metric_name: str,
        value: float = 1.0,
        unit: str = "Count",
        dimensions: Optional[dict] = None,
    ) -> None:
        """
        Emit a custom metric to CloudWatch.

        Args:
            metric_name: Name of the metric
            value: Metric value
            unit: Unit of measurement (Count, Seconds, etc.)
            dimensions: Optional dimensions for filtering
        """
        if not self.client:
            logger.debug(f"CloudWatch not available, skipping metric: {metric_name}")
            return

        try:
            metric_data = {
                "MetricName": metric_name,
                "Value": value,
                "Unit": unit,
                "Timestamp": datetime.utcnow(),
            }

            if dimensions:
                metric_data["Dimensions"] = [
                    {"Name": k, "Value": v} for k, v in dimensions.items()
                ]

            self.client.put_metric_data(
                Namespace=self.namespace, MetricData=[metric_data]
            )
            logger.debug(f"Emitted metric: {metric_name}={value}")

        except ClientError as e:
            logger.error(f"Failed to emit metric {metric_name}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error emitting metric {metric_name}: {e}")

    # Validation metrics — only validation_cost() is wired into the code
    # today (called once per validation completion). The other emitter
    # methods (validation_started/completed/failed, bedrock_*, kb_query,
    # extraction_*, document_*) were removed as dead code: zero callers,
    # and the alarms/dashboard that consumed their metrics were removed
    # alongside them.
    def validation_cost(
        self,
        document_id: str,
        validation_id: str,
        calls: int,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
        chunks: int,
        duration_seconds: float,
    ):
        """Record per-validation Bedrock cost telemetry.

        Emitted once per validation completion. The DocumentId
        dimension lets us spot expensive offenders; aggregate metrics
        (no dimension) drive the cost-over-time dashboard.
        """
        # Aggregate (no dimension) — power the cost-over-time chart.
        self.emit_metric("ValidationBedrockCalls", value=calls, unit="Count")
        self.emit_metric("ValidationInputTokens", value=input_tokens, unit="Count")
        self.emit_metric("ValidationOutputTokens", value=output_tokens, unit="Count")
        self.emit_metric("ValidationEstimatedCostUSD", value=estimated_cost_usd, unit="None")
        self.emit_metric("ValidationChunks", value=chunks, unit="Count")
        self.emit_metric("ValidationDurationSeconds", value=duration_seconds, unit="Seconds")
        # Per-document — surface the high-spend docs in alarms.
        self.emit_metric(
            "ValidationEstimatedCostUSD",
            value=estimated_cost_usd,
            unit="None",
            dimensions={"DocumentId": document_id},
        )


# Singleton instance
metrics_service = MetricsService()
