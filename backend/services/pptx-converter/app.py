# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""pptx-converter sidecar — LibreOffice headless behind a tiny HTTP API.

The API container POSTs presigned URLs (GET source, PUT target) and the
sidecar:

  1. HEADs the source URL to read Content-Length; rejects > max_bytes early.
  2. GETs the source bytes to a per-request tmpfs working directory.
  3. Runs `soffice --headless --convert-to pdf:impress_pdf_Export:...`
     with a 60s subprocess timeout. On timeout, SIGKILL.
  4. PUTs the resulting .pdf to the target URL.
  5. Cleans up the tmpfs working dir.

Container has no AWS credentials, no IAM role, no boto3. The only
way it touches S3 is via the presigned URLs supplied per request,
which scopes the blast radius — even if compromised, the sidecar
can only access the specific objects supplied to it.

Subprocess timeout, fresh tmpfs per request, payload size cap, and
non-root user are all defense-in-depth layers.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="pptx-converter sidecar", version="1.0.0")


# SSRF guard. The sidecar fetches/PUTs whatever URLs it's handed; without a
# check it could be pointed at internal endpoints (e.g. the instance metadata
# service at 169.254.169.254) or other hosts. The backend only ever sends S3
# presigned URLs, so require HTTPS + an S3 host. Host suffix is overridable
# for non-standard partitions/endpoints. Redirects are disabled by callers so
# a 302 can't bounce past this check to an internal host.
_ALLOWED_URL_HOST_SUFFIX = os.getenv("ALLOWED_URL_HOST_SUFFIX", ".amazonaws.com")


def _validate_s3_url(url: str, field: str) -> None:
    """Reject any URL that isn't an https S3 (amazonaws.com) endpoint."""
    try:
        p = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field} is not a valid URL")
    if p.scheme != "https":
        raise HTTPException(status_code=400, detail=f"{field} must be https")
    host = (p.hostname or "").lower()
    if not host or not host.endswith(_ALLOWED_URL_HOST_SUFFIX):
        raise HTTPException(
            status_code=400,
            detail=f"{field} host not allowed (must be an S3 presigned URL)",
        )


# Subprocess timeout for soffice, fixed at 60s — long enough for legitimate
# decks, short enough to recover from hangs.
SOFFICE_TIMEOUT_S = int(os.getenv("SOFFICE_TIMEOUT_S", "60"))

# Default max_bytes if the caller doesn't supply one. Defends against a
# misconfigured caller; the calling API also enforces the cap on its side
# (defense in depth).
DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100MB

# Where to write per-request working dirs. tmpfs preferred (kept on
# RAM disk, automatically wiped on container restart).
TMP_ROOT = os.getenv("CONVERTER_TMP_ROOT", "/tmp")


class ConvertRequest(BaseModel):
    source_url: str = Field(description="Presigned GET URL on the .pptx in S3")
    target_url: str = Field(description="Presigned PUT URL where the .pdf will land")
    max_bytes: int = Field(
        default=DEFAULT_MAX_BYTES,
        description="Reject downloads whose Content-Length exceeds this cap",
    )


class ConvertResponse(BaseModel):
    page_count: int
    duration_ms: int


class ErrorBody(BaseModel):
    detail: str


