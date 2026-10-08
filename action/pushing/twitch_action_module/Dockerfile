# Twitch Action Module Dockerfile
# Build from parent directory: docker build -f action/pushing/twitch_action_module/Dockerfile -t waddlebot/twitch-action:latest .

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY action/pushing/twitch_action_module/requirements.txt /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy module files
COPY action/pushing/twitch_action_module /app/

# Create log directory
RUN mkdir -p /var/log/waddlebotlog

# Create PyDAL migration log directory
RUN mkdir -p /app/databases

# Copy standard healthcheck script
COPY scripts/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 755 /usr/local/bin/healthcheck.py

# Create non-root user
RUN groupadd -r waddlebot && useradd -r -g waddlebot -m waddlebot

# Set proper permissions for app directory and log directory
RUN chown -R waddlebot:waddlebot /app /var/log/waddlebotlog

# Expose ports
EXPOSE 8072

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

CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:8072", "--workers", "4"]
