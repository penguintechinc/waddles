//! The RSI Spectrum [`ConnectorProfile`] (gh #486): derives one stateful poll
//! per binding (a lobby, a forum channel, or one organization's roster/events).
//!
//! Everything security-relevant is a host constant here: the RSI host (pinned,
//! and independently enforced by the credential allowlist in
//! `connector::secrets::approved_hosts("spectrum")`), the endpoint paths, the
//! JSON request bodies, the two credential headers and every bound. The ONLY
//! binding-derived values are `kind` (a closed set), `source_id` (validated to
//! `^[A-Za-z0-9_-]{1,64}$` and placed as a JSON *string value* via
//! `serde_json`, never concatenated into a URL or header) and an optional
//! poll interval (clamped). The tenant is the feed's server-derived
//! `BindingSummary::tenant_id`; a metadata `tenant_id` is inert.
//!
//! | Metadata key | Role |
//! |---|---|
//! | `community_id` | the hub-api provider's contract: the Spectrum community id; kind is then `roster` (default) or `events` |
//! | `kind` | `lobby` \| `forum` \| `roster` \| `events`; required with `source_id` |
//! | `source_id` | direct form: lobby / forum-channel / community id (mutually exclusive with `community_id`) |
//! | `poll_interval_s` | optional integer seconds, clamped to the kind's floor |
//!
//! Spectrum is one-way INBOUND (#101): no outbound path exists in the profile,
//! the connector or the host. History is never backfilled (`emit_backlog` is
//! deliberately not configurable).

use std::collections::HashMap;
use std::time::Duration;

use connection_resolver::{BindingSummary, ConnectionKind, ConnectionRef};
use sha2::{Digest, Sha256};

use crate::connector::lifecycle::PollLifecycle;
use crate::connector::poll::{PollAuth, PollConfig, PollRequest};
use crate::connector::spectrum::{SpectrumKind, SpectrumSpec};
use crate::connector::spectrum_org::{DEFAULT_GUARD_MIN_SIZE, DEFAULT_MAX_DEPARTURE_RATIO};
use crate::connector_runtime::feed::{ConnKey, DesiredConnection, SpectrumPlan, TransportPlan};
use crate::connector_runtime::plan::{ConnectorProfile, PlanError, RuntimeEnv};

/// Platform slug.
pub const PLATFORM: &str = "spectrum";
/// Connector bundle app id.
pub const APP_ID: &str = "waddles.core.service.spectrum";
/// Hub-api provider name.
pub const PROVIDER: &str = "spectrum";
/// Capability the binding list is scoped to.
pub const CAPABILITY: &str = "chat_ingest";
/// The only RSI host this profile ever targets.
pub const RSI_HOST: &str = "robertsspaceindustries.com";
/// API base (a host constant).
pub const API_BASE: &str = "https://robertsspaceindustries.com/api/spectrum";
/// Secret handle the RSI session token resolves through.
pub const HANDLE_SESSION: &str = "spectrum.session";
/// Metadata key: source kind.
pub const META_KIND: &str = "kind";
/// Metadata key: the hub-api provider's Spectrum community id.
pub const META_COMMUNITY_ID: &str = "community_id";
/// Metadata key: source id.
pub const META_SOURCE_ID: &str = "source_id";
/// Metadata key: optional interval.
pub const META_INTERVAL: &str = "poll_interval_s";
/// Default chat poll interval.
pub const DEFAULT_POLL_INTERVAL: Duration = Duration::from_secs(15);
/// Chat poll interval floor (respectful polling).
pub const MIN_POLL_INTERVAL: Duration = Duration::from_secs(5);
/// Default org snapshot interval.
pub const DEFAULT_ORG_INTERVAL: Duration = Duration::from_secs(300);
/// Org snapshot interval floor (a roster fetch is several paged requests).
pub const MIN_ORG_INTERVAL: Duration = Duration::from_secs(60);
/// Interval ceiling.
pub const MAX_INTERVAL: Duration = Duration::from_secs(3600);
/// Per-request timeout.
pub const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
/// Snapshot page size.
pub const PAGE_SIZE: u32 = 100;
/// Snapshot page ceiling.
pub const MAX_PAGES: u32 = 50;
/// Courtesy delay between snapshot pages.
pub const PAGE_DELAY: Duration = Duration::from_secs(1);
/// Seen ids remembered per source.
pub const SEEN_CAP: usize = 2000;
/// State lifetime (refreshed on every write).
pub const STATE_TTL: Duration = Duration::from_secs(30 * 24 * 3600);
/// Consecutive drops of one window before it is poison-skipped.
pub const MAX_DROP_ATTEMPTS: u32 = 3;
/// Name of the host-substituted page field in org request bodies.
pub const PAGE_FIELD: &str = "page";
/// Bumped when the derivation changes so every connection restarts once.
const DERIVATION_VERSION: u8 = 1;

