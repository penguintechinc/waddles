//! Stage-side half of `overlay.publish-surface` (`wit/waddle-bundle/stage.wit`).
//!
//! A bundle hands the stage two strings -- a surface id and a JSON payload.
//! [`SurfaceHost::publish`] then:
//!
//! 1. authorizes the surface id against the surfaces this bundle's manifest
//!    declares ([`SurfacePolicy`]); anything else is `not_granted`;
//! 2. pre-checks the payload (hard size cap, parses as JSON) so abusive input
//!    never costs a network call;
//! 3. delivers it to svc-presentation over **gRPC**
//!    (`waddles.presentation.internal.v1.SurfaceService/Publish`, the only
//!    in-cluster transport) with a machine JWT bound by hub-api to exactly that
//!    community (never a static secret).
//!
//! The community id is a parameter the STAGE derives from the verified
//! invocation scope -- there is no way for a bundle to name another one.
//! Schema validation, per-surface caps, rate limiting, replay and fan-out
//! belong to svc-presentation, the authoritative side; its refusals are mapped
//! to the stable error codes the executor passes to the guest verbatim.
//!
//! This crate also owns the generated gRPC stubs ([`pb`], client AND server) so
//! the caller and the server compile against one contract.
//!
//! Logs carry ids/status/counts only -- never payload content.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use tonic::metadata::MetadataValue;
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Endpoint};
use tonic::{Code, Request};

/// Generated from `proto/waddles/presentation/internal/v1/surface.proto` (`build.rs`).
#[allow(clippy::all, missing_docs)]
pub mod pb {
    tonic::include_proto!("waddles.presentation.internal.v1");
}

use pb::surface_service_client::SurfaceServiceClient;

/// Wire API version sent on every request (`backend.md` gRPC versioning).
pub const API_VERSION: &str = "v1";

/// Trailing-metadata key carrying the suggested retry delay on `RESOURCE_EXHAUSTED`.
pub const RETRY_AFTER_MS_KEY: &str = "retry-after-ms";

/// Manifest permission-id prefix granting one surface: `overlay.surface.<id>`.
pub const SURFACE_PERMISSION_PREFIX: &str = "overlay.surface.";

/// Largest payload this host will forward (matches the contract's
/// `hard_max_payload_bytes`; svc-presentation enforces each surface's own cap).
pub const HARD_MAX_PAYLOAD_BYTES: usize = 16 * 1024;

/// Deadline for the delivery call.
pub const DELIVERY_TIMEOUT: Duration = Duration::from_secs(5);

/// Longest surface id accepted before any lookup.
const MAX_SURFACE_ID_LEN: usize = 64;

/// The surfaces one bundle may publish to, derived from its granted
/// permissions (`overlay.surface.<surface-id>`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SurfacePolicy {
    allowed: HashSet<String>,
    /// `<app_id>.` -- surfaces under the bundle's own namespace are always allowed.
    owned_prefix: Option<String>,
}

impl SurfacePolicy {
    /// Builds the policy from a bundle's granted permission ids; ids without
    /// the surface prefix, or with a malformed surface id, are ignored.
    pub fn from_permissions<'a>(permissions: impl IntoIterator<Item = &'a str>) -> Self {
        let allowed = permissions
            .into_iter()
            .filter_map(|p| p.strip_prefix(SURFACE_PERMISSION_PREFIX))
            .filter(|id| valid_surface_id(id))
            .map(str::to_string)
            .collect();
        Self {
            allowed,
            owned_prefix: None,
        }
    }

    /// Builds the policy for the bundle `app_id`: it OWNS every surface namespaced under its own
    /// app id (`<app_id>.<name>`, the same rule hub-api enforces on `owned_surfaces` at upload),
    /// plus any surface granted by `overlay.surface.<id>` permissions. Ownership is structural --
    /// no bundle can publish into another bundle's namespace.
    pub fn for_app<'a>(app_id: &str, permissions: impl IntoIterator<Item = &'a str>) -> Self {
        let mut policy = Self::from_permissions(permissions);
        policy.owned_prefix = Some(format!("{app_id}."));
        policy
    }

    /// `true` when `surface_id` was granted or is owned by this bundle's namespace.
    pub fn allows(&self, surface_id: &str) -> bool {
        self.allowed.contains(surface_id)
            || self
                .owned_prefix
                .as_deref()
                .is_some_and(|p| surface_id.len() > p.len() && surface_id.starts_with(p))
    }
}

/// Arguments of the stage-side `surface.publish` host call -- exactly the guest's two words;
/// unknown keys are refused so a guest can never name a community, token or address.
#[derive(Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublishArgs {
    /// Surface id.
    pub surface_id: String,
    /// JSON document text.
    pub payload_json: String,
}

