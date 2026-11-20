#!/usr/bin/env bash
# bundle-sweep runner. Extra args go to bundle_sweep.py (--harness, --require-invoked, --allow-old-ids).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/access.sh
source "$HERE/../lib/access.sh"
VENV="${SWEEP_VENV:-/tmp/sweep-venv}"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --require-hashes -r "$HERE/../../../hub_api/requirements.txt"
fi
trap sweep_cleanup EXIT
sweep_access
"$VENV/bin/python" "$HERE/bundle_sweep.py" "$@"
