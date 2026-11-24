#!/usr/bin/env bash
# api-sweep runner: resolves access + credentials (lib/access.sh), dumps the
# url_map, runs the sweep. Extra args go to api_sweep.py
# (e.g. --allow-mutations, --allow-old-ids).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=../lib/access.sh
SWEEP_ACCESS_LAZY=1 source "$HERE/../lib/access.sh"
VENV="${SWEEP_VENV:-/tmp/sweep-venv}"
REPORT_DIR="${REPORT_DIR:-/tmp/api-sweep-report}"
export REPORT_DIR

if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --require-hashes -r "$ROOT/hub_api/requirements.txt"
fi
trap sweep_cleanup EXIT
sweep_access

mkdir -p "$REPORT_DIR"
PYTHONPATH="$ROOT/libs/flask_core:$ROOT/libs:$ROOT/hub_api" \
  "$VENV/bin/python" "$HERE/dump_url_map.py" "$REPORT_DIR/url_map.json" 2>/dev/null
"$VENV/bin/python" "$HERE/api_sweep.py" --url-map "$REPORT_DIR/url_map.json" "$@"
