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

pub mod host_api;
pub mod media;
pub mod token;

use std::collections::{HashMap, VecDeque};
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
/// Subscribers across all rooms.
pub const MAX_SUBSCRIBERS_GLOBAL: usize = 5_000;
/// Longest TTL of a WHEP pipeline viewer token (seconds).
pub const MAX_PIPELINE_TOKEN_TTL_SECS: i64 = 300;

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
}

struct SubscriberEntry {
    pc: Arc<dyn PeerConnection>,
    participant: Uuid,
    ended: Arc<AtomicBool>,
    expires_at: i64,
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
}

/// Parameters for [`RoomService::issue_token`].
#[derive(Debug, Clone)]
pub struct IssueRequest {
    pub participant: Uuid,
    pub publish: bool,
    pub subscribe: bool,
    pub source: Option<Uuid>,
    pub ttl_secs: Option<i64>,
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
    issued: Mutex<HashMap<Uuid, (String, Uuid, i64)>>,
    keys: Option<TokenKeys>,
    whep: Option<Arc<crate::egress::whep::WhepState>>,
    pub(crate) factory: Arc<PeerConnectionFactory>,
    pub(crate) metrics: RtcMetrics,
    public_base_url: String,
}

fn now() -> i64 {
    chrono::Utc::now().timestamp()
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
            issued: Mutex::new(HashMap::new()),
            keys,
            whep: None,
            factory,
            metrics,
            public_base_url,
        }
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
            "WEBRTC_ROOM_TOKEN_SECRET is not set",
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
        }
    }

    /// Creates a room for `community_id`.
    pub async fn create(
        &self,
        community_id: &str,
        label: Option<String>,
        max_publishers: Option<usize>,
        route: RouteMode,
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
        let room = Room {
            community_id: community_id.to_string(),
            label,
            max_publishers,
            route,
            created_at: now(),
            epoch: 0,
            publishers: HashMap::new(),
            subscribers: HashMap::new(),
        };
        let info = Self::info(id, &room);
        rooms.insert(id, room);
        tracing::info!(room_id = %id, community_id, "rooms: created");
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
        let epoch = {
            let rooms = self.rooms.read().await;
            match rooms.get(&room_id) {
                Some(r) if r.community_id == community_id => r.epoch,
                _ => return Err(RoomError::NotFound),
            }
        };
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
        let t = now();
        if issued.len() >= MAX_ISSUED {
            issued.retain(|_, (_, _, e)| *e >= t);
        }
        if issued.len() >= MAX_ISSUED {
            return Err(RoomError::LimitReached("issued-token ledger full".into()));
        }
        issued.insert(jti, (community_id.to_string(), room_id, exp));
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
            epoch: 0,
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
            Some(r) if r.community_id == claims.community && r.epoch == claims.epoch => Ok(claims),
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
        expires_at: i64,
    ) -> Result<Uuid, RoomError> {
        let (session, stale) = {
            let mut rooms = self.rooms.write().await;
            let room = rooms.get_mut(&room_id).ok_or(RoomError::NotFound)?;
            if !room.publishers.contains_key(&participant)
                && room.publishers.len() >= room.max_publishers
            {
                return Err(RoomError::LimitReached("room publisher limit".into()));
            }
            let session = Uuid::new_v4();
            let stale = room.publishers.insert(
                participant,
                PublisherEntry {
                    fanouts,
                    pc,
                    session,
                    ended,
                    expires_at,
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
        expires_at: i64,
    ) -> Result<Uuid, RoomError> {
        let (session, stale) = {
            let mut rooms = self.rooms.write().await;
            let total: usize = rooms.values().map(|r| r.subscribers.len()).sum();
            let room = rooms.get_mut(&room_id).ok_or(RoomError::NotFound)?;
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
            if !replacing && total >= MAX_SUBSCRIBERS_GLOBAL {
                return Err(RoomError::LimitReached("global subscriber limit".into()));
            }
            let stale: Vec<Arc<dyn PeerConnection>> = old
                .iter()
                .filter_map(|k| room.subscribers.remove(k).map(|s| s.pc))
                .collect();
            let session = Uuid::new_v4();
            room.subscribers.insert(
                session,
                SubscriberEntry {
                    pc,
                    participant,
                    ended,
                    expires_at,
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

    /// Spawns the periodic [`Self::sweep`] (every `every`).
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
            }
        });
    }

    /// Removes a session whose peer connection ended (Failed/Closed/Disconnected)
    /// and closes it; a no-op when it is already gone.
    pub(crate) async fn reap_session(&self, room_id: Uuid, session: Uuid) {
        let removed = {
            let mut rooms = self.rooms.write().await;
            let Some(room) = rooms.get_mut(&room_id) else {
                return;
            };
            if let Some(s) = room.subscribers.remove(&session) {
                Some((s.pc, "room_whep"))
            } else {
                let pid = room
                    .publishers
                    .iter()
                    .find(|(_, p)| p.session == session)
                    .map(|(k, _)| *k);
                pid.and_then(|k| room.publishers.remove(&k).map(|p| (p.pc, "room_whip")))
            }
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
            if room
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
            }
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
            secret.map(|s| TokenKeys::new(s).unwrap()),
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
}
