# svc-presentation -- overlay/music-station presentation container. Not
# wired into CI yet; the Helm Deployment
# (k8s/helm/waddlebot/templates/svc-presentation.yaml) still points at
# pipeline.pythonBaseImage as a placeholder until this image has a real CI
# build. Build from parent (repo root):
#   docker build -f core/svc_presentation/Dockerfile -t waddlebot/svc-presentation:local .

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

WORKDIR /app

# Shared library
COPY libs/flask_core /app/libs/flask_core
RUN cd /app/libs/flask_core && pip install --no-cache-dir .

# Module files -- requirements.txt is hash-pinned (uv pip compile
# --generate-hashes, requirements.in is the source); --require-hashes
# fails the build if a future edit ever reintroduces an unhashed line.
COPY core/svc_presentation/requirements.txt /app/
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY core/svc_presentation /app/

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

# MODULE_PORT is set by the Helm Deployment (default 8207 here too) --
# shell form so ${MODULE_PORT} is honored if overridden at runtime.
ENV MODULE_PORT=8207
CMD ["sh", "-c", "hypercorn app:app --bind 0.0.0.0:${MODULE_PORT} --workers 2"]
