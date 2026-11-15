//! YouTube host duties for the generic outbound executor: the [`DutyHook`] behind live-chat
//! send, message delete, bans and moderators.
//!
//! The YouTube connector shapes requests but cannot enforce any of the following, so the HOST
//! does:
//!
//! | duty | enforcement |
//! |---|---|
//! | resource ownership | a `DELETE ...?id=<resource>` names a bundle-supplied id, and YouTube authorizes it against the OAuth token alone -- not against the pinned live chat. Before egress the id must be PROVEN to belong to the pinned live chat: (1) the host's own id cache (ids the host created or already proved, keyed by live-chat id, TTL-bounded), else (2) a host-side list lookup scoped to the pinned chat (`liveChatMessages.list`, `liveChatModerators.list`). A foreign or unknown id is refused (`resource_not_in_chat`); a ban id has no list API, so only host-created ban ids are accepted (`ban_not_host_created`) |
//! | `{{YT_TARGET_USER}}` | the entry's user is a tokenized UUID; [`PlatformUserResolver`] maps it to the raw channel id ONLY here, scoped to the stamp's tenant + community. A raw channel id / unknown UUID is refused. The value lives in a zeroizing [`SlotValue`], never logged |
//! | quota | every op is charged its YouTube Data API unit cost against the community's daily budget BEFORE anything is sent; an exhausted budget is the explicit `quota_exhausted` refusal |
//! | `liveChatId` / `Authorization` | the executor's [`super::youtube_slots::YoutubeSlots`] (per community, per call, stamp-pinned) |
//!
//! Logs and metrics carry fixed reason codes only: no chat id, user id, resource id or text.

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use bundle_host_http::guarded::GuardedClient;

use super::discord_duties::{PlatformUserResolver, UserResolveError};
use super::policy::PlatformPolicy;
use super::subst::SlotValue;
use super::twitch_slots::map_cred;
use super::{default_payload, DutyCx, DutyHook, ExecError, ExecOutcome, SlotError};
use crate::outbound_creds_youtube::{is_live_chat_id, YoutubeCredentialSource};
use crate::outbound_ops::{OutboundAction, PolicyOp};

/// Production Data API origin.
pub const API_BASE: &str = "https://www.googleapis.com";
/// Default per-community daily budget in Data API quota units (Google's default PROJECT quota
/// is 10 000/day shared by every community, so one community may use at most half).
pub const DEFAULT_DAILY_BUDGET_UNITS: u32 = 5_000;
/// How long a proven / host-created resource id is trusted.
pub const OWNED_TTL: Duration = Duration::from_secs(24 * 60 * 60);
/// Most ids the ownership cache holds before it is flushed (fail closed: forces lookups).
pub const OWNED_MAX: usize = 20_000;
/// Most `liveChatModerators.list` pages one lookup walks.
const MAX_MODERATOR_PAGES: usize = 5;
/// Unit cost of one list call.
const LIST_COST: u32 = 5;

/// Which kind of resource an id names.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Class {
    Message,
    Ban,
    Moderator,
}

static DUTY_REFUSED: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_youtube_duty_refused_total",
            "YouTube host-duty refusals, labeled by kind and a fixed reason code",
        ),
        &["kind", "reason"],
    )
    .expect("valid metric definition")
});

static QUOTA_UNITS: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_youtube_quota_units_total",
            "YouTube Data API quota units charged by the host, labeled by kind",
        ),
        &["kind"],
    )
    .expect("valid metric definition")
});

/// Registers the YouTube duty metrics on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(DUTY_REFUSED.clone()))?;
    registry.register(Box::new(QUOTA_UNITS.clone()))
}

/// Count of one refusal (test/observability helper).
#[must_use]
pub fn refusal_count(kind: &str, reason: &str) -> u64 {
    DUTY_REFUSED.with_label_values(&[kind, reason]).get()
}

/// Bounded metric label for a kind (never request-derived text).
fn kind_label(kind: &str) -> &'static str {
    match kind {
        "chat.send" => "chat.send",
        "chat.delete" => "chat.delete",
        "mod.ban" => "mod.ban",
        "mod.unban" => "mod.unban",
        "mod.add" => "mod.add",
        "mod.remove" => "mod.remove",
        _ => "other",
    }
}