/// The Spectrum poll profile.
#[derive(Debug, Default)]
pub struct SpectrumProfile;

impl SpectrumProfile {
    /// A new profile.
    #[must_use]
    pub fn new() -> Self {
        Self
    }
}

/// Parses the closed `kind` set.
fn parse_kind(s: &str) -> Option<SpectrumKind> {
    match s {
        "lobby" => Some(SpectrumKind::Lobby),
        "forum" => Some(SpectrumKind::Forum),
        "roster" => Some(SpectrumKind::Roster),
        "events" => Some(SpectrumKind::Events),
        _ => None,
    }
}

/// True for a valid RSI source id (`^[A-Za-z0-9_-]{1,64}$`).
#[must_use]
pub fn is_source_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// Request path (relative to [`API_BASE`]) of each kind.
fn path_of(kind: SpectrumKind) -> &'static str {
    match kind {
        SpectrumKind::Forum => "/forum/channel/threads",
        SpectrumKind::Lobby => "/lobby/messages",
        SpectrumKind::Roster => "/community/member/list",
        SpectrumKind::Events => "/community/event/list",
    }
}

/// The JSON request body of each kind; `source_id` is a JSON string value.
fn body_of(kind: SpectrumKind, source_id: &str) -> serde_json::Value {
    match kind {
        SpectrumKind::Forum => {
            serde_json::json!({"channel_id": source_id, "page": 1, "sort": "newest"})
        }
        SpectrumKind::Lobby => serde_json::json!({"lobby_id": source_id, "limit": 50}),
        SpectrumKind::Roster | SpectrumKind::Events => {
            serde_json::json!({"community_id": source_id, "page": 1, "pagesize": PAGE_SIZE})
        }
    }
}

/// The poll interval: default or the clamped metadata value.
fn interval_of(b: &BindingSummary, kind: SpectrumKind) -> Result<Duration, PlanError> {
    let (default, floor) = if kind.is_org() {
        (DEFAULT_ORG_INTERVAL, MIN_ORG_INTERVAL)
    } else {
        (DEFAULT_POLL_INTERVAL, MIN_POLL_INTERVAL)
    };
    match b.metadata.get(META_INTERVAL) {
        None => Ok(default),
        Some(v) => {
            let secs: u64 = v
                .parse()
                .map_err(|_| PlanError::InvalidValue(META_INTERVAL))?;
            Ok(Duration::from_secs(secs).clamp(floor, MAX_INTERVAL))
        }
    }
}

fn fold(h: &mut Sha256, field: &[u8]) {
    h.update((field.len() as u64).to_be_bytes());
    h.update(field);
}

/// Fingerprint over the derived config, the tenant and the rotation counter.
/// Secrets are never an input; only the counter is.
fn fingerprint(cfg: &PollConfig, kind: SpectrumKind, tenant: i64, secret_version: i64) -> u64 {
    let mut h = Sha256::new();
    fold(&mut h, &[DERIVATION_VERSION]);
    fold(&mut h, kind.as_str().as_bytes());
    fold(&mut h, cfg.url.as_bytes());
    fold(&mut h, cfg.source_key.as_bytes());
    fold(&mut h, &cfg.interval.as_millis().to_be_bytes());
    fold(&mut h, &(cfg.max_body_bytes as u64).to_be_bytes());
    if let Some(b) = &cfg.request.json_body {
        fold(&mut h, b);
    }
    for r in cfg.handles.values() {
        fold(&mut h, &r.community_id.to_be_bytes());
        fold(&mut h, &r.connection_id.to_be_bytes());
    }
    fold(&mut h, &tenant.to_be_bytes());
    fold(&mut h, &secret_version.to_be_bytes());
    let d = h.finalize();
    let mut b = [0u8; 8];
    b.copy_from_slice(&d[..8]);
    u64::from_be_bytes(b)
}

/// The community-scoped, numeric state key (the RSI id is hashed, not stored).
fn store_key(tenant: i64, community: i64, kind: SpectrumKind, source_id: &str) -> String {
    let d = Sha256::digest(source_id.as_bytes());
    let opaque: String = d.iter().take(8).map(|b| format!("{b:02x}")).collect();
    format!(
        "waddles:ingest:spectrum:{tenant}:{community}:{}:{opaque}",
        kind.as_str()
    )
}

