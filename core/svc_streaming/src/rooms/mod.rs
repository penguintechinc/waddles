//! WebRTC rooms: the local SFU control surface behind the `webrtc` service
//! bundle (`bundles/rust/webrtc`). A [`Room`] is an in-memory, community-scoped
//! set of publishers whose RTP is fanned out (one ingest, N subscribers) via the
//! existing [`crate::rtc::fanout::TrackFanout`]; the media path never leaves this
//! process. Join credentials are short-lived signed tokens ([`token`]); the
//! stage reaches the control operations through the mTLS [`host_api`] listener,
//! and clients reach the media endpoints through [`media`].
//!
//! Every lookup is community-scoped: a room is only visible to the community that
//! created it (a mismatch is indistinguishable from "not found").
//!
//! Lifecycle state kept per room, all in memory (like rooms and locks; a restart
//! drops it): the raised-hand queue ([`HandInfo`]), the member table ([`Member`]: who
//! joined and whether they are staff), join times, and the idle-expiry clock. A room that stays empty (no publishers, no subscribers) for its
//! `empty_timeout_secs` is deleted by [`RoomService::expire_idle_at`] -- the Go-era
//! `EmptyTimeout` -- which also drops its hands, lock and issued-token ledger rows.

pub mod grpc;
pub mod grpc_auth;
pub mod host_api;
pub mod media;
pub mod token;

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tokio::sync::{Mutex, RwLock};
use uuid::Uuid;
use webrtc::peer_connection::PeerConnection;

use crate::rtc::fanout::MediaFanouts;
use crate::rtc::metrics::RtcMetrics;
use crate::rtc::pc_factory::PeerConnectionFactory;
use token::{RoomClaims, TokenError, TokenKeys};

/// Default and maximum publishers per room.
pub const DEFAULT_MAX_PUBLISHERS: usize = 16;
pub const MAX_PUBLISHERS_CAP: usize = 64;
/// Maximum live rooms per community.
pub const MAX_ROOMS_PER_COMMUNITY: usize = 100;
/// Maximum subscribers per room.
pub const MAX_SUBSCRIBERS_PER_ROOM: usize = 500;
/// Bound on the revocation list (oldest evicted only after expiry).
const MAX_REVOKED: usize = 100_000;
/// Live revoked-token entries one community may hold.
pub const MAX_REVOKED_PER_COMMUNITY: usize = 1_000;
/// Bound on the issued-token ledger.
const MAX_ISSUED: usize = 200_000;
/// Live issued-token entries one community may hold.
pub const MAX_ISSUED_PER_COMMUNITY: usize = 10_000;
/// Subscribers one community may hold across all its rooms (well below the global cap).
pub const MAX_SUBSCRIBERS_PER_COMMUNITY: usize = 1_000;
/// Subscribers across all rooms.
pub const MAX_SUBSCRIBERS_GLOBAL: usize = 5_000;
/// Longest TTL of a WHEP pipeline viewer token (seconds).
pub const MAX_PIPELINE_TOKEN_TTL_SECS: i64 = 300;

/// Most participants one room may blocklist (kick) or hold muted.
pub const MAX_MODERATED_PER_ROOM: usize = 1_024;

/// Most raised hands one room's queue may hold.
pub const MAX_HANDS_PER_ROOM: usize = 256;

/// Most joined participants (token holders) one room's member table may hold.
pub const MAX_MEMBERS_PER_ROOM: usize = 1_000;

/// Default empty-room expiry (seconds): the Go-era LiveKit `EmptyTimeout`.
pub const DEFAULT_EMPTY_TIMEOUT_SECS: u32 = 300;
/// Default upper bound a client-requested `empty_timeout_secs` is clamped to (24 h).
pub const DEFAULT_EMPTY_TIMEOUT_MAX_SECS: u32 = 86_400;
/// Absolute ceiling an operator may configure the bound to (7 days).
pub const EMPTY_TIMEOUT_HARD_MAX_SECS: u32 = 604_800;

/// Idle-room expiry policy: the default applied when a create request names none, and
/// the bound every requested value is clamped to. `0` as a default (or as a request)
/// means "never expire".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IdlePolicy {
    /// Applied to rooms created without an explicit `empty_timeout_secs`.
    pub default_secs: u32,
    /// Largest non-zero timeout a room may have; larger requests are clamped to it.
    pub max_secs: u32,
}

impl Default for IdlePolicy {
    fn default() -> Self {
        Self {
            default_secs: DEFAULT_EMPTY_TIMEOUT_SECS,
            max_secs: DEFAULT_EMPTY_TIMEOUT_MAX_SECS,
        }
    }
}

impl IdlePolicy {
    /// Env var holding the default timeout in seconds (`0` = rooms never expire by default).
    pub const ENV_DEFAULT: &'static str = "ROOMS_EMPTY_TIMEOUT_DEFAULT_SECS";
    /// Env var holding the clamp bound in seconds (1..=[`EMPTY_TIMEOUT_HARD_MAX_SECS`]).
    pub const ENV_MAX: &'static str = "ROOMS_EMPTY_TIMEOUT_MAX_SECS";

    /// Builds a validated policy: `1 <= max_secs <= EMPTY_TIMEOUT_HARD_MAX_SECS` and
    /// `default_secs <= max_secs`.
    pub fn new(default_secs: u32, max_secs: u32) -> Result<Self, String> {
        if max_secs == 0 || max_secs > EMPTY_TIMEOUT_HARD_MAX_SECS {
            return Err(format!(
                "{} must be 1..={EMPTY_TIMEOUT_HARD_MAX_SECS}, got {max_secs}",
                Self::ENV_MAX
            ));
        }
        if default_secs > max_secs {
            return Err(format!(
                "{} ({default_secs}) must not exceed {} ({max_secs})",
                Self::ENV_DEFAULT,
                Self::ENV_MAX
            ));
        }
        Ok(Self {
            default_secs,
            max_secs,
        })
    }

    /// Reads the policy through `get` (an env lookup); unset vars take the defaults, a
    /// present-but-invalid value is an error (never silently ignored).
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Self, String> {
        let read = |key: &str, default: u32| -> Result<u32, String> {
            match get(key)
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
            {
                None => Ok(default),
                Some(v) => v
                    .parse::<u32>()
                    .map_err(|_| format!("{key} must be a non-negative integer, got {v:?}")),
            }
        };
        Self::new(
            read(Self::ENV_DEFAULT, DEFAULT_EMPTY_TIMEOUT_SECS)?,
            read(Self::ENV_MAX, DEFAULT_EMPTY_TIMEOUT_MAX_SECS)?,
        )
    }

    /// Reads the policy from the process environment ([`Self::from_lookup`]).
    pub fn from_env() -> Result<Self, String> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// The effective timeout for a create request: unset -> the default, `0` -> never,
    /// anything else clamped to [`Self::max_secs`].
    pub fn resolve(&self, requested: Option<u32>) -> u32 {
        match requested {
            None => self.default_secs,
            Some(0) => 0,
            Some(n) => n.min(self.max_secs),
        }
    }
}

/// The acting participant's room role as resolved by the rooms app from its
/// per-room ACL (`bundles/rust/rooms` `Role`). Interim authorization input:
/// migrates to the SDK `caller_can` check once that lands.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActorRole {
    Guest,
    Member,
    Moderator,
    Admin,
}

impl ActorRole {
    /// True for the roles allowed to run moderator actions.
    pub fn is_moderator(self) -> bool {
        matches!(self, Self::Moderator | Self::Admin)
    }
}

/// One participant of a room as listed by [`RoomService::participants`]: UUID
/// and booleans only, never a name.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ParticipantInfo {
    pub participant_id: Uuid,
    pub publisher: bool,
    pub subscriber: bool,
    pub muted: bool,
    /// Unix seconds the participant's first live session opened; kept while any of
    /// their sessions stays up. `None` is never reported for a listed participant
    /// (a participant is only listed while they hold a session), but the type stays
    /// optional so a missing record can never be rendered as `0`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub joined_at: Option<i64>,
}

/// One raised hand as listed by [`RoomService::list_hands`]: UUID and timestamps only.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HandInfo {
    pub participant_id: Uuid,
    pub raised_at: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub acknowledged_at: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub acknowledged_by: Option<Uuid>,
}

/// A raised-hand queue entry (FIFO; `raised_at` never decreases along the queue).
#[derive(Debug, Clone)]
struct HandEntry {
    participant: Uuid,
    raised_at: i64,
    acknowledged_at: Option<i64>,
    acknowledged_by: Option<Uuid>,
}

impl HandEntry {
    fn info(&self) -> HandInfo {
        HandInfo {
            participant_id: self.participant,
            raised_at: self.raised_at,
            acknowledged_at: self.acknowledged_at,
            acknowledged_by: self.acknowledged_by,
        }
    }
}

/// How a room's publisher media reaches subscribers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RouteMode {
    /// Local selective-forwarding: one ingest fanned out to N subscribers.
    Sfu,
    /// Pass-through/relay to an external SFU. Not implemented -- selecting it
    /// fails loud ([`RoomError::Unsupported`]) rather than silently using SFU.
    Relay,
}