/// Data API quota-unit cost of a kind's own request (documented per method).
#[must_use]
pub fn unit_cost(kind: &str) -> u32 {
    match kind {
        // liveChatMessages.insert / delete.
        "chat.send" | "chat.delete" => 20,
        // liveChatBans / liveChatModerators insert + delete.
        "mod.ban" | "mod.unban" | "mod.add" | "mod.remove" => 50,
        _ => 0,
    }
}

fn refused(reason: &'static str) -> ExecError {
    ExecError::Slot(SlotError::Refused(reason))
}

/// Counts and returns a refusal. Only fixed codes reach the label.
fn deny(kind: &str, e: ExecError) -> ExecError {
    DUTY_REFUSED
        .with_label_values(&[kind_label(kind), e.code()])
        .inc();
    e
}

/// Per-community daily quota ledger (units), UTC day.
#[derive(Debug)]
pub struct QuotaLedger {
    budget: u32,
    clock: fn() -> u64,
    days: Mutex<HashMap<i64, (u64, u32)>>,
}

fn epoch_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

impl QuotaLedger {
    /// A ledger with `budget` units per community per UTC day.
    #[must_use]
    pub fn new(budget: u32) -> Self {
        Self::with_clock(budget, epoch_secs)
    }

    /// Test seam: explicit epoch-seconds clock.
    #[must_use]
    pub fn with_clock(budget: u32, clock: fn() -> u64) -> Self {
        Self {
            budget,
            clock,
            days: Mutex::new(HashMap::new()),
        }
    }

    /// Charges `units` to `community`; `false` (and nothing charged) when it would exceed the
    /// day's budget.
    pub fn charge(&self, community: i64, units: u32) -> bool {
        let day = (self.clock)() / 86_400;
        let mut days = self.days.lock().unwrap_or_else(|e| e.into_inner());
        let entry = days.entry(community).or_insert((day, 0));
        if entry.0 != day {
            *entry = (day, 0);
        }
        match entry.1.checked_add(units) {
            Some(total) if total <= self.budget => {
                entry.1 = total;
                true
            }
            _ => false,
        }
    }

    /// Units used by `community` today (test/observability helper).
    #[must_use]
    pub fn used(&self, community: i64) -> u32 {
        let day = (self.clock)() / 86_400;
        let days = self.days.lock().unwrap_or_else(|e| e.into_inner());
        days.get(&community)
            .filter(|(d, _)| *d == day)
            .map_or(0, |(_, u)| *u)
    }
}

/// Ids proven (or created) for a live chat, with a TTL.
#[derive(Debug, Default)]
struct OwnedCache {
    map: HashMap<(String, Class, String), Instant>,
}

impl OwnedCache {
    fn contains(&self, chat: &str, class: Class, id: &str) -> bool {
        self.map
            .get(&(chat.to_string(), class, id.to_string()))
            .is_some_and(|at| at.elapsed() < OWNED_TTL)
    }

    fn insert(&mut self, chat: &str, class: Class, id: &str) {
        if self.map.len() >= OWNED_MAX {
            self.map.retain(|_, at| at.elapsed() < OWNED_TTL);
            if self.map.len() >= OWNED_MAX {
                // Still full: flush everything. Later checks fall back to a lookup (or refuse).
                self.map.clear();
            }
        }
        self.map
            .insert((chat.to_string(), class, id.to_string()), Instant::now());
    }
}

/// The YouTube [`DutyHook`] over a credential source and the tokenization resolver.
pub struct YoutubeDuties<C: YoutubeCredentialSource, U: PlatformUserResolver> {
    creds: C,
    users: U,
    api_base: String,
    quota: QuotaLedger,
    owned: Mutex<OwnedCache>,
}

impl<C: YoutubeCredentialSource, U: PlatformUserResolver> YoutubeDuties<C, U> {
    /// Production wiring with `budget` quota units per community per day.
    #[must_use]
    pub fn new(creds: C, users: U, budget: u32) -> Self {
        Self::with_endpoint(creds, users, API_BASE.to_string(), QuotaLedger::new(budget))
    }

    /// Test seam: explicit API origin and ledger.
    #[must_use]
    pub fn with_endpoint(creds: C, users: U, api_base: String, quota: QuotaLedger) -> Self {
        Self {
            creds,
            users,
            api_base,
            quota,
            owned: Mutex::new(OwnedCache::default()),
        }
    }

