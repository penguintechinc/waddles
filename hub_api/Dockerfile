# hub-api -- Quart control-plane service (Task 0.5 scaffold).
#
# Multi-stage, rootless. Base digest reused from services/interactive-social/
# Dockerfile (already pinned in this repo -- devops-containers.md forbids
# inventing a second one for the same python:3.13-slim-bookworm tag).
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS builder
WORKDIR /app

COPY hub_api/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# flask_core (libs/flask_core) is a real installable package (setup.py) --
# pip install it directly rather than sys.path surgery at runtime; its own
# requirements.in is the source of truth for its transitive deps.
COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core

# wasm-tools -- `services/bundle_component_validator.py`'s WIT world
# conformance check (`wasm-tools component wit --json`) on every vendor
# component upload (spec Sec9.1's INSPECTING state). Pinned exact version +
# SHA256 verification per critical-rules.md Dependency Pinning ("Build
# tools, SDKs, compilers: pin to exact version with SHA256 hash"); fetched
# from the upstream bytecodealliance GitHub release, never apt (no Debian
# package exists). Multi-arch: this Dockerfile's own multi-arch contract
# (devops.md) selects the matching tarball by `dpkg --print-architecture`.
ARG WASM_TOOLS_VERSION=1.259.0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) wt_arch=x86_64; wt_sha256=3e9b374b4c7715b771b69bf0d65a337990ed4546ec5e97e01c0ff587dfc52160 ;; \
      arm64) wt_arch=aarch64; wt_sha256=9e1644f3841f2b783dd8af82de054667388e46689806fba4909ed45f3182800f ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/bytecodealliance/wasm-tools/releases/download/v${WASM_TOOLS_VERSION}/wasm-tools-${WASM_TOOLS_VERSION}-${wt_arch}-linux.tar.gz"; \
    python3 -c "import urllib.request; urllib.request.urlretrieve('${url}', '/tmp/wasm-tools.tar.gz')"; \
    echo "${wt_sha256}  /tmp/wasm-tools.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/wasm-tools.tar.gz -C /tmp; \
    install -m 0755 "/tmp/wasm-tools-${WASM_TOOLS_VERSION}-${wt_arch}-linux/wasm-tools" /usr/local/bin/wasm-tools; \
    rm -rf /tmp/wasm-tools.tar.gz "/tmp/wasm-tools-${WASM_TOOLS_VERSION}-${wt_arch}-linux"

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY hub_api/app.py hub_api/config.py hub_api/bootstrap.py ./
COPY hub_api/blueprints ./blueprints
COPY hub_api/routers ./routers
COPY hub_api/openapi ./openapi
COPY hub_api/services ./services
COPY hub_api/grpc_internal ./grpc_internal
# bootstrap.py's ScriptDirectory call only ever statically parses this
# directory's revision/down_revision graph -- it never executes alembic/
# env.py (see bootstrap.py's module docstring for why that matters in this
# process). script.py.mako/alembic.ini/tests are revision-authoring/CI-only,
# not needed at runtime.
COPY alembic/versions ./alembic/versions

EXPOSE 8204
# Internal-only gRPC listener (waddles.hub.internal.v1) -- ClusterIP +
# CiliumNetworkPolicy-scoped to svc-ingest/svc-process/svc-action, see
# k8s/helm/waddlebot/templates/hub-api-grpc-networkpolicy.yaml.
EXPOSE 50204
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import httpx; resp = httpx.get('http://localhost:8204/healthz'); exit(0 if resp.status_code == 200 else 1)" || exit 1

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

CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:8204"]
