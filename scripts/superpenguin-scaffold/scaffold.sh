#!/usr/bin/env bash
# Scaffolds a SuperPenguinTV C# utility-port bundle from the boilerplate of bundles/csharp/superpenguin-first (host adapter, WIT
# export shims, Dockerfile, csproj, test world) plus this directory's BundleKit/Kit templates. Business logic, manifests and
# tests are then written by hand.  Usage: scripts/superpenguin-scaffold/scaffold.sh <lowercase-name>   (e.g. lastseen)
set -euo pipefail
cd "$(dirname "$0")/../.."
name="${1:?usage: scaffold.sh <lowercase-name>}"
case "$name" in *[!a-z0-9]*|"") echo "scaffold: bad name" >&2; exit 1 ;; esac
camel="$(tr '[:lower:]' '[:upper:]' <<< "${name:0:1}")${name:1}"
src=bundles/csharp/superpenguin-first
dst="bundles/csharp/superpenguin-${name}"
ns="WaddleBundleSuperpenguin${camel}"
[ -d "$src" ] || { echo "scaffold: missing $src" >&2; exit 1; }
[ ! -e "$dst" ] || { echo "scaffold: $dst exists" >&2; exit 1; }
mkdir -p "$dst/tests"

ren() {
  sed -e "s/superpenguin-first/superpenguin-${name}/g" -e "s/superpenguin_first/superpenguin_${name}/g" \
      -e "s/WaddleBundleSuperpenguinFirst/${ns}/g" -e "s/SuperpenguinFirst/Superpenguin${camel}/g" \
      -e "s/FirstLogic/${camel}Logic/g" -e "s/FirstDispatch/${camel}Dispatch/g" -e "s/FirstJson/${camel}Json/g" "$1"
}

for f in Dockerfile nuget.config WaddleHostAdapter.cs ProcessStageExportsImpl.cs ActionStageExportsImpl.cs \
         StreamingLifecycleExportsImpl.cs superpenguin-first.csproj LICENSE FirstDispatch.cs; do
  out="${f/superpenguin-first/superpenguin-${name}}"
  out="${out/FirstDispatch/${camel}Dispatch}"
  ren "$src/$f" > "$dst/$out"
done

sed -e "s/@NS@/${ns}/" scripts/superpenguin-scaffold/BundleKit.cs.tmpl > "$dst/BundleKit.cs"
sed -e "s/@NS@/${ns}/" scripts/superpenguin-scaffold/TestKit.cs.tmpl > "$dst/tests/TestKit.cs"
sed -e "s/@NS@/${ns}/" scripts/superpenguin-scaffold/BundleKitTests.cs.tmpl > "$dst/tests/BundleKitTests.cs"

ren "$src/tests/SuperpenguinFirst.Tests.csproj" \
  | sed -e "/ReplayGuard.cs/d" -e "s#.*<Compile Include=\"../${camel}Logic.cs\".*#&\n    <Compile Include=\"../BundleKit.cs\" Link=\"BundleKit.cs\" />#" \
  > "$dst/tests/Superpenguin${camel}.Tests.csproj"
ren "$src/tests/coverlet.runsettings" > "$dst/tests/coverlet.runsettings"

# TestWorld: same fakes, plus a settable http client and a per-event config JSON (weather/timers need them).
ren "$src/tests/TestWorld.cs" \
  | sed -e 's/public IHttpClient Http => _inner.Http;/public IHttpClient Http => world.Http;/' \
        -e 's/    public Exception? ActorFailure { get; set; }/    public IHttpClient Http { get; set; } = new FakeHttpClient();\n    public string ConfigJson { get; set; } = "{}";\n    public Exception? ActorFailure { get; set; }/' \
        -e 's/messageId, "{}"), this, actor)/messageId, ConfigJson), this, actor)/' \
  > "$dst/tests/TestWorld.cs"
# Dispatch tests are identical across every chat-reply bundle (relay to the event's own platform, fail-loud paths).
ren "$src/tests/FirstDispatchTests.cs" | sed -e "s/superpenguin\.first/superpenguin.${name}/g" > "$dst/tests/${camel}DispatchTests.cs"
echo "scaffold: created $dst (next: ${camel}Logic.cs, ${camel}Json.cs if needed, bundle.yaml, hub-manifest.yaml, README.md, tests)"
