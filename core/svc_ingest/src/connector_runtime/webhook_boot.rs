//! Webhook route activation for the connector runtime (spec
//! `2026-11-05-connector-runtime-inbound-manager.md` section 4, PR-D).
//!
//! Boot builds ONE [`WebhookIngress`] (resolver-backed secrets, Valkey dedup)
//! via [`build_ingress`]; `lib.rs` stores it in `AppState.webhook` BEFORE
//! `http::router(state)`. The runtime then registers one trusted route per
//! `TransportPlan::Webhook` with [`activate_route`] (fail-closed on community
//! mismatch / takeover / route-key tampering) and drives the returned
//! `WebhookTransport`.
//!
//! Route keys are `base64url(HMAC-SHA256(CONNECTOR_ROUTE_KEY_SECRET,
//! "wh:v1:{platform}:{connection_id}"))`: 43 chars, no ids in the URL,
//! unguessable. They are bearer-ish -- logs carry only [`route_fingerprint`].
//!
//! Rotation: the secret is env-only. Rotating it changes every route key, so
//! each provider callback URL must be re-published; do it by restarting
//! svc-ingest with the new secret and re-running the subscription step.
//! Old URLs then answer 404 (unknown route), never an accept.

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Duration;

use axum::http::HeaderName;
use base64::Engine;
use connection_resolver::{ConnectionRef, ConnectionResolve, SecretString};
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

use super::feed::WebhookPlan;
use crate::connector::kick_keys::{HttpKickKeyFetch, KickKeyCache, KickKeySecrets};
use crate::connector::webhook::{
    ConnectionResolverSecrets, RateLimit, RedisWebhookDedup, RegistryError, WebhookIngress,
    WebhookRoute, WebhookSecretSource, WebhookTransport, HARD_MAX_BODY_BYTES,
};
use crate::connector::webhook_verify::{
    Ed25519Spec, Encoding, Scheme, SignedTemplate, TimestampFormat,
};
use crate::ingest::twitch_eventsub::DEDUP_TTL;

/// Env: csv of platforms whose webhook routes are registered (default empty).
pub const ENV_PLATFORMS: &str = "CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS";
/// Env: HMAC key deriving route keys (secret, never a flag/arg).
pub const ENV_ROUTE_KEY_SECRET: &str = "CONNECTOR_ROUTE_KEY_SECRET";
/// Minimum route-key secret length in bytes.
pub const MIN_SECRET_BYTES: usize = 32;
/// Replay window for platforms that publish none (Discord): <= `DEDUP_TTL`.
pub const REPLAY_WINDOW: Duration = Duration::from_secs(300);
/// Valkey response timeout: a stalled store must fail closed quickly.
pub const VALKEY_RESPONSE_TIMEOUT: Duration = Duration::from_secs(2);

/// Platforms that have a webhook scheme in this host.
const SUPPORTED: [&str; 4] = ["discord", "twitch", "wix", "kick"];

/// Webhook boot / activation failures (names the item, never a value).
#[derive(Debug, thiserror::Error)]
pub enum BootError {
    /// A listed platform has no webhook scheme.
    #[error("unsupported webhook platform {0:?}")]
    UnsupportedPlatform(String),
    /// Webhook platforms are listed but the route-key secret is absent/short.
    #[error("{ENV_ROUTE_KEY_SECRET} is required (>= {MIN_SECRET_BYTES} bytes) when {ENV_PLATFORMS} is set")]
    RouteKeySecret,
    /// Webhook platforms are listed but no Valkey client was provided.
    #[error("webhook dedup needs a Valkey client")]
    ValkeyMissing,
    /// The Valkey connection could not be opened.
    #[error("webhook dedup valkey connect failed: {0}")]
    ValkeyConnect(String),
    /// Activation for a platform that is not enabled.
    #[error("webhook platform not enabled")]
    PlatformDisabled,
    /// The plan's route key is not the host derivation for its connection.
    #[error("route key does not match the host derivation")]
    RouteKeyMismatch,
    /// Registry refused the registration/binding.
    #[error("webhook registry: {0}")]
    Registry(#[from] RegistryError),
    /// The Kick public-key fetcher could not be constructed.
    #[error("kick public key fetcher unavailable")]
    KickKeys,
    /// A scheme template failed to build (programming error, surfaced loudly).
    #[error("webhook scheme invalid: {0}")]
    Scheme(String),
}

/// Derives route keys from the env-only secret.
#[derive(Clone)]
pub struct RouteKeyDeriver {
    secret: Arc<SecretString>,
}

impl std::fmt::Debug for RouteKeyDeriver {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("RouteKeyDeriver(<redacted>)")
    }
}