/// Errors from room operations; mapped onto HTTP by [`host_api`]/[`media`].
#[derive(Debug, thiserror::Error)]
pub enum RoomError {
    #[error("room not found")]
    NotFound,
    #[error("invalid request: {0}")]
    Invalid(String),
    #[error("limit reached: {0}")]
    LimitReached(String),
    #[error("forbidden: {0}")]
    Forbidden(&'static str),
    #[error("unauthorized")]
    Unauthorized,
    #[error("rooms are not configured on this host: {0}")]
    Unconfigured(&'static str),
    #[error("unsupported: {0}")]
    Unsupported(String),
    #[error("ambiguous source: {0}")]
    Ambiguous(String),
    #[error("backend error: {0}")]
    Backend(String),
}

/// Issued-token ledger: jti -> (community, room, expiry), an expiry-ordered index
/// for pruning, and live counts per community.
#[derive(Default)]
struct Ledger {
    by_jti: HashMap<Uuid, (String, Uuid, i64)>,
    by_exp: BTreeSet<(i64, Uuid)>,
    per_community: HashMap<String, usize>,
}

impl Ledger {
    fn get(&self, jti: &Uuid) -> Option<&(String, Uuid, i64)> {
        self.by_jti.get(jti)
    }

    /// Drops every entry that expired before `t`, oldest-expiry first.
    fn prune(&mut self, t: i64) {
        while let Some(&(exp, jti)) = self.by_exp.first() {
            if exp >= t {
                break;
            }
            self.by_exp.remove(&(exp, jti));
            if let Some((community, _, _)) = self.by_jti.remove(&jti) {
                if let Some(n) = self.per_community.get_mut(&community) {
                    *n -= 1;
                    if *n == 0 {
                        self.per_community.remove(&community);
                    }
                }
            }
        }
    }

    fn insert(&mut self, jti: Uuid, community: &str, room: Uuid, exp: i64) {
        self.by_jti.insert(jti, (community.to_string(), room, exp));
        self.by_exp.insert((exp, jti));
        *self.per_community.entry(community.to_string()).or_insert(0) += 1;
    }

    /// Drops every entry issued for `room` (it no longer exists), freeing the owning
    /// community's live-token quota.
    fn forget_room(&mut self, room: Uuid) {
        let doomed: Vec<Uuid> = self
            .by_jti
            .iter()
            .filter(|(_, (_, r, _))| *r == room)
            .map(|(jti, _)| *jti)
            .collect();
        for jti in doomed {
            if let Some((community, _, exp)) = self.by_jti.remove(&jti) {
                self.by_exp.remove(&(exp, jti));
                if let Some(n) = self.per_community.get_mut(&community) {
                    *n -= 1;
                    if *n == 0 {
                        self.per_community.remove(&community);
                    }
                }
            }
        }
    }
}

/// Revoked jti -> (community, expiry), plus insertion order for eviction.
type RevokedSet = (HashMap<Uuid, (String, i64)>, VecDeque<Uuid>);

struct PublisherEntry {
    fanouts: MediaFanouts,
    pc: Arc<dyn PeerConnection>,
    session: Uuid,
    /// Set by the peer-connection state hook when it Failed/Closed/Disconnected.
    ended: Arc<AtomicBool>,
    /// Token expiry: the session may not outlive the credential that opened it.
    expires_at: i64,
    /// Token id the session was opened with (revocation tears the session down).
    jti: Uuid,
}

struct SubscriberEntry {
    pc: Arc<dyn PeerConnection>,
    participant: Uuid,
    ended: Arc<AtomicBool>,
    expires_at: i64,
    jti: Uuid,
}

struct Room {
    community_id: String,
    label: Option<String>,
    max_publishers: usize,
    route: RouteMode,
    created_at: i64,
    /// Bumped to invalidate every token issued before.
    epoch: u64,
    publishers: HashMap<Uuid, PublisherEntry>,
    subscribers: HashMap<Uuid, SubscriberEntry>,
    /// Locked rooms refuse token issuance and new joins.
    locked: bool,
    /// Participants server-side muted (applies again if they reconnect).
    muted: HashSet<Uuid>,
    /// Kicked participants: their tokens no longer authorize in this room.
    kicked: HashSet<Uuid>,
    /// Raised-hand queue, oldest first. Authoritative here (in memory, like locks).
    hands: Vec<HandEntry>,
    /// Participant -> unix seconds their first live session opened; pruned when their
    /// last session goes away.
    joined: HashMap<Uuid, i64>,
    /// Joined participants (token holders) and their server-recorded staff flag; the ACL
    /// hand operations are checked against. Dropped on leave/kick and with the room.
    members: HashMap<Uuid, Member>,
    /// Seconds the room may stay empty before it expires; 0 = never.
    empty_timeout_secs: u32,
    /// When the room last became (observed) empty; `None` while it has sessions.
    empty_since: Option<i64>,
}

impl Room {
    /// True while any publisher or subscriber session is registered.
    fn occupied(&self) -> bool {
        !self.publishers.is_empty() || !self.subscribers.is_empty()
    }

    /// Forgets join times of participants with no live session left.
    fn prune_joined(&mut self) {
        let live: HashSet<Uuid> = self
            .publishers
            .keys()
            .copied()
            .chain(self.subscribers.values().map(|s| s.participant))
            .collect();
        self.joined.retain(|p, _| live.contains(p));
    }
}

/// Snapshot of a room for API responses. Carries ids and counts only.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RoomInfo {
    pub room_id: Uuid,
    pub community_id: String,
    pub label: Option<String>,
    pub route: RouteMode,
    pub max_publishers: usize,
    pub created_at: i64,
    pub publisher_ids: Vec<Uuid>,
    pub subscriber_count: usize,
    #[serde(default)]
    pub locked: bool,
    /// Effective empty-room expiry in seconds (0 = never).
    #[serde(default)]
    pub empty_timeout_secs: u32,
}

/// The credential a live session was opened with: bounds its lifetime and lets
/// token revocation find and close it.
#[derive(Debug, Clone, Copy)]
pub(crate) struct SessionGrant {
    pub expires_at: i64,
    pub jti: Uuid,
}

/// Parameters for [`RoomService::issue_token`].
#[derive(Debug, Clone)]
pub struct IssueRequest {
    pub participant: Uuid,
    pub publish: bool,
    pub subscribe: bool,
    pub source: Option<Uuid>,
    pub ttl_secs: Option<i64>,
    /// Registers the participant as room STAFF in the server-side member table. Only a
    /// transport that has VERIFIED moderation authority may set it (the gRPC `JoinRoom` with a
    /// `streaming.rooms:moderate` service JWT); every other issuance path leaves it `false`.
    /// Sticky: a later non-staff issuance never demotes (leave/kick/expiry does).
    pub staff: bool,
}

/// A participant who joined a room (was issued a token for it): the server-side ACL the
/// raised-hand operations are checked against, instead of any role the caller claims.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Member {
    /// Staff may acknowledge/reset hands and lower or list everyone's.
    pub staff: bool,
}

/// Which raised hands a [`RoomService::list_hands`] caller wants.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HandView {
    /// Only the actor's own entry (members).
    Own,
    /// The whole queue; refused unless the actor is staff.
    All,
    /// Whole queue for staff, own entry for everyone else.
    Auto,
}

/// The result of [`RoomService::list_hands`]: the visible hands plus queue facts that never
/// reveal who else is waiting.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct HandList {
    /// Visible hands, oldest first.
    pub hands: Vec<HandInfo>,
    /// Total hands in the room's queue.
    pub count: usize,
    /// The actor's 1-based position in the queue, when their hand is raised.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub position: Option<usize>,
}

/// An issued token plus its handle/expiry.
#[derive(Debug, Clone, Serialize)]
pub struct IssuedToken {
    pub token: String,
    pub token_id: Uuid,
    pub expires_at: i64,
}

/// Owns every room, the token keys and the revocation list.
pub struct RoomService {
    rooms: RwLock<HashMap<Uuid, Room>>,
    /// jti -> (community, expiry) of revoked tokens.
    revoked: Mutex<RevokedSet>,
    /// jti -> (community, room, expiry) of every issued token: revocation is only
    /// honored for a token the caller's own community+room actually issued.
    issued: Mutex<Ledger>,
    max_community_subscribers: usize,
    keys: Option<TokenKeys>,
    whep: Option<Arc<crate::egress::whep::WhepState>>,
    pub(crate) factory: Arc<PeerConnectionFactory>,
    pub(crate) metrics: RtcMetrics,
    public_base_url: String,
    /// `rooms.moderation` flag (env baseline `FEATURE_ROOMS_MODERATION`), default OFF.
    moderation_enabled: bool,
    /// Idle-room expiry defaults and bound.
    idle: IdlePolicy,
}

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

/// The community id as a span attribute: the digits when it is a plain numeric id, else `-`,
/// so request text can never become telemetry (a bare `community_id` span field would record
/// nothing at all -- tracing only records a field that has a value).
fn span_cid(community_id: &str) -> &str {
    if !community_id.is_empty()
        && community_id.len() <= 20
        && community_id.bytes().all(|b| b.is_ascii_digit())
    {
        community_id
    } else {
        "-"
    }
}

impl RoomService {
    /// Builds the service. `keys == None` disables token issue/verify (fail loud).
    pub fn new(
        factory: Arc<PeerConnectionFactory>,
        metrics: RtcMetrics,
        keys: Option<TokenKeys>,
        public_base_url: String,
    ) -> Self {
        Self {
            rooms: RwLock::new(HashMap::new()),
            revoked: Mutex::new((HashMap::new(), VecDeque::new())),
            issued: Mutex::new(Ledger::default()),
            max_community_subscribers: MAX_SUBSCRIBERS_PER_COMMUNITY,
            keys,
            whep: None,
            factory,
            metrics,
            public_base_url,
            moderation_enabled: false,
            idle: IdlePolicy::default(),
        }
    }

    /// Sets the idle-room expiry policy (defaults to [`IdlePolicy::default`]).
    pub fn with_idle_policy(mut self, idle: IdlePolicy) -> Self {
        self.idle = idle;
        self
    }

    /// The active idle-room expiry policy.
    pub fn idle_policy(&self) -> IdlePolicy {
        self.idle
    }

