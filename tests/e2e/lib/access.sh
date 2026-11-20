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
KCTX="${KUBE_CONTEXT:-dal2alpha}"
NS="${KUBE_NAMESPACE:-waddlebot}"
PF_PID=""
sweep_cleanup() { if [[ -n "$PF_PID" ]]; then pkill -P "$PF_PID" 2>/dev/null || true; kill "$PF_PID" 2>/dev/null || true; fi; }

sweep_access() {
  if [[ -z "${HUB_TEST_PASS:-}" ]]; then
    HUB_TEST_EMAIL="$(kubectl --context "$KCTX" -n "$NS" get secret waddlebot-hub-admin-bootstrap -o jsonpath='{.data.INITIAL_ADMIN_EMAIL}' | base64 -d)"
    HUB_TEST_PASS="$(kubectl --context "$KCTX" -n "$NS" get secret waddlebot-hub-admin-bootstrap -o jsonpath='{.data.INITIAL_ADMIN_PASSWORD}' | base64 -d)"
    export HUB_TEST_EMAIL HUB_TEST_PASS
  fi
  if [[ -z "${BASE_URL:-}" ]]; then
    local port="${SWEEP_LOCAL_PORT:-18204}"
    # Respawn loop: a dropped port-forward must not silently turn the sweep into connection errors.
    ( while true; do kubectl --context "$KCTX" -n "$NS" port-forward svc/waddlebot-hub-api-v3 "$port:8204" >/dev/null 2>&1 || true; sleep 1; done ) &
    PF_PID=$!
    for _ in $(seq 1 20); do
      curl -sk -o /dev/null "https://localhost:$port/health" && break
      sleep 0.5
    done
    export BASE_URL="https://localhost:$port" INSECURE=1
    echo "access: kubectl port-forward ($KCTX/$NS svc/waddlebot-hub-api-v3)"
  else
    echo "access: BASE_URL=$BASE_URL host-header=${HOST_HEADER:-none}"
  fi
}
