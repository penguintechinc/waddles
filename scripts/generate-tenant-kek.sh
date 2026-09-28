#!/bin/bash
# generate-tenant-kek.sh
#
# Generates a fresh 256-bit root KEK for the tenant-DEK broker
# (hub_api/services/tenant_keystore.py::K8sSecretKekProvider) and creates
# or updates the Kubernetes Secret hub-api reads TENANT_KEK_HEX from. This
# is the platform baseline KEK (spec: docs/superpowers/specs/
# 2026-09-28-tenant-envelope-encryption-design.md Sec4) that wraps every
# tenant's DEK in `keystore.tenant_encryption_keys` -- run BEFORE
# enabling `waddles.core.tenant-envelope-encryption`, and again whenever
# rotating the KEK (a KEK rotation re-wraps every tenant's DEK in place;
# see the spec's rotation section -- this script only produces the new
# key material, the re-wrap job is separate).
#
# The key NEVER touches stdout, a file inside this repo, or a CLI argument --
# it lives only in a 0600 temp file for the lifetime of this script, shredded
# on exit (success, failure, or interrupt) by the trap below. Mirrors
# scripts/generate-bundle-signing-key.sh's (PR #431) safe-generation pattern.
#
# Requires: openssl, kubectl.
#
# Usage:
#   scripts/generate-tenant-kek.sh <kube-context> <namespace> [secret-name]
#
# Example:
#   scripts/generate-tenant-kek.sh dal2-beta waddlebot

set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <kube-context> <namespace> [secret-name]" >&2
    exit 1
fi

KUBE_CONTEXT="$1"
NAMESPACE="$2"
SECRET_NAME="${3:-waddlebot-tenant-kek}"

command -v openssl > /dev/null 2>&1 || { echo "ERROR: openssl not found" >&2; exit 1; }
command -v kubectl > /dev/null 2>&1 || { echo "ERROR: kubectl not found" >&2; exit 1; }

# Key material lives only in this directory, mode 0700, for the life of this
# script. TMPDIR (not /tmp directly) respects an already-more-private tmp
# mount if one is configured; falls back to /tmp otherwise.
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/tenant-kek.XXXXXX")"
chmod 700 "$WORKDIR"

cleanup() {
    # shred overwrites before unlink; -u removes the file; -f ignores a file
    # that was never created (e.g. openssl failed before writing it) rather
    # than erroring out of the trap. Falls back to a plain rm if shred is
    # unavailable (e.g. some minimal container base images).
    if command -v shred > /dev/null 2>&1; then
        shred -u -f "$WORKDIR"/*.bin "$WORKDIR"/*.hex 2>/dev/null || true
    fi
    rm -rf "$WORKDIR"
}
trap cleanup EXIT INT TERM

KEK_BIN="$WORKDIR/kek.bin"
KEK_HEX_FILE="$WORKDIR/kek.hex"

umask 077

# 256-bit (32-byte) CSPRNG key -- AES-256-GCM key length required by
# K8sSecretKekProvider._key() in hub_api/services/tenant_keystore.py.
openssl rand -out "$KEK_BIN" 32
xxd -p -c 64 "$KEK_BIN" | tr -d '\n' > "$KEK_HEX_FILE"

kubectl create secret generic "$SECRET_NAME" \
    --context "$KUBE_CONTEXT" \
    --namespace "$NAMESPACE" \
    --from-file=TENANT_KEK_HEX="$KEK_HEX_FILE" \
    --dry-run=client -o yaml \
    | kubectl apply --context "$KUBE_CONTEXT" -f -

echo ""
echo "Secret '${SECRET_NAME}' created/updated in namespace '${NAMESPACE}' on context '${KUBE_CONTEXT}'."
echo "hub-api must mount TENANT_KEK_HEX from this Secret -- key material never printed above."
