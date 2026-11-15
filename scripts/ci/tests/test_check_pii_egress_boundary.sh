#!/usr/bin/env bash
# Negative + positive test for scripts/ci/check-pii-egress-boundary.sh: a fixture tree with a
# violation must fail, a clean one must pass, and an empty tree must fail (zero examined).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
gate="$here/../check-pii-egress-boundary.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mk() { mkdir -p "$tmp/$1/core/svc_action/src" "$tmp/$1/core/svc_ingest/src" "$tmp/$1/core/svc_presentation/src"; }

mk clean
echo 'fn main() {}' > "$tmp/clean/core/svc_action/src/lib.rs"
echo '// see egress_detokenizer for the grammar' > "$tmp/clean/core/svc_action/src/doc.rs"
echo 'use egress_detokenizer::Sink;' > "$tmp/clean/core/svc_ingest/src/egress.rs"
echo 'use egress_detokenizer::Sink;' > "$tmp/clean/core/svc_presentation/src/detok.rs"
bash "$gate" "$tmp/clean" >/dev/null || { echo "FAIL: clean tree rejected"; exit 1; }

mk bad_use
echo 'let t = egress_detokenizer::detokenize_resolving(a, b, c, d);' > "$tmp/bad_use/core/svc_action/src/lib.rs"
if bash "$gate" "$tmp/bad_use" >/dev/null 2>&1; then echo "FAIL: svc_action code use not rejected"; exit 1; fi

mk bad_dep
printf '[dependencies]\negress_detokenizer = { path = "../egress_detokenizer" }\n' > "$tmp/bad_dep/core/svc_action/Cargo.toml"
if bash "$gate" "$tmp/bad_dep" >/dev/null 2>&1; then echo "FAIL: svc_action Cargo dep not rejected"; exit 1; fi

mkdir -p "$tmp/empty"
if bash "$gate" "$tmp/empty" >/dev/null 2>&1; then echo "FAIL: zero files examined must fail"; exit 1; fi

echo "PASS: pii-egress-boundary gate (clean passes; code use, Cargo dep, and zero-examined all fail)"
