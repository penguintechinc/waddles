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
# fix/alpha-deploy-hub-webui -- SERVICES previously omitted hub-webui and the
# plain-name Python svc-ingest/svc-process/svc-action images that values-alpha.yaml
# deploys alongside their *-rust counterparts (coexistence, not cutover -- see
# k8s/helm/waddlebot/values-alpha.yaml pipeline.rustDataPlane comment), so those
# Deployments hit ImagePullBackOff on a fresh alpha build. All four are now built
# and pushed. A generic preflight (check_images_in_registry, below) renders the
# chart with the same values used by the live `helm upgrade` and HEAD-checks
# every localhost:32000/waddlebot/* image:tag it references against the registry
# BEFORE helm ever runs, so any future chart image SERVICES forgets to build
# fails loudly here instead of as an in-cluster ImagePullBackOff.
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
# Requires: docker (with buildx), kubectl, helm. Kube context must be
# local-alpha or microk8s (validated below; KUBE_CONTEXT set to anything
# else is rejected before any build/push/helm step). bash 3.2 compatible (no
# associative arrays, no `mapfile`, no `&>>`) -- macOS ships bash 3.2 as
# /bin/bash and this script must run there unmodified.

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
# "*-rust" repositories -- distinct from the Python images built under the
# plain service name -- so the two coexisting Deployments (Python stays
# enabled alongside the Rust data plane, pipeline.rustDataPlane.enabled=true
# in values-alpha.yaml) never race on a shared repository:tag
# (fix/helm-alpha-self-provisioning; see k8s/helm/waddlebot's
# rustDataPlane.svc{Ingest,Process,Action}.imageRepository values.yaml
# comments). core-bundle-seeder builds the ping/pyping WASM components +
# hub_api/cli seeder in one image (bundles/Dockerfile.core-bundles).
#
# Bash 3.2 has no associative arrays (`declare -A`) -- SERVICES below is a
# plain list; per-service Dockerfile/context/image-name come from the
# service_dockerfile/service_context/service_image case functions.
#
# hub-webui and the svc-*-py entries are the plain-name Python images
# values-alpha.yaml deploys alongside svc-ingest/svc-process/svc-action's
# *-rust counterparts (fix/alpha-deploy-hub-webui) -- see the header comment.
# ---------------------------------------------------------------------------
# resolve-433 -- reputation-module, svc-presentation, and svc-streaming are
# chart-referenced (values-alpha.yaml's reputation/presentation/streaming
# sections) but were never in SERVICES, so the image preflight always found
# them missing. values-alpha.yaml pins each to a static "alpha" tag rather
# than the per-commit SHA8 (same pattern as bundleExecutor's own comment:
# "built locally + pushed to the local registry ... same as every other
# alpha-only module image") -- see service_image_tag below.
readonly SERVICES="hub-api hub-webui waddlebot-migrations svc-ingest svc-process svc-action svc-ingest-py svc-process-py svc-action-py core-bundle-seeder reputation-module svc-presentation svc-streaming"

service_dockerfile() {
    case "$1" in
        hub-api) echo "hub_api/Dockerfile" ;;
        hub-webui) echo "admin/hub_module/Dockerfile.webui" ;;
        waddlebot-migrations) echo "migrations/Dockerfile" ;;
        core-bundle-seeder) echo "bundles/Dockerfile.core-bundles" ;;
        svc-ingest) echo "core/svc_ingest/Dockerfile.rust" ;;
        svc-process) echo "core/svc_process/Dockerfile.rust" ;;
        svc-action) echo "core/svc_action/Dockerfile.rust" ;;
        svc-ingest-py) echo "core/svc_ingest/Dockerfile" ;;
        svc-process-py) echo "core/svc_process/Dockerfile" ;;
        svc-action-py) echo "core/svc_action/Dockerfile" ;;
        reputation-module) echo "core/reputation_module/Dockerfile" ;;
        svc-presentation) echo "core/svc_presentation/Dockerfile" ;;
        svc-streaming) echo "core/svc_streaming/Dockerfile.rust" ;;
        *) err "unknown service: $1"; exit 1 ;;
    esac
}

