//! Kick chat [`ConnectorProfile`]: Pusher over WebSocket.
//!
//! Kick's chat backend is a Pusher application, so the Kick "transport" is the generic
//! [`WsTransport`](crate::connector::WsTransport) driven by the [`WsConfig`] this profile
//! derives -- no platform code in the socket layer. EVERY security-relevant value (URL, slot
//! prefix, limits, lifecycle) is a host constant defined here; binding metadata contributes only
//! the numeric chatroom id and an optional tenant claim that is only ever cross-checked.
//!
//! The Pusher application key in [`PUSHER_URL`] is Kick's own PUBLIC key (every Kick chat viewer's
//! browser connects with it; it identifies the Pusher app, not a caller), the same status the
//! legacy `receivers/kick_pusher.py::DEFAULT_PUSHER_KEY` documents. No credential is ever
//! sent on this socket; the single handshake slot is the non-secret channel name
//! `chatrooms.<id>.v2`, answered from the validated chatroom id by [`KickFieldSecrets`].
//!
//! Webhook events (follow / subscription / livestream status) are NOT planned here: their
//! signature scheme lives in `connector::kick_webhook` and is registered on a webhook route by
//! the webhook seam (see the PR body). This profile never yields a webhook plan.
//!
//! The chatroom id is a numeric platform id (not PII); it is nevertheless only hashed into
//! `source_id` and never logged.

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::time::Duration;

use connection_resolver::{BindingSummary, ConnectionKind, ConnectionRef, SecretString};
use sha2::{Digest, Sha256};

use super::twitch::TenantSource;
use crate::connector::secrets::{Destination, ResolvedAuth};
use crate::connector::webhook::{BoxFut, SecretError};
use crate::connector::{HandshakeAfter, SessionRules, TransportSecretSource, WsConfig};
use crate::connector_runtime::feed::{ConnKey, DesiredConnection, TransportPlan};
use crate::connector_runtime::plan::{ConnectorProfile, PlanError, RuntimeEnv};

/// Platform slug.
pub const PLATFORM: &str = "kick";
/// Connector bundle `app_id` (host constant).
pub const APP_ID: &str = "waddles.core.service.kick";
/// Provider name as hub-api's binding list knows it.
pub const PROVIDER: &str = "kick";
/// Capability the binding list is scoped to.
pub const CAPABILITY: &str = "chat_ingest";
/// Kick's public Pusher endpoint (host constant; never from binding/guest data).
pub const PUSHER_URL: &str = "wss://ws-us2.pusher.com/app/eb1d5f283081a78b932c?protocol=7&client=js&version=7.6.0&flash=false";
/// Handle the connector's subscribe slot names.
pub const HANDLE_CHANNEL: &str = "channel";
/// Host-pinned bytes the subscribe frame must carry directly before the slot; mirrors the
/// bundle's `pusher::REQUIRED_SLOT_PREFIX` (pinned equal by a test in `connector_e2e`).
pub const SLOT_PREFIX: &str = r#"{"event":"pusher:subscribe","data":{"auth":"","channel":""#;
/// Binding metadata key: the numeric Kick chatroom id.
pub const META_CHATROOM_ID: &str = "chatroom_id";
/// Binding metadata key: an OPTIONAL tenant claim, cross-checked only.
pub const META_TENANT_ID: &str = "tenant_id";
/// Synthetic `connection_id` of the non-secret `channel` handle. Never a real row (real ids are
/// positive) and never sent to the resolver: only [`KickFieldSecrets`] answers it.
pub const CHANNEL_CONN_ID: i64 = -2;

/// Client `pusher:ping` cadence: below Pusher's activity timeout so the server never has to ping
/// (the host cannot answer a server ping; see the bundle README).
pub const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(25);
/// Longest quiet period: a few missed pong rounds.
pub const IDLE_TIMEOUT: Duration = Duration::from_secs(90);
/// Connect and handshake-send timeout.
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// Bounded wait for `pusher:connection_established` before subscribing.
pub const FIRST_FRAME_TIMEOUT: Duration = Duration::from_secs(10);
/// Hard connection lifetime.
pub const MAX_LIFETIME: Duration = Duration::from_secs(12 * 3600);
/// Chatroom ids are 64-bit unsigned decimals: at most 20 digits.
const MAX_CHATROOM_DIGITS: usize = 20;

/// The Kick Pusher chat-ingest profile.
#[derive(Clone, Default)]
pub struct KickProfile {
    tenants: Option<Arc<dyn TenantSource>>,
}

impl KickProfile {
    /// Builds the profile with a host community -> tenant cross-check: a feed tenant that
    /// disagrees (or an unknown community) refuses the binding.
    #[must_use]
    pub fn new(tenants: Arc<dyn TenantSource>) -> Self {
        Self {
            tenants: Some(tenants),
        }
    }

