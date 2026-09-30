#!/usr/bin/env bash
# Regression test for resolve-433 (registry-backed buildx cache).
#
# Asserts scripts/alpha-deploy.sh's build step, when NOT skipped:
#   1. provisions the dedicated buildx builder (docker buildx inspect, then
#      create if missing) before building anything;
#   2. builds every service with `docker buildx build` (not plain
#      `docker build`) passing both `--cache-from type=registry,ref=...` and
#      `--cache-to type=registry,ref=...,mode=max,...` pointed at
#      localhost:32000/waddlebot/buildcache/<repo> -- one ref per image, so a
#      chart image whose cache flags are dropped by a future edit fails this
#      test loudly instead of silently reverting to cold builds;
#   3. still pushes and revision-label-verifies each image afterward
#      (unchanged behavior).
#
# docker/helm/kubectl/curl are all stubbed -- no real registry, cluster,
# builder, or build ever runs.
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

# --- kubectl stub: always local-alpha, secret preflight and everything else
#     succeeds. ----------------------------------------------------------
cat > "$STUB_DIR/kubectl" <<EOS
#!/usr/bin/env bash
echo "STUB-CALLED kubectl \$*" >> "$LOG_FILE"
if [ "\$1 \$2" = "config current-context" ]; then echo local-alpha; fi
exit 0
EOS

# --- docker stub -------------------------------------------------------
# `buildx inspect <name>` fails once (builder doesn't exist yet) so
# ensure_cache_builder's create path is exercised; `buildx create` and
# `buildx build` both log their full argv (so the test can grep for the
# cache flags) and succeed; `inspect <image>` (revision-label verify) and
# `push` both succeed as before.
cat > "$STUB_DIR/docker" <<EOS
#!/usr/bin/env bash
echo "STUB-CALLED docker \$*" >> "$LOG_FILE"
if [ "\$1" = "buildx" ] && [ "\$2" = "inspect" ]; then
    exit 1
fi
if [ "\$1" = "inspect" ]; then
    git -C "$REPO_ROOT" rev-parse HEAD
    exit 0
fi
exit 0
EOS

# --- helm stub: `template` renders one fake chart image; everything else
#     just succeeds. ------------------------------------------------------
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
YAML
    exit 0
fi
exit 0
EOS

# --- curl stub: every registry manifest check reports present. -----------
cat > "$STUB_DIR/curl" <<'EOS'
#!/usr/bin/env bash
echo "200"
EOS

chmod +x "$STUB_DIR"/kubectl "$STUB_DIR"/docker "$STUB_DIR"/helm "$STUB_DIR"/curl

echo "Test: build step uses docker buildx with registry cache-from/cache-to"
exit_code=0
PATH="$STUB_DIR:$PATH" bash "$SCRIPT_UNDER_TEST" > "$OUT_FILE" 2>&1 || exit_code=$?

if [ "$exit_code" -eq 0 ]; then
    echo "  - exit code 0 (expected)"
    cases_passed=$((cases_passed + 1))
else
    echo "  x exit code $exit_code (expected 0)"
    cases_failed=$((cases_failed + 1))
    cat "$OUT_FILE"
fi

if grep -q "STUB-CALLED docker buildx inspect alpha-registry-cache" "$LOG_FILE"; then
    echo "  - checked for the cache builder before building"
    cases_passed=$((cases_passed + 1))
else
    echo "  x never checked for the cache builder (docker buildx inspect)"
    cases_failed=$((cases_failed + 1))
fi

if grep -q "STUB-CALLED docker buildx create --name alpha-registry-cache --driver docker-container --driver-opt network=host --buildkitd-config" "$LOG_FILE"; then
    echo "  - created the cache builder (missing on first run)"
    cases_passed=$((cases_passed + 1))
else
    echo "  x never created the cache builder"
    cases_failed=$((cases_failed + 1))
    cat "$LOG_FILE"
fi

if grep -q "STUB-CALLED docker buildx build --builder alpha-registry-cache" "$LOG_FILE"; then
    echo "  - builds go through 'docker buildx build --builder alpha-registry-cache' (not plain 'docker build')"
    cases_passed=$((cases_passed + 1))
else
    echo "  x builds did not use the buildx cache builder"
    cases_failed=$((cases_failed + 1))
    cat "$LOG_FILE"
fi

build_calls="$(grep -c 'STUB-CALLED docker buildx build --builder alpha-registry-cache' "$LOG_FILE" || true)"
cache_from_calls="$(grep -c -- '--cache-from type=registry,ref=localhost:32000/waddlebot/buildcache/' "$LOG_FILE" || true)"
cache_to_calls="$(grep -c -- '--cache-to type=registry,ref=localhost:32000/waddlebot/buildcache/.*mode=max' "$LOG_FILE" || true)"

if [ "$build_calls" -gt 0 ] && [ "$cache_from_calls" -eq "$build_calls" ]; then
    echo "  - every build ($build_calls) carried --cache-from type=registry,ref=.../buildcache/<repo>"
    cases_passed=$((cases_passed + 1))
else
    echo "  x --cache-from missing on one or more builds (builds=$build_calls, cache-from=$cache_from_calls)"
    cases_failed=$((cases_failed + 1))
    cat "$LOG_FILE"
fi

if [ "$build_calls" -gt 0 ] && [ "$cache_to_calls" -eq "$build_calls" ]; then
    echo "  - every build ($build_calls) carried --cache-to type=registry,ref=...,mode=max,..."
    cases_passed=$((cases_passed + 1))
else
    echo "  x --cache-to (mode=max) missing on one or more builds (builds=$build_calls, cache-to=$cache_to_calls)"
    cases_failed=$((cases_failed + 1))
    cat "$LOG_FILE"
fi

if grep -q "STUB-CALLED docker push localhost:32000/waddlebot/hub-api:" "$LOG_FILE"; then
    echo "  - still pushes each built image (unchanged behavior)"
    cases_passed=$((cases_passed + 1))
else
    echo "  x push step missing after switching to buildx"
    cases_failed=$((cases_failed + 1))
fi

if grep -q "STUB-CALLED helm upgrade --install" "$LOG_FILE"; then
    echo "  - reached helm upgrade --install"
    cases_passed=$((cases_passed + 1))
else
    echo "  x never reached helm upgrade --install"
    cases_failed=$((cases_failed + 1))
fi

echo ""
total_cases=$((cases_passed + cases_failed))
if [ "$cases_failed" -eq 0 ]; then
    echo "PASS: $cases_passed/$total_cases cases passed"
    exit 0
else
    echo "FAIL: $cases_passed/$total_cases cases passed, $cases_failed failed"
    exit 1
fi