impl RouteKeyDeriver {
    /// Builds from the secret; refuses a short secret.
    pub fn new(secret: SecretString) -> Result<Self, BootError> {
        if secret.expose().len() < MIN_SECRET_BYTES {
            return Err(BootError::RouteKeySecret);
        }
        Ok(Self {
            secret: Arc::new(secret),
        })
    }

    /// `base64url(HMAC-SHA256(secret, "wh:v1:{platform}:{connection_id}"))`.
    pub fn derive(&self, platform: &str, connection_id: i64) -> Result<String, BootError> {
        let mut mac = Hmac::<Sha256>::new_from_slice(self.secret.expose().as_bytes())
            .map_err(|_| BootError::RouteKeySecret)?;
        mac.update(format!("wh:v1:{platform}:{connection_id}").as_bytes());
        Ok(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes()))
    }
}

/// Parsed webhook env: enabled platforms and (when any) the deriver.
#[derive(Debug, Clone)]
pub struct WebhookEnv {
    platforms: BTreeSet<String>,
    deriver: Option<RouteKeyDeriver>,
}

impl WebhookEnv {
    /// Parses `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS` / `CONNECTOR_ROUTE_KEY_SECRET`
    /// strictly: unknown platform or a missing/short secret is fatal.
    pub fn from_lookup(lookup: &dyn Fn(&str) -> Option<String>) -> Result<Self, BootError> {
        let mut platforms = BTreeSet::new();
        let listed = lookup(ENV_PLATFORMS);
        for raw in listed.iter().flat_map(|s| s.split(',')) {
            let p = raw.trim().to_ascii_lowercase();
            if p.is_empty() {
                continue;
            }
            if !SUPPORTED.contains(&p.as_str()) {
                return Err(BootError::UnsupportedPlatform(p));
            }
            platforms.insert(p);
        }
        let deriver = if platforms.is_empty() {
            None
        } else {
            let s = lookup(ENV_ROUTE_KEY_SECRET).ok_or(BootError::RouteKeySecret)?;
            Some(RouteKeyDeriver::new(SecretString::new(s))?)
        };
        Ok(Self { platforms, deriver })
    }

    /// True when no platform registers webhook routes.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.platforms.is_empty()
    }

    /// True when `platform` registers webhook routes.
    #[must_use]
    pub fn enabled(&self, platform: &str) -> bool {
        self.platforms.contains(platform)
    }

    /// The route-key deriver (`None` when no platform is enabled).
    #[must_use]
    pub fn deriver(&self) -> Option<&RouteKeyDeriver> {
        self.deriver.as_ref()
    }
}

/// Everything [`build_ingress`] needs.
pub struct IngressDeps<'a, R: ConnectionResolve + 'static> {
    /// Parsed webhook env.
    pub env: &'a WebhookEnv,
    /// The hub-api connection resolver (secrets/public keys stay inside it).
    pub resolver: Arc<R>,
    /// Valkey client for the shared dedup store (required when enabled).
    pub valkey: Option<&'a redis::Client>,
    /// Registry for `webhook_requests_total`.
    pub registry: &'a prometheus::Registry,
}

