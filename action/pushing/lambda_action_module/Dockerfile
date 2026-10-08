# Lambda Action Module Dockerfile
# Build from parent directory: docker build -f action/pushing/lambda_action_module/Dockerfile -t waddlebot/lambda-action:latest .

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY action/pushing/lambda_action_module/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY action/pushing/lambda_action_module /app/

# Compile Protobuf messages
RUN python -m grpc_tools.protoc -I./grpc_proto --python_out=./grpc_proto --grpc_python_out=./grpc_proto ./grpc_proto/lambda_action.proto && \
    sed -i 's/import lambda_action_pb2/from grpc_proto import lambda_action_pb2/g' ./grpc_proto/lambda_action_pb2_grpc.py

# Create log directory
RUN mkdir -p /var/log/waddlebotlog

# Copy standard healthcheck script
COPY scripts/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 755 /usr/local/bin/healthcheck.py

# Create non-root user
RUN groupadd -r waddlebot && useradd -r -g waddlebot -m waddlebot

# Set proper permissions for app directory and log directory
RUN chown -R waddlebot:waddlebot /app /var/log/waddlebotlog

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

# Expose port
EXPOSE 8080

# Run application
CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "4"]
