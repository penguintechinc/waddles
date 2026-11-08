//! Generic host-side platform tool serving (replaces the per-platform hand-written handlers).
//!
//! A platform SERVICE bundle (twitch / discord / youtube / kick) DECLARES tools whose execution
//! is host-side: the guest `call` refuses them, the HOST translates an authorized `tool.invoke`
//! into a pinned producer op executed by svc_ingest's `sender_exec`. The translation is driven
//! entirely by the bundle's manifest declaration ([`HostOpDecl`], `exported_tools[].host_op`):
//! tool name -> registered op kind (`bundle_capability_gate::platform_ops`) + argument mapping.
//! No platform knowledge lives in this module beyond the typed op rules below.
//!
//! ```text
//!  tool.invoke ──► planner (grant, matrix, required_scope vs verified actor) ──► HostOpDecl
//!       │ validate args (closed set, never a guild/channel/credential/id)
//!       ▼
//!  origin pin ► drain-ready ► throttle ► detokenize user (hub-api, host-side, fail closed)
//!       ▼
//!  signed envelope (pin_sig) ──LPUSH──► sender_exec ──ack {ok,id|data}──► allowlisted result
//! ```
//!
//! Security properties (each enforced + tested): the destination is never caller-named; the
//! tokenized user UUID is resolved to the platform id HOST-side, community-scoped, and a
//! failure refuses the call; only manifest-allowlisted keys of the platform reply reach the
//! caller (a created webhook's token is dropped); an op the platform table does not register is
//! an explicit refusal, never a default.
//!
//! `TRANSITIONAL_DECLS` carries the declarations of the tools the Discord bundle exported
//! BEFORE manifests could declare them (member roles, scheduled events). A manifest
//! `host_op` always wins; delete the table once the Discord bundle declares them.

use bundle_active_set::{HostOpDecl, HostOpOrigin, HostOpUserWire};
use bundle_tool_call::{ToolPlan, ToolRefusal};
use serde_json::{json, Map, Value};

use super::{
    is_discord_snowflake, is_role_key, now_epoch_ms, pin_twitch_channel, InvokeScope, RelayOp,
    RelayQueue, StageCapabilities, DISCORD_DRAIN_READY_KEY, DISCORD_OP_ACK_KEY_PREFIX,
    DISCORD_OP_ACK_POLL, OUTBOUND_SCHEMA_VERSION,
};
use crate::actor::ActorError;
use crate::platform_user::{is_wire_safe_id, ResolveError};
use bundle_capability_gate::resource::{AppScopedResource, ResourceRef};
use bundle_capability_gate::PermissionId;
use bundle_host_kv::KvBackend;
use penguin_bundle_host::wire::HostResultError;

/// Stable refusal codes of the host tool path. The guest-facing `tool-error` is a closed WIT
/// enum, so the code is the pinned PREFIX of the refusal message (`"<code>: <detail>"`, always
/// on `forbidden` / `invalid_args`): an app maps on the prefix, never on prose. (A dedicated
/// `tool-error` variant is a WIT follow-up.)
pub(crate) const PROTECTED_TARGET: &str = "protected_target";
pub(crate) const NO_LINKED_ACCOUNT: &str = "no_linked_account";
pub(crate) const NO_BOUND_CHANNEL: &str = "no_bound_channel";
pub(crate) const NOT_SUPPORTED: &str = "not_supported";
pub(crate) const SCOPE_DENIED: &str = "scope_denied";

/// Community roles that are never a valid target of a destructive moderation op (the channel
/// owner / community owner, admins and moderators), whatever the platform API would permit.
const PROTECTED_ROLES: &[&str] = &["owner", "community-owner", "community-admin", "moderator"];

/// Destructive moderation ops subject to the protected-target check.
fn is_destructive_moderation(op: &str) -> bool {
    matches!(op, "mod.ban" | "mod.timeout" | "mod.kick")
}

/// Verified external, non-chat platforms whose events may drive guild-scoped (channel-less)
/// community ops (binding guild only, `guild:-`). Everything else is refused; extend deliberately.
const EXTERNAL_GUILD_SCOPE_PLATFORMS: &[&str] = &["wix"];

/// Most strings in a `text_list`.
const MAX_TEXT_LIST: usize = 16;
/// Largest wire `text` a host tool may build (platform bodies are bounded again by the consumer).
const MAX_TEXT_BYTES: usize = 16_384;

