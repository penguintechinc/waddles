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

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use bundle_host_http::guarded::GuardedClient;
use serde_json::json;

use super::discord_slots::{Rest, API_BASE};
use super::role_guild_scope;
use super::subst::SlotValue;
use super::{DutyCx, DutyHook, ExecError, RequestTpl, RoleBulkPlan, SlotError};
use crate::discord_rest::{classify_membership, is_snowflake, Membership};
use crate::outbound_creds::CredError;
use crate::outbound_creds_discord::{is_role_key, DiscordBinding, DiscordCredentialSource};
use crate::outbound_ops::{EventKind, OutboundAction, PolicyOp, RoleBulkKind};

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
    registry.register(Box::new(DUTY_REFUSED.clone()))?;
    registry.register(Box::new(MOD_ACTIONS.clone()))
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
        "mod.kick" => "mod.kick",
        "mod.ban" => "mod.ban",
        "mod.unban" => "mod.unban",
        "mod.timeout" => "mod.timeout",
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
        OutboundAction::RoleBulk {
            user_id: Some(user_id),
            ..
        }
        | OutboundAction::PolicyOp(PolicyOp {
            user_id: Some(user_id),
            ..
        }) => user_id,
        // A moderation op without a target user is a producer bug, never a guess.
        OutboundAction::PolicyOp(_) => return Err(refused("user_not_tokenized")),
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
            OutboundAction::DmSend { tenant_id, .. }
            | OutboundAction::RoleOp { tenant_id, .. }
            | OutboundAction::RoleBulk { tenant_id, .. }
            | OutboundAction::PolicyOp(PolicyOp { tenant_id, .. }) => tenant_id.filter(|t| *t > 0),
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

    /// Where a role op is allowed to land (see the `role_gate` doc): a channel-pinned op proves its
    /// community by the channel's guild; a guild-scoped op lands ONLY in the binding guild and any
    /// asserted source guild must equal it.
    async fn check_role_origin(
        &self,
        rest: &Rest<'_>,
        binding: &DiscordBinding,
        cx: &DutyCx<'_>,
    ) -> Result<(), ExecError> {
        let guild_scope = match cx.action {
            OutboundAction::RoleOp { origin_channel, .. }
            | OutboundAction::RoleBulk { origin_channel, .. } => role_guild_scope(origin_channel),
            OutboundAction::PolicyOp(p) => p.origin_channel.as_deref().and_then(role_guild_scope),
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
        Ok(())
    }

    /// Plans `role.set` / `role.list` (see [`RoleBulkPlan`]).
    async fn plan_role_bulk(&self, cx: &DutyCx<'_>) -> Result<RoleBulkPlan, ExecError> {
        let OutboundAction::RoleBulk {
            kind, role_keys, ..
        } = cx.action
        else {
            return Err(ExecError::BadAction);
        };
        let (community, binding) = self.binding(cx).await?;
        let rest = Rest {
            client: cx.client,
            base: &self.api_base,
            token: binding.bot_token.expose(),
        };
        self.check_role_origin(&rest, &binding, cx).await?;
        // Every allowlisted key with its role id and whether it is assignable (unprivileged and
        // present in the guild). One cached `GET /guilds/{g}/roles` serves them all.
        let mut managed: BTreeMap<String, (String, bool)> = BTreeMap::new();
        for (key, id) in &binding.managed_roles {
            if !is_role_key(key) {
                continue;
            }
            let assignable = match self.role_perms(&rest, &binding.guild_id, id).await {
                Ok(p) => p & FORBIDDEN_ROLE_PERMS == 0,
                Err(ExecError::Slot(SlotError::Refused("role_not_found"))) => false,
                Err(e) => return Err(e),
            };
            managed.insert(key.clone(), (id.clone(), assignable));
        }
        match kind {
            RoleBulkKind::List => {
                let roles: Vec<_> = managed
                    .iter()
                    .map(|(k, (_, a))| json!({"key": k, "assignable": a}))
                    .collect();
                Ok(RoleBulkPlan::Data(json!({ "roles": roles }).to_string()))
            }
            RoleBulkKind::Set => {
                // Desired keys must be allowlisted AND assignable; anything else refuses the whole
                // op before a single change is made.
                for k in role_keys {
                    match managed.get(k) {
                        None => return Err(refused("role_not_managed")),
                        Some((_, false)) => return Err(refused("role_privileged")),
                        Some(_) => {}
                    }
                }
                let raw = self.raw_user(community, cx.action).await?;
                let resp = rest
                    .call(
                        reqwest::Method::GET,
                        &format!("/guilds/{}/members/{}", binding.guild_id, raw.expose()),
                        None,
                    )
                    .await
                    .map_err(ExecError::Slot)?;
                let current: HashSet<String> = match classify_membership(resp.status, &resp.body) {
                    Membership::Member => {
                        let v: serde_json::Value =
                            serde_json::from_slice(&resp.body).map_err(|_| {
                                ExecError::Slot(SlotError::Unavailable("membership_lookup_failed"))
                            })?;
                        v.get("roles")
                            .and_then(serde_json::Value::as_array)
                            .ok_or(ExecError::Slot(SlotError::Unavailable(
                                "membership_lookup_failed",
                            )))?
                            .iter()
                            .filter_map(|r| r.as_str().map(str::to_string))
                            .collect()
                    }
                    Membership::NotMember => return Err(refused("not_in_community")),
                    Membership::Unknown => {
                        return Err(ExecError::Slot(SlotError::Unavailable(
                            "membership_lookup_failed",
                        )))
                    }
                };
                // Minimal diff within the allowlist: add missing desired, remove held managed
                // (assignable) roles that are not desired. Unmanaged roles are never touched.
                let mut ops = Vec::new();
                for (key, (id, assignable)) in &managed {
                    let want = role_keys.contains(key);
                    let has = current.contains(id);
                    if want && !has {
                        ops.push((true, key.clone()));
                    } else if !want && has && *assignable {
                        ops.push((false, key.clone()));
                    }
                }
                Ok(RoleBulkPlan::Apply(ops))
            }
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
        self.check_role_origin(&rest, &binding, cx).await?;
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

    /// Moderation gate (`mod.kick|ban|unban|timeout`): the community has a binding, the stamp
    /// carries its tenant, the origin proves the community (channel in the binding guild, or an
    /// asserted `guild:` equal to it), the target is one of the community's tokenized identities,
    /// and -- from live guild facts -- the bot outranks the target in the role hierarchy, the
    /// target is neither the owner nor the bot, and the bot holds the needed permission. Every
    /// failure is an explicit fixed reason code; nothing is sent. On success one PII-free audit
    /// event (op, community, tenant, target UUID) is emitted.
    async fn mod_gate(&self, cx: &DutyCx<'_>, p: &PolicyOp) -> Result<(), ExecError> {
        let (community, binding) = self.binding(cx).await?;
        if p.tenant_id.filter(|t| *t > 0).is_none() {
            return Err(refused("no_tenant_scope"));
        }
        let rest = Rest {
            client: cx.client,
            base: &self.api_base,
            token: binding.bot_token.expose(),
        };
        self.check_role_origin(&rest, &binding, cx).await?;
        // Validate the arguments (and the timeout bound) before any platform read.
        mod_payload(p, chrono::Utc::now())?;
        let target = self.raw_user(community, cx.action).await?;
        if p.op != "mod.unban" {
            let facts = self
                .guild_facts(&rest, &binding.guild_id, target.expose())
                .await?;
            check_hierarchy(p.op, &facts).map_err(refused)?;
        }
        let uuid = user_uuid(cx.action)?;
        MOD_ACTIONS
            .with_label_values(&[mod_label(p.op), "authorized"])
            .inc();
        tracing::info!(
            target: "audit",
            audit = true,
            event = "discord.moderation",
            op = p.op,
            community_id = community,
            tenant_id = p.tenant_id.unwrap_or_default(),
            target_user = %uuid,
            outcome = "authorized",
            "discord moderation authorized by host gate"
        );
        Ok(())
    }

    /// Live guild facts for the hierarchy check: owner, the bot's id/roles, every role's
    /// position + permissions and (when a member) the target's roles. Never cached.
    async fn guild_facts(
        &self,
        rest: &Rest<'_>,
        guild: &str,
        target: &str,
    ) -> Result<GuildFacts, ExecError> {
        let lookup = || ExecError::Slot(SlotError::Unavailable("moderation_lookup_failed"));
        let get = |path: String| async move {
            let resp = rest
                .call(reqwest::Method::GET, &path, None)
                .await
                .map_err(ExecError::Slot)?;
            Ok::<_, ExecError>(resp)
        };
        let json_of = |resp: &bundle_host_http::guarded::GuardedResponse| {
            serde_json::from_slice::<serde_json::Value>(&resp.body).map_err(|_| lookup())
        };
        let g = get(format!("/guilds/{guild}")).await?;
        if !(200..300).contains(&g.status) {
            return Err(lookup());
        }
        let owner_id = json_of(&g)?
            .get("owner_id")
            .and_then(serde_json::Value::as_str)
            .filter(|s| is_snowflake(s))
            .ok_or_else(lookup)?
            .to_string();
        let me = get("/users/@me".to_string()).await?;
        if !(200..300).contains(&me.status) {
            return Err(lookup());
        }
        let bot_id = json_of(&me)?
            .get("id")
            .and_then(serde_json::Value::as_str)
            .filter(|s| is_snowflake(s))
            .ok_or_else(lookup)?
            .to_string();
        let rl = get(format!("/guilds/{guild}/roles")).await?;
        if !(200..300).contains(&rl.status) {
            return Err(lookup());
        }
        let mut roles = HashMap::new();
        for r in json_of(&rl)?.as_array().ok_or_else(lookup)? {
            let id = r.get("id").and_then(serde_json::Value::as_str);
            let pos = r.get("position").and_then(serde_json::Value::as_i64);
            let perms = match r.get("permissions") {
                Some(serde_json::Value::String(s)) => s.parse::<u64>().ok(),
                Some(serde_json::Value::Number(n)) => n.as_u64(),
                _ => None,
            };
            match (id, pos, perms) {
                (Some(id), Some(pos), Some(perms)) if is_snowflake(id) => {
                    roles.insert(id.to_string(), (pos, perms));
                }
                _ => return Err(lookup()),
            }
        }
        let member_roles = |resp: &bundle_host_http::guarded::GuardedResponse| {
            let v = json_of(resp)?;
            let arr = v
                .get("roles")
                .and_then(serde_json::Value::as_array)
                .ok_or_else(lookup)?;
            Ok::<_, ExecError>(
                arr.iter()
                    .filter_map(|r| r.as_str().map(str::to_string))
                    .collect::<Vec<_>>(),
            )
        };
        let bm = get(format!("/guilds/{guild}/members/{bot_id}")).await?;
        if !(200..300).contains(&bm.status) {
            return Err(lookup());
        }
        let bot_roles = member_roles(&bm)?;
        let tm = get(format!("/guilds/{guild}/members/{target}")).await?;
        let target_roles = match classify_membership(tm.status, &tm.body) {
            Membership::Member => Some(member_roles(&tm)?),
            Membership::NotMember => None,
            Membership::Unknown => return Err(lookup()),
        };
        Ok(GuildFacts {
            guild_id: guild.to_string(),
            owner_id,
            bot_id,
            target_id: target.to_string(),
            roles,
            bot_roles,
            target_roles,
        })
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

/// The moderation kinds this hook owns.
const MOD_KINDS: &[&str] = &["mod.kick", "mod.ban", "mod.unban", "mod.timeout"];
/// `KICK_MEMBERS`.
const PERM_KICK_MEMBERS: u64 = 1 << 1;
/// `BAN_MEMBERS`.
const PERM_BAN_MEMBERS: u64 = 1 << 2;
/// `MODERATE_MEMBERS` (timeouts).
const PERM_MODERATE_MEMBERS: u64 = 1 << 40;
/// Discord's cap on a timeout, enforced here against the host clock.
pub const MAX_TIMEOUT_SECONDS: u64 = 28 * 24 * 60 * 60;
/// Discord's cap on `delete_message_seconds`.
pub const MAX_DELETE_MESSAGE_SECONDS: u64 = 604_800;

static MOD_ACTIONS: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_discord_moderation_total",
            "Discord moderation actions that passed the host gate, by op and outcome",
        ),
        &["op", "outcome"],
    )
    .expect("valid metric definition")
});