    /// Builds the profile trusting only the feed's server-derived tenant (the runtime's
    /// `TenantResolve` still cross-checks it before a connection starts).
    #[must_use]
    pub fn from_feed() -> Self {
        Self { tenants: None }
    }
}

/// A numeric chatroom id: 1..=20 ASCII digits, no sign/space/leading zero-only (`0` refused).
fn chatroom_id(raw: &str) -> Result<&str, PlanError> {
    let ok = !raw.is_empty()
        && raw.len() <= MAX_CHATROOM_DIGITS
        && raw.bytes().all(|b| b.is_ascii_digit())
        && raw.bytes().any(|b| b != b'0');
    if ok {
        Ok(raw)
    } else {
        Err(PlanError::InvalidValue(META_CHATROOM_ID))
    }
}

fn required<'a>(m: &'a BTreeMap<String, String>, key: &'static str) -> Result<&'a str, PlanError> {
    m.get(key)
        .map(String::as_str)
        .ok_or(PlanError::MissingKey(key))
}

/// The feed's server-derived tenant (positive), cross-checked against the host mapping and any
/// editable `tenant_id` metadata claim (which must AGREE or the binding is refused).
fn resolve_tenant(
    tenants: Option<&dyn TenantSource>,
    b: &BindingSummary,
) -> Result<i64, PlanError> {
    let tenant = Some(b.tenant_id)
        .filter(|t| *t > 0)
        .ok_or(PlanError::Config("tenant for community is unresolved"))?;
    if let Some(source) = tenants {
        if source.tenant_of(b.community_id) != Some(tenant) {
            return Err(PlanError::Config("tenant disagrees with the host mapping"));
        }
    }
    if let Some(raw) = b.metadata.get(META_TENANT_ID) {
        let claimed = raw
            .bytes()
            .all(|c| c.is_ascii_digit())
            .then(|| raw.parse::<i64>().ok())
            .flatten();
        if claimed != Some(tenant) {
            return Err(PlanError::InvalidValue(META_TENANT_ID));
        }
    }
    Ok(tenant)
}

/// Stable fingerprint of the derived config (WsConfig holds no secret, so its `Debug` form is
/// safe to hash) plus the tenant and the credential-rotation counter.
fn fingerprint(cfg: &WsConfig, tenant: i64, version: i64) -> u64 {
    let mut h = Sha256::new();
    h.update(b"kick-profile-v1\0");
    h.update(format!("{cfg:?}").as_bytes());
    h.update(tenant.to_be_bytes());
    h.update(version.to_be_bytes());
    let d = h.finalize();
    let mut b = [0u8; 8];
    b.copy_from_slice(&d[..8]);
    u64::from_be_bytes(b)
}

/// The complete, constant Pusher session lifecycle: wait for `pusher:connection_established`
/// before sending the subscribe.
fn session_rules() -> SessionRules {
    SessionRules {
        handshake_after: HandshakeAfter::FirstFrame {
            timeout: FIRST_FRAME_TIMEOUT,
        },
        ..SessionRules::default()
    }
}

/// Answers the `channel` handle with the validated Pusher channel name. Holds no credential
/// and never delegates: any other ref is refused.
pub struct KickFieldSecrets {
    channel: ConnectionRef,
    channel_name: String,
}

impl KickFieldSecrets {
    /// Builds the source from a config this profile derived, re-validating the value it will
    /// release.
    ///
    /// # Errors
    /// [`PlanError::Config`] when the handle ref is not the canonical synthetic one,
    /// [`PlanError::InvalidValue`] when the source key does not carry a valid chatroom id.
    pub fn from_config(cfg: &WsConfig) -> Result<Self, PlanError> {
        let channel = cfg
            .handles
            .get(HANDLE_CHANNEL)
            .copied()
            .ok_or(PlanError::Config("kick handle missing"))?;
        if channel.connection_id != CHANNEL_CONN_ID || cfg.handles.len() != 1 {
            return Err(PlanError::Config("kick handle refs are not canonical"));
        }
        let id = cfg
            .source_key
            .strip_prefix("kick:")
            .ok_or(PlanError::InvalidValue(META_CHATROOM_ID))
            .and_then(chatroom_id)?;
        Ok(Self {
            channel,
            channel_name: format!("chatrooms.{id}.v2"),
        })
    }
}

impl TransportSecretSource for KickFieldSecrets {
    fn resolve(
        &self,
        requested: ConnectionRef,
        _dest: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        let value = (requested == self.channel).then(|| self.channel_name.clone());
        Box::pin(async move {
            match value {
                Some(v) => Ok(ResolvedAuth {
                    secret: SecretString::new(v),
                    header: None,
                }),
                None => Err(SecretError::Missing),
            }
        })
    }
}

