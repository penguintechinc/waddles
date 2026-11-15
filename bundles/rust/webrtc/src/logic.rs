//! Host-independent WebRTC tool logic.
//!
//! Every tool is a pure function over one seam -- [`Media`], the `webrtc` host
//! capability -- so the whole tool table is unit-tested on the host target with a
//! fake; the wasm glue in `lib.rs` supplies the real implementation.
//!
//! Safety shape: each tool validates its args first (UUIDs, bounded numbers, closed
//! enums, unknown fields rejected) and fails LOUD with a typed error -- never a
//! default answer. The community is never an argument: the stage supplies it on the
//! host side. Results are rebuilt from a field whitelist, never forwarded raw. Join
//! tokens are bearer credentials: they are returned to the calling app (whose job is
//! to hand them to exactly one participant) and are never logged.

use std::sync::Arc;

use serde_json::{Map, Value, json};
use waddle_sdk::tool::{ToolContext, ToolError, ToolRegistry};

use crate::grants::{GrantRole, HandOp, LEAVE_TOOL, moderation_scope};

/// Every tool name this bundle exposes (also the only labels ever logged).
pub const TOOL_NAMES: [&str; 29] = [
    "room.create",
    "room.get",
    "room.close",
    "token.issue",
    "token.revoke",
    "whip.endpoint",
    "whep.endpoint",
    "track.route",
    "recording.start",
    "recording.stop",
    "token.revoke_all",
    "admin.verify",
    "room.list",
    "room.participants",
    "room.leave",
    "room.kick",
    "room.mute",
    "room.unmute",
    "room.mute_all",
    "room.lock",
    "room.unlock",
    "room.hand.raise",
    "room.hand.lower",
    "room.hand.lower_other",
    "room.hand.list",
    "room.hand.list_all",
    "room.hand.ack",
    "room.hand.reset",
    "participant.self",
];

/// Largest room label (display text, never a key).
pub const MAX_LABEL: usize = 128;
/// Largest publishers per room (host enforces the same cap).
pub const MAX_PUBLISHERS: u64 = 64;
/// Token TTL bounds in seconds (host enforces the same bounds).
pub const MIN_TTL: i64 = 30;
pub const MAX_TTL: i64 = 3600;

/// Every way a host-capability call can fail (mirrors the WIT `webrtc.error`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostError {
    Denied(String),
    NotFound,
    Invalid(String),
    QuotaExceeded,
    NotImplemented(String),
    Unavailable(String),
    Backend(String),
}

/// The `webrtc` host capability, as the tools use it.
pub trait Media: Send + Sync {
    /// Runs one control op (`room.create`, ...); `args` never carries a community.
    fn call(&self, op: &str, args: &Value) -> Result<Value, HostError>;
}

/// Maps a host failure onto a typed tool error. Messages are closed strings.
fn tool_err(e: HostError) -> ToolError {
    match e {
        HostError::Denied(_) => ToolError::NotGranted,
        HostError::NotFound => ToolError::InvalidArgs("room or source not found".into()),
        HostError::Invalid(m) => ToolError::InvalidArgs(m),
        HostError::QuotaExceeded => ToolError::PoolExhausted,
        HostError::NotImplemented(m) => ToolError::InvalidArgs(format!("unsupported: {m}")),
        HostError::Unavailable(_) | HostError::Backend(_) => ToolError::TargetUnavailable,
    }
}

fn bad(msg: &str) -> ToolError {
    ToolError::InvalidArgs(msg.to_string())
}

/// Parses `args` as an object whose keys are all in `allowed`.
fn object<'a>(args: &'a Value, allowed: &[&str]) -> Result<&'a Map<String, Value>, ToolError> {
    let o = args
        .as_object()
        .ok_or_else(|| bad("args must be an object"))?;
    if let Some(k) = o.keys().find(|k| !allowed.contains(&k.as_str())) {
        // Key names are schema vocabulary at worst; cap what is echoed.
        let k: String = k.chars().take(32).collect();
        return Err(ToolError::InvalidArgs(format!("unknown argument: {k}")));
    }
    Ok(o)
}

/// A canonical lowercase-hyphenated UUID (also what the host emits).
fn is_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit() && !c.is_ascii_uppercase(),
        })
}

fn uuid_arg(
    o: &Map<String, Value>,
    key: &str,
    required: bool,
) -> Result<Option<String>, ToolError> {
    match o.get(key) {
        None | Some(Value::Null) if !required => Ok(None),
        Some(Value::String(s)) if is_uuid(s) => Ok(Some(s.clone())),
        _ => Err(ToolError::InvalidArgs(format!(
            "{key} must be a lowercase UUID"
        ))),
    }
}

fn bool_arg(o: &Map<String, Value>, key: &str) -> Result<bool, ToolError> {
    match o.get(key) {
        Some(Value::Bool(b)) => Ok(*b),
        _ => Err(ToolError::InvalidArgs(format!("{key} must be a boolean"))),
    }
}

fn ttl_arg(o: &Map<String, Value>) -> Result<Option<i64>, ToolError> {
    match o.get("ttl_secs") {
        None | Some(Value::Null) => Ok(None),
        Some(v) => match v.as_i64() {
            Some(t) if (MIN_TTL..=MAX_TTL).contains(&t) => Ok(Some(t)),
            _ => Err(ToolError::InvalidArgs(format!(
                "ttl_secs must be {MIN_TTL}..={MAX_TTL}"
            ))),
        },
    }
}

/// Copies exactly the whitelisted `keys` out of the host `v` (absent keys stay
/// absent); a host result that is not an object is a backend defect.
fn pick(v: &Value, keys: &[&str]) -> Result<Value, ToolError> {
    let src = v.as_object().ok_or(ToolError::TargetUnavailable)?;
    let mut out = Map::new();
    for k in keys {
        if let Some(x) = src.get(*k) {
            out.insert((*k).to_string(), x.clone());
        }
    }
    Ok(Value::Object(out))
}

const ROOM_FIELDS: [&str; 8] = [
    "room_id",
    "label",
    "route",
    "max_publishers",
    "created_at",
    "publisher_ids",
    "subscriber_count",
    "community_id",
];
const TOKEN_FIELDS: [&str; 4] = ["token", "token_id", "participant_id", "expires_at"];
const ENDPOINT_FIELDS: [&str; 5] = ["url", "token", "token_id", "participant_id", "expires_at"];

/// The room view returned to callers: everything but the (host-side) community.
fn room_view(v: &Value) -> Result<Value, ToolError> {
    let mut r = pick(v, &ROOM_FIELDS[..7])?;
    if r.get("room_id").is_none() {
        return Err(ToolError::TargetUnavailable);
    }
    if let Some(o) = r.as_object_mut() {
        o.entry("publisher_ids").or_insert(json!([]));
    }
    Ok(r)
}

