# Build from parent directory: docker build -f reputation_module/Dockerfile -t waddlebot/reputation:latest .

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

WORKDIR /app

# Copy shared library
COPY libs/flask_core /app/libs/flask_core

# Install shared library
RUN pip install --no-cache-dir /app/libs/flask_core

# Copy module files
COPY core/reputation_module/requirements.txt /app/
COPY core/reputation_module /app/

# Install module dependencies
# Create log directory
RUN pip install --no-cache-dir -r requirements.txt && \
    mkdir -p /var/log/waddlebotlog

# Create non-root user
# Set proper permissions for app directory and log directory
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app /var/log/waddlebotlog

# Copy and make entrypoint script executable
COPY core/reputation_module/entrypoint.sh /app/entrypoint.sh
# Remove base image's own bundled pip + ensurepip wheel: pip vendors an
# outdated msgpack (GHSA-6v7p-g79w-8964) and pkg_resources/setuptools
# (CVE-2025-47273) that the upstream python:3.13-slim-bookworm rebuild
# hasn't picked up yet. Neither pip nor its vendor tree is imported by
# any service at runtime (installs happen at build time via uv/pip
# above), so removing them is a real fix, not a suppression.
RUN chmod +x /app/entrypoint.sh && \
    rm -rf /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.13/ensurepip

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
