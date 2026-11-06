#!/usr/bin/env bash
# Publishes the Twitch/Discord/YouTube connector service bundles (wasm + Ed25519-signed sidecar)
# to the bundle bucket the executor boot-pins from (connectors/<p>/<digest>.{wasm,json}).
#
# Builds the SAME reproducible, digest-pinned core-bundles image the seeder ships
# (bundles/Dockerfile.core-bundles), then runs hub_api/cli/publish_connector_bundles.py inside it.
# Prints per-connector digests + a pipeline.connectors.digests values snippet to stdout.
#
# Secrets are NEVER argv: provide them via env / a file you own.
#   BUNDLE_SIGNING_KEY_FILE   path to a 0600 file holding the base64 Ed25519 seed (the value of
#                             BUNDLE_SIGNING_PRIVATE_KEY in the waddlebot-bundle-signing Secret;
#                             USER-held -- this repo never generates/prints it)
#   BUNDLE_SIGNING_KEY_ID     key id whose PUBLIC half is in BUNDLE_SIGNING_PUBLIC_KEYS
#   S3_ENDPOINT_URL           e.g. http://127.0.0.1:8333 (kubectl port-forward svc/infra-seaweedfs 8333)
#   S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY   write-capable identity for the bundle bucket
#   BUNDLE_BUCKET_NAME        optional, default waddles-bundles
#
# Usage: scripts/publish-connector-bundles.sh <env>     (make publish-connector-bundles ENV=dal2alpha)

set -euo pipefail
cd "$(dirname "$0")/.."

ENV_NAME="${1:-}"
case "$ENV_NAME" in
  alpha|dal2alpha|local) ;;
  *) echo "publish-connector-bundles: FAIL -- ENV must be one of alpha|dal2alpha|local (got '${ENV_NAME}')" >&2; exit 1 ;;
esac

need() { [ -n "${!1:-}" ] || { echo "publish-connector-bundles: FAIL -- $1 is not set" >&2; exit 1; }; }
for v in BUNDLE_SIGNING_KEY_FILE BUNDLE_SIGNING_KEY_ID S3_ENDPOINT_URL S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY; do need "$v"; done
[ -f "$BUNDLE_SIGNING_KEY_FILE" ] || { echo "publish-connector-bundles: FAIL -- BUNDLE_SIGNING_KEY_FILE does not exist" >&2; exit 1; }
command -v docker >/dev/null || { echo "publish-connector-bundles: FAIL -- docker is required" >&2; exit 1; }

KEY_ABS="$(cd "$(dirname "$BUNDLE_SIGNING_KEY_FILE")" && pwd)/$(basename "$BUNDLE_SIGNING_KEY_FILE")"
IMAGE="localhost/waddlebot/core-bundle-seeder:connector-publish-$(git rev-parse --short=12 HEAD)"

echo "publish-connector-bundles: building $IMAGE (env=$ENV_NAME)" >&2
docker build -q -f bundles/Dockerfile.core-bundles -t "$IMAGE" . >&2

# Credentials are passed by NAME (docker reads the value from this shell's env), never as a value.
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -v "$KEY_ABS:/run/secrets/bundle_signing_key:ro" \
  -e BUNDLE_SIGNING_PRIVATE_KEY_FILE=/run/secrets/bundle_signing_key \
  -e BUNDLE_SIGNING_KEY_ID -e S3_ENDPOINT_URL -e S3_ACCESS_KEY_ID -e S3_SECRET_ACCESS_KEY \
  -e BUNDLE_BUCKET_NAME \
  --entrypoint python3 "$IMAGE" -m cli.publish_connector_bundles
