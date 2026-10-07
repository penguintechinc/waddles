FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

# uv replaces pip for dependency installation (pinned by digest)
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /usr/local/bin/uv
WORKDIR /app
COPY libs/flask_core /app/libs/flask_core
RUN uv pip install --system --no-cache /app/libs/flask_core
COPY action/interactive/server_manager_interaction_module/requirements.txt /app/
COPY action/interactive/server_manager_interaction_module /app/
RUN uv pip install --system --no-cache -r requirements.txt
RUN mkdir -p /var/log/waddlebotlog
RUN groupadd -r waddlebot && useradd -r -g waddlebot -m waddlebot
RUN chown -R waddlebot:waddlebot /app /var/log/waddlebotlog
EXPOSE 8099
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
CMD hypercorn app:app --bind "0.0.0.0:${MODULE_PORT:-8099}" --workers 4
