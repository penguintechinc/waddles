# Reproducible, containerized build for the `superpenguin-roll` C# bundle.
# Mirrors bundles/csharp/csping/Dockerfile exactly (same pinned toolchain,
# same rootless build user, same pre-seeded WASI SDK cache) -- see that
# Dockerfile's header comment for the full rationale; this file only repeats
# the mechanics for this bundle's own path.
#
# Build (context = repo root; needs the sibling wit/ and sdk/waddle-sdk-cs/
# directories):
#   docker build -f bundles/csharp/superpenguin-roll/Dockerfile \
#     -t waddles/bundle-superpenguin-roll-build:latest .
#
# Extract the built component (bind-mount an output dir; pass
# `--user "$(id -u):$(id -g)"` so the container writes into your bind mount
# as YOUR uid/gid, not the image's baked-in `builder` uid 10001):
#   mkdir -p /tmp/superpenguin-roll-out
#   docker run --rm --user "$(id -u):$(id -g)" \
#     -v /tmp/superpenguin-roll-out:/out waddles/bundle-superpenguin-roll-build:latest
#   ls /tmp/superpenguin-roll-out/superpenguin_roll.wasm
#
# Rootless end to end; WASI SDK pinned + sha256-verified (never NativeAOT-
# LLVM's own live, unverified first-use download) -- identical pins to
# csping's Dockerfile since both bundles use the exact same
# Microsoft.DotNet.ILCompiler.LLVM version (see this bundle's own
# superpenguin-roll.csproj).
FROM mcr.microsoft.com/dotnet/sdk:10.0.401-noble@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29

RUN groupadd --gid 10001 builder \
    && useradd --uid 10001 --gid builder --create-home --home-dir /home/builder \
       --shell /usr/sbin/nologin builder

ENV HOME=/home/builder \
    DOTNET_CLI_HOME=/home/builder \
    NUGET_PACKAGES=/home/builder/.nuget/packages \
    DOTNET_NOLOGO=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1

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
COPY bundles/csharp/superpenguin-roll bundles/csharp/superpenguin-roll
RUN chown -R builder:builder /repo
WORKDIR /repo/bundles/csharp/superpenguin-roll

USER builder
RUN dotnet build -c Release

CMD ["cp", "/repo/bundles/csharp/superpenguin-roll/bin/Release/net10.0/wasi-wasm/publish/superpenguin_roll.wasm", "/out/superpenguin_roll.wasm"]
