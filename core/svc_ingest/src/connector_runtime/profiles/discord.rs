//! The Discord [`OverlayProfile`] (spec 1.2, README `bundles/rust/discord`
//! "Host duties" 1/1b/2, `docs/connector-host-capability.md` "Session
//! lifecycle"): the platform-static derivation from a hub-api Discord binding
//! to one gateway [`WsConfig`].
//!
//! Everything on the wire is a HOST CONSTANT in this file: the gateway URL, the
//! credential-slot prefix, the session/heartbeat/reconnect/close-code rules and
//! the resume-host allow-list. The only binding datum that reaches the config
//! is the guild snowflake (validated `^[0-9]{17,20}$`), and it reaches only the
//! hashed `source_key`; a hostile binding is rejected with a fixed-text
//! [`PlanError`], never sanitized and never echoed.
//!
//! # Self-message filter (`self_user_id`)
//!
//! The overlay drops the bot's own `MESSAGE_CREATE`s using `self_user_id` from
//! the host's session token, which the host renders ONCE per `connect()`; the
//! driver then reuses that connect-time context for every frame of the
//! connection. A fresh IDENTIFY connection therefore starts with no session
//! token and could never filter its own messages. The profile closes that gap
//! with a `READY -> Reconnect` directive: READY is captured (session id,
//! sequence, `/d/user/id`, resume URL) and the connection is ended cleanly with
//! state kept, so the very next connect is a RESUME whose token carries
//! `self_user_id`. RESUME is exempt from the identify budget.
//!
//! # Identify budget
//!
//! Discord must not run without the fleet-wide IDENTIFY budget (a single
//! process bursting IDENTIFYs exhausts the ~1000/24h `session_start_limit` and
//! gets the bot token reset). Until PR-I flips [`IDENTIFY_BUDGET_READY`] the
//! profile refuses every binding ([`PlanError::IdentifyBudgetNotReady`]).

use std::collections::{BTreeMap, HashMap};
use std::hash::{Hash, Hasher};
use std::time::Duration;

use connection_resolver::{BindingSummary, ConnectionKind, ConnectionRef};

use super::super::feed::{ConnKey, DesiredConnection, TransportPlan};
use super::super::plan::{OverlayProfile, PlanError, RuntimeEnv};
use super::super::selector::IDENTIFY_BUDGET_READY;
use crate::connector::{
    CaptureRule, CloseCodeRule, FrameDirective, FrameMatch, HandshakeAfter, HeartbeatAck,
    HeartbeatCapture, LifecycleAction, ReconnectUrlCapture, Scalar, SessionRules, WsConfig,
};

/// Platform slug (also the `ConnKey` platform and the pinned connector slug).
pub const PLATFORM: &str = "discord";
/// Core connector bundle id.
pub const APP_ID: &str = "waddles.core.service.discord";
/// Provider name in hub-api's binding list.
pub const PROVIDER: &str = "discord";
/// Capability the binding list is scoped to.
pub const CAPABILITY: &str = "chat_ingest";
/// Gateway endpoint: API v10, JSON encoding. Host constant, never binding data.
pub const GATEWAY_URL: &str = "wss://gateway.discord.gg/?v=10&encoding=json";
/// Handle the overlay's IDENTIFY names for the bot token (`secret_refs`).
pub const BOT_HANDLE: &str = "discord-bot";
/// Byte prefix the credential slot may directly follow in the IDENTIFY/RESUME.
pub const TOKEN_PREFIX: &str = "\"token\":\"";
/// Hosts a captured `resume_gateway_url` may point at.
pub const RESUME_ALLOWED_HOSTS: &str = "*.discord.gg";
/// Query appended to the captured resume URL.
pub const RESUME_QUERY: &str = "v=10&encoding=json";
/// Binding metadata key naming the guild the community's bot serves.
pub const META_GUILD_ID: &str = "guild_id";

/// Fixed reason carried by the `PlanError::Config` returned when the driver
/// heartbeat is disabled: Discord closes a connection that never heartbeats.
pub const HEARTBEAT_REQUIRED: &str = "discord requires the connector heartbeat";

