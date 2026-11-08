//! Stage-side half of `overlay.publish-surface` (`wit/waddle-bundle/stage.wit`).
//!
//! A bundle hands the stage two strings -- a surface id and a JSON payload.
//! [`SurfaceHost::publish`] then:
//!
//! 1. authorizes the surface id against the surfaces this bundle's manifest
//!    declares ([`SurfacePolicy`]); anything else is `not_granted`;
//! 2. pre-checks the payload (hard size cap, parses as JSON) so abusive input
//!    never costs a network call;
//! 3. delivers it to svc-presentation's internal route
//!    `POST /internal/surfaces/{community}/{surface}/publish` with a machine
//!    JWT bound by hub-api to exactly that community (never a static secret).
//!
//! The community id is a parameter the STAGE derives from the verified
//! invocation scope -- there is no way for a bundle to name another one.
//! Schema validation, per-surface caps, rate limiting, replay and fan-out
//! belong to svc-presentation, the authoritative side; its refusals are mapped
//! to the stable error codes the executor passes to the guest verbatim.
//!
//! Logs carry ids/status/counts only -- never payload content.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;

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
        Self { allowed }
    }

    /// `true` when `surface_id` was granted.
    pub fn allows(&self, surface_id: &str) -> bool {
        self.allowed.contains(surface_id)
    }
}

/// `true` for ids shaped like `music.now_playing` / `alert`: lowercase
/// segments of `[a-z0-9_]` joined by single dots, at most 64 bytes. Also what
/// makes the id safe to splice into a URL path segment.
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
    /// Malformed id or payload, or a svc-presentation 404/422.
    #[error("invalid surface publish: {0}")]
    Invalid(String),
    /// Payload above the size cap (locally or per svc-presentation 413).
    #[error("payload too large: {0}")]
    TooLarge(String),
    /// svc-presentation's per-(community, surface) limit; wait and retry.
    #[error("rate limited; retry_after_ms={retry_after_ms}")]
    RateLimited {
        /// Suggested delay before retrying.
        retry_after_ms: u32,
    },
    /// The delivery path is down (token mint, transport, 5xx); retryable.
    #[error("presentation unavailable: {0}")]
    Unavailable(String),
    /// svc-presentation refused our credential (401/403): a wiring fault.
    #[error("presentation refused the host credential: HTTP {0}")]
    Denied(u16),
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

/// The delivery host. Cheap to share behind an `Arc`.
pub struct SurfaceHost {
    base_url: String,
    tokens: Arc<dyn PushTokenSource>,
    http: reqwest::Client,
}

impl SurfaceHost {
    /// Builds a host delivering to `presentation_url` (scheme + authority,
    /// no trailing path). Fails if the HTTP client cannot be built.
    pub fn new(
        presentation_url: impl Into<String>,
        tokens: Arc<dyn PushTokenSource>,
    ) -> Result<Self, reqwest::Error> {
        let http = reqwest::Client::builder()
            .timeout(DELIVERY_TIMEOUT)
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self {
            base_url: presentation_url.into().trim_end_matches('/').to_string(),
            tokens,
            http,
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
        let url = format!(
            "{}/internal/surfaces/{community_id}/{surface_id}/publish",
            self.base_url
        );
        let response = self
            .http
            .post(url)
            .bearer_auth(&*token)
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .body(payload_json.to_string())
            .send()
            .await
            .map_err(|e| SurfaceError::Unavailable(format!("transport: {}", e.without_url())))?;
        let status = response.status();
        if status.is_success() {
            tracing::debug!(community_id, surface_id, "surface published");
            return Ok(());
        }
        let retry_after_ms = response
            .headers()
            .get(reqwest::header::RETRY_AFTER)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<u32>().ok())
            .map(|s| s.saturating_mul(1000))
            .unwrap_or(1000);
        let detail = response.text().await.unwrap_or_default();
        tracing::warn!(
            community_id,
            surface_id,
            status = status.as_u16(),
            "surface publish refused"
        );
        Err(match status.as_u16() {
            404 | 400 | 422 => SurfaceError::Invalid(refusal_summary(&detail)),
            413 => SurfaceError::TooLarge(refusal_summary(&detail)),
            429 => SurfaceError::RateLimited { retry_after_ms },
            401 | 403 => SurfaceError::Denied(status.as_u16()),
            other => SurfaceError::Unavailable(format!("HTTP {other}")),
        })
    }
}

/// Reduces a svc-presentation error body to `message` plus violation
/// locations/rules -- the body never contains payload values, but only these
/// two parts are forwarded so a future body change cannot leak into the guest.
fn refusal_summary(body: &str) -> String {
    let Ok(v) = serde_json::from_str::<serde_json::Value>(body) else {
        return "rejected".to_string();
    };
    let mut out = v
        .get("message")
        .and_then(|m| m.as_str())
        .unwrap_or("rejected")
        .chars()
        .take(200)
        .collect::<String>();
    if let Some(items) = v.get("violations").and_then(|x| x.as_array()) {
        let parts: Vec<String> = items
            .iter()
            .take(8)
            .filter_map(|i| {
                Some(format!(
                    "{}:{}",
                    i.get("path")?.as_str()?,
                    i.get("rule")?.as_str()?
                ))
            })
            .collect();
        if !parts.is_empty() {
            out.push_str(&format!(" [{}]", parts.join(", ")));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn surface_id_grammar() {
        for ok in ["alert", "music.now_playing", "live.channels", "a1.b2_c"] {
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
            "overlay.surface.music.now_playing",
            "overlay.surface.alert",
            "overlay.media",
            "overlay.surface.BAD ID",
            "kv.read",
        ]);
        assert!(p.allows("music.now_playing"));
        assert!(p.allows("alert"));
        assert!(!p.allows("music.queue"));
        assert!(!p.allows("BAD ID"));
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
        assert_eq!(SurfaceError::Denied(403).code(), "denied");
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
    fn refusal_summary_keeps_only_message_and_violation_locations() {
        let body = r#"{"error":"unprocessable_entity","message":"bad","violations":[{"path":"/a","rule":"required"}],"secret":"leak"}"#;
        let s = refusal_summary(body);
        assert_eq!(s, "bad [/a:required]");
        assert_eq!(refusal_summary("not json"), "rejected");
    }
}
