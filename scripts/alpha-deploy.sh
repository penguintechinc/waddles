#!/usr/bin/env bash
# alpha-deploy.sh -- Waddles local-alpha deploy at a pinned release SHA.
#
# Reusable version of the manual procedure run 3+ times by hand (build @ SHA,
# push with revision-label verification, helm upgrade, re-run seeder, verify
# rollout). Run ONLY from the dedicated worktree
# (/home/penguin/code/waddles/.worktrees/alpha-build) checked out to the exact
# release/v3.0.X SHA you want deployed -- never from the main checkout, which
# may carry stale/unrelated branches (see mem0 "waddles k8s-ops" notes).
#
# fix/alpha-deploy-context-allowlist -- KUBE_CONTEXT is validated against an
# allowlist (local-alpha, microk8s) before any build/push/helm step runs.
# values-alpha.yaml auto-generates secrets on upgrade; running this against a
# shared/non-alpha context (e.g. dal2-beta) would silently provision throwaway
# secrets there. See the validation block below.
#
# fix/alpha-deploy-hub-webui -- SERVICES previously omitted hub-webui, which
# values-alpha.yaml deploys, so that Deployment hit ImagePullBackOff on a fresh
# alpha build. A generic preflight (check_images_in_registry, below) renders the
# chart with the same values used by the live `helm upgrade` and HEAD-checks
# every localhost:32000/waddlebot/* image:tag it references against the registry
# BEFORE helm ever runs, so any future chart image SERVICES forgets to build
# fails loudly here instead of as an in-cluster ImagePullBackOff.
#
# fix/alpha-clean-deploy -- the plain-name Python svc-ingest-py/svc-process-py/
# svc-action-py images (built from core/svc_{ingest,process,action}/Dockerfile)
# were dropped from SERVICES: k8s/helm/waddlebot/values-alpha.yaml now sets
# pipeline.{svcIngest,svcProcess,svcAction}.enabled=false (those Deployments
# crash-looped -- "No module named 'waddle'" -- behind the live Rust data
# plane), so those Deployments no longer render in alpha and building/pushing
# those images is wasted work. waddlebot-egress-proxy was added: the chart's
# egress-proxy Deployment (templates/infrastructure/egress-proxy.yaml,
# egressProxy.image="waddlebot-egress-proxy") was never in this build list, so
# alpha always pulled a nonexistent tag for it until now.
#
# fix/helm-alpha-self-provisioning -- this script NEVER generates or rotates
# any secret material. `helm upgrade -f values-alpha.yaml` is self-sufficient
# on its own now: every platform Secret (seaweedfs-sse-kek, tenant-kek, bundle-signing,
# service-jwt, waddlebot-valkey-tls, and the DB/Redis/S3-root/JWT/module/
# service-api-key/credential-encryption/envelope-binding fields inside
# waddlebot-secrets) is provisioned by the chart itself via lookup(KEEP)-then-
# generate(alpha/local)-or-require(else) Helm template logic -- see
# k8s/helm/waddlebot/templates/auto-provisioned-secrets.yaml,
# auto-provisioned-keys-job.yaml, infrastructure/valkey-tls-secret.yaml, and
# secrets.yaml. An existing Secret is always left untouched. If a real
# --skip-secrets-style step is ever needed again, it belongs in the chart,
# not here.
#
# resolve-433 -- the dev box runs `docker system prune -a` HOURLY, wiping the
# local BuildKit cache, so every build here used to be a full cold build
# (~25min). Builds now go through a dedicated `docker-container` buildx
# builder (see ensure_cache_builder below) with --cache-from/--cache-to
# pointed at localhost:32000/waddlebot/buildcache/<image> -- the MicroK8s
# registry (a pod+PVC in namespace container-registry) lives outside
# docker's image/build-cache store, so it survives the hourly prune. The
# docker-container driver is required: the classic `docker` driver only
# supports `--cache-to type=inline` (baked into the final image, no separate
# cache manifest) and cannot push a standalone `type=registry` cache blob at
# all -- verified locally (`docker buildx build --cache-to type=registry...`
# on the default docker-driver builder errors with "docker exporter does not
# support cache export"). One cache tag per image, overwritten every build
# (mode=max, image-manifest=true, oci-mediatypes=true) -- see
# `make alpha-registry-gc` below for bounding registry disk growth (it was
# evicted once under DiskPressure).
#
# Usage:
#   scripts/alpha-deploy.sh [--skip-build]
#
# --skip-build   Reuse whatever is already pushed under the current HEAD's
#                sha8 tag (skips the build+push step).
#
# fix/valkey-cert-rollout-and-migrate-wait -- if you pipe this script's
# output (e.g. `scripts/alpha-deploy.sh | tee /tmp/alpha-deploy.log`), the
# exit code `$?` an outer caller/CI step reads back is `tee`'s, not this
# script's -- `set -euo pipefail` ABOVE only governs pipelines *inside* this
# script, it cannot reach back into an invoking shell's own pipeline. An
# agent run observed exactly this: a real failure here was masked by a
# green `tee` exit. Either invoke it as
# `bash -o pipefail -c 'scripts/alpha-deploy.sh | tee /tmp/alpha-deploy.log'`
# (or check `${PIPESTATUS[0]}` right after the pipe), or redirect instead of
# piping: `scripts/alpha-deploy.sh >/tmp/alpha-deploy.log 2>&1` preserves
# this script's own exit code with no extra flag.
#
# Requires: docker (with buildx), kubectl, helm, jq. Kube context must be
# local-alpha or microk8s (validated below; KUBE_CONTEXT set to anything
# else is rejected before any build/push/helm step). bash 3.2 compatible (no
# associative arrays, no `mapfile`, no `&>>`) -- macOS ships bash 3.2 as
# /bin/bash and this script must run there unmodified.
#
# fix/alpha-deploy-executor-and-failfast -- two independent bugs fixed:
#   1. bundle-executor (core/bundle_executor/Dockerfile.rust) was never in
#      SERVICES -- both bundle-executor Deployments (bundle-executor,
#      bundle-executor-action) ran a stale, hand-pushed mutable `:alpha` tag
#      forever. Now built+pushed every run, SHA8-tagged like every other
#      live image (values-alpha.yaml's bundleExecutor.image.tag is left
#      unset so it falls through to global.imageTag).
#   2. This script exited 0 on real failures: the Deployment rollout loop
#      only ever logged `err` and kept going, and nothing checked the live
#      `helm upgrade --install` actually left the release in `deployed`
#      status. Both now propagate a non-zero exit -- see the rollout loop
#      and the post-upgrade `helm status` check below. The deliberate
#      no-`--wait` design (see the HELM_ARGS comment below) is UNCHANGED --
#      this is explicit post-hoc verification, not `--wait`/`--atomic`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
readonly SCRIPT_DIR PROJECT_ROOT

