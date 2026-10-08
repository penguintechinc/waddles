FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Shared library -- JWT verification + require_scope for the
# /api/v1/credentials/* routes (SECURITY C6 fix: these were unauthenticated).
COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core

# Copy and install Python dependencies
COPY core/credential_manager_module/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY core/credential_manager_module/ .

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

# Set proper permissions
RUN chown -R appuser:appuser /app

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8095/health || exit 1

EXPOSE 8095

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

CMD ["python", "-m", "quart", "--app", "app:app", "run", "--host", "0.0.0.0", "--port", "8095"]
