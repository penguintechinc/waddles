#!/usr/bin/env bash
# Tests scripts/lib/helm-pending-recover.sh against stub helm/kubectl binaries
# (no cluster touched). Run: make test-helm-pending-recover
# Prints how many assertions ran; zero assertions is a failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"

info() { echo "[INFO] $*"; }
err()  { echo "[ERROR] $*" >&2; }
# shellcheck source=scripts/lib/helm-pending-recover.sh
source "${SCRIPT_DIR}/lib/helm-pending-recover.sh"

# Stub helm: behaviour driven by files under $STUB_STATE.
cat > "${WORK}/bin/helm" <<'STUB'
#!/usr/bin/env bash
echo "helm $*" >> "${STUB_STATE}/calls.log"
case "$1" in
    status)
        st="$(cat "${STUB_STATE}/status")"
        if [[ "${st}" == "notfound" ]]; then echo "Error: release: not found" >&2; exit 1; fi
        [[ "${STATUS_WARN:-0}" == "1" ]] && echo "WARNING: kubeconfig is group-readable { not json" >&2
        printf '{"info":{"status":"%s"},"version":5}\n' "${st}" ;;
    history) cat "${STUB_STATE}/history.json" ;;
    rollback)
        [[ "${FIX_ON_ROLLBACK:-0}" == "1" ]] && echo deployed > "${STUB_STATE}/status"
        exit 0 ;;
    *) echo "unexpected helm $*" >&2; exit 99 ;;
esac
STUB
# Stub kubectl: `get secret` returns secrets.json, `delete secret` is logged.
cat > "${WORK}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
echo "kubectl $*" >> "${STUB_STATE}/calls.log"
case " $* " in
    *" get secret "*) cat "${STUB_STATE}/secrets.json" ;;
    *" delete secret "*)
        [[ "${FIX_ON_DELETE:-0}" == "1" ]] && echo failed > "${STUB_STATE}/status"
        exit 0 ;;
    *) echo "unexpected kubectl $*" >&2; exit 99 ;;
esac
STUB
chmod +x "${WORK}/bin/helm" "${WORK}/bin/kubectl"
export PATH="${WORK}/bin:${PATH}"

ASSERTS=0
FAILS=0
check() { # check <desc> <expected-rc> <actual-rc>
    ASSERTS=$((ASSERTS + 1))
    if [[ "$2" != "$3" ]]; then echo "FAIL: $1 (expected rc=$2 got rc=$3)"; FAILS=$((FAILS + 1)); fi
}
called() { # called <desc> <yes|no> <pattern>
    ASSERTS=$((ASSERTS + 1))
    local found=no
    if grep -q -- "$3" "${STUB_STATE}/calls.log" 2>/dev/null; then found=yes; fi
    if [[ "${found}" != "$2" ]]; then echo "FAIL: $1 (pattern '$3' called=${found}, want $2)"; FAILS=$((FAILS + 1)); fi
}

# setup <status> <history-json> <secrets-json>
setup() {
    STUB_STATE="$(mktemp -d "${WORK}/state.XXXXXX")"
    export STUB_STATE
    : > "${STUB_STATE}/calls.log"
    echo "$1" > "${STUB_STATE}/status"
    echo "$2" > "${STUB_STATE}/history.json"
    echo "$3" > "${STUB_STATE}/secrets.json"
    unset FIX_ON_ROLLBACK FIX_ON_DELETE STATUS_WARN
}
run() { local rc=0; helm_recover_pending_release waddlebot waddlebot local-alpha >/dev/null 2>&1 || rc=$?; echo "${rc}"; }

NO_SECRETS='{"items":[]}'
SECRETS='{"items":[
 {"metadata":{"name":"sh.helm.release.v1.waddlebot.v9","labels":{"status":"pending-upgrade","version":"9"}}},
 {"metadata":{"name":"sh.helm.release.v1.waddlebot.v10","labels":{"status":"pending-upgrade","version":"10"}}},
 {"metadata":{"name":"sh.helm.release.v1.waddlebot.v8","labels":{"status":"failed","version":"8"}}}]}'
HIST_DEPLOYED='[{"revision":3,"status":"superseded"},{"revision":4,"status":"deployed"},{"revision":5,"status":"pending-upgrade"}]'
HIST_NONE='[{"revision":4,"status":"failed"},{"revision":5,"status":"pending-upgrade"}]'

# 1. deployed release is never touched
setup deployed "${HIST_DEPLOYED}" "${SECRETS}"
check "deployed: rc 0" 0 "$(run)"
called "deployed: no rollback" no "helm rollback"
called "deployed: no delete" no "delete secret"

# 2. failed-but-not-pending release is never touched
setup failed "${HIST_NONE}" "${SECRETS}"
check "failed: rc 0" 0 "$(run)"
called "failed: no rollback" no "helm rollback"
called "failed: no delete" no "delete secret"

# 3. release not found = fresh install
setup notfound "[]" "${NO_SECRETS}"
check "notfound: rc 0" 0 "$(run)"
called "notfound: no rollback" no "helm rollback"

# 4. pending + a deployed revision => rollback to it
setup pending-upgrade "${HIST_DEPLOYED}" "${SECRETS}"
export FIX_ON_ROLLBACK=1
check "pending+deployed: rc 0" 0 "$(run)"
called "pending+deployed: rollback to rev 4" yes "helm rollback waddlebot 4 "
called "pending+deployed: no secret delete" no "delete secret"
called "pending+deployed: pinned to context" yes "--kube-context local-alpha"

# 5. pending, no deployed revision => delete highest pending secret (v10, not v9/v8)
setup pending-upgrade "${HIST_NONE}" "${SECRETS}"
export FIX_ON_DELETE=1
check "pending+nodeployed: rc 0" 0 "$(run)"
called "pending+nodeployed: deletes v10" yes "delete secret sh.helm.release.v1.waddlebot.v10"
called "pending+nodeployed: not v9" no "delete secret sh.helm.release.v1.waddlebot.v9"
called "pending+nodeployed: no rollback" no "helm rollback"

# 6. pending-install, rollback target exists but status stays pending => fail loud
setup pending-rollback "${HIST_DEPLOYED}" "${SECRETS}"
check "still pending after rollback: rc 1" 1 "$(run)"

# 7. pending, no deployed, zero pending secrets => fail loud, not silent pass
setup pending-install "${HIST_NONE}" "${NO_SECRETS}"
check "no secrets found: rc 1" 1 "$(run)"
called "no secrets found: nothing deleted" no "delete secret"

# 8. pending, delete succeeds but status still pending => fail loud
setup pending-upgrade "${HIST_NONE}" "${SECRETS}"
check "still pending after delete: rc 1" 1 "$(run)"

# 9. helm status emits a stderr warning alongside valid JSON => no false abort
setup deployed "${HIST_DEPLOYED}" "${SECRETS}"
export STATUS_WARN=1
check "stderr warning + deployed: rc 0" 0 "$(run)"
called "stderr warning + deployed: no rollback" no "helm rollback"
setup pending-upgrade "${HIST_DEPLOYED}" "${SECRETS}"
export STATUS_WARN=1 FIX_ON_ROLLBACK=1
check "stderr warning + pending: recovers rc 0" 0 "$(run)"
called "stderr warning + pending: rollback to rev 4" yes "helm rollback waddlebot 4 "

echo "Examined${ASSERTS} assertion(s), ${FAILS} failure(s)."
if [[ ${ASSERTS} -eq 0 || ${FAILS} -ne 0 ]]; then
    exit 1
fi
echo "PASS"
