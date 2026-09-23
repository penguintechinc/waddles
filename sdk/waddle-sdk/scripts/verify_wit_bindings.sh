#!/usr/bin/env bash
# Verifies waddle-sdk's WIT binding shapes against reality (spec Sec18 R1).
#
# `waddle_sdk/db.py`, `http.py`, `kv.py`, `log.py`, `clock.py`, `relay.py`,
# and this SDK's own test doubles (`tests/wit_shapes.py`) all hardcode the
# exact class/field/enum shapes `componentize-py`'s code generator produces
# from `wit/waddle-bundle/stage.wit` -- confirmed once by hand during
# development (see db.py's module docstring) by running `componentize-py
# bindings` and reading the output. This script re-runs that generation
# step and asserts the shapes are still what the facade assumes, so a
# future edit to the WIT file or a componentize-py upgrade that silently
# changes the generated shape is caught here, in CI, rather than only at
# real-build time inside bundle-compiler.
#
# Requires `componentize-py` on PATH (`pip install componentize-py==0.25.1`).
# Exits non-zero, naming the missing assertion, on any mismatch -- never
# reports success without having examined the generated files.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${SDK_ROOT}/../.." && pwd)"
WIT_DIR="${REPO_ROOT}/wit/waddle-bundle"

if [ ! -f "${WIT_DIR}/stage.wit" ]; then
    echo "FAIL: ${WIT_DIR}/stage.wit not found -- run from a full waddles checkout" >&2
    exit 1
fi

if ! command -v componentize-py >/dev/null 2>&1; then
    echo "FAIL: componentize-py not on PATH -- install with 'pip install componentize-py==0.25.1'" >&2
    exit 1
fi

OUT_DIR="$(mktemp -d)"
trap 'rm -rf "${OUT_DIR}"' EXIT

echo "Generating bindings from ${WIT_DIR}/stage.wit into ${OUT_DIR} ..."
componentize-py -d "${WIT_DIR}" -w stage bindings "${OUT_DIR}"

CHECKS=0
FAILURES=0

check() {
    file="$1"
    pattern="$2"
    label="$3"
    CHECKS=$((CHECKS + 1))
    if ! grep -qF "${pattern}" "${OUT_DIR}/${file}"; then
        echo "FAIL [${label}]: pattern not found in ${file}: ${pattern}" >&2
        FAILURES=$((FAILURES + 1))
    fi
}

# wit_world/imports/db.py -- waddle_sdk/db.py's _to_wit_value/_from_wit_value
check "wit_world/imports/db.py" "class Value_NullValue:" "db.Value_NullValue"
check "wit_world/imports/db.py" "class Value_BoolValue:" "db.Value_BoolValue"
check "wit_world/imports/db.py" "class Value_IntValue:" "db.Value_IntValue"
check "wit_world/imports/db.py" "class Value_FloatValue:" "db.Value_FloatValue"
check "wit_world/imports/db.py" "class Value_TextValue:" "db.Value_TextValue"
check "wit_world/imports/db.py" "class Value_BytesValue:" "db.Value_BytesValue"
check "wit_world/imports/db.py" "class Rows:" "db.Rows"
check "wit_world/imports/db.py" "columns: List[str]" "db.Rows.columns"
check "wit_world/imports/db.py" "rows: List[List[Value]]" "db.Rows.rows"
check "wit_world/imports/db.py" "rows_affected: int" "db.Rows.rows_affected"
check "wit_world/imports/db.py" "def execute(statement: str, params: List[Value]) -> Rows:" "db.execute signature"

# wit_world/imports/http.py -- waddle_sdk/http.py's Request/Response/error classification
check "wit_world/imports/http.py" "class Request:" "http.Request"
check "wit_world/imports/http.py" "class Response:" "http.Response"
check "wit_world/imports/http.py" "class Header:" "http.Header"
check "wit_world/imports/http.py" "class Error_Denied:" "http.Error_Denied"
check "wit_world/imports/http.py" "class Error_Timeout:" "http.Error_Timeout"
check "wit_world/imports/http.py" "class Error_TooLarge:" "http.Error_TooLarge"
check "wit_world/imports/http.py" "class Error_RateLimited:" "http.Error_RateLimited"
check "wit_world/imports/http.py" "class Error_Transport:" "http.Error_Transport"
check "wit_world/imports/http.py" "def send(req: Request) -> Response:" "http.send signature"

