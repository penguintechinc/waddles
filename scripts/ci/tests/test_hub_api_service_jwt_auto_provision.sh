#!/usr/bin/env bash
# Regression test for fix/hub-api-grpc-service-jwt-issuer.
#
# Live evidence (alpha): hub-api logged "hub-api internal gRPC server not
# started: service_jwt issuer unconfigured" (grpc_startup_skipped) and never
# bound port 50204 -- the auto-provisioned `service-jwt-signing-key` Secret
# was stored under a Secret-key shape (`SERVICE_JWT_PRIVATE_KEY`, raw base64
# seed, no `_<kid>` suffix) that `flask_core.service_jwt.load_issuer_from_env`
# can never parse (it scans for a kid-SUFFIXED `SERVICE_JWT_PRIVATE_KEY_`
# prefix holding PEM text).
#
# This test proves the FIX for real, not just textually: it renders the
# actual chart (alpha), extracts the real auto-provision-keys Job script,
# RUNS it against a minimal mocked `kubectl` (no real cluster needed), and
# feeds the Secret keys the script actually produced into a real Python
# process that calls `flask_core.service_jwt.load_issuer_from_env` --
# exactly what `hub_api/app.py::startup` does. A regression in either the
# chart's key-provisioning shape or the default `keyId` (must be a valid
# POSIX env-var-name suffix -- see values.yaml's own comment) fails this
# test.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
CHART_DIR="$REPO_ROOT/k8s/helm/waddlebot"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "Test: alpha-rendered auto-provision-keys Job actually produces a hub-api-loadable service_jwt Secret"

# --- 1. Render the real chart, extract the real Job script + SERVICE_JWT_KEY_ID. ---
helm template waddlebot "$CHART_DIR" --values "$CHART_DIR/values-alpha.yaml" --kube-version 1.30.0 \
    > "$WORKDIR/rendered.yaml"

python3 - "$WORKDIR/rendered.yaml" "$WORKDIR" <<'PYEOF'
import sys
import yaml

rendered_path, workdir = sys.argv[1], sys.argv[2]
with open(rendered_path) as fh:
    docs = [d for d in yaml.safe_load_all(fh) if isinstance(d, dict)]

jobs = [d for d in docs if d.get("kind") == "Job" and "auto-provision-keys" in d.get("metadata", {}).get("name", "")]
if len(jobs) != 1:
    print(f"FAIL: expected exactly 1 auto-provision-keys Job, found {len(jobs)}", file=sys.stderr)
    sys.exit(1)

container = jobs[0]["spec"]["template"]["spec"]["containers"][0]
script = container["args"][0]
if not script.strip():
    print("FAIL: rendered Job script is empty", file=sys.stderr)
    sys.exit(1)
with open(f"{workdir}/job_script.sh", "w") as fh:
    fh.write(script)

env = {e["name"]: e.get("value", "") for e in container.get("env", [])}
required = ["NAMESPACE", "DEPLOYMENT_TIER", "SERVICE_JWT_SECRET", "SERVICE_JWT_CM", "SERVICE_JWT_KEY_ID", "SERVICE_JWT_EXTERNAL"]
missing = [k for k in required if not env.get(k)]
if missing:
    print(f"FAIL: auto-provision-keys Job missing/empty required env var(s): {missing}", file=sys.stderr)
    sys.exit(1)
with open(f"{workdir}/job.env", "w") as fh:
    for k in required + ["BUNDLE_SIGNING_SECRET", "BUNDLE_SIGNING_CM", "BUNDLE_SIGNING_KEY_ID", "BUNDLE_SIGNING_EXTERNAL"]:
        fh.write(f"{k}={env.get(k, '')}\n")

print(f"Extracted Job script ({len(script)} bytes) and {len(env)} env var(s); "
      f"SERVICE_JWT_KEY_ID={env['SERVICE_JWT_KEY_ID']!r}")
PYEOF

chmod +x "$WORKDIR/job_script.sh"
bash -n "$WORKDIR/job_script.sh"

# --- 2. A minimal kubectl mock: persists Secret/ConfigMap data as plain files. ---
mkdir -p "$WORKDIR/mockbin" "$WORKDIR/mockstate"
cat > "$WORKDIR/mockbin/kubectl" <<'MOCKEOF'
#!/usr/bin/env bash
set -euo pipefail
STATE="${MOCK_STATE_DIR:?set MOCK_STATE_DIR}"
mkdir -p "$STATE/secrets" "$STATE/configmaps"

