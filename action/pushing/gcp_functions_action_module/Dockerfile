# GCP Functions Action Module Dockerfile
# Build from parent directory: docker build -f action/pushing/gcp_functions_action_module/Dockerfile -t waddlebot/gcp-functions-action:latest .

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26

# Set working directory
WORKDIR /app

# Install system dependencies (build-essential needed for google-cloud-functions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY action/pushing/gcp_functions_action_module/requirements.txt /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy module files
COPY action/pushing/gcp_functions_action_module /app/

# Create log directory
RUN mkdir -p /var/log/waddlebotlog

# Create non-root user
RUN groupadd -r waddlebot && useradd -r -g waddlebot -m waddlebot

# Set proper permissions for app directory and log directory
RUN chown -R waddlebot:waddlebot /app /var/log/waddlebotlog

# Create databases directory for PyDAL
RUN mkdir -p /app/databases

# Generate gRPC code from proto file
RUN python -m grpc_tools.protoc \
    -I./grpc_proto \
    --python_out=./grpc_proto \
    --grpc_python_out=./grpc_proto \
    ./grpc_proto/gcp_functions_action.proto && \
    # Fix import paths in generated grpc file
    sed -i 's/^import gcp_functions_action_pb2/from grpc_proto import gcp_functions_action_pb2/' ./grpc_proto/gcp_functions_action_pb2_grpc.py

# Copy standard healthcheck script
COPY scripts/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 755 /usr/local/bin/healthcheck.py

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

# Expose ports (REST and gRPC)
EXPOSE 8081 50061

# Health check (using /healthz for Kubernetes compatibility)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 /usr/local/bin/healthcheck.py http://localhost:8081/healthz

# Run with Hypercorn (async ASGI server)
CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:8081", "--workers", "4"]
