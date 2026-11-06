#!/usr/bin/env bash
# Runs tests/e2e_real_media.rs against REAL ffmpeg/ffprobe (ffmpeg 5.1.9, same
# digest-pinned bookworm base as Dockerfile.rust) executed in a container via thin
# wrappers, so it needs only docker + a Rust toolchain -- no host ffmpeg.
# Used by CI (.github/workflows/rust-svc-streaming.yml) and locally.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRATE="$(cd "$HERE/../.." && pwd)"
RUN_ID="e2e-$$-$(date +%s)"
IMAGE="svc-streaming-e2e-ffmpeg:${RUN_ID}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/svc-streaming-real-media.XXXXXX")"
LABEL="svc-streaming-real-media-run=${RUN_ID}"

cleanup() {
  # shellcheck disable=SC2046
  docker rm -f $(docker ps -aq --filter "label=${LABEL}") >/dev/null 2>&1 || true
  docker rmi -f "${IMAGE}" >/dev/null 2>&1 || true
  rm -rf "${WORK}"
}
trap cleanup EXIT

docker build -q -f "${HERE}/Dockerfile.ffmpeg" -t "${IMAGE}" "${HERE}" >/dev/null

# The wrapper mounts WORK at the identical path so paths the test passes to ffmpeg
# resolve unchanged. --init reaps ffmpeg; stdin stays open (-i) because the service
# feeds FLV on pipe:0 and stops ffmpeg by writing `q`.
for tool in ffmpeg ffprobe; do
  cat >"${WORK}/${tool}" <<WRAP
#!/bin/sh
exec docker run --rm -i --init --network none --label "${LABEL}" \\
  --user "$(id -u):$(id -g)" -v "${WORK}:${WORK}" "${IMAGE}" ${tool} "\$@"
WRAP
  chmod 0755 "${WORK}/${tool}"
done

export SVC_STREAMING_REAL_FFMPEG="${WORK}/ffmpeg"
export SVC_STREAMING_REAL_FFPROBE="${WORK}/ffprobe"
export SVC_STREAMING_REAL_WORKDIR="${WORK}"

cd "${CRATE}"
cargo test --locked --features real-media-e2e --test e2e_real_media -- --nocapture "$@"
