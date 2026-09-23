#!/usr/bin/env bash
# Stand-in for `pip-audit -r <requirements> --format json` reporting one
# known vulnerability -- exercises scan::sast's `dependency_vulnerability`
# blocking path without depending on a live network call to PyPI's
# advisory feed (CI runs with no network access; a real `pip-audit`
# invocation there fails exactly like `audit-stub-network-failure.sh`
# simulates, which now correctly blocks the build per the fail-closed fix
# -- so this test must not exercise the real tool at all) or on a
# real, currently-published PyPI advisory (which would make this test
# flaky as advisories are published/fixed upstream over time -- same
# rationale as `npm-audit-stub-vulnerable.sh`). Mirrors real pip-audit's
# JSON shape exactly (see scan::sast's `dependencies[].vulns` parsing).
set -euo pipefail
printf '{"dependencies": [{"name": "requests", "version": "2.31.0", "vulns": [{"id": "FABRICATED-TEST-VULN-0001", "fix_versions": ["2.32.0"], "description": "fabricated for hermetic test coverage"}]}]}'