/// Connect + handshake-send bound.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);
/// Max gap between DATA frames. Discord ACKs a heartbeat roughly every 41 s, so
/// 150 s means three consecutive silent heartbeats before the read fails.
const IDLE_TIMEOUT: Duration = Duration::from_secs(150);
/// Hard lifetime cap per connection; the reconnect RESUMEs.
const MAX_LIFETIME: Duration = Duration::from_secs(24 * 60 * 60);
/// The HELLO must arrive before the handshake is sent.
const FIRST_FRAME_TIMEOUT: Duration = Duration::from_secs(10);
/// Snowflake length bounds for `guild_id`.
const SNOWFLAKE_LEN: std::ops::RangeInclusive<usize> = 17..=20;

/// The Discord overlay profile.
#[derive(Debug, Clone)]
pub struct DiscordProfile {
    budget_ready: bool,
}

impl Default for DiscordProfile {
    fn default() -> Self {
        Self::new()
    }
}

impl DiscordProfile {
    /// Production constructor: readiness follows [`IDENTIFY_BUDGET_READY`].
    #[must_use]
    pub fn new() -> Self {
        // SEAM(PR-I): the identify budget's `HandshakeGate` (awaited before
        // IDENTIFY, never before RESUME) is attached to the transport by the
        // supervisor, not carried by the plan; once PR-I lands this profile
        // only needs the readiness constant flipped.
        Self {
            budget_ready: IDENTIFY_BUDGET_READY,
        }
    }

    /// Overrides the readiness gate. Test seam for the config derivation and
    /// real-path suites; production wiring uses [`Self::new`].
    #[doc(hidden)]
    #[must_use]
    pub fn with_budget_ready(mut self, ready: bool) -> Self {
        self.budget_ready = ready;
        self
    }
}

/// True for a Discord snowflake: 17..=20 ASCII digits and nothing else (no
/// sign, whitespace, CR/LF; a trailing newline is rejected, unlike `$`).
fn is_guild_id(s: &str) -> bool {
    SNOWFLAKE_LEN.contains(&s.len()) && s.bytes().all(|b| b.is_ascii_digit())
}

fn op(n: i64) -> (String, Scalar) {
    ("/op".to_string(), Scalar::Int(n))
}

fn event(t: &str) -> (String, Scalar) {
    ("/t".to_string(), Scalar::Str(t.to_string()))
}

fn ready() -> FrameMatch {
    FrameMatch::Json(vec![op(0), event("READY")])
}

fn directive(conds: Vec<(String, Scalar)>, action: LifecycleAction) -> FrameDirective {
    FrameDirective {
        when: FrameMatch::Json(conds),
        action,
    }
}

fn close(from: u16, to: u16, action: LifecycleAction) -> CloseCodeRule {
    CloseCodeRule { from, to, action }
}

