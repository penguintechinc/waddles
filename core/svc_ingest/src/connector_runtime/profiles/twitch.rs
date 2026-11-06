//! Twitch IRC [`OverlayProfile`] (spec 1.1/1.2, PR-F1).
//!
//! Derives one [`WsConfig`] per hub-api Twitch binding. EVERY security-relevant
//! value (URL, slot prefixes, auto-replies, limits) is a host constant defined
//! here; binding metadata contributes only the bot login, the channel login,
//! the numeric tenant and an optional secret version, each validated strictly.
//! Twitch EventSub is out of scope (svc-gateway owns it): this profile never
//! yields a webhook plan.
//!
//! Logins are usernames (PII): they live only inside the derived config and are
//! never logged or put in an error; logs use [`channel_fingerprint`].

use std::collections::{BTreeMap, HashMap};
use std::time::Duration;

use connection_resolver::{BindingSummary, ConnectionKind, ConnectionRef};
use sha2::{Digest, Sha256};

use crate::connector::transport_common::FrameRules;
use crate::connector::{AutoReplyRule, SelfEchoFilter, SessionRules, WsConfig};
use crate::connector_runtime::feed::{ConnKey, DesiredConnection, TransportPlan};
use crate::connector_runtime::plan::{OverlayProfile, PlanError, RuntimeEnv};

/// Platform slug.
pub const PLATFORM: &str = "twitch";
/// Connector bundle `app_id` (host constant, as in `outbound_boot::app_id`).
pub const APP_ID: &str = "waddles.core.service.twitch";
/// Provider name as hub-api's binding list knows it.
pub const PROVIDER: &str = "twitch";
/// Capability the binding list is scoped to.
pub const CAPABILITY: &str = "chat_ingest";
/// Twitch IRC over WebSocket. A host constant: never from binding/guest data.
pub const IRC_URL: &str = "wss://irc-ws.chat.twitch.tv:443";
/// Byte prefixes a credential/handle slot may directly follow in the handshake.
pub const SLOT_PREFIXES: [&str; 3] = ["PASS oauth:", "NICK ", "JOIN #"];
/// Binding metadata key: the channel login (adapter resource id).
pub const META_CHANNEL: &str = "channel";
/// Binding metadata key: the IRC nick the token authenticates as.
pub const META_BOT_LOGIN: &str = "bot_login";
/// Binding metadata key: the owning tenant (numeric).
pub const META_TENANT_ID: &str = "tenant_id";
/// Binding metadata key: opaque credential version (rotation marker).
pub const META_SECRET_VERSION: &str = "secret_version";
/// Handle names the overlay's handshake slots refer to.
pub const HANDLE_PASS: &str = "pass";
/// See [`HANDLE_PASS`].
pub const HANDLE_NICK: &str = "nick";
/// See [`HANDLE_PASS`].
pub const HANDLE_CHANNEL: &str = "channel";

/// Client PING cadence (overlay README): the PONG keeps the idle timer fed.
pub const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(120);
/// Longest quiet period: above Twitch's ~5 minute server PING cadence.
pub const IDLE_TIMEOUT: Duration = Duration::from_secs(330);
/// Connect and handshake-send timeout.
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// Hard connection lifetime, so the token is re-resolved at least this often.
pub const MAX_LIFETIME: Duration = Duration::from_secs(12 * 3600);
/// Longest accepted PING token echoed in a PONG.
const MAX_PING_TOKEN: usize = 64;
/// Twitch login length ceiling.
const MAX_LOGIN_LEN: usize = 25;
/// Longest accepted secret-version marker.
const MAX_VERSION_LEN: usize = 64;

/// The Twitch IRC chat-ingest profile.
#[derive(Debug, Clone, Copy, Default)]
pub struct TwitchProfile;

/// Lowercases ASCII, strips ONE leading `#`, and requires `^[a-z0-9_]{1,25}$`
/// with no trimming: whitespace, CR/LF, commas, non-ASCII and over-length
/// values are refused, never repaired. The value is never echoed in the error.
fn normalize_login(raw: &str, key: &'static str) -> Result<String, PlanError> {
    let lowered = raw.to_ascii_lowercase();
    let login = match lowered.strip_prefix('#') {
        Some(rest) => rest,
        None => lowered.as_str(),
    };
    let ok = !login.is_empty()
        && login.len() <= MAX_LOGIN_LEN
        && login
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
    if ok {
        Ok(login.to_owned())
    } else {
        Err(PlanError::InvalidValue(key))
    }
}