    /// Readiness for `grpc.health.v1`: `Err(reason)` when the service cannot do its job
    /// (room tokens cannot be issued or verified without the configured secret).
    pub fn readiness(&self) -> Result<(), &'static str> {
        if self.keys.is_none() {
            Err("WEBRTC_ROOM_TOKEN_SIGNING_KEY is not set: room tokens cannot be issued")
        } else {
            Ok(())
        }
    }

    /// Turns the `rooms.moderation` feature (leave/kick/mute/lock ops) on or off.
    pub fn with_moderation(mut self, enabled: bool) -> Self {
        self.moderation_enabled = enabled;
        self
    }

    /// Overrides the per-community subscriber cap (tests / operators).
    pub fn with_community_subscriber_cap(mut self, cap: usize) -> Self {
        self.max_community_subscribers = cap;
        self
    }

    /// Revokes every WHEP viewer token of a pipeline `community_id` owns and closes
    /// its live viewer sessions; returns the new epoch.
    pub async fn revoke_pipeline_viewers(
        &self,
        community_id: &str,
        pipeline_id: Uuid,
    ) -> Result<u64, RoomError> {
        let whep = self.whep.as_ref().ok_or(RoomError::Unconfigured(
            "WHEP is not attached to the room service",
        ))?;
        if whep.owner_of(pipeline_id).await.as_deref() != Some(community_id) {
            return Err(RoomError::NotFound);
        }
        Ok(whep.revoke_viewers(pipeline_id).await)
    }

    /// Attaches the WHEP state so pipeline viewer tokens can be issued for streams it owns.
    pub fn with_whep(mut self, whep: Arc<crate::egress::whep::WhepState>) -> Self {
        self.whep = Some(whep);
        self
    }

    /// Externally-reachable base URL used in returned endpoint URLs.
    pub fn public_base_url(&self) -> &str {
        &self.public_base_url
    }

    /// True when a token secret is configured.
    pub fn tokens_configured(&self) -> bool {
        self.keys.is_some()
    }

    fn keys(&self) -> Result<&TokenKeys, RoomError> {
        self.keys.as_ref().ok_or(RoomError::Unconfigured(
            "WEBRTC_ROOM_TOKEN_SIGNING_KEY is not set",
        ))
    }

    fn info(id: Uuid, r: &Room) -> RoomInfo {
        let mut publisher_ids: Vec<Uuid> = r.publishers.keys().copied().collect();
        publisher_ids.sort();
        RoomInfo {
            room_id: id,
            community_id: r.community_id.clone(),
            label: r.label.clone(),
            route: r.route,
            max_publishers: r.max_publishers,
            created_at: r.created_at,
            publisher_ids,
            subscriber_count: r.subscribers.len(),
            locked: r.locked,
            empty_timeout_secs: r.empty_timeout_secs,
        }
    }

    /// Creates a room for `community_id` with the default idle-expiry policy.
    pub async fn create(
        &self,
        community_id: &str,
        label: Option<String>,
        max_publishers: Option<usize>,
        route: RouteMode,
    ) -> Result<RoomInfo, RoomError> {
        self.create_with(community_id, label, max_publishers, route, None)
            .await
    }

    /// Creates a room for `community_id`. `empty_timeout_secs`: unset = the policy
    /// default, `0` = never expire, otherwise clamped to the policy maximum; an empty room
    /// older than that is deleted by [`Self::expire_idle_at`].
    pub async fn create_with(
        &self,
        community_id: &str,
        label: Option<String>,
        max_publishers: Option<usize>,
        route: RouteMode,
        empty_timeout_secs: Option<u32>,
    ) -> Result<RoomInfo, RoomError> {
        if community_id.is_empty() || !community_id.bytes().all(|b| b.is_ascii_digit()) {
            return Err(RoomError::Invalid("community_id must be numeric".into()));
        }
        if route == RouteMode::Relay {
            return Err(RoomError::Unsupported(
                "route mode relay is unsupported; use sfu".into(),
            ));
        }
        if label.as_deref().is_some_and(|l| l.len() > 128) {
            return Err(RoomError::Invalid("label too long".into()));
        }
        let max_publishers = max_publishers.unwrap_or(DEFAULT_MAX_PUBLISHERS);
        if max_publishers == 0 || max_publishers > MAX_PUBLISHERS_CAP {
            return Err(RoomError::Invalid(format!(
                "max_publishers must be 1..={MAX_PUBLISHERS_CAP}"
            )));
        }
        let mut rooms = self.rooms.write().await;
        if rooms
            .values()
            .filter(|r| r.community_id == community_id)
            .count()
            >= MAX_ROOMS_PER_COMMUNITY
        {
            return Err(RoomError::LimitReached(format!(
                "{MAX_ROOMS_PER_COMMUNITY} rooms per community"
            )));
        }
        let id = Uuid::new_v4();
        let created_at = now();
        let room = Room {
            community_id: community_id.to_string(),
            label,
            max_publishers,
            route,
            created_at,
            epoch: 0,
            publishers: HashMap::new(),
            subscribers: HashMap::new(),
            locked: false,
            muted: HashSet::new(),
            kicked: HashSet::new(),
            hands: Vec::new(),
            joined: HashMap::new(),
            members: HashMap::new(),
            empty_timeout_secs: self.idle.resolve(empty_timeout_secs),
            empty_since: Some(created_at),
        };
        let info = Self::info(id, &room);
        rooms.insert(id, room);
        tracing::info!(room_id = %id, community_id, empty_timeout_secs = info.empty_timeout_secs, "rooms: created");
        Ok(info)
    }

    /// Looks a room up, community-scoped.
    pub async fn get(&self, community_id: &str, room_id: Uuid) -> Result<RoomInfo, RoomError> {
        let rooms = self.rooms.read().await;
        match rooms.get(&room_id) {
            Some(r) if r.community_id == community_id => Ok(Self::info(room_id, r)),
            _ => Err(RoomError::NotFound),
        }
    }

    /// Closes a room: drops every peer connection and forgets it.
    pub async fn close(&self, community_id: &str, room_id: Uuid) -> Result<RoomInfo, RoomError> {
        let room = {
            let mut rooms = self.rooms.write().await;
            match rooms.get(&room_id) {
                Some(r) if r.community_id == community_id => rooms.remove(&room_id),
                _ => None,
            }
        }
        .ok_or(RoomError::NotFound)?;
        let info = Self::info(room_id, &room);
        let mut pcs: Vec<Arc<dyn PeerConnection>> =
            room.publishers.values().map(|p| p.pc.clone()).collect();
        pcs.extend(room.subscribers.values().map(|s| s.pc.clone()));
        for pc in pcs {
            let _ = pc.close().await;
        }
        self.count_hands_cleared("delete", room.hands.len());
        self.issued.lock().await.forget_room(room_id);
        tracing::info!(%room_id, community_id, "rooms: closed");
        Ok(info)
    }

    /// Issues a room-scoped join token and records it in the issued ledger.
    pub async fn issue_token(
        &self,
        community_id: &str,
        room_id: Uuid,
        req: IssueRequest,
    ) -> Result<IssuedToken, RoomError> {
        let keys = self.keys()?;
        if !req.publish && !req.subscribe {
            return Err(RoomError::Invalid(
                "token must grant publish and/or subscribe".into(),
            ));
        }
        if req.source.is_some() && req.publish {
            return Err(RoomError::Invalid(
                "source restriction applies to subscribe-only tokens".into(),
            ));
        }
        let ttl = req.ttl_secs.unwrap_or(token::DEFAULT_TTL_SECS);
        if !(token::MIN_TTL_SECS..=token::MAX_TTL_SECS).contains(&ttl) {
            return Err(RoomError::Invalid(format!(
                "ttl_secs must be {}..={}",
                token::MIN_TTL_SECS,
                token::MAX_TTL_SECS
            )));
        }
        let epoch = {
            let mut rooms = self.rooms.write().await;
            match rooms.get_mut(&room_id) {
                Some(r) if r.community_id == community_id => {
                    if r.locked {
                        return Err(RoomError::Forbidden("room is locked"));
                    }
                    if r.kicked.contains(&req.participant) {
                        return Err(RoomError::Forbidden(
                            "participant was removed from this room",
                        ));
                    }
                    if !r.members.contains_key(&req.participant)
                        && r.members.len() >= MAX_MEMBERS_PER_ROOM
                    {
                        return Err(RoomError::LimitReached(format!(
                            "{MAX_MEMBERS_PER_ROOM} participants per room"
                        )));
                    }
                    // Joining is recorded server-side; staff is sticky (never demoted by a
                    // later ordinary issuance) and only a verified-authority caller sets it.
                    let staff =
                        req.staff || r.members.get(&req.participant).is_some_and(|m| m.staff);
                    r.members.insert(req.participant, Member { staff });
                    // A join in flight counts as activity: an empty room must not expire
                    // between a participant receiving a token and opening their session.
                    if r.empty_since.is_some() {
                        r.empty_since = Some(now());
                    }
                    r.epoch
                }
                _ => return Err(RoomError::NotFound),
            }
        };
        let iat = now();
        let jti = Uuid::new_v4();
        let claims = RoomClaims {
            iss: token::ROOM_TOKEN_ISSUER.into(),
            aud: token::ROOM_TOKEN_AUDIENCE.into(),
            sub: req.participant.to_string(),
            room: room_id.to_string(),
            community: community_id.to_string(),
            publish: req.publish,
            subscribe: req.subscribe,
            source: req.source.map(|s| s.to_string()),
            jti: jti.to_string(),
            iat,
            exp: iat + ttl,
            scope: token::SCOPE_ROOM.into(),
            epoch,
        };
        self.record_issued(jti, community_id, room_id, claims.exp)
            .await?;
        let token = keys
            .sign(&claims)
            .map_err(|e| RoomError::Backend(e.to_string()))?;
        Ok(IssuedToken {
            token,
            token_id: jti,
            expires_at: claims.exp,
        })
    }

    async fn record_issued(
        &self,
        jti: Uuid,
        community_id: &str,
        room_id: Uuid,
        exp: i64,
    ) -> Result<(), RoomError> {
        let mut issued = self.issued.lock().await;
        issued.prune(now());
        if issued.per_community.get(community_id).copied().unwrap_or(0) >= MAX_ISSUED_PER_COMMUNITY
        {
            return Err(RoomError::LimitReached(format!(
                "{MAX_ISSUED_PER_COMMUNITY} live tokens per community"
            )));
        }
        if issued.by_jti.len() >= MAX_ISSUED {
            return Err(RoomError::LimitReached("issued-token ledger full".into()));
        }
        issued.insert(jti, community_id, room_id, exp);
        Ok(())
    }

    /// Issues a short-lived WHEP viewer token for a pipeline stream owned by
    /// `community_id` (the stream must currently be registered to that community).
    pub async fn issue_pipeline_token(
        &self,
        community_id: &str,
        pipeline_id: Uuid,
        ttl_secs: Option<i64>,
    ) -> Result<(IssuedToken, Uuid), RoomError> {
        let keys = self.keys()?;
        let whep = self.whep.as_ref().ok_or(RoomError::Unconfigured(
            "WHEP is not attached to the room service",
        ))?;
        if whep.owner_of(pipeline_id).await.as_deref() != Some(community_id) {
            return Err(RoomError::NotFound);
        }
        let ttl = ttl_secs.unwrap_or(MAX_PIPELINE_TOKEN_TTL_SECS);
        if !(token::MIN_TTL_SECS..=MAX_PIPELINE_TOKEN_TTL_SECS).contains(&ttl) {
            return Err(RoomError::Invalid(format!(
                "ttl_secs must be {}..={MAX_PIPELINE_TOKEN_TTL_SECS}",
                token::MIN_TTL_SECS
            )));
        }
        let (iat, jti, participant) = (now(), Uuid::new_v4(), Uuid::new_v4());
        let claims = RoomClaims {
            iss: token::ROOM_TOKEN_ISSUER.into(),
            aud: token::ROOM_TOKEN_AUDIENCE.into(),
            sub: participant.to_string(),
            room: pipeline_id.to_string(),
            community: community_id.to_string(),
            publish: false,
            subscribe: true,
            source: None,
            jti: jti.to_string(),
            iat,
            exp: iat + ttl,
            scope: token::SCOPE_PIPELINE.into(),
            epoch: whep.epoch_of(pipeline_id).await,
        };
        let token = keys
            .sign(&claims)
            .map_err(|e| RoomError::Backend(e.to_string()))?;
        Ok((
            IssuedToken {
                token,
                token_id: jti,
                expires_at: claims.exp,
            },
            participant,
        ))
    }

    /// Revokes one token id. Honored only for a token this `community_id` +
    /// `room_id` issued (anything else is `NotFound`, indistinguishable from an
    /// unknown id), bounded per community.
    pub async fn revoke_token(
        &self,
        community_id: &str,
        room_id: Uuid,
        token_id: Uuid,
    ) -> Result<(), RoomError> {
        let exp = {
            let issued = self.issued.lock().await;
            match issued.get(&token_id) {
                Some((c, r, exp)) if c == community_id && *r == room_id => *exp,
                _ => return Err(RoomError::NotFound),
            }
        };
        let mut guard = self.revoked.lock().await;
        let (map, order) = &mut *guard;
        let t = now();
        while let Some(front) = order.front().copied() {
            match map.get(&front) {
                Some((_, e)) if *e >= t => break,
                _ => {
                    order.pop_front();
                    map.remove(&front);
                }
            }
        }
        if map.contains_key(&token_id) {
            return Ok(());
        }
        if map
            .values()
            .filter(|(c, e)| c == community_id && *e >= t)
            .count()
            >= MAX_REVOKED_PER_COMMUNITY
        {
            return Err(RoomError::LimitReached(format!(
                "{MAX_REVOKED_PER_COMMUNITY} live revocations per community; use revoke_all"
            )));
        }
        if map.len() >= MAX_REVOKED {
            return Err(RoomError::LimitReached("revocation list full".into()));
        }
        map.insert(token_id, (community_id.to_string(), exp));
        order.push_back(token_id);
        drop(guard);
        let closed = self.close_matching(room_id, |_, jti| jti == token_id).await;
        if closed > 0 {
            tracing::info!(%room_id, community_id, closed, "rooms: revoked token closed live sessions");
        }
        Ok(())
    }

    /// Bumps the room's epoch, invalidating every token issued before; returns it.
    pub async fn revoke_all(&self, community_id: &str, room_id: Uuid) -> Result<u64, RoomError> {
        let mut rooms = self.rooms.write().await;
        match rooms.get_mut(&room_id) {
            Some(r) if r.community_id == community_id => {
                r.epoch += 1;
                Ok(r.epoch)
            }
            _ => Err(RoomError::NotFound),
        }
    }

    /// Verifies a presented token against the live room: signature/expiry,
    /// scope, revocation, room epoch, room id, community and room existence.
    pub async fn authorize(&self, token: &str, room_id: Uuid) -> Result<RoomClaims, RoomError> {
        let keys = self.keys()?;
        let claims = keys.verify(token).map_err(|e| match e {
            TokenError::NotConfigured => RoomError::Unconfigured("room token secret"),
            _ => RoomError::Unauthorized,
        })?;
        if claims.scope != token::SCOPE_ROOM || claims.room != room_id.to_string() {
            return Err(RoomError::Unauthorized);
        }
        let jti = Uuid::parse_str(&claims.jti).map_err(|_| RoomError::Unauthorized)?;
        if self.revoked.lock().await.0.contains_key(&jti) {
            return Err(RoomError::Unauthorized);
        }
        let rooms = self.rooms.read().await;
        match rooms.get(&room_id) {
            Some(r)
                if r.community_id == claims.community
                    && r.epoch == claims.epoch
                    && !Uuid::parse_str(&claims.sub).is_ok_and(|p| r.kicked.contains(&p)) =>
            {
                Ok(claims)
            }
            _ => Err(RoomError::Unauthorized),
        }
    }

    /// Registers (replacing any prior) a publisher; returns the session id.
    pub(crate) async fn add_publisher(
        &self,
        room_id: Uuid,
        participant: Uuid,
        fanouts: MediaFanouts,
        pc: Arc<dyn PeerConnection>,
        ended: Arc<AtomicBool>,
        grant: SessionGrant,
    ) -> Result<Uuid, RoomError> {
        let (session, stale) = {
            let mut rooms = self.rooms.write().await;
            let room = rooms.get_mut(&room_id).ok_or(RoomError::NotFound)?;
            Self::join_allowed(room, participant)?;
            if room.muted.contains(&participant) {
                fanouts.set_muted(true);
            }
            if !room.publishers.contains_key(&participant)
                && room.publishers.len() >= room.max_publishers
            {
                return Err(RoomError::LimitReached("room publisher limit".into()));
            }
            let session = Uuid::new_v4();
            room.joined.entry(participant).or_insert_with(now);
            room.empty_since = None;
            let stale = room.publishers.insert(
                participant,
                PublisherEntry {
                    fanouts,
                    pc,
                    session,
                    ended,
                    expires_at: grant.expires_at,
                    jti: grant.jti,
                },
            );
            (session, stale)
        };
        if let Some(old) = stale {
            let _ = old.pc.close().await;
        }
        Ok(session)
    }

    /// Registers a subscriber session. One active subscriber session per
    /// participant per room: a new one replaces (and closes) the old. Capped per
    /// room and globally.
    pub(crate) async fn add_subscriber(
        &self,
        room_id: Uuid,
        participant: Uuid,
        pc: Arc<dyn PeerConnection>,
        ended: Arc<AtomicBool>,
        grant: SessionGrant,
    ) -> Result<Uuid, RoomError> {
        let (session, stale) = {
            let mut rooms = self.rooms.write().await;
            let total: usize = rooms.values().map(|r| r.subscribers.len()).sum();
            let community = rooms
                .get(&room_id)
                .ok_or(RoomError::NotFound)?
                .community_id
                .clone();
            let in_community: usize = rooms
                .values()
                .filter(|r| r.community_id == community)
                .map(|r| r.subscribers.len())
                .sum();
            let room = rooms.get_mut(&room_id).ok_or(RoomError::NotFound)?;
            Self::join_allowed(room, participant)?;
            let old: Vec<Uuid> = room
                .subscribers
                .iter()
                .filter(|(_, s)| s.participant == participant)
                .map(|(k, _)| *k)
                .collect();
            let replacing = !old.is_empty();
            if room.subscribers.len() - old.len() >= MAX_SUBSCRIBERS_PER_ROOM {
                return Err(RoomError::LimitReached("room subscriber limit".into()));
            }
            if !replacing && in_community >= self.max_community_subscribers {
                return Err(RoomError::LimitReached("community subscriber limit".into()));
            }
            if !replacing && total >= MAX_SUBSCRIBERS_GLOBAL {
                return Err(RoomError::LimitReached("global subscriber limit".into()));
            }
            let stale: Vec<Arc<dyn PeerConnection>> = old
                .iter()
                .filter_map(|k| room.subscribers.remove(k).map(|s| s.pc))
                .collect();
            let session = Uuid::new_v4();
            room.joined.entry(participant).or_insert_with(now);
            room.empty_since = None;
            room.subscribers.insert(
                session,
                SubscriberEntry {
                    pc,
                    participant,
                    ended,
                    expires_at: grant.expires_at,
                    jti: grant.jti,
                },
            );
            (session, stale)
        };
        for pc in stale {
            self.metrics
                .sessions_active
                .with_label_values(&["room_whep"])
                .dec();
            let _ = pc.close().await;
        }
        Ok(session)
    }

    /// Reaps every session whose peer connection has ended (its state hook set
    /// the `ended` flag). Backstop for a hook that fired before the session was
    /// registered; a dead session must never stay counted against the caps.
    /// Returns how many were reaped.
    pub async fn sweep(&self) -> usize {
        self.sweep_at(now()).await
    }

    /// [`Self::sweep`] as of `at` (unix seconds): also reaps sessions whose token
    /// has expired -- webrtc-rs gives no reliable end-of-peer signal after a
    /// silent client disappearance, so the credential lifetime bounds the session.
    pub async fn sweep_at(&self, at: i64) -> usize {
        let mut dead: Vec<(Uuid, Uuid)> = Vec::new();
        {
            let rooms = self.rooms.read().await;
            for (rid, room) in rooms.iter() {
                for (sid, s) in &room.subscribers {
                    if s.ended.load(Ordering::SeqCst) || s.expires_at <= at {
                        dead.push((*rid, *sid));
                    }
                }
                for p in room.publishers.values() {
                    if p.ended.load(Ordering::SeqCst) || p.expires_at <= at {
                        dead.push((*rid, p.session));
                    }
                }
            }
        }
        for (rid, sid) in &dead {
            self.reap_session(*rid, *sid).await;
        }
        dead.len()
    }

    /// Spawns the periodic reaper (every `every`): [`Self::sweep`] dead sessions first, so a
    /// room whose last peer just died is seen as empty, then [`Self::expire_idle`].
    pub fn spawn_sweeper(self: &Arc<Self>, every: Duration) {
        let svc = Arc::clone(self);
        tokio::spawn(async move {
            let mut t = tokio::time::interval(every);
            loop {
                t.tick().await;
                let n = svc.sweep().await;
                if n > 0 {
                    tracing::info!(reaped = n, "rooms: swept dead peer connections");
                }
                svc.expire_idle().await;
            }
        });
    }

    /// [`Self::expire_idle_at`] as of now.
    pub async fn expire_idle(&self) -> usize {
        self.expire_idle_at(now()).await
    }

    /// Deletes every room that has been empty for its `empty_timeout_secs` as of `at` (unix
    /// seconds; tests pass a controlled clock). A room is empty while it has no publisher and
    /// no subscriber session; the clock starts at creation, or at the first pass that observes
    /// it empty after its last session went away, and restarts on a join. `0` never expires.
    /// Hands, lock state and issued-token ledger rows go with the room. Returns how many
    /// rooms were expired; each emits an INFO log (numeric ids only) and the
    /// `rooms_expired_total` counter.
    pub async fn expire_idle_at(&self, at: i64) -> usize {
        let expired: Vec<(Uuid, Room)> = {
            let mut rooms = self.rooms.write().await;
            let mut doomed: Vec<Uuid> = Vec::new();
            for (id, room) in rooms.iter_mut() {
                if room.occupied() {
                    room.empty_since = None;
                    continue;
                }
                let since = *room.empty_since.get_or_insert(at);
                if room.empty_timeout_secs > 0
                    && at.saturating_sub(since) >= i64::from(room.empty_timeout_secs)
                {
                    doomed.push(*id);
                }
            }
            doomed
                .into_iter()
                .filter_map(|id| rooms.remove(&id).map(|r| (id, r)))
                .collect()
        };
        if expired.is_empty() {
            return 0;
        }
        {
            let mut issued = self.issued.lock().await;
            for (id, _) in &expired {
                issued.forget_room(*id);
            }
        }
        for (id, room) in &expired {
            self.count_hands_cleared("expiry", room.hands.len());
            self.metrics.rooms_expired_total.inc();
            tracing::info!(
                room_id = %id,
                community_id = %room.community_id,
                empty_timeout_secs = room.empty_timeout_secs,
                locked = room.locked,
                hands_dropped = room.hands.len(),
                "rooms: expired idle room"
            );
        }
        expired.len()
    }

    /// Adds `n` to the cleared-hands counter under `reason` (no-op for 0).
    fn count_hands_cleared(&self, reason: &'static str, n: usize) {
        if n > 0 {
            self.metrics
                .hands_cleared_total
                .with_label_values(&[reason])
                .inc_by(n as u64);
        }
    }

    /// Removes a session whose peer connection ended (Failed/Closed/Disconnected)
    /// and closes it; a no-op when it is already gone.
    pub(crate) async fn reap_session(&self, room_id: Uuid, session: Uuid) {
        let removed = {
            let mut rooms = self.rooms.write().await;
            let Some(room) = rooms.get_mut(&room_id) else {
                return;
            };
            let removed = if let Some(s) = room.subscribers.remove(&session) {
                Some((s.pc, "room_whep"))
            } else {
                let pid = room
                    .publishers
                    .iter()
                    .find(|(_, p)| p.session == session)
                    .map(|(k, _)| *k);
                pid.and_then(|k| room.publishers.remove(&k).map(|p| (p.pc, "room_whip")))
            };
            room.prune_joined();
            removed
        };
        if let Some((pc, kind)) = removed {
            self.metrics
                .sessions_active
                .with_label_values(&[kind])
                .dec();
            let _ = pc.close().await;
        }
    }

    /// Resolves which publisher's fanouts a subscriber attaches to.
    pub(crate) async fn source_fanouts(
        &self,
        room_id: Uuid,
        source: Option<Uuid>,
    ) -> Result<(Uuid, MediaFanouts), RoomError> {
        let rooms = self.rooms.read().await;
        let room = rooms.get(&room_id).ok_or(RoomError::NotFound)?;
        match source {
            Some(s) => room
                .publishers
                .get(&s)
                .map(|p| (s, p.fanouts.clone()))
                .ok_or(RoomError::NotFound),
            None => {
                let mut it = room.publishers.iter();
                match (it.next(), it.next()) {
                    (Some((id, p)), None) => Ok((*id, p.fanouts.clone())),
                    (None, _) => Err(RoomError::NotFound),
                    _ => Err(RoomError::Ambiguous(
                        "several publishers; pass ?source=<participant id>".into(),
                    )),
                }
            }
        }
    }

    /// Ends one session (publisher or subscriber) owned by `participant`.
    pub(crate) async fn end_session(
        &self,
        room_id: Uuid,
        session: Uuid,
        participant: Uuid,
    ) -> Result<(), RoomError> {
        let pc = {
            let mut rooms = self.rooms.write().await;
            let room = rooms.get_mut(&room_id).ok_or(RoomError::NotFound)?;
            let removed = if room
                .subscribers
                .get(&session)
                .is_some_and(|s| s.participant == participant)
            {
                room.subscribers
                    .remove(&session)
                    .map(|s| (s.pc, "room_whep"))
            } else if room
                .publishers
                .get(&participant)
                .is_some_and(|p| p.session == session)
            {
                room.publishers
                    .remove(&participant)
                    .map(|p| (p.pc, "room_whip"))
            } else {
                None
            };
            room.prune_joined();
            removed
        };
        match pc {
            Some((pc, kind)) => {
                self.metrics
                    .sessions_active
                    .with_label_values(&[kind])
                    .dec();
                let _ = pc.close().await;
                Ok(())
            }
            None => Err(RoomError::NotFound),
        }
    }

    /// Time budget for ICE gathering on room signaling.
    pub(crate) const GATHER_TIMEOUT: Duration = Duration::from_secs(5);
}

