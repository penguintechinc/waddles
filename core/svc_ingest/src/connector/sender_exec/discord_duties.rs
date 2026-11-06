//! Discord host duties for the generic outbound executor (P6a T6d-2): the
//! [`DutyHook`] behind `dm.send`, `role.add` and `role.remove`.
//!
//! The Discord connector shapes requests but cannot enforce any of this, so the
//! HOST does (connector README "Host duties"):
//!
//! | duty | enforcement |
//! |---|---|
//! | `{user:<uuid>}` recipient | the queue entry's user is a tokenized UUID; [`PlatformUserResolver`] maps it to the raw Discord id ONLY inside this module, scoped to the stamp's community. A UUID of another community, an unknown one, or a raw snowflake is refused. The raw id lives in zeroizing values, never logged or stored |
//! | `send_dm_in_community` gate | BEFORE anything is built: the stamp-pinned channel must belong to the community's guild and the recipient must be a member of it (same membership interpretation as the legacy client, [`crate::discord_rest`]); non-member / foreign channel -> `Slot(Refused("not_in_community"))`, lookup failure -> refused, nothing sent |
//! | `{dm.channel_id}` | the host runs `POST /users/@me/channels` for THIS operation's recipient through the guarded client and fills the slot only from that reply; no state survives the call, so another dm.send's channel can never be reused. There is no `dm.post` kind |
//! | managed roles | the connector emits a KEY; the host maps it through the binding's allowlist to a snowflake, REFUSES any role whose permissions include `ADMINISTRATOR`, `MANAGE_ROLES` or `MANAGE_GUILD` (guild role list fetched through the guarded client, short-TTL bounded cache keyed by numeric guild id, fail closed), and pins the guild from the binding (`{target.guild_id}`); a guild/role in the payload is not expressible |
//! | `event.*` (guild scheduled events) | the guild is pinned from the community's binding (`{target.guild_id}`); the entry's MAC-covered `expect_guild` (wire `origin_channel`) is only an ASSERTION compared to it -- a mismatch is refused (`guild_mismatch`) before anything is built. A guild/channel in the event JSON is not expressible (the connector refuses it). The reply's new event id is returned to the producer |
//! | `Authorization` | the executor's [`super::discord_slots::DiscordSlots`] (per community, per call) |
//!
//! **Template normalization** ([`DutyHook::bind_template`]): the connector
//! spells the recipient and role as `{user:<uuid>}` / `{target.role.<key>}`.
//! The hook rewrites EXACTLY the strings it put into the payload to the fixed
//! duty slots (`{target.user}`, `{target.role}`) and consumes the
//! `X-Waddle-Host-Dm-Recipient` pseudo-header (checking it equals the entry's
//! own recipient placeholder). Anything else -- another user, another role
//! key -- is left as-is and the verifier refuses it as an undeclared slot. The
//! pseudo-header therefore stays forbidden in the policy; it is never an
//! allowed header, it is stripped before verification.
//!
//! Logs and metrics carry fixed reason codes only: no user id, role id,
//! handle or message text.

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use bundle_host_http::guarded::GuardedClient;
use serde_json::json;

use super::discord_slots::{Rest, API_BASE};
use super::role_guild_scope;
use super::subst::SlotValue;
use super::{DutyCx, DutyHook, ExecError, RequestTpl, SlotError};
use crate::discord_rest::{classify_membership, is_snowflake, Membership};
use crate::outbound_creds::CredError;
use crate::outbound_creds_discord::{is_role_key, DiscordBinding, DiscordCredentialSource};
use crate::outbound_ops::{EventKind, OutboundAction};

use super::twitch_slots::map_cred;

/// `ADMINISTRATOR`.
const PERM_ADMINISTRATOR: u64 = 1 << 3;
/// `MANAGE_GUILD`.
const PERM_MANAGE_GUILD: u64 = 1 << 5;
/// `MANAGE_ROLES`.
const PERM_MANAGE_ROLES: u64 = 1 << 28;
/// Permissions no managed role may carry, even if allowlisted.
pub const FORBIDDEN_ROLE_PERMS: u64 = PERM_ADMINISTRATOR | PERM_MANAGE_GUILD | PERM_MANAGE_ROLES;

