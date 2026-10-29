FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# apt-get upgrade patches OS packages already in the base image even
# when the python:3.11-slim tag itself lags a CVE fix. Combined with
# `docker build --pull` in deploy.sh, this keeps both the base layer
# and its packages current on every deploy (SEV prevention).
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Drop root. The container only ever needs to read its own code and write
# temporary files, so there is no reason for the API — which parses
# attacker-supplied PDFs and PowerPoint files with native libraries — to run as
# uid 0. The pptx sidecar has always run as a non-root user; the API did not,
# which left the more exposed of the two services as the more privileged.
#
# Ownership is set on /app so the app can still read its code, and on /tmp
# because PyMuPDF rasterization and the PowerPoint path write scratch files
# there. Created with a fixed uid so file ownership is stable across rebuilds.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app \
    && chmod 1777 /tmp
USER appuser

EXPOSE 8000

# Correlate every log line with its active trace/span so a CloudWatch Logs
# Insights query can pivot from an error straight to the trace that produced it.
ENV OTEL_PYTHON_LOG_CORRELATION=true

# Run under the ADOT auto-instrumentation wrapper so FastAPI requests, boto3
# calls (Bedrock, DynamoDB, S3), and HTTP clients are traced without touching
# application code. This is what gives the agentic pipeline — extraction,
# validation, and query — end-to-end spans; infrastructure/modules/tracing then
# routes them into CloudWatch `aws/spans` where they are queryable.
#
# Note: exporting requires the task role's X-Ray permissions
# (modules/ecs: aws_iam_policy.tracing). Without them the process still serves
# traffic and only span export fails, so a permissions regression shows up as
# missing traces rather than an outage.
CMD ["opentelemetry-instrument", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
