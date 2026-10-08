# YouTube Action Module - Python 3.12 Container
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

# uv replaces pip for dependency installation (pinned by digest)
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /usr/local/bin/uv

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY action/pushing/youtube_action_module/requirements.txt /app/
RUN uv pip install --system --no-cache -r requirements.txt

# Copy application files
COPY action/pushing/youtube_action_module/ /app/

# Generate gRPC protobuf files
RUN python -m grpc_tools.protoc \
    -I./proto \
    --python_out=./proto \
    --grpc_python_out=./proto \
    ./proto/youtube_action.proto && \
    sed -i 's/^import youtube_action_pb2/from . import youtube_action_pb2/' ./proto/youtube_action_pb2_grpc.py && \
    python -c "from proto import youtube_action_pb2, youtube_action_pb2_grpc; assert youtube_action_pb2_grpc.YouTubeActionStub"

# Create log directory
RUN mkdir -p /var/log/waddlebotlog

# Copy standard healthcheck script
COPY scripts/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 755 /usr/local/bin/healthcheck.py

# Create non-root user
RUN useradd -m -u 1000 waddlebot && \
    chown -R waddlebot:waddlebot /app /var/log/waddlebotlog

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

# Expose ports
EXPOSE 8073 50054

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 /usr/local/bin/healthcheck.py http://localhost:8073/health

# Run with Hypercorn
CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:8073", "--workers", "4"]