NAMESPACE="${NAMESPACE:-waddlebot}"
RELEASE="${RELEASE:-waddlebot}"
HELM_CHART="${HELM_CHART:-k8s/helm/waddlebot}"
REGISTRY="${REGISTRY:-localhost:32000/waddlebot}"
CACHE_BUILDER="${CACHE_BUILDER:-alpha-registry-cache}"
readonly NAMESPACE RELEASE HELM_CHART REGISTRY CACHE_BUILDER

SKIP_BUILD=false
for arg in "$@"; do
    case "$arg" in
        --skip-build) SKIP_BUILD=true ;;
        *) echo "unknown arg: $arg" >&2; exit 1 ;;
    esac
done

info() { echo "[INFO] $*"; }
err()  { echo "[ERROR] $*" >&2; }

# ---------------------------------------------------------------------------
# KUBE_CONTEXT allowlist -- alpha-deploy.sh may only ever target local-alpha
# or microk8s. values-alpha.yaml auto-generates platform secrets on upgrade;
# pointing this script at a shared/non-alpha context (e.g. `KUBE_CONTEXT=
# dal2-beta make alpha-deploy`) would silently provision throwaway secrets
# there. This check runs before any build, push, or helm step.
# ---------------------------------------------------------------------------
is_allowed_kube_context() {
    case "$1" in
        local-alpha|microk8s) return 0 ;;
        *) return 1 ;;
    esac
}

if [[ -n "${KUBE_CONTEXT:-}" ]]; then
    if ! is_allowed_kube_context "${KUBE_CONTEXT}"; then
        err "KUBE_CONTEXT='${KUBE_CONTEXT}' is not allowed. alpha-deploy.sh may only target: local-alpha microk8s"
        exit 1
    fi
else
    KUBE_CONTEXT="$(kubectl config current-context)"
    if ! is_allowed_kube_context "${KUBE_CONTEXT}"; then
        err "current kubectl context '${KUBE_CONTEXT}' is not allowed. alpha-deploy.sh may only target: local-alpha microk8s. Set KUBE_CONTEXT explicitly to one of these."
        exit 1
    fi
fi
readonly KUBE_CONTEXT