/// Fetches a required metadata value or names the missing key.
fn required<'a>(m: &'a BTreeMap<String, String>, key: &'static str) -> Result<&'a str, PlanError> {
    m.get(key)
        .map(String::as_str)
        .ok_or(PlanError::MissingKey(key))
}

/// Parses the strictly-positive numeric tenant id.
fn tenant_id(m: &BTreeMap<String, String>) -> Result<i64, PlanError> {
    let raw = required(m, META_TENANT_ID)?;
    match raw.parse::<i64>() {
        Ok(t) if t > 0 && raw.bytes().all(|b| b.is_ascii_digit()) => Ok(t),
        _ => Err(PlanError::InvalidValue(META_TENANT_ID)),
    }
}

/// The optional credential version marker (`[A-Za-z0-9._-]{1,64}`); absent is
/// represented explicitly (and hashed distinctly), never defaulted.
fn secret_version(m: &BTreeMap<String, String>) -> Result<Option<&str>, PlanError> {
    let Some(v) = m.get(META_SECRET_VERSION) else {
        return Ok(None);
    };
    let ok = !v.is_empty()
        && v.len() <= MAX_VERSION_LEN
        && v.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'));
    if ok {
        Ok(Some(v.as_str()))
    } else {
        Err(PlanError::InvalidValue(META_SECRET_VERSION))
    }
}

