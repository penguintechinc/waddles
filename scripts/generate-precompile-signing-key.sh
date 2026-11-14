#!/bin/bash
# generate-precompile-signing-key.sh
#
# Generates a fresh, DEDICATED Ed25519 keypair for signing AOT-precompiled `.cwasm`
# artifacts (core/bundle_executor/src/precompile.rs). It is a different keypair from the
# bundle-approval signing key (scripts/generate-bundle-signing-key.sh): compromise of one
# must never be able to forge the other, and executors REFUSE a precompile key set that
# reuses a bundle-signing public key.
#
# Creates/updates, in one namespace:
#   Secret    <fullname>-executor-precompile-signing        (PUBLISHER side, private)
#             PRECOMPILE_SIGNING_PRIVATE_KEY  base64 32-byte seed (mounted as a FILE into
#                                              the executor-precompile sign step only)
#             PRECOMPILE_SIGNING_KEY_ID
#   ConfigMap <fullname>-executor-precompile-signing-public (EXECUTOR side, public)
#             PRECOMPILE_SIGNING_PUBLIC_KEYS  JSON {"<key-id>": "<base64 public key>", ...}
#
# ROTATION: any key ids already in the ConfigMap are KEPT, so executors accept the new
# (current) key and the previous one while artifacts signed under the old key are
# re-published. Remove the old id afterwards with:
#   kubectl -n <ns> edit configmap <fullname>-executor-precompile-signing-public
#
# The private key NEVER touches stdout, a repo file, or a CLI argument: it lives only in a
# 0600 temp file, shredded on exit. Only the key id and the PUBLIC key are printed.
# This script is run by the OWNER -- agents/CI must not run it (secret-store write).
#
# Requires: openssl (Ed25519, 1.1.1+), kubectl, python3.
#
# Usage:
#   scripts/generate-precompile-signing-key.sh <key-id> <kube-context> <namespace> <fullname>
#
# Example (alpha, release "waddlebot"):
#   scripts/generate-precompile-signing-key.sh <key-id> dal2alpha waddlebot waddlebot

set -euo pipefail

if [ "$#" -lt 4 ]; then
    echo "Usage: $0 <key-id> <kube-context> <namespace> <fullname>" >&2
    exit 1
fi

KEY_ID="$1"
KUBE_CONTEXT="$2"
NAMESPACE="$3"
FULLNAME="$4"
SECRET_NAME="${FULLNAME}-executor-precompile-signing"
CM_NAME="${FULLNAME}-executor-precompile-signing-public"

if ! [[ "$KEY_ID" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "ERROR: key-id must match [A-Za-z0-9._-]+ (got: $KEY_ID)" >&2
    exit 1
fi

for tool in openssl kubectl python3; do
    command -v "$tool" > /dev/null 2>&1 || { echo "ERROR: $tool not found" >&2; exit 1; }
done

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/precompile-signing-key.XXXXXX")"
chmod 700 "$WORKDIR"

cleanup() {
    if command -v shred > /dev/null 2>&1; then
        shred -u -f "$WORKDIR"/*.der "$WORKDIR"/*.pem "$WORKDIR"/priv.b64 2>/dev/null || true
    fi
    rm -rf "$WORKDIR"
}
trap cleanup EXIT INT TERM

umask 077

openssl genpkey -algorithm ed25519 -out "$WORKDIR/priv.pem" > /dev/null 2>&1
openssl pkey -in "$WORKDIR/priv.pem" -outform DER -out "$WORKDIR/priv.der" > /dev/null 2>&1
openssl pkey -in "$WORKDIR/priv.pem" -pubout -outform DER -out "$WORKDIR/pub.der" > /dev/null 2>&1

# PKCS8 / SPKI DER of a raw Ed25519 key end in exactly the 32 raw key bytes.
tail -c 32 "$WORKDIR/priv.der" | base64 | tr -d '\n' > "$WORKDIR/priv.b64"
PUB_B64="$(tail -c 32 "$WORKDIR/pub.der" | base64 | tr -d '\n')"

# Keep previously published ids (rotation window); the new id replaces any same-named one.
EXISTING="$(kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get configmap "$CM_NAME" \
    -o 'jsonpath={.data.PRECOMPILE_SIGNING_PUBLIC_KEYS}' 2> /dev/null || true)"
PUB_JSON="$(EXISTING="$EXISTING" KEY_ID="$KEY_ID" PUB_B64="$PUB_B64" python3 -I -c '
import json, os
existing = os.environ.get("EXISTING", "").strip()
keys = json.loads(existing) if existing else {}
keys[os.environ["KEY_ID"]] = os.environ["PUB_B64"]
print(json.dumps(keys, sort_keys=True, separators=(",", ":")))
')"

kubectl create secret generic "$SECRET_NAME" \
    --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
    --from-file=PRECOMPILE_SIGNING_PRIVATE_KEY="$WORKDIR/priv.b64" \
    --from-literal=PRECOMPILE_SIGNING_KEY_ID="$KEY_ID" \
    --dry-run=client -o yaml \
    | kubectl apply --context "$KUBE_CONTEXT" -f -
kubectl annotate secret "$SECRET_NAME" --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
    helm.sh/resource-policy=keep --overwrite > /dev/null

kubectl create configmap "$CM_NAME" \
    --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
    --from-literal=PRECOMPILE_SIGNING_PUBLIC_KEYS="$PUB_JSON" \
    --dry-run=client -o yaml \
    | kubectl apply --context "$KUBE_CONTEXT" -f -

echo ""
echo "Secret    '${SECRET_NAME}' (private, publisher side) written in '${NAMESPACE}' on '${KUBE_CONTEXT}'."
echo "ConfigMap '${CM_NAME}' (public keys for executors) written."
echo ""
echo "key_id:     ${KEY_ID}"
echo "public_key: ${PUB_B64}"
echo ""
echo "Next: set autoProvisionedKeys.precompileSigning.externalSecret=true and keyId=${KEY_ID} in"
echo "the target environment's values file (beta/gamma/prod must not auto-generate), then run the"
echo "executor-precompile backfill so artifacts are published under the new key."