# ---------------------------------------------------------------------------
# fix/chart-fresh-install-hooks (alpha 2026-10-01) -- create/adopt the
# namespace BEFORE the platform-credentials preflight below. The chart itself
# owns the Namespace object (templates/namespace.yaml); after a full wipe
# (cluster reset, `kubectl delete namespace waddlebot`) the namespace does not
# exist yet, so the credentials Secret could never be pre-created either --
# the preflight below would always fail with no way to satisfy it, since
# `kubectl create secret -n waddlebot` itself requires the namespace to
# already exist. Idempotent: a no-op if the namespace is already there.
#
# Adoption labels/annotations (app.kubernetes.io/managed-by=Helm,
# meta.helm.sh/release-name, meta.helm.sh/release-namespace) match exactly
# what Helm itself stamps on a resource it creates -- without them, the
# chart's own `helm install` would fail later with "already exists and
# cannot be imported" (a non-Helm-owned Namespace object), which is the
# standard Helm workaround for "let Helm manage a resource something else
# created first" (https://helm.sh/docs/howto/charts_tips_and_tricks/
# #tell-helm-not-to-uninstall-a-resource).
# ---------------------------------------------------------------------------
if ! kubectl --context "${KUBE_CONTEXT}" get namespace "${NAMESPACE}" >/dev/null 2>&1; then
    info "Namespace ${NAMESPACE} does not exist yet -- creating it (Helm-adopted) before the credentials preflight"
    kubectl --context "${KUBE_CONTEXT}" create namespace "${NAMESPACE}"
    kubectl --context "${KUBE_CONTEXT}" label namespace "${NAMESPACE}" \
        app.kubernetes.io/managed-by=Helm --overwrite
    kubectl --context "${KUBE_CONTEXT}" annotate namespace "${NAMESPACE}" \
        meta.helm.sh/release-name="${RELEASE}" \
        meta.helm.sh/release-namespace="${NAMESPACE}" --overwrite
fi

# ---------------------------------------------------------------------------
# fix/helm-platform-credentials preflight -- externally-issued platform
# credentials (Discord bot token, Twitch OAuth token, etc.) live ONLY in
# waddlebot-platform-credentials, a Secret this chart never renders or
# writes (see k8s/helm/waddlebot/docs/PLATFORM_CREDENTIALS.md). Existence
# check only -- never reads its data -- so a missing Secret fails loudly
# BEFORE any image is built, instead of pods silently starting with those
# platforms disabled after a full build+push+deploy cycle.
# ---------------------------------------------------------------------------
if ! kubectl --context "${KUBE_CONTEXT}" get secret waddlebot-platform-credentials \
        -n "${NAMESPACE}" >/dev/null 2>&1; then
    err "Secret 'waddlebot-platform-credentials' not found in namespace ${NAMESPACE}."
    err "Create it once (values read from files, never argv/history), e.g.:"
    err "  kubectl create secret generic waddlebot-platform-credentials \\"
    err "    --namespace ${NAMESPACE} \\"
    err "    --from-file=DISCORD_BOT_TOKEN=./discord-bot-token.txt \\"
    err "    --from-file=TWITCH_OAUTH_TOKEN=./twitch-oauth-token.txt"
    err "See k8s/helm/waddlebot/docs/PLATFORM_CREDENTIALS.md for the full key list."
    exit 1
fi
info "waddlebot-platform-credentials Secret present in namespace ${NAMESPACE}"

cd "${PROJECT_ROOT}"

SHA="$(git rev-parse HEAD)"
SHA8="${SHA:0:8}"
readonly SHA SHA8
info "Deploying release SHA ${SHA} (tag ${SHA8}) to context ${KUBE_CONTEXT}, namespace ${NAMESPACE}"

