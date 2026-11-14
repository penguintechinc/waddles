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

use crate::grants::{GrantRole, HandOp};

/// Every tool name this bundle exposes (also the only labels ever logged).
pub const TOOL_NAMES: [&str; 26] = [
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
    "room.hand.list",
    "room.hand.ack",
    "room.hand.reset",
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
    let o = object(&args, &["label", "max_publishers", "route"])?;
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

/// Roles the rooms app may assert for the acting participant (host re-checks).
const ACTOR_ROLES: [&str; 4] = ["guest", "member", "moderator", "admin"];

/// Parses the `actor_id` / `actor_role` pair every moderation tool carries.
/// The rooms app resolved the role from its per-room ACL; svc-streaming
/// enforces it again as defense in depth (interim until the SDK `caller_can`
/// check replaces `actor_role`).
fn actor_args(o: &Map<String, Value>) -> Result<(String, String), ToolError> {
    let id = uuid_arg(o, "actor_id", true)?.ok_or_else(|| bad("actor_id required"))?;
    match o.get("actor_role") {
        Some(Value::String(r)) if ACTOR_ROLES.contains(&r.as_str()) => Ok((id, r.clone())),
        _ => Err(bad("actor_role must be guest|member|moderator|admin")),
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

/// Moderation on one participant (`leave|kick|mute|unmute`): validated args in,
/// whitelisted result out.
fn participant_op(
    op: &'static str,
    fields: &'static [&'static str],
) -> impl Fn(&dyn Media, &ToolContext, Value) -> Result<Value, ToolError> {
    move |m, _, args| {
        let o = object(
            &args,
            &["room_id", "participant_id", "actor_id", "actor_role"],
        )?;
        let (actor_id, actor_role) = actor_args(o)?;
        let req = json!({
            "room_id": uuid_arg(o, "room_id", true)?,
            "participant_id": uuid_arg(o, "participant_id", true)?,
            "actor_id": actor_id, "actor_role": actor_role,
        });
        pick(&m.call(op, &req).map_err(tool_err)?, fields)
    }
}

/// Moderation on a whole room (`mute_all|lock|unlock`).
fn room_mod_op(
    op: &'static str,
    fields: &'static [&'static str],
) -> impl Fn(&dyn Media, &ToolContext, Value) -> Result<Value, ToolError> {
    move |m, _, args| {
        let o = object(&args, &["room_id", "actor_id", "actor_role"])?;
        let (actor_id, actor_role) = actor_args(o)?;
        let req = json!({
            "room_id": uuid_arg(o, "room_id", true)?,
            "actor_id": actor_id, "actor_role": actor_role,
        });
        pick(&m.call(op, &req).map_err(tool_err)?, fields)
    }
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

/// Parses the acting participant of a hand tool and enforces the contract's
/// minimum role for `op` BEFORE any host call (the host re-checks, and the
/// manifest `required_scope` is verified by the host before dispatch).
fn hand_actor(o: &Map<String, Value>, op: HandOp) -> Result<(String, GrantRole), ToolError> {
    let id = uuid_arg(o, "actor_id", true)?.ok_or_else(|| bad("actor_id required"))?;
    let role = match o.get("actor_role") {
        Some(Value::String(r)) => GrantRole::parse(r),
        _ => None,
    }
    .ok_or_else(|| bad("actor_role must be guest|member|moderator|admin"))?;
    if role < op.min_role() {
        return Err(ToolError::NotGranted);
    }
    Ok((id, role))
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

/// `room.hand.raise`: the actor raises THEIR OWN hand (idempotent host-side).
fn hand_raise(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(&args, &["api_version", "room_id", "actor_id", "actor_role"])?;
    api_version_arg(o)?;
    let (actor, _) = hand_actor(o, HandOp::Raise)?;
    let room = uuid_arg(o, "room_id", true)?;
    let req = json!({"api_version": API_VERSION, "room_id": room, "participant_id": actor});
    let out = m.call(HandOp::Raise.tool(), &req).map_err(tool_err)?;
    let hand = hand_view(out.get("hand").ok_or(ToolError::TargetUnavailable)?)?;
    Ok(json!({"room_id": room, "hand": hand}))
}

/// `room.hand.lower`: members lower their own hand; lowering another participant's
/// needs a moderator role (refused here before the host sees it).
fn hand_lower(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(
        &args,
        &[
            "api_version",
            "room_id",
            "participant_id",
            "actor_id",
            "actor_role",
        ],
    )?;
    api_version_arg(o)?;
    let (actor, role) = hand_actor(o, HandOp::Lower)?;
    let participant =
        uuid_arg(o, "participant_id", true)?.ok_or_else(|| bad("participant_id required"))?;
    if participant != actor && !role.is_staff() {
        return Err(ToolError::NotGranted);
    }
    let room = uuid_arg(o, "room_id", true)?;
    let req = json!({
        "api_version": API_VERSION, "room_id": room, "participant_id": participant,
        "actor_id": actor, "actor_role": role_wire(role),
    });
    let out = m.call(HandOp::Lower.tool(), &req).map_err(tool_err)?;
    let lowered = out
        .get("lowered")
        .and_then(Value::as_bool)
        .ok_or(ToolError::TargetUnavailable)?;
    Ok(json!({"room_id": room, "participant_id": participant, "lowered": lowered}))
}

/// `room.hand.list`: staff see the whole ordered queue; members see only their own
/// entry (with its position) plus the queue length.
fn hand_list(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(&args, &["api_version", "room_id", "actor_id", "actor_role"])?;
    api_version_arg(o)?;
    let (actor, role) = hand_actor(o, HandOp::List)?;
    let room = uuid_arg(o, "room_id", true)?;
    let req = json!({"api_version": API_VERSION, "room_id": room});
    let out = m.call(HandOp::List.tool(), &req).map_err(tool_err)?;
    let queue = out
        .get("hands")
        .and_then(Value::as_array)
        .ok_or(ToolError::TargetUnavailable)?;
    let mut hands = Vec::with_capacity(queue.len());
    for (i, h) in queue.iter().enumerate() {
        let mut v = hand_view(h)?;
        let mine = v.get("participant_id").and_then(Value::as_str) == Some(actor.as_str());
        if role.is_staff() || mine {
            v["position"] = json!(i + 1);
            hands.push(v);
        }
    }
    Ok(json!({"room_id": room, "count": queue.len(), "hands": hands}))
}

/// `room.hand.ack`: a moderator marks one raised hand acknowledged.
fn hand_ack(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(
        &args,
        &[
            "api_version",
            "room_id",
            "participant_id",
            "actor_id",
            "actor_role",
        ],
    )?;
    api_version_arg(o)?;
    let (actor, role) = hand_actor(o, HandOp::Ack)?;
    let room = uuid_arg(o, "room_id", true)?;
    let req = json!({
        "api_version": API_VERSION, "room_id": room,
        "participant_id": uuid_arg(o, "participant_id", true)?,
        "actor_id": actor, "actor_role": role_wire(role),
    });
    let out = m.call(HandOp::Ack.tool(), &req).map_err(tool_err)?;
    let hand = hand_view(out.get("hand").ok_or(ToolError::TargetUnavailable)?)?;
    Ok(json!({"room_id": room, "hand": hand}))
}

/// `room.hand.reset`: a moderator clears the whole queue; returns how many.
fn hand_reset(m: &dyn Media, _: &ToolContext, args: Value) -> Result<Value, ToolError> {
    let o = object(&args, &["api_version", "room_id", "actor_id", "actor_role"])?;
    api_version_arg(o)?;
    let (actor, role) = hand_actor(o, HandOp::Reset)?;
    let room = uuid_arg(o, "room_id", true)?;
    let req = json!({
        "api_version": API_VERSION, "room_id": room,
        "actor_id": actor, "actor_role": role_wire(role),
    });
    let out = m.call(HandOp::Reset.tool(), &req).map_err(tool_err)?;
    let cleared = out
        .get("cleared")
        .and_then(Value::as_u64)
        .ok_or(ToolError::TargetUnavailable)?;
    Ok(json!({"room_id": room, "cleared": cleared}))
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
    reg(&mut r, "room.list", Box::new(rooms_list));
    reg(&mut r, "room.participants", Box::new(participants));
    let gone: &[&str] = &["room_id", "participant_id", "sessions_closed"];
    let muted: &[&str] = &["room_id", "participant_id", "muted"];
    reg(
        &mut r,
        "room.leave",
        Box::new(participant_op("room.leave", gone)),
    );
    reg(
        &mut r,
        "room.kick",
        Box::new(participant_op("room.kick", gone)),
    );
    reg(
        &mut r,
        "room.mute",
        Box::new(participant_op("room.mute", muted)),
    );
    reg(
        &mut r,
        "room.unmute",
        Box::new(participant_op("room.unmute", muted)),
    );
    reg(
        &mut r,
        "room.mute_all",
        Box::new(room_mod_op("room.mute_all", &["room_id", "muted"])),
    );
    reg(
        &mut r,
        "room.lock",
        Box::new(room_mod_op("room.lock", &["room_id", "locked"])),
    );
    reg(
        &mut r,
        "room.unlock",
        Box::new(room_mod_op("room.unlock", &["room_id", "locked"])),
    );
    reg(&mut r, HandOp::Raise.tool(), Box::new(hand_raise));
    reg(&mut r, HandOp::Lower.tool(), Box::new(hand_lower));
    reg(&mut r, HandOp::List.tool(), Box::new(hand_list));
    reg(&mut r, HandOp::Ack.tool(), Box::new(hand_ack));
    reg(&mut r, HandOp::Reset.tool(), Box::new(hand_reset));
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

    #[test]
    fn moderation_tools_forward_actor_and_whitelist_results() {
        let f = Fake::ok(
            json!({"room_id": ROOM, "participant_id": USER, "sessions_closed": 2, "leak": 1}),
        );
        let out = call(
            &f,
            "room.kick",
            json!({"room_id": ROOM, "participant_id": USER, "actor_id": MOD, "actor_role": "moderator"}),
        )
        .unwrap();
        assert_eq!(
            out,
            json!({"room_id": ROOM, "participant_id": USER, "sessions_closed": 2})
        );
        assert_eq!(
            f.calls()[0],
            (
                "room.kick".to_string(),
                json!({"room_id": ROOM, "participant_id": USER, "actor_id": MOD, "actor_role": "moderator"})
            )
        );
        let f = Fake::ok(json!({"room_id": ROOM, "muted": 3, "x": 1}));
        let out = call(
            &f,
            "room.mute_all",
            json!({"room_id": ROOM, "actor_id": MOD, "actor_role": "admin"}),
        )
        .unwrap();
        assert_eq!(out, json!({"room_id": ROOM, "muted": 3}));
        let f = Fake::ok(json!({"room_id": ROOM, "locked": true}));
        for t in ["room.lock", "room.unlock"] {
            call(
                &f,
                t,
                json!({"room_id": ROOM, "actor_id": MOD, "actor_role": "moderator"}),
            )
            .unwrap();
        }
        assert_eq!(f.calls().len(), 2);
    }

    #[test]
    fn moderation_tools_reject_bad_actor_or_extra_args_before_the_host() {
        let f = Fake::ok(json!({}));
        for args in [
            json!({"room_id": ROOM, "participant_id": USER, "actor_id": MOD}),
            json!({"room_id": ROOM, "participant_id": USER, "actor_id": MOD, "actor_role": "root"}),
            json!({"room_id": ROOM, "participant_id": USER, "actor_id": "bob", "actor_role": "admin"}),
            json!({"room_id": ROOM, "participant_id": USER, "actor_id": MOD, "actor_role": "admin", "community_id": "7"}),
            json!({"room_id": ROOM, "participant_id": "alice", "actor_id": MOD, "actor_role": "admin"}),
        ] {
            for t in ["room.kick", "room.mute", "room.unmute", "room.leave"] {
                assert!(
                    matches!(call(&f, t, args.clone()), Err(ToolError::InvalidArgs(_))),
                    "{t} {args}"
                );
            }
        }
        assert!(matches!(
            call(&f, "room.lock", json!({"room_id": ROOM, "actor_id": MOD})),
            Err(ToolError::InvalidArgs(_))
        ));
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
        let e = call(
            &f,
            "room.kick",
            json!({"room_id": ROOM, "participant_id": USER, "actor_id": MOD, "actor_role": "member"}),
        )
        .unwrap_err();
        assert!(matches!(e, ToolError::NotGranted));
        let f = Fake::err(HostError::NotImplemented(
            "feature rooms.moderation is disabled".into(),
        ));
        let e = call(
            &f,
            "room.lock",
            json!({"room_id": ROOM, "actor_id": MOD, "actor_role": "admin"}),
        )
        .unwrap_err();
        assert!(matches!(e, ToolError::InvalidArgs(m) if m.contains("disabled")));
    }

    const OTHER: &str = "44444444-4444-4444-4444-444444444444";

    /// A host queue of two hands (USER first, OTHER second, OTHER acknowledged).
    fn queue_json() -> Value {
        json!({"room_id": ROOM, "hands": [
            {"participant_id": USER, "raised_at": 10, "leak": 1},
            {"participant_id": OTHER, "raised_at": 11, "acknowledged_at": 12, "acknowledged_by": MOD}]})
    }

    fn actor(who: &str, role: &str) -> Value {
        json!({"room_id": ROOM, "actor_id": who, "actor_role": role})
    }

    #[test]
    fn raise_forwards_v1_with_the_actor_as_participant_and_whitelists() {
        let f = Fake::ok(json!({"hand": {"participant_id": USER, "raised_at": 10, "leak": 1}}));
        let out = call(&f, "room.hand.raise", actor(USER, "member")).unwrap();
        assert_eq!(
            out,
            json!({"room_id": ROOM, "hand": {"participant_id": USER, "raised_at": 10}})
        );
        assert_eq!(
            f.calls(),
            vec![(
                "room.hand.raise".to_string(),
                json!({"api_version": "v1", "room_id": ROOM, "participant_id": USER})
            )]
        );
    }

    #[test]
    fn lower_self_is_member_level_and_lowering_others_needs_staff() {
        let f = Fake::ok(json!({"room_id": ROOM, "participant_id": USER, "lowered": true}));
        let mut own = actor(USER, "member");
        own["participant_id"] = json!(USER);
        let out = call(&f, "room.hand.lower", own).unwrap();
        assert_eq!(
            out,
            json!({"room_id": ROOM, "participant_id": USER, "lowered": true})
        );
        assert_eq!(
            f.calls()[0].1,
            json!({"api_version": "v1", "room_id": ROOM, "participant_id": USER,
                   "actor_id": USER, "actor_role": "member"})
        );
        let mut other = actor(USER, "member");
        other["participant_id"] = json!(OTHER);
        assert!(matches!(
            call(&f, "room.hand.lower", other.clone()),
            Err(ToolError::NotGranted)
        ));
        assert_eq!(f.calls().len(), 1, "refusal never reached the host");
        other["actor_id"] = json!(MOD);
        other["actor_role"] = json!("moderator");
        assert!(call(&f, "room.hand.lower", other).is_ok());
        // `lowered: false` (no hand) is a no-op answer, not an error.
        let f = Fake::ok(json!({"lowered": false}));
        let mut own = actor(USER, "member");
        own["participant_id"] = json!(USER);
        assert_eq!(call(&f, "room.hand.lower", own).unwrap()["lowered"], false);
    }

    #[test]
    fn list_shows_staff_the_queue_and_members_only_their_own_entry() {
        let f = Fake::ok(queue_json());
        let staff = call(&f, "room.hand.list", actor(MOD, "moderator")).unwrap();
        assert_eq!(staff["count"], 2);
        assert_eq!(staff["hands"].as_array().unwrap().len(), 2);
        assert_eq!(staff["hands"][1]["position"], 2);
        assert_eq!(staff["hands"][1]["acknowledged_by"], MOD);
        assert!(staff["hands"][0].get("leak").is_none());
        let mine = call(&f, "room.hand.list", actor(OTHER, "member")).unwrap();
        assert_eq!(mine["count"], 2, "queue length is visible");
        assert_eq!(mine["hands"].as_array().unwrap().len(), 1);
        assert_eq!(mine["hands"][0]["participant_id"], OTHER);
        assert_eq!(mine["hands"][0]["position"], 2);
        let none = call(&f, "room.hand.list", actor(MOD, "member")).unwrap();
        assert!(none["hands"].as_array().unwrap().is_empty());
        assert_eq!(
            f.calls()[0],
            (
                "room.hand.list".to_string(),
                json!({"api_version": "v1", "room_id": ROOM})
            )
        );
    }

    #[test]
    fn ack_and_reset_are_moderator_only_and_forward_the_role() {
        let f = Fake::ok(json!({"hand": {"participant_id": USER, "raised_at": 1,
            "acknowledged_at": 2, "acknowledged_by": MOD}, "cleared": 3}));
        let mut ack = actor(MOD, "moderator");
        ack["participant_id"] = json!(USER);
        let out = call(&f, "room.hand.ack", ack.clone()).unwrap();
        assert_eq!(out["hand"]["acknowledged_by"], MOD);
        assert_eq!(
            f.calls()[0].1,
            json!({"api_version": "v1", "room_id": ROOM, "participant_id": USER,
                   "actor_id": MOD, "actor_role": "moderator"})
        );
        let out = call(&f, "room.hand.reset", actor(MOD, "admin")).unwrap();
        assert_eq!(out, json!({"room_id": ROOM, "cleared": 3}));
        let n = f.calls().len();
        ack["actor_role"] = json!("member");
        for (tool, args) in [
            ("room.hand.ack", ack),
            ("room.hand.reset", actor(USER, "member")),
            ("room.hand.reset", actor(USER, "guest")),
        ] {
            assert!(
                matches!(call(&f, tool, args), Err(ToolError::NotGranted)),
                "{tool}"
            );
        }
        assert_eq!(f.calls().len(), n, "role refusals never reach the host");
    }

    #[test]
    fn guests_cannot_use_any_hand_tool() {
        let f = Fake::ok(queue_json());
        for tool in ["room.hand.raise", "room.hand.list"] {
            assert!(matches!(
                call(&f, tool, actor(USER, "guest")),
                Err(ToolError::NotGranted)
            ));
        }
        assert!(f.calls().is_empty());
    }

    #[test]
    fn invalid_hand_args_never_reach_the_host() {
        let f = Fake::ok(queue_json());
        let mut v2 = actor(USER, "member");
        v2["api_version"] = json!("v2");
        let mut extra = actor(MOD, "moderator");
        extra["community_id"] = json!("7");
        let mut name = actor(USER, "member");
        name["name"] = json!("alice");
        let mut bad_participant = actor(MOD, "moderator");
        bad_participant["participant_id"] = json!("alice");
        for (tool, args) in [
            ("room.hand.raise", v2),
            ("room.hand.list", extra),
            ("room.hand.raise", name),
            ("room.hand.ack", bad_participant.clone()),
            ("room.hand.lower", bad_participant),
            ("room.hand.ack", actor(MOD, "moderator")),
            (
                "room.hand.raise",
                json!({"room_id": "x", "actor_id": USER, "actor_role": "member"}),
            ),
            ("room.hand.raise", actor("bob", "member")),
            ("room.hand.reset", actor(MOD, "root")),
            ("room.hand.reset", json!({"room_id": ROOM, "actor_id": MOD})),
        ] {
            assert!(
                matches!(call(&f, tool, args.clone()), Err(ToolError::InvalidArgs(_))),
                "{tool} {args}"
            );
        }
        assert!(f.calls().is_empty(), "no invalid call may reach the host");
    }

    #[test]
    fn hand_host_failures_are_typed_and_malformed_results_fail_loud() {
        let f = Fake::err(HostError::Denied("forbidden".into()));
        assert!(matches!(
            call(&f, "room.hand.raise", actor(USER, "member")),
            Err(ToolError::NotGranted)
        ));
        let f = Fake::err(HostError::NotFound);
        assert!(matches!(
            call(&f, "room.hand.list", actor(USER, "member")),
            Err(ToolError::InvalidArgs(_))
        ));
        let f = Fake::err(HostError::Unavailable("x".into()));
        assert!(matches!(
            call(&f, "room.hand.reset", actor(MOD, "admin")),
            Err(ToolError::TargetUnavailable)
        ));
        for (tool, args, bad_host) in [
            ("room.hand.raise", actor(USER, "member"), json!({})),
            (
                "room.hand.raise",
                actor(USER, "member"),
                json!({"hand": {"raised_at": 1}}),
            ),
            (
                "room.hand.list",
                actor(MOD, "admin"),
                json!({"hands": "nope"}),
            ),
            (
                "room.hand.list",
                actor(MOD, "admin"),
                json!({"hands": [{"raised_at": 1}]}),
            ),
            (
                "room.hand.reset",
                actor(MOD, "admin"),
                json!({"cleared": "many"}),
            ),
        ] {
            let f = Fake::ok(bad_host);
            assert!(
                matches!(call(&f, tool, args), Err(ToolError::TargetUnavailable)),
                "{tool}"
            );
        }
        let mut lower = actor(USER, "member");
        lower["participant_id"] = json!(USER);
        let f = Fake::ok(json!({"room_id": ROOM}));
        assert!(matches!(
            call(&f, "room.hand.lower", lower),
            Err(ToolError::TargetUnavailable)
        ));
    }

    #[test]
    fn hand_tool_names_match_the_contract_ops() {
        for op in HandOp::ALL {
            assert!(TOOL_NAMES.contains(&op.tool()), "{}", op.tool());
        }
    }
}
