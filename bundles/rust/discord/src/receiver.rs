//! Receiver logic (`on-connect` / `on-frame` / `on-heartbeat-due` /
//! `on-disconnect`) over a small [`Host`] port, so the whole state machine is
//! exercised on the host without generated bindings. The wasm glue implements
//! [`Host`] with the real `http` + `log` imports.

use serde_json::Value;

use crate::error::{OverlayError, Result};
use crate::gateway::{
    self, DEFAULT_INTENTS, Lifecycle, TOKEN_HANDLE, TOKEN_SLOT, build_heartbeat, build_identify,
    build_resume, classify, parse_frame,
};
use crate::normalize::{self, Event, PLATFORM};
use crate::perms::{DiscordPermissions, GuildInfo, compute_permissions};
use crate::session::{SessionState, is_snowflake};

/// Log levels the overlay emits (mirror of WIT `log.level`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    /// Error.
    Error,
    /// Warn.
    Warn,
    /// Info.
    Info,
    /// Debug.
    Debug,
}

/// Everything the receiver needs from its host: the guild/member REST lookups
/// (guarded `http`, bot token as a secret-ref) and logging.
pub trait Host {
    /// `GET /guilds/{guild_id}`.
    ///
    /// # Errors
    /// Any lookup failure (denied, timeout, transport, malformed); callers fail closed.
    fn guild(&self, guild_id: &str) -> Result<GuildInfo>;
    /// `GET /guilds/{guild_id}/members/{user_id}` role ids (incl. implicit @everyone).
    ///
    /// # Errors
    /// As for [`Host::guild`].
    fn member_roles(&self, guild_id: &str, user_id: &str) -> Result<Vec<String>>;
    /// Emits a log line. Fields are PII-free by the callers' construction.
    fn log(&self, level: Level, message: &str, fields_json: &str);
}

/// Mirror of WIT `connection-ctx` (host-constructed, never guest-influenced).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnCtx {
    /// Must be `discord`.
    pub platform: String,
    /// Opaque source id.
    pub source_id: String,
    /// Host-assigned connection id.
    pub connection_id: String,
    /// The host-owned [`SessionState`] token, if any.
    pub session_token: Option<String>,
}

/// Mirror of WIT `handshake-payload`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Handshake {
    /// Frame bytes with the credential SLOT in place of the token.
    pub bytes: Vec<u8>,
    /// `(slot, opaque handle)`; never a credential.
    pub secret_refs: Vec<(String, String)>,
}

fn require_platform(ctx: &ConnCtx) -> Result<()> {
    if ctx.platform == PLATFORM {
        Ok(())
    } else {
        Err(OverlayError::Unsupported(
            "connection platform is not discord".into(),
        ))
    }
}

