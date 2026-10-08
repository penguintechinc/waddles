//! Fixed operational ceilings for the `kv` capability (task requirement:
//! "max key length, max value size, max TTL, and a max key count per app
//! per community... Also a per-invocation op rate limit"). One bundle
//! calling `kv` in a tight loop, storing oversized values, or trying to
//! grow its key count without bound must hit a WIT error, never degrade
//! Valkey for every other tenant sharing it.

/// Longest guest key accepted, in bytes -- see `crate::scope::MAX_GUEST_KEY_LEN`
/// for the actual enforcement; re-exported here so every quota lives under
/// one module for callers auditing limits in one place.
pub use crate::scope::MAX_GUEST_KEY_LEN;

/// Largest value `set`/`increment` accepts, in bytes (task quota: "max
/// value size (e.g. 64KiB)"). Matches the task's own example exactly.
pub const MAX_VALUE_BYTES: usize = 64 * 1024;

/// Longest TTL a bundle may request, in seconds (30 days). `stage.wit`
/// `interface kv`'s doc comment: "the host clamps to KV_MAX_TTL_S" -- a
/// TTL over this is silently capped, never rejected, matching that spec
/// text exactly (unlike key/value size, which are hard rejections).
pub const MAX_TTL_SECONDS: u32 = 30 * 24 * 60 * 60;

/// Highest number of *live* keys one `(tenant, community, app_id)` may
/// hold at once (task quota: "max key count per app per community").
/// Enforced against `KvScope::count_key`'s counter, which this crate keeps
/// consistent with the live key set via the same atomic Lua script every
/// `set`/`increment`/`delete` runs (`crate::backend`) -- see that module's
/// doc for the one known eventual-consistency gap (a key that expires via
/// Valkey's own TTL sweep, rather than an explicit `delete`, is not
/// observed by this counter until something next touches it).
pub const MAX_KEYS_PER_APP: u64 = 10_000;

/// Highest number of `kv` ops one invocation may perform (task quota:
/// "a per-invocation op rate limit"). Enforced against
/// `KvScope::rate_key`, keyed by the host-API `call_id` so it applies
/// identically whether the capability set is constructed fresh per invoke
/// (`core/svc_process`) or shared across a connection's many invokes
/// (`core/svc_action`).
pub const MAX_OPS_PER_INVOKE: u32 = 64;

/// How long a per-invocation op-rate-limit key lives before Valkey expires
/// it on its own (`crate::backend::check_and_increment_rate`). Generous
/// relative to `EXECUTOR_MAX_CALL_TIMEOUT_MS`'s existing 10s ceiling
/// (`penguin-bundle-host::manifest::rules::TIMEOUT_MS_MAX`) so it always
/// outlives the invocation it is scoped to, without lingering indefinitely.
pub const RATE_LIMIT_WINDOW_SECONDS: u64 = 30;

/// How long the self-heal reconciliation lock
/// (`KvScope::reconcile_lock_key`) is held, in milliseconds -- bounds a
/// stuck/crashed reconciler's lock lifetime, and doubles as the de facto
/// rate limit on how often one app can trigger a full `SCAN` (at most once
/// per this window, since the lock is only released early on success, and
/// a successful reconciliation makes `count_key` present again so no
/// subsequent write re-triggers reconciliation until it is evicted again).
pub const RECONCILE_LOCK_TTL_MS: u64 = 5_000;

/// Upper bound on how many keys [`crate::backend`]'s self-heal `SCAN`
/// counts before giving up and reporting this ceiling itself -- bounds the
/// one-shot Lua script's own runtime against a pathological namespace, and
/// naturally saturates at "quota already exceeded" for an app that
/// somehow holds far more live keys than [`MAX_KEYS_PER_APP`] should ever
/// allow (a defensive ceiling, not an expected steady-state count).
pub const RECONCILE_SCAN_LIMIT: u64 = MAX_KEYS_PER_APP * 2;

/// Clamps a guest-supplied `ttl_seconds` to [`MAX_TTL_SECONDS`], preserving
/// the WIT-documented `0` = "no expiry" sentinel unchanged.
pub fn clamp_ttl_seconds(ttl_seconds: u32) -> u32 {
    if ttl_seconds == 0 {
        0
    } else {
        ttl_seconds.min(MAX_TTL_SECONDS)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_ttl_seconds_preserves_the_no_expiry_sentinel() {
        assert_eq!(clamp_ttl_seconds(0), 0);
    }

    #[test]
    fn clamp_ttl_seconds_passes_through_values_within_range() {
        assert_eq!(clamp_ttl_seconds(60), 60);
        assert_eq!(clamp_ttl_seconds(MAX_TTL_SECONDS), MAX_TTL_SECONDS);
    }

    #[test]
    fn clamp_ttl_seconds_caps_values_above_the_maximum() {
        assert_eq!(clamp_ttl_seconds(MAX_TTL_SECONDS + 1), MAX_TTL_SECONDS);
        assert_eq!(clamp_ttl_seconds(u32::MAX), MAX_TTL_SECONDS);
    }
}
