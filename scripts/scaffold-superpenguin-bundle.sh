#!/usr/bin/env bash
# Scaffolds the mechanical boilerplate of a SuperPenguinTV C# app bundle (stage-next-tools world) by stamping
# bundles/csharp/superpenguin-roll (host adapter, WIT export shims, csproj, Dockerfile, nuget.config, LICENSE), the First bundle's
# test world and the shared sources' test template into bundles/csharp/superpenguin-<name>. It writes NO business logic,
# manifests or README. Refuses to overwrite an existing bundle directory.
#
# Usage: scripts/scaffold-superpenguin-bundle.sh <name>     (name = [a-z0-9]+, e.g. wheel, bonustickets)
# Afterwards write, by hand: <P>Logic.cs (class <P>Logic : WaddleProcessStageNext, parameterless ctor), optional <P>Json.cs
# (own [JsonSerializable] context), tests/<P>LogicTests.cs, bundle.yaml, hub-manifest.yaml, README.md, overlay/.
# Pascal name <P> = name with its first letter upper-cased (matches scripts/test-superpenguin-bundle.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

name="${1:?usage: scaffold-superpenguin-bundle.sh <name>}"
case "$name" in *[!a-z0-9]*|"") echo "scaffold: FAIL -- name must be [a-z0-9]+" >&2; exit 1 ;; esac
pascal="$(tr '[:lower:]' '[:upper:]' <<< "${name:0:1}")${name:1}"
src="bundles/csharp/superpenguin-roll"
first="bundles/csharp/superpenguin-first"
shared="bundles/csharp/superpenguin-shared"
dst="bundles/csharp/superpenguin-${name}"
asm="superpenguin_${name}"

for d in "$src" "$first" "$shared"; do
  [ -d "$d" ] || { echo "scaffold: FAIL -- template directory missing: $d" >&2; exit 1; }
done
[ ! -e "$dst" ] || { echo "scaffold: FAIL -- $dst already exists" >&2; exit 1; }
mkdir -p "$dst/tests"

# Renames the roll template's identifiers to the new bundle's.
rename() {
  sed -e "s/WaddleBundleSuperpenguinRoll/WaddleBundleSuperpenguin${pascal}/g" \
      -e "s/WaddleBundleSuperpenguinFirst/WaddleBundleSuperpenguin${pascal}/g" \
      -e "s/superpenguin_roll/${asm}/g" \
      -e "s/superpenguin-roll/superpenguin-${name}/g" \
      -e "s/SuperpenguinRoll/Superpenguin${pascal}/g" \
      -e "s/RollLogic/${pascal}Logic/g" \
      -e "s/RollJson/${pascal}Json/g" \
      -e "s/RollDispatch/ReplyDispatch/g" "$@"
}

cp "$src/nuget.config" "$src/LICENSE" "$dst/"
rename "$src/WaddleHostAdapter.cs" > "$dst/WaddleHostAdapter.cs"
rename "$src/StreamingLifecycleExportsImpl.cs" > "$dst/StreamingLifecycleExportsImpl.cs"
for f in ProcessStageExportsImpl ActionStageExportsImpl; do
  rename "$src/$f.cs" | sed -e "s/^using WaddleBundleSuperpenguin${pascal};\$/using WaddleBundleSuperpenguin${pascal};\nusing SuperPenguinShared;/" > "$dst/$f.cs"
done

# Bundle csproj: the template plus a link to the shared sources.
rename "$src/superpenguin-roll.csproj" \
  | sed -e 's#<Compile Remove="tests/\*\*/\*.cs" />#<Compile Remove="tests/**/*.cs" />\n    <Compile Include="../superpenguin-shared/*.cs" Link="shared/%(Filename)%(Extension)" />#' \
  > "$dst/superpenguin-${name}.csproj"