if [[ "$1" == "-n" && "$3" == "get" && "$4" == "secret" ]]; then
  name="$5"
  f="$STATE/secrets/$name"
  if [[ "${6:-}" == "-o" && "${7:-}" == jsonpath=* ]]; then
    jp="${7#jsonpath=}"; jp="${jp#\{.data.}"; jp="${jp%\}}"
    [[ -f "$f/$jp" ]] || { echo ""; exit 1; }
    cat "$f/$jp"; exit 0
  fi
  [[ -d "$f" ]] || exit 1
  exit 0
fi

if [[ "$1" == "-n" && "$3" == "create" && "$4" == "secret" ]]; then
  name="$6"; mkdir -p "$STATE/secrets/$name"; shift 6
  for arg in "$@"; do
    case "$arg" in
      --from-literal=*)
        kv="${arg#--from-literal=}"; k="${kv%%=*}"; v="${kv#*=}"
        printf '%s' "$v" | base64 > "$STATE/secrets/$name/$k" ;;
      --from-file=*)
        kv="${arg#--from-file=}"; k="${kv%%=*}"; path="${kv#*=}"
        base64 "$path" > "$STATE/secrets/$name/$k" ;;
    esac
  done
  exit 0
fi

[[ "$1" == "-n" && "$3" == "annotate" ]] && exit 0

if [[ "$1" == "-n" && "$3" == "create" && "$4" == "configmap" ]]; then
  name="$6"; mkdir -p "$STATE/configmaps/$name"; shift 6
  for arg in "$@"; do
    case "$arg" in
      --from-literal=*)
        kv="${arg#--from-literal=}"; k="${kv%%=*}"; v="${kv#*=}"
        printf '%s' "$v" > "$STATE/configmaps/$name/$k" ;;
    esac
  done
  echo "kind: ConfigMap"; exit 0
fi

[[ "$1" == "-n" && "$3" == "apply" ]] && { cat >/dev/null; exit 0; }

echo "mock kubectl: unhandled invocation: $*" >&2
exit 1
MOCKEOF
chmod +x "$WORKDIR/mockbin/kubectl"

# --- 3. Run the REAL rendered Job script against the mock -- no textual guessing. ---
set -a
# shellcheck disable=SC1090,SC1091
source "$WORKDIR/job.env"
set +a
PATH="$WORKDIR/mockbin:$PATH" MOCK_STATE_DIR="$WORKDIR/mockstate" bash "$WORKDIR/job_script.sh"

SECRET_NAME="$(grep '^SERVICE_JWT_SECRET=' "$WORKDIR/job.env" | cut -d= -f2)"
KEY_ID="$(grep '^SERVICE_JWT_KEY_ID=' "$WORKDIR/job.env" | cut -d= -f2)"
SECRET_DIR="$WORKDIR/mockstate/secrets/$SECRET_NAME"

if [[ ! -f "$SECRET_DIR/SERVICE_JWT_ACTIVE_KID" ]]; then
    echo "FAIL: produced Secret has no SERVICE_JWT_ACTIVE_KID key" >&2
    exit 1
fi
if [[ ! -f "$SECRET_DIR/SERVICE_JWT_PRIVATE_KEY_${KEY_ID}" ]]; then
    echo "FAIL: produced Secret has no SERVICE_JWT_PRIVATE_KEY_${KEY_ID} key -- the exact" \
         "regression this test guards (unsuffixed/wrong-format key name)" >&2
    exit 1
fi
echo "Secret/$SECRET_NAME has SERVICE_JWT_ACTIVE_KID and SERVICE_JWT_PRIVATE_KEY_${KEY_ID}."

# --- 4. Prove hub-api would actually load this -- run the real loader. ---
ACTIVE_KID="$(base64 -d "$SECRET_DIR/SERVICE_JWT_ACTIVE_KID")"
base64 -d "$SECRET_DIR/SERVICE_JWT_PRIVATE_KEY_${KEY_ID}" > "$WORKDIR/priv.pem"

PYTHONPATH="$REPO_ROOT/libs/flask_core" \
    python3 - "$ACTIVE_KID" "$WORKDIR/priv.pem" <<'PYEOF2'
import os
import sys

active_kid, pem_path = sys.argv[1], sys.argv[2]
with open(pem_path) as fh:
    pem = fh.read()
os.environ["SERVICE_JWT_ACTIVE_KID"] = active_kid
os.environ[f"SERVICE_JWT_PRIVATE_KEY_{active_kid}"] = pem

from flask_core.service_jwt import load_issuer_from_env  # noqa: E402

issuer = load_issuer_from_env([])
assert issuer.active_kid == active_kid
token = issuer.keys[active_kid]
assert token.private_key is not None
print(f"PASS: load_issuer_from_env built a working issuer (active_kid={active_kid!r}).")
PYEOF2

echo "PASS: fixed auto-provisioned service-jwt Secret is loadable by hub-api's service_jwt loader."