fn room_create(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(
        &args,
        &[
            "label",
            "max_publishers",
            "route",
            "kind",
            "closed",
            "allow_guests",
            "starts_at",
            "ends_at",
        ],
    )?;
    let mut req = Map::new();
    match o.get("label") {
        None | Some(Value::Null) => {}
        Some(Value::String(s)) if s.len() <= MAX_LABEL => {
            req.insert("label".into(), json!(s));
        }
        _ => {
            return Err(ToolError::InvalidArgs(format!(
                "label must be a string <= {MAX_LABEL} bytes"
            )));
        }
    }
    match o.get("max_publishers") {
        None | Some(Value::Null) => {}
        Some(v) => match v.as_u64() {
            Some(n) if (1..=MAX_PUBLISHERS).contains(&n) => {
                req.insert("max_publishers".into(), json!(n));
            }
            _ => {
                return Err(ToolError::InvalidArgs(format!(
                    "max_publishers must be 1..={MAX_PUBLISHERS}"
                )));
            }
        },
    }
    match o.get("route") {
        None | Some(Value::Null) => {}
        Some(Value::String(s)) if s == "sfu" => {
            req.insert("route".into(), json!("sfu"));
        }
        _ => return Err(bad("route must be \"sfu\"")),
    }
    // Room facts svc-streaming enforces on join. A closed set: an unknown kind is refused (a
    // town hall is created by hub-api only, never through this tool).
    match o.get("kind") {
        None | Some(Value::Null) => {}
        Some(Value::String(k)) if ["community", "stage", "green_room"].contains(&k.as_str()) => {
            req.insert("kind".into(), json!(k));
        }
        _ => return Err(bad("kind must be community, stage or green_room")),
    }
    for flag in ["closed", "allow_guests"] {
        match o.get(flag) {
            None | Some(Value::Null) => {}
            Some(Value::Bool(b)) => {
                req.insert(flag.into(), json!(b));
            }
            _ => return Err(ToolError::InvalidArgs(format!("{flag} must be a boolean"))),
        }
    }
    for bound in ["starts_at", "ends_at"] {
        match o.get(bound) {
            None | Some(Value::Null) => {}
            Some(v) => match v.as_i64() {
                Some(n) if n >= 0 => {
                    req.insert(bound.into(), json!(n));
                }
                _ => {
                    return Err(ToolError::InvalidArgs(format!(
                        "{bound} must be a non-negative unix time"
                    )));
                }
            },
        }
    }
    room_view(
        &m.call("room.create", &Value::Object(req))
            .map_err(tool_err)?,
    )
}

fn room_only(
    op: &'static str,
) -> impl Fn(&dyn Media, &ToolContext, Value) -> Result<Value, ToolError> {
    move |m, _, args| {
        let o = object(&args, &["room_id"])?;
        let room = uuid_arg(o, "room_id", true)?;
        room_view(&m.call(op, &json!({"room_id": room})).map_err(tool_err)?)
    }
}

fn token_issue(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(
        &args,
        &[
            "room_id",
            "participant_id",
            "publish",
            "subscribe",
            "source",
            "ttl_secs",
        ],
    )?;
    let publish = bool_arg(o, "publish")?;
    let subscribe = bool_arg(o, "subscribe")?;
    if !publish && !subscribe {
        return Err(bad("token must grant publish and/or subscribe"));
    }
    let source = uuid_arg(o, "source", false)?;
    if source.is_some() && publish {
        return Err(bad("source restricts a subscribe-only token"));
    }
    let req = json!({
        "room_id": uuid_arg(o, "room_id", true)?,
        "participant_id": uuid_arg(o, "participant_id", false)?,
        "publish": publish, "subscribe": subscribe, "source": source, "ttl_secs": ttl_arg(o)?,
    });
    let mut req = req;
    strip_nulls(&mut req);
    pick(
        &m.call("token.issue", &req).map_err(tool_err)?,
        &TOKEN_FIELDS,
    )
}

/// Bumps the room's token epoch host-side, invalidating every older token.
fn token_revoke_all(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(&args, &["room_id"])?;
    let req = json!({"room_id": uuid_arg(o, "room_id", true)?});
    pick(
        &m.call("token.revoke_all", &req).map_err(tool_err)?,
        &["room_id", "epoch"],
    )
}

/// Authorization proof for apps: succeeds only when the HOST has verified the
/// actor holds `community:admin` (the manifest's `required_scope` is enforced
/// before dispatch, so reaching this handler IS the proof). Takes no arguments
/// and never reads actor/scope data from the caller.
/// `participant.self`: the caller's host-derived per-community call-participant id (the same
/// pseudonym hub-api's REST layer uses), so a front-end app can compare it with the
/// participant ids svc-streaming reports without ever holding the global user uuid.
fn participant_self(_: &dyn Media, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    object(&args, &[])?;
    let id = ctx
        .participant
        .as_deref()
        .map(str::to_ascii_lowercase)
        .filter(|p| is_uuid(p))
        .ok_or(ToolError::Forbidden)?;
    if !ctx.has_scope("community:member") {
        return Err(ToolError::NotGranted);
    }
    Ok(json!({"participant": id}))
}

fn admin_verify(_: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    object(&args, &[])?;
    Ok(json!({"verified": true}))
}

fn token_revoke(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(&args, &["room_id", "token_id"])?;
    let req = json!({
        "room_id": uuid_arg(o, "room_id", true)?,
        "token_id": uuid_arg(o, "token_id", true)?,
    });
    pick(
        &m.call("token.revoke", &req).map_err(tool_err)?,
        &["revoked", "token_id"],
    )
}

fn endpoint(
    op: &'static str,
) -> impl Fn(&dyn Media, &ToolContext, Value) -> Result<Value, ToolError> {
    move |m, _, args| {
        let whep = op == "whep.endpoint";
        let allowed: &[&str] = if whep {
            &["room_id", "participant_id", "source", "ttl_secs"]
        } else {
            &["room_id", "participant_id", "ttl_secs"]
        };
        let o = object(&args, allowed)?;
        let mut req = json!({
            "room_id": uuid_arg(o, "room_id", true)?,
            "participant_id": uuid_arg(o, "participant_id", false)?,
            "source": if whep { uuid_arg(o, "source", false)? } else { None },
            "ttl_secs": ttl_arg(o)?,
        });
        strip_nulls(&mut req);
        pick(&m.call(op, &req).map_err(tool_err)?, &ENDPOINT_FIELDS)
    }
}

fn track_route(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(&args, &["room_id", "mode"])?;
    let mode = match o.get("mode") {
        Some(Value::String(s)) if s == "sfu" || s == "relay" => s.clone(),
        _ => return Err(bad("mode must be \"sfu\" or \"relay\"")),
    };
    let req = json!({"room_id": uuid_arg(o, "room_id", true)?, "mode": mode});
    pick(
        &m.call("track.route", &req).map_err(tool_err)?,
        &["room_id", "mode"],
    )
}

fn recording(
    op: &'static str,
) -> impl Fn(&dyn Media, &ToolContext, Value) -> Result<Value, ToolError> {
    move |m, _, args| {
        let o = object(&args, &["room_id"])?;
        let req = json!({"room_id": uuid_arg(o, "room_id", true)?});
        pick(
            &m.call(op, &req).map_err(tool_err)?,
            &["room_id", "recording_id", "object_key", "state"],
        )
    }
}

/// Read-only `room.list`: live rooms of the invoking community, whitelisted.
fn rooms_list(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    object(&args, &[])?;
    let out = m.call("room.list", &json!({})).map_err(tool_err)?;
    let rooms = out
        .get("rooms")
        .and_then(Value::as_array)
        .ok_or(ToolError::TargetUnavailable)?;
    let mut view = Vec::with_capacity(rooms.len());
    for r in rooms {
        let mut v = pick(
            r,
            &[
                "room_id",
                "label",
                "created_at",
                "publisher_ids",
                "subscriber_count",
                "locked",
            ],
        )?;
        if v.get("room_id").is_none() {
            return Err(ToolError::TargetUnavailable);
        }
        if let Some(o) = v.as_object_mut() {
            o.entry("publisher_ids").or_insert(json!([]));
        }
        view.push(v);
    }
    Ok(json!({ "rooms": view }))
}

/// Read-only `room.participants`: participant UUIDs and media flags only.
fn participants(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(&args, &["room_id"])?;
    let req = json!({"room_id": uuid_arg(o, "room_id", true)?});
    let out = m.call("room.participants", &req).map_err(tool_err)?;
    let list = out
        .get("participants")
        .and_then(Value::as_array)
        .ok_or(ToolError::TargetUnavailable)?;
    let mut view = Vec::with_capacity(list.len());
    for p in list {
        let v = pick(p, &["participant_id", "publisher", "subscriber", "muted"])?;
        if v.get("participant_id").is_none() {
            return Err(ToolError::TargetUnavailable);
        }
        view.push(v);
    }
    let mut head = pick(&out, &["room_id", "locked", "count"])?;
    head["participants"] = Value::Array(view);
    Ok(head)
}

