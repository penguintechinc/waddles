#!/bin/bash
# generate-minio-kms-key.sh
#
# Generates a MinIO static single-key KMS secret (MINIO_KMS_SECRET_KEY, format
# <key-name>:<base64 32 bytes>) and creates/updates it as a Kubernetes Secret,
# without ever printing the key material or passing it as a CLI argument.
#
# At-rest encryption is mandatory (security.md Encryption: Storage) -- MinIO
# refuses ServerSideEncryption="AES256" put_object calls with "Server side
# encryption specified but KMS is not configured" until this secret exists and
# infrastructure.minio.kms.secretName in the waddlebot Helm chart points at it.
#
# For production, a real external KMS (KES/Vault) is recommended over this
# static-key mechanism -- see k8s/helm/waddlebot/README.md MinIO KMS section
# and ~/.claude/rules/security.md Encryption tiering. This script is intended
# for alpha/beta/gamma and as a stopgap for production until KES/Vault lands.
#
# Usage:
#   ./scripts/generate-minio-kms-key.sh [OPTIONS]
#
# Options:
#   --context CONTEXT       kubectl context (required; no default -- never guess the cluster)
#   --namespace NAMESPACE   Target namespace (default: waddlebot)
#   --secret-name NAME      Secret name to create (default: minio-kms)
#   --key-name KEY_NAME     KMS key label used in the <key-name>:<base64> value (default: waddlebot-minio)
#   -h, --help              Show this help message
#
# Environment (equivalent to the flags above, flags take precedence):
#   KUBE_CONTEXT, NAMESPACE, SECRET_NAME, KMS_KEY_NAME

set -euo pipefail

umask 077

NAMESPACE="${NAMESPACE:-waddlebot}"
SECRET_NAME="${SECRET_NAME:-minio-kms}"
KMS_KEY_NAME="${KMS_KEY_NAME:-waddlebot-minio}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"

usage() {
    sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
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
        --key-name)
            KMS_KEY_NAME="$2"
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

KEY_FILE="$(mktemp "${TMPDIR:-/tmp}/minio-kms-key.XXXXXX")"
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

# 32 random bytes, base64-encoded, assembled into MinIO's static KMS key
# format entirely inside the temp file -- key material never touches argv or
# an interactive shell buffer.
openssl rand -base64 32 > "$KEY_FILE"
printf '%s:' "$KMS_KEY_NAME" | cat - "$KEY_FILE" | tr -d '\n' > "${KEY_FILE}.fmt"
mv "${KEY_FILE}.fmt" "$KEY_FILE"

kubectl create secret generic "$SECRET_NAME" \
    --context "$KUBE_CONTEXT" \
    --namespace "$NAMESPACE" \
    --from-file=MINIO_KMS_SECRET_KEY="$KEY_FILE" \
    --dry-run=client -o yaml \
    | kubectl apply --context "$KUBE_CONTEXT" -f -

echo "Secret '${SECRET_NAME}' (key MINIO_KMS_SECRET_KEY) applied in namespace '${NAMESPACE}' on context '${KUBE_CONTEXT}'."
echo "Set infrastructure.minio.kms.secretName=${SECRET_NAME} in the target environment's values file/override to use it."