# wit_world/imports/kv.py -- waddle_sdk/kv.py
check "wit_world/imports/kv.py" "def get(key: str) -> Optional[bytes]:" "kv.get signature"
check "wit_world/imports/kv.py" "def set(key: str, value: bytes, ttl_seconds: int) -> None:" "kv.set signature"
check "wit_world/imports/kv.py" "def increment(key: str, delta: int, ttl_seconds: int) -> int:" "kv.increment signature"

# wit_world/imports/relay.py -- waddle_sdk/relay.py
check "wit_world/imports/relay.py" "def push(provider: str, message_json: str) -> None:" "relay.push signature"

# wit_world/imports/log.py -- waddle_sdk/log.py's Level enum (exact int values matter)
check "wit_world/imports/log.py" "class Level(Enum):" "log.Level"
check "wit_world/imports/log.py" "ERROR = 0" "log.Level.ERROR"
check "wit_world/imports/log.py" "WARN = 1" "log.Level.WARN"
check "wit_world/imports/log.py" "INFO = 2" "log.Level.INFO"
check "wit_world/imports/log.py" "DEBUG = 3" "log.Level.DEBUG"
check "wit_world/imports/log.py" "def write(lvl: Level, message: str, fields_json: str) -> None:" "log.write signature"

# wit_world/imports/clock.py -- waddle_sdk/clock.py
check "wit_world/imports/clock.py" "def now_millis() -> int:" "clock.now_millis signature"
check "wit_world/imports/clock.py" "def now_rfc3339() -> str:" "clock.now_rfc3339 signature"
check "wit_world/imports/clock.py" "def monotonic_nanos() -> int:" "clock.monotonic_nanos signature"

# wit_world/imports/context.py -- waddle_sdk/flask_core/bundle_runtime.py's bundle_context()
check "wit_world/imports/context.py" "class BundleContext:" "context.BundleContext"
check "wit_world/imports/context.py" "def get_context() -> BundleContext:" "context.get_context signature"

# wit_world/imports/types.py -- waddle_sdk/flask_core/stream_pipeline.py's from_wit_record/to_wit_record
check "wit_world/imports/types.py" "class PlatformEvent:" "types.PlatformEvent"
check "wit_world/imports/types.py" "payload_json: str" "types.PlatformEvent.payload_json"
check "wit_world/imports/types.py" "class StageEnvelope:" "types.StageEnvelope"
check "wit_world/imports/types.py" "class TransportResult:" "types.TransportResult"
check "wit_world/imports/types.py" "class TransportError:" "types.TransportError"
check "wit_world/imports/types.py" "class UnsupportedStage:" "types.UnsupportedStage"

# wit_world/exports/__init__.py -- waddle_sdk/_component_entry.py's WitWorld.transform/dispatch
check "wit_world/exports/__init__.py" "def transform(self, event: types.PlatformEvent) -> Optional[types.PlatformEvent]:" "exports.transform signature"
check "wit_world/exports/__init__.py" "def dispatch(self, envelope: types.StageEnvelope, config: str) -> types.TransportResult:" "exports.dispatch signature"

echo ""
echo "Checked ${CHECKS} binding-shape assertions against componentize-py's real generated output."
if [ "${FAILURES}" -gt 0 ]; then
    echo "FAILED: ${FAILURES} of ${CHECKS} assertions did not match -- waddle-sdk's facade/test-doubles" >&2
    echo "        have drifted from the real WIT binding shapes. Update db.py/http.py/kv.py/log.py/" >&2
    echo "        clock.py/relay.py/_component_entry.py/stream_pipeline.py and tests/wit_shapes.py." >&2
    exit 1
fi
echo "PASS: all ${CHECKS} binding-shape assertions matched."
