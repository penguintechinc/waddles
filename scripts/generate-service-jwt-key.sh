#!/usr/bin/env bash
# Generates the Ed25519 signing keypair for hub-api's per-service machine
# JWTs (flask_core.service_jwt / core/service_auth) and creates the k8s
# Secret directly from it -- the private key is never printed to stdout,
# never passed as a CLI arg (argv is world-readable via /proc/<pid>/cmdline
# and `ps` on a shared host), and never held in a shell variable that could
# be exported to argv or the environment of a child process. It touches
# disk for exactly as long as `kubectl create secret --from-file` needs to
# read it, in a `chmod 700` per-invocation temp directory, and is always
# shredded on exit (success, failure, or signal) via the `trap`. Mirrors
# the safe-generation pattern used for bundle artifact signing
# (scripts/generate-bundle-signing-key.sh, PR #431).
#
# Usage: KID=20260928 NAMESPACE=waddlebot ./scripts/generate-service-jwt-key.sh
set -euo pipefail

KID="${KID:?set KID to a short rotation identifier, e.g. the generation date}"
# fix/hub-api-grpc-service-jwt-issuer -- KID becomes the
# `SERVICE_JWT_PRIVATE_KEY_<KID>` SECRET KEY NAME below, which reaches hub-api as
# an env var of the identical name via the chart's `envFrom.secretRef`. A hyphen
# (or any non-POSIX-identifier character) makes that an invalid env var name --
# kubelet silently drops it, and hub-api's `load_issuer_from_env` never sees it.
# Fail loudly here instead of producing a Secret hub-api can never parse.
if ! [[ "${KID}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  echo "KID='${KID}' must match [A-Za-z_][A-Za-z0-9_]* (it becomes an env var name suffix, e.g. use 20260928 not 2026-09-28)" >&2
  exit 1
fi
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

# umask 077 belt-and-suspenders on top of `mktemp -d`'s own 0700 dir --
# any file created under it inherits non-readable-by-others permissions
# even if a step below forgets to chmod explicitly.
umask 077

tmpdir="$(mktemp -d)"
key_file="${tmpdir}/private.pem"

cleanup() {
  # `shred -u` overwrites before unlinking -- a plain `rm` leaves recoverable
  # plaintext on disk until the block is reused. `|| true` on both: if
  # openssl never got to write the file (early failure), shred has nothing
  # to shred, and that must not mask this trap's own cleanup of tmpdir.
  if [[ -f "${key_file}" ]]; then
    shred -u "${key_file}" || true
  fi
  rm -rf "${tmpdir}"
}
trap cleanup EXIT INT TERM

# Writes PKCS8 PEM straight to the temp file -- the private key material
# never passes through a shell variable (which `ps`/`/proc` can expose via
# argv, or a stray `echo`/`set -x` could leak) at any point in this script.
openssl genpkey -algorithm ed25519 -out "${key_file}"
pub_key="$(openssl pkey -in "${key_file}" -pubout)"

# `--from-file=<VAR_NAME>=<path>` reads the file's contents directly;
# kubectl never receives the key material as a CLI argument, so it never
# appears in this process's argv either.
kubectl create secret generic "${SECRET_NAME}" \
  --namespace "${NAMESPACE}" \
  --from-literal="SERVICE_JWT_ACTIVE_KID=${KID}" \
  --from-file="SERVICE_JWT_PRIVATE_KEY_${KID}=${key_file}" \
  --dry-run=client -o yaml \
  | kubectl apply -f -

echo "Secret ${SECRET_NAME} (kid=${KID}) applied in namespace ${NAMESPACE}."
echo "Public key (safe to commit to values.yaml under serviceJwt.publicKeys.${KID}):"
echo "${pub_key}"
