#!/usr/bin/env bash
# RTC parity-gate wiring marker (plan T8 creates the gate, T10 deletes core/module_rtc).
#
# Counts the three parity artifacts T8 must add (L5 route-consistency script, L6 parity checker,
# L6 machine matrix). While they are missing AND core/module_rtc still exists, the gate is
# explicitly "not yet wired": a visible notice, exit 0. Once core/module_rtc is deleted (T10)
# with any artifact still missing, this exits 1 -- the Go module cannot leave before the parity
# gate that proves its replacement exists.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
artifacts=(
  "scripts/ci/rtc_route_consistency.py"
  "scripts/ci/rtc_parity_check.py"
  "docs/rtc-parity-matrix.json"
)

present=0
missing=""
for rel in "${artifacts[@]}"; do
  if [ -f "$REPO_ROOT/$rel" ]; then
    present=$((present + 1))
  else
    missing="${missing} ${rel}"
  fi
done

module_rtc_present=0
if [ -d "$REPO_ROOT/core/module_rtc" ]; then
  module_rtc_present=1
fi

echo "rtc-parity wiring: ${present}/${#artifacts[@]} parity artifacts present; core/module_rtc present=${module_rtc_present}"

if [ -z "$missing" ]; then
  echo "rtc-parity wiring: WIRED"
  exit 0
fi

if [ "$module_rtc_present" -eq 1 ]; then
  echo "::notice::rtc-parity gate NOT YET WIRED (plan T8 pending); missing:${missing}; core/module_rtc still present"
  exit 0
fi

echo "::error::rtc-parity gate NOT WIRED but core/module_rtc is deleted; missing:${missing}. T10 must not land before T8." >&2
exit 1
