//! The YouTube [`OverlayProfile`] (spec 1.2, PR-F3): derives one
//! `liveChat/messages` [`PollConfig`] per binding.
//!
//! Everything security-relevant is a host constant here: the endpoint, the
//! query shape, the auth header and prefix, the cursor wiring, the interval
//! bounds, the error rules and the secret handle. The ONLY binding-derived
//! value that reaches the URL is `metadata.live_chat_id`, which must match
//! `^[A-Za-z0-9_-]{1,128}$` (the same contract the outbound path and the
//! overlay enforce) and is then added through `Url::query_pairs_mut`, which
//! percent-encodes -- never by string concatenation.
//!
//! | Metadata key | Role |
//! |---|---|
//! | `live_chat_id` | required; absent = connection not ready (no plan, counted) |
//! | `secret_version` | optional; folded into the fingerprint so a rotated credential restarts the connection |
//!
//! Auth is an OAuth bearer (`youtube.oauth`); no API-key query secret is used
//! on this path, so `query_secret` is omitted. A `liveChatEnded` 403 parks the
//! transport; the next reconcile restarts it when the binding carries a new
//! `live_chat_id` (the id is part of the fingerprint).

use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use connection_resolver::{BindingSummary, ConnectionKind, ConnectionRef};
use sha2::{Digest, Sha256};

use crate::connector::lifecycle::{HttpAction, HttpErrorRule, IntervalHint, PollLifecycle};
use crate::connector::poll::{PollAuth, PollConfig};
use crate::connector_runtime::feed::{ConnKey, DesiredConnection, TransportPlan};
use crate::connector_runtime::plan::{OverlayProfile, PlanError, RuntimeEnv};
use crate::outbound_creds_youtube::{is_live_chat_id, META_LIVE_CHAT_ID};

/// Platform slug.
pub const PLATFORM: &str = "youtube";
/// Connector bundle `app_id`.
pub const APP_ID: &str = "waddles.core.service.youtube";
/// Provider name as hub-api's binding list knows it.
pub const PROVIDER: &str = "youtube";
/// Capability the binding list is scoped to.
pub const CAPABILITY: &str = "chat_ingest";
/// `DesiredConnection::tenant_id` as this profile emits it: UNRESOLVED. The
/// binding list carries no server-derived tenant and editable metadata must
/// never supply one, so the runtime MUST resolve the tenant from the numeric
/// `community_id` (scope resolver) and refuse on mismatch. `0` is rejected by
/// `ResolvedScope::validated` (`tenant_id >= 1`), so an unresolved value can
/// never publish.
pub const TENANT_UNRESOLVED: i64 = 0;
/// Metadata key: opaque credential version (optional).
pub const META_SECRET_VERSION: &str = "secret_version";

/// The poll endpoint (a host constant).
pub const POLL_URL: &str = "https://www.googleapis.com/youtube/v3/liveChat/messages";
/// `part` query value requested from the API.
pub const POLL_PART: &str = "id,snippet,authorDetails";
/// `maxResults` requested per poll (API allows 200..=2000; overlay bound 1..=2000).
pub const POLL_MAX_RESULTS: u32 = 200;
/// Secret handle the bearer token resolves through.
pub const HANDLE_OAUTH: &str = "youtube.oauth";
/// Cursor query parameter.
pub const CURSOR_PARAM: &str = "pageToken";
/// Pointer to the next cursor in a response.
pub const CURSOR_POINTER: &str = "/nextPageToken";
/// Pointer to the server-suggested interval (milliseconds).
pub const INTERVAL_POINTER: &str = "/pollingIntervalMillis";
/// Pointer to the error reason in a 403 body.
pub const REASON_POINTER: &str = "/error/errors/0/reason";
/// Interval used until the first response carries a hint (Python default).
pub const DEFAULT_INTERVAL: Duration = Duration::from_secs(5);
/// Interval floor.
pub const MIN_INTERVAL: Duration = Duration::from_secs(2);
/// Interval ceiling.
pub const MAX_INTERVAL: Duration = Duration::from_secs(60);
/// First quota back-off wait.
pub const BACKOFF_BASE: Duration = Duration::from_secs(30);
/// Quota back-off ceiling.
pub const BACKOFF_MAX: Duration = Duration::from_secs(3600);
/// Per-request timeout.
pub const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
/// Consecutive dropped outcomes before a window is poison-skipped.
pub const MAX_DROP_ATTEMPTS: u32 = 3;
/// 403 reasons that mean "slow down".
pub const QUOTA_REASONS: [&str; 4] = [
    "quotaExceeded",
    "rateLimitExceeded",
    "dailyLimitExceeded",
    "userRateLimitExceeded",
];
/// 403 reasons that mean "stop" (credential/permission/chat gone).
pub const PARK_REASONS: [&str; 3] = ["forbidden", "insufficientPermissions", "liveChatEnded"];
/// Bumped when the derivation changes so every connection restarts once.
const DERIVATION_VERSION: u8 = 1;
/// Longest accepted `secret_version`.
const MAX_SECRET_VERSION_LEN: usize = 64;

