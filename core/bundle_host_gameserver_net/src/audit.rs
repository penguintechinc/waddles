//! Audit trail: every open, RCON command and close. Records carry the connection id, an
//! allowlisted command VERB, the command length, a salted hash and the outcome -- NEVER the raw
//! command text (it can carry passwords or PII), the RCON password, host or resolved IP.

use crate::directory::Scope;

/// One audited fact.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuditEvent {
    /// A session/exchange open attempt and its outcome (`ok` or a fixed error label/code).
    Open {
        scope: Scope,
        server: String,
        op: &'static str,
        outcome: String,
    },
    /// One RCON command the guest wrote on an authenticated session (allowed by the host caps).
    Command {
        scope: Scope,
        server: String,
        /// Allowlisted verb (`other` for anything unlisted; credential verbs keep their name).
        verb: String,
        /// Length in bytes of the full command text.
        len: usize,
        /// Per-process salted hash of the full command for correlation (`-` for credential verbs
        /// and rejected writes; the salt never leaves the process).
        digest: String,
        outcome: String,
    },
    /// A session closed, with its byte totals.
    Close {
        scope: Scope,
        server: String,
        bytes_in: usize,
        bytes_out: usize,
    },
}

/// Verbs safe to log by name (read-only / moderation / common admin commands of Source and
/// Minecraft servers). Anything else is logged as `other`.
const VERB_ALLOWLIST: &[&str] = &[
    "status",
    "users",
    "list",
    "players",
    "say",
    "kick",
    "kickid",
    "ban",
    "banid",
    "unban",
    "unbanid",
    "map",
    "maps",
    "changelevel",
    "whitelist",
    "op",
    "deop",
    "tp",
    "time",
    "weather",
    "save-all",
    "stop",
    "help",
    "version",
    "cvarlist",
    "sm",
    "mp_restartgame",
    "exec",
];

/// Verbs whose ARGUMENTS are credentials. They are named in the audit (the fact that a credential
/// command ran matters) but their arguments are never hashed or logged.
const CREDENTIAL_VERBS: &[&str] = &[
    "rcon_password",
    "sv_password",
    "password",
    "setpassword",
    "changepassword",
    "passwd",
    "login",
    "register",
    "auth",
    "sv_setsteamaccount",
];

/// What the audit stores about one command in place of its text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandSummary {
    /// Allowlisted or credential verb, else `other`.
    pub verb: String,
    /// Byte length of the full command.
    pub len: usize,
    /// Salted hash of the full command, `-` for a credential verb.
    pub digest: String,
}

/// Summarizes `command` for auditing without retaining its text. `salt` is a per-process random
/// SipHash key, so digests correlate repeats within a process but cannot be brute-forced offline.
pub fn summarize(command: &str, salt: &std::hash::RandomState) -> CommandSummary {
    use std::hash::{BuildHasher, Hasher};
    let first = command
        .split_whitespace()
        .next()
        .unwrap_or("")
        .trim_start_matches('/')
        .to_ascii_lowercase();
    let credential = CREDENTIAL_VERBS.contains(&first.as_str());
    let verb = if credential || VERB_ALLOWLIST.contains(&first.as_str()) {
        first
    } else {
        "other".to_string()
    };
    let digest = if credential {
        "-".to_string()
    } else {
        let mut h = salt.build_hasher();
        h.write(command.as_bytes());
        format!("{:016x}", h.finish())
    };
    CommandSummary {
        verb,
        len: command.len(),
        digest,
    }
}

#[cfg(test)]
#[allow(clippy::items_after_test_module)]
mod tests {
    use super::*;

    // regression: raw RCON command text (passwords / PII) must never reach the audit log
    #[test]
    fn credential_verbs_are_named_but_their_arguments_are_never_kept() {
        let salt = std::hash::RandomState::new();
        for cmd in [
            "rcon_password hunter2",
            "sv_password s3cret",
            "/login bob@example.com pw123",
            "setpassword x",
        ] {
            let s = summarize(cmd, &salt);
            assert_eq!(s.digest, "-", "{cmd}");
            assert_eq!(s.len, cmd.len());
            let rendered = format!("{s:?}");
            for leak in ["hunter2", "s3cret", "example.com", "pw123"] {
                assert!(!rendered.contains(leak), "{leak} leaked: {rendered}");
            }
        }
        assert_eq!(summarize("rcon_password x", &salt).verb, "rcon_password");
    }

    #[test]
    fn unlisted_verbs_collapse_to_other_and_text_is_never_stored() {
        let salt = std::hash::RandomState::new();
        let s = summarize("frobnicate my.email@example.com", &salt);
        assert_eq!((s.verb.as_str(), s.len), ("other", 31));
        assert!(!format!("{s:?}").contains("example.com"));
        let t = summarize("say my pin is 1234", &salt);
        assert_eq!(t.verb, "say");
        assert!(!format!("{t:?}").contains("1234"));
    }

    #[test]
    fn digest_is_salted_and_stable_within_a_process() {
        let a = std::hash::RandomState::new();
        let b = std::hash::RandomState::new();
        assert_eq!(summarize("status", &a), summarize("status", &a));
        assert_ne!(
            summarize("status", &a).digest,
            summarize("status", &b).digest
        );
        assert_ne!(
            summarize("status", &a).digest,
            summarize("status ", &a).digest
        );
    }
}

/// Destination for audit records (default: structured log; production may also persist).
pub trait AuditSink: Send + Sync {
    /// Records one event; must not block or fail the call.
    fn record(&self, event: &AuditEvent);
}

/// Emits each event as a structured `tracing` record on target `gameserver_net_audit`.
pub struct TracingAudit;

impl AuditSink for TracingAudit {
    fn record(&self, event: &AuditEvent) {
        match event {
            AuditEvent::Open {
                scope,
                server,
                op,
                outcome,
            } => tracing::info!(
                target: "gameserver_net_audit",
                tenant_id = scope.tenant_id,
                community_id = scope.community_id,
                server = %server,
                op,
                outcome = %outcome,
                "gameserver-net open"
            ),
            AuditEvent::Command {
                scope,
                server,
                verb,
                len,
                digest,
                outcome,
            } => tracing::info!(
                target: "gameserver_net_audit",
                tenant_id = scope.tenant_id,
                community_id = scope.community_id,
                server = %server,
                verb = %verb,
                len,
                digest = %digest,
                outcome = %outcome,
                "gameserver-net rcon command"
            ),
            AuditEvent::Close {
                scope,
                server,
                bytes_in,
                bytes_out,
            } => tracing::info!(
                target: "gameserver_net_audit",
                tenant_id = scope.tenant_id,
                community_id = scope.community_id,
                server = %server,
                bytes_in,
                bytes_out,
                "gameserver-net close"
            ),
        }
    }
}
