# Build from parent directory: docker build -f core/workflow_core_module/Dockerfile -t waddlebot/workflow-core:latest .

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26

# uv replaces pip for dependency installation (pinned by digest)
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /usr/local/bin/uv

WORKDIR /app

# Copy shared library
COPY libs/flask_core /app/libs/flask_core

# Install shared library
RUN uv pip install --system --no-cache /app/libs/flask_core

# Copy module files
COPY core/workflow_core_module/requirements.txt /app/
COPY core/workflow_core_module /app/

# Install module dependencies
RUN uv pip install --system --no-cache -r requirements.txt

# Compile Protobuf messages. Output MUST land in ./proto (not repo root) --
# services/grpc_handler.py does `sys.path.insert(0, ".../proto")` then a
# bare `import workflow_pb2`/`import workflow_pb2_grpc`; generating into
# `.` (the previous `--python_out=.`) put the stubs where nothing imports
# them, so `import workflow_pb2` fell through to the `except ImportError`
# fallback (`workflow_pb2 = None`) on every container start, which then
# crashed `class WorkflowGrpcService(workflow_pb2_grpc.WorkflowServiceServicer)`
# at import time with AttributeError -- the gRPC service never actually ran.
RUN python -m grpc_tools.protoc -I./proto --python_out=./proto --grpc_python_out=./proto ./proto/workflow.proto

# Create non-root user
RUN useradd -m -u 1000 waddlebot && \
    mkdir -p /var/log/waddlebotlog && \
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

# Expose port
EXPOSE 8090

# Copy standard healthcheck script
COPY scripts/healthcheck.py /usr/local/bin/healthcheck.py

# Health check (using /health for consistency with other modules)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 /usr/local/bin/healthcheck.py http://localhost:${MODULE_PORT:-8090}/health

# Run with Hypercorn (shell form so MODULE_PORT env var is substituted)
CMD hypercorn app:app --bind "0.0.0.0:${MODULE_PORT:-8090}" --workers 4
