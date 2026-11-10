//! Discord application-command registration (bulk overwrite). NOT IMPLEMENTED YET: every kind returns an explicit
//! `Unsupported` error (never a template, never `Ok`) so the host refuses the op loudly until
//! the owning stream implements it. The host-side policy for these kinds already exists in
//! `core/svc_ingest/.../sender_exec/policy/discord.rs`.

use crate::error::{ConnectorError, Result};
use crate::sender::HttpTpl;

/// The kinds this module will own once implemented.
pub const KINDS: &[&str] = &["commands.register", "commands.register.guild"];

/// Builds the request template for `kind`; today always `Unsupported`.
pub fn build(kind: &str) -> Result<HttpTpl> {
    Err(ConnectorError::Unsupported(format!(
        "discord {kind} is not implemented in this connector build"
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_kind_is_explicitly_unsupported_never_ok() {
        for k in KINDS {
            assert!(
                matches!(build(k), Err(ConnectorError::Unsupported(_))),
                "{k}"
            );
        }
        assert!(matches!(
            build("other"),
            Err(ConnectorError::Unsupported(_))
        ));
    }
}
