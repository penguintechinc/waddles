FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS builder
WORKDIR /app

# Copy requirements
COPY services/core-identity/requirements.txt .

# Install dependencies with no cache
RUN pip install --no-cache-dir -r requirements.txt

# Copy and install flask_core from shared libs
COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core

# Copy application code
COPY services/core-identity/app.py .
COPY core/identity_core_module/ ./identity_core_module/
COPY core/security_core_module/ ./security_core_module/
COPY core/credential_manager_module/ ./credential_manager_module/


FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
WORKDIR /app

# Copy installed Python packages and binaries from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code from builder
COPY --from=builder /app /app

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
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

# Expose port
EXPOSE 8050

# Health check
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import socket; s = socket.socket(); s.connect(('localhost', 8050)); s.close()" || exit 1

# Run Quart app via Hypercorn
CMD ["python3", "-m", "hypercorn", "app:app", "--bind", "0.0.0.0:8050"]