# ---------------------------------------------------------------------------
# Step 1: build + push, one image per line, verified by revision label.
#
# svc-ingest/svc-process/svc-action build from Dockerfile.rust into their own
# "*-rust" repositories (fix/helm-alpha-self-provisioning; see k8s/helm/
# waddlebot's rustDataPlane.svc{Ingest,Process,Action}.imageRepository
# values.yaml comments) -- these are the live path in alpha, the Python
# pipeline.svc{Ingest,Process,Action} Deployments are disabled there (see
# fix/alpha-clean-deploy header comment). core-bundle-seeder builds the ping/
# pyping WASM components + hub_api/cli seeder in one image (bundles/
# Dockerfile.core-bundles). waddlebot-egress-proxy builds core/egress_proxy/
# Dockerfile.rust from the "core" context (its Cargo.toml has same-repo path
# deps on sibling crates service_auth/bundle_host_http/egress_assertion --
# see that Dockerfile's own header).
#
# Bash 3.2 has no associative arrays (`declare -A`) -- SERVICES below is a
# plain list; per-service Dockerfile/context/image-name come from the
# service_dockerfile/service_context/service_image case functions.
#
# hub-webui is the plain-name Python image values-alpha.yaml deploys
# (fix/alpha-deploy-hub-webui) -- see the header comment.
# ---------------------------------------------------------------------------
# resolve-433 -- reputation-module, svc-presentation, and svc-streaming are
# chart-referenced (values-alpha.yaml's reputation/presentation/streaming
# sections) but were never in SERVICES, so the image preflight always found
# them missing. values-alpha.yaml pins each to a static "alpha" tag rather
# than the per-commit SHA8 (same pattern as bundleExecutor's own comment:
# "built locally + pushed to the local registry ... same as every other
# alpha-only module image") -- see service_image_tag below.
readonly SERVICES="hub-api hub-webui waddlebot-migrations svc-ingest svc-process svc-action waddlebot-egress-proxy core-bundle-seeder reputation-module svc-presentation svc-streaming bundle-executor"

service_dockerfile() {
    case "$1" in
        hub-api) echo "hub_api/Dockerfile" ;;
        hub-webui) echo "admin/hub_module/Dockerfile.webui" ;;
        waddlebot-migrations) echo "migrations/Dockerfile" ;;
        core-bundle-seeder) echo "bundles/Dockerfile.core-bundles" ;;
        svc-ingest) echo "core/svc_ingest/Dockerfile.rust" ;;
        svc-process) echo "core/svc_process/Dockerfile.rust" ;;
        svc-action) echo "core/svc_action/Dockerfile.rust" ;;
        waddlebot-egress-proxy) echo "core/egress_proxy/Dockerfile.rust" ;;
        reputation-module) echo "core/reputation_module/Dockerfile" ;;
        # chore/deploy-svc-presentation-rust-alpha -- Rust svc-presentation (values-alpha.yaml
        # presentation.rust.enabled); Dockerfile.rust builds from the "core" context.
        svc-presentation) echo "core/svc_presentation/Dockerfile.rust" ;;
        svc-streaming) echo "core/svc_streaming/Dockerfile.rust" ;;
        # fix/alpha-deploy-executor-and-failfast -- core/bundle_executor/Dockerfile.rust
        # now exists; this crate backs BOTH the bundle-executor and bundle-executor-action
        # Deployments (same image, dialed at two different stages via env only -- see
        # k8s/helm/waddlebot/templates/bundle-executor-action.yaml's own header comment).
        bundle-executor) echo "core/bundle_executor/Dockerfile.rust" ;;
        *) err "unknown service: $1"; exit 1 ;;
    esac
}

service_context() {
    case "$1" in
        # bundle-executor's Dockerfile COPYs core/bundle_executor AND wit/ from the repo
        # root (see its own header comment) -- same "." context as hub-api et al.
        hub-api|hub-webui|waddlebot-migrations|core-bundle-seeder|reputation-module|bundle-executor) echo "." ;;
        svc-ingest) echo "core/svc_ingest" ;;
        svc-process|svc-action|waddlebot-egress-proxy|svc-presentation) echo "core" ;;
        svc-streaming) echo "core/svc_streaming" ;;
        *) err "unknown service: $1"; exit 1 ;;
    esac
}

# Image repository name (differs from the service name for the 3 Rust
# stages -- see the comment above; waddlebot-egress-proxy pushes under its
# own name, matching egressProxy.image in values.yaml).
service_image_repo() {
    case "$1" in
        svc-ingest) echo "svc-ingest-rust" ;;
        svc-process) echo "svc-process-rust" ;;
        svc-action) echo "svc-action-rust" ;;
        svc-presentation) echo "svc-presentation-rust" ;;
        *) echo "$1" ;;
    esac
}

# Image tag: every service tags with this run's commit SHA8 except the
# alpha-only local modules values-alpha.yaml pins to a static "alpha" tag
# (reputation-module, svc-presentation, svc-streaming). fix/alpha-deploy-
# executor-and-failfast: bundle-executor is deliberately NOT in this list --
# it now gets the same per-commit SHA8 tag as every other live data-plane
# image (values-alpha.yaml's bundleExecutor.image.tag is unset), closing the
# stale-mutable-`:alpha`-tag bug this fix addresses.
service_image_tag() {
    case "$1" in
        reputation-module|svc-streaming) echo "alpha" ;;
        *) echo "${SHA8}" ;;
    esac
}

