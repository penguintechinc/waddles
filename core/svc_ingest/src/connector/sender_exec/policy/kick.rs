//! Kick template policy: DECLARED but EMPTY. Kick ops land with the Kick bundle; until a kind
//! is added here every Kick op is `UnknownOp` at parse time and `UnsupportedKind` at
//! execution -- fail-closed, never a silent no-op.

use super::{PlatformPolicy, SlotSyntax};

impl PlatformPolicy {
    /// Kick public-API policy (<https://docs.kick.com/>): the host is declared so a future
    /// kind can only ever reach `api.kick.com`; no kinds, slots or headers yet.
    #[must_use]
    pub fn kick() -> Self {
        Self {
            platform: "kick",
            syntax: SlotSyntax::Double,
            hosts: vec!["api.kick.com".to_string()],
            slots: vec![],
            kinds: vec![],
            auth_prefixes: vec![],
            allowed_headers: vec![],
            allow_http: false,
        }
    }
}
