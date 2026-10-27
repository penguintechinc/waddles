#!/usr/bin/env bash
# Self-test for scripts/ci/supply-chain.sh against a throwaway local registry (key-based mode).
#
# Positive: sign, SBOM, attest and verify succeed on a real image.
# Negative: an unsigned image, a wrong-key signature, a zero-package SBOM and a
# tag (non-digest) ref must each FAIL. A check that cannot fail is broken, so every
# negative case is asserted, not assumed.
#
# Requires cosign, syft, jq, docker and openssl on PATH; missing tools fail loudly.
# Ephemeral key pairs live in a 0700 temp dir and are removed on exit (never committed).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SC="${HERE}/supply-chain.sh"
REG_IMAGE="registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
BASE_IMAGE="debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587"
EXPECTED_CHECKS=8

for tool in cosign syft jq docker openssl curl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "FAIL: required tool '${tool}' not on PATH" >&2; exit 2; }
done

WORK="$(mktemp -d)"
chmod 700 "$WORK"
REG="sc-selftest-reg-$$"
PASSED=0
RUN=0

cleanup() {
  local rc=$?
  docker rm -f "$REG" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit "$rc"
}
trap cleanup EXIT

# check <label> <expect: pass|fail> <cmd...>: runs cmd, asserts outcome, counts it.
check() {
  local label="$1" expect="$2" rc=0
  shift 2
  RUN=$((RUN + 1))
  "$@" >"${WORK}/last.log" 2>&1 || rc=$?
  if { [ "$expect" = pass ] && [ "$rc" -eq 0 ]; } || { [ "$expect" = fail ] && [ "$rc" -ne 0 ]; }; then
    PASSED=$((PASSED + 1))
    echo "PASS [${RUN}/${EXPECTED_CHECKS}] ${label}"
  else
    echo "FAIL [${RUN}/${EXPECTED_CHECKS}] ${label} (expected ${expect}, exit ${rc})" >&2
    sed 's/^/  | /' "${WORK}/last.log" >&2
    exit 1
  fi
}

docker run -d --name "$REG" -p 127.0.0.1::5000 "$REG_IMAGE" >/dev/null
PORT="$(docker port "$REG" 5000/tcp | head -n 1 | sed 's/.*://')"
HOST="127.0.0.1:${PORT}"
for _ in $(seq 1 30); do
  curl -fsS "http://${HOST}/v2/" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://${HOST}/v2/" >/dev/null || { echo "FAIL: local registry not ready on ${HOST}" >&2; exit 1; }

export COSIGN_ALLOW_HTTP_REGISTRY=true
export COSIGN_PASSWORD=""
export SYFT_REGISTRY_INSECURE_USE_HTTP=true

cosign generate-key-pair --output-key-prefix "${WORK}/signer" >/dev/null 2>&1
cosign generate-key-pair --output-key-prefix "${WORK}/intruder" >/dev/null 2>&1

docker pull "$BASE_IMAGE" >/dev/null
docker tag "$BASE_IMAGE" "${HOST}/selftest/base:ok"
docker push "${HOST}/selftest/base:ok" >/dev/null

# Second image with a distinct digest (never signed) -- a real layer added on top of the base.
mkdir -p "${WORK}/unsigned-ctx"
printf 'FROM %s\nRUN echo unsigned > /selftest-unsigned.txt\n' "$BASE_IMAGE" > "${WORK}/unsigned-ctx/Dockerfile"
docker build -q -t "${HOST}/selftest/unsigned:v1" "${WORK}/unsigned-ctx" >/dev/null
docker push "${HOST}/selftest/unsigned:v1" >/dev/null

# Zero-package image: scratch + one plain-text file that no syft cataloger recognises
# (binary blobs are classified as packages by syft, so they would not be zero-package).
mkdir -p "${WORK}/empty-ctx"
printf 'FROM scratch\nCOPY notes.txt /notes.txt\n' > "${WORK}/empty-ctx/Dockerfile"
printf 'no packages here\n' > "${WORK}/empty-ctx/notes.txt"
docker build -q -t "${HOST}/selftest/empty:v1" "${WORK}/empty-ctx" >/dev/null
docker push "${HOST}/selftest/empty:v1" >/dev/null

OK_REF="${HOST}/selftest/base@$("$SC" digest "${HOST}/selftest/base" ok)"
UNSIGNED_REF="${HOST}/selftest/unsigned@$("$SC" digest "${HOST}/selftest/unsigned" v1)"
EMPTY_REF="${HOST}/selftest/empty@$("$SC" digest "${HOST}/selftest/empty" v1)"
SBOM="${WORK}/sbom.spdx.json"

# 1. positive: sign the real image with the ephemeral signer key.
check "sign pinned digest with signer key" pass \
  env COSIGN_KEY="${WORK}/signer.key" "$SC" sign "$OK_REF"
# 2. positive: SBOM lists >=1 package.
check "syft SBOM lists >=1 package" pass "$SC" sbom "$OK_REF" "$SBOM"
# 3. positive: attach SBOM as SPDX attestation.
check "attest SPDX SBOM with signer key" pass \
  env COSIGN_KEY="${WORK}/signer.key" "$SC" attest "$OK_REF" "$SBOM"
# 4. positive: verify passes (signature + attestation with packages).
check "verify signed image with signer public key" pass \
  env COSIGN_PUB_KEY="${WORK}/signer.pub" "$SC" verify "$OK_REF"
# 5. negative: an unsigned image must not verify.
check "verify rejects unsigned image" fail \
  env COSIGN_PUB_KEY="${WORK}/signer.pub" "$SC" verify "$UNSIGNED_REF"
# 6. negative: a signature made by a different key must not verify.
env COSIGN_KEY="${WORK}/intruder.key" "$SC" sign "$UNSIGNED_REF" >/dev/null
check "verify rejects signature from wrong key" fail \
  env COSIGN_PUB_KEY="${WORK}/signer.pub" "$SC" verify "$UNSIGNED_REF"
# 7. negative: a zero-package SBOM must be refused.
check "sbom refuses zero-package image" fail "$SC" sbom "$EMPTY_REF" "${WORK}/empty-sbom.json"
# 8. negative: mutable tag refs are refused for verify.
check "verify refuses non-digest tag ref" fail \
  env COSIGN_PUB_KEY="${WORK}/signer.pub" "$SC" verify "${HOST}/selftest/base:ok"

if [ "$PASSED" -ne "$EXPECTED_CHECKS" ]; then
  echo "FAIL: supply-chain self-test ${PASSED}/${EXPECTED_CHECKS}" >&2
  exit 1
fi
echo "supply-chain self-test: ${PASSED}/${EXPECTED_CHECKS} checks passed"
