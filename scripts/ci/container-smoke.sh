#!/usr/bin/env bash
# CI container smoke test: boot one built image and poll its /health endpoint.
#
# Usage: scripts/ci/container-smoke.sh <module> <image-ref> <port>
#
# Passes when /health answers 200 or 503 (503 = booted, deep readiness pending).
# Fails when the container exits before answering, or after MAX_RETRIES polls.
#
# Per-module runtime prerequisites (the image fails fast without them, by design):
#   hub-api    NODE_ENV=production requires JWT_SECRET + SERVICE_API_KEY, and
#              initializeDatabase() requires a reachable Postgres. A throwaway
#              postgres sidecar on a private docker network supplies the DB.
#   hub-webui  NODE_ENV=production requires HUB_API_URL (no localhost fallback).
#
# Secrets are ephemeral (openssl rand), written to 0600 env-files, never echoed.
# The app container is started WITHOUT --rm so its logs survive a failed boot.
set -euo pipefail

MODULE="${1:?usage: container-smoke.sh <module> <image-ref> <port>}"
IMAGE="${2:?usage: container-smoke.sh <module> <image-ref> <port>}"
PORT="${3:?usage: container-smoke.sh <module> <image-ref> <port>}"
case "$PORT" in
  ''|*[!0-9]*) echo "FAIL: port must be numeric (got '${PORT}')" >&2; exit 2 ;;
esac

RUN_ID="$$"
NAME="smoke-${MODULE}-${RUN_ID}"
DB_NAME="smoke-pg-${MODULE}-${RUN_ID}"
NET="smoke-net-${MODULE}-${RUN_ID}"
PG_IMAGE="${SMOKE_POSTGRES_IMAGE:-postgres:16-bookworm@sha256:0ea6700a3b4f0ae6ce746519073558aed4d88a79d8d07622a9a644946c7319c4}"
MAX_RETRIES=12

WORK="$(mktemp -d)"
chmod 700 "$WORK"
APP_ENV="${WORK}/app.env"
PG_ENV="${WORK}/pg.env"
: > "$APP_ENV"
: > "$PG_ENV"
chmod 600 "$APP_ENV" "$PG_ENV"

# Cleanup runs on every exit path. The exit code is preserved, so the diagnostic
# log dump and best-effort removals never mask a failed gate.
# shellcheck disable=SC2317  # invoked indirectly via the EXIT trap below
cleanup() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "--- last 200 log lines from ${NAME} (exit ${rc}) ---"
    docker logs "$NAME" 2>&1 | tail -n 200 || echo "(no logs: container ${NAME} not found)"
    echo "--- end logs ---"
  fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker rm -f "$DB_NAME" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit "$rc"
}
trap cleanup EXIT

NET_ARGS=()
case "$MODULE" in
  hub-api)
    umask 077
    DB_PASS="$(openssl rand -hex 24)"
    {
      echo "JWT_SECRET=$(openssl rand -hex 32)"
      echo "SERVICE_API_KEY=$(openssl rand -hex 32)"
      echo "DATABASE_URL=postgresql://smoke:${DB_PASS}@${DB_NAME}:5432/smoke"
    } > "$APP_ENV"
    printf 'POSTGRES_USER=smoke\nPOSTGRES_DB=smoke\nPOSTGRES_PASSWORD=%s\n' "$DB_PASS" > "$PG_ENV"

    docker network create "$NET" >/dev/null
    docker run -d --name "$DB_NAME" --network "$NET" --env-file "$PG_ENV" "$PG_IMAGE" >/dev/null

    PG_READY=0
    for _ in $(seq 1 60); do
      if docker exec "$DB_NAME" pg_isready -h 127.0.0.1 -U smoke -d smoke >/dev/null 2>&1; then
        PG_READY=1
        break
      fi
      sleep 2
    done
    if [ "$PG_READY" != 1 ]; then
      echo "FAIL: hub-api smoke Postgres sidecar not ready after 120s"
      exit 1
    fi
    echo "INFO: hub-api smoke Postgres sidecar ready (${PG_IMAGE%%@*})"
    NET_ARGS=(--network "$NET")
    ;;
  hub-webui)
    echo "HUB_API_URL=http://127.0.0.1:8060" > "$APP_ENV"
    ;;
esac

docker run -d --name "$NAME" ${NET_ARGS[@]+"${NET_ARGS[@]}"} --env-file "$APP_ENV" \
  -p "${PORT}:${PORT}" "$IMAGE" >/dev/null

RETRY=0
WAIT=5
HTTP_CODE=000
while [ "$RETRY" -lt "$MAX_RETRIES" ]; do
  HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/health" 2>/dev/null)" || HTTP_CODE=000
  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "503" ]; then
    echo "PASS: ${MODULE} /health answered HTTP ${HTTP_CODE} (attempt $((RETRY + 1))/${MAX_RETRIES})"
    exit 0
  fi
  RUNNING="$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" || RUNNING=false
  if [ "$RUNNING" != "true" ]; then
    EXIT_CODE="$(docker inspect -f '{{.State.ExitCode}}' "$NAME" 2>/dev/null)" || EXIT_CODE=unknown
    echo "FAIL: ${MODULE} container exited (code ${EXIT_CODE}) before /health passed"
    exit 1
  fi
  RETRY=$((RETRY + 1))
  if [ "$RETRY" -lt "$MAX_RETRIES" ]; then
    echo "INFO: ${MODULE} /health HTTP ${HTTP_CODE}, attempt ${RETRY}/${MAX_RETRIES}, retrying in ${WAIT}s"
    sleep "$WAIT"
    WAIT=$((WAIT + 2))
  fi
done
echo "FAIL: ${MODULE} /health not ready after ${MAX_RETRIES} attempts (last HTTP ${HTTP_CODE})"
exit 1
