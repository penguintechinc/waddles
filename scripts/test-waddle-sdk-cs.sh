#!/usr/bin/env bash
# Runs waddle-sdk-cs's xUnit test suite in the same pinned, containerized
# .NET SDK image bundles/csharp/csping/Dockerfile uses -- never a
# host-machine SDK (rules/client.md Build & Distribution). Coverage is
# collected via coverlet and gated at 90% (rules/critical-rules.md Coverage).
#
# Bash 3.2 compatible (general.md) -- no associative arrays, no mapfile.
#
# Usage: scripts/test-waddle-sdk-cs.sh   (make test-waddle-sdk-cs)

set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "test-waddle-sdk-cs: FAIL -- docker is required and not on PATH" >&2
  exit 1
fi

DOTNET_SDK_IMAGE="mcr.microsoft.com/dotnet/sdk:10.0.401-noble@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29"
RESULTS_DIR="$(mktemp -d)"
trap 'rm -rf "$RESULTS_DIR"' EXIT

echo "test-waddle-sdk-cs: running xUnit tests + coverage in ${DOTNET_SDK_IMAGE}"
docker run --rm \
  -v "$(pwd)":/repo -v "$RESULTS_DIR":/covout -w /repo \
  -e HOME=/tmp -e DOTNET_CLI_HOME=/tmp -e NUGET_PACKAGES=/tmp/.nuget \
  -e DOTNET_NOLOGO=1 -e DOTNET_CLI_TELEMETRY_OPTOUT=1 \
  "$DOTNET_SDK_IMAGE" \
  dotnet test sdk/waddle-sdk-cs/tests/WaddleSdk.Tests.csproj -c Release \
    --settings sdk/waddle-sdk-cs/tests/coverlet.runsettings \
    --collect:"XPlat Code Coverage" --results-directory /covout

# Verification Integrity (rules/critical-rules.md): a coverage gate that
# never reads its own number cannot fail. Assert the 90% floor here rather
# than trusting `dotnet test`'s exit code alone (it does not fail on low
# coverage by itself).
report=$(find "$RESULTS_DIR" -name coverage.cobertura.xml | head -1)
if [ -z "$report" ]; then
  echo "test-waddle-sdk-cs: FAIL -- no coverage.cobertura.xml produced" >&2
  exit 1
fi
line_rate=$(grep -o 'line-rate="[0-9.]*"' "$report" | head -1 | grep -o '[0-9.]*')
echo "test-waddle-sdk-cs: line coverage = ${line_rate}"
awk -v r="$line_rate" 'BEGIN { exit !(r >= 0.90) }' || {
  echo "test-waddle-sdk-cs: FAIL -- line coverage ${line_rate} is below the 90% floor" >&2
  exit 1
}

echo "test-waddle-sdk-cs: PASS"