# fix/pii-tokenization-env-override -- svc-process/svc-action's Dockerfile.rust
# (`COPY --from=proto . /proto`) needs the repo-root proto/ tree passed as a
# named `proto` build context so `hub_client`'s build.rs (tonic-prost-build)
# can compile `proto/waddles/hub/internal/v1/*.proto` -- CI already does this
# (build-svc-process.yml/build-svc-action.yml's `build-contexts: proto=proto`);
# this local build loop was missing the equivalent `--build-context` flag,
# so a local alpha build failed with "proto: not found"/a missing COPY source.
service_build_context_args() {
    case "$1" in
        svc-process|svc-action|svc-presentation) echo "--build-context proto=proto" ;;
        *) echo "" ;;
    esac
}

# ---------------------------------------------------------------------------
# resolve-433 -- idempotent buildx builder for the registry-backed cache.
#
# Requires the `docker-container` driver: it's the only driver that can
# export `--cache-to type=registry` as a standalone cache manifest (the
# default `docker` driver only supports `type=inline`, and refuses
# `type=registry` outright -- "docker exporter does not support cache
# export"). `--driver-opt network=host` puts the buildkitd container on the
# host network namespace so it can actually reach localhost:32000 (the
# MicroK8s registry NodePort) -- without it the builder container has no
# route to the host's localhost. The registry has no TLS, so buildkitd needs
# an explicit insecure/http registry entry in its own config (buildx has no
# CLI flag for this -- it's buildkitd.toml only), written to a scratch temp
# file and passed via --buildkitd-config.
# ---------------------------------------------------------------------------
ensure_cache_builder() {
    if docker buildx inspect "${CACHE_BUILDER}" >/dev/null 2>&1; then
        info "buildx builder '${CACHE_BUILDER}' already exists"
    else
        info "Creating buildx builder '${CACHE_BUILDER}' (docker-container driver, insecure registry ${REGISTRY_HOST})"
        local buildkitd_config
        buildkitd_config="$(mktemp /tmp/alpha-cache-buildkitd-XXXXXX.toml)"
        cat > "${buildkitd_config}" <<EOF
[registry."${REGISTRY_HOST}"]
  http = true
  insecure = true
EOF
        if ! docker buildx create --name "${CACHE_BUILDER}" \
            --driver docker-container \
            --driver-opt network=host \
            --buildkitd-config "${buildkitd_config}" \
            --bootstrap; then
            rm -f "${buildkitd_config}"
            err "Failed to create buildx builder '${CACHE_BUILDER}'"
            exit 1
        fi
        rm -f "${buildkitd_config}"
    fi
}

# Host[:port] portion of REGISTRY (e.g. "localhost:32000") -- used both for
# the buildkitd insecure-registry config above and to build each image's
# buildcache ref below.
REGISTRY_HOST="${REGISTRY%%/*}"
readonly REGISTRY_HOST

if [[ "${SKIP_BUILD}" != "true" ]]; then
    ensure_cache_builder
    for svc in ${SERVICES}; do
        repo="$(service_image_repo "${svc}")"
        tag="$(service_image_tag "${svc}")"
        img="${REGISTRY}/${repo}:${tag}"
        dockerfile="$(service_dockerfile "${svc}")"
        context="$(service_context "${svc}")"
        extra_build_context_args="$(service_build_context_args "${svc}")"
        cache_ref="${REGISTRY}/buildcache/${repo}"
        info "Building ${img} (${dockerfile}), cache ${cache_ref}"
        # shellcheck disable=SC2086 # extra_build_context_args is either empty or a
        # fixed, space-free "--build-context proto=proto" literal -- intentional
        # word-splitting, never user/env-controlled content.
        docker buildx build --builder "${CACHE_BUILDER}" --pull=false \
            --cache-from "type=registry,ref=${cache_ref}" \
            --cache-to "type=registry,ref=${cache_ref},mode=max,image-manifest=true,oci-mediatypes=true" \
            --load \
            -f "${dockerfile}" \
            -t "${img}" \
            --label "org.opencontainers.image.revision=${SHA}" \
            ${extra_build_context_args} \
            "${context}"

        info "Pushing ${img}"
        docker push "${img}"

        # Verify the pushed image's revision label actually matches this SHA --
        # a stale/reused layer silently keeping an old revision label has
        # bitten this deploy before.
        got_rev="$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "${img}")"
        if [[ "${got_rev}" != "${SHA}" ]]; then
            err "${img} revision label mismatch: expected ${SHA}, got ${got_rev}"
            exit 1
        fi
    done
    info "All images built, pushed, and label-verified at ${SHA8}"