/// Moderation surface ported from Go `module_rtc` (`handlers.go`,
/// `call_features.go`): participants/rooms listing, leave, kick, server-side
/// mute and room lock, all community-scoped and gated by the `rooms.moderation`
/// flag. Authorization is the interim `actor_role` model (see [`ActorRole`]).
impl RoomService {
    /// Counts a moderation op's outcome and logs it (ids only, never names).
    fn observe<T>(&self, op: &'static str, r: Result<T, RoomError>) -> Result<T, RoomError> {
        let outcome = if r.is_ok() { "ok" } else { "refused" };
        self.metrics
            .moderation_ops_total
            .with_label_values(&[op, outcome])
            .inc();
        match &r {
            Ok(_) => tracing::info!(op, "rooms: moderation op applied"),
            Err(e) => tracing::info!(op, error = %e, "rooms: moderation op refused"),
        }
        r
    }

    /// Fails loud when `rooms.moderation` is OFF.
    fn require_enabled(&self) -> Result<(), RoomError> {
        if self.moderation_enabled {
            Ok(())
        } else {
            Err(RoomError::Unsupported(
                "feature rooms.moderation is disabled".into(),
            ))
        }
    }

    /// Flag check plus the moderator-role requirement for staff-only ops.
    fn require_moderator(&self, role: ActorRole) -> Result<(), RoomError> {
        self.require_enabled()?;
        if role.is_moderator() {
            Ok(())
        } else {
            Err(RoomError::Forbidden("moderator role required"))
        }
    }