/// Builds the ingress, or `None` when no webhook platform is enabled (the
/// route then stays 503 -- fail loud). Any listed platform with missing
/// prerequisites is a [`BootError`].
pub async fn build_ingress<R: ConnectionResolve + 'static>(
    deps: IngressDeps<'_, R>,
) -> Result<Option<Arc<WebhookIngress>>, BootError> {
    if deps.env.is_empty() {
        tracing::info!("no webhook platforms enabled; /intake/webhook stays 503");
        return Ok(None);
    }
    let client = deps.valkey.ok_or(BootError::ValkeyMissing)?;
    let cfg =
        redis::AsyncConnectionConfig::new().set_response_timeout(Some(VALKEY_RESPONSE_TIMEOUT));
    let conn = client
        .get_multiplexed_async_connection_with_config(&cfg)
        .await
        .map_err(|e| BootError::ValkeyConnect(format!("{:?}", e.kind())))?;
    // Kick's verification key is Kick's own published key (fetched, TTL-cached, rotation-aware),
    // not a community secret: its routes get it from `KickKeySecrets`; every other scheme keeps
    // the community-bound resolver secret.
    let base = ConnectionResolverSecrets(deps.resolver);
    let secrets: Arc<dyn WebhookSecretSource> = if deps.env.enabled("kick") {
        let fetch = HttpKickKeyFetch::new().map_err(|_| BootError::KickKeys)?;
        Arc::new(KickKeySecrets::new(
            base,
            Arc::new(KickKeyCache::new(fetch)),
        ))
    } else {
        Arc::new(base)
    };
    let ingress = WebhookIngress::new(
        secrets,
        Arc::new(RedisWebhookDedup::new(conn, DEDUP_TTL)),
        Some(deps.registry),
    );
    tracing::info!(
        platforms = deps.env.platforms.len(),
        "webhook ingress built"
    );
    Ok(Some(Arc::new(ingress)))
}

/// Discord interactions: Ed25519, headers `X-Signature-Ed25519` /
/// `X-Signature-Timestamp`, signed string `timestamp + body`, hex.
fn discord_scheme() -> Result<Scheme, BootError> {
    Ok(Scheme::Ed25519(Ed25519Spec {
        signature_header: HeaderName::from_static("x-signature-ed25519"),
        timestamp_header: HeaderName::from_static("x-signature-timestamp"),
        id_header: None,
        template: SignedTemplate::parse("{ts}{body}").map_err(BootError::Scheme)?,
        encoding: Encoding::Hex,
        timestamp_format: TimestampFormat::UnixSeconds,
    }))
}

/// The scheme for `platform`. Twitch's HMAC scheme stays available but is only
/// reachable when an operator lists `twitch` (never a default).
pub fn scheme_for(platform: &str) -> Result<Scheme, BootError> {
    match platform {
        "discord" => discord_scheme(),
        "twitch" => Ok(Scheme::twitch_eventsub()),
        // Kick routes are bound to one connection's broadcaster id (Kick signs with a single
        // global key): the scheme is built per connection from its binding metadata
        // (`Scheme::kick`), never from a platform constant.
        "kick" => Err(BootError::Scheme(
            "kick scheme is built per connection from its broadcaster id".into(),
        )),
        // Wix routes are bound to a per-connection instance id: their scheme
        // is built by `profiles::wix`, never from a platform constant.
        "wix" => Err(BootError::Scheme(
            "wix scheme is built per connection by its profile".into(),
        )),
        other => Err(BootError::UnsupportedPlatform(other.to_string())),
    }
}

/// Builds the trusted [`WebhookRoute`] for a connection (derived key, the
/// platform's scheme, the connection as `secret_ref`).
pub fn build_route(
    env: &WebhookEnv,
    platform: &str,
    secret_ref: ConnectionRef,
) -> Result<WebhookRoute, BootError> {
    if !env.enabled(platform) {
        return Err(BootError::PlatformDisabled);
    }
    let deriver = env.deriver().ok_or(BootError::RouteKeySecret)?;
    Ok(WebhookRoute {
        route_key: deriver.derive(platform, secret_ref.connection_id)?,
        owner_bundle: platform.to_string(),
        scheme: scheme_for(platform)?,
        secret_ref,
        replay_window: REPLAY_WINDOW,
        rate_limit: RateLimit {
            max: 300,
            per: Duration::from_secs(60),
        },
        body_limit: HARD_MAX_BODY_BYTES,
    })
}