/// The complete, constant Discord session lifecycle (host-capability doc
/// "Discord overlay host config" + "Gateway gaps closed" + `self_user_id`).
fn session_rules() -> SessionRules {
    SessionRules {
        captures: vec![
            CaptureRule {
                when: ready(),
                pointer: "/d/session_id".into(),
                name: "SESSION_ID".into(),
            },
            CaptureRule {
                when: FrameMatch::Json(vec![op(0)]),
                pointer: "/s".into(),
                name: "SEQ".into(),
            },
            CaptureRule {
                when: ready(),
                pointer: "/d/user/id".into(),
                name: "SELF_USER_ID".into(),
            },
        ],
        heartbeat: Some(HeartbeatCapture {
            when: FrameMatch::Json(vec![op(10)]),
            pointer: "/d/heartbeat_interval".into(),
        }),
        directives: vec![
            directive(vec![op(7)], LifecycleAction::Reconnect),
            directive(
                vec![op(9), ("/d".into(), Scalar::Bool(true))],
                LifecycleAction::Reconnect,
            ),
            directive(
                vec![op(9), ("/d".into(), Scalar::Bool(false))],
                LifecycleAction::ReconnectFresh,
            ),
            // READY ends the connection cleanly with state kept so the next
            // connect is a RESUME whose session token carries self_user_id
            // (see the module docs).
            FrameDirective {
                when: ready(),
                action: LifecycleAction::Reconnect,
            },
        ],
        close_codes: vec![
            close(4004, 4004, LifecycleAction::Park),
            close(4010, 4014, LifecycleAction::Park),
            close(4007, 4007, LifecycleAction::ReconnectFresh),
            close(4009, 4009, LifecycleAction::ReconnectFresh),
            close(4000, 4003, LifecycleAction::Reconnect),
            close(4005, 4005, LifecycleAction::Reconnect),
            close(4008, 4008, LifecycleAction::Reconnect),
        ],
        session_token_template: Some(
            r#"{"session_id":"{{SESSION_ID}}","seq":{{SEQ}},"self_user_id":"{{SELF_USER_ID}}"}"#
                .into(),
        ),
        default_heartbeat: Some(r#"{"op":1,"d":{{SEQ|null}}}"#.into()),
        host_owned_heartbeat: true,
        reconnect_url: Some(ReconnectUrlCapture {
            when: ready(),
            pointer: "/d/resume_gateway_url".into(),
            allowed_hosts: vec![RESUME_ALLOWED_HOSTS.into()],
            query: RESUME_QUERY.into(),
        }),
        heartbeat_ack: Some(HeartbeatAck {
            when: FrameMatch::Json(vec![op(11)]),
            max_unacked: 1,
        }),
        handshake_after: HandshakeAfter::FirstFrame {
            timeout: FIRST_FRAME_TIMEOUT,
        },
        jitter_first_heartbeat: true,
    }
}

/// Builds the gateway config for one community's guild. `guild_id` is already
/// validated; it only feeds the hashed `source_key`.
fn ws_config(guild_id: &str, conn: ConnectionRef, env: &RuntimeEnv) -> WsConfig {
    WsConfig {
        platform: PLATFORM.to_string(),
        url: GATEWAY_URL.to_string(),
        source_key: format!("discord:guild:{guild_id}"),
        handles: HashMap::from([(BOT_HANDLE.to_string(), conn)]),
        resume_token: None,
        connect_timeout: CONNECT_TIMEOUT,
        idle_timeout: IDLE_TIMEOUT,
        max_lifetime: MAX_LIFETIME,
        egress_allow: env.egress_allow.clone(),
        max_frame_bytes: env.max_frame_bytes,
        dedup_window: 0,
        allow_loopback_insecure: false,
        slot_prefixes: vec![TOKEN_PREFIX.to_string()],
        auto_replies: Vec::new(),
        self_echo: None,
        // The interval is captured from HELLO (`session.heartbeat`).
        heartbeat_interval: None,
        session: session_rules(),
    }
}

/// Deterministic digest of the derived config (host constants + guild +
/// connection ids), the feed tenant and the credential-rotation counter.
/// `WsConfig` holds no secret, so its `Debug` form is safe to hash; the
/// counter is not secret material either.
fn fingerprint(cfg: &WsConfig, tenant: i64, secret_version: i64) -> u64 {
    let mut h = std::collections::hash_map::DefaultHasher::new();
    format!("{cfg:?}").hash(&mut h);
    tenant.hash(&mut h);
    secret_version.hash(&mut h);
    h.finish()
}

impl OverlayProfile for DiscordProfile {
    fn platform(&self) -> &'static str {
        PLATFORM
    }

    fn app_id(&self) -> &'static str {
        APP_ID
    }

    fn provider(&self) -> &'static str {
        PROVIDER
    }

    fn capability(&self) -> &'static str {
        CAPABILITY
    }

    fn plan(
        &self,
        b: &BindingSummary,
        env: &RuntimeEnv,
    ) -> Result<Vec<DesiredConnection>, PlanError> {
        if b.provider != PROVIDER {
            return Err(PlanError::WrongProvider);
        }
        if b.kind != ConnectionKind::PlatformOauth {
            return Err(PlanError::WrongKind);
        }
        if !self.budget_ready {
            return Err(PlanError::IdentifyBudgetNotReady);
        }
        if !env.heartbeat_enabled {
            return Err(PlanError::Config(HEARTBEAT_REQUIRED));
        }
        let tenant = Some(b.tenant_id)
            .filter(|t| *t >= 1)
            .ok_or(PlanError::Config("binding tenant is not positive"))?;
        let version = Some(b.secret_version)
            .filter(|v| *v >= 1)
            .ok_or(PlanError::Config("binding secret version is not positive"))?;
        let guild_id = metadata_guild_id(&b.metadata)?;
        let cfg = ws_config(
            guild_id,
            ConnectionRef {
                community_id: b.community_id,
                connection_id: b.connection_id,
            },
            env,
        );
        let fingerprint = fingerprint(&cfg, tenant, version);
        Ok(vec![DesiredConnection {
            key: ConnKey {
                platform: PLATFORM,
                community_id: b.community_id,
                connection_id: b.connection_id,
            },
            tenant_id: tenant,
            plan: TransportPlan::Ws(Box::new(cfg)),
            fingerprint,
        }])
    }
}