    /// Refuses a join into a locked room or by a kicked participant.
    fn join_allowed(room: &Room, participant: Uuid) -> Result<(), RoomError> {
        if room.locked {
            return Err(RoomError::Forbidden("room is locked"));
        }
        if room.kicked.contains(&participant) {
            return Err(RoomError::Forbidden(
                "participant was removed from this room",
            ));
        }
        Ok(())
    }

    /// Removes every live session in `room_id` for which `pred(participant, jti)`
    /// holds and closes its peer connection; returns how many were closed.
    async fn close_matching(&self, room_id: Uuid, pred: impl Fn(Uuid, Uuid) -> bool) -> usize {
        let doomed: Vec<(Arc<dyn PeerConnection>, &'static str)> = {
            let mut rooms = self.rooms.write().await;
            let Some(room) = rooms.get_mut(&room_id) else {
                return 0;
            };
            let sub_keys: Vec<Uuid> = room
                .subscribers
                .iter()
                .filter(|(_, s)| pred(s.participant, s.jti))
                .map(|(k, _)| *k)
                .collect();
            let pub_keys: Vec<Uuid> = room
                .publishers
                .iter()
                .filter(|(k, p)| pred(**k, p.jti))
                .map(|(k, _)| *k)
                .collect();
            let mut v = Vec::new();
            for k in sub_keys {
                if let Some(s) = room.subscribers.remove(&k) {
                    v.push((s.pc, "room_whep"));
                }
            }
            for k in pub_keys {
                if let Some(p) = room.publishers.remove(&k) {
                    v.push((p.pc, "room_whip"));
                }
            }
            room.prune_joined();
            v
        };
        let n = doomed.len();
        for (pc, kind) in doomed {
            self.metrics
                .sessions_active
                .with_label_values(&[kind])
                .dec();
            self.metrics.moderation_sessions_closed_total.inc();
            let _ = pc.close().await;
        }
        n
    }

    /// Lists every live room `community_id` owns (ids and counts only).
    #[tracing::instrument(skip_all, fields(op = "list", community_id = span_cid(community_id)))]
    pub async fn list_rooms(&self, community_id: &str) -> Result<Vec<RoomInfo>, RoomError> {
        let rooms = self.rooms.read().await;
        let mut v: Vec<RoomInfo> = rooms
            .iter()
            .filter(|(_, r)| r.community_id == community_id)
            .map(|(id, r)| Self::info(*id, r))
            .collect();
        v.sort_by_key(|r| (r.created_at, r.room_id));
        self.observe("list", Ok(v))
    }

    /// Lists a room's participants by UUID (publisher/subscriber/muted flags).
    #[tracing::instrument(skip_all, fields(op = "participants", %room_id, community_id = span_cid(community_id)))]
    pub async fn participants(
        &self,
        community_id: &str,
        room_id: Uuid,
    ) -> Result<Vec<ParticipantInfo>, RoomError> {
        let rooms = self.rooms.read().await;
        let r = match rooms.get(&room_id) {
            Some(r) if r.community_id == community_id => {
                let mut m: BTreeMap<Uuid, ParticipantInfo> = BTreeMap::new();
                for (id, p) in &r.publishers {
                    m.insert(
                        *id,
                        ParticipantInfo {
                            participant_id: *id,
                            publisher: true,
                            subscriber: false,
                            muted: p.fanouts.is_muted() || r.muted.contains(id),
                            joined_at: r.joined.get(id).copied(),
                        },
                    );
                }
                for s in r.subscribers.values() {
                    m.entry(s.participant)
                        .and_modify(|e| e.subscriber = true)
                        .or_insert(ParticipantInfo {
                            participant_id: s.participant,
                            publisher: false,
                            subscriber: true,
                            muted: r.muted.contains(&s.participant),
                            joined_at: r.joined.get(&s.participant).copied(),
                        });
                }
                Ok(m.into_values().collect())
            }
            _ => Err(RoomError::NotFound),
        };
        self.observe("participants", r)
    }

    /// Ends a participant's live sessions. Self-service, or by a moderator
    /// (`actor_id != participant`); does not blocklist. Returns sessions closed.
    #[tracing::instrument(skip_all, fields(op = "leave", %room_id, community_id = span_cid(community_id)))]
    pub async fn leave(
        &self,
        community_id: &str,
        room_id: Uuid,
        participant: Uuid,
        actor_id: Uuid,
        actor_role: ActorRole,
    ) -> Result<usize, RoomError> {
        let r = async {
            self.get(community_id, room_id).await?;
            if actor_id != participant {
                self.require_moderator(actor_role)?;
            }
            let closed = self.close_matching(room_id, |p, _| p == participant).await;
            self.clear_hand(room_id, participant, "leave").await;
            self.drop_member(room_id, participant).await;
            Ok(closed)
        }
        .await;
        self.observe("leave", r)
    }

    /// Removes a participant: closes their live peers and blocklists them for
    /// this room so their tokens stop authorizing. Moderator only; not self.
    #[tracing::instrument(skip_all, fields(op = "kick", %room_id, community_id = span_cid(community_id)))]
    pub async fn kick(
        &self,
        community_id: &str,
        room_id: Uuid,
        participant: Uuid,
        actor_id: Uuid,
        actor_role: ActorRole,
    ) -> Result<usize, RoomError> {
        let r = async {
            self.require_moderator(actor_role)?;
            if actor_id == participant {
                return Err(RoomError::Invalid(
                    "a participant cannot kick themselves".into(),
                ));
            }
            {
                let mut rooms = self.rooms.write().await;
                match rooms.get_mut(&room_id) {
                    Some(room) if room.community_id == community_id => {
                        if !room.kicked.contains(&participant)
                            && room.kicked.len() >= MAX_MODERATED_PER_ROOM
                        {
                            return Err(RoomError::LimitReached(format!(
                                "{MAX_MODERATED_PER_ROOM} kicked participants per room"
                            )));
                        }
                        room.kicked.insert(participant);
                        room.muted.remove(&participant);
                        room.members.remove(&participant);
                        let before = room.hands.len();
                        room.hands.retain(|h| h.participant != participant);
                        self.count_hands_cleared("kick", before - room.hands.len());
                    }
                    _ => return Err(RoomError::NotFound),
                }
            }
            Ok(self.close_matching(room_id, |p, _| p == participant).await)
        }
        .await;
        self.observe("kick", r)
    }

    /// Server-side mutes (or unmutes) one participant: their fanout stops
    /// forwarding, enforced in the SFU rather than trusting the client.
    #[tracing::instrument(skip_all, fields(op = "mute", %room_id, community_id = span_cid(community_id)))]
    pub async fn set_muted(
        &self,
        community_id: &str,
        room_id: Uuid,
        participant: Uuid,
        muted: bool,
        actor_role: ActorRole,
    ) -> Result<(), RoomError> {
        let op = if muted { "mute" } else { "unmute" };
        let r = async {
            self.require_moderator(actor_role)?;
            let mut rooms = self.rooms.write().await;
            let room = match rooms.get_mut(&room_id) {
                Some(r) if r.community_id == community_id => r,
                _ => return Err(RoomError::NotFound),
            };
            if muted {
                if !room.muted.contains(&participant) && room.muted.len() >= MAX_MODERATED_PER_ROOM
                {
                    return Err(RoomError::LimitReached(format!(
                        "{MAX_MODERATED_PER_ROOM} muted participants per room"
                    )));
                }
                room.muted.insert(participant);
            } else {
                room.muted.remove(&participant);
            }
            if let Some(p) = room.publishers.get(&participant) {
                p.fanouts.set_muted(muted);
            }
            Ok(())
        }
        .await;
        self.observe(op, r)
    }

