#!/usr/bin/env bash
# Stand-in for `dotnet list package --vulnerable --include-transitive
# --format json` reporting zero vulnerable packages -- used by the
# "examines a real lockfile" happy-path test so it never depends on a
# live network call to NuGet's advisory feed (CI runs with no network
# access; a real `dotnet` invocation there fails exactly like
# `audit-stub-network-failure.sh` simulates, which now correctly blocks
# the build per the fail-closed fix -- so this test must not exercise the
# real tool at all). Mirrors real `dotnet list package --vulnerable`'s
# JSON shape exactly, same as `dotnet-audit-stub-vulnerable.sh`.
set -euo pipefail
printf '{"projects": [{"path": "fixture.csproj", "frameworks": [{"framework": "net8.0", "topLevelPackages": [{"id": "Newtonsoft.Json", "resolvedVersion": "13.0.3", "vulnerabilities": []}], "transitivePackages": []}]}]}'
