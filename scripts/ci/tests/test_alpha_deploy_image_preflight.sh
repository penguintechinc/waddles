#!/usr/bin/env bash
# Regression test for fix/alpha-deploy-hub-webui.
#
# Asserts scripts/alpha-deploy.sh's check_images_in_registry preflight:
#   1. renders the chart (stubbed `helm template`) and HEAD-checks every
#      localhost:32000/waddlebot/* image:tag it finds against the registry
#      (stubbed `curl`) BEFORE any helm upgrade/install step runs;
#   2. fails the whole script, before touching helm upgrade/install, if any
#      referenced image is missing (the hub-webui ImagePullBackOff this fix
#      addresses);
#   3. fails if the rendered chart yields zero matching images (denominator
#      guard -- critical-rules.md Verification Integrity: zero examined is a
#      FAIL, never a silent pass);
#   4. passes through to helm upgrade/install when every referenced image is
#      present;
#   5. sends a single combined Accept header listing every valid manifest
#      media type (OCI image index, OCI image manifest, docker manifest
#      list, docker v2 manifest) -- buildx pushes OCI image indexes, so a
#      docker-v2-only Accept header 404s a real, present image;
#   6. treats an image that exists ONLY as an OCI image index (no legacy
#      docker v2 manifest) as present.
#
# docker/helm/kubectl/curl are all stubbed -- no real registry, cluster, or
# build ever runs.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
SCRIPT_UNDER_TEST="$REPO_ROOT/scripts/alpha-deploy.sh"

cases_passed=0
cases_failed=0

STUB_DIR="$(mktemp -d)"
LOG_FILE="$(mktemp)"
OUT_FILE="$(mktemp)"
# shellcheck disable=SC2317  # invoked indirectly via trap
cleanup() { rm -rf "$STUB_DIR" "$LOG_FILE" "$OUT_FILE"; }
trap cleanup EXIT

# --- kubectl stub: always local-alpha, always succeeds ----------------------
cat > "$STUB_DIR/kubectl" <<'EOS'
#!/usr/bin/env bash
if [ "$1 $2" = "config current-context" ]; then echo local-alpha; fi
exit 0
EOS

# --- docker stub: build/push/inspect all succeed -----------------------------
cat > "$STUB_DIR/docker" <<'EOS'
#!/usr/bin/env bash
if [ "$1" = "inspect" ]; then
    # revision-label verification step reads this back -- must match the SHA
    # alpha-deploy.sh computes from git rev-parse HEAD.
    git rev-parse HEAD
    exit 0
fi
exit 0
EOS

# --- helm stub: `template` renders two fake chart images; everything else
#     (lint/dry-run/upgrade/wait) just logs and succeeds. -------------------
cat > "$STUB_DIR/helm" <<EOS
#!/usr/bin/env bash
echo "STUB-CALLED helm \$*" >> "$LOG_FILE"
if [ "\$1" = "template" ]; then
    cat <<'YAML'
---
# Source: waddlebot/templates/hub-api.yaml
    containers:
      - name: hub-api
        image: "localhost:32000/waddlebot/hub-api:faketag"
---
# Source: waddlebot/templates/hub-webui.yaml
    containers:
      - name: hub-webui
        image: "localhost:32000/waddlebot/hub-webui:faketag"
YAML
    exit 0
fi
exit 0
EOS

# --- curl stub: registry v2 manifest requests. The last argument is the
#     URL; MOCK_MISSING_REPO (if set) makes that one repo 404, all others
#     200. MOCK_ZERO_IMAGES is handled by the helm stub above (n/a here).
#     MOCK_OCI_INDEX_ONLY_REPO models a registry image pushed by buildx as
#     an OCI image index ONLY (no legacy docker v2 manifest entry) -- it
#     must still 200 when queried with a combined Accept header.
#
#     Regression guard: buildx pushes OCI image indexes, so a single
#     docker-v2-only Accept header 404s a real, present image (the bug
#     this test protects against). Any request missing one of the four
#     required manifest media types in its Accept header is treated as
#     malformed and gets "000" -- it must never reach a real registry.
cat > "$STUB_DIR/curl" <<'EOS'
#!/usr/bin/env bash
echo "STUB-CALLED curl $*" >> "$LOG_FILE"
url=""
accept=""
prev=""
for a in "$@"; do
    if [ "$prev" = "-H" ]; then
        accept="$a"
    fi
    prev="$a"
    url="$a"
done

for m in \
    "application/vnd.oci.image.index.v1+json" \
    "application/vnd.oci.image.manifest.v1+json" \
    "application/vnd.docker.distribution.manifest.list.v2+json" \
    "application/vnd.docker.distribution.manifest.v2+json"
do
    case "$accept" in
        *"$m"*) ;;
        *) echo "000"; exit 0 ;;
    esac
done