/// The pseudo-header the connector puts a `dm.send` recipient in (lowercase).
const DM_RECIPIENT_HEADER: &str = "x-waddle-host-dm-recipient";
/// How long a guild's role-permission list is trusted.
pub const ROLE_CACHE_TTL: Duration = Duration::from_secs(30);
/// Most guilds the role cache holds.
pub const ROLE_CACHE_MAX_GUILDS: usize = 256;

static DUTY_REFUSED: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_discord_duty_refused_total",
            "Discord host-duty refusals, labeled by kind and a fixed reason code",
        ),
        &["kind", "reason"],
    )
    .expect("valid metric definition")
});

/// Registers the duty-refusal counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(DUTY_REFUSED.clone()))
}

/// Count of one refusal (test/observability helper).
#[must_use]
pub fn refusal_count(kind: &str, reason: &str) -> u64 {
    DUTY_REFUSED.with_label_values(&[kind, reason]).get()
}

/// Why a UUID could not be resolved to a platform user.
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum UserResolveError {
    /// No such identity in THIS community (unknown, or another community's).
    #[error("user is not an identity of this community")]
    NotFound,
    /// The tokenization service could not answer; retryable.
    #[error("tokenization service unavailable")]
    Unavailable,
    /// The tokenization service is rate limiting this caller; retry with backoff
    /// (never a poison/refusal of the entry).
    #[error("tokenization service rate limited")]
    RateLimited,
    /// This service's credential is not entitled to detokenize (bad/expired token,
    /// missing scope, tenant not authorized): an operator problem, not retryable.
    #[error("tokenization service denied the caller")]
    Denied,
    /// The request itself was malformed (a caller bug, never a membership answer).
    #[error("tokenization request was invalid")]
    Invalid,
}

/// Community-scoped detokenization of a user UUID to the raw Discord user id.
/// The answer is a PII value: callers keep it only in a [`SlotValue`] for the
/// span of one operation. An implementation MUST refuse a UUID that is not an
/// identity of `community_id` and MUST NOT probe per-uuid after a miss.
/// `tenant_id` is the numeric tenant of the MAC-bound stamp.
#[allow(async_fn_in_trait)] // service-binary-internal
pub trait PlatformUserResolver: Send + Sync {
    /// The raw platform user id of `user_uuid` within `community_id`.
    async fn resolve(
        &self,
        tenant_id: i64,
        community_id: i64,
        user_uuid: &str,
    ) -> Result<SlotValue, UserResolveError>;
}

/// The default resolver: no tokenization service is wired, so every user
/// reference is refused loudly (never a guess, never a raw-id passthrough).
#[derive(Clone, Copy, Debug, Default)]
pub struct NoUserResolver;

impl PlatformUserResolver for NoUserResolver {
    async fn resolve(&self, _t: i64, _c: i64, _u: &str) -> Result<SlotValue, UserResolveError> {
        Err(UserResolveError::Unavailable)
    }
}

fn refused(reason: &'static str) -> ExecError {
    ExecError::Slot(SlotError::Refused(reason))
}

/// Counts and returns a refusal. Only fixed codes reach the label.
fn deny(kind: &str, e: ExecError) -> ExecError {
    let reason = e.code();
    let kind = match kind {
        "dm.send" => "dm.send",
        "role.add" => "role.add",
        "role.remove" => "role.remove",
        "event.create" => "event.create",
        "event.update" => "event.update",
        "event.cancel" => "event.cancel",
        "event.delete" => "event.delete",
        _ => "other",
    };
    DUTY_REFUSED.with_label_values(&[kind, reason]).inc();
    e
}

/// Canonical lowercase 8-4-4-4-12 hex UUID, or `None`.
fn canonical_uuid(s: &str) -> Option<String> {
    let b = s.as_bytes();
    let ok = b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit(),
        });
    ok.then(|| s.to_ascii_lowercase())
}