/// The YouTube live-chat poll profile.
#[derive(Debug, Default)]
pub struct YoutubeProfile {
    skipped_incomplete: AtomicU64,
}

impl YoutubeProfile {
    /// A fresh profile with a zeroed counter.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Bindings skipped because they carry no `live_chat_id` yet.
    #[must_use]
    pub fn skipped_incomplete(&self) -> u64 {
        self.skipped_incomplete.load(Ordering::Relaxed)
    }
}

/// The host-constant lifecycle: interval hint plus the 403 rules. No
/// `query_secret` (bearer auth only on this path).
#[must_use]
pub fn lifecycle() -> PollLifecycle {
    PollLifecycle {
        interval_hint: Some(IntervalHint {
            pointer: INTERVAL_POINTER.to_owned(),
            min: MIN_INTERVAL,
            max: MAX_INTERVAL,
        }),
        error_rules: vec![
            HttpErrorRule {
                status: 403,
                reason_pointer: REASON_POINTER.to_owned(),
                reasons: QUOTA_REASONS.iter().map(|s| (*s).to_owned()).collect(),
                action: HttpAction::Backoff {
                    base: BACKOFF_BASE,
                    max: BACKOFF_MAX,
                    honor_retry_after: true,
                },
            },
            HttpErrorRule {
                status: 403,
                reason_pointer: REASON_POINTER.to_owned(),
                reasons: PARK_REASONS.iter().map(|s| (*s).to_owned()).collect(),
                action: HttpAction::Park,
            },
        ],
        query_secret: None,
    }
}

/// Builds the poll URL. The chat id is appended as a query pair (percent
/// encoded); it must already have passed [`is_live_chat_id`].
fn poll_url(live_chat_id: &str) -> Result<String, PlanError> {
    let mut url = reqwest::Url::parse(POLL_URL)
        .map_err(|_| PlanError::Config("poll endpoint constant is not a URL"))?;
    url.query_pairs_mut()
        .append_pair("part", POLL_PART)
        .append_pair("liveChatId", live_chat_id)
        .append_pair("maxResults", &POLL_MAX_RESULTS.to_string());
    Ok(url.into())
}

/// Optional credential version: bounded charset, never echoed on failure.
fn secret_version(meta: &BTreeMap<String, String>) -> Result<Option<&str>, PlanError> {
    let Some(v) = meta.get(META_SECRET_VERSION) else {
        return Ok(None);
    };
    let ok = !v.is_empty()
        && v.len() <= MAX_SECRET_VERSION_LEN
        && v.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b));
    if ok {
        Ok(Some(v.as_str()))
    } else {
        Err(PlanError::InvalidValue(META_SECRET_VERSION))
    }
}

/// Folds one length-prefixed field into the digest (no ambiguity between
/// adjacent fields).
fn fold(h: &mut Sha256, field: &[u8]) {
    h.update((field.len() as u64).to_be_bytes());
    h.update(field);
}