/// Count of authorized moderation actions for `op` (test/observability helper).
#[must_use]
pub fn moderation_count(op: &str, outcome: &str) -> u64 {
    MOD_ACTIONS
        .with_label_values(&[mod_label(op), outcome])
        .get()
}

/// Fixed metric label for a moderation op.
fn mod_label(op: &str) -> &'static str {
    MOD_KINDS
        .iter()
        .copied()
        .find(|k| *k == op)
        .unwrap_or("other")
}

/// Live guild state the hierarchy check decides from.
#[derive(Debug)]
pub(crate) struct GuildFacts {
    pub(crate) guild_id: String,
    pub(crate) owner_id: String,
    pub(crate) bot_id: String,
    pub(crate) target_id: String,
    /// role id -> (position, permissions); includes `@everyone` (id == guild id).
    pub(crate) roles: HashMap<String, (i64, u64)>,
    pub(crate) bot_roles: Vec<String>,
    /// `None` = the target is not a guild member.
    pub(crate) target_roles: Option<Vec<String>>,
}

impl GuildFacts {
    /// `(highest position, OR of permissions)` over `member_roles` + `@everyone`; `None` when a
    /// held role is missing from the guild's role list (fail closed).
    fn standing(&self, member_roles: &[String]) -> Option<(i64, u64)> {
        let mut pos = 0;
        let mut perms = 0;
        let everyone = self.roles.get(&self.guild_id).copied().unwrap_or((0, 0));
        pos = pos.max(everyone.0);
        perms |= everyone.1;
        for r in member_roles {
            let (p, bits) = self.roles.get(r).copied()?;
            pos = pos.max(p);
            perms |= bits;
        }
        Some((pos, perms))
    }
}