/// The entry's recipient as a canonical UUID: accepts the bare UUID or the
/// `{user:<uuid>}` wrapper; a raw snowflake (or anything else) is refused --
/// raw platform ids never enter the executor.
fn user_uuid(action: &OutboundAction) -> Result<String, ExecError> {
    let raw = match action {
        OutboundAction::DmSend { user_id, .. } | OutboundAction::RoleOp { user_id, .. } => user_id,
        _ => return Err(ExecError::BadAction),
    };
    let inner = raw
        .strip_prefix("{user:")
        .and_then(|r| r.strip_suffix('}'))
        .unwrap_or(raw);
    canonical_uuid(inner).ok_or_else(|| refused("user_not_tokenized"))
}

fn placeholder(uuid: &str) -> String {
    format!("{{user:{uuid}}}")
}

/// The connector payload for an event op: the producer's event field object
/// (create/update) plus the event id (update/cancel/delete). The host adds NO
/// guild/channel field; the connector refuses any that the producer smuggled
/// into the field object.
pub(super) fn event_payload(
    kind: EventKind,
    event_id: Option<&str>,
    event_json: Option<&str>,
) -> Result<String, ExecError> {
    let mut obj = match (kind, event_json) {
        (EventKind::Create | EventKind::Update, Some(raw)) => {
            match serde_json::from_str::<serde_json::Value>(raw) {
                Ok(serde_json::Value::Object(m)) => m,
                _ => return Err(ExecError::BadAction),
            }
        }
        (EventKind::Cancel | EventKind::Delete, None) => serde_json::Map::new(),
        _ => return Err(ExecError::BadAction),
    };
    match (kind, event_id) {
        (EventKind::Create, None) => {}
        (EventKind::Update | EventKind::Cancel | EventKind::Delete, Some(id)) => {
            obj.insert("event_id".to_string(), json!(id));
        }
        _ => return Err(ExecError::BadAction),
    }
    serde_json::to_string(&serde_json::Value::Object(obj)).map_err(|_| ExecError::BadAction)
}

/// Cached permissions of one guild's roles.
struct RoleSet {
    fetched: Instant,
    perms: HashMap<String, u64>,
}

/// The Discord [`DutyHook`]. See the module doc.
pub struct DiscordDuties<C: DiscordCredentialSource, U: PlatformUserResolver> {
    creds: C,
    users: U,
    api_base: String,
    ttl: Duration,
    roles: Mutex<HashMap<u64, RoleSet>>,
}

impl<C: DiscordCredentialSource, U: PlatformUserResolver> DiscordDuties<C, U> {
    /// Production wiring: the real REST base, [`ROLE_CACHE_TTL`].
    #[must_use]
    pub fn new(creds: C, users: U) -> Self {
        Self::with_endpoint(creds, users, API_BASE.to_string(), ROLE_CACHE_TTL)
    }

    /// Explicit REST base + role-cache TTL (tests point it at a local fake).
    #[must_use]
    pub fn with_endpoint(creds: C, users: U, api_base: String, ttl: Duration) -> Self {
        Self {
            creds,
            users,
            api_base,
            ttl,
            roles: Mutex::new(HashMap::new()),
        }
    }

    fn community(cx: &DutyCx<'_>) -> Result<i64, ExecError> {
        cx.community_id
            .filter(|c| *c > 0)
            .ok_or_else(|| refused("no_community_scope"))
    }

    async fn binding(&self, cx: &DutyCx<'_>) -> Result<(i64, DiscordBinding), ExecError> {
        let community = Self::community(cx)?;
        let b = self
            .creds
            .binding(Some(community))
            .await
            .map_err(|e| match e {
                // Several usable bindings for the community: the guild is never guessed.
                CredError::AmbiguousBinding => refused("ambiguous_paired_guild"),
                e => ExecError::Slot(map_cred(e)),
            })?;
        Ok((community, b))
    }

