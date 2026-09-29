# Multi-stage build for Core Community Service
# Combines 4 modules: community, workflow_core, browser_source, video_proxy
# Build from repo root: docker build -f services/core-community/Dockerfile -t core-community:latest .

# NOTE: Replace placeholder SHA256 with actual digest from:
# docker pull python:3.13-slim-bookworm && \
# docker inspect python:3.13-slim-bookworm --format='{{index .RepoDigests 0}}'

# Builder stage
FROM python:3.13-slim-bookworm@sha256:f13a6b7565175da40695e8109f64cbc4d2e65f4c9ef2e3b321c3a44fa3c06fe7 AS builder

WORKDIR /app

# Copy requirements and install dependencies
COPY services/core-community/requirements.txt .
# Pin pip itself: base image ships 26.0.1 (PYSEC-2026-2875/2876/196/3721, fixed <=26.1.2/26.2)
RUN pip install --no-cache-dir --upgrade "pip==26.2.1" && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY services/core-community/app.py .
COPY services/core-community/config.py .

# Copy all 4 module directories
COPY core/community_module ./community_module
COPY core/workflow_core_module ./workflow_core_module
COPY core/browser_source_core_module ./browser_source_core_module
COPY core/video_proxy_module ./video_proxy_module

# Copy shared libs
COPY libs ./libs

# Install flask_core from libs. requirements.txt above is compiled with
# --constraint against libs/flask_core/requirements.txt so shared deps
# (protobuf, grpcio, pydal, cryptography, pyjwt, click, quart) already match
# flask_core's own lockfile exactly -- this second install only adds
# flask_core's remaining deps (flask, authlib, sqlalchemy, ...) without
# silently re-resolving the ones pinned above.
RUN pip install --no-cache-dir ./libs/flask_core

# Runtime stage
FROM python:3.13-slim-bookworm@sha256:f13a6b7565175da40695e8109f64cbc4d2e65f4c9ef2e3b321c3a44fa3c06fe7

WORKDIR /app

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

# Copy from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder --chown=appuser:appuser /app .

# This runtime stage's own base image ships an older bundled pip
# (pip-26.0.1.dist-info) that the COPY above doesn't remove (COPY merges,
# it doesn't delete stale destination files) -- drop it so only the
# builder's patched pip metadata (26.2.1) remains.
RUN find /usr/local/lib/python3.13/site-packages -maxdepth 1 -name 'pip-*.dist-info' ! -name 'pip-26.2.1.dist-info' -exec rm -rf {} +

# Switch to non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8020/healthz').read()"

# Expose port
EXPOSE 8020

# Start application
CMD ["python3", "-m", "hypercorn", "app:app", "--bind", "0.0.0.0:8020"]
