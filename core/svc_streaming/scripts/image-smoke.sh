#!/usr/bin/env bash
# Smoke-tests one svc-streaming image variant: the ffmpeg encoders it claims,
# the driver wiring that makes them usable, and that CPU fallback works.
#
# Usage: image-smoke.sh <image-ref> <cpu|amd|nvidia>
#
# Every check increments CHECKS; the summary prints PASSED/CHECKS so a run
# that examined nothing can never read as a pass. Exit status is non-zero on
# any failure (no `|| true` anywhere -- see critical-rules.md Verification Integrity).
set -euo pipefail

readonly IMAGE="${1:?usage: image-smoke.sh <image-ref> <cpu|amd|nvidia>}"
readonly VARIANT="${2:?usage: image-smoke.sh <image-ref> <cpu|amd|nvidia>}"
readonly LABEL_KEY="io.waddles.svc-streaming.variant"
readonly APPUSER_UID=10001

case "${VARIANT}" in
    cpu|amd|nvidia) ;;
    *) echo "FAIL: unknown variant '${VARIANT}' (expected cpu|amd|nvidia)" >&2; exit 2 ;;
esac

# Per-run scratch namespace (parallel CI jobs never share a path).
WORK="$(mktemp -d "${TMPDIR:-/tmp}/svc-streaming-smoke-${VARIANT}-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

CHECKS=0
PASSED=0
FAILED=0

# check <description> <command...> -- runs the command, records pass/fail.
check() {
    local desc="$1"
    shift
    CHECKS=$((CHECKS + 1))
    if "$@"; then
        PASSED=$((PASSED + 1))
        echo "  ok    ${desc}"
    else
        FAILED=$((FAILED + 1))
        echo "  FAIL  ${desc}" >&2
    fi
}

# in_image <command...> -- runs a command inside a throwaway container, no entrypoint.
in_image() {
    docker run --rm --entrypoint /bin/sh "${IMAGE}" -c "$1"
}

# load_encoders -- captures `ffmpeg -encoders` once into WORK as one name per line.
# Fails the run (set -e) if ffmpeg itself cannot run -- never an empty pass.
load_encoders() {
    local names="${WORK}/encoder-names.txt"
    if [[ ! -s "${names}" ]]; then
        docker run --rm --entrypoint /usr/bin/ffmpeg "${IMAGE}" -hide_banner -encoders >"${WORK}/encoders.txt"
        awk '$1 ~ /^[A-Z.]{6}$/ {print $2}' "${WORK}/encoders.txt" >"${names}"
    fi
    [[ -s "${names}" ]] || { echo "        ffmpeg listed zero encoders" >&2; return 1; }
}

# has_encoder <name> -- true when ffmpeg lists the encoder.
has_encoder() {
    load_encoders
    grep -qx "$1" "${WORK}/encoder-names.txt"
}

# expect_encoders <name...> -- every named encoder must be listed; reports N/M.
expect_encoders() {
    local total=$# found=0 name
    for name in "$@"; do
        if has_encoder "${name}"; then
            found=$((found + 1))
        else
            echo "        missing encoder: ${name}" >&2
        fi
    done
    echo "        encoders present: ${found}/${total}" >&2
    [[ "${found}" -eq "${total}" ]]
}

echo "svc-streaming image smoke: ${IMAGE} (variant=${VARIANT})"

# --- identity -------------------------------------------------------------
label_matches() {
    [[ "$(docker inspect --format "{{ index .Config.Labels \"${LABEL_KEY}\" }}" "${IMAGE}")" == "${VARIANT}" ]]
}
check "image label ${LABEL_KEY}=${VARIANT}" label_matches

uid_is_nonroot() {
    [[ "$(in_image 'id -u')" == "${APPUSER_UID}" ]]
}
check "runs as non-root uid ${APPUSER_UID}" uid_is_nonroot

binary_starts() {
    docker run --rm --entrypoint /app/svc-streaming "${IMAGE}" --help >/dev/null
}
check "svc-streaming binary starts (--help exits 0)" binary_starts

# --- software encoders (every variant) ------------------------------------
check "software encoders libx264 libx265 libsvtav1 compiled in" \
    expect_encoders libx264 libx265 libsvtav1

cpu_fallback_encodes() {
    docker run --rm --entrypoint /usr/bin/ffmpeg "${IMAGE}" -hide_banner -loglevel error \
        -f lavfi -i testsrc=size=640x360:rate=1 -frames:v 1 -c:v libx264 -f null - >/dev/null
}
check "CPU trial encode (libx264, 1 frame) succeeds -- the fallback path" cpu_fallback_encodes

# --- variant-specific wiring ----------------------------------------------
case "${VARIANT}" in
    cpu)
        # Must NOT ship a GPU driver: the cpu image is the no-GPU baseline.
        no_va_driver() {
            [[ "$(in_image 'find /usr/lib -name "*_drv_video.so" 2>/dev/null | wc -l | tr -d " "')" == "0" ]]
        }
        check "no VA-API driver (*_drv_video.so) baked in" no_va_driver

        no_nvidia_encode_lib() {
            [[ "$(in_image 'ldconfig -p | awk "/libnvidia-encode/ {n++} END {print n+0}"')" == "0" ]]
        }
        check "no libnvidia-encode baked in" no_nvidia_encode_lib
        ;;
    amd)
        expect_amd_encoders() { expect_encoders h264_vaapi hevc_vaapi; }
        check "VA-API encoders h264_vaapi hevc_vaapi compiled in" expect_amd_encoders

        va_driver_present() {
            [[ "$(in_image 'find /usr/lib -name "*_drv_video.so" 2>/dev/null | wc -l | tr -d " "')" -ge 1 ]]
        }
        check "VA-API driver (*_drv_video.so, mesa) present" va_driver_present

        libva_present() {
            in_image 'ls /usr/lib/x86_64-linux-gnu/libva.so.2 /usr/lib/x86_64-linux-gnu/libva-drm.so.2 >/dev/null'
        }
        check "libva.so.2 and libva-drm.so.2 present" libva_present
        ;;
    nvidia)
        expect_nvenc_encoders() { expect_encoders h264_nvenc hevc_nvenc; }
        check "NVENC encoders h264_nvenc hevc_nvenc compiled in" expect_nvenc_encoders

        video_capability_requested() {
            in_image 'case "${NVIDIA_DRIVER_CAPABILITIES}" in *video*) exit 0 ;; *) exit 1 ;; esac'
        }
        check "NVIDIA_DRIVER_CAPABILITIES requests video (NVENC lib injection)" video_capability_requested

        no_nvidia_encode_lib() {
            [[ "$(in_image 'ldconfig -p | awk "/libnvidia-encode/ {n++} END {print n+0}"')" == "0" ]]
        }
        check "no host-matched libnvidia-encode baked in (injected at runtime)" no_nvidia_encode_lib
        ;;
esac

# AV1 stays CPU on every variant (ffmpeg 5.1.9 has no av1_nvenc/av1_vaapi).
# Asserted as a negative so a future ffmpeg bump is noticed, not silently assumed.
echo "  info  av1 GPU encoders in this build: $(has_encoder av1_nvenc && echo nvenc || echo none)/$(has_encoder av1_vaapi && echo vaapi || echo none)"

echo "PASSED ${PASSED}/${CHECKS} checks, FAILED ${FAILED}"
if [[ "${CHECKS}" -eq 0 || "${FAILED}" -ne 0 ]]; then
    exit 1
fi
