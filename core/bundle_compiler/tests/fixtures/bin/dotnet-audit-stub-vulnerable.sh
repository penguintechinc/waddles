#!/usr/bin/env bash
# Stand-in for `dotnet list package --vulnerable --include-transitive
# --format json` reporting one high-severity advisory -- exercises
# scan::sast's `dependency_vulnerability` blocking path without depending
# on a real, currently-vulnerable NuGet package (which would make this
# test flaky as advisories are published/fixed over time). Mirrors real
# `dotnet list package --vulnerable`'s JSON shape exactly.
set -euo pipefail
printf '{"projects": [{"path": "fixture.csproj", "frameworks": [{"framework": "net8.0", "topLevelPackages": [{"id": "Newtonsoft.Json", "resolvedVersion": "13.0.3", "vulnerabilities": [{"severity": "High", "advisoryurl": "https://example.invalid/GHSA-fake"}]}], "transitivePackages": []}]}]}'