/// The Discord service bundle's pre-declaration tools: `(tool, decl-json)`. Same schema as the
/// manifest `hostOp` summary object. See the module doc.
const TRANSITIONAL_DECLS: &[(&str, &str)] = &[
    (
        "discord.member.roles.add",
        r#"{"platform":"discord","op":"role.add","user":"user","userWire":"placeholder","text":"role_key","required":["user","role_key"],"origin":"guild_scope"}"#,
    ),
    (
        "discord.member.roles.remove",
        r#"{"platform":"discord","op":"role.remove","user":"user","userWire":"placeholder","text":"role_key","required":["user","role_key"],"origin":"guild_scope"}"#,
    ),
    (
        "discord.member.roles.set",
        r#"{"platform":"discord","op":"role.set","user":"user","userWire":"placeholder","textList":"role_keys","required":["user","role_keys"],"origin":"guild_scope"}"#,
    ),
    (
        "discord.roles.list",
        r#"{"platform":"discord","op":"role.list","origin":"guild_scope","returns":["roles"]}"#,
    ),
    (
        "discord.scheduled_event.create",
        r#"{"platform":"discord","op":"event.create","origin":"none","returns":["id"]}"#,
    ),
    (
        "discord.scheduled_event.update",
        r#"{"platform":"discord","op":"event.update","origin":"none","returns":["id"]}"#,
    ),
    (
        "discord.scheduled_event.cancel",
        r#"{"platform":"discord","op":"event.cancel","origin":"none"}"#,
    ),
    (
        "discord.scheduled_event.delete",
        r#"{"platform":"discord","op":"event.delete","origin":"none"}"#,
    ),
];

/// The transitional declaration of `(target, tool)`, if it is one of the pre-declaration tools.
#[must_use]
pub(crate) fn transitional_decl(target: &str, tool: &str) -> Option<HostOpDecl> {
    if target != super::event_sync::DISCORD_SERVICE_APP_ID {
        return None;
    }
    let (_, raw) = TRANSITIONAL_DECLS.iter().find(|(t, _)| *t == tool)?;
    let v: Value = serde_json::from_str(raw).ok()?;
    HostOpDecl::from_summary(&v)
}

/// How the host executes a declared op.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum OpClass {
    /// A platform-table op served by the generic confirmed producer path.
    Registered(&'static str),
    /// Discord managed-role ops (`role.add|remove|set|list`): typed, key-allowlisted.
    Role(&'static str),
    /// Discord scheduled-event ops: served by the event-sync producer.
    Event,
}

fn classify(decl: &HostOpDecl) -> Result<OpClass, ToolRefusal> {
    if decl.platform == "discord" {
        for role in ["role.add", "role.remove", "role.set", "role.list"] {
            if decl.op == role {
                return Ok(OpClass::Role(role));
            }
        }
        if matches!(
            decl.op.as_str(),
            "event.create" | "event.update" | "event.cancel" | "event.delete"
        ) {
            return Ok(OpClass::Event);
        }
    }
    bundle_capability_gate::platform_ops::registered(&decl.platform, &decl.op)
        .map(OpClass::Registered)
        .ok_or_else(|| {
            ToolRefusal::Forbidden(format!(
                "{NOT_SUPPORTED}: host op {:?} is not registered for platform {:?}",
                decl.op, decl.platform
            ))
        })
}

fn is_canonical_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_digit() || (b'a'..=b'f').contains(c),
        })
}

fn bad(msg: impl Into<String>) -> ToolRefusal {
    ToolRefusal::InvalidArgs(msg.into())
}

/// The call's arguments after validation against the declaration: the closed set of wire
/// fields the producer op carries.
#[derive(Debug, Default, PartialEq)]
struct WireArgs {
    /// Tokenized user UUID (still a token here).
    user_uuid: Option<String>,
    message_id: Option<String>,
    text: Option<String>,
}