impl PublishArgs {
    /// Parses the host call's JSON args; `Invalid` on any shape mismatch.
    pub fn parse(args: &serde_json::Value) -> Result<Self, SurfaceError> {
        serde_json::from_value(args.clone()).map_err(|_| {
            SurfaceError::Invalid("surface.publish expects {surface_id, payload_json}".into())
        })
    }
}

/// `true` for ids shaped like `waddles.core.example.music.now_playing` / `alert`: lowercase
/// segments of `[a-z0-9_]` joined by single dots, at most 64 bytes.
pub fn valid_surface_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= MAX_SURFACE_ID_LEN
        && id.split('.').all(|seg| {
            !seg.is_empty()
                && seg
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
        })
}

/// Why a publish failed. [`Self::code`] values are the stable vocabulary the
/// executor hands the guest as `transport-error.code`.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum SurfaceError {
    /// The bundle did not declare this surface.
    #[error("surface {0:?} is not granted to this bundle")]
    NotGranted(String),
    /// Malformed id or payload, or an unknown surface / schema violation.
    #[error("invalid surface publish: {0}")]
    Invalid(String),
    /// Payload above the size cap (locally or per svc-presentation).
    #[error("payload too large: {0}")]
    TooLarge(String),
    /// svc-presentation's per-(community, surface) limit; wait and retry.
    #[error("rate limited; retry_after_ms={retry_after_ms}")]
    RateLimited {
        /// Suggested delay before retrying.
        retry_after_ms: u32,
    },
    /// The delivery path is down (token mint, transport, server fault); retryable.
    #[error("presentation unavailable: {0}")]
    Unavailable(String),
    /// svc-presentation refused our credential: a wiring fault.
    #[error("presentation refused the host credential: {0}")]
    Denied(String),
}

impl SurfaceError {
    /// The stable error code surfaced to the guest.
    pub fn code(&self) -> &'static str {
        match self {
            Self::NotGranted(_) => "not_granted",
            Self::Invalid(_) => "invalid",
            Self::TooLarge(_) => "too_large",
            Self::RateLimited { .. } => "rate_limited",
            Self::Unavailable(_) => "unavailable",
            Self::Denied(_) => "denied",
        }
    }
}

/// Startup failure building a [`SurfaceHost`]. Always loud: a mis-wired
/// endpoint must stop the stage, not degrade every publish.
#[derive(Debug, thiserror::Error)]
pub enum SurfaceInitError {
    /// The endpoint is not `http(s)://host:port`.
    #[error("invalid presentation gRPC endpoint {0:?}: expected http(s)://host:port")]
    BadEndpoint(String),
    /// Plaintext to a non-loopback host.
    #[error("refusing plaintext gRPC to non-loopback host {0:?}: use https:// plus a CA file")]
    PlaintextNotLoopback(String),
    /// `https://` without a CA bundle to trust.
    #[error("an https:// presentation endpoint requires a CA file")]
    MissingCa,
    /// The CA file could not be read.
    #[error("cannot read CA file {path:?}: {reason}")]
    CaRead {
        /// The path that failed.
        path: String,
        /// The OS error text.
        reason: String,
    },
    /// tonic refused the transport configuration.
    #[error("transport configuration: {0}")]
    Transport(String),
}

/// Source of community-bound PUSH bearer tokens.
#[async_trait]
pub trait PushTokenSource: Send + Sync {
    /// A bearer valid for publishing to exactly `community_id`.
    async fn token_for(&self, community_id: i64) -> Result<Arc<str>, String>;
}

#[async_trait]
impl PushTokenSource for service_auth::BoundMachineJwtClient {
    async fn token_for(&self, community_id: i64) -> Result<Arc<str>, String> {
        self.get_token_for(community_id, None)
            .await
            .map_err(|e| e.to_string())
    }
}

/// `true` for hosts plaintext gRPC is allowed to reach.
fn is_loopback_host(host: &str) -> bool {
    host == "localhost"
        || host
            .trim_matches(['[', ']'])
            .parse::<std::net::IpAddr>()
            .map(|ip| ip.is_loopback())
            .unwrap_or(false)
}

/// The delivery host. Cheap to share behind an `Arc`.
pub struct SurfaceHost {
    channel: Channel,
    tokens: Arc<dyn PushTokenSource>,
}

