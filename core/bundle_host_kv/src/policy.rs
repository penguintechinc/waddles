//! Startup check: bundle `kv` requires a Valkey `maxmemory-policy` that
//! never evicts the per-app `count_key` under memory pressure (low-severity
//! fix, security review of PR #425). `KvScope::count_key` carries no TTL
//! (`crate::KvHost` never sets one on it), so it is a **non-volatile**
//! key -- safe under `noeviction` or any `volatile-*` policy (those only
//! ever evict keys that *do* have a TTL), but an `allkeys-*` policy evicts
//! non-volatile keys too. Losing `count_key` while live
//! `bundlekv:...:data:*` keys survive silently resets the app's key-count
//! quota to zero, letting it exceed [`crate::MAX_KEYS_PER_APP`] one
//! eviction at a time -- [`crate::backend::reconcile`] is the
//! self-healing backstop for when this happens anyway; this check is the
//! preventive half, surfaced loudly (`ERROR` + a metric) so an operator
//! fixes the Valkey config rather than relying on the backstop.

/// The Valkey `maxmemory-policy` family that is genuinely unsafe for this
/// crate's non-volatile `count_key`.
fn is_unsafe_policy(policy: &str) -> bool {
    policy.starts_with("allkeys-")
}

/// Outcome of [`check_maxmemory_policy`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PolicyCheck {
    /// `noeviction` or a `volatile-*` policy -- `count_key` is safe.
    Compliant(String),
    /// An `allkeys-*` policy -- `count_key` can be evicted.
    Violation(String),
    /// `CONFIG GET` failed or returned an unexpected shape (e.g. a managed
    /// Valkey offering that restricts `CONFIG GET`) -- logged at `WARN`,
    /// never escalated to `ERROR`: an inability to *check* the policy is
    /// not evidence the policy is actually wrong.
    CheckFailed(String),
}

/// Runs `CONFIG GET maxmemory-policy` against `conn` and classifies the
/// result. Call once per service at startup, right after opening the `kv`
/// Valkey connection (`core/svc_action`/`core/svc_process`'s
/// `connect_kv`/`build_stage_capabilities` call site) -- never on the
/// per-op hot path.
pub async fn check_maxmemory_policy(conn: &mut redis::aio::MultiplexedConnection) -> PolicyCheck {
    let result: Result<Vec<String>, redis::RedisError> = redis::cmd("CONFIG")
        .arg("GET")
        .arg("maxmemory-policy")
        .query_async(conn)
        .await;
    match result {
        // `CONFIG GET <param>` replies `[param, value]`.
        Ok(kv) if kv.len() == 2 => {
            let policy = kv[1].clone();
            if is_unsafe_policy(&policy) {
                PolicyCheck::Violation(policy)
            } else {
                PolicyCheck::Compliant(policy)
            }
        }
        Ok(other) => PolicyCheck::CheckFailed(format!(
            "CONFIG GET maxmemory-policy returned an unexpected shape: {other:?}"
        )),
        Err(err) => PolicyCheck::CheckFailed(err.to_string()),
    }
}

/// Logs `check` at the appropriate level and records
/// [`crate::metrics::record_maxmemory_policy_violation`] on a violation.
/// Split from [`check_maxmemory_policy`] so the check itself stays
/// pure-ish (one Valkey round trip, no logging side effects) and unit
/// -testable via its return value alone.
pub fn log_and_record(check: &PolicyCheck) {
    match check {
        PolicyCheck::Compliant(policy) => {
            tracing::info!(
                maxmemory_policy = %policy,
                "kv capability: Valkey maxmemory-policy is compliant (noeviction or volatile-*)"
            );
        }
        PolicyCheck::Violation(policy) => {
            tracing::error!(
                maxmemory_policy = %policy,
                "kv capability: Valkey maxmemory-policy is an allkeys-* eviction policy -- the \
                 per-app storage.kv key-count quota counter can be evicted under memory \
                 pressure, silently resetting the quota; set maxmemory-policy to noeviction (or \
                 a volatile-* policy) on this Valkey instance"
            );
            crate::metrics::record_maxmemory_policy_violation(policy);
        }
        PolicyCheck::CheckFailed(err) => {
            tracing::warn!(
                error = %err,
                "kv capability: could not verify Valkey maxmemory-policy at startup"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn noeviction_is_not_unsafe() {
        assert!(!is_unsafe_policy("noeviction"));
    }

    #[test]
    fn every_volatile_policy_is_not_unsafe() {
        for policy in [
            "volatile-lru",
            "volatile-lfu",
            "volatile-random",
            "volatile-ttl",
        ] {
            assert!(
                !is_unsafe_policy(policy),
                "{policy} must be considered safe"
            );
        }
    }

    #[test]
    fn every_allkeys_policy_is_unsafe() {
        for policy in ["allkeys-lru", "allkeys-lfu", "allkeys-random"] {
            assert!(
                is_unsafe_policy(policy),
                "{policy} must be considered unsafe"
            );
        }
    }

    #[test]
    fn log_and_record_does_not_panic_for_every_outcome() {
        log_and_record(&PolicyCheck::Compliant("noeviction".to_string()));
        log_and_record(&PolicyCheck::Violation("allkeys-lru".to_string()));
        log_and_record(&PolicyCheck::CheckFailed("connection refused".to_string()));
    }
}