    /// The quota ledger (observability / tests).
    #[must_use]
    pub fn quota(&self) -> &QuotaLedger {
        &self.quota
    }

    fn remember(&self, chat: &str, class: Class, id: &str) {
        self.owned
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(chat, class, id);
    }

    fn known(&self, chat: &str, class: Class, id: &str) -> bool {
        self.owned
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains(chat, class, id)
    }

    fn charge(&self, community: i64, units: u32) -> Result<(), ExecError> {
        if self.quota.charge(community, units) {
            Ok(())
        } else {
            Err(refused("quota_exhausted"))
        }
    }

    /// One authenticated GET against the Data API, returning the parsed 2xx JSON.
    async fn get_json(
        &self,
        client: &GuardedClient,
        token: &str,
        path_and_query: &str,
    ) -> Result<serde_json::Value, ExecError> {
        let resp = client
            .fetch(
                reqwest::Method::GET,
                &format!("{}{path_and_query}", self.api_base),
                vec![
                    ("Authorization".to_string(), format!("Bearer {token}")),
                    ("Accept".to_string(), "application/json".to_string()),
                ],
                None,
            )
            .await
            .map_err(|_| ExecError::Slot(SlotError::Unavailable("youtube_lookup_failed")))?;
        match resp.status {
            200..=299 => serde_json::from_slice(&resp.body)
                .map_err(|_| ExecError::Slot(SlotError::Unavailable("youtube_lookup_failed"))),
            401 => Err(refused("connection_not_usable")),
            403 | 404 => Err(refused("resource_not_in_chat")),
            _ => Err(ExecError::Slot(SlotError::Unavailable(
                "youtube_lookup_failed",
            ))),
        }
    }

    /// Host-side proof that message/moderator `id` belongs to `chat`, by listing the PINNED
    /// chat's own resources. Everything seen is cached; absence is a refusal.
    async fn lookup(
        &self,
        cx: &DutyCx<'_>,
        community: i64,
        chat: &str,
        token: &str,
        class: Class,
        id: &str,
    ) -> Result<(), ExecError> {
        let mut page: Option<String> = None;
        let pages = if class == Class::Message {
            1
        } else {
            MAX_MODERATOR_PAGES
        };
        for _ in 0..pages {
            self.charge(community, LIST_COST)?;
            QUOTA_UNITS
                .with_label_values(&["lookup"])
                .inc_by(u64::from(LIST_COST));
            let mut q = if class == Class::Message {
                format!("/youtube/v3/liveChat/messages?part=id&maxResults=2000&liveChatId={chat}")
            } else {
                format!("/youtube/v3/liveChat/moderators?part=id&maxResults=50&liveChatId={chat}")
            };
            if let Some(t) = &page {
                q.push_str("&pageToken=");
                q.push_str(t);
            }
            let body = self.get_json(cx.client, token, &q).await?;
            let mut found = false;
            for item in body
                .get("items")
                .and_then(serde_json::Value::as_array)
                .into_iter()
                .flatten()
            {
                if let Some(seen) = item.get("id").and_then(serde_json::Value::as_str) {
                    if is_resource_id(seen) {
                        self.remember(chat, class, seen);
                        found |= seen == id;
                    }
                }
            }
            if found {
                return Ok(());
            }
            match body
                .get("nextPageToken")
                .and_then(serde_json::Value::as_str)
            {
                Some(t)
                    if class != Class::Message
                        && !t.is_empty()
                        && t.len() <= 256
                        && t.bytes()
                            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-')) =>
                {
                    page = Some(t.to_string());
                }
                _ => break,
            }
        }
        Err(refused("resource_not_in_chat"))
    }

    /// The ownership gate for a delete kind.
    async fn ownership_gate(
        &self,
        cx: &DutyCx<'_>,
        community: i64,
        chat: &str,
        class: Class,
    ) -> Result<(), ExecError> {
        let id = resource_id(cx.action).ok_or(ExecError::BadAction)?;
        // Same charset the verifier enforces on the literal; checked first so a hostile id never
        // reaches the cache or a lookup URL.
        if !is_resource_id(id) {
            return Err(refused("resource_not_in_chat"));
        }
        if self.known(chat, class, id) {
            return Ok(());
        }
        if class == Class::Ban {
            // `liveChatBans` has no list method: only ids this host created are provable.
            return Err(refused("ban_not_host_created"));
        }
        let binding = self
            .creds
            .binding(Some(community), chat)
            .await
            .map_err(|e| ExecError::Slot(map_cred(e)))?;
        // Defense in depth: the binding must be for exactly the pinned chat.
        if binding.live_chat_id != chat {
            return Err(refused("channel_not_owned"));
        }
        self.lookup(cx, community, chat, binding.oauth_token.expose(), class, id)
            .await
    }

