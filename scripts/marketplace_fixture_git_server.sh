#!/usr/bin/env bash
# Thin launcher for scripts/marketplace_fixture_git_server.py (see its docstring + docs/guides/
# marketplace-sources-e2e.md). Needs a python3 with the hub_api deps (HUB_PYTHON overrides) and
# FIXTURE_ADMIN_TOKEN in the ENVIRONMENT (never an argument). Any missing prerequisite exits non-zero.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${FIXTURE_ADMIN_TOKEN:?FIXTURE_ADMIN_TOKEN must be set in the environment}"
command -v git >/dev/null || { echo "ERROR: git not found" >&2; exit 2; }
exec "${HUB_PYTHON:-python3}" "$here/marketplace_fixture_git_server.py" "$@"
