FROM python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

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

USER appuser

CMD ["python", "-m", "quart", "--app", "app:app", "run", "--host", "0.0.0.0", "--port", "8095"]
