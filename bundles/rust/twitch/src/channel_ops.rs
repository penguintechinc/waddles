//! Twitch channel operations (v3.1). NOT IMPLEMENTED YET: every kind returns an explicit
//! `Unsupported` frame error (never a template, never `Ok`) so the host refuses the op
//! loudly until the owning stream implements it. The host-side policy for these kinds already
//! exists in `core/svc_ingest/.../sender_exec/policy/twitch.rs`.

use crate::common::FrameErr;
use crate::sender::Tpl;

/// The kinds this module will own once implemented.
pub const KINDS: &[&str] = &[];

/// Builds the request template for `kind`; today always `Unsupported`.
pub fn build(kind: &str, _payload_json: &str) -> Result<Tpl, FrameErr> {
    Err(FrameErr::Unsupported(format!(
        "twitch {kind} is not implemented in this connector build"
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_kind_is_explicitly_unsupported_never_ok() {
        for k in KINDS {
            assert!(
                matches!(build(k, "{}"), Err(FrameErr::Unsupported(_))),
                "{k}"
            );
        }
        assert!(matches!(
            build("other", "{}"),
            Err(FrameErr::Unsupported(_))
        ));
    }
}
