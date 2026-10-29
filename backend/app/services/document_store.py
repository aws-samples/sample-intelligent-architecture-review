# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S3 document storage service."""

import json
import logging
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError

from app.config import settings

logger = logging.getLogger(__name__)


class DocumentStore:
    """S3-based document storage for PDFs."""

    def __init__(self):
        """Initialize S3 client."""
        self._client = None
        self._available = True

    @property
    def client(self):
        """Lazy-initialize S3 client."""
        if self._client is None:
            try:
                # Force SigV4 + virtual-hosted addressing
                # so generate_presigned_url emits the modern signature
                # format. Boto3's default for s3 is "s3" (SigV2-style
                # query string with AWSAccessKeyId/Signature/Expires)
                # which fresh-account buckets reject as 400 Bad Request.
                self._client = boto3.client(
                    "s3",
                    region_name=settings.aws_region,
                    config=Config(
                        signature_version="s3v4",
                        s3={"addressing_style": "virtual"},
                    ),
                )
                # Verify bucket exists
                self._client.head_bucket(Bucket=settings.s3_documents_bucket)
                logger.info("S3 client initialized successfully")
            except NoCredentialsError:
                logger.warning("AWS credentials not found. S3 operations will be mocked.")
                self._available = False
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                logger.warning(
                    f"S3 bucket '{settings.s3_documents_bucket}' not accessible ({error_code}). "
                    "S3 operations will be mocked."
                )
                self._client = None
                self._available = False
            except Exception as e:
                logger.warning(f"Failed to initialize S3 client: {e}. S3 operations will be mocked.")
                self._client = None
                self._available = False
        return self._client

    def upload_document(self, file_bytes: bytes, s3_key: str, content_type: str = "application/pdf") -> str:
        """
        Upload a document to S3.

        Args:
            file_bytes: File content as bytes
            s3_key: S3 object key
            content_type: MIME type

        Returns:
            S3 URI of the uploaded file
        """
        if not self._available or self.client is None:
            logger.info(f"Mock upload: {s3_key} ({len(file_bytes)} bytes)")
            return f"s3://{settings.s3_documents_bucket}/{s3_key}"

        try:
            self.client.put_object(
                Bucket=settings.s3_documents_bucket,
                Key=s3_key,
                Body=file_bytes,
                ContentType=content_type,
            )
            uri = f"s3://{settings.s3_documents_bucket}/{s3_key}"
            logger.info(f"Uploaded document to {uri}")
            return uri
        except ClientError as e:
            logger.error(f"Failed to upload to S3: {e}")
            raise RuntimeError(f"S3 upload failed: {e}") from e

    def presigned_get_url(self, s3_key: str, expires: int = 300) -> str:
        """Generate a presigned GET URL for the given S3 key.

        Used to hand the .pptx to the pptx-converter
        sidecar without ever streaming bytes through the API ↔
        sidecar HTTP boundary. The URL is short-lived (default 5min)
        because conversions complete in seconds.
        """
        if not self._available or self.client is None:
            return f"http://mock-s3/{s3_key}"
        return self.client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": settings.s3_documents_bucket, "Key": s3_key},
            ExpiresIn=expires,
        )

    def presigned_put_url(
        self,
        s3_key: str,
        expires: int = 300,
        content_type: str = "application/pdf",
    ) -> str:
        """Generate a presigned PUT URL with a fixed Content-Type.

        Used as the converter sidecar's destination so
        the converted .pdf lands in the documents bucket without
        needing AWS credentials in the sidecar container.
        """
        if not self._available or self.client is None:
            return f"http://mock-s3/{s3_key}"
        return self.client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": settings.s3_documents_bucket,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=expires,
        )

    def download_document(self, s3_key: str) -> Optional[bytes]:
        """
        Download a document from S3.

        Args:
            s3_key: S3 object key

        Returns:
            File content as bytes, or None if not found
        """
        if not self._available or self.client is None:
            logger.info(f"Mock download: {s3_key}")
            return None

        try:
            response = self.client.get_object(
                Bucket=settings.s3_documents_bucket,
                Key=s3_key,
            )
            return response["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                logger.warning(f"Document not found: {s3_key}")
                return None
            logger.error(f"Failed to download from S3: {e}")
            raise RuntimeError(f"S3 download failed: {e}") from e

    def list_documents(self, prefix: str = "documents/") -> list[dict]:
        """
        List documents in S3 bucket.

        Args:
            prefix: S3 key prefix to filter

        Returns:
            List of document metadata dicts
        """
        if not self._available or self.client is None:
            logger.info("Mock list documents")
            return []

        try:
            response = self.client.list_objects_v2(
                Bucket=settings.s3_documents_bucket,
                Prefix=prefix,
            )

            documents = []
            for obj in response.get("Contents", []):
                documents.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                })
            return documents
        except ClientError as e:
            logger.error(f"Failed to list S3 objects: {e}")
            raise RuntimeError(f"S3 list failed: {e}") from e

    def delete_document(self, s3_key: str) -> bool:
        """
        Delete a document from S3.

        Args:
            s3_key: S3 object key

        Returns:
            True if deleted successfully
        """
        if not self._available or self.client is None:
            logger.info(f"Mock delete: {s3_key}")
            return True

        try:
            self.client.delete_object(
                Bucket=settings.s3_documents_bucket,
                Key=s3_key,
            )
            logger.info(f"Deleted document: {s3_key}")
            return True
        except ClientError as e:
            logger.error(f"S3 object removal failed: {e}")
            return False

    def save_parsed_content(self, document_id: str, sections: list, components: list) -> bool:
        """
        Save parsed content to S3 for persistence across restarts.

        Args:
            document_id: Document identifier
            sections: List of ParsedSection objects
            components: List of ExtractedComponent objects

        Returns:
            True if saved successfully
        """
        if not self._available or self.client is None:
            logger.info(f"Mock save parsed content for {document_id}")
            return True

        try:
            content = {
                "sections": [s.model_dump() if hasattr(s, 'model_dump') else s for s in sections],
                "components": [c.model_dump() if hasattr(c, 'model_dump') else c for c in components],
            }
            key = f"documents/{document_id}/parsed_content.json"
            self.client.put_object(
                Bucket=settings.s3_documents_bucket,
                Key=key,
                Body=json.dumps(content),
                ContentType="application/json",
            )
            logger.info(f"Saved parsed content for {document_id} to S3")
            return True
        except ClientError as e:
            logger.error(f"Failed to save parsed content to S3: {e}")
            return False

    def load_parsed_content(self, document_id: str) -> Optional[dict]:
        """
        Load parsed content from S3.

        Args:
            document_id: Document identifier

        Returns:
            Dict with 'sections' and 'components' keys, or None if not found
        """
        if not self._available or self.client is None:
            logger.info(f"Mock load parsed content for {document_id}")
            return None

        try:
            key = f"documents/{document_id}/parsed_content.json"
            response = self.client.get_object(
                Bucket=settings.s3_documents_bucket,
                Key=key,
            )
            content = json.loads(response["Body"].read())
            logger.info(f"Loaded parsed content for {document_id} from S3")
            return content
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                logger.debug(f"No parsed content found for {document_id}")
                return None
            logger.error(f"Failed to load parsed content from S3: {e}")
            return None


# Singleton instance
document_store = DocumentStore()