impl SurfaceHost {
    /// Builds a host delivering to `endpoint` (`https://host:port`, or
    /// `http://127.0.0.1:port` on loopback only). The channel connects lazily
    /// and is reused by every publish; must be called inside a Tokio runtime.
    pub fn new(
        endpoint: &str,
        ca_file: Option<&str>,
        tokens: Arc<dyn PushTokenSource>,
    ) -> Result<Self, SurfaceInitError> {
        let uri: tonic::transport::Uri = endpoint
            .parse()
            .map_err(|_| SurfaceInitError::BadEndpoint(endpoint.to_string()))?;
        let host = uri
            .host()
            .filter(|_| uri.port_u16().is_some())
            .ok_or_else(|| SurfaceInitError::BadEndpoint(endpoint.to_string()))?
            .to_string();
        let mut builder = Endpoint::from_shared(endpoint.to_string())
            .map_err(|_| SurfaceInitError::BadEndpoint(endpoint.to_string()))?
            .timeout(DELIVERY_TIMEOUT)
            .connect_timeout(DELIVERY_TIMEOUT);
        match uri.scheme_str() {
            Some("https") => {
                let path = ca_file.ok_or(SurfaceInitError::MissingCa)?;
                let pem = std::fs::read(path).map_err(|e| SurfaceInitError::CaRead {
                    path: path.to_string(),
                    reason: e.to_string(),
                })?;
                builder = builder
                    .tls_config(ClientTlsConfig::new().ca_certificate(Certificate::from_pem(pem)))
                    .map_err(|e| SurfaceInitError::Transport(e.to_string()))?;
            }
            Some("http") => {
                if !is_loopback_host(&host) {
                    return Err(SurfaceInitError::PlaintextNotLoopback(host));
                }
            }
            _ => return Err(SurfaceInitError::BadEndpoint(endpoint.to_string())),
        }
        Ok(Self {
            channel: builder.connect_lazy(),
            tokens,
        })
    }

    /// Publishes `payload_json` to `surface_id` for `community_id`.
    #[tracing::instrument(
        name = "surface.publish",
        skip(self, policy, payload_json),
        fields(community_id, surface_id)
    )]
    pub async fn publish(
        &self,
        policy: &SurfacePolicy,
        community_id: i64,
        surface_id: &str,
        payload_json: &str,
    ) -> Result<(), SurfaceError> {
        if !valid_surface_id(surface_id) {
            return Err(SurfaceError::Invalid("malformed surface id".into()));
        }
        if !policy.allows(surface_id) {
            return Err(SurfaceError::NotGranted(surface_id.to_string()));
        }
        if community_id <= 0 {
            return Err(SurfaceError::Invalid(
                "no community in the invocation scope".into(),
            ));
        }
        if payload_json.len() > HARD_MAX_PAYLOAD_BYTES {
            return Err(SurfaceError::TooLarge(format!(
                "payload exceeds {HARD_MAX_PAYLOAD_BYTES} bytes"
            )));
        }
        if serde_json::from_str::<serde_json::Value>(payload_json).is_err() {
            return Err(SurfaceError::Invalid("payload is not valid JSON".into()));
        }
        let token = self
            .tokens
            .token_for(community_id)
            .await
            .map_err(|e| SurfaceError::Unavailable(format!("push token: {e}")))?;

        let mut request = Request::new(pb::PublishRequest {
            api_version: API_VERSION.to_string(),
            community_id,
            surface_id: surface_id.to_string(),
            payload_json: payload_json.to_string(),
        });
        request.set_timeout(DELIVERY_TIMEOUT);
        let mut bearer: MetadataValue<_> = format!("Bearer {token}")
            .parse()
            .map_err(|_| SurfaceError::Unavailable("push token is not valid ASCII".into()))?;
        bearer.set_sensitive(true);
        request.metadata_mut().insert("authorization", bearer);

        match SurfaceServiceClient::new(self.channel.clone())
            .publish(request)
            .await
        {
            Ok(_) => {
                tracing::debug!(community_id, surface_id, "surface published");
                Ok(())
            }
            Err(status) => {
                tracing::warn!(
                    community_id,
                    surface_id,
                    code = ?status.code(),
                    "surface publish refused"
                );
                Err(map_status(&status))
            }
        }
    }
}

/// The stage-neutral core of the `surface.publish` host call, shared by every stage that hosts it
/// (svc-action, svc-process): parses the guest's two words, derives the policy from the
/// host-verified `app_id` (a bundle owns exactly its own `<app_id>.` namespace) and delivers for
/// the host-verified `community_id`. The stage owns wiring and the feature flag; this owns
/// everything that decides what may be published and where.
pub async fn publish_for_app(
    host: &SurfaceHost,
    app_id: &str,
    community_id: i64,
    args: &serde_json::Value,
) -> Result<(), SurfaceError> {
    let args = PublishArgs::parse(args)?;
    let policy = SurfacePolicy::for_app(app_id, std::iter::empty());
    host.publish(&policy, community_id, &args.surface_id, &args.payload_json)
        .await
}