    /// The tokenized target user of a ban / add op, detokenized for THIS community only.
    async fn target_user(&self, cx: &DutyCx<'_>) -> Result<SlotValue, ExecError> {
        let OutboundAction::PolicyOp(PolicyOp {
            user_id, tenant_id, ..
        }) = cx.action
        else {
            return Err(ExecError::BadAction);
        };
        let uuid = user_id
            .as_deref()
            .and_then(canonical_user)
            .ok_or_else(|| refused("user_not_tokenized"))?;
        let community = cx
            .community_id
            .filter(|c| *c > 0)
            .ok_or_else(|| refused("no_community_scope"))?;
        let tenant = tenant_id
            .filter(|t| *t > 0)
            .ok_or_else(|| refused("no_tenant_scope"))?;
        match self.users.resolve(tenant, community, &uuid).await {
            Ok(v) if is_live_chat_id(v.expose()) => Ok(v),
            Ok(_) => Err(refused("tokenization_bad_answer")),
            Err(UserResolveError::NotFound) => Err(refused("user_not_in_community")),
            Err(UserResolveError::Unavailable) => Err(ExecError::Slot(SlotError::Unavailable(
                "tokenization_unavailable",
            ))),
            Err(UserResolveError::RateLimited) => Err(ExecError::Slot(SlotError::Unavailable(
                "tokenization_rate_limited",
            ))),
            Err(UserResolveError::Denied) => Err(refused("tokenization_denied")),
            Err(UserResolveError::Invalid) => Err(refused("tokenization_bad_request")),
        }
    }
}

/// A YouTube resource id (`LCC.…` message ids carry dots): `^[A-Za-z0-9_.-]{1,128}$`, the
/// charset the verifier allows for a literal `id` query value.
fn is_resource_id(s: &str) -> bool {
    (1..=128).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
}

/// The resource id a delete-style action names.
fn resource_id(action: &OutboundAction) -> Option<&str> {
    match action {
        OutboundAction::ChatDelete { message_id, .. } => Some(message_id),
        OutboundAction::PolicyOp(p) => p.message_id.as_deref(),
        _ => None,
    }
}

/// Canonical lowercase UUID from a bare or `{user:<uuid>}` spelling.
fn canonical_user(raw: &str) -> Option<String> {
    let inner = raw
        .strip_prefix("{user:")
        .and_then(|r| r.strip_suffix('}'))
        .unwrap_or(raw);
    let b = inner.as_bytes();
    let ok = b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit(),
        });
    ok.then(|| inner.to_ascii_lowercase())
}

/// The class of resource a delete kind removes.
fn delete_class(kind: &str) -> Option<Class> {
    match kind {
        "chat.delete" => Some(Class::Message),
        "mod.unban" => Some(Class::Ban),
        "mod.remove" => Some(Class::Moderator),
        _ => None,
    }
}

impl<C: YoutubeCredentialSource, U: PlatformUserResolver> DutyHook for YoutubeDuties<C, U> {
    fn claims(&self, platform: &str, kind: &str) -> bool {
        platform == "youtube"
            && matches!(
                kind,
                "chat.send" | "chat.delete" | "mod.ban" | "mod.unban" | "mod.add" | "mod.remove"
            )
    }

    async fn gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError> {
        let kind = cx.action.op();
        let res = async {
            let community = cx
                .community_id
                .filter(|c| *c > 0)
                .ok_or_else(|| refused("no_community_scope"))?;
            let chat = cx.pinned_channel.ok_or_else(|| refused("no_origin_pin"))?;
            if !is_live_chat_id(chat) {
                return Err(refused("channel_not_owned"));
            }
            // Ownership first (a refused foreign id must not burn the op's own quota), then the
            // op's own cost.
            if let Some(class) = delete_class(kind) {
                self.ownership_gate(cx, community, chat, class).await?;
            }
            let cost = unit_cost(kind);
            self.charge(community, cost)?;
            QUOTA_UNITS
                .with_label_values(&[kind_label(kind)])
                .inc_by(u64::from(cost));
            Ok(())
        }
        .await;
        res.map_err(|e| deny(kind, e))
    }