else
    info "Skipping build (--skip-build) -- assuming ${SHA8} images already pushed"
fi

# ---------------------------------------------------------------------------
# Step 1.5: preflight -- every chart-referenced local image:tag must already
# be in the registry BEFORE helm ever runs (fix/alpha-deploy-hub-webui).
#
# Renders the chart with the exact same values-alpha.yaml + global.imageTag
# used by the real `helm upgrade` below, extracts every localhost:32000/
# waddlebot/* image:tag it references, and does a registry v2 HEAD manifest
# request for each. This catches a chart image SERVICES forgot to build
# (like hub-webui was) as a clear preflight failure instead of an in-cluster
# ImagePullBackOff discovered only after `helm upgrade` already applied.
#
# Runs unconditionally (even with --skip-build) -- the point is to verify
# what's actually in the registry right now, not just what this run built.
# ---------------------------------------------------------------------------
image_exists_in_registry() {
    # image_exists_in_registry <host[:port]>/<repo>:<tag> -- registry v2 HEAD
    # manifest request. Split on the FIRST "/" for host, LAST ":" for tag, so
    # a repo with no nested path (e.g. "waddlebot/hub-webui") still parses.
    local image="$1"
    local host repo_and_tag tag repo code
    host="${image%%/*}"
    repo_and_tag="${image#*/}"
    tag="${repo_and_tag##*:}"
    repo="${repo_and_tag%:*}"
    # buildx pushes OCI image indexes (or manifest lists) rather than plain
    # docker v2 manifests, so a single-media-type Accept header causes the
    # registry to 404 an image that is actually present. Send one request
    # advertising every valid manifest media type -- a 200 means present.
    code="$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json" \
        "http://${host}/v2/${repo}/manifests/${tag}" 2>/dev/null || echo "000")"
    [[ "${code}" == "200" ]]
}

check_images_in_registry() {
    info "Preflight: rendering chart to collect every ${REGISTRY}/* image:tag"

    local rendered
    if ! rendered="$(helm template "${RELEASE}" "${HELM_CHART}" \
        --kube-version 1.30.0 \
        --values "${HELM_CHART}/values-alpha.yaml" \
        --set-string "global.imageTag=${SHA8}" 2>&1)"; then
        err "helm template failed while collecting the preflight image list:"
        echo "${rendered}" >&2
        exit 1
    fi

    local images
    images="$(printf '%s\n' "${rendered}" \
        | grep -E '^[[:space:]]*image:[[:space:]]*"?'"${REGISTRY}"'/' \
        | sed -E 's/^[[:space:]]*image:[[:space:]]*"?([^"[:space:]]+)"?.*/\1/' \
        | sort -u)"

    if [[ -z "${images}" ]]; then
        err "Preflight found zero ${REGISTRY}/* images in the rendered chart -- registry prefix mismatch or empty render. Treating as failure."
        exit 1
    fi

    local checked=0
    local missing=""
    local image
    while IFS= read -r image; do
        [[ -z "${image}" ]] && continue
        checked=$((checked + 1))
        if ! image_exists_in_registry "${image}"; then
            missing="${missing}${image}
"
        fi
    done <<EOF
${images}
EOF

    info "Preflight checked ${checked} chart-referenced image(s) against ${REGISTRY}"

    if [[ "${checked}" -eq 0 ]]; then
        err "Preflight examined zero images -- treating as failure"
        exit 1
    fi

    if [[ -n "${missing}" ]]; then
        err "The following chart-referenced image(s) are missing from ${REGISTRY}:"
        printf '%s' "${missing}" | while IFS= read -r m; do
            [[ -n "${m}" ]] && err "  - ${m}"
        done
        err "Add the missing service(s) to alpha-deploy.sh's SERVICES/service_dockerfile/service_context/service_image_repo above, then re-run."
        exit 1
    fi

    info "All ${checked} chart-referenced image(s) present in ${REGISTRY}"
}

check_images_in_registry