impl ConnectorProfile for SpectrumProfile {
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
        if !matches!(
            b.kind,
            ConnectionKind::PlatformOauth | ConnectionKind::ExternalHttp
        ) {
            return Err(PlanError::WrongKind);
        }
        let (kind, source_id) = match (
            b.metadata.get(META_COMMUNITY_ID),
            b.metadata.get(META_SOURCE_ID),
        ) {
            (Some(_), Some(_)) => return Err(PlanError::InvalidValue(META_SOURCE_ID)),
            // hub-api contract: one Spectrum community; org kinds only.
            (Some(c), None) => {
                let kind = match b.metadata.get(META_KIND) {
                    None => SpectrumKind::Roster,
                    Some(k) => parse_kind(k)
                        .filter(|k| k.is_org())
                        .ok_or(PlanError::InvalidValue(META_KIND))?,
                };
                (kind, c)
            }
            (None, Some(sid)) => {
                let k = b
                    .metadata
                    .get(META_KIND)
                    .ok_or(PlanError::MissingKey(META_KIND))?;
                (
                    parse_kind(k).ok_or(PlanError::InvalidValue(META_KIND))?,
                    sid,
                )
            }
            (None, None) => return Err(PlanError::MissingKey(META_COMMUNITY_ID)),
        };
        if !is_source_id(source_id) {
            return Err(PlanError::InvalidValue(META_SOURCE_ID));
        }
        let tenant = Some(b.tenant_id)
            .filter(|t| *t >= 1)
            .ok_or(PlanError::Config("binding tenant is not positive"))?;
        let version = Some(b.secret_version)
            .filter(|v| *v >= 1)
            .ok_or(PlanError::Config("binding secret version is not positive"))?;
        let interval = interval_of(b, kind)?;

        let conn = ConnectionRef {
            community_id: b.community_id,
            connection_id: b.connection_id,
        };
        let body = serde_json::to_vec(&body_of(kind, source_id))
            .map_err(|_| PlanError::Config("request body not serializable"))?;
        let cfg = PollConfig {
            platform: PLATFORM.to_owned(),
            url: format!("{API_BASE}{}", path_of(kind)),
            source_key: format!("spectrum:{}:{source_id}", kind.as_str()),
            handles: HashMap::from([(HANDLE_SESSION.to_owned(), conn)]),
            auth: Some(PollAuth {
                header: "X-Rsi-Token".to_owned(),
                prefix: String::new(),
                handle: HANDLE_SESSION.to_owned(),
            }),
            cursor_param: None,
            cursor_pointer: None,
            initial_cursor: None,
            interval,
            request_timeout: REQUEST_TIMEOUT,
            max_body_bytes: env.max_frame_bytes,
            max_drop_attempts: MAX_DROP_ATTEMPTS,
            egress_allow: env.egress_allow.clone(),
            allow_loopback_insecure: false,
            lifecycle: PollLifecycle::default(),
            request: PollRequest {
                json_body: Some(body),
                extra_auth: vec![PollAuth {
                    header: "Cookie".to_owned(),
                    prefix: "Rsi-Token=".to_owned(),
                    handle: HANDLE_SESSION.to_owned(),
                }],
                page_field: kind.is_org().then(|| PAGE_FIELD.to_owned()),
            },
            rediscover: None,
        };
        let url = reqwest::Url::parse(&cfg.url)
            .map_err(|_| PlanError::Config("derived poll url is invalid"))?;
        if url.host_str() != Some(RSI_HOST) {
            return Err(PlanError::Config("derived poll host is not the RSI host"));
        }
        cfg.lifecycle
            .validate(&cfg.handles, cfg.cursor_param.as_deref(), &url)
            .map_err(|_| PlanError::Config("derived poll lifecycle rejected"))?;
        let fingerprint = fingerprint(&cfg, kind, tenant, version);
        let spec = SpectrumSpec {
            kind,
            key: store_key(tenant, b.community_id, kind, source_id),
            page_delay: PAGE_DELAY,
            max_pages: MAX_PAGES,
            guard_ratio: DEFAULT_MAX_DEPARTURE_RATIO,
            guard_min_size: DEFAULT_GUARD_MIN_SIZE,
            seen_cap: SEEN_CAP,
            ttl: STATE_TTL,
            max_drop_attempts: MAX_DROP_ATTEMPTS,
        };
        Ok(vec![DesiredConnection {
            key: ConnKey {
                platform: PLATFORM,
                community_id: b.community_id,
                connection_id: b.connection_id,
            },
            tenant_id: tenant,
            plan: TransportPlan::Spectrum(Box::new(SpectrumPlan { poll: cfg, spec })),
            fingerprint,
        }])
    }
}

#[cfg(test)]
mod tests;
