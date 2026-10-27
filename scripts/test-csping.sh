#!/usr/bin/env bash
# Runs the csping bundle's xUnit test suite (CspingLogic/CspingDispatch
# business logic, on the host CLR -- see
# bundles/csharp/csping/tests/Csping.Tests.csproj's header comment for why
# this is a separate project from the wasi-wasm bundle itself) in the same
# pinned, containerized .NET SDK image every other C# build/test in this
# repo uses. Mirrors scripts/test-superpenguin-roll.sh exactly.
#
# Bash 3.2 compatible (general.md) -- no associative arrays, no mapfile.
#
# Usage: scripts/test-csping.sh   (make test-csping)

set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "test-csping: FAIL -- docker is required and not on PATH" >&2
  exit 1
fi

DOTNET_SDK_IMAGE="mcr.microsoft.com/dotnet/sdk:10.0.401-noble@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29"
RESULTS_DIR="$(mktemp -d)"
trap 'rm -rf "$RESULTS_DIR"' EXIT

echo "test-csping: running xUnit tests + coverage in ${DOTNET_SDK_IMAGE}"
docker run --rm \
  -v "$(pwd)":/repo -v "$RESULTS_DIR":/covout -w /repo \
  -e HOME=/tmp -e DOTNET_CLI_HOME=/tmp -e NUGET_PACKAGES=/tmp/.nuget \
  -e DOTNET_NOLOGO=1 -e DOTNET_CLI_TELEMETRY_OPTOUT=1 \
  "$DOTNET_SDK_IMAGE" \
  dotnet test bundles/csharp/csping/tests/Csping.Tests.csproj -c Release \
    --settings bundles/csharp/csping/tests/coverlet.runsettings \
    --collect:"XPlat Code Coverage" --results-directory /covout

# Verification Integrity (rules/critical-rules.md) -- see
# scripts/test-waddle-sdk-cs.sh's identical gate for the rationale.
report=$(find "$RESULTS_DIR" -name coverage.cobertura.xml | head -1)
if [ -z "$report" ]; then
  echo "test-csping: FAIL -- no coverage.cobertura.xml produced" >&2
  exit 1
fi
line_rate=$(grep -o 'line-rate="[0-9.]*"' "$report" | head -1 | grep -o '[0-9.]*')
echo "test-csping: line coverage = ${line_rate}"
awk -v r="$line_rate" 'BEGIN { exit !(r >= 0.90) }' || {
  echo "test-csping: FAIL -- line coverage ${line_rate} is below the 90% floor" >&2
  exit 1
}

echo "test-csping: PASS"
