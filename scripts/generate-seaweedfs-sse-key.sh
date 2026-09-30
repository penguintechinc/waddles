#!/bin/bash
# generate-seaweedfs-sse-key.sh
#
# Generates a SeaweedFS S3-gateway SSE-S3 KEK (key-encryption-key), WEED_S3_SSE_KEK,
# a 64-lowercase-hex-char (256-bit) key, and creates/updates it as a Kubernetes
# Secret, without ever printing the key material or passing it as a CLI argument.
#
# At-rest encryption is mandatory (security.md Encryption: Storage). Without a
# configured KEK, SeaweedFS auto-generates one and persists it on the filer --
# acceptable for throwaway alpha, not acceptable for beta/gamma/production
# where losing the filer volume must not silently rotate the encryption key.
# This secret must exist and infrastructure.seaweedfs.encryption.secretName in
# the waddlebot Helm chart must point at it before those tiers deploy.
#
# Replaces scripts/generate-minio-kms-key.sh (MinIO's static-KMS key, format
# <key-name>:<base64 32 bytes>) now that the object store is SeaweedFS --
# the key FORMAT changed too (hex, not label:base64), so an existing minio-kms
# Secret cannot simply be renamed/reused; run this script to create a fresh one.
#
# For production, a real external KMS (KES/Vault) fronting envelope encryption
# is recommended over this static-key mechanism -- see
# k8s/helm/waddlebot/README.md SeaweedFS Encryption section and
# ~/.claude/rules/security.md Encryption tiering. This script is intended for
# alpha/beta/gamma and as a stopgap for production until KES/Vault lands.
#
# Usage:
#   ./scripts/generate-seaweedfs-sse-key.sh [OPTIONS]
#
# Options:
#   --context CONTEXT       kubectl context (required; no default -- never guess the cluster)
#   --namespace NAMESPACE   Target namespace (default: waddlebot)
#   --secret-name NAME      Secret name to create (default: seaweedfs-sse-kek)
#   -h, --help              Show this help message
#
# Environment (equivalent to the flags above, flags take precedence):
#   KUBE_CONTEXT, NAMESPACE, SECRET_NAME

set -euo pipefail

umask 077

NAMESPACE="${NAMESPACE:-waddlebot}"
SECRET_NAME="${SECRET_NAME:-seaweedfs-sse-kek}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"

usage() {
    sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --context)
            KUBE_CONTEXT="$2"
            shift 2
            ;;
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --secret-name)
            SECRET_NAME="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if [ -z "$KUBE_CONTEXT" ]; then
    echo "ERROR: --context (or KUBE_CONTEXT env var) is required -- refusing to guess the target cluster" >&2
    exit 1
fi

if ! command -v kubectl > /dev/null 2>&1; then
    echo "ERROR: kubectl not found on PATH" >&2
    exit 1
fi

KEY_FILE="$(mktemp "${TMPDIR:-/tmp}/seaweedfs-sse-key.XXXXXX")"
cleanup() {
    # Best-effort secure erase; shred may be unavailable (e.g. macOS) -- fall
    # back to overwrite+rm rather than leaving key material on disk.
    if command -v shred > /dev/null 2>&1; then
        shred -u "$KEY_FILE" 2> /dev/null || rm -f "$KEY_FILE"
    else
        rm -f "$KEY_FILE"
    fi
}
trap cleanup EXIT INT TERM

# Ensure namespace exists
kubectl --context "$KUBE_CONTEXT" get namespace "$NAMESPACE" > /dev/null 2>&1 || \
    kubectl --context "$KUBE_CONTEXT" create namespace "$NAMESPACE"

# 32 random bytes, hex-encoded (SeaweedFS's WEED_S3_SSE_KEK / s3.sse.kek
# requires lowercase hex, never base64) -- key material never touches argv or
# an interactive shell buffer.
openssl rand -hex 32 | tr -d '\n' > "$KEY_FILE"

kubectl create secret generic "$SECRET_NAME" \
    --context "$KUBE_CONTEXT" \
    --namespace "$NAMESPACE" \
    --from-file=WEED_S3_SSE_KEK="$KEY_FILE" \
    --dry-run=client -o yaml \
    | kubectl apply --context "$KUBE_CONTEXT" -f -

echo "Secret '${SECRET_NAME}' (key WEED_S3_SSE_KEK) applied in namespace '${NAMESPACE}' on context '${KUBE_CONTEXT}'."
echo "Set infrastructure.seaweedfs.encryption.secretName=${SECRET_NAME} in the target environment's values file/override to use it."
