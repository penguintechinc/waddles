#!/usr/bin/env bash
# Real-path test of core/bundle_host_storage against a throwaway local SeaweedFS.
# Fails if the container does not start or if 0 tests ran (zero examined = FAIL).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMG="chrislusf/seaweedfs@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d"
NAME="bundle-storage-sw-$$"
PORT="${BUNDLE_STORAGE_TEST_PORT:-18333}"
TMP="$(mktemp -d)"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT
AK="testak$$"; SK="testsk$$secret"
cat >"$TMP/s3.json" <<JSON
{"identities":[{"name":"t","credentials":[{"accessKey":"$AK","secretKey":"$SK"}],"actions":["Admin","Read","Write","List","Tagging"]}]}
JSON
chmod 644 "$TMP/s3.json"; mkdir "$TMP/data"
docker run -d --name "$NAME" --user "$(id -u):$(id -g)" -p "127.0.0.1:${PORT}:8333" \
  -v "$TMP:/cfg:ro" -v "$TMP/data:/data" "$IMG" server -s3 -s3.config=/cfg/s3.json -dir=/data >/dev/null
ready=0
for _ in $(seq 1 90); do
  if curl -s -o /dev/null "http://127.0.0.1:${PORT}/"; then ready=1; break; fi; sleep 1
done
[ "$ready" = 1 ] || { docker logs "$NAME" 2>&1 | tail -20; echo "FAIL: SeaweedFS did not start"; exit 1; }
sleep 5
export BUNDLE_STORAGE_TEST_ENDPOINT="http://127.0.0.1:${PORT}" BUNDLE_STORAGE_TEST_BUCKET="bundle-storage-test" \
  BUNDLE_STORAGE_TEST_ACCESS_KEY="$AK" BUNDLE_STORAGE_TEST_SECRET_KEY="$SK"
# create bucket with a signed request via aws-cli image
docker run --rm --network host -e AWS_ACCESS_KEY_ID="$AK" -e AWS_SECRET_ACCESS_KEY="$SK" \
  docker.io/amazon/aws-cli:2.37.6@sha256:82905aa8fdab6e2403b40dceec12dc33aea2535a4c2a6f13d596d98501951105 \
  --endpoint-url "$BUNDLE_STORAGE_TEST_ENDPOINT" --region us-east-1 s3api create-bucket --bucket "$BUNDLE_STORAGE_TEST_BUCKET" >/dev/null
OUT="$(cd "$ROOT/core/bundle_host_storage" && cargo test --test seaweedfs_real_path -- --ignored 2>&1)" || { echo "$OUT"; exit 1; }
echo "$OUT" | grep -E "^test result"
echo "$OUT" | grep -qE "test result: ok\. [1-9][0-9]* passed" || { echo "FAIL: zero real-path tests ran"; exit 1; }
