# Build from repo root: docker build -f action/interactive/lfg_interaction_module/Dockerfile -t waddlebot/lfg-interaction:latest .

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26

# uv replaces pip for dependency installation (pinned by digest)
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /usr/local/bin/uv

WORKDIR /app

# Copy shared library
COPY libs/flask_core /app/libs/flask_core

# Install shared library
RUN uv pip install --system --no-cache /app/libs/flask_core

# Copy module files
COPY action/interactive/lfg_interaction_module/requirements.txt /app/
COPY action/interactive/lfg_interaction_module /app/

# Install module dependencies
RUN uv pip install --system --no-cache -r requirements.txt

# Create log directory and non-root user, then hand over ownership.
# Combined into one layer: all three steps are simple filesystem updates.
RUN mkdir -p /var/log/waddlebotlog && \
    groupadd -r waddlebot && useradd -r -g waddlebot -m waddlebot && \
    chown -R waddlebot:waddlebot /app /var/log/waddlebotlog

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

CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:8096", "--workers", "4"]
