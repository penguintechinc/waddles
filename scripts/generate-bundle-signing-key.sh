#!/bin/bash
# generate-bundle-signing-key.sh
#
# Generates a fresh Ed25519 keypair for platform bundle-artifact signing
# (hub_api/services/bundle_signing_service.py / core/bundle_executor/src/signing.rs)
# and creates or updates the Kubernetes Secret hub-api reads
# BUNDLE_SIGNING_PRIVATE_KEY/BUNDLE_SIGNING_KEY_ID from -- see the chart README's
# "Bundle artifact signing rollout order" section. Run this BEFORE deploying hub-api
# with pipeline.rustDataPlane.enabled, and again whenever rotating the active key.
#
# The private key NEVER touches stdout, a file inside this repo, or a CLI argument --
# it lives only in a 0600 temp file for the lifetime of this script, shredded on exit
# (success, failure, or interrupt) by the trap below. Only the key id and the PUBLIC
# key are printed, for pasting into pipeline.rustDataPlane.bundleSigningPublicKeys in
# the target environment's values file.
#
# Requires: openssl (Ed25519 support, OpenSSL 1.1.1+), kubectl.
#
# Usage:
#   scripts/generate-bundle-signing-key.sh <key-id> <kube-context> <namespace> [secret-name]
#
# Example:
#   scripts/generate-bundle-signing-key.sh 2026-09-key1 dal2-beta waddlebot

set -euo pipefail

if [ "$#" -lt 3 ]; then
    echo "Usage: $0 <key-id> <kube-context> <namespace> [secret-name]" >&2
    exit 1
fi

KEY_ID="$1"
KUBE_CONTEXT="$2"
NAMESPACE="$3"
SECRET_NAME="${4:-waddlebot-bundle-signing}"

if ! [[ "$KEY_ID" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "ERROR: key-id must match [A-Za-z0-9._-]+ (got: $KEY_ID)" >&2
    exit 1
fi

command -v openssl > /dev/null 2>&1 || { echo "ERROR: openssl not found" >&2; exit 1; }
command -v kubectl > /dev/null 2>&1 || { echo "ERROR: kubectl not found" >&2; exit 1; }

# Private key material lives only in this directory, mode 0700, for the life of this
# script. TMPDIR (not /tmp directly) respects an already-more-private tmp mount if one
# is configured; falls back to /tmp otherwise -- same default `mktemp -d` would use.
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/bundle-signing-key.XXXXXX")"
chmod 700 "$WORKDIR"

cleanup() {
    # shred overwrites before unlink; -u removes the file; -f ignores a file that was
    # never created (e.g. openssl failed before writing it) rather than erroring out of
    # the trap. Falls back to a plain rm if shred is unavailable (e.g. some minimal
    # container base images) so cleanup still happens, just without the overwrite pass.
    if command -v shred > /dev/null 2>&1; then
        shred -u -f "$WORKDIR"/*.der "$WORKDIR"/*.pem "$WORKDIR"/priv.b64 2>/dev/null || true
    fi
    rm -rf "$WORKDIR"
}
trap cleanup EXIT INT TERM

PRIV_PEM="$WORKDIR/priv.pem"
PRIV_DER="$WORKDIR/priv.der"
PUB_DER="$WORKDIR/pub.der"
PRIV_B64_FILE="$WORKDIR/priv.b64"

umask 077

openssl genpkey -algorithm ed25519 -out "$PRIV_PEM" > /dev/null 2>&1
openssl pkey -in "$PRIV_PEM" -outform DER -out "$PRIV_DER" > /dev/null 2>&1
openssl pkey -in "$PRIV_PEM" -pubout -outform DER -out "$PUB_DER" > /dev/null 2>&1

# PKCS8 (private) and SubjectPublicKeyInfo (public) DER encodings of a raw Ed25519 key
# both end in exactly the 32 raw key bytes after a fixed-length ASN.1 prefix -- `tail -c
# 32` pulls those bytes without depending on the exact prefix length OpenSSL emits
# (stable across the OpenSSL versions this project supports).
tail -c 32 "$PRIV_DER" | base64 > "$PRIV_B64_FILE"
# base64(1) may wrap output; kubectl --from-file reads the raw file bytes as the secret
# value, and Python's `base64.b64decode(..., validate=True)` rejects embedded newlines
# -- strip them so the stored secret is a single unbroken base64 line.
tr -d '\n' < "$PRIV_B64_FILE" > "$PRIV_B64_FILE.stripped"
mv "$PRIV_B64_FILE.stripped" "$PRIV_B64_FILE"

PUB_B64="$(tail -c 32 "$PUB_DER" | base64 | tr -d '\n')"

kubectl create secret generic "$SECRET_NAME" \
    --context "$KUBE_CONTEXT" \
    --namespace "$NAMESPACE" \
    --from-file=BUNDLE_SIGNING_PRIVATE_KEY="$PRIV_B64_FILE" \
    --from-literal=BUNDLE_SIGNING_KEY_ID="$KEY_ID" \
    --dry-run=client -o yaml \
    | kubectl apply --context "$KUBE_CONTEXT" -f -

echo ""
echo "Secret '${SECRET_NAME}' created/updated in namespace '${NAMESPACE}' on context '${KUBE_CONTEXT}'."
echo ""
echo "key_id:     ${KEY_ID}"
echo "public_key: ${PUB_B64}"
echo ""
echo "Add to values-<env>.yaml under pipeline.rustDataPlane.bundleSigningPublicKeys:"
echo "  ${KEY_ID}: \"${PUB_B64}\""
