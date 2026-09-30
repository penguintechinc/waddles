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
# Usage:
#   scripts/alpha-deploy.sh [--skip-build]
#
# --skip-build   Reuse whatever is already pushed under the current HEAD's
#                sha8 tag (skips the build+push step).
#
# Requires: docker, kubectl, helm. Kube context must be local-alpha or
# microk8s (validated below; KUBE_CONTEXT set to anything else is rejected
# before any build/push/helm step). bash 3.2 compatible (no associative
# arrays, no `mapfile`, no `&>>`) -- macOS ships bash 3.2 as /bin/bash and
# this script must run there unmodified.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
readonly SCRIPT_DIR PROJECT_ROOT

NAMESPACE="${NAMESPACE:-waddlebot}"
RELEASE="${RELEASE:-waddlebot}"
HELM_CHART="${HELM_CHART:-k8s/helm/waddlebot}"
REGISTRY="${REGISTRY:-localhost:32000/waddlebot}"
readonly NAMESPACE RELEASE HELM_CHART REGISTRY

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
# ---------------------------------------------------------------------------
readonly SERVICES="hub-api waddlebot-migrations svc-ingest svc-process svc-action core-bundle-seeder"

service_dockerfile() {
    case "$1" in
        hub-api) echo "hub_api/Dockerfile" ;;
        waddlebot-migrations) echo "migrations/Dockerfile" ;;
        core-bundle-seeder) echo "bundles/Dockerfile.core-bundles" ;;
        svc-ingest) echo "core/svc_ingest/Dockerfile.rust" ;;
        svc-process) echo "core/svc_process/Dockerfile.rust" ;;
        svc-action) echo "core/svc_action/Dockerfile.rust" ;;
        *) err "unknown service: $1"; exit 1 ;;
    esac
}

service_context() {
    case "$1" in
        hub-api|waddlebot-migrations|core-bundle-seeder) echo "." ;;
        svc-ingest) echo "core/svc_ingest" ;;
        svc-process|svc-action) echo "core" ;;
        *) err "unknown service: $1"; exit 1 ;;
    esac
}

# Image repository name (differs from the service name for the 3 Rust
# stages -- see the comment above).
service_image_repo() {
    case "$1" in
        svc-ingest) echo "svc-ingest-rust" ;;
        svc-process) echo "svc-process-rust" ;;
        svc-action) echo "svc-action-rust" ;;
        *) echo "$1" ;;
    esac
}

if [[ "${SKIP_BUILD}" != "true" ]]; then
    for svc in ${SERVICES}; do
        repo="$(service_image_repo "${svc}")"
        img="${REGISTRY}/${repo}:${SHA8}"
        dockerfile="$(service_dockerfile "${svc}")"
        context="$(service_context "${svc}")"
        info "Building ${img} (${dockerfile})"
        docker build --pull=false \
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
# fix/helm-platform-credentials preflight -- externally-issued platform
# credentials (Discord bot token, Twitch OAuth token, etc.) live ONLY in
# waddlebot-platform-credentials, a Secret this chart never renders or
# writes (see k8s/helm/waddlebot/docs/PLATFORM_CREDENTIALS.md). Existence
# check only -- never reads its data -- so a missing Secret fails loudly
# BEFORE `helm upgrade`, instead of pods silently starting with those
# platforms disabled.
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
