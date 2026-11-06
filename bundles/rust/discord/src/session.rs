//! Opaque gateway session state carried through the host's `session-token`.
//!
//! A connector component is instantiated fresh for every call, so it can hold
//! no state between `on-connect`, `on-frame` and `on-heartbeat-due`. The only
//! durable channel the connector contract offers is `connection-ctx.session-token`,
//! which the HOST owns (it is the one persisting `session_id`/`seq`/
//! `resume_gateway_url`, exactly as `discord_identify_budget::SessionStore`
//! does for the hardcoded path). This module defines the JSON the host puts in
//! that token and validates it strictly, so a hostile token can never reach a
//! wire payload.

use serde::{Deserialize, Serialize};

use crate::error::{ConnectorError, Result};

/// Longest accepted `resume_gateway_url`.
const MAX_URL_LEN: usize = 512;

/// Host-maintained gateway session snapshot. Every field is optional: an absent
/// token means "no session -> fresh IDENTIFY".
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionState {
    /// Discord's `READY.session_id`; present => the next connect is a RESUME.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    /// Last dispatch sequence number seen (RESUME + heartbeat body).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub seq: Option<u64>,
    /// `READY.resume_gateway_url` (host-only: the transport reconnects there).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resume_gateway_url: Option<String>,
    /// This bot's own user id from `READY.user.id`; drives the self-message
    /// filter (the hardcoded path keeps it inside `GatewaySession`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub self_user_id: Option<String>,
}

/// True for a Discord snowflake: 1..=20 ASCII digits.
#[must_use]
pub fn is_snowflake(s: &str) -> bool {
    !s.is_empty() && s.len() <= 20 && s.bytes().all(|b| b.is_ascii_digit())
}

/// `[A-Za-z0-9_-]` only: Discord session ids are hex, and refusing anything
/// else keeps a token that spells the credential slot (`{{...}}`) out of the
/// handshake bytes entirely.
fn is_session_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

impl SessionState {
    /// Parses the host's session token. `None` => empty state (fresh session).
    ///
    /// # Errors
    /// [`ConnectorError::Malformed`] for a token that is not this module's JSON or
    /// carries an out-of-grammar field -- fail loud, never "assume fresh".
    pub fn from_token(token: Option<&str>) -> Result<Self> {
        let Some(raw) = token else {
            return Ok(Self::default());
        };
        let st: Self = serde_json::from_str(raw).map_err(|_| {
            ConnectorError::Malformed("session-token is not the connector's JSON".into())
        })?;
        if st
            .session_id
            .as_deref()
            .is_some_and(|id| !is_session_id(id))
        {
            return Err(ConnectorError::Malformed("session-token session_id".into()));
        }
        if st
            .self_user_id
            .as_deref()
            .is_some_and(|id| !is_snowflake(id))
        {
            return Err(ConnectorError::Malformed(
                "session-token self_user_id".into(),
            ));
        }
        if st
            .resume_gateway_url
            .as_deref()
            .is_some_and(|u| u.len() > MAX_URL_LEN)
        {
            return Err(ConnectorError::Malformed(
                "session-token resume_gateway_url".into(),
            ));
        }
        Ok(st)
    }

    /// Serializes back to the token form the host persists.
    ///
    /// # Errors
    /// [`ConnectorError::Backend`] only if serialization fails (never for these types).
    pub fn to_token(&self) -> Result<String> {
        serde_json::to_string(self).map_err(|e| ConnectorError::Backend(e.to_string()))
    }

    /// True when a RESUME (not IDENTIFY) should be attempted.
    #[must_use]
    pub fn is_resumable(&self) -> bool {
        self.session_id.is_some()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn none_is_a_fresh_session() {
        assert_eq!(
            SessionState::from_token(None).unwrap(),
            SessionState::default()
        );
        assert!(!SessionState::default().is_resumable());
    }

    #[test]
    fn round_trips() {
        let st = SessionState {
            session_id: Some("abc123".into()),
            seq: Some(42),
            resume_gateway_url: Some("wss://resume.discord.gg".into()),
            self_user_id: Some("999".into()),
        };
        let tok = st.to_token().unwrap();
        assert_eq!(SessionState::from_token(Some(&tok)).unwrap(), st);
        assert!(st.is_resumable());
    }

    #[test]
    fn unknown_fields_are_ignored_never_emitted() {
        // The parity harness passes its whole vector context as the token.
        let st = SessionState::from_token(Some(
            r#"{"self_user_id":"999","guild_roles":[["a",8]],"member_role_ids":["a"]}"#,
        ))
        .unwrap();
        assert_eq!(st.self_user_id.as_deref(), Some("999"));
        assert!(!st.is_resumable());
    }

    #[test]
    fn rejects_non_json_unknown_fields_and_bad_grammar() {
        for bad in [
            "sess-1",
            "",
            r#"{"session_id":"{{DISCORD_BOT_TOKEN}}"}"#,
            r#"{"session_id":""}"#,
            r#"{"self_user_id":"abc"}"#,
            r#"{"seq":-1}"#,
        ] {
            assert!(
                matches!(
                    SessionState::from_token(Some(bad)),
                    Err(ConnectorError::Malformed(_))
                ),
                "{bad}"
            );
        }
        let long = format!(r#"{{"resume_gateway_url":"{}"}}"#, "x".repeat(600));
        assert!(SessionState::from_token(Some(&long)).is_err());
    }
}