if [ -n "${MOCK_MISSING_REPO:-}" ]; then
    case "$url" in
        */"${MOCK_MISSING_REPO}"/manifests/*) echo "404"; exit 0 ;;
    esac
fi

if [ -n "${MOCK_OCI_INDEX_ONLY_REPO:-}" ]; then
    case "$url" in
        */"${MOCK_OCI_INDEX_ONLY_REPO}"/manifests/*)
            case "$accept" in
                *"application/vnd.oci.image.index.v1+json"*) echo "200"; exit 0 ;;
                *) echo "404"; exit 0 ;;
            esac
            ;;
    esac
fi

echo "200"
EOS

chmod +x "$STUB_DIR"/kubectl "$STUB_DIR"/docker "$STUB_DIR"/helm "$STUB_DIR"/curl

# Preserve the real (two-image) helm stub content so it can be restored
# after the "zero images" case below replaces $STUB_DIR/helm with a symlink
# -- without this, every case run after the zero-images case would silently
# inherit the empty-image stub instead of the real one.
cp "$STUB_DIR/helm" "$STUB_DIR/helm-real"

run_case() {
    # run_case <label> <MOCK_MISSING_REPO_or_empty> <MOCK_ZERO_IMAGES_or_empty> <expect_exit_zero:0|1> [<MOCK_OCI_INDEX_ONLY_REPO_or_empty>]
    local label="$1" missing_repo="$2" zero_images="$3" expect_success="$4" oci_index_only_repo="${5:-}"
    : > "$LOG_FILE"
    : > "$OUT_FILE"
    echo ""
    echo "Test: $label"

    local helm_bin="$STUB_DIR/helm"
    if [ "$zero_images" = "1" ]; then
        # Swap in a helm stub whose `template` renders no matching images at all
        # -- exercises the "zero images found" denominator guard.
        helm_bin="$STUB_DIR/helm-empty"
        cat > "$helm_bin" <<EOS
#!/usr/bin/env bash
echo "STUB-CALLED helm-empty \$*" >> "$LOG_FILE"
if [ "\$1" = "template" ]; then
    echo "---"
    echo "# no images here"
    exit 0
fi
exit 0
EOS
        chmod +x "$helm_bin"
        ln -sf "$helm_bin" "$STUB_DIR/helm"
    else
        # Restore the real two-image helm stub -- a prior case may have left
        # $STUB_DIR/helm symlinked to helm-empty (see zero-images branch
        # above); without this restore, every subsequent case would silently
        # render zero images too.
        rm -f "$STUB_DIR/helm"
        cp "$STUB_DIR/helm-real" "$STUB_DIR/helm"
        chmod +x "$STUB_DIR/helm"
    fi

    local exit_code=0
    MOCK_MISSING_REPO="$missing_repo" MOCK_OCI_INDEX_ONLY_REPO="$oci_index_only_repo" \
        PATH="$STUB_DIR:$PATH" \
        bash "$SCRIPT_UNDER_TEST" --skip-build > "$OUT_FILE" 2>&1 || exit_code=$?

    if [ "$expect_success" -eq 1 ]; then
        if [ "$exit_code" -eq 0 ]; then
            echo "  - exit code 0 (expected)"
            cases_passed=$((cases_passed + 1))
        else
            echo "  x exit code $exit_code (expected 0)"
            cases_failed=$((cases_failed + 1))
            cat "$OUT_FILE"
        fi
        if grep -q "STUB-CALLED helm upgrade --install" "$LOG_FILE"; then
            echo "  - reached helm upgrade --install (preflight passed through)"
            cases_passed=$((cases_passed + 1))
        else
            echo "  x never reached helm upgrade --install"
            cases_failed=$((cases_failed + 1))
        fi
        if grep -qE "Preflight checked [0-9]+ chart-referenced image" "$OUT_FILE"; then
            echo "  - preflight printed a non-empty checked count"
            cases_passed=$((cases_passed + 1))
        else
            echo "  x preflight never printed a checked count"
            cases_failed=$((cases_failed + 1))
        fi
    else
        if [ "$exit_code" -ne 0 ]; then
            echo "  - exit code $exit_code (non-zero, expected)"
            cases_passed=$((cases_passed + 1))
        else
            echo "  x exit code 0 (expected non-zero rejection)"
            cases_failed=$((cases_failed + 1))
        fi
        if grep -q "STUB-CALLED helm upgrade --install" "$LOG_FILE"; then
            echo "  x helm upgrade --install was reached despite the preflight failure"
            cases_failed=$((cases_failed + 1))
            cat "$OUT_FILE"
        else
            echo "  - helm upgrade --install never reached (preflight blocked it)"
            cases_passed=$((cases_passed + 1))
        fi
    fi
}

run_case "all chart images present -> preflight passes through to helm upgrade" "" "" 1
run_case "hub-webui missing from registry -> preflight fails before helm upgrade" "hub-webui" "" 0
run_case "zero chart images rendered -> denominator guard fails" "" "1" 0
run_case "hub-webui present only as OCI image index -> treated as present" "" "" 1 "hub-webui"

echo ""
total_cases=$((cases_passed + cases_failed))
if [ "$cases_failed" -eq 0 ]; then
    echo "PASS: $cases_passed/$total_cases cases passed"
    exit 0
else
    echo "FAIL: $cases_passed/$total_cases cases passed, $cases_failed failed"
    exit 1
fi