/// Refuses `actor_id` / `actor_role` in the arguments of any actor-bound tool: the
/// actor is the host-verified context, so nothing the caller sends can name or
/// upgrade it.
fn reject_actor_args(args: &Value) -> Result<(), ToolError> {
    if let Some(o) = args.as_object()
        && (o.contains_key("actor_id") || o.contains_key("actor_role"))
    {
        return Err(bad(
            "actor is derived by the host and is not accepted as an argument",
        ));
    }
    Ok(())
}

/// Runs one moderation tool (`leave|kick|mute|unmute|mute_all|lock|unlock`).
///
/// The actor is the host-verified context and its role comes from its host-derived
/// scopes; `actor_id`/`actor_role` arguments are refused. Which scope is required
/// follows from the tool (see [`moderation_scope`]): `room.leave` removes only the
/// verified caller (member scope), everything else needs `community:moderate`,
/// verified by the host before dispatch and again here on the verified context.
/// `actor_id` and the scope-derived `actor_role` ride on every host op so
/// svc-streaming can bind and re-check them.
fn moderation_call(
    tool: &'static str,
    with_participant: bool,
    fields: &'static [&'static str],
    m: &dyn Media,
    ctx: &ToolContext,
    args: Value,
) -> Result<Value, ToolError> {
    reject_actor_args(&args)?;
    let allowed: &[&str] = if with_participant {
        &["room_id", "participant_id"]
    } else {
        &["room_id"]
    };
    let o = object(&args, allowed)?;
    let room = uuid_arg(o, "room_id", true)?;
    let target = if with_participant {
        Some(uuid_arg(o, "participant_id", true)?.ok_or_else(|| bad("participant_id required"))?)
    } else {
        None
    };
    let scope = moderation_scope(tool).ok_or(ToolError::Forbidden)?;
    let actor = verified_actor_scope(ctx, scope)?;
    let mut req = json!({
        "room_id": room, "actor_id": actor.id, "actor_role": role_wire(actor.role),
    });
    // `room.leave` acts on the verified caller only; the others name their target.
    match (&target, tool == LEAVE_TOOL) {
        (Some(t), _) => req["participant_id"] = json!(t),
        (None, true) => req["participant_id"] = json!(actor.id),
        (None, false) => {}
    }
    pick(&m.call(tool, &req).map_err(tool_err)?, fields)
}

/// The only contract version the hand tools (and the host ops behind them) speak.
const API_VERSION: &str = "v1";

/// Accepts an absent `api_version` or exactly `"v1"`; anything else fails loud
/// rather than being forwarded to a host that may route it differently.
fn api_version_arg(o: &Map<String, Value>) -> Result<(), ToolError> {
    match o.get("api_version") {
        None | Some(Value::Null) => Ok(()),
        Some(Value::String(v)) if v == API_VERSION => Ok(()),
        _ => Err(bad("api_version must be \"v1\"")),
    }
}

/// The acting participant of a hand call: the HOST-VERIFIED actor of the tool
/// context and the role its host-derived scopes imply -- never anything the caller
/// put in the arguments (receiver-gated: the receiver decides who is asking).
struct Actor {
    id: String,
    role: GrantRole,
}

/// Resolves the verified actor for `op`. No verified actor is `forbidden`; an actor
/// whose host-derived scopes do not include the scope `op` declares is refused here
/// too (the host already refused it before dispatch, this is defence in depth on
/// verified data).
fn verified_actor(ctx: &ToolContext, op: HandOp) -> Result<Actor, ToolError> {
    verified_actor_scope(ctx, op.scope())
}

/// [`verified_actor`] for any tool, given the scope it declares in its manifest.
fn verified_actor_scope(ctx: &ToolContext, scope: &str) -> Result<Actor, ToolError> {
    // The id sent to svc-streaming is the HOST-derived per-community pseudonym, never
    // the global `actor` user uuid; no pseudonym => forbidden (fail closed, no fallback).
    let id = ctx
        .participant
        .as_deref()
        .map(str::to_ascii_lowercase)
        .filter(|a| is_uuid(a))
        .ok_or(ToolError::Forbidden)?;
    if !ctx.has_scope(scope) {
        return Err(ToolError::NotGranted);
    }
    let role = if ctx.has_scope("community:admin") {
        GrantRole::Admin
    } else if ctx.has_scope("community:moderate") {
        GrantRole::Moderator
    } else {
        GrantRole::Member
    };
    Ok(Actor { id, role })
}

/// The closed wire form of a role (what the host's `actor_role` expects).
fn role_wire(r: GrantRole) -> &'static str {
    match r {
        GrantRole::Guest => "guest",
        GrantRole::Member => "member",
        GrantRole::Moderator => "moderator",
        GrantRole::Admin => "admin",
    }
}

/// The host request of one hand op. `actor_id` and `actor_role` ride on EVERY op
/// (derived from the verified context) so svc-streaming can bind the actor to the
/// authenticated call and re-check it itself instead of trusting this bundle.
fn host_req(room: &Option<String>, actor: &Actor, participant: Option<&str>) -> Value {
    let mut req = json!({
        "api_version": API_VERSION, "room_id": room,
        "actor_id": actor.id, "actor_role": role_wire(actor.role),
    });
    if let Some(p) = participant {
        req["participant_id"] = json!(p);
    }
    req
}

/// One queue entry rebuilt from a field whitelist; a host entry without a
/// participant id is a backend defect.
fn hand_view(v: &Value) -> Result<Value, ToolError> {
    let h = pick(
        v,
        &[
            "participant_id",
            "raised_at",
            "acknowledged_at",
            "acknowledged_by",
        ],
    )?;
    if h.get("participant_id").is_none() {
        return Err(ToolError::TargetUnavailable);
    }
    Ok(h)
}

/// Runs one raised-hand tool. Arguments are only `room_id` (+ `participant_id` for
/// `lower_other`/`ack`) and an optional `api_version`; `actor_id`/`actor_role` are
/// refused outright -- the actor is the host-verified context, and which tool was
/// called (hence which scope the host verified) decides what is allowed.
fn hand_call(
    op: HandOp,
    m: &dyn Media,
    ctx: &ToolContext,
    args: Value,
) -> Result<Value, ToolError> {
    reject_actor_args(&args)?;
    let allowed: &[&str] = match op {
        HandOp::LowerOther | HandOp::Ack => &["api_version", "room_id", "participant_id"],
        _ => &["api_version", "room_id"],
    };
    let o = object(&args, allowed)?;
    api_version_arg(o)?;
    let room = uuid_arg(o, "room_id", true)?;
    let target = match op {
        HandOp::LowerOther | HandOp::Ack => Some(
            uuid_arg(o, "participant_id", true)?.ok_or_else(|| bad("participant_id required"))?,
        ),
        _ => None,
    };
    let actor = verified_actor(ctx, op)?;
    let host = op.host_op();
    match op {
        HandOp::Raise => {
            let out = m
                .call(host, &host_req(&room, &actor, Some(&actor.id)))
                .map_err(tool_err)?;
            let hand = hand_view(out.get("hand").ok_or(ToolError::TargetUnavailable)?)?;
            Ok(json!({"room_id": room, "hand": hand}))
        }
        HandOp::Lower | HandOp::LowerOther => {
            let who = target.unwrap_or_else(|| actor.id.clone());
            let out = m
                .call(host, &host_req(&room, &actor, Some(&who)))
                .map_err(tool_err)?;
            let lowered = out
                .get("lowered")
                .and_then(Value::as_bool)
                .ok_or(ToolError::TargetUnavailable)?;
            Ok(json!({"room_id": room, "participant_id": who, "lowered": lowered}))
        }
        HandOp::List | HandOp::ListAll => {
            let out = m
                .call(host, &host_req(&room, &actor, None))
                .map_err(tool_err)?;
            let queue = out
                .get("hands")
                .and_then(Value::as_array)
                .ok_or(ToolError::TargetUnavailable)?;
            let mut hands = Vec::with_capacity(queue.len());
            for (i, h) in queue.iter().enumerate() {
                let mut v = hand_view(h)?;
                let mine =
                    v.get("participant_id").and_then(Value::as_str) == Some(actor.id.as_str());
                if op == HandOp::ListAll || mine {
                    v["position"] = json!(i + 1);
                    hands.push(v);
                }
            }
            Ok(json!({"room_id": room, "count": queue.len(), "hands": hands}))
        }
        HandOp::Ack => {
            let out = m
                .call(host, &host_req(&room, &actor, target.as_deref()))
                .map_err(tool_err)?;
            let hand = hand_view(out.get("hand").ok_or(ToolError::TargetUnavailable)?)?;
            Ok(json!({"room_id": room, "hand": hand}))
        }
        HandOp::Reset => {
            let out = m
                .call(host, &host_req(&room, &actor, None))
                .map_err(tool_err)?;
            let cleared = out
                .get("cleared")
                .and_then(Value::as_u64)
                .ok_or(ToolError::TargetUnavailable)?;
            Ok(json!({"room_id": room, "cleared": cleared}))
        }
    }
}