service_context() {
    case "$1" in
        hub-api|hub-webui|waddlebot-migrations|core-bundle-seeder|svc-ingest-py|svc-process-py|svc-action-py|reputation-module|svc-presentation) echo "." ;;
        svc-ingest) echo "core/svc_ingest" ;;
        svc-process|svc-action) echo "core" ;;
        svc-streaming) echo "core/svc_streaming" ;;
        *) err "unknown service: $1"; exit 1 ;;
    esac
}

# Image repository name (differs from the service name for the 3 Rust
# stages, and for the svc-*-py entries which push under the chart's plain
# service-name repo -- see the comment above).
service_image_repo() {
    case "$1" in
        svc-ingest) echo "svc-ingest-rust" ;;
        svc-process) echo "svc-process-rust" ;;
        svc-action) echo "svc-action-rust" ;;
        svc-ingest-py) echo "svc-ingest" ;;
        svc-process-py) echo "svc-process" ;;
        svc-action-py) echo "svc-action" ;;
        *) echo "$1" ;;
    esac
}

# Image tag: every service tags with this run's commit SHA8 except the
# alpha-only local modules values-alpha.yaml pins to a static "alpha" tag
# (reputation-module, svc-presentation, svc-streaming, and bundleExecutor --
# bundle-executor has no build step here yet, see scripts/deploy-alpha.sh).
service_image_tag() {
    case "$1" in
        reputation-module|svc-presentation|svc-streaming) echo "alpha" ;;
        *) echo "${SHA8}" ;;
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
        cache_ref="${REGISTRY}/buildcache/${repo}"
        info "Building ${img} (${dockerfile}), cache ${cache_ref}"
        docker buildx build --builder "${CACHE_BUILDER}" --pull=false \
            --cache-from "type=registry,ref=${cache_ref}" \
            --cache-to "type=registry,ref=${cache_ref},mode=max,image-manifest=true,oci-mediatypes=true" \
            --load \
            -f "${dockerfile}" \
            -t "${img}" \
            --label "org.opencontainers.image.revision=${SHA}" \
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
        --set "global.imageTag=${SHA8}" 2>&1)"; then
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
# ---------------------------------------------------------------------------
helm lint "${HELM_CHART}" -f "${HELM_CHART}/values-alpha.yaml"

HELM_ARGS=(
    upgrade --install "${RELEASE}" "${HELM_CHART}"
    --kube-context "${KUBE_CONTEXT}"
    --namespace "${NAMESPACE}" --create-namespace
    --values "${HELM_CHART}/values-alpha.yaml"
    --set "global.imageTag=${SHA8}"
)

info "helm upgrade --dry-run validation"
helm "${HELM_ARGS[@]}" --dry-run=server >/dev/null

info "helm upgrade --install (live)"
helm "${HELM_ARGS[@]}"

# ---------------------------------------------------------------------------
# Step 3: wait for the pre-upgrade migrations Job, then every Deployment.
# ---------------------------------------------------------------------------
info "Waiting for db-migrate Job"
kubectl --context "${KUBE_CONTEXT}" wait --for=condition=complete \
    "job/${RELEASE}-db-migrate" -n "${NAMESPACE}" --timeout=180s || {
        err "db-migrate Job did not complete"
        kubectl --context "${KUBE_CONTEXT}" logs -n "${NAMESPACE}" "job/${RELEASE}-db-migrate" --tail=50
        exit 1
    }

info "Waiting for Deployments"
for d in $(kubectl --context "${KUBE_CONTEXT}" get deployments -n "${NAMESPACE}" -o jsonpath='{.items[*].metadata.name}'); do
    kubectl --context "${KUBE_CONTEXT}" rollout status "deployment/${d}" -n "${NAMESPACE}" --timeout=180s \
        || err "deployment/${d} did not roll out cleanly"
done

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
