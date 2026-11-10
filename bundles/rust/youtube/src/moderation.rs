//! YouTube live-chat moderation (delete / ban / moderators). NOT IMPLEMENTED YET: every kind
//! returns an explicit `Unsupported` error (never a template, never `Ok`) so the host refuses
//! the op loudly until the owning stream implements it. The host-side policy for these kinds
//! already exists in `core/svc_ingest/.../sender_exec/policy/youtube.rs`.

use crate::logic::{LogicError, RequestTpl};

/// The kinds this module will own once implemented.
pub const KINDS: &[&str] = &[
    "chat.delete",
    "mod.ban",
    "mod.unban",
    "mod.add",
    "mod.remove",
];

/// Builds the request template for `kind`; today always `Unsupported`.
pub fn build(kind: &str) -> Result<RequestTpl, LogicError> {
    Err(LogicError::Unsupported(format!(
        "youtube {kind} is not implemented in this connector build"
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_kind_is_explicitly_unsupported_never_ok() {
        for k in KINDS {
            assert!(matches!(build(k), Err(LogicError::Unsupported(_))), "{k}");
        }
        assert!(matches!(build("other"), Err(LogicError::Unsupported(_))));
    }
}