/// The role-hierarchy decision: `Err(reason)` is a fixed refusal code.
pub(crate) fn check_hierarchy(op: &str, f: &GuildFacts) -> Result<(), &'static str> {
    if f.target_id == f.bot_id {
        return Err("target_is_self");
    }
    if f.target_id == f.owner_id {
        return Err("target_is_owner");
    }
    let (bot_pos, bot_perms) = f.standing(&f.bot_roles).ok_or("role_lookup_failed")?;
    let needed = match op {
        "mod.kick" => PERM_KICK_MEMBERS,
        "mod.ban" => PERM_BAN_MEMBERS,
        "mod.timeout" => PERM_MODERATE_MEMBERS,
        _ => return Err("not_a_moderation_op"),
    };
    if bot_perms & (PERM_ADMINISTRATOR | needed) == 0 {
        return Err("bot_lacks_permission");
    }
    let Some(target_roles) = &f.target_roles else {
        // A ban of a non-member is allowed (pre-emptive ban); kick/timeout need a member.
        return if op == "mod.ban" {
            Ok(())
        } else {
            Err("not_in_community")
        };
    };
    let (target_pos, target_perms) = f.standing(target_roles).ok_or("role_lookup_failed")?;
    if op == "mod.timeout" && target_perms & PERM_ADMINISTRATOR != 0 {
        return Err("target_is_admin");
    }
    // The guild owner outranks every role; otherwise the bot's highest role must be strictly
    // above the target's.
    if f.bot_id != f.owner_id && bot_pos <= target_pos {
        return Err("role_hierarchy");
    }
    Ok(())
}

