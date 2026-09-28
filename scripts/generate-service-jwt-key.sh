#!/usr/bin/env bash
# Generates the Ed25519 signing keypair for hub-api's per-service machine
# JWTs (flask_core.service_jwt / core/service_auth) and creates the k8s
# Secret directly from it -- the private key is never printed to stdout,
# never passed as a CLI arg, and never written to a file on disk. Mirrors
# the safe-generation pattern used for bundle artifact signing
# (scripts/generate-bundle-signing-key.sh, PR #431).
#
# Usage: KID=2026-09-28 NAMESPACE=waddlebot ./scripts/generate-service-jwt-key.sh
set -euo pipefail

KID="${KID:?set KID to a short rotation identifier, e.g. the generation date}"
NAMESPACE="${NAMESPACE:-waddlebot}"
SECRET_NAME="${SECRET_NAME:-service-jwt-signing-key}"

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required" >&2
  exit 1
fi
if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required" >&2
  exit 1
fi

# Generate straight to a PEM-in-memory pipeline; nothing touches disk.
# `openssl genpkey` writes PKCS8 PEM to stdout, piped directly into
# `kubectl create secret --from-file=-` equivalent via process substitution
# -- the private key exists only in these two processes' memory and pipes.
priv_key="$(openssl genpkey -algorithm ed25519)"
pub_key="$(openssl pkey -pubout <<<"$priv_key")"

kubectl create secret generic "${SECRET_NAME}" \
  --namespace "${NAMESPACE}" \
  --from-literal="SERVICE_JWT_ACTIVE_KID=${KID}" \
  --from-literal="SERVICE_JWT_PRIVATE_KEY_${KID}=${priv_key}" \
  --dry-run=client -o yaml \
  | kubectl apply -f -

unset priv_key

echo "Secret ${SECRET_NAME} (kid=${KID}) applied in namespace ${NAMESPACE}."
echo "Public key (safe to commit to values.yaml under serviceJwt.publicKeys.${KID}):"
echo "$pub_key"