fn strip_nulls(v: &mut Value) {
    if let Some(o) = v.as_object_mut() {
        o.retain(|_, x| !x.is_null());
    }
}

/// The known tool label to log for `name`, otherwise the constant
/// `unknown_tool` -- a caller can put anything in a tool name and bundle logs
/// must stay PII-free.
pub fn tool_log_label(name: &str) -> &'static str {
    TOOL_NAMES
        .iter()
        .copied()
        .find(|known| *known == name)
        .unwrap_or("unknown_tool")
}

/// A tool handler over the media seam.
type Handler =
    Box<dyn Fn(&dyn Media, &ToolContext, Value) -> Result<Value, ToolError> + Send + Sync>;

/// Builds the tool table: each tool name routes to its handler over `media`.
pub fn build_registry(media: Arc<dyn Media>) -> ToolRegistry {
    let mut r = ToolRegistry::new();
    let reg = |r: &mut ToolRegistry, name: &str, f: Handler| {
        let m = Arc::clone(&media);
        r.register(name, move |ctx, args| f(m.as_ref(), ctx, args));
    };
    reg(&mut r, "room.create", Box::new(room_create));
    reg(&mut r, "room.get", Box::new(room_only("room.get")));
    reg(&mut r, "room.close", Box::new(room_only("room.close")));
    reg(&mut r, "token.issue", Box::new(token_issue));
    reg(&mut r, "token.revoke", Box::new(token_revoke));
    reg(&mut r, "whip.endpoint", Box::new(endpoint("whip.endpoint")));
    reg(&mut r, "whep.endpoint", Box::new(endpoint("whep.endpoint")));
    reg(&mut r, "track.route", Box::new(track_route));
    reg(
        &mut r,
        "recording.start",
        Box::new(recording("recording.start")),
    );
    reg(
        &mut r,
        "recording.stop",
        Box::new(recording("recording.stop")),
    );
    reg(&mut r, "token.revoke_all", Box::new(token_revoke_all));
    reg(&mut r, "admin.verify", Box::new(admin_verify));
    reg(&mut r, "participant.self", Box::new(participant_self));
    reg(&mut r, "room.list", Box::new(rooms_list));
    reg(&mut r, "room.participants", Box::new(participants));
    let gone: &'static [&'static str] = &["room_id", "participant_id", "sessions_closed"];
    let muted: &'static [&'static str] = &["room_id", "participant_id", "muted"];
    let moderation: [(&'static str, bool, &'static [&'static str]); 7] = [
        ("room.leave", false, gone),
        ("room.kick", true, gone),
        ("room.mute", true, muted),
        ("room.unmute", true, muted),
        ("room.mute_all", false, &["room_id", "muted"]),
        ("room.lock", false, &["room_id", "locked"]),
        ("room.unlock", false, &["room_id", "locked"]),
    ];
    for (tool, with_participant, fields) in moderation {
        reg(
            &mut r,
            tool,
            Box::new(move |m, ctx, args| {
                moderation_call(tool, with_participant, fields, m, ctx, args)
            }),
        );
    }
    for op in HandOp::ALL {
        reg(
            &mut r,
            op.tool(),
            Box::new(move |m, ctx, args| hand_call(op, m, ctx, args)),
        );
    }
    r
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    const ROOM: &str = "11111111-1111-1111-1111-111111111111";
    const USER: &str = "22222222-2222-2222-2222-222222222222";

    /// Records every host call and answers with a canned result or error.
    struct Fake {
        calls: Mutex<Vec<(String, Value)>>,
        answer: Mutex<Result<Value, HostError>>,
    }
    impl Fake {
        fn ok(v: Value) -> Arc<Self> {
            Arc::new(Self {
                calls: Mutex::new(vec![]),
                answer: Mutex::new(Ok(v)),
            })
        }
        fn err(e: HostError) -> Arc<Self> {
            Arc::new(Self {
                calls: Mutex::new(vec![]),
                answer: Mutex::new(Err(e)),
            })
        }
        fn calls(&self) -> Vec<(String, Value)> {
            self.calls.lock().unwrap().clone()
        }
    }
    impl Media for Fake {
        fn call(&self, op: &str, args: &Value) -> Result<Value, HostError> {
            self.calls
                .lock()
                .unwrap()
                .push((op.to_string(), args.clone()));
            self.answer.lock().unwrap().clone()
        }
    }

    fn call(f: &Arc<Fake>, tool: &str, args: Value) -> Result<Value, ToolError> {
        let reg = build_registry(f.clone());
        reg.call(tool, args.to_string().as_bytes())
            .map(|b| serde_json::from_slice(&b).unwrap())
    }

    fn room_json() -> Value {
        json!({"room_id": ROOM, "community_id": "7", "label": "x", "route": "sfu",
               "max_publishers": 16, "created_at": 1, "publisher_ids": [], "subscriber_count": 0,
               "internal": "leak"})
    }

    #[test]
    fn every_manifest_tool_is_registered() {
        let f = Fake::ok(room_json());
        let reg = build_registry(f);
        for t in TOOL_NAMES {
            // A registered tool fails on bad args (invalid-args "args must be an object"
            // style), an unregistered one fails with "unknown tool".
            let e = reg.call(t, b"{\"zz\":1}").unwrap_err();
            assert!(
                !matches!(&e, ToolError::InvalidArgs(m) if m.starts_with("unknown tool")),
                "{t}"
            );
        }
        assert!(
            matches!(reg_unknown(), ToolError::InvalidArgs(m) if m.starts_with("unknown tool"))
        );
    }
    fn reg_unknown() -> ToolError {
        build_registry(Fake::ok(json!({})))
            .call("nope", b"{}")
            .unwrap_err()
    }

    #[test]
    fn room_create_forwards_validated_args_and_whitelists_the_result() {
        let f = Fake::ok(room_json());
        let out = call(
            &f,
            "room.create",
            json!({"label": "x", "max_publishers": 8, "route": "sfu"}),
        )
        .unwrap();
        assert_eq!(f.calls()[0].0, "room.create");
        assert_eq!(
            f.calls()[0].1,
            json!({"label": "x", "max_publishers": 8, "route": "sfu"})
        );
        assert!(out.get("internal").is_none(), "host extras never leak");
        assert!(
            out.get("community_id").is_none(),
            "community stays host-side"
        );
        assert_eq!(out["room_id"], ROOM);
    }

    #[test]
    fn room_create_forwards_room_facts_and_refuses_bad_ones() {
        let f = Fake::ok(room_json());
        let facts = json!({"kind": "stage", "closed": false, "allow_guests": true,
                           "starts_at": 10, "ends_at": 20});
        call(&f, "room.create", facts.clone()).unwrap();
        assert_eq!(f.calls()[0].1, facts, "facts reach svc-streaming unchanged");
        for bad in [
            json!({"kind": "town_hall"}),
            json!({"kind": "nope"}),
            json!({"closed": "yes"}),
            json!({"allow_guests": 1}),
            json!({"starts_at": -1}),
            json!({"ends_at": "x"}),
        ] {
            let g = Fake::ok(room_json());
            assert!(
                matches!(call(&g, "room.create", bad), Err(ToolError::InvalidArgs(_))),
            );
            assert!(g.calls().is_empty());
        }
    }

    #[test]
    fn invalid_args_never_reach_the_host() {
        let f = Fake::ok(room_json());
        for (tool, args) in [
            ("room.create", json!({"max_publishers": 0})),
            ("room.create", json!({"max_publishers": 65})),
            ("room.create", json!({"route": "relay"})),
            ("room.create", json!({"community_id": "8"})),
            ("room.create", json!({"label": "x".repeat(129)})),
            ("room.get", json!({"room_id": "not-a-uuid"})),
            (
                "room.get",
                json!({"room_id": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"}),
            ),
            ("room.close", json!({})),
            (
                "token.issue",
                json!({"room_id": ROOM, "publish": false, "subscribe": false}),
            ),
            (
                "token.issue",
                json!({"room_id": ROOM, "publish": true, "subscribe": false, "source": USER}),
            ),
            (
                "token.issue",
                json!({"room_id": ROOM, "publish": true, "subscribe": true, "ttl_secs": 5}),
            ),
            (
                "token.issue",
                json!({"room_id": ROOM, "publish": true, "subscribe": true, "ttl_secs": 99999}),
            ),
            (
                "token.issue",
                json!({"room_id": ROOM, "publish": "yes", "subscribe": true}),
            ),
            ("token.revoke", json!({"room_id": ROOM})),
            ("whip.endpoint", json!({"room_id": ROOM, "source": USER})),
            ("whep.endpoint", json!({"room_id": "x"})),
            ("track.route", json!({"room_id": ROOM, "mode": "mesh"})),
            ("recording.start", json!({"room_id": ROOM, "extra": 1})),
        ] {
            assert!(
                matches!(call(&f, tool, args.clone()), Err(ToolError::InvalidArgs(_))),
                "{tool} {args}"
            );
        }
        assert!(f.calls().is_empty(), "no invalid call may reach the host");
    }

    #[test]
    fn token_and_endpoint_tools_return_only_whitelisted_fields() {
        let host = json!({"token": "T", "token_id": USER, "participant_id": USER, "expires_at": 9,
                          "url": "https://x/whip", "secret": "no"});
        let f = Fake::ok(host);
        let t = call(
            &f,
            "token.issue",
            json!({"room_id": ROOM, "publish": true, "subscribe": false}),
        )
        .unwrap();
        assert_eq!(t["token"], "T");
        assert!(t.get("secret").is_none() && t.get("url").is_none());
        // null optionals are stripped before the host call
        assert_eq!(
            f.calls()[0].1,
            json!({"room_id": ROOM, "publish": true, "subscribe": false})
        );
        let e = call(
            &f,
            "whep.endpoint",
            json!({"room_id": ROOM, "source": USER, "ttl_secs": 60}),
        )
        .unwrap();
        assert_eq!(e["url"], "https://x/whip");
        assert!(e.get("secret").is_none());
        assert_eq!(
            f.calls()[1],
            (
                "whep.endpoint".to_string(),
                json!({"room_id": ROOM, "source": USER, "ttl_secs": 60})
            )
        );
    }

    #[test]
    fn host_failures_map_to_typed_errors_never_success() {
        for (host, want) in [
            (HostError::Denied("not_granted".into()), "not_granted"),
            (HostError::QuotaExceeded, "pool_exhausted"),
            (HostError::Unavailable("x".into()), "target_unavailable"),
            (HostError::Backend("x".into()), "target_unavailable"),
        ] {
            let f = Fake::err(host);
            let e = call(&f, "room.get", json!({"room_id": ROOM})).unwrap_err();
            let got = match e {
                ToolError::NotGranted => "not_granted",
                ToolError::PoolExhausted => "pool_exhausted",
                ToolError::TargetUnavailable => "target_unavailable",
                _ => "other",
            };
            assert_eq!(got, want);
        }
        let f = Fake::err(HostError::NotImplemented("relay".into()));
        let e = call(&f, "track.route", json!({"room_id": ROOM, "mode": "relay"})).unwrap_err();
        assert!(matches!(e, ToolError::InvalidArgs(m) if m.contains("unsupported")));
        let f = Fake::err(HostError::NotFound);
        assert!(matches!(
            call(&f, "room.close", json!({"room_id": ROOM})),
            Err(ToolError::InvalidArgs(_))
        ));
    }

    #[test]
    fn malformed_host_result_fails_loud() {
        let f = Fake::ok(json!("not an object"));
        assert!(matches!(
            call(&f, "room.get", json!({"room_id": ROOM})),
            Err(ToolError::TargetUnavailable)
        ));
        let f = Fake::ok(json!({"no_room_id": 1}));
        assert!(matches!(
            call(&f, "room.get", json!({"room_id": ROOM})),
            Err(ToolError::TargetUnavailable)
        ));
    }

    #[test]
    fn log_label_never_echoes_unknown_names() {
        assert_eq!(tool_log_label("room.create"), "room.create");
        assert_eq!(tool_log_label("someone@example.com"), "unknown_tool");
    }

    #[test]
    fn admin_verify_makes_no_host_call_and_rejects_arguments() {
        let f = Fake::ok(json!({}));
        assert_eq!(
            call(&f, "admin.verify", json!({})).unwrap(),
            json!({"verified": true})
        );
        assert!(matches!(
            call(&f, "admin.verify", json!({"actor": USER})),
            Err(ToolError::InvalidArgs(_))
        ));
        assert!(
            f.calls().is_empty(),
            "scope is enforced host-side; nothing to ask the host"
        );
    }

    #[test]
    fn revoke_all_validates_and_whitelists() {
        let f = Fake::ok(json!({"room_id": ROOM, "epoch": 3, "leak": 1}));
        let out = call(&f, "token.revoke_all", json!({"room_id": ROOM})).unwrap();
        assert_eq!(out, json!({"room_id": ROOM, "epoch": 3}));
        assert!(matches!(
            call(&f, "token.revoke_all", json!({"room_id": "x"})),
            Err(ToolError::InvalidArgs(_))
        ));
    }

    const MOD: &str = "33333333-3333-3333-3333-333333333333";

    /// The moderation tools and whether they take a `participant_id`.
    const MOD_TOOLS: [(&str, bool); 7] = [
        ("room.leave", false),
        ("room.kick", true),
        ("room.mute", true),
        ("room.unmute", true),
        ("room.mute_all", false),
        ("room.lock", false),
        ("room.unlock", false),
    ];

    /// A host answer good enough for every moderation tool.
    fn any_moderation_answer() -> Value {
        json!({"room_id": ROOM, "participant_id": USER, "sessions_closed": 1,
               "muted": 1, "locked": true, "leak": 1})
    }

    fn mod_args(takes_participant: bool) -> Value {
        if takes_participant {
            json!({"room_id": ROOM, "participant_id": USER})
        } else {
            json!({"room_id": ROOM})
        }
    }

    #[test]
    fn moderation_tools_forward_the_verified_actor_and_whitelist_results() {
        let f = Fake::ok(
            json!({"room_id": ROOM, "participant_id": USER, "sessions_closed": 2, "leak": 1}),
        );
        let out = call(&f, "room.kick", ctx(MOD, &MOD_SCOPES, mod_args(true))).unwrap();
        assert_eq!(
            out,
            json!({"room_id": ROOM, "participant_id": USER, "sessions_closed": 2})
        );
        assert_eq!(
            f.calls()[0],
            (
                "room.kick".to_string(),
                json!({"room_id": ROOM, "participant_id": USER, "actor_id": MOD,
                       "actor_role": "moderator"})
            )
        );
        let f = Fake::ok(json!({"room_id": ROOM, "muted": 3, "x": 1}));
        let out = call(
            &f,
            "room.mute_all",
            ctx(MOD, &ADMIN_SCOPES, mod_args(false)),
        )
        .unwrap();
        assert_eq!(out, json!({"room_id": ROOM, "muted": 3}));
        assert_eq!(
            f.calls()[0].1,
            json!({"room_id": ROOM, "actor_id": MOD, "actor_role": "admin"}),
            "mute_all names the caller so the server can exclude them"
        );
        let f = Fake::ok(json!({"room_id": ROOM, "locked": true}));
        for t in ["room.lock", "room.unlock"] {
            call(&f, t, ctx(MOD, &MOD_SCOPES, mod_args(false))).unwrap();
        }
        assert_eq!(f.calls().len(), 2);
    }

    #[test]
    fn leave_removes_only_the_verified_caller_with_member_scope() {
        let f = Fake::ok(json!({"room_id": ROOM, "participant_id": USER, "sessions_closed": 1}));
        let out = call(&f, "room.leave", ctx(USER, &MEMBER_SCOPES, mod_args(false))).unwrap();
        assert_eq!(out["participant_id"], USER);
        assert_eq!(
            f.calls()[0].1,
            json!({"room_id": ROOM, "participant_id": USER, "actor_id": USER,
                   "actor_role": "member"})
        );
        // There is no participant argument: removing someone else is `room.kick`.
        assert!(matches!(
            call(
                &f,
                "room.leave",
                ctx(USER, &MEMBER_SCOPES, with_participant(OTHER))
            ),
            Err(ToolError::InvalidArgs(_))
        ));
        assert_eq!(f.calls().len(), 1);
    }

    #[test]
    fn moderation_tools_refuse_a_forged_actor_or_role_in_the_arguments() {
        let f = Fake::ok(any_moderation_answer());
        for (tool, takes) in MOD_TOOLS {
            for forged in [
                json!({"actor_role": "moderator"}),
                json!({"actor_role": "admin", "actor_id": MOD}),
                json!({"actor_id": MOD}),
                json!({"actor_id": USER}),
            ] {
                let mut args = mod_args(takes);
                for (k, v) in forged.as_object().unwrap() {
                    args[k] = v.clone();
                }
                let got = call(&f, tool, ctx(USER, &MEMBER_SCOPES, args.clone()));
                assert!(
                    matches!(got, Err(ToolError::InvalidArgs(ref m)) if m.contains("derived by the host")),
                    "{tool} {args}: {got:?}"
                );
            }
        }
        assert!(
            f.calls().is_empty(),
            "a forged actor never reaches the host"
        );
    }

    #[test]
    fn member_scope_cannot_reach_any_moderator_tool_and_the_scope_is_the_gate() {
        let f = Fake::ok(any_moderation_answer());
        for (tool, takes) in MOD_TOOLS {
            let member = call(&f, tool, ctx(USER, &MEMBER_SCOPES, mod_args(takes)));
            if tool == "room.leave" {
                assert!(member.is_ok(), "leave is member-level");
            } else {
                assert!(
                    matches!(member, Err(ToolError::NotGranted)),
                    "{tool} must need community:moderate"
                );
            }
        }
        assert_eq!(
            f.calls().len(),
            1,
            "only the member-level leave reached the host"
        );
        for (tool, takes) in MOD_TOOLS {
            call(&f, tool, ctx(MOD, &MOD_SCOPES, mod_args(takes))).unwrap();
        }
        assert_eq!(f.calls().len(), 1 + MOD_TOOLS.len());
    }

    #[test]
    fn no_verified_actor_or_scope_means_no_moderation_call() {
        let f = Fake::ok(any_moderation_answer());
        for (tool, takes) in MOD_TOOLS {
            let args = || mod_args(takes);
            assert!(
                matches!(call(&f, tool, args()), Err(ToolError::Forbidden)),
                "{tool} without context"
            );
            assert!(
                matches!(
                    call(&f, tool, ctx("alice", &ADMIN_SCOPES, args())),
                    Err(ToolError::Forbidden)
                ),
                "{tool} non-uuid actor"
            );
            assert!(
                matches!(
                    call(&f, tool, ctx(USER, &[], args())),
                    Err(ToolError::NotGranted)
                ),
                "{tool} no scopes"
            );
        }
        assert!(f.calls().is_empty());
    }

    #[test]
    fn every_moderation_host_call_carries_the_verified_actor_and_scope_derived_role() {
        for (who, scopes, role) in [
            (USER, &MEMBER_SCOPES[..], "member"),
            (MOD, &MOD_SCOPES[..], "moderator"),
            (MOD, &ADMIN_SCOPES[..], "admin"),
        ] {
            for (tool, takes) in MOD_TOOLS {
                if tool != "room.leave" && scopes == &MEMBER_SCOPES[..] {
                    continue;
                }
                let f = Fake::ok(any_moderation_answer());
                call(&f, tool, ctx(who, scopes, mod_args(takes))).unwrap();
                let calls = f.calls();
                assert_eq!(calls.len(), 1, "{tool}");
                assert_eq!(calls[0].0, tool);
                assert_eq!(calls[0].1["actor_id"], who, "{tool} actor_id");
                assert_eq!(calls[0].1["actor_role"], role, "{tool} actor_role");
            }
        }
    }

    #[test]
    fn invalid_moderation_args_never_reach_the_host() {
        let f = Fake::ok(json!({}));
        for (tool, args) in [
            (
                "room.kick",
                json!({"room_id": ROOM, "participant_id": "alice"}),
            ),
            ("room.kick", json!({"room_id": ROOM})),
            (
                "room.mute",
                json!({"room_id": ROOM, "participant_id": USER, "community_id": "7"}),
            ),
            ("room.lock", json!({"room_id": "x"})),
            ("room.unlock", json!({})),
            (
                "room.mute_all",
                json!({"room_id": ROOM, "participant_id": USER}),
            ),
        ] {
            let got = call(&f, tool, ctx(MOD, &ADMIN_SCOPES, args.clone()));
            assert!(
                matches!(got, Err(ToolError::InvalidArgs(_))),
                "{tool} {args}: {got:?}"
            );
        }
        assert!(f.calls().is_empty(), "invalid args never reach the host");
    }

    #[test]
    fn list_and_participants_whitelist_and_fail_loud_on_malformed_hosts() {
        let f = Fake::ok(
            json!({"rooms": [{"room_id": ROOM, "label": "x", "locked": false, "subscriber_count": 1, "community_id": "7"}]}),
        );
        let out = call(&f, "room.list", json!({})).unwrap();
        assert_eq!(out["rooms"][0].get("community_id"), None);
        assert_eq!(out["rooms"][0]["publisher_ids"], json!([]));
        let f = Fake::ok(json!({"room_id": ROOM, "locked": true, "count": 1,
            "participants": [{"participant_id": USER, "publisher": true, "subscriber": false, "muted": true, "name": "leak"}]}));
        let out = call(&f, "room.participants", json!({"room_id": ROOM})).unwrap();
        assert_eq!(
            out["participants"][0],
            json!({"participant_id": USER, "publisher": true, "subscriber": false, "muted": true})
        );
        assert_eq!(out["count"], 1);
        let f = Fake::ok(json!({"rooms": "nope"}));
        assert!(matches!(
            call(&f, "room.list", json!({})),
            Err(ToolError::TargetUnavailable)
        ));
        let f = Fake::ok(json!({"participants": [{"muted": true}]}));
        assert!(matches!(
            call(&f, "room.participants", json!({"room_id": ROOM})),
            Err(ToolError::TargetUnavailable)
        ));
    }

    #[test]
    fn moderation_host_refusals_are_typed_errors() {
        let f = Fake::err(HostError::Denied("forbidden".into()));
        let e = call(&f, "room.kick", ctx(MOD, &MOD_SCOPES, mod_args(true))).unwrap_err();
        assert!(matches!(e, ToolError::NotGranted));
        let f = Fake::err(HostError::NotImplemented(
            "feature rooms.moderation is disabled".into(),
        ));
        let e = call(&f, "room.lock", ctx(MOD, &ADMIN_SCOPES, mod_args(false))).unwrap_err();
        assert!(matches!(e, ToolError::InvalidArgs(m) if m.contains("disabled")));
    }

    const OTHER: &str = "44444444-4444-4444-4444-444444444444";
    const MEMBER_SCOPES: [&str; 1] = ["community:member"];
    const MOD_SCOPES: [&str; 2] = ["community:member", "community:moderate"];
    const ADMIN_SCOPES: [&str; 3] = ["community:member", "community:moderate", "community:admin"];

    /// `args` as the host would deliver them: with the verified actor and its
    /// host-derived scopes injected under the reserved context key.
    fn ctx(who: &str, scopes: &[&str], mut args: Value) -> Value {
        args["__ctx"] = json!({"actor": GLOBAL, "scopes": scopes, "participant": who});
        args
    }

    /// The caller's GLOBAL user uuid: must never appear in a host request.
    const GLOBAL: &str = "99999999-9999-9999-9999-999999999999";

    #[test]
    fn participant_self_returns_the_host_pseudonym_and_fails_closed() {
        let f = Fake::ok(json!({}));
        let out = call(&f, "participant.self", ctx(USER, &MEMBER_SCOPES, json!({}))).unwrap();
        assert_eq!(out, json!({"participant": USER}));
        assert!(f.calls().is_empty());
        // No pseudonym (host could not derive it): forbidden, never the global uuid.
        let no_p = json!({"__ctx": {"actor": GLOBAL, "scopes": MEMBER_SCOPES}});
        assert!(matches!(
            call(&f, "participant.self", no_p),
            Err(ToolError::Forbidden)
        ));
        assert!(matches!(
            call(&f, "participant.self", ctx(USER, &[], json!({}))),
            Err(ToolError::Forbidden | ToolError::NotGranted)
        ));
    }

    #[test]
    fn hand_calls_send_the_pseudonym_and_never_the_global_uuid() {
        let f = Fake::ok(json!({"room_id": ROOM, "hands": []}));
        let _ = call(&f, "room.hand.list", ctx(USER, &MEMBER_SCOPES, room_args()));
        let sent = format!("{:?}", f.calls());
        assert!(sent.contains(USER), "pseudonym is the actor id: {sent}");
        assert!(!sent.contains(GLOBAL), "global uuid leaked: {sent}");
    }

    fn room_args() -> Value {
        json!({"room_id": ROOM})
    }

    fn with_participant(p: &str) -> Value {
        json!({"room_id": ROOM, "participant_id": p})
    }

    /// A host queue of two hands (USER first, OTHER second, OTHER acknowledged).
    fn queue_json() -> Value {
        json!({"room_id": ROOM, "hands": [
            {"participant_id": USER, "raised_at": 10, "leak": 1},
            {"participant_id": OTHER, "raised_at": 11, "acknowledged_at": 12, "acknowledged_by": MOD}]})
    }

    /// A host answer good enough for every hand op.
    fn any_hand_answer() -> Value {
        json!({"room_id": ROOM, "participant_id": USER, "lowered": true, "cleared": 2,
               "hand": {"participant_id": USER, "raised_at": 1},
               "hands": [{"participant_id": USER, "raised_at": 1}]})
    }

    /// Valid args per hand tool (no actor fields, ever).
    fn good_args(op: HandOp) -> Value {
        match op {
            HandOp::LowerOther | HandOp::Ack => with_participant(OTHER),
            _ => room_args(),
        }
    }

    #[test]
    fn raise_forwards_v1_and_the_verified_actor_never_an_arg() {
        let f = Fake::ok(json!({"hand": {"participant_id": USER, "raised_at": 10, "leak": 1}}));
        let out = call(
            &f,
            "room.hand.raise",
            ctx(USER, &MEMBER_SCOPES, room_args()),
        )
        .unwrap();
        assert_eq!(
            out,
            json!({"room_id": ROOM, "hand": {"participant_id": USER, "raised_at": 10}})
        );
        assert_eq!(
            f.calls(),
            vec![(
                "room.hand.raise".to_string(),
                json!({"api_version": "v1", "room_id": ROOM, "participant_id": USER,
                       "actor_id": USER, "actor_role": "member"})
            )]
        );
    }

    #[test]
    fn every_hand_call_forwards_the_verified_actor_id_and_scope_derived_role() {
        for (who, scopes, role) in [
            (USER, &MEMBER_SCOPES[..], "member"),
            (MOD, &MOD_SCOPES[..], "moderator"),
            (MOD, &ADMIN_SCOPES[..], "admin"),
        ] {
            for op in HandOp::ALL {
                if op.min_role() > GrantRole::Member && scopes == &MEMBER_SCOPES[..] {
                    continue;
                }
                let f = Fake::ok(any_hand_answer());
                call(&f, op.tool(), ctx(who, scopes, good_args(op))).unwrap();
                let calls = f.calls();
                assert_eq!(calls.len(), 1, "{}", op.tool());
                assert_eq!(calls[0].0, op.host_op(), "{}", op.tool());
                assert_eq!(calls[0].1["actor_id"], who, "{} actor_id", op.tool());
                assert_eq!(calls[0].1["actor_role"], role, "{} actor_role", op.tool());
                assert_eq!(calls[0].1["api_version"], "v1", "{}", op.tool());
            }
        }
    }

    #[test]
    fn a_member_cannot_forge_a_role_or_actor_through_the_arguments() {
        let f = Fake::ok(any_hand_answer());
        for op in HandOp::ALL {
            for forged in [
                json!({"actor_role": "moderator"}),
                json!({"actor_role": "admin", "actor_id": MOD}),
                json!({"actor_id": MOD}),
                json!({"actor_id": USER}),
            ] {
                let mut args = good_args(op);
                for (k, v) in forged.as_object().unwrap() {
                    args[k] = v.clone();
                }
                let got = call(&f, op.tool(), ctx(USER, &MEMBER_SCOPES, args.clone()));
                assert!(
                    matches!(got, Err(ToolError::InvalidArgs(ref m)) if m.contains("derived by the host")),
                    "{} {args}: {got:?}",
                    op.tool()
                );
            }
        }
        assert!(
            f.calls().is_empty(),
            "a forged actor never reaches the host"
        );
    }

    #[test]
    fn member_scope_cannot_reach_any_moderator_hand_tool() {
        let f = Fake::ok(any_hand_answer());
        for op in [
            HandOp::LowerOther,
            HandOp::ListAll,
            HandOp::Ack,
            HandOp::Reset,
        ] {
            let got = call(&f, op.tool(), ctx(USER, &MEMBER_SCOPES, good_args(op)));
            assert!(matches!(got, Err(ToolError::NotGranted)), "{}", op.tool());
        }
        assert!(f.calls().is_empty(), "scope refusals never reach the host");
        // The same calls succeed with the moderate scope: the scope is the gate.
        for op in [
            HandOp::LowerOther,
            HandOp::ListAll,
            HandOp::Ack,
            HandOp::Reset,
        ] {
            call(&f, op.tool(), ctx(MOD, &MOD_SCOPES, good_args(op))).unwrap();
        }
        assert_eq!(f.calls().len(), 4);
    }

    #[test]
    fn no_verified_actor_or_scope_means_no_hand_call() {
        let f = Fake::ok(any_hand_answer());
        for op in HandOp::ALL {
            // No host context at all.
            assert!(
                matches!(
                    call(&f, op.tool(), good_args(op)),
                    Err(ToolError::Forbidden)
                ),
                "{} without context",
                op.tool()
            );
            // An actor id that is not a UUID is not a verified participant.
            assert!(
                matches!(
                    call(&f, op.tool(), ctx("alice", &ADMIN_SCOPES, good_args(op))),
                    Err(ToolError::Forbidden)
                ),
                "{} non-uuid actor",
                op.tool()
            );
            // A verified actor with no community scope at all (e.g. a guest).
            assert!(
                matches!(
                    call(&f, op.tool(), ctx(USER, &[], good_args(op))),
                    Err(ToolError::NotGranted)
                ),
                "{} no scopes",
                op.tool()
            );
        }
        assert!(f.calls().is_empty());
    }

    #[test]
    fn own_hand_tools_act_only_on_the_actor() {
        let f = Fake::ok(json!({"lowered": true}));
        let out = call(
            &f,
            "room.hand.lower",
            ctx(USER, &MEMBER_SCOPES, room_args()),
        )
        .unwrap();
        assert_eq!(
            out,
            json!({"room_id": ROOM, "participant_id": USER, "lowered": true})
        );
        assert_eq!(f.calls()[0].1["participant_id"], USER);
        // `lower` has no participant argument: naming someone else is rejected.
        assert!(matches!(
            call(
                &f,
                "room.hand.lower",
                ctx(USER, &MEMBER_SCOPES, with_participant(OTHER))
            ),
            Err(ToolError::InvalidArgs(_))
        ));
        assert_eq!(f.calls().len(), 1);
        // Moderators lower another participant's hand via `lower_other`.
        let out = call(
            &f,
            "room.hand.lower_other",
            ctx(MOD, &MOD_SCOPES, with_participant(OTHER)),
        )
        .unwrap();
        assert_eq!(out["participant_id"], OTHER);
        assert_eq!(f.calls()[1].1["participant_id"], OTHER);
        assert_eq!(f.calls()[1].1["actor_id"], MOD);
        // `lowered: false` (no hand) is a no-op answer, not an error.
        let f = Fake::ok(json!({"lowered": false}));
        let out = call(
            &f,
            "room.hand.lower",
            ctx(USER, &MEMBER_SCOPES, room_args()),
        )
        .unwrap();
        assert_eq!(out["lowered"], false);
    }

    #[test]
    fn list_shows_members_only_their_own_entry_and_list_all_shows_moderators_the_queue() {
        let f = Fake::ok(queue_json());
        let mine = call(
            &f,
            "room.hand.list",
            ctx(OTHER, &MEMBER_SCOPES, room_args()),
        )
        .unwrap();
        assert_eq!(mine["count"], 2, "queue length is visible");
        assert_eq!(mine["hands"].as_array().unwrap().len(), 1);
        assert_eq!(mine["hands"][0]["participant_id"], OTHER);
        assert_eq!(mine["hands"][0]["position"], 2);
        // Even a moderator calling the own-view tool only sees their own entry.
        let none = call(&f, "room.hand.list", ctx(MOD, &MOD_SCOPES, room_args())).unwrap();
        assert!(none["hands"].as_array().unwrap().is_empty());
        assert_eq!(none["count"], 2);
        let all = call(&f, "room.hand.list_all", ctx(MOD, &MOD_SCOPES, room_args())).unwrap();
        assert_eq!(all["hands"].as_array().unwrap().len(), 2);
        assert_eq!(all["hands"][1]["position"], 2);
        assert_eq!(all["hands"][1]["acknowledged_by"], MOD);
        assert!(all["hands"][0].get("leak").is_none());
        assert_eq!(
            f.calls()[0],
            (
                "room.hand.list".to_string(),
                json!({"api_version": "v1", "room_id": ROOM, "actor_id": OTHER,
                       "actor_role": "member"})
            )
        );
    }

    #[test]
    fn ack_and_reset_whitelist_results() {
        let f = Fake::ok(json!({"hand": {"participant_id": USER, "raised_at": 1,
            "acknowledged_at": 2, "acknowledged_by": MOD, "leak": 1}, "cleared": 3}));
        let out = call(
            &f,
            "room.hand.ack",
            ctx(MOD, &MOD_SCOPES, with_participant(USER)),
        )
        .unwrap();
        assert_eq!(out["hand"]["acknowledged_by"], MOD);
        assert!(out["hand"].get("leak").is_none());
        assert_eq!(
            f.calls()[0].1,
            json!({"api_version": "v1", "room_id": ROOM, "participant_id": USER,
                   "actor_id": MOD, "actor_role": "moderator"})
        );
        let out = call(&f, "room.hand.reset", ctx(MOD, &ADMIN_SCOPES, room_args())).unwrap();
        assert_eq!(out, json!({"room_id": ROOM, "cleared": 3}));
    }

    #[test]
    fn invalid_hand_args_never_reach_the_host() {
        let f = Fake::ok(queue_json());
        for (tool, args) in [
            (
                "room.hand.raise",
                json!({"room_id": ROOM, "api_version": "v2"}),
            ),
            (
                "room.hand.list",
                json!({"room_id": ROOM, "community_id": "7"}),
            ),
            ("room.hand.raise", json!({"room_id": ROOM, "name": "alice"})),
            ("room.hand.ack", with_participant("alice")),
            ("room.hand.lower_other", with_participant("alice")),
            ("room.hand.ack", room_args()),
            ("room.hand.raise", json!({"room_id": "x"})),
            ("room.hand.reset", json!({})),
        ] {
            let got = call(&f, tool, ctx(MOD, &ADMIN_SCOPES, args.clone()));
            assert!(
                matches!(got, Err(ToolError::InvalidArgs(_))),
                "{tool} {args}: {got:?}"
            );
        }
        assert!(f.calls().is_empty(), "no invalid call may reach the host");
    }

    #[test]
    fn hand_host_failures_are_typed_and_malformed_results_fail_loud() {
        let member = || ctx(USER, &MEMBER_SCOPES, room_args());
        let staff = || ctx(MOD, &MOD_SCOPES, room_args());
        let f = Fake::err(HostError::Denied("forbidden".into()));
        assert!(matches!(
            call(&f, "room.hand.raise", member()),
            Err(ToolError::NotGranted)
        ));
        let f = Fake::err(HostError::NotFound);
        assert!(matches!(
            call(&f, "room.hand.list", member()),
            Err(ToolError::InvalidArgs(_))
        ));
        let f = Fake::err(HostError::Unavailable("x".into()));
        assert!(matches!(
            call(&f, "room.hand.reset", staff()),
            Err(ToolError::TargetUnavailable)
        ));
        for (tool, args, bad_host) in [
            ("room.hand.raise", member(), json!({})),
            (
                "room.hand.raise",
                member(),
                json!({"hand": {"raised_at": 1}}),
            ),
            ("room.hand.list", member(), json!({"hands": "nope"})),
            (
                "room.hand.list_all",
                staff(),
                json!({"hands": [{"raised_at": 1}]}),
            ),
            ("room.hand.reset", staff(), json!({"cleared": "many"})),
            ("room.hand.lower", member(), json!({"room_id": ROOM})),
        ] {
            let f = Fake::ok(bad_host);
            assert!(
                matches!(call(&f, tool, args), Err(ToolError::TargetUnavailable)),
                "{tool}"
            );
        }
    }

    #[test]
    fn hand_tool_names_match_the_contract_ops() {
        for op in HandOp::ALL {
            assert!(TOOL_NAMES.contains(&op.tool()), "{}", op.tool());
        }
        assert_eq!(TOOL_NAMES.len(), 22 + HandOp::ALL.len());
    }
}
