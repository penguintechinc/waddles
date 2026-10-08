# Build from parent directory: docker build -f processing/router_module/Dockerfile -t waddlebot/router:latest .

# Use Python 3.12 for better fastText compatibility (C++17 issues with 3.13)
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

WORKDIR /app

# Install build dependencies for native extensions (fasttext, pydantic-core, lingua)
# Also install wget for downloading the fastText model
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    g++ \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copy shared library
COPY libs/flask_core /app/libs/flask_core

# Install shared library
RUN cd /app/libs/flask_core && pip install --no-cache-dir .

# Copy module files
COPY processing/router_module/requirements.txt /app/
COPY processing/router_module /app/

# Install module dependencies (includes fasttext-wheel for language detection)
RUN pip install --no-cache-dir -r requirements.txt

# Create models directory and download fastText language identification model
# Model: lid.176.bin (~130MB) - supports 176 languages with high accuracy
RUN mkdir -p /app/models && \
    wget -q --show-progress -O /app/models/lid.176.bin \
    https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin && \
    echo "FastText model downloaded: $(du -h /app/models/lid.176.bin | cut -f1)"

# Create log directory
RUN mkdir -p /var/log/waddlebotlog

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

# Set proper permissions for app directory, models, and log directory
RUN chown -R appuser:appuser /app /var/log/waddlebotlog

# Environment variable for fastText model path
ENV FASTTEXT_MODEL_PATH=/app/models/lid.176.bin

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

CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:8000", "--workers", "4"]
