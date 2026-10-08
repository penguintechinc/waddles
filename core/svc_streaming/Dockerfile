# svc-streaming -- Streaming module control plane + real ffmpeg data plane.
# Build from parent (repo root):
#   docker build -f core/svc_streaming/Dockerfile -t waddlebot/svc-streaming:local .
#
# Real forwarding: `ffmpeg` (Debian's `ffmpeg` package, apt) is the actual
# media engine this container shells out to (`services/ffmpeg_engine.py`)
# -- not simulated. Design spec targets a Rust control plane / first-party
# media engine (docs/plans/2026-08-31-svc-streaming-design.md §1, §8.1);
# this is the Python/Quart + ffmpeg build for tonight's alpha demo -- see
# app.py's module docstring and this PR's description for the full
# rationale. No CI image exists for svc-streaming yet -- the Helm chart
# still points at the shared `pipeline.pythonBaseImage` placeholder
# (`k8s/helm/waddlebot/templates/svc-streaming.yaml`), same posture every
# other freshly-built core/svc_* container in this repo is in today
# (`core/svc_presentation/Dockerfile`'s own header comment).

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

WORKDIR /app

# Real ffmpeg binary -- pinned to whatever `ffmpeg` apt resolves to on this
# exact base-image digest (Debian bookworm's own repos, not a third-party
# PPA); `--no-install-recommends` keeps the image lean, no X11/GUI deps.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Shared library
COPY libs/flask_core /app/libs/flask_core
RUN cd /app/libs/flask_core && pip install --no-cache-dir .

# Module files
COPY core/svc_streaming/requirements.txt /app/
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY core/svc_streaming /app/

RUN mkdir -p /var/log/waddlebotlog /var/lib/svc-streaming/recordings \
    && useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app /var/log/waddlebotlog /var/lib/svc-streaming

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

# MODULE_PORT is set by the Helm Deployment (default 8208 here too) --
# shell form so ${MODULE_PORT} is honored if overridden at runtime.
ENV MODULE_PORT=8208
CMD ["sh", "-c", "hypercorn app:app --bind 0.0.0.0:${MODULE_PORT} --workers 2"]
