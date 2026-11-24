#!/usr/bin/env bash
# Sourced by sweep runners. Resolves alpha access + admin credentials.
#
# Access: BASE_URL set -> used as-is (HOST_HEADER optional, internal LB);
#         else kubectl port-forward svc/waddlebot-hub-api-v3 (Cloudflare Access
#         fronts the public URL, so port-forward is the alpha default).
# Credentials: HUB_TEST_EMAIL/HUB_TEST_PASS from env, else read from the
# waddlebot-hub-admin-bootstrap secret straight into env vars -- never echoed,
# logged, passed as an argument, or written to a file.
# Sets PF_PID (caller must `trap sweep_cleanup EXIT`).
#
# BASE_URL is NEVER left empty: sourcing this file on its own (without calling sweep_access) used
# to leave it unset, and a following `curl "$BASE_URL/..."` died with curl code 000. Now sourcing
# derives it immediately (same as sweep_access) and fails loudly -- `return 1` -- if it cannot.
# A runner that wants to control when access is resolved sets SWEEP_ACCESS_LAZY=1 before sourcing
# and calls sweep_access itself (run.sh does).
KCTX="${KUBE_CONTEXT:-dal2alpha}"
NS="${KUBE_NAMESPACE:-waddlebot}"
PF_PID=""
sweep_cleanup() { if [[ -n "$PF_PID" ]]; then pkill -P "$PF_PID" 2>/dev/null || true; kill "$PF_PID" 2>/dev/null || true; fi; }

# Fails loudly (non-zero + message on stderr) when BASE_URL is empty/unset.
sweep_require_base_url() {
  if [[ -z "${BASE_URL:-}" ]]; then
    echo "access.sh: BASE_URL is unset/empty -- set BASE_URL or run sweep_access (port-forward)" >&2
    return 1
  fi
}

sweep_access() {
  if [[ -z "${HUB_TEST_PASS:-}" ]]; then
    HUB_TEST_EMAIL="$(kubectl --context "$KCTX" -n "$NS" get secret waddlebot-hub-admin-bootstrap -o jsonpath='{.data.INITIAL_ADMIN_EMAIL}' | base64 -d)" || { echo "access.sh: cannot read admin credentials from $KCTX/$NS" >&2; return 1; }
    HUB_TEST_PASS="$(kubectl --context "$KCTX" -n "$NS" get secret waddlebot-hub-admin-bootstrap -o jsonpath='{.data.INITIAL_ADMIN_PASSWORD}' | base64 -d)" || { echo "access.sh: cannot read admin credentials from $KCTX/$NS" >&2; return 1; }
    export HUB_TEST_EMAIL HUB_TEST_PASS
  fi
  if [[ -z "${BASE_URL:-}" ]]; then
    local port="${SWEEP_LOCAL_PORT:-18204}"
    # Respawn loop: a dropped port-forward must not silently turn the sweep into connection errors.
    ( while true; do kubectl --context "$KCTX" -n "$NS" port-forward svc/waddlebot-hub-api-v3 "$port:8204" >/dev/null 2>&1 || true; sleep 1; done ) &
    PF_PID=$!
    local healthy=0
    for _ in $(seq 1 20); do
      if curl -sk -o /dev/null --fail "https://localhost:$port/health"; then healthy=1; break; fi
      sleep 0.5
    done
    if [[ "$healthy" != 1 ]]; then
      echo "access.sh: port-forward to $KCTX/$NS never became healthy on :$port" >&2
      sweep_cleanup
      return 1
    fi
    export BASE_URL="https://localhost:$port" INSECURE=1
    echo "access: kubectl port-forward ($KCTX/$NS svc/waddlebot-hub-api-v3)"
  else
    echo "access: BASE_URL=$BASE_URL host-header=${HOST_HEADER:-none}"
  fi
  sweep_require_base_url
}

# Sourced on its own: resolve access now so BASE_URL is never silently empty.
if [[ "${SWEEP_ACCESS_LAZY:-0}" != 1 ]]; then
  sweep_access || return 1
fi