impl ConnectorProfile for KickProfile {
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

    /// Derives the single Pusher [`WsConfig`] for one binding. Fails closed with a value-free
    /// [`PlanError`] on any wrong/missing/invalid input.
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
        let room = chatroom_id(required(&b.metadata, META_CHATROOM_ID)?)?;
        let tenant = resolve_tenant(self.tenants.as_deref(), b)?;
        let version = Some(b.secret_version)
            .filter(|v| *v >= 1)
            .ok_or(PlanError::Config("secret version must be at least 1"))?;

        let cfg = WsConfig {
            platform: PLATFORM.to_owned(),
            url: PUSHER_URL.to_owned(),
            // Identifies the upstream resource: the feed fails closed when two communities
            // claim the same chatroom.
            source_key: format!("{PLATFORM}:{room}"),
            handles: HashMap::from([(
                HANDLE_CHANNEL.to_owned(),
                ConnectionRef {
                    community_id: b.community_id,
                    connection_id: CHANNEL_CONN_ID,
                },
            )]),
            resume_token: None,
            connect_timeout: CONNECT_TIMEOUT,
            idle_timeout: IDLE_TIMEOUT,
            max_lifetime: MAX_LIFETIME,
            egress_allow: env.egress_allow.clone(),
            max_frame_bytes: env.max_frame_bytes,
            dedup_window: 0,
            allow_loopback_insecure: false,
            slot_prefixes: HashMap::from([(
                HANDLE_CHANNEL.to_owned(),
                vec![SLOT_PREFIX.to_owned()],
            )]),
            auto_replies: Vec::new(),
            self_echo: None,
            heartbeat_interval: env.heartbeat_enabled.then_some(HEARTBEAT_INTERVAL),
            session: session_rules(),
        };
        let fp = fingerprint(&cfg, tenant, version);
        tracing::debug!(
            community_id = b.community_id,
            connection_id = b.connection_id,
            "planned kick pusher connection"
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

    fn env() -> RuntimeEnv {
        RuntimeEnv {
            environment: "alpha".into(),
            max_frame_bytes: 65_536,
            egress_allow: vec![],
            heartbeat_enabled: true,
        }
    }

    fn binding(community: i64, conn: i64, room: &str) -> BindingSummary {
        let mut metadata = BTreeMap::new();
        metadata.insert(META_CHATROOM_ID.to_owned(), room.to_owned());
        metadata.insert(META_TENANT_ID.to_owned(), "42".to_owned());
        BindingSummary {
            tenant_id: 42,
            secret_version: 1,
            community_id: community,
            connection_id: conn,
            provider: "kick".to_owned(),
            kind: ConnectionKind::PlatformOauth,
            metadata,
        }
    }

    fn profile() -> KickProfile {
        KickProfile::new(Arc::new(|_c: i64| Some(42)))
    }

    fn one(b: &BindingSummary) -> (DesiredConnection, WsConfig) {
        let mut v = profile().plan(b, &env()).expect("plan ok");
        assert_eq!(v.len(), 1);
        let d = v.remove(0);
        let TransportPlan::Ws(cfg) = &d.plan else {
            panic!("kick plans a Ws transport");
        };
        let cfg = (**cfg).clone();
        (d, cfg)
    }

    #[test]
    fn plan_derives_only_host_constants_plus_the_chatroom() {
        let (d, cfg) = one(&binding(7, 9, "12345"));
        assert_eq!(cfg.platform, "kick");
        assert_eq!(cfg.url, PUSHER_URL);
        assert!(cfg.url.starts_with("wss://ws-us2.pusher.com/app/"));
        assert_eq!(cfg.source_key, "kick:12345");
        assert_eq!(d.tenant_id, 42);
        assert_eq!(d.key.connection_id, 9);
        assert_eq!(cfg.heartbeat_interval, Some(HEARTBEAT_INTERVAL));
        assert!(!cfg.allow_loopback_insecure && cfg.auto_replies.is_empty());
        assert_eq!(cfg.slot_prefixes[HANDLE_CHANNEL], [SLOT_PREFIX.to_owned()]);
        assert_eq!(cfg.handles.len(), 1);
        assert_eq!(cfg.handles[HANDLE_CHANNEL].connection_id, CHANNEL_CONN_ID);
        assert_eq!(
            cfg.session.handshake_after,
            HandshakeAfter::FirstFrame {
                timeout: FIRST_FRAME_TIMEOUT
            }
        );
    }

    #[test]
    fn heartbeat_follows_the_runtime_baseline() {
        let mut e = env();
        e.heartbeat_enabled = false;
        let v = profile().plan(&binding(7, 9, "1"), &e).unwrap();
        let TransportPlan::Ws(cfg) = &v[0].plan else {
            panic!()
        };
        assert_eq!(cfg.heartbeat_interval, None);
    }

    #[test]
    fn plan_refuses_every_bad_input_without_echoing_values() {
        for room in ["", "0", "000", "-1", "1 2", "12a", "１２", &"9".repeat(21)] {
            let e = profile().plan(&binding(7, 9, room), &env()).unwrap_err();
            assert_eq!(e, PlanError::InvalidValue(META_CHATROOM_ID), "{room:?}");
            assert!(!e.to_string().contains(room) || room.is_empty());
        }
        let mut b = binding(7, 9, "1");
        b.metadata.remove(META_CHATROOM_ID);
        assert_eq!(
            profile().plan(&b, &env()).unwrap_err(),
            PlanError::MissingKey(META_CHATROOM_ID)
        );
        let mut b = binding(7, 9, "1");
        b.provider = "twitch".into();
        assert_eq!(
            profile().plan(&b, &env()).unwrap_err(),
            PlanError::WrongProvider
        );
        let mut b = binding(7, 9, "1");
        b.kind = ConnectionKind::ExternalHttp;
        assert_eq!(
            profile().plan(&b, &env()).unwrap_err(),
            PlanError::WrongKind
        );
        assert!(profile().plan(&binding(0, 9, "1"), &env()).is_err());
        assert!(profile().plan(&binding(7, 0, "1"), &env()).is_err());
        let mut b = binding(7, 9, "1");
        b.secret_version = 0;
        assert!(profile().plan(&b, &env()).is_err());
        let mut e = env();
        e.max_frame_bytes = 0;
        assert!(profile().plan(&binding(7, 9, "1"), &e).is_err());
    }

    #[test]
    fn tenant_is_the_feeds_and_a_disagreeing_claim_is_refused() {
        let mut b = binding(7, 9, "1");
        b.tenant_id = 0;
        assert!(profile().plan(&b, &env()).is_err());
        let mut b = binding(7, 9, "1");
        b.metadata.insert(META_TENANT_ID.into(), "43".into());
        assert_eq!(
            profile().plan(&b, &env()).unwrap_err(),
            PlanError::InvalidValue(META_TENANT_ID)
        );
        let mismatch = KickProfile::new(Arc::new(|_c: i64| Some(99)));
        assert!(mismatch.plan(&binding(7, 9, "1"), &env()).is_err());
        assert!(KickProfile::from_feed()
            .plan(&binding(7, 9, "1"), &env())
            .is_ok());
    }

    #[test]
    fn fingerprint_tracks_room_tenant_and_secret_version_only() {
        let a = one(&binding(7, 9, "12345")).0.fingerprint;
        assert_eq!(a, one(&binding(7, 9, "12345")).0.fingerprint);
        assert_ne!(a, one(&binding(7, 9, "12346")).0.fingerprint);
        let mut b = binding(7, 9, "12345");
        b.secret_version = 2;
        assert_ne!(a, one(&b).0.fingerprint);
    }

    #[tokio::test]
    async fn field_secrets_answer_only_the_channel_handle_with_the_validated_name() {
        let (_, cfg) = one(&binding(7, 9, "12345"));
        let s = KickFieldSecrets::from_config(&cfg).unwrap();
        let dest = Destination::from_url(&reqwest::Url::parse(PUSHER_URL).unwrap()).unwrap();
        let ok = s
            .resolve(cfg.handles[HANDLE_CHANNEL], dest.clone())
            .await
            .unwrap();
        assert_eq!(ok.secret.expose(), "chatrooms.12345.v2");
        for other in [
            ConnectionRef {
                community_id: 7,
                connection_id: 9,
            },
            ConnectionRef {
                community_id: 8,
                connection_id: CHANNEL_CONN_ID,
            },
            ConnectionRef {
                community_id: 7,
                connection_id: -1,
            },
        ] {
            assert!(matches!(
                s.resolve(other, dest.clone()).await,
                Err(SecretError::Missing)
            ));
        }
    }

    #[test]
    fn field_secrets_refuse_a_tampered_config() {
        let (_, mut cfg) = one(&binding(7, 9, "12345"));
        cfg.source_key = "kick:12;45".into();
        assert!(KickFieldSecrets::from_config(&cfg).is_err());
        let (_, mut cfg) = one(&binding(7, 9, "12345"));
        cfg.source_key = "twitch:chan".into();
        assert!(KickFieldSecrets::from_config(&cfg).is_err());
        let (_, mut cfg) = one(&binding(7, 9, "12345"));
        cfg.handles.get_mut(HANDLE_CHANNEL).unwrap().connection_id = 9;
        assert!(KickFieldSecrets::from_config(&cfg).is_err());
        let (_, mut cfg) = one(&binding(7, 9, "12345"));
        cfg.handles.clear();
        assert!(KickFieldSecrets::from_config(&cfg).is_err());
    }
}