/// Short, non-reversible id of a route key for logs (never the key itself).
#[must_use]
pub fn route_fingerprint(route_key: &str) -> String {
    let d = Sha256::digest(route_key.as_bytes());
    crate::connector::webhook_verify::hex_encode(&d)[..8].to_string()
}

/// Registers `route` (trusted, host-built) and binds its transport. Refuses a
/// disabled platform and any route key that is not the host derivation for
/// `(owner, secret_ref.connection_id)`; `register` itself refuses community
/// mismatch and cross-community takeover.
pub fn activate_route(
    ingress: &WebhookIngress,
    env: &WebhookEnv,
    route: WebhookRoute,
    authorized_community: i64,
) -> Result<WebhookTransport, BootError> {
    if !env.enabled(&route.owner_bundle) {
        return Err(BootError::PlatformDisabled);
    }
    let deriver = env.deriver().ok_or(BootError::RouteKeySecret)?;
    let expected = deriver.derive(&route.owner_bundle, route.secret_ref.connection_id)?;
    if !bool::from(expected.as_bytes().ct_eq(route.route_key.as_bytes())) {
        return Err(BootError::RouteKeyMismatch);
    }
    let key = route.route_key.clone();
    let owner = route.owner_bundle.clone();
    let (community_id, connection_id) = (
        route.secret_ref.community_id,
        route.secret_ref.connection_id,
    );
    ingress.register(route, authorized_community)?;
    let transport = ingress.bind_transport(&key, &owner)?;
    tracing::info!(
        platform = %owner,
        community_id,
        connection_id,
        route_fp = %route_fingerprint(&key),
        "webhook route activated"
    );
    Ok(transport)
}

/// Activates a runtime [`WebhookPlan`] (the `TransportPlan::Webhook` payload):
/// [`activate_route`] with the plan's authorized community.
pub fn activate_plan(
    ingress: &WebhookIngress,
    env: &WebhookEnv,
    plan: WebhookPlan,
) -> Result<WebhookTransport, BootError> {
    activate_route(ingress, env, plan.route, plan.authorized_community)
}