/// The connector payload for a moderation op: the tokenized target plus ONLY the validated
/// arguments from the producer's `text` JSON. A timeout's `duration_seconds` becomes the
/// absolute `until` against the host clock `now` (capped at 28 days); `clear` lifts it.
pub(crate) fn mod_payload(
    p: &PolicyOp,
    now: chrono::DateTime<chrono::Utc>,
) -> Result<String, ExecError> {
    let bad = || ExecError::BadAction;
    let args: serde_json::Map<String, serde_json::Value> = match p.text.as_deref() {
        None => serde_json::Map::new(),
        Some(t) => match serde_json::from_str(t) {
            Ok(serde_json::Value::Object(m)) => m,
            _ => return Err(bad()),
        },
    };
    let allowed: &[&str] = match p.op {
        "mod.kick" | "mod.unban" => &[],
        "mod.ban" => &["delete_message_seconds"],
        "mod.timeout" => &["duration_seconds", "clear"],
        _ => return Err(bad()),
    };
    if args.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(bad());
    }
    let uuid = user_uuid(&OutboundAction::PolicyOp(p.clone()))?;
    let mut out = serde_json::json!({ "user_id": placeholder(&uuid) });
    match p.op {
        "mod.ban" => {
            if let Some(v) = args.get("delete_message_seconds") {
                let n = v
                    .as_u64()
                    .filter(|n| *n <= MAX_DELETE_MESSAGE_SECONDS)
                    .ok_or_else(bad)?;
                out["delete_message_seconds"] = n.into();
            }
        }
        "mod.timeout" => match (args.get("duration_seconds"), args.get("clear")) {
            (Some(d), None) => {
                let secs = d
                    .as_u64()
                    .filter(|n| (1..=MAX_TIMEOUT_SECONDS).contains(n))
                    .ok_or_else(bad)?;
                let secs = i64::try_from(secs).map_err(|_| bad())?;
                let until = now + chrono::Duration::seconds(secs);
                out["until"] = until.format("%Y-%m-%dT%H:%M:%SZ").to_string().into();
            }
            (None, Some(serde_json::Value::Bool(true))) => out["clear"] = true.into(),
            _ => return Err(bad()),
        },
        _ => {}
    }
    serde_json::to_string(&out).map_err(|_| bad())
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
                    | "role.set"
                    | "role.list"
                    | "mod.kick"
                    | "mod.ban"
                    | "mod.unban"
                    | "mod.timeout"
            )
    }

    async fn role_bulk(&self, cx: &DutyCx<'_>) -> Result<RoleBulkPlan, ExecError> {
        let kind = cx.action.op();
        self.plan_role_bulk(cx).await.map_err(|e| deny(kind, e))
    }

    async fn gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError> {
        let kind = cx.action.op();
        let res = match cx.action {
            OutboundAction::DmSend { .. } => self.dm_gate(cx).await,
            OutboundAction::RoleOp { .. } => self.role_gate(cx).await,
            OutboundAction::EventOp { .. } => self.event_gate(cx).await,
            OutboundAction::PolicyOp(p) if MOD_KINDS.contains(&p.op) => self.mod_gate(cx, p).await,
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
            OutboundAction::PolicyOp(_) => {
                tpl.url = tpl.url.replacen(&me, "{target.user}", 1);
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
        if let OutboundAction::PolicyOp(p) = cx.action {
            return mod_payload(p, chrono::Utc::now()).map_err(|e| deny(kind, e));
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

#[cfg(test)]
#[path = "discord_mod_tests.rs"]
mod mod_tests;