/// The validated `guild_id`, or a fixed-text error that never echoes it.
fn metadata_guild_id(m: &BTreeMap<String, String>) -> Result<&str, PlanError> {
    let raw = m
        .get(META_GUILD_ID)
        .ok_or(PlanError::MissingKey(META_GUILD_ID))?;
    if is_guild_id(raw) {
        Ok(raw.as_str())
    } else {
        Err(PlanError::InvalidValue(META_GUILD_ID))
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use std::net::IpAddr;

    use super::*;
    use crate::connector::secrets::{Destination, ResolvedAuth};
    use crate::connector::webhook::{BoxFut, SecretError};
    use crate::connector::{TransportError, TransportSecretSource, WsTransport};

    const GUILD: &str = "123456789012345678";

    fn env() -> RuntimeEnv {
        RuntimeEnv {
            environment: "alpha".into(),
            max_frame_bytes: 65_536,
            egress_allow: vec![],
            heartbeat_enabled: true,
        }
    }

    fn binding(guild: Option<&str>) -> BindingSummary {
        let mut metadata = BTreeMap::new();
        if let Some(g) = guild {
            metadata.insert(META_GUILD_ID.to_string(), g.to_string());
        }
        BindingSummary {
            tenant_id: 1,
            secret_version: 1,
            community_id: 7,
            connection_id: 11,
            provider: "discord".into(),
            kind: ConnectionKind::PlatformOauth,
            metadata,
        }
    }

    fn ready_profile() -> DiscordProfile {
        DiscordProfile::new().with_budget_ready(true)
    }

    fn plan_ok(b: &BindingSummary) -> DesiredConnection {
        let mut v = ready_profile().plan(b, &env()).unwrap();
        assert_eq!(v.len(), 1, "exactly one connection per binding");
        v.remove(0)
    }

    fn ws(d: DesiredConnection) -> WsConfig {
        match d.plan {
            TransportPlan::Ws(c) => *c,
            _ => panic!("discord must plan a Ws transport"),
        }
    }

    #[derive(Clone)]
    struct NoSecrets;

    impl TransportSecretSource for NoSecrets {
        fn resolve(
            &self,
            _r: ConnectionRef,
            _d: Destination,
        ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
            Box::pin(async { Err(SecretError::Missing) })
        }
    }

    #[test]
    fn identity_is_the_discord_overlay() {
        let p = DiscordProfile::default();
        assert_eq!(p.platform(), "discord");
        assert_eq!(p.app_id(), "waddles.core.service.discord");
        assert_eq!(p.provider(), "discord");
        assert_eq!(p.capability(), "chat_ingest");
    }

    #[test]
    fn golden_ws_config_for_a_well_formed_binding() {
        let d = plan_ok(&binding(Some(GUILD)));
        assert_eq!(
            d.key,
            ConnKey {
                platform: "discord",
                community_id: 7,
                connection_id: 11
            }
        );
        assert_eq!(d.tenant_id, 1, "the feed's tenant");
        let c = ws(d);
        assert_eq!(c.platform, "discord");
        assert_eq!(c.url, "wss://gateway.discord.gg/?v=10&encoding=json");
        assert_eq!(c.source_key, "discord:guild:123456789012345678");
        assert_eq!(
            c.handles,
            HashMap::from([(
                "discord-bot".to_string(),
                ConnectionRef {
                    community_id: 7,
                    connection_id: 11
                }
            )])
        );
        assert_eq!(c.slot_prefixes, vec!["\"token\":\"".to_string()]);
        assert_eq!(c.resume_token, None);
        assert_eq!(c.connect_timeout, Duration::from_secs(15));
        assert_eq!(c.idle_timeout, Duration::from_secs(150));
        assert_eq!(c.max_lifetime, Duration::from_secs(86_400));
        assert_eq!(c.max_frame_bytes, 65_536);
        assert!(c.egress_allow.is_empty());
        assert_eq!(c.dedup_window, 0);
        assert!(!c.allow_loopback_insecure, "never loopback-insecure");
        assert!(c.auto_replies.is_empty());
        assert!(c.self_echo.is_none());
        assert_eq!(c.heartbeat_interval, None, "interval comes from HELLO");
        assert_eq!(c.session, session_rules());
    }

    #[test]
    fn golden_session_rules_match_the_documented_host_config() {
        let s = ws(plan_ok(&binding(Some(GUILD)))).session;
        let names: Vec<(&str, &str)> = s
            .captures
            .iter()
            .map(|c| (c.name.as_str(), c.pointer.as_str()))
            .collect();
        assert_eq!(
            names,
            vec![
                ("SESSION_ID", "/d/session_id"),
                ("SEQ", "/s"),
                ("SELF_USER_ID", "/d/user/id"),
            ]
        );
        assert_eq!(s.captures[0].when, ready());
        assert_eq!(s.captures[1].when, FrameMatch::Json(vec![op(0)]));
        assert_eq!(s.captures[2].when, ready());
        assert_eq!(
            s.heartbeat,
            Some(HeartbeatCapture {
                when: FrameMatch::Json(vec![op(10)]),
                pointer: "/d/heartbeat_interval".into()
            })
        );
        let dirs: Vec<(FrameMatch, LifecycleAction)> = s
            .directives
            .iter()
            .map(|d| (d.when.clone(), d.action))
            .collect();
        assert_eq!(
            dirs,
            vec![
                (FrameMatch::Json(vec![op(7)]), LifecycleAction::Reconnect),
                (
                    FrameMatch::Json(vec![op(9), ("/d".into(), Scalar::Bool(true))]),
                    LifecycleAction::Reconnect
                ),
                (
                    FrameMatch::Json(vec![op(9), ("/d".into(), Scalar::Bool(false))]),
                    LifecycleAction::ReconnectFresh
                ),
                (ready(), LifecycleAction::Reconnect),
            ]
        );
        let codes: Vec<(u16, u16, LifecycleAction)> = s
            .close_codes
            .iter()
            .map(|c| (c.from, c.to, c.action))
            .collect();
        assert_eq!(
            codes,
            vec![
                (4004, 4004, LifecycleAction::Park),
                (4010, 4014, LifecycleAction::Park),
                (4007, 4007, LifecycleAction::ReconnectFresh),
                (4009, 4009, LifecycleAction::ReconnectFresh),
                (4000, 4003, LifecycleAction::Reconnect),
                (4005, 4005, LifecycleAction::Reconnect),
                (4008, 4008, LifecycleAction::Reconnect),
            ]
        );
        assert_eq!(
            s.session_token_template.as_deref(),
            Some(
                r#"{"session_id":"{{SESSION_ID}}","seq":{{SEQ}},"self_user_id":"{{SELF_USER_ID}}"}"#
            )
        );
        assert_eq!(
            s.default_heartbeat.as_deref(),
            Some(r#"{"op":1,"d":{{SEQ|null}}}"#)
        );
        assert!(s.host_owned_heartbeat);
        let r = s.reconnect_url.unwrap();
        assert_eq!(r.when, ready());
        assert_eq!(r.pointer, "/d/resume_gateway_url");
        assert_eq!(r.allowed_hosts, vec!["*.discord.gg".to_string()]);
        assert_eq!(r.query, "v=10&encoding=json");
        let a = s.heartbeat_ack.unwrap();
        assert_eq!(a.when, FrameMatch::Json(vec![op(11)]));
        assert_eq!(a.max_unacked, 1);
        assert_eq!(
            s.handshake_after,
            HandshakeAfter::FirstFrame {
                timeout: Duration::from_secs(10)
            }
        );
        assert!(s.jitter_first_heartbeat);
    }

    /// The self-message filter needs `self_user_id` captured from READY and
    /// rendered into the session token the overlay reads (README host duty 2).
    #[test]
    fn self_user_id_is_captured_from_ready_and_rendered_into_the_token() {
        let s = ws(plan_ok(&binding(Some(GUILD)))).session;
        let cap = s
            .captures
            .iter()
            .find(|c| c.name == "SELF_USER_ID")
            .expect("SELF_USER_ID capture present");
        assert_eq!(cap.pointer, "/d/user/id");
        assert_eq!(cap.when, ready());
        assert!(s
            .session_token_template
            .as_deref()
            .unwrap()
            .contains(r#""self_user_id":"{{SELF_USER_ID}}""#));
        // Without a directive ending the first connection the filter would be
        // inert for it (the connect-time token is None): READY must reconnect.
        assert!(s
            .directives
            .iter()
            .any(|d| d.when == ready() && d.action == LifecycleAction::Reconnect));
    }

    #[test]
    fn derived_config_is_accepted_by_the_real_transport_and_engine() {
        let c = ws(plan_ok(&binding(Some(GUILD))));
        WsTransport::new(c, NoSecrets).expect("host validation accepts the derived config");
    }

    #[test]
    fn a_misconfigured_derivation_would_be_refused_by_the_transport() {
        // Guards the previous test against vacuity: the same constructor DOES
        // refuse a broken derivation.
        let mut c = ws(plan_ok(&binding(Some(GUILD))));
        c.session.captures.truncate(1);
        assert!(matches!(
            WsTransport::new(c, NoSecrets),
            Err(TransportError::Connect(_))
        ));
    }

    #[test]
    fn host_constants_flow_from_the_runtime_env() {
        let mut e = env();
        e.egress_allow = vec!["127.0.0.1".parse::<IpAddr>().unwrap()];
        e.max_frame_bytes = 4096;
        let d = ready_profile().plan(&binding(Some(GUILD)), &e).unwrap();
        let c = ws(d.into_iter().next().unwrap());
        assert_eq!(c.egress_allow, vec!["127.0.0.1".parse::<IpAddr>().unwrap()]);
        assert_eq!(c.max_frame_bytes, 4096);
        assert!(!c.allow_loopback_insecure);
    }

    #[test]
    fn hostile_guild_ids_are_rejected_without_echo() {
        let max = "9".repeat(20);
        let too_long = "9".repeat(21);
        let too_short = "9".repeat(16);
        let cases: Vec<(String, bool)> = vec![
            (GUILD.into(), true),
            (max, true),
            ("9".repeat(17), true),
            (too_short, false),
            (too_long, false),
            ("12345678901234567x".into(), false),
            ("-12345678901234567".into(), false),
            ("+12345678901234567".into(), false),
            ("123456789012345678\n".into(), false),
            ("123456789012345678\r\nPING".into(), false),
            (" 123456789012345678".into(), false),
            ("１２３４５６７８９０１２３４５６７８".into(), false),
            ("1234567890123456\u{0663}8".into(), false),
            (String::new(), false),
            ("{{DISCORD_BOT_TOKEN}}".into(), false),
        ];
        let mut examined = 0;
        for (g, ok) in cases {
            examined += 1;
            let r = ready_profile().plan(&binding(Some(&g)), &env());
            assert_eq!(r.is_ok(), ok, "guild {g:?}");
            if !ok {
                let err = r.err().unwrap();
                assert_eq!(err, PlanError::InvalidValue("guild_id"));
                let shown = format!("{err}{err:?}");
                assert!(
                    g.is_empty() || !shown.contains(g.trim()),
                    "error echoed the value"
                );
            }
        }
        assert_eq!(examined, 15, "non-zero denominator");
    }

    #[test]
    fn missing_guild_id_wrong_provider_and_wrong_kind_are_typed() {
        assert_eq!(
            ready_profile().plan(&binding(None), &env()).err(),
            Some(PlanError::MissingKey("guild_id"))
        );
        let mut b = binding(Some(GUILD));
        b.provider = "twitch".into();
        assert_eq!(
            ready_profile().plan(&b, &env()).err(),
            Some(PlanError::WrongProvider)
        );
        for kind in [
            ConnectionKind::ExternalHttp,
            ConnectionKind::AwsLambda,
            ConnectionKind::GcpFunction,
        ] {
            let mut b = binding(Some(GUILD));
            b.kind = kind;
            assert_eq!(
                ready_profile().plan(&b, &env()).err(),
                Some(PlanError::WrongKind)
            );
        }
    }

    /// Binding data (any extra metadata key, however hostile) can change
    /// neither the URL, the slot prefixes, the allow-list nor the rules.
    #[test]
    fn binding_data_cannot_change_wire_constants() {
        let baseline = ws(plan_ok(&binding(Some(GUILD))));
        let mut b = binding(Some(GUILD));
        for (k, v) in [
            ("url", "wss://evil.example/?v=10"),
            ("gateway_url", "wss://evil.example"),
            ("slot_prefixes", "x"),
            ("allowed_hosts", "*.evil.example"),
            ("resume_gateway_url", "wss://evil.example"),
            ("reconnect_url", "wss://evil.example"),
            ("session", "none"),
            ("handles", "pwn"),
            ("intents", "32767"),
            ("shard", "[0,2]"),
            ("bot_user_id", "999999999999999999"),
            ("managed_roles", "a:1"),
        ] {
            b.metadata.insert(k.to_string(), v.to_string());
        }
        let hostile = ws(plan_ok(&b));
        assert_eq!(hostile.url, baseline.url);
        assert_eq!(hostile.slot_prefixes, baseline.slot_prefixes);
        assert_eq!(hostile.session, baseline.session);
        assert_eq!(
            hostile
                .session
                .reconnect_url
                .as_ref()
                .unwrap()
                .allowed_hosts,
            vec!["*.discord.gg".to_string()]
        );
        assert_eq!(hostile.handles, baseline.handles);
        assert_eq!(hostile.source_key, baseline.source_key);
    }

    #[test]
    fn source_key_identifies_the_guild_not_the_community() {
        let a = ws(plan_ok(&binding(Some(GUILD))));
        let mut other_community = binding(Some(GUILD));
        other_community.community_id = 99;
        other_community.connection_id = 100;
        let b = ws(plan_ok(&other_community));
        assert_eq!(
            a.source_key, b.source_key,
            "two communities claiming one guild collide (feed fails closed)"
        );
        let c = ws(plan_ok(&binding(Some("223456789012345678"))));
        assert_ne!(a.source_key, c.source_key);
    }

    #[test]
    fn fingerprint_is_stable_and_tracks_every_input() {
        let base = plan_ok(&binding(Some(GUILD))).fingerprint;
        assert_eq!(plan_ok(&binding(Some(GUILD))).fingerprint, base, "stable");
        assert_ne!(base, 0);
        // guild
        assert_ne!(
            plan_ok(&binding(Some("223456789012345678"))).fingerprint,
            base
        );
        // connection (credential row) and community
        let mut b = binding(Some(GUILD));
        b.connection_id = 12;
        assert_ne!(plan_ok(&b).fingerprint, base);
        let mut b = binding(Some(GUILD));
        b.community_id = 8;
        assert_ne!(plan_ok(&b).fingerprint, base);
        // env-derived limits
        let mut e = env();
        e.max_frame_bytes = 1;
        let d = ready_profile().plan(&binding(Some(GUILD)), &e).unwrap();
        assert_ne!(d[0].fingerprint, base);
        // unrelated metadata the profile ignores must not restart the connection
        let mut b = binding(Some(GUILD));
        b.metadata.insert("managed_roles".into(), "mod:1".into());
        b.metadata.insert("bot_username".into(), "Waddles".into());
        assert_eq!(plan_ok(&b).fingerprint, base);
    }

    #[test]
    fn identify_budget_gate_blocks_discord_until_ready() {
        // The production constructor follows the constant, which is false.
        let budget_ready = std::hint::black_box(IDENTIFY_BUDGET_READY);
        assert!(!budget_ready, "flipped only by the budget PR");
        let p = DiscordProfile::new();
        assert_eq!(
            p.plan(&binding(Some(GUILD)), &env()).err(),
            Some(PlanError::IdentifyBudgetNotReady)
        );
        assert!(DiscordProfile::default()
            .plan(&binding(Some(GUILD)), &env())
            .is_err());
        assert!(DiscordProfile::new()
            .with_budget_ready(true)
            .plan(&binding(Some(GUILD)), &env())
            .is_ok());
        assert!(DiscordProfile::new()
            .with_budget_ready(false)
            .plan(&binding(Some(GUILD)), &env())
            .is_err());
    }

    #[test]
    fn heartbeat_disabled_fails_loud() {
        let mut e = env();
        e.heartbeat_enabled = false;
        assert_eq!(
            ready_profile().plan(&binding(Some(GUILD)), &e).err(),
            Some(PlanError::Config(HEARTBEAT_REQUIRED))
        );
    }

    /// Resolver double returning one Discord OAuth-kind row (the bot token).
    struct OneRow;

    impl connection_resolver::ConnectionResolve for OneRow {
        async fn resolve(
            &self,
            r: ConnectionRef,
        ) -> Result<connection_resolver::ResolvedConnection, connection_resolver::ResolveError>
        {
            Ok(connection_resolver::ResolvedConnection {
                connection_id: r.connection_id,
                community_id: r.community_id,
                provider: "discord".into(),
                kind: ConnectionKind::PlatformOauth,
                endpoint: connection_resolver::Endpoint::default(),
                secret: connection_resolver::ConnectionSecret::OAuthAccessToken {
                    access_token: connection_resolver::SecretString::new("bot-tok".into()),
                },
                expires_at: None,
                metadata: BTreeMap::new(),
            })
        }

        async fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            _a: i64,
            _l: u32,
        ) -> Result<connection_resolver::BindingPage, connection_resolver::ResolveError> {
            Err(connection_resolver::ResolveError::Malformed("unused"))
        }
    }

    /// Only the bot-token handle exists and only the exact token slot may carry
    /// a secret; through the REAL `TransportSecrets` the token resolves solely
    /// for the gateway destination and the owning community.
    #[tokio::test]
    async fn only_the_bot_token_handle_resolves_through_the_real_secret_source() {
        let c = ws(plan_ok(&binding(Some(GUILD))));
        assert_eq!(c.handles.keys().collect::<Vec<_>>(), vec!["discord-bot"]);
        assert_eq!(c.slot_prefixes, vec!["\"token\":\"".to_string()]);
        let conn = c.handles["discord-bot"];
        let secrets = crate::connector::TransportSecrets::new(std::sync::Arc::new(OneRow), 7)
            .unwrap()
            .with_expected_provider("discord");
        let gw = Destination::from_url(&reqwest::Url::parse(GATEWAY_URL).unwrap()).unwrap();
        let ok = secrets.resolve(conn, gw).await.unwrap();
        assert_eq!(ok.secret.expose(), "bot-tok");
        let evil =
            Destination::from_url(&reqwest::Url::parse("wss://evil.example/").unwrap()).unwrap();
        assert!(
            secrets.resolve(conn, evil).await.is_err(),
            "wrong destination"
        );
        let other = ConnectionRef {
            community_id: 8,
            connection_id: 11,
        };
        let gw = Destination::from_url(&reqwest::Url::parse(GATEWAY_URL).unwrap()).unwrap();
        assert!(secrets.resolve(other, gw).await.is_err(), "other community");
    }

    /// The tenant is the feed's, never read from (editable) binding metadata.
    #[test]
    fn tenant_never_comes_from_binding_metadata() {
        let mut b = binding(Some(GUILD));
        for k in ["tenant_id", "tenantId", "tenant", "scope"] {
            b.metadata.insert(k.into(), "424242".into());
        }
        let d = plan_ok(&b);
        assert_eq!(d.tenant_id, 1);
        assert_eq!(d.fingerprint, plan_ok(&binding(Some(GUILD))).fingerprint);
    }

    /// A rotation (secret_version bump) or a tenant change restarts the
    /// connection; a non-positive feed tenant/version is refused.
    #[test]
    fn rotation_and_tenant_move_the_fingerprint_and_bad_feed_values_are_refused() {
        let base = plan_ok(&binding(Some(GUILD))).fingerprint;
        let mut rotated = binding(Some(GUILD));
        rotated.secret_version = 2;
        assert_ne!(plan_ok(&rotated).fingerprint, base);
        let mut moved = binding(Some(GUILD));
        moved.tenant_id = 9;
        assert_ne!(plan_ok(&moved).fingerprint, base);
        for bad in [0_i64, -1] {
            let mut b = binding(Some(GUILD));
            b.tenant_id = bad;
            assert_eq!(
                ready_profile().plan(&b, &env()).err(),
                Some(PlanError::Config("binding tenant is not positive"))
            );
            let mut b = binding(Some(GUILD));
            b.secret_version = bad;
            assert_eq!(
                ready_profile().plan(&b, &env()).err(),
                Some(PlanError::Config("binding secret version is not positive"))
            );
        }
    }

    #[test]
    fn guild_id_validator_boundaries() {
        for n in 0..=25usize {
            let s = "7".repeat(n);
            assert_eq!(is_guild_id(&s), (17..=20).contains(&n), "len {n}");
        }
    }
}