# Dockerfile: also copy the shared sources.
rename "$src/Dockerfile" \
  | sed -e 's#^COPY sdk/waddle-sdk-cs sdk/waddle-sdk-cs$#COPY sdk/waddle-sdk-cs sdk/waddle-sdk-cs\nCOPY bundles/csharp/superpenguin-shared bundles/csharp/superpenguin-shared#' \
  > "$dst/Dockerfile"
grep -q 'superpenguin-shared' "$dst/Dockerfile" || { echo "scaffold: FAIL -- Dockerfile shared COPY not stamped" >&2; exit 1; }

# Tests: coverage settings, project, world, shared-source tests.
rename "$src/tests/coverlet.runsettings" \
  | sed -e "s#\[Superpenguin${pascal}.Tests\]WaddleSdk.Tests.\*#[Superpenguin${pascal}.Tests]WaddleSdk.Tests.*,[Superpenguin${pascal}.Tests]SuperPenguinShared.Tests.*#" \
  > "$dst/tests/coverlet.runsettings"

cat > "$dst/tests/Superpenguin${pascal}.Tests.csproj" <<EOF
<Project Sdk="Microsoft.NET.Sdk">

  <!--
    xUnit tests for the \`superpenguin-${name}\` bundle (${pascal}Logic and the shared sources), run on the normal host CLR. Not a
    ProjectReference to the wasi-wasm bundle project: the logic sources are linked in (real source, not a copy) while the
    wit-bindgen-dependent adapter/export shims are excluded. Component behaviour is covered by the real-executor e2e matrix.
    Pascal-case name and the coverlet settings follow scripts/test-superpenguin-bundle.sh.
  -->

  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <IsPackable>false</IsPackable>
    <RootNamespace>WaddleBundleSuperpenguin${pascal}.Tests</RootNamespace>
  </PropertyGroup>

  <ItemGroup>
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.14.1" />
    <PackageReference Include="xunit" Version="2.9.3" />
    <PackageReference Include="xunit.runner.visualstudio" Version="2.8.2" />
    <PackageReference Include="coverlet.collector" Version="10.1.0" />
  </ItemGroup>

  <ItemGroup>
    <ProjectReference Include="../../../../sdk/waddle-sdk-cs/waddle-sdk-cs.csproj" />
  </ItemGroup>

  <ItemGroup>
    <!-- Every bundle source file except the wit-bindgen adapter/export shims and the tests directory. -->
    <Compile Include="../*.cs" Exclude="../WaddleHostAdapter.cs;../ProcessStageExportsImpl.cs;../ActionStageExportsImpl.cs;../StreamingLifecycleExportsImpl.cs" Link="Bundle/%(Filename)%(Extension)" />
    <Compile Include="../../superpenguin-shared/*.cs" Link="shared/%(Filename)%(Extension)" />
    <Compile Include="../../../../sdk/waddle-sdk-cs/tests/Fakes.cs" Link="SdkFakes.cs" />
    <Compile Include="../../../../sdk/waddle-sdk-cs/tests/FakesNext.cs" Link="SdkFakesNext.cs" />
  </ItemGroup>

</Project>
EOF

rename "$first/tests/TestWorld.cs" \
  | sed -e 's/public IOverlayClient Overlay => _inner.Overlay;/public IOverlayClient Overlay => world.Overlay;/' \
        -e 's#^    public FakeKvClient Kv { get; } = new();#    public FakeKvClient Kv { get; } = new();\n    public IOverlayClient Overlay { get; set; } = new FakeOverlayClient();\n    public FakeOverlayClient Pushed => (FakeOverlayClient)Overlay;#' \
  > "$dst/tests/TestWorld.cs"
grep -q 'world.Overlay' "$dst/tests/TestWorld.cs" || { echo "scaffold: FAIL -- TestWorld overlay seam not stamped" >&2; exit 1; }

sed -e "s/__NS__/WaddleBundleSuperpenguin${pascal}/g" "$shared/SharedSourcesTests.cs.tmpl" > "$dst/tests/SharedSourcesTests.cs"

echo "scaffold: created $dst -- now write ${pascal}Logic.cs, tests/${pascal}LogicTests.cs, manifests, README"