    /// The raw Discord user id of the entry's recipient, scoped to the
    /// community. Never logged.
    async fn raw_user(
        &self,
        community: i64,
        action: &OutboundAction,
    ) -> Result<SlotValue, ExecError> {
        let uuid = user_uuid(action)?;
        // The stamp's tenant is MAC-bound; without it detokenization is refused.
        let tenant = match action {
            OutboundAction::DmSend { tenant_id, .. } | OutboundAction::RoleOp { tenant_id, .. } => {
                tenant_id.filter(|t| *t > 0)
            }
            _ => None,
        }
        .ok_or_else(|| refused("no_tenant_scope"))?;
        match self.users.resolve(tenant, community, &uuid).await {
            Ok(v) if is_snowflake(v.expose()) => Ok(v),
            // A resolver answer that is not a snowflake is never used.
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

    /// `send_dm_in_community`: the pinned channel is in the community's guild
    /// AND the recipient is a member of it.
    async fn dm_gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError> {
        let (community, binding) = self.binding(cx).await?;
        let rest = Rest {
            client: cx.client,
            base: &self.api_base,
            token: binding.bot_token.expose(),
        };
        let channel = cx.pinned_channel.ok_or_else(|| refused("no_origin_pin"))?;
        match rest.channel_guild(channel).await {
            Ok(Some(g)) if g == binding.guild_id => {}
            // Foreign guild, a guild-less channel, or an unreadable one.
            Ok(_) | Err(SlotError::Refused("channel_not_owned")) => {
                return Err(refused("not_in_community"))
            }
            Err(e) => return Err(ExecError::Slot(e)),
        }
        let user = self.raw_user(community, cx.action).await?;
        let resp = rest
            .call(
                reqwest::Method::GET,
                &format!("/guilds/{}/members/{}", binding.guild_id, user.expose()),
                None,
            )
            .await
            .map_err(ExecError::Slot)?;
        match classify_membership(resp.status, &resp.body) {
            Membership::Member => Ok(()),
            Membership::NotMember => Err(refused("not_in_community")),
            Membership::Unknown => Err(ExecError::Slot(SlotError::Unavailable(
                "membership_lookup_failed",
            ))),
        }
    }

    /// Role gate: the pinned channel is in the community's guild, the key is
    /// allowlisted and its role is unprivileged, and the user is one of the
    /// community's identities.
    async fn role_gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError> {
        let (community, binding) = self.binding(cx).await?;
        let rest = Rest {
            client: cx.client,
            base: &self.api_base,
            token: binding.bot_token.expose(),
        };
        // Where the op is allowed to land. A channel-pinned op proves its community by the
        // channel's guild; a guild-scoped op (gateway / external event, no channel) lands ONLY
        // in the community's own binding guild, and any guild the producer asserts (the source
        // guild of a gateway event) must equal it.
        let guild_scope = match cx.action {
            OutboundAction::RoleOp { origin_channel, .. } => role_guild_scope(origin_channel),
            _ => None,
        };
        match (cx.pinned_channel, guild_scope) {
            (Some(channel), None) => match rest.channel_guild(channel).await {
                Ok(Some(g)) if g == binding.guild_id => {}
                Ok(_) | Err(SlotError::Refused("channel_not_owned")) => {
                    return Err(refused("channel_not_owned"))
                }
                Err(e) => return Err(ExecError::Slot(e)),
            },
            (None, Some(asserted)) => {
                if asserted.is_some_and(|g| g != binding.guild_id) {
                    return Err(refused("guild_mismatch"));
                }
            }
            _ => return Err(refused("no_origin_pin")),
        }
        self.managed_role(&rest, &binding, cx.action).await?;
        self.raw_user(community, cx.action).await?;
        Ok(())
    }

    /// Event gate: the community has a binding, the binding's guild is the one
    /// the producer expects, and the stamp carries its tenant.
    async fn event_gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError> {
        let OutboundAction::EventOp {
            expect_guild,
            tenant_id,
            ..
        } = cx.action
        else {
            return Err(ExecError::BadAction);
        };
        if tenant_id.filter(|t| *t > 0).is_none() {
            return Err(refused("no_tenant_scope"));
        }
        let (_, binding) = self.binding(cx).await?;
        if expect_guild
            .as_deref()
            .is_some_and(|g| g != binding.guild_id)
        {
            return Err(refused("guild_mismatch"));
        }
        Ok(())
    }

    /// Maps the entry's role KEY to its snowflake through the binding's
    /// allowlist and refuses a privileged role. Fail closed on any lookup
    /// problem.
    async fn managed_role(
        &self,
        rest: &Rest<'_>,
        binding: &DiscordBinding,
        action: &OutboundAction,
    ) -> Result<SlotValue, ExecError> {
        let OutboundAction::RoleOp { role_key, .. } = action else {
            return Err(ExecError::BadAction);
        };
        if !is_role_key(role_key) {
            return Err(refused("role_not_managed"));
        }
        let role_id = binding
            .managed_roles
            .get(role_key)
            .ok_or_else(|| refused("role_not_managed"))?;
        let perms = self.role_perms(rest, &binding.guild_id, role_id).await?;
        if perms & FORBIDDEN_ROLE_PERMS != 0 {
            return Err(refused("role_privileged"));
        }
        Ok(SlotValue::new(role_id.clone()))
    }

    /// Permissions of `role_id` in `guild`, from the short-TTL cache or one
    /// guarded `GET /guilds/{guild}/roles`.
    async fn role_perms(
        &self,
        rest: &Rest<'_>,
        guild: &str,
        role_id: &str,
    ) -> Result<u64, ExecError> {
        let key: u64 = guild.parse().map_err(|_| refused("role_lookup_failed"))?;
        if let Some(p) = self.cached(key, role_id) {
            return p.ok_or_else(|| refused("role_not_found"));
        }
        let resp = rest
            .call(
                reqwest::Method::GET,
                &format!("/guilds/{guild}/roles"),
                None,
            )
            .await
            .map_err(|_| refused("role_lookup_failed"))?;
        if !(200..300).contains(&resp.status) {
            return Err(refused("role_lookup_failed"));
        }
        let list: Vec<serde_json::Value> =
            serde_json::from_slice(&resp.body).map_err(|_| refused("role_lookup_failed"))?;
        let mut perms = HashMap::new();
        for r in &list {
            let id = r.get("id").and_then(serde_json::Value::as_str);
            // `permissions` is a decimal string (or number on old API shapes);
            // anything unparseable poisons the whole answer (fail closed).
            let p = match r.get("permissions") {
                Some(serde_json::Value::String(s)) => s.parse::<u64>().ok(),
                Some(serde_json::Value::Number(n)) => n.as_u64(),
                _ => None,
            };
            match (id, p) {
                (Some(id), Some(p)) if is_snowflake(id) => {
                    perms.insert(id.to_string(), p);
                }
                _ => return Err(refused("role_lookup_failed")),
            }
        }
        let found = perms.get(role_id).copied();
        self.store(key, perms);
        found.ok_or_else(|| refused("role_not_found"))
    }

    /// `Some(Some(p))` hit, `Some(None)` fresh entry without the role, `None`
    /// miss/expired. The std mutex is never held across an await.
    fn cached(&self, guild: u64, role_id: &str) -> Option<Option<u64>> {
        let map = self
            .roles
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let set = map.get(&guild)?;
        (set.fetched.elapsed() < self.ttl).then(|| set.perms.get(role_id).copied())
    }

    /// Inserts a guild's role set; the map stays bounded.
    fn store(&self, guild: u64, perms: HashMap<String, u64>) {
        let mut map = self
            .roles
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if map.len() >= ROLE_CACHE_MAX_GUILDS && !map.contains_key(&guild) {
            let ttl = self.ttl;
            map.retain(|_, s| s.fetched.elapsed() < ttl);
            if map.len() >= ROLE_CACHE_MAX_GUILDS {
                map.clear();
            }
        }
        map.insert(
            guild,
            RoleSet {
                fetched: Instant::now(),
                perms,
            },
        );
    }

    /// Opens the DM channel for the recipient; returns the `id` of Discord's
    /// reply, which is the ONLY source of `{dm.channel_id}`.
    async fn open_dm(
        &self,
        cx: &DutyCx<'_>,
        client: &GuardedClient,
    ) -> Result<SlotValue, ExecError> {
        let (community, binding) = self.binding(cx).await?;
        let user = self.raw_user(community, cx.action).await?;
        let rest = Rest {
            client,
            base: &self.api_base,
            token: binding.bot_token.expose(),
        };
        let body = json!({ "recipient_id": user.expose() });
        let resp = rest
            .call(reqwest::Method::POST, "/users/@me/channels", Some(&body))
            .await
            .map_err(ExecError::Slot)?;
        if !(200..300).contains(&resp.status) {
            return Err(refused("dm_open_failed"));
        }
        let v: serde_json::Value =
            serde_json::from_slice(&resp.body).map_err(|_| refused("dm_open_failed"))?;
        match v.get("id").and_then(serde_json::Value::as_str) {
            Some(id) if is_snowflake(id) => Ok(SlotValue::new(id.to_string())),
            _ => Err(refused("dm_open_failed")),
        }
    }
}

impl<C: DiscordCredentialSource, U: PlatformUserResolver> DutyHook for DiscordDuties<C, U> {
    fn claims(&self, platform: &str, kind: &str) -> bool {
        platform == "discord"
            && matches!(
                kind,
                "dm.send"
                    | "role.add"
                    | "role.remove"
                    | "event.create"
                    | "event.update"
                    | "event.cancel"
                    | "event.delete"
            )
    }

    async fn gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError> {
        let kind = cx.action.op();
        let res = match cx.action {
            OutboundAction::DmSend { .. } => self.dm_gate(cx).await,
            OutboundAction::RoleOp { .. } => self.role_gate(cx).await,
            OutboundAction::EventOp { .. } => self.event_gate(cx).await,
            _ => Err(ExecError::BadAction),
        };
        res.map_err(|e| deny(kind, e))
    }

    async fn bind_template(&self, cx: &DutyCx<'_>, tpl: &mut RequestTpl) -> Result<(), ExecError> {
        let kind = cx.action.op();
        // Event requests carry no user and no placeholders to normalize.
        if matches!(cx.action, OutboundAction::EventOp { .. }) {
            return Ok(());
        }
        let uuid = user_uuid(cx.action).map_err(|e| deny(kind, e))?;
        let me = placeholder(&uuid);
        match cx.action {
            OutboundAction::DmSend { .. } => {
                // Consume the pseudo-header: it must name exactly this entry's
                // recipient and is NEVER sent to Discord.
                let mut kept = Vec::with_capacity(tpl.headers.len());
                for (n, v) in std::mem::take(&mut tpl.headers) {
                    if n.eq_ignore_ascii_case(DM_RECIPIENT_HEADER) {
                        if v != me {
                            return Err(deny(kind, refused("recipient_mismatch")));
                        }
                    } else {
                        kept.push((n, v));
                    }
                }
                tpl.headers = kept;
            }
            OutboundAction::RoleOp { role_key, .. } => {
                tpl.url = tpl.url.replacen(&me, "{target.user}", 1).replacen(
                    &format!("{{target.role.{role_key}}}"),
                    "{target.role}",
                    1,
                );
            }
            _ => return Err(ExecError::BadAction),
        }
        Ok(())
    }

    async fn payload(&self, cx: &DutyCx<'_>) -> Result<String, ExecError> {
        let kind = cx.action.op();
        if let OutboundAction::EventOp {
            kind: ek,
            event_id,
            event_json,
            ..
        } = cx.action
        {
            return event_payload(*ek, event_id.as_deref(), event_json.as_deref())
                .map_err(|e| deny(kind, e));
        }
        let uuid = user_uuid(cx.action).map_err(|e| deny(kind, e))?;
        let v = match cx.action {
            OutboundAction::DmSend { text, .. } => {
                json!({ "recipient_id": placeholder(&uuid), "content": text })
            }
            OutboundAction::RoleOp { role_key, .. } => {
                json!({ "user_id": placeholder(&uuid), "role": role_key })
            }
            _ => return Err(ExecError::BadAction),
        };
        serde_json::to_string(&v).map_err(|_| ExecError::BadAction)
    }

    async fn dynamic_slot(
        &self,
        cx: &DutyCx<'_>,
        slot: &str,
        client: &GuardedClient,
    ) -> Result<SlotValue, ExecError> {
        let kind = cx.action.op();
        let res = match slot {
            "dm.channel_id" => self.open_dm(cx, client).await,
            "target.guild_id" => self
                .binding(cx)
                .await
                .map(|(_, b)| SlotValue::new(b.guild_id.clone())),
            "target.user" => match self.binding(cx).await {
                Ok((community, _)) => self.raw_user(community, cx.action).await,
                Err(e) => Err(e),
            },
            "target.role" => match self.binding(cx).await {
                Ok((_, b)) => {
                    let rest = Rest {
                        client,
                        base: &self.api_base,
                        token: b.bot_token.expose(),
                    };
                    self.managed_role(&rest, &b, cx.action).await
                }
                Err(e) => Err(e),
            },
            _ => Err(refused("unknown_duty_slot")),
        };
        res.map_err(|e| deny(kind, e))
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::outbound_creds::CredError;
    use crate::outbound_creds_discord::DiscordBinding;

    struct NoCreds;
    impl DiscordCredentialSource for NoCreds {
        async fn binding(&self, _c: Option<i64>) -> Result<DiscordBinding, CredError> {
            Err(CredError::NoBinding)
        }
    }

    fn duties(ttl: Duration) -> DiscordDuties<NoCreds, NoUserResolver> {
        DiscordDuties::with_endpoint(NoCreds, NoUserResolver, API_BASE.to_string(), ttl)
    }

    fn one(role: &str, perms: u64) -> HashMap<String, u64> {
        HashMap::from([(role.to_string(), perms)])
    }

    #[test]
    fn the_role_cache_is_bounded_by_guild_count() {
        let d = duties(Duration::from_secs(600));
        for g in 0..(ROLE_CACHE_MAX_GUILDS as u64 * 3) {
            d.store(g, one("1", 0));
            assert!(d.roles.lock().unwrap().len() <= ROLE_CACHE_MAX_GUILDS);
        }
        // the newest guild is always retained
        assert_eq!(
            d.cached(ROLE_CACHE_MAX_GUILDS as u64 * 3 - 1, "1"),
            Some(Some(0))
        );
    }

    #[test]
    fn expired_entries_are_evicted_before_the_map_is_cleared() {
        let d = duties(Duration::ZERO);
        for g in 0..(ROLE_CACHE_MAX_GUILDS as u64) {
            d.store(g, one("1", 0));
        }
        d.store(10_000, one("1", 0));
        assert_eq!(d.roles.lock().unwrap().len(), 1);
        // ttl 0: nothing is ever served from cache
        assert_eq!(d.cached(10_000, "1"), None);
    }

    #[test]
    fn cache_distinguishes_hit_missing_role_and_miss() {
        let d = duties(Duration::from_secs(60));
        d.store(5, one("7", 1024));
        assert_eq!(d.cached(5, "7"), Some(Some(1024)));
        assert_eq!(d.cached(5, "8"), Some(None));
        assert_eq!(d.cached(6, "7"), None);
    }

    #[test]
    fn uuid_and_placeholder_forms() {
        let u = "123E4567-E89B-12D3-A456-426614174000";
        assert_eq!(canonical_uuid(u).unwrap(), u.to_ascii_lowercase());
        for bad in [
            "",
            "123",
            "700000000000000001",
            "123e4567e89b12d3a456426614174000",
            "123e4567-e89b-12d3-a456-42661417400z",
        ] {
            assert!(canonical_uuid(bad).is_none(), "{bad}");
        }
        assert_eq!(placeholder("x"), "{user:x}");
    }
}
