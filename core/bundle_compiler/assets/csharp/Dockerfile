# Generic, reproducible, containerized build for ANY C#-toolchain app
# bundle -- `core/bundle_compiler/src/build/csharp.rs`'s `CSharpBuilder`
# writes this file plus a build context into a fresh temp directory for
# every build (see that module's doc comment for why `docker build`/
# `docker run`, not a host toolchain binary, is this recipe's approach).
#
# This is a deliberate, line-for-line generalization of the ORIGINAL,
# bundle-specific `bundles/csharp/csping/Dockerfile` (spec S18 R13 / D35):
# same base image digest, same pinned+sha256-verified WASI SDK, same
# non-root build user -- only the two bundle-specific bits are
# generalized:
#   1. the bundle's project directory is always copied to the fixed path
#      `bundles/csharp/bundle` (CSharpBuilder renames it on the way in),
#      not a bundle-specific name, so this Dockerfile never needs a
#      per-bundle `ARG`/templating step;
#   2. the final `CMD` globs for `*.wasm` in the publish output rather
#      than hardcoding an assembly name, since a bundle's `AssemblyName`
#      is author-chosen (`csping`, in the original).
#
# If `bundles/csharp/csping/Dockerfile`'s toolchain pins ever change
# (SDK version, WASI SDK version/sha256, dotnet-experimental feed), this
# file must be re-generalized from it by hand -- there is no shared
# single-source-of-truth template yet (a real follow-up, not this
# wiring pass's scope).
FROM mcr.microsoft.com/dotnet/sdk:10.0.401-noble@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29

# `bookworm`/Debian has no published .NET 10 SDK image as of this build
# (only `noble`, `alpine`, `azurelinux`, and `trixie-slim` exist for the
# `10.0` SDK on mcr.microsoft.com) -- Ubuntu Noble (an LTS release) is the
# `devops-containers.md` fallback-order-correct choice, not a violation.

# Non-root build user (rules/devops-containers.md Rootless Containers --
# no exception needed: this build has no genuine root requirement).
RUN groupadd --gid 10001 builder \
    && useradd --uid 10001 --gid builder --create-home --home-dir /home/builder \
       --shell /usr/sbin/nologin builder

ENV HOME=/home/builder \
    DOTNET_CLI_HOME=/home/builder \
    NUGET_PACKAGES=/home/builder/.nuget/packages \
    DOTNET_NOLOGO=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1

# Pinned, checksum-verified WASI SDK -- see the original Dockerfile's own
# header comment for why this is pre-seeded rather than left to
# NativeAOT-LLVM's live, unverified first-use download. Re-pin both ARGs
# (version + sha256) whenever `Microsoft.DotNet.ILCompiler.LLVM` (pinned
# per-bundle in each bundle's own `.csproj`) is bumped.
ARG WASI_SDK_RELEASE=wasi-sdk-29
ARG WASI_SDK_VERSION=29.0
ARG WASI_SDK_SHA256=87d1d1a2879d139cdc624b968efad3d4a97b8078cdff95e63ac88ecafd1a0171
RUN curl -fsSL -o /tmp/wasi-sdk.tar.gz \
      "https://github.com/WebAssembly/wasi-sdk/releases/download/${WASI_SDK_RELEASE}/wasi-sdk-${WASI_SDK_VERSION}-x86_64-linux.tar.gz" \
    && echo "${WASI_SDK_SHA256}  /tmp/wasi-sdk.tar.gz" | sha256sum -c - \
    && mkdir -p "/home/builder/.wasi-sdk/wasi-sdk-${WASI_SDK_VERSION}" \
    && tar -xzf /tmp/wasi-sdk.tar.gz --strip-components=1 \
         -C "/home/builder/.wasi-sdk/wasi-sdk-${WASI_SDK_VERSION}" \
    && rm /tmp/wasi-sdk.tar.gz \
    && chown -R builder:builder /home/builder

WORKDIR /repo
# `CSharpBuilder` (see that module's doc comment) writes its build context
# with exactly this layout: the normative `wit/waddle-bundle/stage.wit`
# vendored at the repo-root-relative path every bundle's own `.csproj`
# already expects (`<Wit Include="../../../wit/waddle-bundle/stage.wit" .../>`),
# and the bundle's own source tree copied to the fixed `bundles/csharp/bundle`
# path (plus this builder's own enforced `nuget.config`, overwriting
# whatever the bundle source provided).
COPY wit wit
COPY bundles/csharp/bundle bundles/csharp/bundle
RUN chown -R builder:builder /repo
WORKDIR /repo/bundles/csharp/bundle

USER builder
# `IlcExportUnmanagedEntrypoints`/`RuntimeIdentifier`/`SelfContained`/
# `PublishTrimmed` are passed as MSBuild property overrides here --
# unconditionally, on every build -- rather than requiring a bundle
# author's own `.csproj` to declare them (README "What a CSharpBuilder
# would need" item 3: "a bundle author should not need to know any of
# this"). Passing them again when a bundle's `.csproj` (like `csping`'s)
# already sets them is idempotent.
RUN dotnet build -c Release \
      -p:IlcExportUnmanagedEntrypoints=true \
      -p:RuntimeIdentifier=wasi-wasm \
      -p:SelfContained=true \
      -p:PublishTrimmed=true

# Globs rather than hardcoding an assembly name (see header comment) --
# `CSharpBuilder` always mounts its own output directory at `/out`.
CMD ["sh", "-c", "cp bin/Release/net10.0/wasi-wasm/publish/*.wasm /out/component.wasm"]
