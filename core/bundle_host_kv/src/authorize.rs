//! The single seam that decides whether the `kv` capability is granted for
//! one invocation's app -- every other module in this crate (and every
//! caller in `core/svc_process`/`core/svc_action`) reaches the Valkey
//! backend only through [`authorize_kv`] first, so replacing *how* the
//! decision is made later never touches key derivation, quota, or metrics
//! code.
//!
//! **Interim implementation.** `docs/bundle-permissions-capability-gate`
//! (branch of the same name) is designing the standard, Android-style
//! install-time permission gate -- one `authorize(scope, permission,
//! resource)` entry point every host call will route through. Until that
//! lands, this function is the manifest-sourced stand-in: `stage.wit`'s
//! `interface kv` doc comment states the capability's tier plainly
//! ("Capability: always granted"), the same tier `context`/`clock`/`log`
//! already occupy in both stages' `capabilities.rs`, and no manifest field
//! exists yet to declare `kv` per-bundle the way `egress`/`data.tables`
//! gate `http`/`db` (`penguin-bundle-host::manifest::Manifest` has no
//! `capabilities`/`kv` field as of the pinned rev either service depends
//! on). So this stand-in grants unconditionally, exactly mirroring the
//! spec today -- the actual security boundary for `kv` is the namespace
//! derivation in `crate::scope::KvScope`, not this gate, until the
//! standard permission model lands and this function's body is replaced
//! with a real lookup.
//!
//! `PERMISSION_ID` is the stable identifier this capability is known by in
//! that future model (and is used today, ahead of time, as the `permission`
//! label on every log line and metric this crate emits) so nothing about
//! observability needs to change when [`authorize_kv`]'s body does.

use crate::scope::KvScope;

/// The permission id `kv` is known by in the forthcoming install-time
/// permission model, and the label value this crate attaches to every log
/// line and OTel metric it emits (task instruction: "Use the permission id
/// `storage.kv` in logs and metrics").
pub const KV_PERMISSION_ID: &str = "storage.kv";

/// One denied authorization decision -- deliberately a plain struct
/// (rather than borrowing `HostResultError`, which lives in
/// `penguin-bundle-host` and is per-service, not per-crate) so this
/// crate's public API has no dependency on either stage's wire types;
/// `crate::KvHost`'s callers map this to whatever error shape their own
/// stage needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Denied {
    /// Stable, machine-matchable reason (e.g. `"not_granted"`).
    pub code: &'static str,
    pub message: String,
}

/// Decides whether `scope.app_id` may use the `kv` capability at all. See
/// the module doc for why this always grants today, and for exactly what
/// replaces this body once the standard permission gate lands.
///
/// # Errors
/// Returns [`Denied`] if the capability is not granted for this scope.
/// Never panics.
pub fn authorize_kv(_scope: &KvScope) -> Result<(), Denied> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn authorize_kv_grants_by_default_matching_the_spec_tier() {
        let scope = KvScope::new("acme", Some("main".to_string()), "waddles.bot.a");
        assert_eq!(authorize_kv(&scope), Ok(()));
    }

    #[test]
    fn permission_id_matches_the_agreed_identifier() {
        assert_eq!(KV_PERMISSION_ID, "storage.kv");
    }
}