/// Maps a svc-presentation refusal onto the stable guest-facing vocabulary. The
/// message is reduced to its first 200 characters: the server only ever sends
/// `path:rule` locations, but nothing longer is forwarded to a guest.
fn map_status(status: &tonic::Status) -> SurfaceError {
    let message: String = status.message().chars().take(200).collect();
    match status.code() {
        Code::NotFound | Code::InvalidArgument => SurfaceError::Invalid(message),
        Code::OutOfRange => SurfaceError::TooLarge(message),
        Code::ResourceExhausted => SurfaceError::RateLimited {
            retry_after_ms: status
                .metadata()
                .get(RETRY_AFTER_MS_KEY)
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.parse::<u32>().ok())
                .unwrap_or(1000),
        },
        Code::Unauthenticated | Code::PermissionDenied | Code::Unimplemented => {
            SurfaceError::Denied(format!("{:?}", status.code()))
        }
        other => SurfaceError::Unavailable(format!("{other:?}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn surface_id_grammar() {
        for ok in [
            "alert",
            "waddles.core.example.music.now_playing",
            "live.channels",
            "a1.b2_c",
        ] {
            assert!(valid_surface_id(ok), "{ok}");
        }
        for bad in [
            "",
            ".",
            "a..b",
            "A.b",
            "a/b",
            "a b",
            "a.b.",
            "../x",
            "a?b=c",
            "é",
            &"a".repeat(65),
        ] {
            assert!(!valid_surface_id(bad), "{bad:?}");
        }
    }

    #[test]
    fn policy_reads_only_surface_permissions() {
        let p = SurfacePolicy::from_permissions([
            "overlay.surface.waddles.core.example.music.now_playing",
            "overlay.surface.alert",
            "overlay.media",
            "overlay.surface.BAD ID",
            "kv.read",
        ]);
        assert!(p.allows("waddles.core.example.music.now_playing"));
        assert!(p.allows("alert"));
        assert!(!p.allows("waddles.core.example.music.queue"));
        assert!(!p.allows("BAD ID"));
    }

    #[test]
    fn a_bundle_owns_exactly_its_own_namespace() {
        let p = SurfacePolicy::for_app("waddles.core.example.music", ["overlay.surface.alert"]);
        assert!(p.allows("waddles.core.example.music.now_playing"));
        assert!(p.allows("waddles.core.example.music.queue"));
        assert!(p.allows("alert"), "explicit grants still apply");
        // Another bundle's namespace, a prefix sibling, the bare prefix and the empty name.
        assert!(!p.allows("waddles.core.example.musicbox.queue"));
        assert!(!p.allows("waddles.core.example.dj.now_playing"));
        assert!(!p.allows("waddles.core.example.music"));
        assert!(!p.allows("waddles.core.example.music."));
        assert!(!p.allows("live.channels"));
        // A policy without an app id owns nothing.
        assert!(!SurfacePolicy::default().allows("waddles.core.example.music.queue"));
    }

    #[test]
    fn publish_args_refuse_unknown_keys_and_wrong_shapes() {
        let ok = PublishArgs::parse(&serde_json::json!({"surface_id": "a", "payload_json": "{}"}))
            .unwrap();
        assert_eq!(
            (ok.surface_id.as_str(), ok.payload_json.as_str()),
            ("a", "{}")
        );
        for bad in [
            serde_json::json!({"surface_id": "a"}),
            serde_json::json!({"surface_id": "a", "payload_json": "{}", "community_id": 9}),
            serde_json::json!({"surface_id": 1, "payload_json": "{}"}),
            serde_json::json!([]),
        ] {
            assert_eq!(PublishArgs::parse(&bad).unwrap_err().code(), "invalid");
        }
    }

    #[test]
    fn codes_are_the_stable_vocabulary() {
        assert_eq!(SurfaceError::NotGranted("x".into()).code(), "not_granted");
        assert_eq!(SurfaceError::Invalid("x".into()).code(), "invalid");
        assert_eq!(SurfaceError::TooLarge("x".into()).code(), "too_large");
        assert_eq!(
            SurfaceError::RateLimited { retry_after_ms: 1 }.code(),
            "rate_limited"
        );
        assert_eq!(SurfaceError::Unavailable("x".into()).code(), "unavailable");
        assert_eq!(SurfaceError::Denied("x".into()).code(), "denied");
    }

    #[test]
    fn rate_limited_message_carries_the_parseable_delay() {
        let msg = SurfaceError::RateLimited {
            retry_after_ms: 2500,
        }
        .to_string();
        assert!(msg.contains("retry_after_ms=2500"));
    }

    #[test]
    fn status_mapping_covers_every_refusal() {
        use tonic::Status;
        assert!(matches!(
            map_status(&Status::not_found("x")),
            SurfaceError::Invalid(_)
        ));
        assert!(matches!(
            map_status(&Status::invalid_argument("x")),
            SurfaceError::Invalid(_)
        ));
        assert!(matches!(
            map_status(&Status::out_of_range("x")),
            SurfaceError::TooLarge(_)
        ));
        for denied in [
            Status::unauthenticated("x"),
            Status::permission_denied("x"),
            Status::unimplemented("x"),
        ] {
            assert_eq!(map_status(&denied).code(), "denied");
        }
        assert_eq!(map_status(&Status::unavailable("x")).code(), "unavailable");
        assert_eq!(map_status(&Status::internal("x")).code(), "unavailable");

        // Rate limit: delay from trailing metadata, default one second.
        let mut limited = Status::resource_exhausted("slow down");
        limited
            .metadata_mut()
            .insert(RETRY_AFTER_MS_KEY, "2500".parse().unwrap());
        assert_eq!(
            map_status(&limited),
            SurfaceError::RateLimited {
                retry_after_ms: 2500
            }
        );
        assert_eq!(
            map_status(&Status::resource_exhausted("x")),
            SurfaceError::RateLimited {
                retry_after_ms: 1000
            }
        );
    }

    #[test]
    fn long_refusal_messages_are_truncated() {
        let long = "x".repeat(500);
        match map_status(&tonic::Status::invalid_argument(long)) {
            SurfaceError::Invalid(m) => assert_eq!(m.len(), 200),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn loopback_detection() {
        for ok in ["localhost", "127.0.0.1", "[::1]", "::1"] {
            assert!(is_loopback_host(ok), "{ok}");
        }
        for bad in ["10.0.0.1", "svc-presentation", "example.com"] {
            assert!(!is_loopback_host(bad), "{bad}");
        }
    }

    struct NoTokens;

    #[async_trait]
    impl PushTokenSource for NoTokens {
        async fn token_for(&self, _community_id: i64) -> Result<Arc<str>, String> {
            Err("no tokens in this test".into())
        }
    }

    #[tokio::test]
    async fn endpoint_validation_fails_loud() {
        let t: Arc<dyn PushTokenSource> = Arc::new(NoTokens);
        let err = |e: &str, ca: Option<&str>| SurfaceHost::new(e, ca, t.clone()).err().unwrap();
        assert!(matches!(
            err("not a url", None),
            SurfaceInitError::BadEndpoint(_)
        ));
        assert!(matches!(
            err("http://127.0.0.1", None),
            SurfaceInitError::BadEndpoint(_)
        ));
        assert!(matches!(
            err("ftp://127.0.0.1:1", None),
            SurfaceInitError::BadEndpoint(_)
        ));
        assert!(matches!(
            err("http://presentation.internal:50051", None),
            SurfaceInitError::PlaintextNotLoopback(_)
        ));
        assert!(matches!(
            err("https://presentation.internal:50051", None),
            SurfaceInitError::MissingCa
        ));
        assert!(matches!(
            err(
                "https://presentation.internal:50051",
                Some("/nonexistent/ca.pem")
            ),
            SurfaceInitError::CaRead { .. }
        ));
        assert!(SurfaceHost::new("http://127.0.0.1:50051", None, t).is_ok());
    }

    #[tokio::test]
    async fn local_refusals_never_touch_the_network() {
        let host = SurfaceHost::new("http://127.0.0.1:1", None, Arc::new(NoTokens)).unwrap();
        let policy = SurfacePolicy::from_permissions(["overlay.surface.alert"]);
        let big = format!("\"{}\"", "a".repeat(HARD_MAX_PAYLOAD_BYTES));
        for (surface, community, body, want) in [
            ("BAD ID", 1, "{}", "invalid"),
            ("waddles.core.example.music.queue", 1, "{}", "not_granted"),
            ("alert", 0, "{}", "invalid"),
            ("alert", 1, big.as_str(), "too_large"),
            ("alert", 1, "not json", "invalid"),
            // Valid request: the token source itself fails first -> unavailable.
            ("alert", 1, "{}", "unavailable"),
        ] {
            let e = host
                .publish(&policy, community, surface, body)
                .await
                .unwrap_err();
            assert_eq!(e.code(), want, "{surface} {community}");
        }
    }
}