/// Removes a route (owning community only).
pub fn deactivate_route(
    ingress: &WebhookIngress,
    route_key: &str,
    authorized_community: i64,
) -> Result<(), BootError> {
    ingress.unregister(route_key, authorized_community)?;
    tracing::info!(route_fp = %route_fingerprint(route_key), "webhook route deactivated");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connector::webhook::WebhookSecretSource;
    use crate::connector::webhook::{BoxFut, DedupToken, SecretError, WebhookDedup};

    const SECRET: &str = "0123456789abcdef0123456789abcdef";

    fn lookup(pairs: &'static [(&'static str, &'static str)]) -> impl Fn(&str) -> Option<String> {
        move |k| {
            pairs
                .iter()
                .find(|(n, _)| *n == k)
                .map(|(_, v)| (*v).to_string())
        }
    }

    fn env_discord() -> WebhookEnv {
        WebhookEnv::from_lookup(&lookup(&[
            (ENV_PLATFORMS, "discord"),
            (ENV_ROUTE_KEY_SECRET, SECRET),
        ]))
        .unwrap()
    }

    struct Sec;
    impl WebhookSecretSource for Sec {
        fn resolve(&self, _r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
            Box::pin(async { Err(SecretError::Missing) })
        }
    }
    struct Dd;
    impl WebhookDedup for Dd {
        fn first_seen<'a>(&'a self, _k: &'a str) -> BoxFut<'a, Result<Option<DedupToken>, ()>> {
            Box::pin(async { Ok(None) })
        }
        fn release<'a>(&'a self, _k: &'a str, _t: &'a DedupToken) -> BoxFut<'a, Result<(), ()>> {
            Box::pin(async { Ok(()) })
        }
    }
    fn ingress() -> WebhookIngress {
        WebhookIngress::new(Arc::new(Sec), Arc::new(Dd), None)
    }

    #[test]
    fn derive_is_43_urlsafe_chars_deterministic_and_scoped() {
        let d = RouteKeyDeriver::new(SecretString::new(SECRET.into())).unwrap();
        let k = d.derive("discord", 7).unwrap();
        assert_eq!(k.len(), 43);
        assert!(k
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'));
        assert_eq!(k, d.derive("discord", 7).unwrap());
        assert_ne!(k, d.derive("discord", 8).unwrap());
        assert_ne!(k, d.derive("twitch", 7).unwrap());
        let d2 = RouteKeyDeriver::new(SecretString::new(format!("{SECRET}x"))).unwrap();
        assert_ne!(k, d2.derive("discord", 7).unwrap());
        assert_eq!(format!("{d:?}"), "RouteKeyDeriver(<redacted>)");
    }

    #[test]
    fn derive_golden_vector() {
        // HMAC-SHA256(key=SECRET, "wh:v1:discord:42"), base64url no pad.
        let d = RouteKeyDeriver::new(SecretString::new(SECRET.into())).unwrap();
        let mut m = Hmac::<Sha256>::new_from_slice(SECRET.as_bytes()).unwrap();
        m.update(b"wh:v1:discord:42");
        let want =
            base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(m.finalize().into_bytes());
        assert_eq!(d.derive("discord", 42).unwrap(), want);
    }

    #[test]
    fn short_secret_is_refused() {
        assert!(matches!(
            RouteKeyDeriver::new(SecretString::new("short".into())),
            Err(BootError::RouteKeySecret)
        ));
    }

    #[test]
    fn env_empty_by_default_and_twitch_is_not_default() {
        let e = WebhookEnv::from_lookup(&lookup(&[])).unwrap();
        assert!(e.is_empty());
        assert!(!e.enabled("twitch") && !e.enabled("discord"));
        assert!(e.deriver().is_none());
        let blank = WebhookEnv::from_lookup(&lookup(&[(ENV_PLATFORMS, " , ")])).unwrap();
        assert!(blank.is_empty());
    }

    #[test]
    fn env_requires_secret_and_known_platforms() {
        assert!(matches!(
            WebhookEnv::from_lookup(&lookup(&[(ENV_PLATFORMS, "discord")])),
            Err(BootError::RouteKeySecret)
        ));
        assert!(matches!(
            WebhookEnv::from_lookup(&lookup(&[
                (ENV_PLATFORMS, "slack"),
                (ENV_ROUTE_KEY_SECRET, SECRET)
            ])),
            Err(BootError::UnsupportedPlatform(p)) if p == "slack"
        ));
        let e = WebhookEnv::from_lookup(&lookup(&[
            (ENV_PLATFORMS, "Discord, discord ,twitch"),
            (ENV_ROUTE_KEY_SECRET, SECRET),
        ]))
        .unwrap();
        assert!(e.enabled("discord") && e.enabled("twitch"));
    }

    #[test]
    fn schemes_validate_and_unknown_is_refused() {
        scheme_for("discord").unwrap().validate().unwrap();
        scheme_for("twitch").unwrap().validate().unwrap();
        assert!(matches!(
            scheme_for("slack"),
            Err(BootError::UnsupportedPlatform(_))
        ));
        // Kick is bound per connection (its signing key is global), never a platform constant.
        assert!(matches!(scheme_for("kick"), Err(BootError::Scheme(_))));
        Scheme::kick("12345").validate().unwrap();
        for bad in ["", "0", "abc", "12 3"] {
            assert!(Scheme::kick(bad).validate().is_err(), "{bad:?}");
        }
    }

    #[test]
    fn build_route_is_valid_and_gated_on_enabled() {
        let env = env_discord();
        let r = ConnectionRef {
            community_id: 1,
            connection_id: 9,
        };
        let route = build_route(&env, "discord", r).unwrap();
        route.validate().unwrap();
        assert_eq!(route.route_key.len(), 43);
        assert!(matches!(
            build_route(&env, "twitch", r),
            Err(BootError::PlatformDisabled)
        ));
    }

    #[test]
    fn fingerprint_is_short_and_not_the_key() {
        let f = route_fingerprint("some-route-key-abcdef");
        assert_eq!(f.len(), 8);
        assert!(!"some-route-key-abcdef".contains(&f));
    }

    #[test]
    fn activate_registers_binds_and_refuses_tampering() {
        let env = env_discord();
        let ing = ingress();
        let r = ConnectionRef {
            community_id: 1,
            connection_id: 9,
        };
        let route = build_route(&env, "discord", r).unwrap();
        let key = route.route_key.clone();
        activate_route(&ing, &env, route.clone(), 1).unwrap();
        assert_eq!(ing.owner_of(&key).as_deref(), Some("discord"));

        // Wrong authorized community: fail closed.
        assert!(matches!(
            activate_route(&ing, &env, route.clone(), 2),
            Err(BootError::Registry(RegistryError::CommunityMismatch))
        ));
        // Cross-community takeover of the same key.
        let mut taken = route.clone();
        taken.secret_ref.community_id = 2;
        assert!(matches!(
            activate_route(&ing, &env, taken, 2),
            Err(BootError::Registry(RegistryError::CommunityMismatch))
        ));
        // Non-derived key refused.
        let mut forged = route.clone();
        forged.route_key = "attacker-chosen-key-0001".into();
        assert!(matches!(
            activate_route(&ing, &env, forged, 1),
            Err(BootError::RouteKeyMismatch)
        ));
        // Disabled platform refused.
        let mut tw = route.clone();
        tw.owner_bundle = "twitch".into();
        assert!(matches!(
            activate_route(&ing, &env, tw, 1),
            Err(BootError::PlatformDisabled)
        ));
        // Deactivate: only the owning community.
        assert!(matches!(
            deactivate_route(&ing, &key, 2),
            Err(BootError::Registry(RegistryError::CommunityMismatch))
        ));
        deactivate_route(&ing, &key, 1).unwrap();
        assert!(ing.owner_of(&key).is_none());
        assert!(matches!(
            deactivate_route(&ing, &key, 1),
            Err(BootError::Registry(RegistryError::Unknown))
        ));
    }

    #[test]
    fn activate_plan_uses_the_plans_authorized_community() {
        let env = env_discord();
        let ing = ingress();
        let r = ConnectionRef {
            community_id: 3,
            connection_id: 4,
        };
        let ok = WebhookPlan {
            route: build_route(&env, "discord", r).unwrap(),
            authorized_community: 3,
        };
        activate_plan(&ing, &env, ok).unwrap();
        let bad = WebhookPlan {
            route: build_route(&env, "discord", r).unwrap(),
            authorized_community: 4,
        };
        assert!(matches!(
            activate_plan(&ing, &env, bad),
            Err(BootError::Registry(RegistryError::CommunityMismatch))
        ));
    }

    struct NoResolve;
    impl ConnectionResolve for NoResolve {
        async fn resolve(
            &self,
            _r: ConnectionRef,
        ) -> Result<connection_resolver::ResolvedConnection, connection_resolver::ResolveError>
        {
            Err(connection_resolver::ResolveError::Auth)
        }
        async fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            _a: i64,
            _l: u32,
        ) -> Result<connection_resolver::BindingPage, connection_resolver::ResolveError> {
            Err(connection_resolver::ResolveError::Auth)
        }
    }

    #[tokio::test]
    async fn build_ingress_none_when_disabled_and_loud_when_unready() {
        let reg = prometheus::Registry::new();
        let empty = WebhookEnv::from_lookup(&lookup(&[])).unwrap();
        let none = build_ingress(IngressDeps {
            env: &empty,
            resolver: Arc::new(NoResolve),
            valkey: None,
            registry: &reg,
        })
        .await
        .unwrap();
        assert!(none.is_none());

        let env = env_discord();
        let err = build_ingress(IngressDeps {
            env: &env,
            resolver: Arc::new(NoResolve),
            valkey: None,
            registry: &reg,
        })
        .await;
        assert!(matches!(err, Err(BootError::ValkeyMissing)));

        // Nothing listens on port 1: connect fails loudly, never a stub.
        let dead = redis::Client::open("redis://127.0.0.1:1/").unwrap();
        let err = build_ingress(IngressDeps {
            env: &env,
            resolver: Arc::new(NoResolve),
            valkey: Some(&dead),
            registry: &reg,
        })
        .await;
        assert!(matches!(err, Err(BootError::ValkeyConnect(_))));
    }
}
