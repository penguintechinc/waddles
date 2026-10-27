#!/usr/bin/env bash
# Sanity check for wit/waddle-connector/connector.wit -- cheap grep-based
# structural assertions runnable with no WASM toolchain installed, mirroring
# wit/waddle-bundle/stage_wit_test.sh. The authoritative check is
# `wasm-tools component wit wit/waddle-connector` (resolves the
# `deps/waddle-bundle/stage.wit` cross-package imports), run in CI once the
# pinned toolchain image exists.
set -euo pipefail

FILE="wit/waddle-connector/connector.wit"
DEPS_FILE="wit/waddle-connector/deps/waddle-bundle/stage.wit"
test -f "$FILE"
test -f "$DEPS_FILE"
grep -q "^package waddle:connector@1.0.0;" "$FILE"
grep -q "^world connector {" "$FILE"

# `identity` is connector-only -- never present in waddle:bundle/stage.wit.
grep -q "^interface identity {" "$FILE"
grep -q "  lookup: func(key: identity-key) -> result<identity-record, error>;" "$FILE"
! grep -q "interface identity {" "wit/waddle-bundle/stage.wit"

# receiver exports: on-connect, on-frame, on-heartbeat-due, on-disconnect.
for export_fn in on-connect on-frame on-heartbeat-due on-disconnect; do
  grep -q "  $export_fn:" "$FILE"
done

# sender exports: build-request.
grep -q "  build-request:" "$FILE"

grep -q "import identity;" "$FILE"
for iface in http log clock "%flags"; do
  grep -q "import waddle:bundle/$iface@1.0.0;" "$FILE"
done
for exp in receiver sender; do
  grep -q "export $exp;" "$FILE"
done

echo "PASS: connector.wit declares identity + 4 receiver exports + 1 sender export + 4 imports + 2 exports"