# ---------------------------------------------------------------------------
# Step 2: helm lint + dry-run + upgrade.
#
# Only the image tag is set here -- no secret/TLS material of any kind.
# k8s/helm/waddlebot/values-alpha.yaml already sets global.deploymentTier:
# "alpha" (drives every auto-provisioned Secret's generate-vs-require branch)
# and spire.enabled: false (the spire-auto-enroll post-upgrade hook Job
# crash-loops on this cluster -- SPIRE server/agent not viable here), so
# neither needs to be repeated via --set.
#
# DELIBERATELY NO --wait/--atomic on HELM_ARGS below (USER DECISION,
# 2026-10-01, fix/chart-fresh-install-hooks) -- this is load-bearing, not an
# oversight; do not add either flag here. db-migrate (templates/
# migrations-job.yaml) now runs post-install, and every one of the 40 other
# workloads in this chart (including hub-api itself, via its own `/ready`
# readinessProbe) only becomes Ready AFTER that hook has run. With --wait (or
# --atomic, which implies it), Helm blocks on every just-created Deployment
# reaching Ready BEFORE it runs any post-install hook -- so a --wait install
# would deadlock: Deployments waiting on hub-api, hub-api waiting on
# db-migrate, db-migrate waiting on Helm to finish waiting on Deployments.
# Without --wait, Helm creates the Deployment objects (pods scheduled, not
# required to be Ready) and immediately proceeds to run the post-install hook
# phase -- Step 3 below does the equivalent waiting explicitly and in the
# correct order instead (db-migrate Job first, Deployment rollouts after),
# which is race-free by construction.
# ---------------------------------------------------------------------------
# fix/seeder-throughput -- core-bundle-seeder-job.yaml is a post-install/
# post-upgrade Helm hook (helm.sh/hook); Helm always waits on hook completion
# using the --timeout flag below, a separate mechanism from the omitted flags
# discussed above (those govern waiting on normal resource readiness, not
# hooks). The previous default (5m) was shorter than
# pipeline.coreBundleSeeder.activeDeadlineSeconds (3000s/50min), so `helm
# upgrade` gave up and errored out while the Job was still healthy and
# seeding. HELM_ARGS now sets an explicit 60m to give headroom above the
# Job's own deadline.
# ---------------------------------------------------------------------------
helm lint "${HELM_CHART}" -f "${HELM_CHART}/values-alpha.yaml"

HELM_ARGS=(
    upgrade --install "${RELEASE}" "${HELM_CHART}"
    --kube-context "${KUBE_CONTEXT}"
    --namespace "${NAMESPACE}" --create-namespace
    --values "${HELM_CHART}/values-alpha.yaml"
    --set-string "global.imageTag=${SHA8}"
    --timeout 60m
)

info "helm upgrade --dry-run validation"
helm "${HELM_ARGS[@]}" --dry-run=server >/dev/null

info "helm upgrade --install (live)"
helm "${HELM_ARGS[@]}"

# ---------------------------------------------------------------------------
# fix/alpha-deploy-executor-and-failfast -- `helm upgrade --install` above can
# return 0 while still leaving the release in a non-"deployed" status (e.g. a
# post-upgrade hook failure pins it FAILED -- see mem0 "spire-auto-enroll"
# incident). Fail loudly here instead of silently continuing into Step 3 as
# if the upgrade had actually succeeded. This is explicit post-hoc
# verification, NOT `--wait`/`--atomic` on HELM_ARGS above (still forbidden,
# still enforced by scripts/check_alpha_deploy_no_wait.py) -- it runs AFTER
# the hook phase has already completed, so it cannot deadlock the fresh-
# install hook ordering that --wait would.
# ---------------------------------------------------------------------------
info "Verifying helm release status"
HELM_STATUS_JSON="$(helm status "${RELEASE}" --kube-context "${KUBE_CONTEXT}" -n "${NAMESPACE}" -o json)"
HELM_RELEASE_STATUS="$(printf '%s' "${HELM_STATUS_JSON}" | jq -r '.info.status')"
HELM_RELEASE_REVISION="$(printf '%s' "${HELM_STATUS_JSON}" | jq -r '.version')"
if [[ "${HELM_RELEASE_STATUS}" != "deployed" ]]; then
    err "helm release ${RELEASE} is not 'deployed' after upgrade (status=${HELM_RELEASE_STATUS}, revision=${HELM_RELEASE_REVISION})"
    exit 1
fi
info "helm release ${RELEASE} is 'deployed' at revision ${HELM_RELEASE_REVISION}"

