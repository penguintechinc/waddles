# Multi-stage build for interactive-gaming service
# Combines 4 gaming interaction modules into one Quart app on port 8104

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS builder

WORKDIR /app

# Copy requirements and install dependencies
COPY services/interactive-gaming/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy and install flask_core from shared libs
COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core

# Copy config and app
COPY services/interactive-gaming/config.py .
COPY services/interactive-gaming/app.py .

# Copy all 4 module directories
COPY action/interactive/lfg_interaction_module/ ./lfg_interaction_module/
COPY action/interactive/inventory_interaction_module/ ./inventory_interaction_module/
COPY action/interactive/server_manager_interaction_module/ ./server_manager_interaction_module/
COPY action/interactive/server_status_interaction_module/ ./server_status_interaction_module/

# Runtime stage
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

WORKDIR /app

# Copy installed dependencies from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy config and app
COPY services/interactive-gaming/config.py .
COPY services/interactive-gaming/app.py .

# Copy all 4 module directories
COPY action/interactive/lfg_interaction_module/ ./lfg_interaction_module/
COPY action/interactive/inventory_interaction_module/ ./inventory_interaction_module/
COPY action/interactive/server_manager_interaction_module/ ./server_manager_interaction_module/
COPY action/interactive/server_status_interaction_module/ ./server_status_interaction_module/

# Health check using native Python (no curl)
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8104/healthz').read()" || exit 1

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

EXPOSE 8104

CMD ["python3", "app.py"]
