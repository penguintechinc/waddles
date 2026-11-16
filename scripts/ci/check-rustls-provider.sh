#!/usr/bin/env bash
# Fails if a core/ bin crate depends on rustls (directly or via a TLS feature) but never
# installs a process-level CryptoProvider (`install_default()`). Prints the denominator.
set -euo pipefail
cd "$(dirname "$0")/../.."
examined=0
bad=0
for toml in core/*/Cargo.toml; do
  dir=$(dirname "$toml")
  [ -f "$dir/src/main.rs" ] || continue
  grep -Eq 'rustls|tls-ring|tls-rustls' "$toml" || continue
  examined=$((examined + 1))
  if ! grep -rq 'install_default()' "$dir/src"; then
    echo "FAIL: $dir uses rustls but never calls install_default()"
    bad=$((bad + 1))
  fi
done
echo "rustls-provider check: examined=$examined failing=$bad"
[ "$examined" -gt 0 ] || { echo "FAIL: zero crates examined"; exit 1; }
[ "$bad" -eq 0 ]
