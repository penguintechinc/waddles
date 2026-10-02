#!/usr/bin/env bash
# Regression test for fix/alpha-deploy-executor-and-failfast.
#
# Before this fix, scripts/alpha-deploy.sh could exit 0 on a real deploy
# failure:
#   1. the Deployment rollout-status loop only ever logged `err` on a failed
#      `kubectl rollout status` and kept going -- the script's own exit code
#      never reflected the failure;
#   2. nothing checked that the live `helm upgrade --install` actually left
#      the release in `deployed` status (a post-upgrade hook failure can pin
#      a release FAILED while `helm upgrade --install` itself still returns
#      0 -- see mem0 "spire-auto-enroll" incident).
#
# A gate that cannot fail is not a gate (critical-rules.md Verification
# Integrity) -- this test proves BOTH failure paths actually propagate a
# non-zero exit, and that the success path is untouched.
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

cat > "$STUB_DIR/kubectl" <<EOS
#!/usr/bin/env bash
echo "STUB-CALLED kubectl \$*" >> "$LOG_FILE"
args="\$*"
if [ "\$1 \$2" = "config current-context" ]; then echo local-alpha; fi
# Two Deployments exist in this fake cluster -- ROLLOUT_FAIL_DEPLOYMENT below
# decides which (if any) fail their rollout.
case "\$args" in
    *"get deployments -n"*)
        echo "waddlebot-hub-api waddlebot-bundle-executor"
        exit 0
        ;;
    *"rollout status"*)
        if [ -n "\${ROLLOUT_FAIL_DEPLOYMENT:-}" ]; then
            case "\$args" in
                *"deployment/\${ROLLOUT_FAIL_DEPLOYMENT} "*)
                    exit 1
                    ;;
            esac
        fi
        exit 0
        ;;
esac
exit 0
EOS

cat > "$STUB_DIR/docker" <<EOS
#!/usr/bin/env bash
if [ "\$1" = "inspect" ]; then
    git -C "$REPO_ROOT" rev-parse HEAD
    exit 0
fi
exit 0
EOS

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
if [ "\$1" = "status" ]; then
    echo "{\"info\":{\"status\":\"\${MOCK_HELM_STATUS:-deployed}\"},\"version\":\${MOCK_HELM_REVISION:-1}}"
    exit 0
fi
exit 0
EOS

cat > "$STUB_DIR/curl" <<'EOS'
#!/usr/bin/env bash
echo "200"
EOS

chmod +x "$STUB_DIR"/kubectl "$STUB_DIR"/docker "$STUB_DIR"/helm "$STUB_DIR"/curl

run_case() {
    # run_case <label> <MOCK_HELM_STATUS> <ROLLOUT_FAIL_DEPLOYMENT> <expect_exit_zero:0|1>
    local label="$1" mock_status="$2" rollout_fail="$3" expect_success="$4"
    : > "$LOG_FILE"
    : > "$OUT_FILE"
    echo ""
    echo "Test: $label"

    local exit_code=0
    MOCK_HELM_STATUS="$mock_status" ROLLOUT_FAIL_DEPLOYMENT="$rollout_fail" \
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
    else
        if [ "$exit_code" -ne 0 ]; then
            echo "  - exit code $exit_code (non-zero, expected)"
            cases_passed=$((cases_passed + 1))
        else
            echo "  x exit code 0 (expected non-zero -- the bug this test guards against)"
            cases_failed=$((cases_failed + 1))
            cat "$OUT_FILE"
        fi
    fi
}

run_case "baseline: deployed status, every rollout clean -> exit 0" "deployed" "" 1

run_case "helm status != deployed after upgrade -> exit non-zero" "failed" "" 0
if grep -qE "is not 'deployed' after upgrade" "$OUT_FILE"; then
    echo "  - error message names the actual status"
    cases_passed=$((cases_passed + 1))
else
    echo "  x error message missing"
    cases_failed=$((cases_failed + 1))
fi

run_case "one Deployment fails rollout -> exit non-zero, both Deployments still checked" "deployed" "waddlebot-bundle-executor" 0
if grep -q "bundle-executor did not roll out cleanly" "$OUT_FILE" && grep -qE "did not roll out cleanly:" "$OUT_FILE"; then
    echo "  - failure summary names the failed Deployment"
    cases_passed=$((cases_passed + 1))
else
    echo "  x failure summary missing/incomplete"
    cases_failed=$((cases_failed + 1))
    cat "$OUT_FILE"
fi
if grep -q "STUB-CALLED kubectl .*rollout status deployment/waddlebot-hub-api" "$LOG_FILE"; then
    echo "  - the OTHER (clean) Deployment was still checked, not short-circuited"
    cases_passed=$((cases_passed + 1))
else
    echo "  x rollout status for waddlebot-hub-api was never invoked"
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