    /// Mutes every current publisher except `actor_id`; returns how many.
    #[tracing::instrument(skip_all, fields(op = "mute_all", %room_id, community_id = span_cid(community_id)))]
    pub async fn mute_all(
        &self,
        community_id: &str,
        room_id: Uuid,
        actor_id: Uuid,
        actor_role: ActorRole,
    ) -> Result<usize, RoomError> {
        let r = async {
            self.require_moderator(actor_role)?;
            let mut rooms = self.rooms.write().await;
            let room = match rooms.get_mut(&room_id) {
                Some(r) if r.community_id == community_id => r,
                _ => return Err(RoomError::NotFound),
            };
            let targets: Vec<Uuid> = room
                .publishers
                .keys()
                .copied()
                .filter(|p| *p != actor_id)
                .collect();
            for t in &targets {
                room.muted.insert(*t);
                if let Some(p) = room.publishers.get(t) {
                    p.fanouts.set_muted(true);
                }
            }
            Ok(targets.len())
        }
        .await;
        self.observe("mute_all", r)
    }

    /// Locks or unlocks a room: locked refuses token issuance and new joins.
    #[tracing::instrument(skip_all, fields(op = "lock", %room_id, community_id = span_cid(community_id)))]
    pub async fn set_locked(
        &self,
        community_id: &str,
        room_id: Uuid,
        locked: bool,
        actor_role: ActorRole,
    ) -> Result<(), RoomError> {
        let op = if locked { "lock" } else { "unlock" };
        let r = async {
            self.require_moderator(actor_role)?;
            let mut rooms = self.rooms.write().await;
            match rooms.get_mut(&room_id) {
                Some(room) if room.community_id == community_id => {
                    room.locked = locked;
                    Ok(())
                }
                _ => Err(RoomError::NotFound),
            }
        }
        .await;
        self.observe(op, r)
    }
}

/// The raised-hand queue (Go `call_features.go` parity, authoritative here).
///
/// Authorization is NEVER taken from a role the caller states. Every operation names an
/// `actor` (the end user acting, UUID) and is checked against the server-side member table
/// ([`Member`]), which is filled when a participant is issued a join token and whose `staff`
/// flag only a verified-authority issuer can set ([`IssueRequest::staff`]):
///
/// * raise / lower-own / list-own: the actor must have joined this room, and for raise and
///   lower-own `participant == actor`;
/// * lower-other / list-all / acknowledge / reset: the actor must be recorded STAFF (and the
///   `rooms.moderation` flag on). The transport additionally demands the moderate scope
///   (gRPC: the verified service JWT; host API: the tool's manifest scope).
///
/// Participant ids are UUIDs, never names. The queue is cleared per participant on
/// leave/kick and wholesale on room delete/expiry/reset.
impl RoomService {
    /// The member record of `actor` in `room`, or `Forbidden` when they never joined.
    fn member_of(room: &Room, actor: Uuid) -> Result<Member, RoomError> {
        room.members
            .get(&actor)
            .copied()
            .ok_or(RoomError::Forbidden("actor has not joined this room"))
    }

    /// Staff gate: the `rooms.moderation` flag is on and `actor` is recorded staff.
    fn require_staff(&self, room: &Room, actor: Uuid) -> Result<(), RoomError> {
        self.require_enabled()?;
        if Self::member_of(room, actor)?.staff {
            Ok(())
        } else {
            Err(RoomError::Forbidden("moderator role required"))
        }
    }

    /// Drops `participant` from the member table of `room_id` (they left).
    async fn drop_member(&self, room_id: Uuid, participant: Uuid) {
        if let Some(room) = self.rooms.write().await.get_mut(&room_id) {
            room.members.remove(&participant);
        }
    }

    /// Registers `actor` as room STAFF. Only for a caller that has VERIFIED moderation
    /// authority (the host API's `room.staff.register`, gated by the tool's manifest scope
    /// `community:moderate`); never reachable from a participant-supplied field.
    #[tracing::instrument(skip_all, fields(op = "staff_register", %room_id, community_id = span_cid(community_id)))]
    pub async fn register_staff(
        &self,
        community_id: &str,
        room_id: Uuid,
        actor: Uuid,
    ) -> Result<(), RoomError> {
        let r = async {
            self.require_enabled()?;
            let mut rooms = self.rooms.write().await;
            let room = match rooms.get_mut(&room_id) {
                Some(r) if r.community_id == community_id => r,
                _ => return Err(RoomError::NotFound),
            };
            if room.kicked.contains(&actor) {
                return Err(RoomError::Forbidden(
                    "participant was removed from this room",
                ));
            }
            if !room.members.contains_key(&actor) && room.members.len() >= MAX_MEMBERS_PER_ROOM {
                return Err(RoomError::LimitReached(format!(
                    "{MAX_MEMBERS_PER_ROOM} participants per room"
                )));
            }
            room.members.insert(actor, Member { staff: true });
            Ok(())
        }
        .await;
        self.observe("staff_register", r)
    }

    /// Drops `participant`'s raised hand from `room_id` (if any), counting `reason`.
    async fn clear_hand(&self, room_id: Uuid, participant: Uuid, reason: &'static str) {
        let removed = {
            let mut rooms = self.rooms.write().await;
            rooms.get_mut(&room_id).map_or(0, |room| {
                let before = room.hands.len();
                room.hands.retain(|h| h.participant != participant);
                before - room.hands.len()
            })
        };
        self.count_hands_cleared(reason, removed);
    }

    /// Raises the actor's OWN hand (`participant` must equal `actor`, who must have joined).
    /// Idempotent: an already-raised hand keeps its original position and time. Kicked
    /// participants are refused; the queue is bounded.
    #[tracing::instrument(skip_all, fields(op = "hand_raise", %room_id, community_id = span_cid(community_id)))]
    pub async fn raise_hand(
        &self,
        community_id: &str,
        room_id: Uuid,
        participant: Uuid,
        actor: Uuid,
    ) -> Result<HandInfo, RoomError> {
        let r = async {
            let mut rooms = self.rooms.write().await;
            let room = match rooms.get_mut(&room_id) {
                Some(r) if r.community_id == community_id => r,
                _ => return Err(RoomError::NotFound),
            };
            if actor != participant {
                return Err(RoomError::Forbidden(
                    "a hand can only be raised by its own participant",
                ));
            }
            Self::member_of(room, actor)?;
            if room.kicked.contains(&participant) {
                return Err(RoomError::Forbidden(
                    "participant was removed from this room",
                ));
            }
            if let Some(h) = room.hands.iter().find(|h| h.participant == participant) {
                return Ok(h.info());
            }
            if room.hands.len() >= MAX_HANDS_PER_ROOM {
                return Err(RoomError::LimitReached(format!(
                    "{MAX_HANDS_PER_ROOM} raised hands per room"
                )));
            }
            // Never earlier than the previous hand, so queue order == raised_at order
            // even if the wall clock steps backwards.
            let raised_at = room
                .hands
                .last()
                .map_or_else(now, |last| now().max(last.raised_at));
            let hand = HandEntry {
                participant,
                raised_at,
                acknowledged_at: None,
                acknowledged_by: None,
            };
            let info = hand.info();
            room.hands.push(hand);
            self.metrics.hands_raised_total.inc();
            Ok(info)
        }
        .await;
        self.observe("hand_raise", r)
    }

    /// Lowers `participant`'s hand; returns whether one was raised (lowering a hand that is
    /// not raised is a no-op, not an error). Lowering your own needs only to have joined;
    /// lowering someone else's needs staff.
    #[tracing::instrument(skip_all, fields(op = "hand_lower", %room_id, community_id = span_cid(community_id)))]
    pub async fn lower_hand(
        &self,
        community_id: &str,
        room_id: Uuid,
        participant: Uuid,
        actor: Uuid,
    ) -> Result<bool, RoomError> {
        let r = async {
            let removed = {
                let mut rooms = self.rooms.write().await;
                let room = match rooms.get_mut(&room_id) {
                    Some(r) if r.community_id == community_id => r,
                    _ => return Err(RoomError::NotFound),
                };
                if actor == participant {
                    Self::member_of(room, actor)?;
                } else {
                    self.require_staff(room, actor)?;
                }
                let before = room.hands.len();
                room.hands.retain(|h| h.participant != participant);
                before - room.hands.len()
            };
            self.count_hands_cleared("lower", removed);
            Ok(removed > 0)
        }
        .await;
        self.observe("hand_lower", r)
    }

    /// Lists raised hands (oldest first) as `view` allows. The actor must have joined; the
    /// whole queue ([`HandView::All`], or [`HandView::Auto`] for staff) is staff-only,
    /// everyone else sees only their own entry plus the queue length and their position.
    #[tracing::instrument(skip_all, fields(op = "hand_list", %room_id, community_id = span_cid(community_id)))]
    pub async fn list_hands(
        &self,
        community_id: &str,
        room_id: Uuid,
        actor: Uuid,
        view: HandView,
    ) -> Result<HandList, RoomError> {
        let r = async {
            let rooms = self.rooms.read().await;
            let room = match rooms.get(&room_id) {
                Some(r) if r.community_id == community_id => r,
                _ => return Err(RoomError::NotFound),
            };
            let member = Self::member_of(room, actor)?;
            let all = match view {
                HandView::Own => false,
                HandView::All => {
                    self.require_staff(room, actor)?;
                    true
                }
                HandView::Auto => member.staff && self.require_enabled().is_ok(),
            };
            let mut queue: Vec<HandInfo> = room.hands.iter().map(HandEntry::info).collect();
            queue.sort_by_key(|h| h.raised_at);
            let position = queue
                .iter()
                .position(|h| h.participant_id == actor)
                .map(|i| i + 1);
            let count = queue.len();
            let hands = if all {
                queue
            } else {
                queue
                    .into_iter()
                    .filter(|h| h.participant_id == actor)
                    .collect()
            };
            Ok(HandList {
                hands,
                count,
                position,
            })
        }
        .await;
        self.observe("hand_list", r)
    }

    /// Marks `participant`'s raised hand acknowledged by staff `actor`. Idempotent (the
    /// first acknowledgement is kept); a participant with no raised hand is `Invalid`
    /// (nothing to acknowledge), never a silent success.
    #[tracing::instrument(skip_all, fields(op = "hand_ack", %room_id, community_id = span_cid(community_id)))]
    pub async fn acknowledge_hand(
        &self,
        community_id: &str,
        room_id: Uuid,
        participant: Uuid,
        actor: Uuid,
    ) -> Result<HandInfo, RoomError> {
        let r = async {
            let mut rooms = self.rooms.write().await;
            let room = match rooms.get_mut(&room_id) {
                Some(r) if r.community_id == community_id => r,
                _ => return Err(RoomError::NotFound),
            };
            self.require_staff(room, actor)?;
            let hand = room
                .hands
                .iter_mut()
                .find(|h| h.participant == participant)
                .ok_or_else(|| RoomError::Invalid("participant has no raised hand".into()))?;
            if hand.acknowledged_at.is_none() {
                let t = now();
                hand.acknowledged_at = Some(t);
                hand.acknowledged_by = Some(actor);
                self.metrics.hands_acknowledged_total.inc();
                self.metrics
                    .hand_ack_wait_seconds
                    .observe((t - hand.raised_at).max(0) as f64);
            }
            Ok(hand.info())
        }
        .await;
        self.observe("hand_ack", r)
    }

