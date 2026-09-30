#!/usr/bin/env bash
# Regression test for fix/alpha-deploy-context-allowlist.
#
# Asserts scripts/alpha-deploy.sh rejects any KUBE_CONTEXT outside the
# local-alpha/microk8s allowlist BEFORE calling docker/helm/kubectl (a stray
# `KUBE_CONTEXT=dal2-beta make alpha-deploy` must never reach a real cluster),
# and that both allowed contexts pass validation and reach the deploy steps.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
SCRIPT_UNDER_TEST="$REPO_ROOT/scripts/alpha-deploy.sh"

cases_passed=0
cases_failed=0

STUB_DIR="$(mktemp -d)"
LOG_FILE="$(mktemp)"
# shellcheck disable=SC2317  # invoked indirectly via trap
cleanup() { rm -rf "$STUB_DIR" "$LOG_FILE"; }
trap cleanup EXIT

# Stub docker/helm/kubectl on PATH -- each just logs its invocation and exits
# 0 (`kubectl ... config current-context` additionally prints local-alpha, so
# the "unset KUBE_CONTEXT" default path has something to validate).
#
# `helm template` additionally renders one fake image so alpha-deploy.sh's
# check_images_in_registry preflight (fix/alpha-deploy-hub-webui) has a
# non-empty image list to find -- this test is about context allowlisting,
# not the preflight itself (see test_alpha_deploy_image_preflight.sh), so it
# must not trip the "zero images rendered" denominator guard.
for tool in docker kubectl; do
    stub="$STUB_DIR/$tool"
    {
        echo '#!/usr/bin/env bash'
        echo "echo \"STUB-CALLED $tool \$*\" >> \"$LOG_FILE\""
        # shellcheck disable=SC2016  # single-quoted on purpose: expands in the stub, not here
        echo 'if [ "$1 $2" = "config current-context" ]; then echo local-alpha; fi'
        echo 'exit 0'
    } > "$stub"
    chmod +x "$stub"
done

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

cat > "$STUB_DIR/curl" <<'EOS'
#!/usr/bin/env bash
echo "200"
EOS

chmod +x "$STUB_DIR/helm" "$STUB_DIR/curl"

run_case() {
    # run_case <label> <kube_context_or_empty> <expected_exit_zero:0|1>
    local label="$1" ctx="$2" expect_success="$3"
    : > "$LOG_FILE"
    echo ""
    echo "Test: $label"

    local exit_code=0
    if [ -n "$ctx" ]; then
        PATH="$STUB_DIR:$PATH" KUBE_CONTEXT="$ctx" bash "$SCRIPT_UNDER_TEST" --skip-build \
            > /tmp/alpha-deploy-context-test-out.log 2>&1 || exit_code=$?
    else
        PATH="$STUB_DIR:$PATH" bash "$SCRIPT_UNDER_TEST" --skip-build \
            > /tmp/alpha-deploy-context-test-out.log 2>&1 || exit_code=$?
    fi

    if [ "$expect_success" -eq 1 ]; then
        if [ "$exit_code" -eq 0 ]; then
            echo "  - exit code 0 (expected)"
            cases_passed=$((cases_passed + 1))
        else
            echo "  x exit code $exit_code (expected 0)"
            cases_failed=$((cases_failed + 1))
            cat /tmp/alpha-deploy-context-test-out.log
        fi
        if grep -q "STUB-CALLED helm" "$LOG_FILE"; then
            echo "  - reached helm step (validation passed through)"
            cases_passed=$((cases_passed + 1))
        else
            echo "  x never reached helm step"
            cases_failed=$((cases_failed + 1))
        fi
        # fix/helm-platform-credentials -- the secret-existence preflight's
        # kubectl call must carry the validated --context, never the
        # ambient/default context, once the allowlist check has passed.
        if grep -q "STUB-CALLED kubectl --context $ctx get secret waddlebot-platform-credentials" "$LOG_FILE"; then
            echo "  - platform-credentials preflight used --context $ctx"
            cases_passed=$((cases_passed + 1))
        else
            echo "  x platform-credentials preflight did not use --context $ctx"
            cases_failed=$((cases_failed + 1))
            cat "$LOG_FILE"
        fi
    else
        if [ "$exit_code" -ne 0 ]; then
            echo "  - exit code $exit_code (non-zero, expected)"
            cases_passed=$((cases_passed + 1))
        else
            echo "  x exit code 0 (expected non-zero rejection)"
            cases_failed=$((cases_failed + 1))
        fi
        if [ ! -s "$LOG_FILE" ]; then
            echo "  - docker/helm/kubectl never invoked"
            cases_passed=$((cases_passed + 1))
        else
            echo "  x docker/helm/kubectl was invoked before rejection:"
            cases_failed=$((cases_failed + 1))
            cat "$LOG_FILE"
        fi
    fi
}

run_case "KUBE_CONTEXT=dal2-beta is rejected before docker/helm/kubectl" "dal2-beta" 0
run_case "KUBE_CONTEXT=local-alpha passes validation" "local-alpha" 1
run_case "KUBE_CONTEXT=microk8s passes validation" "microk8s" 1

echo ""
total_cases=$((cases_passed + cases_failed))
if [ "$cases_failed" -eq 0 ]; then
    echo "PASS: $cases_passed/$total_cases cases passed"
    exit 0
else
    echo "FAIL: $cases_passed/$total_cases cases passed, $cases_failed failed"
    exit 1
fi