/// Validates `args` against `decl` and returns the wire fields. Any key the declaration does
/// not map is REFUSED (never ignored): a caller naming a guild / channel / credential / raw id
/// is an injection attempt and fails loudly.
fn validate_args(decl: &HostOpDecl, class: OpClass, args: &Value) -> Result<WireArgs, ToolRefusal> {
    let obj = args
        .as_object()
        .ok_or_else(|| bad("args must be an object"))?;
    let mut allowed: Vec<&str> = Vec::new();
    allowed.extend(decl.user.as_deref());
    allowed.extend(decl.message_id.as_deref());
    allowed.extend(decl.text.as_deref());
    allowed.extend(decl.text_list.as_deref());
    allowed.extend(decl.text_object.iter().map(|(_, a)| a.as_str()));
    if let Some(extra) = obj.keys().find(|k| !allowed.contains(&k.as_str())) {
        return Err(bad(format!(
            "unexpected argument {extra:?}: the destination, credentials and ids are host-pinned"
        )));
    }
    if let Some(missing) = decl.required.iter().find(|r| !obj.contains_key(r.as_str())) {
        return Err(bad(format!("argument {missing:?} is required")));
    }
    let mut wire = WireArgs::default();
    if let Some(name) = &decl.user {
        if let Some(v) = obj.get(name) {
            let u = v
                .as_str()
                .filter(|u| is_canonical_uuid(u))
                .ok_or_else(|| bad(format!("`{name}` must be a lowercase canonical UUID")))?;
            wire.user_uuid = Some(u.to_string());
        }
    }
    if let Some(name) = &decl.message_id {
        if let Some(v) = obj.get(name) {
            let m = v
                .as_str()
                .filter(|m| !m.is_empty() && m.len() <= 256 && !m.chars().any(char::is_control))
                .ok_or_else(|| bad(format!("`{name}` must be a short id string")))?;
            wire.message_id = Some(m.to_string());
        }
    }
    if let Some(name) = &decl.text {
        if let Some(v) = obj.get(name) {
            let t = v
                .as_str()
                .filter(|t| !t.is_empty() && t.len() <= MAX_TEXT_BYTES && !t.contains('\0'))
                .ok_or_else(|| bad(format!("`{name}` must be a non-empty string")))?;
            if matches!(class, OpClass::Role("role.add" | "role.remove")) && !is_role_key(t) {
                return Err(bad(format!(
                    "`{name}` must be a managed-role key ([a-z][a-z0-9_-]{{0,31}})"
                )));
            }
            wire.text = Some(t.to_string());
        }
    }
    if let Some(name) = &decl.text_list {
        if let Some(v) = obj.get(name) {
            let mut items: Vec<String> = v
                .as_array()
                .ok_or_else(|| bad(format!("`{name}` must be an array of strings")))?
                .iter()
                .map(|i| {
                    i.as_str()
                        .filter(|s| {
                            if matches!(class, OpClass::Role(_)) {
                                is_role_key(s)
                            } else {
                                !s.is_empty()
                                    && s.len() <= 64
                                    && !s.contains(',')
                                    && !s.chars().any(char::is_control)
                            }
                        })
                        .map(str::to_string)
                })
                .collect::<Option<_>>()
                .ok_or_else(|| bad(format!("`{name}` entries are malformed")))?;
            items.sort();
            items.dedup();
            if items.is_empty() || items.len() > MAX_TEXT_LIST {
                return Err(bad(format!(
                    "`{name}` must hold 1..={MAX_TEXT_LIST} distinct entries"
                )));
            }
            wire.text = Some(items.join(","));
        }
    }
    if !decl.text_object.is_empty() {
        let mut m = Map::new();
        for (key, arg) in &decl.text_object {
            if let Some(v) = obj.get(arg) {
                m.insert(key.clone(), v.clone());
            }
        }
        if !m.is_empty() {
            let s = serde_json::to_string(&Value::Object(m)).map_err(|_| bad("not encodable"))?;
            if s.len() > MAX_TEXT_BYTES {
                return Err(bad("arguments exceed the maximum size"));
            }
            wire.text = Some(s);
        }
    }
    Ok(wire)
}