@app.get("/health")
def health() -> dict:
    """Returns 200 + libreoffice version. Used as ECS task health
    check and for the API client to verify reachability."""
    try:
        result = subprocess.run(  # nosec B603 - static argv, no shell
            ["soffice", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = result.stdout.strip() or result.stderr.strip()
    except Exception as exc:
        logger.exception("soffice --version failed")
        raise HTTPException(
            status_code=500, detail=f"soffice not available: {exc}"
        )
    return {"status": "ok", "soffice": version}


@app.post(
    "/convert",
    response_model=ConvertResponse,
    responses={
        413: {"model": ErrorBody, "description": "Oversized input"},
        422: {"model": ErrorBody, "description": "Corrupt or password-protected"},
        502: {"model": ErrorBody, "description": "S3 upstream error"},
        504: {"model": ErrorBody, "description": "LibreOffice timeout"},
    },
)
def convert(req: ConvertRequest) -> ConvertResponse:
    # SSRF guard — both URLs must be https S3 endpoints before we touch them.
    _validate_s3_url(req.source_url, "source_url")
    _validate_s3_url(req.target_url, "target_url")

    started = time.monotonic()
    workdir = Path(tempfile.mkdtemp(prefix=f"conv-{uuid.uuid4().hex[:8]}-", dir=TMP_ROOT))
    pptx_path = workdir / "input.pptx"
    pdf_path = workdir / "input.pdf"
    try:
        # 1. Size guard via HEAD. Redirects disabled so a 302 can't bounce
        # the request past the SSRF host check to an internal endpoint.
        try:
            head = httpx.head(req.source_url, timeout=15, follow_redirects=False)
        except httpx.HTTPError as exc:
            logger.warning(f"HEAD source_url failed: {exc}")
            raise HTTPException(
                status_code=502, detail=f"could not HEAD source: {exc}"
            )
        cl = head.headers.get("Content-Length")
        if cl is not None and cl.isdigit():
            size = int(cl)
            if size > req.max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"source is {size} bytes; max_bytes={req.max_bytes}",
                )

        # 2. GET the .pptx to disk, streaming so we don't blow memory.
        try:
            with httpx.stream("GET", req.source_url, timeout=60,
                               follow_redirects=False) as resp:
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=502,
                        detail=f"source GET returned {resp.status_code}",
                    )
                bytes_received = 0
                with open(pptx_path, "wb") as f:
                    for chunk in resp.iter_bytes(64 * 1024):
                        bytes_received += len(chunk)
                        if bytes_received > req.max_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail=(
                                    f"source exceeds max_bytes={req.max_bytes} mid-stream"
                                ),
                            )
                        f.write(chunk)
        except httpx.HTTPError as exc:
            logger.warning(f"GET source_url failed: {exc}")
            raise HTTPException(
                status_code=502, detail=f"could not GET source: {exc}"
            )

        # 3. Run soffice. Force impress export options so the PDF is
        # one slide per page (no notes, no handouts) and PDF
        # producer fields don't drift between LibreOffice versions
        # in ways that would defeat caching downstream.
        #
        # Every argv element is a string literal: soffice runs with
        # cwd=workdir (a mkdtemp() dir) on the constant "input.pptx" and
        # writes "input.pdf" beside it. No request field and no computed
        # value reaches argv, and there is no shell, so there is nothing to
        # inject into. The only external input, source_url, is
        # host-validated (_validate_s3_url) and used solely for the HTTP fetch.
        try:
            result = subprocess.run(  # nosec B603 - static argv, no shell
                [
                    "soffice",
                    "--headless",
                    "--convert-to",
                    "pdf:impress_pdf_Export:"
                    '{"ExportNotes":{"type":"boolean","value":"false"},'
                    '"ExportNotesPages":{"type":"boolean","value":"false"},'
                    '"OpenBookmarkLevels":{"type":"long","value":"-1"},'
                    '"SinglePageSheets":{"type":"boolean","value":"true"}}',
                    "--outdir",
                    ".",
                    "input.pptx",
                ],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=SOFFICE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"soffice timed out after {SOFFICE_TIMEOUT_S}s on {pptx_path}"
            )
            raise HTTPException(
                status_code=504,
                detail=f"soffice exceeded {SOFFICE_TIMEOUT_S}s",
            )
        if result.returncode != 0 or not pdf_path.exists():
            # LibreOffice on bad input often exits 0 but produces no
            # PDF (yes, really). Check both.
            stderr = (result.stderr or "")[-2000:]
            stdout = (result.stdout or "")[-1000:]
            logger.warning(
                f"soffice failed rc={result.returncode}; stdout={stdout!r}; stderr={stderr!r}"
            )
            raise HTTPException(
                status_code=422,
                detail=f"conversion produced no output (rc={result.returncode})",
            )

        # 4. PUT the PDF.
        try:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            put = httpx.put(
                req.target_url,
                content=pdf_bytes,
                headers={"Content-Type": "application/pdf"},
                timeout=60,
                follow_redirects=False,
            )
            if not (200 <= put.status_code < 300):
                raise HTTPException(
                    status_code=502,
                    detail=f"target PUT returned {put.status_code}",
                )
        except httpx.HTTPError as exc:
            logger.warning(f"PUT target_url failed: {exc}")
            raise HTTPException(
                status_code=502, detail=f"could not PUT target: {exc}"
            )

        # 5. Count pages (lightweight via PyMuPDF). The caller wants
        # this so it can sanity-check against the slide count.
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            page_count = doc.page_count
            doc.close()
        except Exception as exc:
            # Producing a PDF without being able to count pages is
            # weird but not fatal — return 0 and let the caller
            # decide.
            logger.warning(f"page count failed: {exc}")
            page_count = 0

        return ConvertResponse(
            page_count=page_count,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    finally:
        # Best-effort cleanup; tmpfs gets wiped on container restart
        # anyway but tidy as we go.
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass
