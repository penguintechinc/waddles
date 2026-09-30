# Teams Action Module Dockerfile
# Python 3.12 container for pushing actions to Microsoft Teams

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26

# uv replaces pip for dependency installation (pinned by digest)
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /usr/local/bin/uv

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    postgresql-client \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY action/pushing/teams_action_module/requirements.txt /app/
RUN uv pip install --system --no-cache -r requirements.txt

# Copy proto files and generate gRPC code
COPY action/pushing/teams_action_module/proto /app/proto
RUN python -m grpc_tools.protoc \
    -I./proto \
    --python_out=./proto \
    --grpc_python_out=./proto \
    ./proto/teams_action.proto && \
    sed -i 's/^import teams_action_pb2/from proto import teams_action_pb2/' ./proto/teams_action_pb2_grpc.py

# Copy application files
COPY action/pushing/teams_action_module/config.py /app/
COPY action/pushing/teams_action_module/app.py /app/
COPY action/pushing/teams_action_module/services /app/services/

# Create log directory
RUN mkdir -p /var/log/waddlebotlog

# Create PyDAL migration log directory
RUN mkdir -p /tmp/pydal

# Copy standard healthcheck script
COPY scripts/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 755 /usr/local/bin/healthcheck.py

# Create non-root user
RUN useradd -m -u 1000 waddlebot && \
    chown -R waddlebot:waddlebot /app /var/log/waddlebotlog /tmp/pydal

# Switch to non-root user
# Remove base image's own bundled pip + ensurepip wheel: pip vendors an
# outdated msgpack (GHSA-6v7p-g79w-8964) and pkg_resources/setuptools
# (CVE-2025-47273) that the upstream python:3.13-slim-bookworm rebuild
# hasn't picked up yet. Neither pip nor its vendor tree is imported by
# any service at runtime (installs happen at build time via uv/pip
# above), so removing them is a real fix, not a suppression.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.13/ensurepip

USER waddlebot

# Ports and worker count are build-time overridable and runtime-overridable.
# The names match config.py so container env drives the app's own settings.
ARG REST_PORT=8074
ARG GRPC_PORT=50057
ARG HYPERCORN_WORKERS=4
ENV REST_PORT=${REST_PORT} \
    GRPC_PORT=${GRPC_PORT} \
    HYPERCORN_WORKERS=${HYPERCORN_WORKERS}

# Expose REST API and gRPC ports
EXPOSE ${REST_PORT}
EXPOSE ${GRPC_PORT}

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 /usr/local/bin/healthcheck.py http://localhost:${REST_PORT}/healthz

# Run with Hypercorn
CMD ["sh", "-c", "exec hypercorn app:app --bind 0.0.0.0:${REST_PORT} --workers ${HYPERCORN_WORKERS}"]
