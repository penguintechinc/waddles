# Multi-stage build for combined serverless action module
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY services/action-serverless/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ============================================================================
# Runtime stage
# ============================================================================
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

WORKDIR /app

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY services/action-serverless/app.py .
COPY action/pushing/lambda_action_module/ ./lambda_action_module/
COPY action/pushing/openwhisk_action_module/ ./openwhisk_action_module/
COPY action/pushing/gcp_functions_action_module/ ./gcp_functions_action_module/

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
# Remove base image's own bundled pip + ensurepip wheel: pip vendors an
# outdated msgpack (GHSA-6v7p-g79w-8964) and pkg_resources/setuptools
# (CVE-2025-47273) that the upstream python:3.13-slim-bookworm rebuild
# hasn't picked up yet. Neither pip nor its vendor tree is imported by
# any service at runtime (installs happen at build time via uv/pip
# above), so removing them is a real fix, not a suppression.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.13/ensurepip

USER appuser

# Health check - use native Python HTTP module (no curl required)
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python3 -c "import http.client; conn = http.client.HTTPConnection('localhost', 8103); conn.request('GET', '/health'); exit(0 if conn.getresponse().status == 200 else 1)"

# Expose REST port (8103) and gRPC ports (50051, 50052, 50053)
EXPOSE 8103 50051 50052 50053

# Run application
CMD ["python3", "app.py"]