/// Fingerprint over the derived config plus the credential version. Secrets
/// are never an input; only the opaque version string is.
fn fingerprint(cfg: &PollConfig, secret_version: Option<&str>) -> u64 {
    let mut h = Sha256::new();
    fold(&mut h, &[DERIVATION_VERSION]);
    fold(&mut h, cfg.url.as_bytes());
    fold(&mut h, cfg.source_key.as_bytes());
    fold(&mut h, &cfg.interval.as_millis().to_be_bytes());
    fold(&mut h, &(cfg.max_body_bytes as u64).to_be_bytes());
    let mut handles: Vec<_> = cfg.handles.iter().collect();
    handles.sort_by(|a, b| a.0.cmp(b.0));
    for (name, r) in handles {
        fold(&mut h, name.as_bytes());
        fold(&mut h, &r.community_id.to_be_bytes());
        fold(&mut h, &r.connection_id.to_be_bytes());
    }
    match secret_version {
        Some(v) => {
            fold(&mut h, &[1]);
            fold(&mut h, v.as_bytes());
        }
        None => fold(&mut h, &[0]),
    }
    let d = h.finalize();
    let mut b = [0u8; 8];
    b.copy_from_slice(&d[..8]);
    u64::from_be_bytes(b)
}

impl OverlayProfile for YoutubeProfile {
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
        let Some(chat) = b.metadata.get(META_LIVE_CHAT_ID) else {
            // Mirrors hub-api's 409 metadata_incomplete: not ready yet, not a
            // fault, and it must not stop the rest of the feed.
            self.skipped_incomplete.fetch_add(1, Ordering::Relaxed);
            tracing::debug!(
                community_id = b.community_id,
                connection_id = b.connection_id,
                "youtube binding has no live chat yet; skipped"
            );
            return Ok(Vec::new());
        };
        if !is_live_chat_id(chat) {
            return Err(PlanError::InvalidValue(META_LIVE_CHAT_ID));
        }
        let version = secret_version(&b.metadata)?;

        let conn = ConnectionRef {
            community_id: b.community_id,
            connection_id: b.connection_id,
        };
        let cfg = PollConfig {
            platform: PLATFORM.to_owned(),
            url: poll_url(chat)?,
            source_key: format!("youtube:livechat:{chat}"),
            handles: HashMap::from([(HANDLE_OAUTH.to_owned(), conn)]),
            auth: Some(PollAuth {
                header: "Authorization".to_owned(),
                prefix: "Bearer ".to_owned(),
                handle: HANDLE_OAUTH.to_owned(),
            }),
            cursor_param: Some(CURSOR_PARAM.to_owned()),
            cursor_pointer: Some(CURSOR_POINTER.to_owned()),
            initial_cursor: None,
            interval: DEFAULT_INTERVAL,
            request_timeout: REQUEST_TIMEOUT,
            max_body_bytes: env.max_frame_bytes,
            max_drop_attempts: MAX_DROP_ATTEMPTS,
            egress_allow: env.egress_allow.clone(),
            allow_loopback_insecure: false,
            lifecycle: lifecycle(),
        };
        let url = reqwest::Url::parse(&cfg.url)
            .map_err(|_| PlanError::Config("derived poll url is invalid"))?;
        cfg.lifecycle
            .validate(&cfg.handles, cfg.cursor_param.as_deref(), &url)
            .map_err(|_| PlanError::Config("derived poll lifecycle rejected"))?;
        let fingerprint = fingerprint(&cfg, version);
        Ok(vec![DesiredConnection {
            key: ConnKey {
                platform: PLATFORM,
                community_id: b.community_id,
                connection_id: b.connection_id,
            },
            tenant_id: TENANT_UNRESOLVED,
            plan: TransportPlan::Poll(Box::new(cfg)),
            fingerprint,
        }])
    }
}

#[cfg(test)]
mod tests;