/// Short SHA-256 fingerprint of a channel login, safe to log (the login is PII).
#[must_use]
pub fn channel_fingerprint(channel: &str) -> String {
    Sha256::digest(channel.as_bytes())
        .iter()
        .take(6)
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// The host-owned PING -> PONG rule (the #996 mechanism): the transport answers
/// keepalives itself and the guest never sees them.
fn ping_pong() -> AutoReplyRule {
    AutoReplyRule {
        prefix: "PING :".into(),
        reply_prefix: "PONG :".into(),
        reply_suffix: "\r\n".into(),
        max_token_len: MAX_PING_TOKEN,
        forward_to_guest: false,
    }
}

/// Feeds a length-prefixed field into the fingerprint hash (no ambiguity
/// between adjacent fields).
fn feed(h: &mut Sha256, field: &[u8]) {
    h.update(field.len().to_be_bytes());
    h.update(field);
}

/// Hash of the derived config plus the secret VERSION (never a secret). Stable
/// across processes (SHA-256, not `DefaultHasher`) so a restart decision never
/// depends on the build.
fn fingerprint(cfg: &WsConfig, tenant: i64, version: Option<&str>) -> u64 {
    let mut h = Sha256::new();
    feed(&mut h, b"twitch-profile-v1");
    for s in [&cfg.platform, &cfg.url, &cfg.source_key] {
        feed(&mut h, s.as_bytes());
    }
    for p in &cfg.slot_prefixes {
        feed(&mut h, p.as_bytes());
    }
    for r in &cfg.auto_replies {
        for s in [&r.prefix, &r.reply_prefix, &r.reply_suffix] {
            feed(&mut h, s.as_bytes());
        }
        feed(&mut h, &r.max_token_len.to_be_bytes());
        feed(&mut h, &[u8::from(r.forward_to_guest)]);
    }
    if let Some(e) = &cfg.self_echo {
        for s in [&e.self_id, &e.sender_open, &e.sender_close, &e.command] {
            feed(&mut h, s.as_bytes());
        }
    }
    let mut handles: Vec<(&String, &ConnectionRef)> = cfg.handles.iter().collect();
    handles.sort_by(|a, b| a.0.cmp(b.0));
    for (name, r) in handles {
        feed(&mut h, name.as_bytes());
        feed(&mut h, &r.community_id.to_be_bytes());
        feed(&mut h, &r.connection_id.to_be_bytes());
    }
    for d in [cfg.connect_timeout, cfg.idle_timeout, cfg.max_lifetime] {
        feed(&mut h, &d.as_millis().to_be_bytes());
    }
    match cfg.heartbeat_interval {
        Some(d) => feed(&mut h, &d.as_millis().to_be_bytes()),
        None => feed(&mut h, b"-"),
    }
    feed(&mut h, &cfg.max_frame_bytes.to_be_bytes());
    feed(&mut h, &cfg.dedup_window.to_be_bytes());
    feed(&mut h, &[u8::from(cfg.allow_loopback_insecure)]);
    for ip in &cfg.egress_allow {
        feed(&mut h, ip.to_string().as_bytes());
    }
    feed(&mut h, &tenant.to_be_bytes());
    match version {
        Some(v) => {
            feed(&mut h, b"v");
            feed(&mut h, v.as_bytes());
        }
        None => feed(&mut h, b"-"),
    }
    let d = h.finalize();
    let mut b = [0u8; 8];
    b.copy_from_slice(&d[..8]);
    u64::from_be_bytes(b)
}

impl OverlayProfile for TwitchProfile {
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

    /// Derives the single IRC [`WsConfig`] for one binding. Fails closed with a
    /// value-free [`PlanError`] on any wrong/missing/invalid input.
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
        if b.community_id <= 0 {
            return Err(PlanError::InvalidValue("community_id"));
        }
        if b.connection_id <= 0 {
            return Err(PlanError::InvalidValue("connection_id"));
        }
        if env.max_frame_bytes == 0 {
            return Err(PlanError::Config("max_frame_bytes must be non-zero"));
        }
        let channel = normalize_login(required(&b.metadata, META_CHANNEL)?, META_CHANNEL)?;
        let bot_login = normalize_login(required(&b.metadata, META_BOT_LOGIN)?, META_BOT_LOGIN)?;
        let tenant = tenant_id(&b.metadata)?;
        let version = secret_version(&b.metadata)?;

        let conn = ConnectionRef {
            community_id: b.community_id,
            connection_id: b.connection_id,
        };
        let channel_hash = channel_fingerprint(&channel);
        let self_echo = SelfEchoFilter {
            self_id: bot_login,
            sender_open: ":".into(),
            sender_close: "!".into(),
            command: "PRIVMSG".into(),
        };
        let auto_replies = vec![ping_pong()];
        FrameRules::new(auto_replies.clone(), Some(self_echo.clone()))
            .map_err(|_| PlanError::Config("frame rules rejected"))?;

        let cfg = WsConfig {
            platform: PLATFORM.to_owned(),
            url: IRC_URL.to_owned(),
            // Identifies the upstream resource: the feed fails closed when two
            // communities claim the same Twitch channel.
            source_key: format!("{PLATFORM}:{channel}"),
            handles: HashMap::from([
                (HANDLE_PASS.to_owned(), conn),
                (HANDLE_NICK.to_owned(), conn),
                (HANDLE_CHANNEL.to_owned(), conn),
            ]),
            resume_token: None,
            connect_timeout: CONNECT_TIMEOUT,
            idle_timeout: IDLE_TIMEOUT,
            max_lifetime: MAX_LIFETIME,
            egress_allow: env.egress_allow.clone(),
            max_frame_bytes: env.max_frame_bytes,
            dedup_window: 0,
            allow_loopback_insecure: false,
            slot_prefixes: SLOT_PREFIXES.iter().map(|p| (*p).to_owned()).collect(),
            auto_replies,
            self_echo: Some(self_echo),
            heartbeat_interval: env.heartbeat_enabled.then_some(HEARTBEAT_INTERVAL),
            session: SessionRules::default(),
        };
        let fp = fingerprint(&cfg, tenant, version);
        tracing::debug!(
            community_id = b.community_id,
            connection_id = b.connection_id,
            channel_hash = %channel_hash,
            "planned twitch irc connection"
        );
        Ok(vec![DesiredConnection {
            key: ConnKey {
                platform: PLATFORM,
                community_id: b.community_id,
                connection_id: b.connection_id,
            },
            tenant_id: tenant,
            plan: TransportPlan::Ws(Box::new(cfg)),
            fingerprint: fp,
        }])
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use std::net::{IpAddr, Ipv4Addr};

    fn env() -> RuntimeEnv {
        RuntimeEnv {
            environment: "alpha".into(),
            max_frame_bytes: 65_536,
            egress_allow: vec![],
            heartbeat_enabled: true,
        }
    }

    fn binding(community: i64, conn: i64, channel: &str, bot: &str) -> BindingSummary {
        let mut metadata = BTreeMap::new();
        metadata.insert(META_CHANNEL.to_owned(), channel.to_owned());
        metadata.insert(META_BOT_LOGIN.to_owned(), bot.to_owned());
        metadata.insert(META_TENANT_ID.to_owned(), "42".to_owned());
        BindingSummary {
            community_id: community,
            connection_id: conn,
            provider: "twitch".to_owned(),
            kind: ConnectionKind::PlatformOauth,
            metadata,
        }
    }

    fn one(b: &BindingSummary, e: &RuntimeEnv) -> (DesiredConnection, WsConfig) {
        let mut v = TwitchProfile.plan(b, e).expect("plan ok");
        assert_eq!(v.len(), 1);
        let d = v.remove(0);
        let TransportPlan::Ws(cfg) = &d.plan else {
            panic!("twitch must plan a Ws transport");
        };
        let cfg = (**cfg).clone();
        (d, cfg)
    }

    #[test]
    fn identity_matches_the_legacy_discover() {
        let p = TwitchProfile;
        assert_eq!(
            (p.platform(), p.app_id(), p.provider(), p.capability()),
            (
                "twitch",
                "waddles.core.service.twitch",
                "twitch",
                "chat_ingest"
            )
        );
    }

    #[test]
    fn golden_derived_config() {
        let ip = IpAddr::V4(Ipv4Addr::new(10, 1, 2, 3));
        let mut e = env();
        e.egress_allow = vec![ip];
        let (d, cfg) = one(&binding(7, 9, "PenguinzPlays", "WaddleBots"), &e);
        assert_eq!(
            d.key,
            ConnKey {
                platform: "twitch",
                community_id: 7,
                connection_id: 9
            }
        );
        assert_eq!(d.tenant_id, 42);
        assert_eq!(cfg.platform, "twitch");
        assert_eq!(cfg.url, "wss://irc-ws.chat.twitch.tv:443");
        assert_eq!(cfg.source_key, "twitch:penguinzplays");
        assert_eq!(cfg.slot_prefixes, vec!["PASS oauth:", "NICK ", "JOIN #"]);
        assert_eq!(
            cfg.auto_replies,
            vec![AutoReplyRule {
                prefix: "PING :".into(),
                reply_prefix: "PONG :".into(),
                reply_suffix: "\r\n".into(),
                max_token_len: 64,
                forward_to_guest: false,
            }]
        );
        assert_eq!(
            cfg.self_echo,
            Some(SelfEchoFilter {
                self_id: "waddlebots".into(),
                sender_open: ":".into(),
                sender_close: "!".into(),
                command: "PRIVMSG".into(),
            })
        );
        let conn = ConnectionRef {
            community_id: 7,
            connection_id: 9,
        };
        assert_eq!(
            cfg.handles,
            HashMap::from([
                ("pass".to_owned(), conn),
                ("nick".to_owned(), conn),
                ("channel".to_owned(), conn)
            ])
        );
        assert_eq!(cfg.heartbeat_interval, Some(Duration::from_secs(120)));
        assert_eq!(cfg.idle_timeout, Duration::from_secs(330));
        assert_eq!(cfg.connect_timeout, Duration::from_secs(10));
        assert_eq!(cfg.max_lifetime, Duration::from_secs(12 * 3600));
        assert_eq!(cfg.max_frame_bytes, 65_536);
        assert_eq!(cfg.dedup_window, 0);
        assert_eq!(cfg.egress_allow, vec![ip]);
        assert!(!cfg.allow_loopback_insecure);
        assert!(cfg.resume_token.is_none());
    }

    #[test]
    fn heartbeat_follows_the_env_switch() {
        let mut e = env();
        e.heartbeat_enabled = false;
        let (_, cfg) = one(&binding(1, 2, "chan", "bot"), &e);
        assert_eq!(cfg.heartbeat_interval, None);
    }

    #[test]
    fn derived_config_builds_a_real_transport_config() {
        // The host constants must pass the transport's own validation.
        let (_, cfg) = one(&binding(1, 2, "chan", "bot"), &env());
        let secrets =
            crate::connector::TransportSecrets::new(std::sync::Arc::new(NoResolver), 1).unwrap();
        crate::connector::WsTransport::new(cfg, secrets).expect("derived config is valid");
    }

    struct NoResolver;

    impl connection_resolver::ConnectionResolve for NoResolver {
        async fn resolve(
            &self,
            _r: ConnectionRef,
        ) -> Result<connection_resolver::ResolvedConnection, connection_resolver::ResolveError>
        {
            Err(connection_resolver::ResolveError::NotFound)
        }
        async fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            _a: i64,
            _l: u32,
        ) -> Result<connection_resolver::BindingPage, connection_resolver::ResolveError> {
            Err(connection_resolver::ResolveError::NotFound)
        }
    }

    #[test]
    fn normalizes_hash_and_ascii_case() {
        let (_, cfg) = one(&binding(1, 2, "#MixedCase_9", "Bot_Nick"), &env());
        assert_eq!(cfg.source_key, "twitch:mixedcase_9");
        assert_eq!(cfg.self_echo.unwrap().self_id, "bot_nick");
    }

    #[test]
    fn hostile_logins_are_refused_for_both_keys() {
        let long = "x".repeat(26);
        let hostile = [
            "evil\r\nJOIN #other",
            "a b",
            "a\nb",
            "#a,#b",
            "a,b",
            "caf\u{e9}",
            "\u{0430}dmin",  // cyrillic a lookalike
            "\u{ff21}bc",    // fullwidth A
            "\u{212a}elvin", // Kelvin sign: lowercases to ASCII k under Unicode rules
            "",
            "#",
            "##a",
            " a",
            "a ",
            "a\r\n",
            "a\0",
            "a{{x}}",
            long.as_str(),
        ];
        for bad in hostile {
            for key in [META_CHANNEL, META_BOT_LOGIN] {
                let mut b = binding(1, 2, "chan", "bot");
                b.metadata.insert(key.to_owned(), bad.to_owned());
                assert_eq!(
                    TwitchProfile.plan(&b, &env()).err(),
                    Some(PlanError::InvalidValue(if key == META_CHANNEL {
                        META_CHANNEL
                    } else {
                        META_BOT_LOGIN
                    })),
                    "{key}: {bad:?}"
                );
            }
        }
    }

    #[test]
    fn max_length_login_is_accepted_and_one_more_is_not() {
        let ok = "a".repeat(25);
        assert!(TwitchProfile
            .plan(&binding(1, 2, &ok, "bot"), &env())
            .is_ok());
        let bad = "a".repeat(26);
        assert!(TwitchProfile
            .plan(&binding(1, 2, &bad, "bot"), &env())
            .is_err());
    }

    #[test]
    fn errors_never_carry_the_value() {
        let secret = "ZZ-secret-login!";
        let mut b = binding(1, 2, "chan", "bot");
        b.metadata
            .insert(META_CHANNEL.to_owned(), secret.to_owned());
        let e = TwitchProfile.plan(&b, &env()).err().unwrap();
        assert!(!format!("{e} {e:?}").contains("secret"));
    }

    #[test]
    fn wrong_provider_kind_and_ids_are_refused() {
        let mut b = binding(1, 2, "c", "b");
        b.provider = "discord".into();
        assert_eq!(
            TwitchProfile.plan(&b, &env()).err(),
            Some(PlanError::WrongProvider)
        );
        let mut b = binding(1, 2, "c", "b");
        b.kind = ConnectionKind::ExternalHttp;
        assert_eq!(
            TwitchProfile.plan(&b, &env()).err(),
            Some(PlanError::WrongKind)
        );
        assert_eq!(
            TwitchProfile.plan(&binding(0, 2, "c", "b"), &env()).err(),
            Some(PlanError::InvalidValue("community_id"))
        );
        assert_eq!(
            TwitchProfile.plan(&binding(1, -1, "c", "b"), &env()).err(),
            Some(PlanError::InvalidValue("connection_id"))
        );
    }

    #[test]
    fn missing_keys_are_named() {
        for key in [META_CHANNEL, META_BOT_LOGIN, META_TENANT_ID] {
            let mut b = binding(1, 2, "c", "b");
            b.metadata.remove(key);
            let want = match key {
                META_CHANNEL => META_CHANNEL,
                META_BOT_LOGIN => META_BOT_LOGIN,
                _ => META_TENANT_ID,
            };
            assert_eq!(
                TwitchProfile.plan(&b, &env()).err(),
                Some(PlanError::MissingKey(want))
            );
        }
    }

    #[test]
    fn tenant_must_be_a_positive_decimal() {
        for bad in ["0", "-3", "abc", "", "+5", "1 ", "99999999999999999999"] {
            let mut b = binding(1, 2, "c", "b");
            b.metadata.insert(META_TENANT_ID.into(), bad.into());
            assert_eq!(
                TwitchProfile.plan(&b, &env()).err(),
                Some(PlanError::InvalidValue(META_TENANT_ID)),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn zero_frame_cap_is_a_config_error() {
        let mut e = env();
        e.max_frame_bytes = 0;
        assert_eq!(
            TwitchProfile.plan(&binding(1, 2, "c", "b"), &e).err(),
            Some(PlanError::Config("max_frame_bytes must be non-zero"))
        );
    }

    #[test]
    fn secret_version_is_validated() {
        for bad in ["", "a b", "a\r\n", "v\u{e9}", &"v".repeat(65)] {
            let mut b = binding(1, 2, "c", "b");
            b.metadata.insert(META_SECRET_VERSION.into(), bad.into());
            assert_eq!(
                TwitchProfile.plan(&b, &env()).err(),
                Some(PlanError::InvalidValue(META_SECRET_VERSION)),
                "{bad:?}"
            );
        }
        let mut b = binding(1, 2, "c", "b");
        b.metadata
            .insert(META_SECRET_VERSION.into(), "v1.2-3_x".into());
        assert!(TwitchProfile.plan(&b, &env()).is_ok());
    }

    #[test]
    fn binding_data_cannot_change_the_host_constants() {
        let mut b = binding(1, 2, "chan", "bot");
        for (k, v) in [
            ("url", "wss://evil.example:443"),
            ("slot_prefixes", "ANYTHING "),
            ("auto_replies", "x"),
            ("host", "evil.example"),
            ("endpoint", "ws://127.0.0.1:1"),
            ("allow_loopback_insecure", "true"),
            ("egress_allow", "127.0.0.1"),
            ("heartbeat_interval", "1"),
            ("max_frame_bytes", "999999999"),
        ] {
            b.metadata.insert(k.into(), v.into());
        }
        let (_, with_noise) = one(&b, &env());
        let (_, clean) = one(&binding(1, 2, "chan", "bot"), &env());
        assert_eq!(with_noise.url, IRC_URL);
        assert_eq!(with_noise.slot_prefixes, clean.slot_prefixes);
        assert_eq!(with_noise.auto_replies, clean.auto_replies);
        assert_eq!(with_noise.egress_allow, clean.egress_allow);
        assert_eq!(with_noise.max_frame_bytes, clean.max_frame_bytes);
        assert_eq!(with_noise.heartbeat_interval, clean.heartbeat_interval);
        assert!(!with_noise.allow_loopback_insecure);
    }

    #[test]
    fn fingerprint_tracks_config_and_secret_version_only() {
        let base = binding(1, 2, "chan", "bot");
        let fp = |b: &BindingSummary, e: &RuntimeEnv| one(b, e).0.fingerprint;
        let f0 = fp(&base, &env());
        // Deterministic, and unrelated metadata does not move it.
        assert_eq!(f0, fp(&base, &env()));
        let mut noise = base.clone();
        noise
            .metadata
            .insert("display_name".into(), "Whatever".into());
        assert_eq!(f0, fp(&noise, &env()));
        // Normalization-equivalent spellings are the same config.
        assert_eq!(f0, fp(&binding(1, 2, "#CHAN", "BOT"), &env()));

        let mut versions = std::collections::HashSet::new();
        versions.insert(f0);
        let mut v1 = base.clone();
        v1.metadata.insert(META_SECRET_VERSION.into(), "1".into());
        let mut v2 = base.clone();
        v2.metadata.insert(META_SECRET_VERSION.into(), "2".into());
        assert!(
            versions.insert(fp(&v1, &env())),
            "version present changes it"
        );
        assert!(versions.insert(fp(&v2, &env())), "version bump changes it");
        assert_eq!(fp(&v1, &env()), fp(&v1.clone(), &env()));

        let mut tenant = base.clone();
        tenant.metadata.insert(META_TENANT_ID.into(), "43".into());
        assert!(versions.insert(fp(&tenant, &env())));
        assert!(versions.insert(fp(&binding(1, 2, "other", "bot"), &env())));
        assert!(versions.insert(fp(&binding(1, 2, "chan", "bot2"), &env())));
        assert!(versions.insert(fp(&binding(1, 3, "chan", "bot"), &env())));
        assert!(versions.insert(fp(&binding(5, 2, "chan", "bot"), &env())));
        let mut e = env();
        e.heartbeat_enabled = false;
        assert!(versions.insert(fp(&base, &e)));
        let mut e = env();
        e.max_frame_bytes = 1024;
        assert!(versions.insert(fp(&base, &e)));
        let mut e = env();
        e.egress_allow = vec![IpAddr::V4(Ipv4Addr::LOCALHOST)];
        assert!(versions.insert(fp(&base, &e)));
        assert_eq!(versions.len(), 11);
    }

    #[test]
    fn fingerprint_never_contains_the_secret_version_text() {
        // The version influences the hash only; it is not recoverable as text.
        let mut b = binding(1, 2, "c", "b");
        b.metadata
            .insert(META_SECRET_VERSION.into(), "hunter2".into());
        let fp = one(&b, &env()).0.fingerprint;
        assert!(!format!("{fp:x}").contains("hunter2"));
    }

    #[test]
    fn n_bindings_in_n_plans_or_counted_skips() {
        let mut bindings = Vec::new();
        for i in 0..6_i64 {
            bindings.push(binding(i + 1, i + 100, &format!("chan{i}"), "bot"));
        }
        bindings.push(binding(1, 200, "bad chan", "bot"));
        bindings.push(binding(1, 201, "ok", "bad bot"));
        let mut other = binding(1, 202, "ok", "bot");
        other.provider = "youtube".into();
        bindings.push(other);
        let (mut planned, mut skipped) = (0usize, 0usize);
        for b in &bindings {
            match TwitchProfile.plan(b, &env()) {
                Ok(v) => planned += v.len(),
                Err(_) => skipped += 1,
            }
        }
        assert_eq!(bindings.len(), 9);
        assert_eq!((planned, skipped), (6, 3));
        assert_eq!(planned + skipped, bindings.len());
    }

    #[test]
    fn two_communities_on_one_channel_share_a_source_key() {
        // The feed relies on this to fail closed on a contested channel.
        let (_, a) = one(&binding(1, 10, "shared", "bota"), &env());
        let (_, b) = one(&binding(2, 20, "shared", "botb"), &env());
        assert_eq!(a.source_key, b.source_key);
        let (_, c) = one(&binding(2, 21, "different", "botb"), &env());
        assert_ne!(a.source_key, c.source_key);
    }

    #[test]
    fn channel_fingerprint_is_short_stable_and_not_the_login() {
        let f = channel_fingerprint("somechannel");
        assert_eq!(f.len(), 12);
        assert_eq!(f, channel_fingerprint("somechannel"));
        assert_ne!(f, channel_fingerprint("otherchannel"));
        assert!(!f.contains("somechannel"));
    }

    #[test]
    fn plan_never_yields_a_webhook_or_poll_transport() {
        let (d, _) = one(&binding(1, 2, "c", "b"), &env());
        assert!(matches!(d.plan, TransportPlan::Ws(_)));
    }
}
