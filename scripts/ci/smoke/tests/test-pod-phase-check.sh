#!/usr/bin/env bash
# Integration test for scripts/ci/smoke/pod-phase-check.sh against stub helm + kubectl
# (no cluster touched). Run: make test-smoke-pod-phase
#
# Proves the wrapper wires helm (release boundary) + kubectl (pods/jobs JSON) +
# pod-phase-classify.py together, and that the owner-aware behaviour holds end to end:
#   - a healthy Deployment pod plus a transient role-sync CronJob pod in Init:0/1 (this
#     release) PASSES -- the single-snapshot flake that used to fail the deploy;
#   - a broken Deployment pod FAILS.
# Granular classification cases live in test_pod_phase_classify.py. Prints how many
# assertions ran; zero is a failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${SCRIPT_DIR}/../pod-phase-check.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"

ASSERTIONS=0
fail() { echo "[FAIL] $*" >&2; exit 1; }
ok()   { ASSERTIONS=$((ASSERTIONS + 1)); echo "[OK] $*"; }

# Stub helm: `status ... -o json` -> fixed release deploy time.
cat > "${WORK}/bin/helm" <<'STUB'
#!/usr/bin/env bash
printf '{"info":{"last_deployed":"2026-11-16T15:25:47.248121990Z"}}\n'
STUB
# Stub kubectl: `get pods -o json` and `get jobs -o json` echo the fixture files.
cat > "${WORK}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *" get pods "*) cat "${PODS_JSON}" ;;
  *" get jobs "*) cat "${JOBS_JSON}" ;;
  *) echo "unexpected kubectl $*" >&2; exit 99 ;;
esac
STUB
chmod +x "${WORK}/bin/helm" "${WORK}/bin/kubectl"
export PATH="${WORK}/bin:${PATH}"
export PODS_JSON="${WORK}/pods.json"
export JOBS_JSON="${WORK}/jobs.json"

# Healthy Deployment pod + transient role-sync CronJob pod Init:0/1 (Pending), Job created
# after the release boundary and still active (no Failed condition).
cat > "${PODS_JSON}" <<'JSON'
{"items":[
 {"metadata":{"name":"waddlebot-hub-api-v3-a","ownerReferences":[{"kind":"ReplicaSet","name":"rs"}]},"status":{"phase":"Running"}},
 {"metadata":{"name":"waddlebot-role-sync-29914079-x","ownerReferences":[{"kind":"Job","name":"waddlebot-role-sync-29914079"}],"labels":{"job-name":"waddlebot-role-sync-29914079"}},"status":{"phase":"Pending"}}
]}
JSON
cat > "${JOBS_JSON}" <<'JSON'
{"items":[{"metadata":{"name":"waddlebot-role-sync-29914079","creationTimestamp":"2026-11-16T15:30:00Z"},"status":{"conditions":[]}}]}
JSON
out="$(bash "${SCRIPT}" waddlebot waddlebot)" || fail "healthy+transient namespace rejected: ${out}"
[ "${out##*$'\n'}" = "pods examined=1 bad=0" ] || fail "expected 'pods examined=1 bad=0', got: ${out}"
ok "transient role-sync Init pod does not fail the deploy (examined=1 bad=0)"

# A broken Deployment pod must fail.
cat > "${PODS_JSON}" <<'JSON'
{"items":[
 {"metadata":{"name":"waddlebot-hub-api-v3-a","ownerReferences":[{"kind":"ReplicaSet","name":"rs"}]},"status":{"phase":"Running"}},
 {"metadata":{"name":"waddlebot-hub-webui-b","ownerReferences":[{"kind":"ReplicaSet","name":"rs2"}]},"status":{"phase":"CrashLoopBackOff"}}
]}
JSON
echo '{"items":[]}' > "${JOBS_JSON}"
if bash "${SCRIPT}" waddlebot waddlebot > "${WORK}/broken.out" 2>&1; then
  fail "broken Deployment pod did not fail: $(cat "${WORK}/broken.out")"
fi
grep -q 'pods examined=2 bad=1' "${WORK}/broken.out" || fail "expected examined=2 bad=1, got: $(cat "${WORK}/broken.out")"
ok "a broken long-running pod trips the check (examined=2 bad=1, exit!=0)"

[ "${ASSERTIONS}" -ge 2 ] || fail "ran only ${ASSERTIONS} assertions"
echo "PASS: ${ASSERTIONS} assertions"