    async fn payload(&self, cx: &DutyCx<'_>) -> Result<String, ExecError> {
        let kind = cx.action.op();
        let policy = PlatformPolicy::youtube();
        let kp = policy.kind(kind).ok_or(ExecError::UnsupportedKind)?;
        default_payload(kp, cx.action).map_err(|e| deny(kind, e))
    }

    async fn after_send(&self, cx: &DutyCx<'_>, outcome: &ExecOutcome) {
        let class = match cx.action.op() {
            "mod.ban" => Class::Ban,
            "mod.add" => Class::Moderator,
            _ => return,
        };
        let (Some(chat), Some(body)) = (cx.pinned_channel, outcome.result_id.as_deref()) else {
            return;
        };
        let id = serde_json::from_str::<serde_json::Value>(body)
            .ok()
            .and_then(|v| {
                v.get("id")
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_string)
            });
        if let Some(id) = id.filter(|i| is_resource_id(i)) {
            self.remember(chat, class, &id);
        }
    }

    async fn dynamic_slot(
        &self,
        cx: &DutyCx<'_>,
        slot: &str,
        _client: &GuardedClient,
    ) -> Result<SlotValue, ExecError> {
        let kind = cx.action.op();
        match slot {
            "YT_TARGET_USER" => self.target_user(cx).await.map_err(|e| deny(kind, e)),
            _ => Err(deny(kind, refused("unknown_duty_slot"))),
        }
    }
}

/// Routes duty calls to the YouTube hook for `youtube` actions and to `inner` (Discord's hook)
/// for everything else, so one executor serves both.
pub struct WithYoutube<D, Y> {
    inner: D,
    youtube: Y,
}

impl<D, Y> WithYoutube<D, Y> {
    /// Composes `inner` (every non-YouTube platform) with `youtube`.
    #[must_use]
    pub fn new(inner: D, youtube: Y) -> Self {
        Self { inner, youtube }
    }
}

impl<D: DutyHook, Y: DutyHook> DutyHook for WithYoutube<D, Y> {
    fn claims(&self, platform: &str, kind: &str) -> bool {
        if platform == "youtube" {
            self.youtube.claims(platform, kind)
        } else {
            self.inner.claims(platform, kind)
        }
    }

    async fn gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError> {
        if cx.platform == "youtube" {
            self.youtube.gate(cx).await
        } else {
            self.inner.gate(cx).await
        }
    }

    async fn bind_template(
        &self,
        cx: &DutyCx<'_>,
        tpl: &mut super::RequestTpl,
    ) -> Result<(), ExecError> {
        if cx.platform == "youtube" {
            self.youtube.bind_template(cx, tpl).await
        } else {
            self.inner.bind_template(cx, tpl).await
        }
    }

    async fn payload(&self, cx: &DutyCx<'_>) -> Result<String, ExecError> {
        if cx.platform == "youtube" {
            self.youtube.payload(cx).await
        } else {
            self.inner.payload(cx).await
        }
    }

    async fn role_bulk(&self, cx: &DutyCx<'_>) -> Result<super::RoleBulkPlan, ExecError> {
        if cx.platform == "youtube" {
            self.youtube.role_bulk(cx).await
        } else {
            self.inner.role_bulk(cx).await
        }
    }

    async fn after_send(&self, cx: &DutyCx<'_>, outcome: &ExecOutcome) {
        if cx.platform == "youtube" {
            self.youtube.after_send(cx, outcome).await;
        } else {
            self.inner.after_send(cx, outcome).await;
        }
    }

    async fn dynamic_slot(
        &self,
        cx: &DutyCx<'_>,
        slot: &str,
        client: &GuardedClient,
    ) -> Result<SlotValue, ExecError> {
        if cx.platform == "youtube" {
            self.youtube.dynamic_slot(cx, slot, client).await
        } else {
            self.inner.dynamic_slot(cx, slot, client).await
        }
    }
}

#[cfg(test)]
#[path = "youtube_duties_tests.rs"]
mod tests;
