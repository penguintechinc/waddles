# Multi-stage build for interactive-productivity service
# Combines calendar, memories, and translate modules
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS builder

WORKDIR /app

# Copy requirements and install dependencies
COPY services/interactive-productivity/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy and install flask_core from shared libs
COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core

# Runtime stage
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy config and combined app
COPY services/interactive-productivity/config.py .
COPY services/interactive-productivity/app.py .

# Copy all three module directories
COPY action/interactive/calendar_interaction_module ./calendar_interaction_module
COPY action/interactive/memories_interaction_module ./memories_interaction_module
COPY action/interactive/translate_interaction_module ./translate_interaction_module

# Expose ports
# Port 8030: REST API (calendar, memories, translate)
# Port 50033: gRPC (translate module)
EXPOSE 8030 50033

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -m appuser && chown -R appuser:appuser /app

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

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python3 -c "import http.client; conn = http.client.HTTPConnection('localhost', 8030); conn.request('GET', '/healthz'); conn.getresponse().status == 200 or exit(1)" || exit 1

CMD ["python3", "app.py"]
