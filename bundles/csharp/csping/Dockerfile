# Reproducible, containerized build for the `csping` C#-toolchain
# feasibility spike bundle (spec
# docs/superpowers/specs/2026-09-14-rust-data-plane-design.md S18 R13 /
# D35). Mirrors core/bundle_executor/Dockerfile.rust's role for this
# bundle: a pinned, from-scratch build with no reliance on host machine
# .NET/componentize-dotnet setup (rules/client.md Build & Distribution:
# "All client builds must use containerized toolchain").
#
# Build (context = repo root; needs the sibling wit/ directory):
#   docker build -f bundles/csharp/csping/Dockerfile \
#     -t waddles/bundle-csping-build:spike .
#
# Extract the built component (bind-mount an output dir; the image's
# default CMD copies it there -- there is no "runtime" stage, this image
# only ever produces a build artifact, never runs as a service):
#   mkdir -p /tmp/csping-out
#   docker run --rm -v /tmp/csping-out:/out waddles/bundle-csping-build:spike
#   ls /tmp/csping-out/csping.wasm
#
# NOT byte-for-byte reproducible in the strict sense: `componentize-dotnet`
# downloads and caches the WASI SDK (dotnet/runtimelab NativeAOT-LLVM's own
# dependency, ~120MB, fetched from
# github.com/WebAssembly/wasi-sdk/releases) on first use inside the image,
# and its own version is chosen by the pinned
# `Microsoft.DotNet.ILCompiler.LLVM` package below, not independently
# pinned here -- see bundles/csharp/csping/README.md "Toolchain versions"
# for the exact versions this build resolved to.
# ROOT EXCEPTION (approved): this image is a one-shot local build tool,
# never deployed to any cluster and never running as a service -- it
# compiles bundles/csharp/csping and exits (CMD copies the artifact to a
# bind-mounted /out). Its risk profile is identical to running
# `cargo build`/`dotnet build` in a container: no exposed port, no
# long-lived process, no attacker-reachable surface. It stays root
# because the artifact lands in a host bind-mount (`-v <hostdir>:/out`)
# whose ownership/UID is unknown at image-build time -- a fixed non-root
# UID (the `Dockerfile.rust` pattern) can silently fail to write there
# depending on the host directory's permissions. FLAGGED FOR EXPLICIT
# USER SIGN-OFF (spec `rules/devops-containers.md` Rootless Containers
# exception process) before this Dockerfile is used beyond this spike.
FROM mcr.microsoft.com/dotnet/sdk:10.0.401-noble@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29

# `bookworm`/Debian has no published .NET 10 SDK image as of this build
# (checked against mcr.microsoft.com's own tag list: only `noble`
# (Ubuntu 24.04 LTS), `alpine`, `azurelinux`, and `trixie-slim`
# (Debian 13, preview-only) exist for the `10.0` SDK) -- per
# rules/devops-containers.md's own fallback order
# "Debian 12 -> 11 -> 13 -> Ubuntu LTS", Ubuntu Noble (an LTS release) is
# the correct next choice, not a rule violation.

WORKDIR /repo
COPY wit wit
COPY bundles/csharp/csping bundles/csharp/csping
WORKDIR /repo/bundles/csharp/csping

RUN dotnet build -c Release

CMD ["cp", "/repo/bundles/csharp/csping/bin/Release/net10.0/wasi-wasm/publish/csping.wasm", "/out/csping.wasm"]
