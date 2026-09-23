#!/usr/bin/env bash
# Regenerates src/generated/*.d.ts (ambient `declare module 'waddle:bundle/*@1.0.0'`
# typings) from the normative wit/waddle-bundle/stage.wit via `jco guest-types`.
#
# Run this whenever wit/waddle-bundle/stage.wit changes. The generated files
# are committed so `tsc` type-checks the SDK without requiring jco at
# typecheck time; this script is how they are kept in sync.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

WIT_DIR="../../wit/waddle-bundle"
OUT_DIR="src/generated"

if [ ! -f "${WIT_DIR}/stage.wit" ]; then
  echo "error: ${WIT_DIR}/stage.wit not found (run from sdk/waddle-sdk-js)" >&2
  exit 1
fi

rm -rf "${OUT_DIR}"
npx jco guest-types "${WIT_DIR}" -o "${OUT_DIR}" --world-name stage

banner="// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).
"

for f in $(find "${OUT_DIR}" -name '*.d.ts'); do
  tmp="$(mktemp)"
  printf '%s\n' "${banner}" >"${tmp}"
  cat "${f}" >>"${tmp}"
  mv "${tmp}" "${f}"
done

echo "wrote $(find "${OUT_DIR}" -name '*.d.ts' | wc -l) generated .d.ts files to ${OUT_DIR}"
