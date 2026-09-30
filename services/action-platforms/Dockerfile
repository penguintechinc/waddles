# Multi-stage build for action-platforms service
# Consolidates 6 platform action modules: Slack, Teams, Mattermost, GoogleChat, Twitch, YouTube

# Stage 1: Builder
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS builder

WORKDIR /app

# Copy requirements
COPY services/action-platforms/requirements.txt .

# Install dependencies
# Pin pip itself: base image ships 26.0.1 (PYSEC-2026-2875/2876/196/3721, fixed <=26.1.2/26.2)
RUN pip install --no-cache-dir --upgrade "pip==26.2.1" && \
    pip install --no-cache-dir -r requirements.txt

# Copy and install flask_core from shared libs
COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core

# Stage 2: Runtime
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26

WORKDIR /app

# Install runtime dependencies only (no dev packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# This runtime stage's own base image ships an older bundled pip
# (pip-26.0.1.dist-info) that the COPY above doesn't remove (COPY merges,
# it doesn't delete stale destination files) -- drop it so only the
# builder's patched pip metadata (26.2.1) remains.
RUN find /usr/local/lib/python3.13/site-packages -maxdepth 1 -name 'pip-*.dist-info' ! -name 'pip-26.2.1.dist-info' -exec rm -rf {} +

# Copy application code
COPY services/action-platforms/app.py .

# Copy all 6 platform module directories
COPY action/pushing/slack_action_module ./slack_action_module
COPY action/pushing/teams_action_module ./teams_action_module
COPY action/pushing/mattermost_action_module ./mattermost_action_module
COPY action/pushing/googlechat_action_module ./googlechat_action_module
COPY action/pushing/twitch_action_module ./twitch_action_module
COPY action/pushing/youtube_action_module ./youtube_action_module

# Generate gRPC protobuf stubs for the YouTube module. Output MUST land in
# ./youtube_action_module/proto (not the image root) -- youtube_action_module/
# services/grpc_handler.py runs with youtube_action_module/ prepended to
# sys.path (see app.py's _setup_module_imports) and does a bare
# `from proto import youtube_action_pb2, youtube_action_pb2_grpc`; that
# resolves to youtube_action_module/proto/, the package with the __init__.py
# marker, and nowhere else. The standalone youtube_action_module/Dockerfile
# already generates into ./proto for its own single-service image -- this
# mirrors that exact invocation for the consolidated action-platforms image,
# which previously never ran protoc at all, so services/__init__.py's
# `from .grpc_handler import ...` raised ImportError on every real deploy
# (services/action-platforms/app.py imports
# youtube_action_module.services.oauth_manager, which pulls in the package's
# __init__.py, which pulls in grpc_handler.py unconditionally). Slack/Teams/
# Twitch's own grpc_handler.py modules already guard this same import with
# try/except ImportError (or never uncomment it), so they don't crash here --
# only YouTube's is unconditional.
RUN python3 -m grpc_tools.protoc \
    -I./youtube_action_module/proto \
    --python_out=./youtube_action_module/proto \
    --grpc_python_out=./youtube_action_module/proto \
    ./youtube_action_module/proto/youtube_action.proto && \
    sed -i 's/^import youtube_action_pb2/from . import youtube_action_pb2/' \
        ./youtube_action_module/proto/youtube_action_pb2_grpc.py

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

# Health check
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python3 -c "import http.client; conn=http.client.HTTPConnection('localhost:8102'); conn.request('GET', '/health'); exit(0 if conn.getresponse().status == 200 else 1)" || exit 1

# Expose REST API port
EXPOSE 8102

# Run with gunicorn/hypercorn
CMD ["hypercorn", "--bind", "0.0.0.0:8102", "--workers", "4", "app:app"]
