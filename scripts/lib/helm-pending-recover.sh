#!/usr/bin/env bash
# helm-pending-recover.sh -- sourced by scripts/alpha-deploy.sh (not executed).
#
# Recovery contract for a wedged Helm release. An interrupted `helm upgrade`
# leaves the newest release revision in pending-upgrade / pending-install /
# pending-rollback, and Helm then refuses ALL further upgrades ("another
# operation is in progress"). helm_recover_pending_release detects exactly
# that and nothing else:
#
#   1. `helm status` the release. Not found => first install, nothing to do.
#      deployed / failed / anything non-pending => untouched, return 0.
#   2. Status is pending-* => log loudly, then recover:
#        a. a DEPLOYED revision exists in `helm history` => helm rollback to
#           the newest such revision;
#        b. none exists (all failed/pending) => delete the highest-numbered
#           pending release secret sh.helm.release.v1.<release>.v<N>
#           (found via `kubectl get secret -l owner=helm,name=<release>`).
#   3. Re-check status. Still pending-* (or unreadable) => return 1, so the
#      caller never proceeds into a doomed upgrade. Zero secrets found when
#      one is required is a failure, never a silent pass.
#
# Every helm/kubectl call is pinned to the validated --kube-context passed in
# by the caller. Requires jq. bash 3.2 compatible (no mapfile/assoc arrays).
# Expects info() and err() to be defined by the caller.

# Prints the release's .info.status on stdout and returns 0; returns 2 if the
# release does not exist, 1 on any other failure.
_helm_release_status() {
    local release="$1" ns="$2" ctx="$3" out rc=0 status
    out="$(helm status "${release}" --kube-context "${ctx}" -n "${ns}" -o json 2>&1)" || rc=$?
    if [[ ${rc} -ne 0 ]]; then
        if [[ "${out}" == *"not found"* ]]; then
            return 2
        fi
        err "helm status ${release} failed: ${out}"
        return 1
    fi
    status="$(printf '%s' "${out}" | jq -r '.info.status // empty')" || return 1
    if [[ -z "${status}" ]]; then
        err "helm status ${release} returned no .info.status"
        return 1
    fi
    printf '%s\n' "${status}"
}

# Returns 0 if the given status string is a Helm pending-* state.
_helm_status_is_pending() {
    case "$1" in
        pending-install|pending-upgrade|pending-rollback) return 0 ;;
        *) return 1 ;;
    esac
}

# helm_recover_pending_release <release> <namespace> <kube-context>
helm_recover_pending_release() {
    local release="$1" ns="$2" ctx="$3" status rc=0

    status="$(_helm_release_status "${release}" "${ns}" "${ctx}")" || rc=$?
    if [[ ${rc} -eq 2 ]]; then
        info "helm release ${release} not found in ${ns} -- fresh install, no pending-state recovery needed"
        return 0
    elif [[ ${rc} -ne 0 ]]; then
        err "cannot determine status of helm release ${release}; refusing to continue"
        return 1
    fi

    if ! _helm_status_is_pending "${status}"; then
        info "helm release ${release} status=${status} -- no stuck pending release"
        return 0
    fi

    err "################################################################"
    err "helm release ${release} is STUCK in '${status}' (interrupted upgrade) -- auto-recovering"
    err "################################################################"

    local deployed_rev="" history_json
    if ! history_json="$(helm history "${release}" --kube-context "${ctx}" -n "${ns}" -o json)"; then
        err "helm history ${release} failed; cannot choose a recovery path"
        return 1
    fi
    deployed_rev="$(printf '%s' "${history_json}" \
        | jq -r '[.[] | select(.status == "deployed") | .revision] | max // empty')" || return 1

    if [[ -n "${deployed_rev}" ]]; then
        info "recovery: rolling back ${release} to last DEPLOYED revision ${deployed_rev}"
        if ! helm rollback "${release}" "${deployed_rev}" --kube-context "${ctx}" -n "${ns}"; then
            err "helm rollback ${release} ${deployed_rev} failed"
        fi
    else
        info "recovery: no DEPLOYED revision for ${release} -- deleting the stuck pending release secret"
        local secrets_json secret_name
        if ! secrets_json="$(kubectl --context "${ctx}" -n "${ns}" get secret \
                -l "owner=helm,name=${release}" -o json)"; then
            err "kubectl get secret (owner=helm,name=${release}) failed"
            return 1
        fi
        # Highest revision among secrets whose own status label is pending-*.
        secret_name="$(printf '%s' "${secrets_json}" | jq -r '
            [.items[]
             | select(.metadata.labels.status
                      | IN("pending-install","pending-upgrade","pending-rollback"))
             | {n: .metadata.name, v: (.metadata.labels.version | tonumber)}]
            | sort_by(.v) | last | .n // empty')" || return 1
        if [[ -z "${secret_name}" ]]; then
            err "no pending helm release secret found for ${release} in ${ns} (expected one) -- cannot recover"
            return 1
        fi
        info "deleting stuck pending release secret ${secret_name}"
        if ! kubectl --context "${ctx}" -n "${ns}" delete secret "${secret_name}"; then
            err "failed to delete ${secret_name}"
            return 1
        fi
    fi

    rc=0
    status="$(_helm_release_status "${release}" "${ns}" "${ctx}")" || rc=$?
    if [[ ${rc} -eq 2 ]]; then
        # Only possible if the lone (pending-install) revision secret was deleted.
        info "helm release ${release} no longer exists after recovery -- upgrade --install will recreate it"
        return 0
    elif [[ ${rc} -ne 0 ]]; then
        err "cannot re-check helm release ${release} status after recovery"
        return 1
    fi
    if _helm_status_is_pending "${status}"; then
        err "helm release ${release} is STILL '${status}' after recovery attempt -- refusing to run a doomed upgrade. Manual intervention required."
        return 1
    fi
    info "recovery complete: helm release ${release} now status=${status}"
}
