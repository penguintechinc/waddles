# Reproducible, containerized build for the `csping` bundle (originally the
# C#-toolchain feasibility spike, spec
# docs/superpowers/specs/2026-09-14-rust-data-plane-design.md S18 R13 /
# D35; now also built on `sdk/waddle-sdk-cs` -- see this bundle's README).
# Mirrors core/bundle_executor/Dockerfile.rust's role for this bundle: a
# pinned, from-scratch build with no reliance on host machine .NET/
# componentize-dotnet setup (rules/client.md Build & Distribution: "All
# client builds must use containerized toolchain"). Identical to
# bundles/csharp/superpenguin-roll/Dockerfile other than the bundle path and
# output assembly name.
#
# Build (context = repo root; needs the sibling wit/ and sdk/waddle-sdk-cs/
# directories):
#   docker build -f bundles/csharp/csping/Dockerfile \
#     -t waddles/bundle-csping-build:spike .
#
# Extract the built component (bind-mount an output dir; the image's
# default CMD copies it there -- there is no "runtime" stage, this image
# only ever produces a build artifact, never runs as a service). Pass
# `--user "$(id -u):$(id -g)"` so the container writes into your bind
# mount as YOUR uid/gid, not the image's baked-in `builder` uid (10001) --
# whichever one doesn't match your host directory's ownership will fail
# with a permission error on the final `cp`:
#   mkdir -p /tmp/csping-out
#   docker run --rm --user "$(id -u):$(id -g)" \
#     -v /tmp/csping-out:/out waddles/bundle-csping-build:spike
#   ls /tmp/csping-out/csping.wasm
#
# Rootless end to end: the image never runs (build steps or the final
# CMD) as UID 0. NativeAOT-LLVM's own WASI SDK dependency (see below) is
# pinned by exact version + sha256 and verified before use, so this build
# needs network access only for `dotnet restore` (NuGet, itself hash-
# verified by the NuGet client) -- not for an unpinned, unverified binary
# blob.
FROM mcr.microsoft.com/dotnet/sdk:10.0.401-noble@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29

# `bookworm`/Debian has no published .NET 10 SDK image as of this build
# (checked against mcr.microsoft.com's own tag list: only `noble`
# (Ubuntu 24.04 LTS), `alpine`, `azurelinux`, and `trixie-slim`
# (Debian 13, preview-only) exist for the `10.0` SDK) -- per
# rules/devops-containers.md's own fallback order
# "Debian 12 -> 11 -> 13 -> Ubuntu LTS", Ubuntu Noble (an LTS release) is
# the correct next choice, not a rule violation.

# Non-root build user (rules/devops-containers.md Rootless Containers --
# no exception needed: this build has no genuine root requirement).
# HOME/DOTNET_CLI_HOME/NUGET_PACKAGES all point under this user's own
# home directory so `dotnet restore`/NativeAOT-LLVM never need to write
# outside it.
RUN groupadd --gid 10001 builder \
    && useradd --uid 10001 --gid builder --create-home --home-dir /home/builder \
       --shell /usr/sbin/nologin builder

ENV HOME=/home/builder \
    DOTNET_CLI_HOME=/home/builder \
    NUGET_PACKAGES=/home/builder/.nuget/packages \
    DOTNET_NOLOGO=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1

# Pinned, checksum-verified WASI SDK. NativeAOT-LLVM (this build's
# `Microsoft.DotNet.ILCompiler.LLVM` 10.0.0-rc.1.26306.1 dependency, see
# csping.csproj) fetches this itself, live and unverified, on first use
# if its cache directory (`$HOME/.wasi-sdk/wasi-sdk-<version>`) is empty
# -- confirmed by observing an unpinned build's own log line
# ("Downloading from
# https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-29/
# wasi-sdk-29.0-x86_64-linux.tar.gz"). Pre-seeding that exact directory
# here, from a version+sha256 we pin and verify ourselves, makes the
# toolchain skip its own download entirely (confirmed empirically:
# `docker run --network none` against a pre-seeded cache still builds
# successfully) and turns a silent trust-on-first-use fetch into a
# fail-closed one. If `Microsoft.DotNet.ILCompiler.LLVM` is ever bumped,
# rebuild once with network access, read the WASI SDK version/URL its
# log reports, and re-pin both ARGs below against the newly observed
# release asset's sha256.
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
COPY wit wit
COPY sdk/waddle-sdk-cs sdk/waddle-sdk-cs
COPY bundles/csharp/csping bundles/csharp/csping
RUN chown -R builder:builder /repo
WORKDIR /repo/bundles/csharp/csping

USER builder
RUN dotnet build -c Release

CMD ["cp", "/repo/bundles/csharp/csping/bin/Release/net10.0/wasi-wasm/publish/csping.wasm", "/out/csping.wasm"]
