# Multi-stage build for Interactive Media Service
# Combines clip, spotify, and youtube_music interaction modules

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS builder
WORKDIR /app

# Copy requirements and install dependencies
COPY services/interactive-media/requirements.txt .
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

# Copy config and application code
COPY services/interactive-media/config.py .
COPY services/interactive-media/app.py .

# Copy module directories
COPY action/interactive/clip_interaction_module ./clip_interaction_module
COPY action/interactive/spotify_interaction_module ./spotify_interaction_module
COPY action/interactive/youtube_music_interaction_module ./youtube_music_interaction_module

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV MODULE_NAME=interactive_media_service
ENV MODULE_VERSION=1.0.0
ENV MODULE_PORT=8105

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
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import asyncio; import sys; \
    async def check(): \
      import httpx; \
      try: \
        async with httpx.AsyncClient() as client: \
          r = await client.get('http://localhost:8105/healthz', timeout=2); \
          sys.exit(0 if r.status_code == 200 else 1); \
      except: sys.exit(1); \
    asyncio.run(check())" || exit 1

# Run the application
CMD ["python3", "app.py"]

# Expose port
EXPOSE 8105
