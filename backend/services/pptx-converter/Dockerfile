# pptx-converter sidecar.
#
# LibreOffice headless behind a tiny FastAPI HTTP service. Runs as
# its own ECS service registered with Cloud Map (DNS:
# pptx-converter.resilience-review-dev.local:8001). The main backend
# POSTs presigned-URL conversion jobs in; the sidecar fetches the
# .pptx via the source URL, runs `soffice --convert-to pdf`, and
# PUTs the result via the target URL. Container has no AWS creds.
#
# Base: python:3.13-slim (Debian trixie), pulled from the ECR Public mirror of
# the Docker official image. Chosen for the apt-get LibreOffice install path —
# alpine doesn't ship a usable soffice headless binary.

FROM public.ecr.aws/docker/library/python:3.13-slim

# Install LibreOffice headless + curl (needed by the ECS health
# check that runs `curl -f http://localhost:8001/health`). Strip
# locales / docs to keep the image small.
# apt-get upgrade patches OS packages already in the base image even
# when the python:3.13-slim tag itself lags a CVE fix. Combined with
# `docker build --pull` in deploy.sh, this keeps both the base layer
# and its packages current on every deploy (SEV prevention).
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        libreoffice-core \
        curl \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Non-root user — defense in depth. Even if someone tricks the
# sidecar into running unexpected soffice macros, they're confined
# to a low-privileged user with no AWS creds.
RUN useradd --uid 1001 --create-home --shell /bin/bash converter \
 && chown -R converter:converter /app
USER converter

# The task runs with a read-only root filesystem. These are the only paths
# LibreOffice writes to: per-request scratch dirs in /tmp and its user profile
# under $HOME. Declaring them as volumes means the task-scoped volumes ECS
# mounts there start with this image's ownership and permissions.
VOLUME ["/tmp", "/home/converter"]

EXPOSE 8001

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