/// The MAC-covered wire `origin_channel` for a guild-scoped op, decided by the ONE platform
/// field (`scope.origin_platform`): a Discord event with a channel keeps the channel pin; a
/// Discord event without one asserts its host-derived source guild or is refused; an
/// allowlisted external platform gets `guild:-` (binding guild only); every other origin is
/// refused.
fn guild_scope_origin(
    scope: &InvokeScope,
    platform: &str,
    what: &str,
) -> Result<String, ToolRefusal> {
    let origin_platform = scope.origin_platform.as_deref();
    if platform == "discord" && origin_platform == Some("discord") {
        if let Some(channel) = scope.origin_channel_id.as_deref() {
            return if is_discord_snowflake(channel) {
                Ok(channel.to_string())
            } else {
                Err(bad(format!(
                    "{platform} {what} origin channel id is not a valid snowflake"
                )))
            };
        }
        return match scope.origin_guild_id.as_deref() {
            Some(g) => Ok(format!("guild:{g}")),
            None => Err(bad(format!(
                "{platform} {what} from a discord event needs a valid source guild"
            ))),
        };
    }
    match origin_platform {
        Some(p) if EXTERNAL_GUILD_SCOPE_PLATFORMS.contains(&p) => Ok("guild:-".to_string()),
        _ => Err(bad(format!(
            "{platform} {what} cannot be triggered from this event origin"
        ))),
    }
}

/// The verified origin channel pin for a chat-scoped op (never caller-named).
fn channel_origin(scope: &InvokeScope, platform: &str, what: &str) -> Result<String, ToolRefusal> {
    let origin = scope.origin_channel_id.as_deref();
    if scope.origin_platform.as_deref() != Some(platform) || origin.is_none() {
        return Err(bad(format!(
            "{NO_BOUND_CHANNEL}: {platform} {what} requires an origin channel from a {platform} event"
        )));
    }
    let pinned = match platform {
        "discord" => origin
            .filter(|c| is_discord_snowflake(c))
            .map(str::to_string),
        "twitch" => pin_twitch_channel(Some("twitch"), origin, None).ok(),
        _ => origin
            .filter(|c| {
                !c.is_empty()
                    && c.len() <= 128
                    && c.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
            })
            .map(str::to_string),
    };
    pinned.ok_or_else(|| {
        bad(format!(
            "{NO_BOUND_CHANNEL}: {platform} {what} origin channel id is not valid"
        ))
    })
}

/// Maps a drain refusal code onto the tool framework's taxonomy (typed, never swallowed).
fn refusal_for_code(code: Option<&str>, platform: &str, op: &str) -> ToolRefusal {
    let c = code.unwrap_or("relay_failed");
    match c {
        "quota_exhausted" => ToolRefusal::TargetUnavailable(format!(
            "quota_exhausted: {platform} {op} refused: platform quota exhausted"
        )),
        "unsupported" | "unsupported_op" => {
            ToolRefusal::Forbidden(format!("{NOT_SUPPORTED}: {platform} {op} is not supported"))
        }
        "role_not_managed"
        | "role_privileged"
        | "role_not_found"
        | "user_not_in_community"
        | "not_in_community"
        | "bad_request" => ToolRefusal::InvalidArgs(format!("{platform} {op} refused: {c}")),
        "ambiguous_paired_guild" | "guild_mismatch" | "channel_not_owned" | "no_origin_pin" => {
            ToolRefusal::Forbidden(format!("{c}: {platform} {op} refused"))
        }
        _ => ToolRefusal::TargetUnavailable(format!("{platform} {op} refused: {c}")),
    }
}

fn unavailable(e: &HostResultError) -> ToolRefusal {
    ToolRefusal::TargetUnavailable(e.code.clone())
}