# ---------------------------------------------------------------------------
# Step 3: every Deployment. (No separate db-migrate Job wait -- see below.)
#
# fix/valkey-cert-rollout-and-migrate-wait -- previously waited here with
# `kubectl wait --for=condition=complete job/${RELEASE}-db-migrate`, which
# intermittently failed with `jobs.batch "waddlebot-db-migrate" not found`
# even when migrations succeeded. Root cause: migrations-job.yaml's
# `helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded` deletes
# the Job object as part of the SAME synchronous hook phase that
# `helm upgrade --install` above already waited on -- by the time this
# script resumes, a successful Job is already gone, so `kubectl wait` races
# an object Helm itself just deleted.
#
# This is consistent with the #526 (fix/chart-fresh-install-hooks) design:
# Helm hooks are a synchronous barrier -- `helm upgrade --install` does not
# return until db-migrate (helm.sh/hook: post-install,pre-upgrade) has
# finished, and a hook failure leaves the release in a non-"deployed" status
# (or makes the `helm upgrade` command itself exit non-zero). The
# "Verifying helm release status" check immediately above this comment
# already fails loudly in that case, BEFORE this script ever reaches Step 3
# -- so it is already the authoritative, race-free confirmation that
# db-migrate succeeded; a second, separate `kubectl wait` on the (likely
# already-deleted) Job object added no additional guarantee, only a flaky
# false-negative. Deliberately not retaining the Job past hook-succeeded
# (e.g. dropping `hook-succeeded` from the delete-policy) to make the old
# wait work again -- that would be a chart change, out of scope for this
# script-only fix, and would leave a stale db-migrate Job object sitting in
# the namespace between upgrades for no behavioral benefit.
info "Waiting for Deployments"
# fix/alpha-deploy-executor-and-failfast -- previously `|| err ...` only logged and kept
# going, so a Deployment that never rolled out cleanly still left the script exiting 0.
# Every failure is now collected and the script exits non-zero after checking the rest
# (so one bad rollout doesn't hide a second one), listing every failed Deployment.
ROLLOUT_FAILURES=""
for d in $(kubectl --context "${KUBE_CONTEXT}" get deployments -n "${NAMESPACE}" -o jsonpath='{.items[*].metadata.name}'); do
    if ! kubectl --context "${KUBE_CONTEXT}" rollout status "deployment/${d}" -n "${NAMESPACE}" --timeout=180s; then
        err "deployment/${d} did not roll out cleanly"
        ROLLOUT_FAILURES="${ROLLOUT_FAILURES}${d}
"
    fi
done

if [[ -n "${ROLLOUT_FAILURES}" ]]; then
    err "The following Deployment(s) did not roll out cleanly:"
    printf '%s' "${ROLLOUT_FAILURES}" | while IFS= read -r f; do
        [[ -n "${f}" ]] && err "  - ${f}"
    done
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 4: re-run the core-bundle-seeder Job (helm's post-upgrade hook already
# ran it once above; this re-triggers it explicitly and blocks on the result
# -- useful for --skip-build re-runs after fixing a seeder-only issue).
# ---------------------------------------------------------------------------
SEEDER_JOB="${RELEASE}-core-bundle-seeder"
info "Re-running ${SEEDER_JOB}"
kubectl --context "${KUBE_CONTEXT}" delete job "${SEEDER_JOB}" -n "${NAMESPACE}" --ignore-not-found
helm "${HELM_ARGS[@]}" >/dev/null
kubectl --context "${KUBE_CONTEXT}" wait --for=condition=complete \
    "job/${SEEDER_JOB}" -n "${NAMESPACE}" --timeout=120s || {
        err "core-bundle-seeder Job failed"
        kubectl --context "${KUBE_CONTEXT}" logs -n "${NAMESPACE}" "job/${SEEDER_JOB}" --tail=80
        exit 1
    }

# ---------------------------------------------------------------------------
# Step 5: verification.
# ---------------------------------------------------------------------------
info "Pod status:"
kubectl --context "${KUBE_CONTEXT}" get pods -n "${NAMESPACE}"

info "svc-process-rust / svc-action-rust image digests:"
kubectl --context "${KUBE_CONTEXT}" get deploy -n "${NAMESPACE}" \
    "${RELEASE}-svc-process-rust" "${RELEASE}-svc-action-rust" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}' 2>&1 || true

info "svc-ingest-rust (Discord gateway) READY line (no token values):"
kubectl --context "${KUBE_CONTEXT}" logs -n "${NAMESPACE}" \
    -l app.kubernetes.io/instance="${RELEASE}",app.kubernetes.io/component=svc-ingest-rust --tail=200 2>&1 \
    | grep -iE "ready|identify|connected" | grep -viE "token" | tail -5 || echo "no ready/connected line found"

info "Done."
