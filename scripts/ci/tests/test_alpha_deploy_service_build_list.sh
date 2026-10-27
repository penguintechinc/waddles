#!/usr/bin/env bash
# Regression test for fix/alpha-clean-deploy.
#
# Asserts scripts/alpha-deploy.sh's SERVICES/service_dockerfile/
# service_context/service_image_repo build list:
#   1. builds waddlebot-egress-proxy from core/egress_proxy/Dockerfile.rust
#      with the "core" build context (the chart's egress-proxy Deployment
#      references egressProxy.image="waddlebot-egress-proxy" and was
#      previously never built here, leaving it stuck on a nonexistent tag);
#   2. no longer builds the plain-name Python svc-ingest/svc-process/
#      svc-action images (core/svc_{ingest,process,action}/Dockerfile,
#      without the .rust suffix) -- values-alpha.yaml now disables those
#      Deployments (pipeline.svc{Ingest,Process,Action}.enabled=false), so
#      building them is wasted work;
#   3. still builds the live Rust svc-ingest/svc-process/svc-action stages
#      (core/svc_{ingest,process,action}/Dockerfile.rust) -- this fix must
#      never regress the actual live data-plane build;
#   4. builds bundle-executor from core/bundle_executor/Dockerfile.rust with
#      the "." (repo-root) context -- fix/alpha-deploy-executor-and-failfast
#      (#538) added bundle-executor to SERVICES, backing both the
#      bundle-executor and bundle-executor-action Deployments.
#
# docker/helm/kubectl/curl are all stubbed -- no real registry, cluster, or
# build ever runs. The real SERVICES loop and service_dockerfile/
# service_context/service_image_repo functions in the script under test are
# NOT stubbed -- this test exercises them directly via the logged `docker
# buildx build` invocations (resolve-433's registry-backed buildx cache
# replaced the script's plain `docker build` call with `docker buildx
# build`).
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

cat > "$STUB_DIR/kubectl" <<'EOS'
#!/usr/bin/env bash
if [ "$1 $2" = "config current-context" ]; then echo local-alpha; fi
exit 0
EOS

# Logs every `docker build -f <dockerfile> ... <context>` call so this test
# can assert exactly which Dockerfiles the real SERVICES loop invokes.
cat > "$STUB_DIR/docker" <<EOS
#!/usr/bin/env bash
echo "STUB-CALLED docker \$*" >> "$LOG_FILE"
if [ "\$1" = "inspect" ]; then
    git -C "$REPO_ROOT" rev-parse HEAD
    exit 0
fi
exit 0
EOS

cat > "$STUB_DIR/helm" <<'EOS'
#!/usr/bin/env bash
if [ "$1" = "template" ]; then
    cat <<'YAML'
---
# Source: waddlebot/templates/hub-api.yaml
    containers:
      - name: hub-api
        image: "localhost:32000/waddlebot/hub-api:faketag"
YAML
    exit 0
fi
# fix/alpha-deploy-executor-and-failfast -- the post-upgrade helm-status
# verification needs valid JSON with status=deployed to pass through.
if [ "$1" = "status" ]; then
    echo '{"info":{"status":"deployed"},"version":1}'
    exit 0
fi
exit 0
EOS

cat > "$STUB_DIR/curl" <<'EOS'
#!/usr/bin/env bash
echo "200"
EOS

chmod +x "$STUB_DIR"/kubectl "$STUB_DIR"/docker "$STUB_DIR"/helm "$STUB_DIR"/curl

: > "$LOG_FILE"
: > "$OUT_FILE"
PATH="$STUB_DIR:$PATH" bash "$SCRIPT_UNDER_TEST" > "$OUT_FILE" 2>&1 || {
    echo "alpha-deploy.sh exited non-zero unexpectedly:"
    cat "$OUT_FILE"
    exit 1
}

assert_built() {
    local label="$1" pattern="$2"
    if grep -q "STUB-CALLED docker buildx build .*-f $pattern " "$LOG_FILE"; then
        echo "  - $label built ($pattern)"
        cases_passed=$((cases_passed + 1))
    else
        echo "  x $label NOT built (expected -f $pattern)"
        cases_failed=$((cases_failed + 1))
    fi
}

assert_not_built() {
    local label="$1" pattern="$2"
    if grep -q "STUB-CALLED docker buildx build .*-f $pattern " "$LOG_FILE"; then
        echo "  x $label WAS built (expected absent: -f $pattern)"
        cases_failed=$((cases_failed + 1))
    else
        echo "  - $label correctly absent ($pattern)"
        cases_passed=$((cases_passed + 1))
    fi
}

echo "Test: waddlebot-egress-proxy is built from the core/ context"
assert_built "waddlebot-egress-proxy" "core/egress_proxy/Dockerfile.rust"
if grep -qE "STUB-CALLED docker buildx build .*-t localhost:32000/waddlebot/waddlebot-egress-proxy:" "$LOG_FILE"; then
    echo "  - tagged under the egressProxy.image repo name"
    cases_passed=$((cases_passed + 1))
else
    echo "  x not tagged under localhost:32000/waddlebot/waddlebot-egress-proxy"
    cases_failed=$((cases_failed + 1))
fi

echo ""
echo "Test: legacy Python svc-ingest/svc-process/svc-action are no longer built"
assert_not_built "svc-ingest (python)" "core/svc_ingest/Dockerfile"
assert_not_built "svc-process (python)" "core/svc_process/Dockerfile"
assert_not_built "svc-action (python)" "core/svc_action/Dockerfile"

echo ""
echo "Test: live Rust svc-ingest/svc-process/svc-action are still built"
assert_built "svc-ingest (rust)" "core/svc_ingest/Dockerfile.rust"
assert_built "svc-process (rust)" "core/svc_process/Dockerfile.rust"
assert_built "svc-action (rust)" "core/svc_action/Dockerfile.rust"

echo ""
echo "Test: bundle-executor is built from the repo-root context (#538)"
assert_built "bundle-executor" "core/bundle_executor/Dockerfile.rust"
if grep -qE "STUB-CALLED docker buildx build .*-t localhost:32000/waddlebot/bundle-executor:" "$LOG_FILE"; then
    echo "  - tagged under the bundle-executor repo name"
    cases_passed=$((cases_passed + 1))
else
    echo "  x not tagged under localhost:32000/waddlebot/bundle-executor"
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