impl<Q: RelayQueue, K: KvBackend> StageCapabilities<Q, K> {
    /// Executes one authorized tool call as the producer op its manifest declaration names.
    /// The planner has already enforced the caller's `requested_tools`, the `tool.call` grant,
    /// the write key and the `required_scope` against the host-verified actor.
    ///
    /// # Errors
    /// A typed [`ToolRefusal`]; nothing is queued on any refusal that precedes the queue push.
    pub(crate) async fn run_host_tool(
        &self,
        scope: &InvokeScope,
        plan: &ToolPlan,
        decl: &HostOpDecl,
    ) -> Result<Value, ToolRefusal> {
        let class = classify(decl)?;
        if scope.tenant_id < 1 || scope.community_id < 1 {
            return Err(ToolRefusal::Forbidden(format!(
                "{} {} needs a community-scoped invoke",
                decl.platform, decl.op
            )));
        }
        // Host ops run ONLY from the registered platform service bundle: a manifest naming a
        // platform op on any other target is refused, whatever its declaration says.
        let service = format!("waddles.core.service.{}", decl.platform);
        if plan.target_app_id != service {
            return Err(ToolRefusal::Forbidden(format!(
                "{NOT_SUPPORTED}: host ops of platform {:?} are served only by {service}",
                decl.platform
            )));
        }
        // The service bundle itself must hold the op's catalog permission for EVERY class (the
        // caller holds only `tool.call`): defense in depth on top of the planner. Role grants
        // and scheduled events are moderation-class community actions.
        let permission = match class {
            OpClass::Registered(op) => {
                bundle_capability_gate::platform_ops::permission_for(&decl.platform, op)
            }
            OpClass::Role(_) | OpClass::Event => {
                Some(PermissionId::Moderation(decl.platform.clone()))
            }
        }
        .ok_or_else(|| {
            ToolRefusal::Forbidden(format!("host op {:?} has no catalog permission", decl.op))
        })?;
        self.authorize_service(scope, plan, permission)?;
        if class == OpClass::Event {
            return self.run_event_class(scope, plan).await;
        }
        let what = decl.op.as_str();
        // Phase 1 -- validation only (no I/O): an invalid call costs no readiness probe, rate
        // budget or detokenizer round trip.
        let wire = validate_args(decl, class, &plan.args)?;
        let origin = match decl.origin {
            HostOpOrigin::Channel => Some(channel_origin(scope, &decl.platform, what)?),
            HostOpOrigin::GuildScope => Some(guild_scope_origin(scope, &decl.platform, what)?),
            HostOpOrigin::None => None,
        };
        // Phase 2 -- producer-side safety checks, cheapest first.
        if decl.platform == "discord" {
            match self.relay_queue.key_exists(DISCORD_DRAIN_READY_KEY).await {
                Ok(true) => {}
                Ok(false) | Err(_) => {
                    tracing::error!(
                        platform = %decl.platform, op = what, app_id = %scope.app_id,
                        "discord outbound drain not ready (or unverifiable); nothing queued"
                    );
                    return Err(ToolRefusal::TargetUnavailable(format!(
                        "{} {what} refused: relay_unavailable",
                        decl.platform
                    )));
                }
            }
        }
        match class {
            OpClass::Registered(op) => self
                .throttle_platform_op(scope, &decl.platform, RelayOp::Registered(op))
                .await
                .map_err(|e| unavailable(&e))?,
            OpClass::Role(_) => self
                .throttle_role_op(scope)
                .await
                .map_err(|e| unavailable(&e))?,
            // Served before this pipeline; reaching here is a routing bug -- refused loudly.
            OpClass::Event => {
                return Err(ToolRefusal::Forbidden(
                    "event-class host ops are not served by the shared producer pipeline"
                        .to_string(),
                ))
            }
        }
        // Phase 3 -- detokenize the user HOST-side (fail closed), then build + queue.
        let user_wire = match (&wire.user_uuid, decl.user_wire) {
            (None, _) => None,
            (Some(u), HostOpUserWire::Placeholder) => {
                // The consumer re-resolves the placeholder (the raw id never rides the wire),
                // but a destructive op must still clear the protected-target check here: the
                // id is resolved ONLY for that check, host-side, and is never queued or logged.
                if is_destructive_moderation(what) {
                    let id = self.detokenize_user(scope, &decl.platform, u).await?;
                    self.refuse_protected_target(scope, &decl.platform, what, &id)
                        .await?;
                }
                Some(format!("{{user:{u}}}"))
            }
            (Some(u), HostOpUserWire::PlatformId) => {
                let id = self.detokenize_user(scope, &decl.platform, u).await?;
                if is_destructive_moderation(what) {
                    self.refuse_protected_target(scope, &decl.platform, what, &id)
                        .await?;
                }
                Some(id)
            }
        };
        let op_id = uuid::Uuid::new_v4().to_string();
        let ack_timeout = self.discord_ack_timeout;
        let exp_ms = now_epoch_ms()
            .saturating_add(u64::try_from(ack_timeout.as_millis()).unwrap_or(u64::MAX));
        let mut env = json!({
            "v": OUTBOUND_SCHEMA_VERSION,
            "op": what,
            "platform": decl.platform,
            "community_id": i64::from(scope.community_id),
            "tenant_id": i64::from(scope.tenant_id),
            "op_id": op_id,
            "exp_ms": exp_ms,
        });
        if let Some(obj) = env.as_object_mut() {
            if let Some(o) = &origin {
                if decl.origin == HostOpOrigin::Channel {
                    obj.insert("channel".to_string(), Value::String(o.clone()));
                }
                obj.insert("origin_channel".to_string(), Value::String(o.clone()));
            }
            for (k, v) in [
                ("user_id", &user_wire),
                ("text", &wire.text),
                ("message_id", &wire.message_id),
            ] {
                if let Some(v) = v {
                    obj.insert(k.to_string(), Value::String(v.clone()));
                }
            }
        }
        // Signed AFTER the handshake so the MAC binds `exp_ms`.
        self.sign_envelope(&mut env).map_err(|e| unavailable(&e))?;
        let payload = env.to_string();
        let outbound_bytes = payload.len() as u64;
        self.relay_queue
            .lpush(&super::outbound_relay_queue_key(&decl.platform), payload)
            .await
            .map_err(|_| {
                ToolRefusal::TargetUnavailable(format!(
                    "{} {what} refused: relay_unavailable",
                    decl.platform
                ))
            })?;
        let ack = self
            .await_host_ack(&op_id, ack_timeout, &decl.platform, what)
            .await?;
        self.usage
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .record_relay_call(
                &scope.tenant,
                scope.community.as_deref(),
                "",
                &scope.app_id,
                outbound_bytes,
            );
        tracing::info!(
            platform = %decl.platform, op = what, app_id = %scope.app_id,
            community_id = scope.community_id, "host platform tool confirmed"
        );
        shape_result(decl, what, &ack)
    }

