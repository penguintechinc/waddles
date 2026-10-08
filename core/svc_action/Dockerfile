# svc-action -- ACTION stage-runner. Rootless, pinned digest, mirrors the
# same shape as core/svc_streaming/Dockerfile / core/svc_presentation/Dockerfile.
# Replaces the Helm skeleton's placeholder image
# (k8s/helm/waddlebot/templates/svc-action.yaml `pipeline.pythonBaseImage`)
# once CI builds and pushes this image. Build from parent (repo root):
#   docker build -f core/svc_action/Dockerfile -t waddlebot/svc-action:local .

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

WORKDIR /app

# Shared libraries
COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core
COPY libs/waddle_transports /app/libs/waddle_transports
RUN pip install --no-cache-dir --require-hashes -r /app/libs/waddle_transports/requirements.txt \
    && pip install --no-cache-dir /app/libs/waddle_transports

# waddle_transports (libs/waddle_transports) -- shared, direction-tagged
# transport contracts (see that package's own README for the coordination
# note); same install pattern as flask_core above.
COPY libs/waddle_transports /app/libs/waddle_transports
RUN pip install --no-cache-dir /app/libs/waddle_transports

# Module files
COPY core/svc_action/requirements.txt /app/
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY core/svc_action /app/

RUN mkdir -p /var/log/waddlebotlog \
    && useradd --create-home --shell /bin/bash appuser \
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

# MODULE_PORT is set by the Helm Deployment (default 8202 here too,
# values.yaml pipeline.svcAction.port) -- shell form so ${MODULE_PORT} is
# honored if overridden at runtime.
ENV MODULE_PORT=8202
CMD ["sh", "-c", "hypercorn app:app --bind 0.0.0.0:${MODULE_PORT} --workers 2"]
