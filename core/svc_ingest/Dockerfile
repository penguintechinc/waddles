# svc-ingest -- real Quart control-plane + stage-runner + supervised socket receivers. Rootless.
# Base digest matches core/svc_streaming/Dockerfile and hub_api/Dockerfile
# (devops-containers.md forbids inventing a second pin for the same
# python:3.13-slim-bookworm tag). Build from parent (repo root):
#   docker build -f core/svc_ingest/Dockerfile -t waddlebot/svc-ingest:local .

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS builder
WORKDIR /app

COPY core/svc_ingest/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# flask_core (libs/flask_core) is a real installable package (setup.py) --
# pip install it directly, matching hub_api/Dockerfile's own pattern.
COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core

# waddle_transports (libs/waddle_transports) -- shared inbound/outbound
# transport primitives; receivers/discord_gateway.py's DiscordGatewayReceiver
# and receivers/twitch_irc.py's TwitchIrcReceiver both subclass its
# Transport ABC. Same direct-install pattern as flask_core.
COPY libs/waddle_transports /app/libs/waddle_transports
RUN pip install --no-cache-dir /app/libs/waddle_transports

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY core/svc_ingest/app.py core/svc_ingest/config.py core/svc_ingest/runner.py \
     core/svc_ingest/supervisor.py core/svc_ingest/fanout.py core/svc_ingest/socket_lease.py \
     core/svc_ingest/eventsub.py core/svc_ingest/outbound_drain.py ./
COPY core/svc_ingest/bundles ./bundles
COPY core/svc_ingest/receivers ./receivers

EXPOSE 8210
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import httpx; resp = httpx.get('http://localhost:8210/healthz'); exit(0 if resp.status_code == 200 else 1)" || exit 1

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

CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:8210"]