    /// Clears the whole raised-hand queue; returns how many hands were cleared. Staff only.
    #[tracing::instrument(skip_all, fields(op = "hand_reset", %room_id, community_id = span_cid(community_id)))]
    pub async fn reset_hands(
        &self,
        community_id: &str,
        room_id: Uuid,
        actor: Uuid,
    ) -> Result<usize, RoomError> {
        let r = async {
            let cleared = {
                let mut rooms = self.rooms.write().await;
                let room = match rooms.get_mut(&room_id) {
                    Some(r) if r.community_id == community_id => r,
                    _ => return Err(RoomError::NotFound),
                };
                self.require_staff(room, actor)?;
                let n = room.hands.len();
                room.hands.clear();
                n
            };
            self.count_hands_cleared("reset", cleared);
            Ok(cleared)
        }
        .await;
        self.observe("hand_reset", r)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rtc::config::RtcConfig;

    fn svc(secret: Option<&str>) -> RoomService {
        let metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
        let factory = Arc::new(
            PeerConnectionFactory::new(RtcConfig {
                bind_ip: "127.0.0.1".parse().unwrap(),
                udp_port_range: (41800, 41810),
                nat_1to1_ip: None,
            })
            .unwrap(),
        );
        RoomService::new(
            factory,
            metrics,
            secret.map(|_| TokenKeys::generate_for_tests("room-test")),
            "http://localhost".into(),
        )
    }

    const SECRET: &str = "0123456789abcdef0123456789abcdef";

    fn issue(p: bool, s: bool) -> IssueRequest {
        IssueRequest {
            participant: Uuid::new_v4(),
            publish: p,
            subscribe: s,
            source: None,
            ttl_secs: Some(60),
            staff: false,
        }
    }

    #[tokio::test]
    async fn create_get_close_are_community_scoped() {
        let s = svc(Some(SECRET));
        let r = s.create("7", None, None, RouteMode::Sfu).await.unwrap();
        assert!(s.get("7", r.room_id).await.is_ok());
        assert!(matches!(
            s.get("8", r.room_id).await,
            Err(RoomError::NotFound)
        ));
        assert!(matches!(
            s.close("8", r.room_id).await,
            Err(RoomError::NotFound)
        ));
        s.close("7", r.room_id).await.unwrap();
        assert!(matches!(
            s.get("7", r.room_id).await,
            Err(RoomError::NotFound)
        ));
    }

    #[tokio::test]
    async fn validation_and_limits_fail_loud() {
        let s = svc(Some(SECRET));
        assert!(matches!(
            s.create("x", None, None, RouteMode::Sfu).await,
            Err(RoomError::Invalid(_))
        ));
        assert!(matches!(
            s.create("7", None, None, RouteMode::Relay).await,
            Err(RoomError::Unsupported(_))
        ));
        assert!(matches!(
            s.create("7", None, Some(0), RouteMode::Sfu).await,
            Err(RoomError::Invalid(_))
        ));
        for _ in 0..MAX_ROOMS_PER_COMMUNITY {
            s.create("7", None, None, RouteMode::Sfu).await.unwrap();
        }
        assert!(matches!(
            s.create("7", None, None, RouteMode::Sfu).await,
            Err(RoomError::LimitReached(_))
        ));
        s.create("9", None, None, RouteMode::Sfu).await.unwrap();
    }

    #[tokio::test]
    async fn tokens_are_room_scoped_revocable_and_bounded() {
        let s = svc(Some(SECRET));
        let a = s.create("7", None, None, RouteMode::Sfu).await.unwrap();
        let b = s.create("7", None, None, RouteMode::Sfu).await.unwrap();
        let t = s
            .issue_token("7", a.room_id, issue(true, true))
            .await
            .unwrap();
        assert!(s.authorize(&t.token, a.room_id).await.is_ok());
        assert!(matches!(
            s.authorize(&t.token, b.room_id).await,
            Err(RoomError::Unauthorized)
        ));
        s.revoke_token("7", a.room_id, t.token_id).await.unwrap();
        assert!(matches!(
            s.authorize(&t.token, a.room_id).await,
            Err(RoomError::Unauthorized)
        ));
        assert!(matches!(
            s.issue_token("8", a.room_id, issue(true, true)).await,
            Err(RoomError::NotFound)
        ));
        assert!(matches!(
            s.issue_token("7", a.room_id, issue(false, false)).await,
            Err(RoomError::Invalid(_))
        ));
        let mut long = issue(true, false);
        long.ttl_secs = Some(99999);
        assert!(matches!(
            s.issue_token("7", a.room_id, long).await,
            Err(RoomError::Invalid(_))
        ));
        // closed room invalidates outstanding tokens
        let t2 = s
            .issue_token("7", b.room_id, issue(true, true))
            .await
            .unwrap();
        s.close("7", b.room_id).await.unwrap();
        assert!(s.authorize(&t2.token, b.room_id).await.is_err());
    }

    #[tokio::test]
    async fn unconfigured_secret_fails_loud() {
        let s = svc(None);
        let r = s.create("7", None, None, RouteMode::Sfu).await.unwrap();
        assert!(matches!(
            s.issue_token("7", r.room_id, issue(true, true)).await,
            Err(RoomError::Unconfigured(_))
        ));
        assert!(matches!(
            s.authorize("x", r.room_id).await,
            Err(RoomError::Unconfigured(_))
        ));
    }

    fn moderated() -> RoomService {
        svc(Some(SECRET)).with_moderation(true)
    }

    async fn new_room(s: &RoomService, community: &str) -> RoomInfo {
        s.create(community, None, None, RouteMode::Sfu)
            .await
            .unwrap()
    }

    #[test]
    fn idle_policy_validates_and_resolves() {
        let p = IdlePolicy::default();
        assert_eq!((p.default_secs, p.max_secs), (300, 86_400));
        assert_eq!(p.resolve(None), 300, "unset takes the default");
        assert_eq!(p.resolve(Some(0)), 0, "0 means never expire");
        assert_eq!(p.resolve(Some(45)), 45);
        assert_eq!(p.resolve(Some(u32::MAX)), 86_400, "clamped to the maximum");
        assert!(
            IdlePolicy::new(0, 10).is_ok(),
            "default 0 = never is allowed"
        );
        for (d, m) in [(5, 0), (11, 10), (1, EMPTY_TIMEOUT_HARD_MAX_SECS + 1)] {
            assert!(IdlePolicy::new(d, m).is_err(), "({d}, {m}) must be refused");
        }
    }

    #[test]
    fn idle_policy_env_lookup_is_strict() {
        let env = |pairs: &'static [(&'static str, &'static str)]| {
            IdlePolicy::from_lookup(move |k| {
                pairs
                    .iter()
                    .find(|(name, _)| *name == k)
                    .map(|(_, v)| v.to_string())
            })
        };
        assert_eq!(env(&[]).unwrap(), IdlePolicy::default());
        let p = env(&[
            ("ROOMS_EMPTY_TIMEOUT_DEFAULT_SECS", "60"),
            ("ROOMS_EMPTY_TIMEOUT_MAX_SECS", "600"),
        ])
        .unwrap();
        assert_eq!((p.default_secs, p.max_secs), (60, 600));
        // A present-but-invalid value is an error, never silently the default.
        assert!(env(&[("ROOMS_EMPTY_TIMEOUT_DEFAULT_SECS", "soon")]).is_err());
        assert!(env(&[("ROOMS_EMPTY_TIMEOUT_MAX_SECS", "-1")]).is_err());
        assert!(env(&[("ROOMS_EMPTY_TIMEOUT_DEFAULT_SECS", "99999999")]).is_err());
    }

    #[tokio::test]
    async fn idle_room_expires_exactly_at_its_timeout_on_a_controlled_clock() {
        let s = svc(Some(SECRET));
        let r = new_room(&s, "7").await;
        assert_eq!(r.empty_timeout_secs, 300);
        let t0 = r.created_at;
        assert_eq!(s.expire_idle_at(t0 + 299).await, 0, "one second early");
        assert!(s.get("7", r.room_id).await.is_ok());
        assert_eq!(s.expire_idle_at(t0 + 300).await, 1);
        assert!(matches!(
            s.get("7", r.room_id).await,
            Err(RoomError::NotFound)
        ));
        assert_eq!(s.expire_idle_at(t0 + 301).await, 0, "already gone");
        assert_eq!(s.metrics.rooms_expired_total.get(), 1);
    }

    #[tokio::test]
    async fn zero_timeout_never_expires_and_requests_are_clamped() {
        let s = svc(Some(SECRET)).with_idle_policy(IdlePolicy::new(60, 120).unwrap());
        let never = s
            .create_with("7", None, None, RouteMode::Sfu, Some(0))
            .await
            .unwrap();
        let clamped = s
            .create_with("7", None, None, RouteMode::Sfu, Some(10_000))
            .await
            .unwrap();
        let dflt = new_room(&s, "7").await;
        assert_eq!(never.empty_timeout_secs, 0);
        assert_eq!(clamped.empty_timeout_secs, 120);
        assert_eq!(dflt.empty_timeout_secs, 60);
        let far = never.created_at + 10_000_000;
        assert_eq!(s.expire_idle_at(far).await, 2, "only the two timed rooms");
        assert!(s.get("7", never.room_id).await.is_ok());
        assert_eq!(s.expire_idle_at(far * 2).await, 0);
    }

