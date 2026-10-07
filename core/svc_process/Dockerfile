# svc-process -- real Quart control-plane + stage-runner. Rootless.
# Mirrors core/svc_ingest/Dockerfile exactly (same base digest pin -- see
# its own comment for the devops-containers.md rationale).

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS builder
WORKDIR /app

COPY core/svc_process/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core

# moderation_module -- content-moderation classifier (docs/plans/2026-09-08-
# content-moderation-design.md), same local-package pattern as flask_core.
COPY libs/moderation_module /app/libs/moderation_module
RUN pip install --no-cache-dir /app/libs/moderation_module

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY core/svc_process/app.py core/svc_process/config.py core/svc_process/runner.py ./
COPY core/svc_process/bundles ./bundles
COPY core/svc_process/services ./services

EXPOSE 8211
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import httpx; resp = httpx.get('http://localhost:8211/healthz'); exit(0 if resp.status_code == 200 else 1)" || exit 1

RUN mkdir -p /var/log/waddlebotlog \
    && groupadd -r appuser && useradd -r -g appuser -m appuser \
    && chown -R appuser:appuser /app /var/log/waddlebotlog

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

CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:8211"]
