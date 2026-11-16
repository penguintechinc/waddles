#!/usr/bin/env bash
# Tests scripts/ci/smoke/pod-phase-check.sh against a stub kubectl (no cluster touched).
# Run: make test-smoke-pod-phase  (or: bash scripts/ci/smoke/tests/test-pod-phase-check.sh)
#
# Regression (alpha deploy 43894038667): a pre-#1391 precompile CronJob batch pod
# (waddlebot-executor-precompile-29914020-cnpqp) was stuck in Init and failed the deploy
# smoke step, because the old generic pod scan excluded precompile pods by the
# waddlebot.io/workload label -- which that pod, created before the label existed, did not
# carry (and k8s `!=` selects label-less pods). The fix excludes by NAME. This test pins:
#   - the stale, label-less, Pending precompile pod is EXCLUDED by the new script (pass);
#   - the per-release enqueue pod is EXCLUDED too;
#   - a genuinely broken non-precompile pod FAILS the check;
#   - an empty namespace FAILS (zero denominator);
#   - the OLD label-selector logic (embedded below) FAILS on the stale pod -- proving both
#     that the stub reproduces the bug and that the name-based fix is what resolves it.
# Prints how many assertions ran; zero assertions is a failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${SCRIPT_DIR}/../pod-phase-check.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"

ASSERTIONS=0
fail() { echo "[FAIL] $*" >&2; exit 1; }
ok()   { ASSERTIONS=$((ASSERTIONS + 1)); echo "[OK] $*"; }

# Stub kubectl. Pod fixtures live in ${POD_FIXTURES} as "name<TAB>phase<TAB>workloadlabel"
# ('-' = no waddlebot.io/workload label). The stub honours the three call shapes the real and
# the pre-fix scripts use: custom-columns (name+phase for all pods), `-l workload!=...` (k8s
# `!=` INCLUDES label-less pods), and `--field-selector status.phase!=Running,!=Succeeded`.
cat > "${WORK}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
args="$*"
emit_custom_columns() {
  while IFS=$'\t' read -r name phase _; do
    [ -n "${name}" ] || continue
    printf '%s   %s\n' "${name}" "${phase}"
  done < "${POD_FIXTURES}"
}
# label `!=executor-precompile`: include pods whose workload label != executor-precompile,
# INCLUDING pods with no label at all (reproduces the k8s equality-selector semantics).
emit_label_notpc() {
  while IFS=$'\t' read -r name phase label; do
    [ -n "${name}" ] || continue
    [ "${label}" = "executor-precompile" ] && continue
    if [[ "${args}" == *"status.phase!=Running"* ]]; then
      case "${phase}" in Running|Succeeded) continue ;; esac
    fi
    printf '%s\n' "${name}"
  done < "${POD_FIXTURES}"
}
case "${args}" in
  *"custom-columns"*)                          emit_custom_columns ;;
  *"waddlebot.io/workload!=executor-precompile"*) emit_label_notpc ;;
  *) echo "unexpected kubectl ${args}" >&2; exit 99 ;;
esac
STUB
chmod +x "${WORK}/bin/kubectl"
export PATH="${WORK}/bin:${PATH}"
export POD_FIXTURES="${WORK}/pods.tsv"

# Pre-fix reference: the OLD generic pod scan (label selector), to prove it regresses.
prefix_scan() {
  local ns="$1" pods bad
  pods="$(kubectl -n "${ns}" get pods --no-headers -l 'waddlebot.io/workload!=executor-precompile' | wc -l)"
  bad="$(kubectl -n "${ns}" get pods --no-headers -l 'waddlebot.io/workload!=executor-precompile' --field-selector=status.phase!=Running,status.phase!=Succeeded | wc -l)"
  echo "pods examined=${pods} bad=${bad}"
  [ "${pods}" -gt 0 ] && [ "${bad}" -eq 0 ]
}

# Healthy namespace plus the stale pre-merge precompile pod (Pending, NO workload label) and
# the per-release enqueue pod (also label-less in this fixture, as a pre-label run would be).
printf '%s\t%s\t%s\n' \
  'waddlebot-hub-api-v3-abc-1'                       'Running'  'hub-api' \
  'waddlebot-svc-bundles-executor-def-2'             'Running'  'svc-bundles-executor' \
  'waddlebot-executor-precompile-29914020-cnpqp'     'Pending'  '-' \
  'waddlebot-executor-precompile-enqueue-r57-xyz'    'Pending'  '-' \
  > "${POD_FIXTURES}"

out="$(bash "${SCRIPT}" waddlebot)" || fail "new script rejected a healthy namespace: ${out}"
[ "${out##*$'\n'}" = "pods examined=2 bad=0" ] || fail "expected 'pods examined=2 bad=0', got: ${out}"
ok "name-based scan excludes the stale precompile + enqueue pods (examined=2, pass)"

# Same fixture, OLD label-based scan: the stale Pending precompile pod is counted as bad.
if prefix_scan waddlebot > "${WORK}/prefix.out" 2>&1; then
  fail "pre-fix label scan unexpectedly PASSED; stub does not reproduce the bug: $(cat "${WORK}/prefix.out")"
fi
ok "pre-fix label scan FAILS on the stale label-less precompile pod ($(tail -1 "${WORK}/prefix.out"))"

# A genuinely broken non-precompile pod must fail the check.
printf '%s\t%s\t%s\n' \
  'waddlebot-hub-api-v3-abc-1'                   'Running'           'hub-api' \
  'waddlebot-hub-webui-ghi-3'                    'CrashLoopBackOff'  'hub-webui' \
  'waddlebot-executor-precompile-29914020-cnpqp' 'Pending'          '-' \
  > "${POD_FIXTURES}"
if bash "${SCRIPT}" waddlebot > "${WORK}/broken.out" 2>&1; then
  fail "broken hub-webui pod did not fail the check: $(cat "${WORK}/broken.out")"
fi
grep -q 'pods examined=2 bad=1' "${WORK}/broken.out" || fail "expected examined=2 bad=1 for broken pod, got: $(cat "${WORK}/broken.out")"
ok "a non-precompile broken pod trips the check (examined=2 bad=1, exit!=0)"

# Only precompile pods present -> zero denominator -> FAIL (never a silent pass).
printf '%s\t%s\t%s\n' \
  'waddlebot-executor-precompile-29914020-cnpqp' 'Pending' '-' \
  > "${POD_FIXTURES}"
if bash "${SCRIPT}" waddlebot > "${WORK}/empty.out" 2>&1; then
  fail "zero examined pods must fail: $(cat "${WORK}/empty.out")"
fi
grep -q 'zero pods examined' "${WORK}/empty.out" || fail "expected zero-denominator error, got: $(cat "${WORK}/empty.out")"
ok "empty denominator (only precompile pods) fails loudly"

[ "${ASSERTIONS}" -ge 4 ] || fail "ran only ${ASSERTIONS} assertions"
echo "PASS: ${ASSERTIONS} assertions"
