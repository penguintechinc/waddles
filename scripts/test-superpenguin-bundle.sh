#!/usr/bin/env bash
# Runs one SuperPenguin C# bundle's xUnit suite (bundles/csharp/superpenguin-<name>/tests) in the pinned containerized .NET SDK
# image and enforces the 90% line-coverage floor on the bundle's own logic. Same mechanics as scripts/test-superpenguin-roll.sh,
# parameterized over the bundle name (slots | gamble | defuse | ...). Prints the test count and the coverage; a missing coverage
# report, a zero-test run or coverage below the floor is a FAILURE (rules/critical-rules.md Verification Integrity).
#
# Usage: scripts/test-superpenguin-bundle.sh <name>   (make test-superpenguin-<name>)
set -euo pipefail
cd "$(dirname "$0")/.."

name="${1:?usage: test-superpenguin-bundle.sh <slots|gamble|defuse|...>}"
case "$name" in *[!a-z0-9-]*|"") echo "test-superpenguin-$name: FAIL -- bad bundle name" >&2; exit 1 ;; esac
label="test-superpenguin-${name}"
camel="$(tr '[:lower:]' '[:upper:]' <<< "${name:0:1}")${name:1}"
project="bundles/csharp/superpenguin-${name}/tests/Superpenguin${camel}.Tests.csproj"
settings="bundles/csharp/superpenguin-${name}/tests/coverlet.runsettings"

if ! command -v docker >/dev/null 2>&1; then
  echo "${label}: FAIL -- docker is required and not on PATH" >&2
  exit 1
fi
for f in "$project" "$settings"; do
  [ -f "$f" ] || { echo "${label}: FAIL -- expected file missing: $f" >&2; exit 1; }
done

DOTNET_SDK_IMAGE="mcr.microsoft.com/dotnet/sdk:10.0.401-noble@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29"
RESULTS_DIR="$(mktemp -d)"
trap 'rm -rf "$RESULTS_DIR"' EXIT
log="$RESULTS_DIR/test.log"

echo "${label}: running xUnit tests + coverage in ${DOTNET_SDK_IMAGE}"
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$(pwd)":/repo -v "$RESULTS_DIR":/covout -w /repo \
  -e HOME=/tmp -e DOTNET_CLI_HOME=/tmp -e NUGET_PACKAGES=/tmp/.nuget \
  -e DOTNET_NOLOGO=1 -e DOTNET_CLI_TELEMETRY_OPTOUT=1 \
  "$DOTNET_SDK_IMAGE" \
  dotnet test "$project" -c Release --settings "$settings" \
    --collect:"XPlat Code Coverage" --results-directory /covout 2>&1 | tee "$log"

passed=$(grep -Eo 'Passed: +[0-9]+' "$log" | tail -1 | grep -Eo '[0-9]+' || true)
if [ -z "${passed:-}" ] || [ "$passed" -lt 1 ]; then
  echo "${label}: FAIL -- no passing tests reported (zero examined is a failure)" >&2
  exit 1
fi
echo "${label}: tests passed = ${passed}"

report=$(find "$RESULTS_DIR" -name coverage.cobertura.xml | head -1)
if [ -z "$report" ]; then
  echo "${label}: FAIL -- no coverage.cobertura.xml produced" >&2
  exit 1
fi
line_rate=$(grep -o 'line-rate="[0-9.]*"' "$report" | head -1 | grep -o '[0-9.]*')
echo "${label}: line coverage = ${line_rate}"
awk -v r="$line_rate" 'BEGIN { exit !(r >= 0.90) }' || {
  echo "${label}: FAIL -- line coverage ${line_rate} is below the 90% floor" >&2
  exit 1
}

echo "${label}: PASS"