/// `on-connect`: RESUME when the host's session token names a session,
/// otherwise a fresh IDENTIFY. Never contains the token itself.
///
/// # Errors
/// [`OverlayError::Unsupported`] for a foreign platform, [`OverlayError::Malformed`]
/// for an invalid session token (fail loud -- never silently re-IDENTIFY, which
/// burns the fleet-wide ~1000/24h IDENTIFY budget).
pub fn on_connect(host: &dyn Host, ctx: &ConnCtx) -> Result<Handshake> {
    require_platform(ctx)?;
    let session = SessionState::from_token(ctx.session_token.as_deref())?;
    let (bytes, mode) = if session.is_resumable() {
        (build_resume(&session)?, "resume")
    } else {
        (build_identify(DEFAULT_INTENTS)?, "identify")
    };
    host.log(
        Level::Debug,
        "discord handshake built",
        &format!(r#"{{"op":"on_connect","mode":"{mode}"}}"#),
    );
    Ok(Handshake {
        bytes,
        secret_refs: vec![(TOKEN_SLOT.to_string(), TOKEN_HANDLE.to_string())],
    })
}

/// `on-heartbeat-due`: a body carrying the host-tracked sequence when the
/// token has one; otherwise `None` so the host sends its platform-default
/// heartbeat (`{"op":1,"d":null}`, valid before any dispatch).
///
/// # Errors
/// As for [`on_connect`]'s token handling.
pub fn on_heartbeat_due(ctx: &ConnCtx) -> Result<Option<Vec<u8>>> {
    require_platform(ctx)?;
    let session = SessionState::from_token(ctx.session_token.as_deref())?;
    match session.seq {
        Some(seq) => Ok(Some(build_heartbeat(Some(seq))?)),
        None => Ok(None),
    }
}

/// `on-disconnect`: logs the reason class only (the reason string is
/// transport-supplied text; it is length-bounded, never frame content).
pub fn on_disconnect(host: &dyn Host, reason: &str) {
    let bounded: String = reason.chars().take(64).collect();
    host.log(
        Level::Info,
        "discord disconnected",
        &serde_json::json!({ "op": "on_disconnect", "reason": bounded }).to_string(),
    );
}

/// Resolves guild permissions through the host, failing CLOSED (`false`/`false`)
/// on any lookup problem and logging a PII-free warning, exactly like the
/// hardcoded resolver's fail-closed path.
fn resolve_permissions(host: &dyn Host, guild_id: &str, author_id: &str) -> DiscordPermissions {
    let result = host.guild(guild_id).and_then(|g| {
        let roles = host.member_roles(guild_id, author_id)?;
        Ok(compute_permissions(
            author_id,
            &g.owner_id,
            &roles,
            &g.roles,
        ))
    });
    match result {
        Ok(p) => p,
        Err(e) => {
            let class = match e {
                OverlayError::Unsupported(_) => "unsupported",
                OverlayError::Malformed(_) => "malformed",
                OverlayError::Backend(_) => "backend",
            };
            host.log(
                Level::Warn,
                "discord permissions unavailable; failing closed",
                &serde_json::json!({ "op": "resolve_permissions", "class": class }).to_string(),
            );
            DiscordPermissions::default()
        }
    }
}

/// `on-frame`: one raw frame in, zero or more events out.
///
/// * Gateway frame (`op` present): lifecycle frames yield no events;
///   `MESSAGE_CREATE` yields one `message`; `INTERACTION_CREATE` yields one
///   `interaction.*`; every other dispatch yields none.
/// * Interaction webhook body (no `op`, has `type`): one `interaction.*`
///   (PING yields none -- the host answers it). The host has ALREADY verified
///   the Ed25519 signature; this code never sees an unverified body.
///
/// # Errors
/// [`OverlayError::Malformed`] / [`OverlayError::Unsupported`] -- the host drops
/// that frame and keeps the connection (frame-error contract).
pub fn on_frame(
    host: &dyn Host,
    ctx: &ConnCtx,
    bytes: &[u8],
    received_at: &str,
) -> Result<Vec<Event>> {
    require_platform(ctx)?;
    let v: Value = serde_json::from_slice(bytes)
        .map_err(|_| OverlayError::Malformed("frame is not JSON".into()))?;
    if v.get("op").is_some() {
        return gateway_frame(host, ctx, bytes, received_at);
    }
    if v.get("type").is_some() {
        return Ok(normalize::interaction_event(&v, received_at)?
            .into_iter()
            .collect());
    }
    Err(OverlayError::Malformed(
        "frame is neither a gateway envelope nor an interaction".into(),
    ))
}

fn gateway_frame(
    host: &dyn Host,
    ctx: &ConnCtx,
    bytes: &[u8],
    received_at: &str,
) -> Result<Vec<Event>> {
    let frame = parse_frame(bytes)?;
    match classify(&frame)? {
        Lifecycle::Dispatch => {}
        // Lifecycle frames never carry events. Their ACTION (reconnect, session
        // persistence, ack tracking) is a host duty -- see README "Host duties".
        Lifecycle::Reconnect | Lifecycle::InvalidSession { .. } => {
            host.log(
                Level::Warn,
                "discord gateway lifecycle frame",
                &serde_json::json!({ "op": "on_frame", "gateway_op": frame.op }).to_string(),
            );
            return Ok(Vec::new());
        }
        _ => return Ok(Vec::new()),
    }
    match frame.t.as_deref() {
        Some("MESSAGE_CREATE") => {
            let session = SessionState::from_token(ctx.session_token.as_deref())?;
            let Some(msg) =
                normalize::normalize_message_create(&frame.d, session.self_user_id.as_deref())
            else {
                host.log(
                    Level::Debug,
                    "MESSAGE_CREATE dropped (self-authored or missing author/channel)",
                    r#"{"op":"on_frame"}"#,
                );
                return Ok(Vec::new());
            };
            let perms = match msg.guild_id.as_deref() {
                Some(g) if is_snowflake(g) && is_snowflake(&msg.author_id) => {
                    resolve_permissions(host, g, &msg.author_id)
                }
                // DM: no guild, nothing to resolve -> fail closed without a lookup.
                _ => DiscordPermissions::default(),
            };
            Ok(vec![normalize::message_event(&msg, perms, received_at)])
        }
        Some("INTERACTION_CREATE") => Ok(normalize::interaction_event(&frame.d, received_at)?
            .into_iter()
            .collect()),
        _ => Ok(Vec::new()),
    }
}

/// Re-export for the tool surface.
pub use gateway::{CloseCodeClass, classify_close_code};

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    #[derive(Default)]
    struct FakeHost {
        guild: Option<GuildInfo>,
        roles: Option<Vec<String>>,
        logs: RefCell<Vec<(Level, String, String)>>,
        lookups: RefCell<u32>,
    }

    impl Host for FakeHost {
        fn guild(&self, _g: &str) -> Result<GuildInfo> {
            *self.lookups.borrow_mut() += 1;
            self.guild
                .clone()
                .ok_or_else(|| OverlayError::Backend("denied".into()))
        }
        fn member_roles(&self, _g: &str, _u: &str) -> Result<Vec<String>> {
            *self.lookups.borrow_mut() += 1;
            self.roles
                .clone()
                .ok_or_else(|| OverlayError::Backend("denied".into()))
        }
        fn log(&self, l: Level, m: &str, f: &str) {
            self.logs.borrow_mut().push((l, m.into(), f.into()));
        }
    }

    fn ctx(token: Option<&str>) -> ConnCtx {
        ConnCtx {
            platform: "discord".into(),
            source_id: "src".into(),
            connection_id: "c1".into(),
            session_token: token.map(str::to_string),
        }
    }

    fn mc(author: &str, guild: Option<&str>) -> Vec<u8> {
        let mut d = serde_json::json!({"author":{"id":author,"username":"someuser"},"channel_id":"222","id":"333","content":"hi"});
        if let Some(g) = guild {
            d["guild_id"] = g.into();
        }
        serde_json::to_vec(&serde_json::json!({"op":0,"s":4,"t":"MESSAGE_CREATE","d":d})).unwrap()
    }

    #[test]
    fn connect_identifies_when_fresh_and_resumes_with_a_session() {
        let h = FakeHost::default();
        let hs = on_connect(&h, &ctx(None)).unwrap();
        assert_eq!(
            hs.secret_refs,
            vec![(TOKEN_SLOT.to_string(), TOKEN_HANDLE.to_string())]
        );
        let v: Value = serde_json::from_slice(&hs.bytes).unwrap();
        assert_eq!(v["op"], 2);

        let tok = r#"{"session_id":"s1","seq":12}"#;
        let hs = on_connect(&h, &ctx(Some(tok))).unwrap();
        let v: Value = serde_json::from_slice(&hs.bytes).unwrap();
        assert_eq!(
            (
                v["op"].as_u64(),
                v["d"]["session_id"].as_str(),
                v["d"]["seq"].as_u64()
            ),
            (Some(6), Some("s1"), Some(12))
        );
        // Never the credential.
        assert!(!String::from_utf8(hs.bytes).unwrap().contains("Bot "));
    }

    #[test]
    fn connect_fails_loud_on_a_bad_token_or_foreign_platform() {
        let h = FakeHost::default();
        assert!(matches!(
            on_connect(&h, &ctx(Some("sess-1"))),
            Err(OverlayError::Malformed(_))
        ));
        let mut c = ctx(None);
        c.platform = "twitch".into();
        assert!(matches!(
            on_connect(&h, &c),
            Err(OverlayError::Unsupported(_))
        ));
        assert!(on_frame(&h, &c, b"{}", "t").is_err());
        assert!(on_heartbeat_due(&c).is_err());
    }

    #[test]
    fn heartbeat_body_only_when_a_seq_is_known() {
        assert_eq!(on_heartbeat_due(&ctx(None)).unwrap(), None);
        assert_eq!(
            on_heartbeat_due(&ctx(Some(r#"{"session_id":"s"}"#))).unwrap(),
            None
        );
        let b = on_heartbeat_due(&ctx(Some(r#"{"seq":7}"#)))
            .unwrap()
            .unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&b).unwrap()["d"], 7);
    }

    #[test]
    fn lifecycle_frames_yield_no_events() {
        let h = FakeHost::default();
        for raw in [
            r#"{"op":10,"d":{"heartbeat_interval":41250}}"#,
            r#"{"op":11}"#,
            r#"{"op":1,"d":null}"#,
            r#"{"op":7,"d":null}"#,
            r#"{"op":9,"d":false}"#,
            r#"{"op":0,"s":1,"t":"READY","d":{"session_id":"a","user":{"id":"9"}}}"#,
            r#"{"op":0,"s":2,"t":"RESUMED","d":{}}"#,
            r#"{"op":0,"s":3,"t":"GUILD_CREATE","d":{"id":"1"}}"#,
        ] {
            assert!(
                on_frame(&h, &ctx(None), raw.as_bytes(), "t")
                    .unwrap()
                    .is_empty(),
                "{raw}"
            );
        }
        // op 7 / 9 are logged at WARN (operators need to see them).
        assert_eq!(
            h.logs
                .borrow()
                .iter()
                .filter(|(l, ..)| *l == Level::Warn)
                .count(),
            2
        );
    }

    #[test]
    fn message_create_resolves_permissions_via_the_host() {
        let h = FakeHost {
            guild: Some(GuildInfo {
                owner_id: "900".into(),
                roles: vec![("adm".into(), 8)],
            }),
            roles: Some(vec!["adm".into(), "111".into()]),
            ..FakeHost::default()
        };
        let evs = on_frame(&h, &ctx(None), &mc("901", Some("111")), "ts").unwrap();
        assert_eq!(evs.len(), 1);
        let p: Value = serde_json::from_str(&evs[0].payload_json).unwrap();
        assert_eq!(
            (p["is_mod"].as_bool(), p["is_broadcaster"].as_bool()),
            (Some(true), Some(true))
        );
        assert_eq!(evs[0].occurred_at, "ts");
    }

    /// Pins a KNOWN GAP (host duty): with no `self_user_id` in the session token the
    /// overlay cannot filter the bot's own messages. The host-side self-identity
    /// filter (feature/p6b-host-transport-support) must populate it before the flag
    /// is enabled; if this ever starts filtering, update the README too.
    #[test]
    fn without_a_self_id_the_bots_own_messages_are_not_filtered() {
        let h = FakeHost::default();
        assert_eq!(
            on_frame(&h, &ctx(None), &mc("999", None), "t")
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn lookup_failure_fails_closed_and_logs_without_pii() {
        let h = FakeHost::default();
        let evs = on_frame(&h, &ctx(None), &mc("901", Some("111")), "ts").unwrap();
        let p: Value = serde_json::from_str(&evs[0].payload_json).unwrap();
        assert_eq!(
            (p["is_mod"].as_bool(), p["is_broadcaster"].as_bool()),
            (Some(false), Some(false))
        );
        let logs = h.logs.borrow();
        let warn = logs.iter().find(|(l, ..)| *l == Level::Warn).unwrap();
        assert!(!warn.2.contains("901") && !warn.2.contains("someuser"));
    }

    #[test]
    fn dm_never_attempts_a_lookup() {
        let h = FakeHost::default();
        let evs = on_frame(&h, &ctx(None), &mc("444", None), "ts").unwrap();
        assert_eq!(*h.lookups.borrow(), 0);
        let p: Value = serde_json::from_str(&evs[0].payload_json).unwrap();
        assert!(p["guild_id"].is_null());
    }

    #[test]
    fn self_messages_are_dropped_using_the_session_self_id() {
        let h = FakeHost::default();
        let c = ctx(Some(r#"{"self_user_id":"999"}"#));
        assert!(
            on_frame(&h, &c, &mc("999", Some("111")), "t")
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            on_frame(&h, &c, &mc("444", Some("111")), "t")
                .unwrap()
                .len(),
            1
        );
        // Only the kept message looked anything up (guild() failed first -> 1 call).
        assert_eq!(
            *h.lookups.borrow(),
            1,
            "dropped message must not trigger REST"
        );
    }

    #[test]
    fn interactions_via_webhook_and_gateway_dispatch() {
        let h = FakeHost::default();
        let body = br#"{"id":"777","type":2,"guild_id":"1","channel_id":"2","member":{"user":{"id":"4","username":"u"},"permissions":"0"},"data":{"name":"ping"}}"#;
        let evs = on_frame(&h, &ctx(None), body, "t").unwrap();
        assert_eq!(evs[0].event_type, "interaction.command");
        assert!(
            on_frame(&h, &ctx(None), br#"{"id":"1","type":1}"#, "t")
                .unwrap()
                .is_empty()
        );
        let wrapped = serde_json::to_vec(&serde_json::json!({"op":0,"s":2,"t":"INTERACTION_CREATE","d":serde_json::from_slice::<Value>(body).unwrap()})).unwrap();
        assert_eq!(on_frame(&h, &ctx(None), &wrapped, "t").unwrap().len(), 1);
    }

    #[test]
    fn garbage_fails_loud() {
        let h = FakeHost::default();
        for raw in ["nope", "{}", r#"{"op":99}"#, r#"{"op":"x"}"#] {
            assert!(
                on_frame(&h, &ctx(None), raw.as_bytes(), "t").is_err(),
                "{raw}"
            );
        }
    }

    #[test]
    fn disconnect_logs_a_bounded_reason() {
        let h = FakeHost::default();
        on_disconnect(&h, &"x".repeat(500));
        let logs = h.logs.borrow();
        let f: Value = serde_json::from_str(&logs[0].2).unwrap();
        assert_eq!(f["reason"].as_str().unwrap().len(), 64);
    }
}