    /// Enforces that the platform SERVICE bundle (the tool's target) holds `permission`.
    fn authorize_service(
        &self,
        scope: &InvokeScope,
        plan: &ToolPlan,
        permission: PermissionId,
    ) -> Result<(), ToolRefusal> {
        let service_scope = InvokeScope {
            app_id: plan.target_app_id.clone(),
            app_version: plan.target_version,
            ..scope.clone()
        };
        self.gate
            .authorize(
                &service_scope.gate_scope(),
                permission,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map(|_| ())
            .map_err(|d| {
                ToolRefusal::Forbidden(format!(
                    "{SCOPE_DENIED}: the platform bundle lacks a grant: {d}"
                ))
            })
    }

    /// Resolves the tokenized user UUID to the platform id, community-scoped, HOST-side.
    /// Any failure refuses the call (nothing queued); the raw id is never logged.
    async fn detokenize_user(
        &self,
        scope: &InvokeScope,
        platform: &str,
        uuid: &str,
    ) -> Result<String, ToolRefusal> {
        let Some(resolver) = &self.platform_users else {
            tracing::error!(
                app_id = %scope.app_id,
                "host tool names a user but no platform-user resolver is wired; refusing"
            );
            return Err(ToolRefusal::TargetUnavailable(
                "user detokenization is unavailable on this stage".to_string(),
            ));
        };
        match resolver
            .resolve(scope.tenant_id, scope.community_id, platform, uuid)
            .await
        {
            Ok(id) if is_wire_safe_id(&id) => Ok(id),
            Ok(_) | Err(ResolveError::Invalid) => Err(bad("user_unresolvable")),
            Err(ResolveError::NotFound) => Err(bad(format!(
                "{NO_LINKED_ACCOUNT}: the user has no linked account for this community on this platform"
            ))),
            Err(e @ (ResolveError::Denied | ResolveError::RateLimited | ResolveError::Unavailable)) => {
                tracing::warn!(
                    app_id = %scope.app_id, community_id = scope.community_id, error = %e,
                    "user detokenization failed; refusing (fail closed)"
                );
                Err(ToolRefusal::TargetUnavailable(format!("user_detokenization_{e}")))
            }
        }
    }

    /// Refuses a destructive moderation op whose target holds a protected community role
    /// (owner / admin / moderator), HOST-side and platform-independent: the platform API may
    /// well permit it, the host never does. The strict default has no mod-on-mod exemption (no
    /// community-config source is wired). Fails closed: with no directory, or on a directory
    /// failure, the call is refused as unavailable -- an unverifiable target is not attacked.
    async fn refuse_protected_target(
        &self,
        scope: &InvokeScope,
        platform: &str,
        op: &str,
        platform_user_id: &str,
    ) -> Result<(), ToolRefusal> {
        let Some(dir) = &self.actor_directory else {
            tracing::error!(
                app_id = %scope.app_id, op,
                "protected-target check needs the member directory but none is wired; refusing"
            );
            return Err(ToolRefusal::TargetUnavailable(
                "protected-target check is unavailable on this stage".to_string(),
            ));
        };
        match dir
            .member(
                scope.tenant_id,
                scope.community_id,
                platform,
                platform_user_id,
            )
            .await
        {
            Ok((_, role)) if PROTECTED_ROLES.contains(&role.as_str()) => {
                tracing::info!(
                    app_id = %scope.app_id, community_id = scope.community_id, platform, op,
                    "moderation refused: protected target"
                );
                Err(ToolRefusal::Forbidden(format!(
                    "{PROTECTED_TARGET}: the target is the owner, an admin or a moderator of this community"
                )))
            }
            Ok(_) | Err(ActorError::NotAMember | ActorError::NotLinked) => Ok(()),
            Err(e) => {
                tracing::warn!(
                    app_id = %scope.app_id, community_id = scope.community_id, error = %e,
                    "protected-target check failed; refusing (fail closed)"
                );
                Err(ToolRefusal::TargetUnavailable(
                    "protected-target check failed; retry".to_string(),
                ))
            }
        }
    }

    /// Scheduled-event tools ride the event-sync producer; the result carries the platform id.
    async fn run_event_class(
        &self,
        scope: &InvokeScope,
        plan: &ToolPlan,
    ) -> Result<Value, ToolRefusal> {
        self.run_discord_event_tool(scope, &plan.tool, &plan.args)
            .await
            .unwrap_or_else(|| {
                Err(ToolRefusal::Forbidden(format!(
                    "tool {:?} has no scheduled-event handler",
                    plan.tool
                )))
            })
    }

    /// Polls the drain's result key for `op_id` until `timeout`; a result that never arrives is
    /// `not_confirmed` (the op may or may not have run -- never reported as done).
    async fn await_host_ack(
        &self,
        op_id: &str,
        timeout: std::time::Duration,
        platform: &str,
        op: &str,
    ) -> Result<Value, ToolRefusal> {
        let key = format!("{DISCORD_OP_ACK_KEY_PREFIX}{op_id}");
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            match self.relay_queue.take(&key).await {
                Ok(Some(raw)) => {
                    let ack: Value = serde_json::from_str(&raw).map_err(|_| {
                        ToolRefusal::TargetUnavailable(format!(
                            "{platform} {op} result unreadable; not confirmed"
                        ))
                    })?;
                    return match ack.get("ok").and_then(Value::as_bool) {
                        Some(true) => Ok(ack),
                        Some(false) => Err(refusal_for_code(
                            ack.get("code").and_then(Value::as_str),
                            platform,
                            op,
                        )),
                        None => Err(ToolRefusal::TargetUnavailable(format!(
                            "{platform} {op} result unreadable; not confirmed"
                        ))),
                    };
                }
                Ok(None) => {}
                Err(_) => {
                    return Err(ToolRefusal::TargetUnavailable(
                        "relay_unavailable".to_string(),
                    ))
                }
            }
            if tokio::time::Instant::now() >= deadline {
                return Err(ToolRefusal::TargetUnavailable("not_confirmed".to_string()));
            }
            tokio::time::sleep(DISCORD_OP_ACK_POLL).await;
        }
    }
}

/// Builds the caller-visible result from the confirmed ack: a bare `{ok, op}` without
/// `returns`, else ONLY the allowlisted keys (looked up in the drain's `data` object, or the
/// top-level `id`). A missing allowlisted key is a refusal -- never a fabricated value.
fn shape_result(decl: &HostOpDecl, op: &str, ack: &Value) -> Result<Value, ToolRefusal> {
    if decl.returns.is_empty() {
        return Ok(json!({"ok": true, "op": op}));
    }
    let mut out = Map::new();
    out.insert("ok".to_string(), Value::Bool(true));
    for key in &decl.returns {
        let v = ack
            .get("data")
            .and_then(|d| d.get(key))
            .or_else(|| (key == "id").then(|| ack.get("id")).flatten())
            .ok_or_else(|| {
                ToolRefusal::TargetUnavailable(format!(
                    "{} {op} refused: bad_result",
                    decl.platform
                ))
            })?;
        out.insert(key.clone(), v.clone());
    }
    Ok(Value::Object(out))
}

#[cfg(test)]
#[path = "host_tools_tests.rs"]
mod host_tools_tests;