    /// Registers `who` as a joined member (and optionally staff) the way the gRPC `JoinRoom`
    /// does: by being issued a join token.
    async fn join(s: &RoomService, community: &str, room: Uuid, who: Uuid, staff: bool) {
        s.issue_token(
            community,
            room,
            IssueRequest {
                participant: who,
                publish: false,
                subscribe: true,
                source: None,
                ttl_secs: Some(60),
                staff,
            },
        )
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn expiry_drops_hands_locks_and_issued_tokens() {
        let s = moderated();
        let r = new_room(&s, "7").await;
        let who = Uuid::new_v4();
        join(&s, "7", r.room_id, who, true).await;
        s.raise_hand("7", r.room_id, who, who).await.unwrap();
        s.set_locked("7", r.room_id, true, ActorRole::Admin)
            .await
            .unwrap();
        // A sibling room supplies a ledger row of a different room.
        let other = new_room(&s, "7").await;
        s.issue_token("7", other.room_id, issue(true, true))
            .await
            .unwrap();
        assert_eq!(s.issued.lock().await.per_community.get("7"), Some(&2));
        let doomed = s
            .create_with("7", None, None, RouteMode::Sfu, Some(10))
            .await
            .unwrap();
        // A never-expire room survives the same pass that takes the other three.
        let keep = s
            .create_with("7", None, None, RouteMode::Sfu, Some(0))
            .await
            .unwrap();
        let at = r.created_at + 400;
        let expired = s.expire_idle_at(at).await;
        assert_eq!(expired, 3, "r, other and doomed; keep (0) survives");
        assert!(s.get("7", keep.room_id).await.is_ok());
        for gone in [r.room_id, other.room_id, doomed.room_id] {
            assert!(matches!(
                s.list_hands("7", gone, who, HandView::Own).await,
                Err(RoomError::NotFound)
            ));
        }
        assert_eq!(
            s.issued.lock().await.per_community.get("7"),
            None,
            "expired rooms release their token-ledger quota"
        );
        assert_eq!(
            s.metrics
                .hands_cleared_total
                .with_label_values(&["expiry"])
                .get(),
            1
        );
    }

    #[tokio::test]
    async fn hands_are_fifo_idempotent_and_community_scoped() {
        let s = moderated();
        let r = new_room(&s, "7").await;
        let (a, b, staff) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
        for (p, st) in [(a, false), (b, false), (staff, true)] {
            join(&s, "7", r.room_id, p, st).await;
        }
        let first = s.raise_hand("7", r.room_id, a, a).await.unwrap();
        s.raise_hand("7", r.room_id, b, b).await.unwrap();
        let again = s.raise_hand("7", r.room_id, a, a).await.unwrap();
        assert_eq!(
            first, again,
            "raising twice is a no-op that keeps position and time"
        );
        let all = s
            .list_hands("7", r.room_id, staff, HandView::All)
            .await
            .unwrap();
        assert_eq!(
            all.hands
                .iter()
                .map(|h| h.participant_id)
                .collect::<Vec<_>>(),
            vec![a, b]
        );
        assert_eq!(all.count, 2);
        assert!(all
            .hands
            .windows(2)
            .all(|w| w[0].raised_at <= w[1].raised_at));
        assert_eq!(
            s.metrics.hands_raised_total.get(),
            2,
            "re-raise not counted"
        );

        // Another community can neither see nor touch the room's hands: NotFound everywhere,
        // even for staff of the real room.
        for r in [
            s.raise_hand("8", r.room_id, a, a).await.map(|_| ()),
            s.list_hands("8", r.room_id, staff, HandView::All)
                .await
                .map(|_| ()),
            s.lower_hand("8", r.room_id, a, staff).await.map(|_| ()),
            s.acknowledge_hand("8", r.room_id, a, staff)
                .await
                .map(|_| ()),
            s.reset_hands("8", r.room_id, staff).await.map(|_| ()),
        ] {
            assert!(matches!(r, Err(RoomError::NotFound)), "{r:?}");
        }
        assert_eq!(
            s.list_hands("7", r.room_id, staff, HandView::All)
                .await
                .unwrap()
                .count,
            2
        );
    }

    #[tokio::test]
    async fn hand_actions_need_a_joined_actor_matching_the_participant() {
        let s = moderated();
        let r = new_room(&s, "7").await;
        let (a, b, stranger) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
        join(&s, "7", r.room_id, a, false).await;
        join(&s, "7", r.room_id, b, false).await;
        // Mismatched actor (b raising a's hand) and a non-participant are refused.
        assert!(matches!(
            s.raise_hand("7", r.room_id, a, b).await,
            Err(RoomError::Forbidden(_))
        ));
        assert!(matches!(
            s.raise_hand("7", r.room_id, stranger, stranger).await,
            Err(RoomError::Forbidden(_))
        ));
        assert!(matches!(
            s.lower_hand("7", r.room_id, stranger, stranger).await,
            Err(RoomError::Forbidden(_))
        ));
        assert!(matches!(
            s.list_hands("7", r.room_id, stranger, HandView::Own).await,
            Err(RoomError::Forbidden(_))
        ));
        s.raise_hand("7", r.room_id, a, a).await.unwrap();
        // A joined non-staff member cannot lower another's hand, list the queue, ack or reset;
        // there is no role field to assert: the member table is the only authority.
        assert!(matches!(
            s.lower_hand("7", r.room_id, a, b).await,
            Err(RoomError::Forbidden(_))
        ));
        assert!(matches!(
            s.list_hands("7", r.room_id, b, HandView::All).await,
            Err(RoomError::Forbidden(_))
        ));
        assert!(matches!(
            s.acknowledge_hand("7", r.room_id, a, b).await,
            Err(RoomError::Forbidden(_))
        ));
        assert!(matches!(
            s.reset_hands("7", r.room_id, b).await,
            Err(RoomError::Forbidden(_))
        ));
        // Own view: a sees only their entry, b sees no entry but the queue length.
        let mine = s
            .list_hands("7", r.room_id, a, HandView::Own)
            .await
            .unwrap();
        assert_eq!(
            (mine.hands.len(), mine.count, mine.position),
            (1, 1, Some(1))
        );
        let theirs = s
            .list_hands("7", r.room_id, b, HandView::Auto)
            .await
            .unwrap();
        assert_eq!(
            (theirs.hands.len(), theirs.count, theirs.position),
            (0, 1, None),
            "Auto for a non-staff member is the own view"
        );
        // Lower-own works; lower-own is a no-op when nothing is raised.
        assert!(s.lower_hand("7", r.room_id, a, a).await.unwrap());
        assert!(!s.lower_hand("7", r.room_id, a, a).await.unwrap());
    }

    #[tokio::test]
    async fn staff_is_recorded_server_side_sticky_and_never_claimed() {
        let s = moderated();
        let r = new_room(&s, "7").await;
        let (a, m) = (Uuid::new_v4(), Uuid::new_v4());
        join(&s, "7", r.room_id, a, false).await;
        join(&s, "7", r.room_id, m, true).await;
        s.raise_hand("7", r.room_id, a, a).await.unwrap();
        // Staff may lower another's hand, ack, list all, reset.
        let ack = s.acknowledge_hand("7", r.room_id, a, m).await.unwrap();
        assert_eq!(ack.acknowledged_by, Some(m));
        assert!(ack.acknowledged_at.is_some());
        let again = s.acknowledge_hand("7", r.room_id, a, m).await.unwrap();
        assert_eq!(again, ack, "the first acknowledgement is kept");
        assert_eq!(s.metrics.hands_acknowledged_total.get(), 1);
        assert!(matches!(
            s.acknowledge_hand("7", r.room_id, Uuid::new_v4(), m).await,
            Err(RoomError::Invalid(_))
        ));
        let auto = s
            .list_hands("7", r.room_id, m, HandView::Auto)
            .await
            .unwrap();
        assert_eq!(auto.hands.len(), 1, "Auto = whole queue for staff");
        // An ordinary re-issue for a staff participant never demotes them.
        join(&s, "7", r.room_id, m, false).await;
        assert_eq!(s.reset_hands("7", r.room_id, m).await.unwrap(), 1);
        assert_eq!(s.reset_hands("7", r.room_id, m).await.unwrap(), 0);
        // register_staff promotes a participant who only ever got an ordinary token.
        assert!(matches!(
            s.reset_hands("7", r.room_id, a).await,
            Err(RoomError::Forbidden(_))
        ));
        s.register_staff("7", r.room_id, a).await.unwrap();
        assert_eq!(s.reset_hands("7", r.room_id, a).await.unwrap(), 0);
        // ...but not in another community's room.
        assert!(matches!(
            s.register_staff("8", r.room_id, a).await,
            Err(RoomError::NotFound)
        ));
    }

    #[tokio::test]
    async fn staff_hand_ops_follow_the_moderation_flag() {
        let s = svc(Some(SECRET)); // flag OFF
        let r = new_room(&s, "7").await;
        let (a, m) = (Uuid::new_v4(), Uuid::new_v4());
        join(&s, "7", r.room_id, a, false).await;
        join(&s, "7", r.room_id, m, true).await;
        // Participant-level raise/lower-own/list-own need no flag...
        s.raise_hand("7", r.room_id, a, a).await.unwrap();
        s.list_hands("7", r.room_id, a, HandView::Own)
            .await
            .unwrap();
        // ...staff actions fail loud while the flag is off, even for a recorded staff member.
        for r in [
            s.acknowledge_hand("7", r.room_id, a, m).await.map(|_| ()),
            s.reset_hands("7", r.room_id, m).await.map(|_| ()),
            s.lower_hand("7", r.room_id, a, m).await.map(|_| ()),
            s.list_hands("7", r.room_id, m, HandView::All)
                .await
                .map(|_| ()),
            s.register_staff("7", r.room_id, m).await.map(|_| ()),
        ] {
            assert!(matches!(r, Err(RoomError::Unsupported(_))), "{r:?}");
        }
        // Auto degrades to the own view rather than failing.
        let auto = s
            .list_hands("7", r.room_id, m, HandView::Auto)
            .await
            .unwrap();
        assert_eq!(auto.hands.len(), 0);
        assert_eq!(auto.count, 1);
    }

    #[tokio::test]
    async fn hands_and_membership_clear_on_leave_kick_and_close() {
        let s = moderated();
        let r = new_room(&s, "7").await;
        let (leaver, kicked, stays, actor) = (
            Uuid::new_v4(),
            Uuid::new_v4(),
            Uuid::new_v4(),
            Uuid::new_v4(),
        );
        for p in [leaver, kicked, stays] {
            join(&s, "7", r.room_id, p, false).await;
            s.raise_hand("7", r.room_id, p, p).await.unwrap();
        }
        join(&s, "7", r.room_id, actor, true).await;
        s.leave("7", r.room_id, leaver, leaver, ActorRole::Member)
            .await
            .unwrap();
        s.kick("7", r.room_id, kicked, actor, ActorRole::Moderator)
            .await
            .unwrap();
        let left: Vec<Uuid> = s
            .list_hands("7", r.room_id, actor, HandView::All)
            .await
            .unwrap()
            .hands
            .into_iter()
            .map(|h| h.participant_id)
            .collect();
        assert_eq!(
            left,
            vec![stays],
            "leave and kick drop exactly their own hand"
        );
        // Having left, the participant is no longer a member; a kicked one cannot rejoin.
        assert!(matches!(
            s.raise_hand("7", r.room_id, leaver, leaver).await,
            Err(RoomError::Forbidden(_))
        ));
        assert!(matches!(
            s.raise_hand("7", r.room_id, kicked, kicked).await,
            Err(RoomError::Forbidden(_))
        ));
        assert!(matches!(
            s.issue_token(
                "7",
                r.room_id,
                IssueRequest {
                    participant: kicked,
                    ..issue(true, true)
                }
            )
            .await,
            Err(RoomError::Forbidden(_))
        ));
        s.close("7", r.room_id).await.unwrap();
        for reason in ["leave", "kick", "delete"] {
            assert_eq!(
                s.metrics
                    .hands_cleared_total
                    .with_label_values(&[reason])
                    .get(),
                1,
                "{reason}"
            );
        }
    }

    #[tokio::test]
    async fn hand_queue_and_member_table_are_bounded() {
        let s = moderated();
        let r = new_room(&s, "7").await;
        for _ in 0..MAX_HANDS_PER_ROOM {
            let p = Uuid::new_v4();
            join(&s, "7", r.room_id, p, false).await;
            s.raise_hand("7", r.room_id, p, p).await.unwrap();
        }
        let p = Uuid::new_v4();
        join(&s, "7", r.room_id, p, false).await;
        assert!(matches!(
            s.raise_hand("7", r.room_id, p, p).await,
            Err(RoomError::LimitReached(_))
        ));
    }

    #[tokio::test]
    async fn readiness_requires_the_token_secret() {
        assert!(svc(Some(SECRET)).readiness().is_ok());
        assert!(svc(None).readiness().is_err());
    }
}
