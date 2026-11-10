//! Host-independent rooms logic.
//!
//! The rooms app owns the *policy* of who may be in which room and when; every
//! media/token operation is delegated to the `webrtc` SERVICE bundle through
//! gated tool calls (app -> service only; this bundle never calls another app).
//! All state lives in the bundle's community-scoped kv and is reached through
//! three seams ([`Tools`], [`Store`], [`Ident`]) so the whole behavior is
//! unit-tested with fakes and the wasm glue in `lib.rs` supplies the real ones.
//!
//! Two surfaces share this logic:
//! * **chat commands** (`!room ...`) -- members list/inspect rooms; creating,
//!   closing and nesting rooms ride webrtc tools that carry
//!   `required_scope: community:admin`, so the HOST verifies the actor's scope
//!   (this bundle never sees or trusts a chat badge);
//! * **admin actions** ([`admin`]) -- called by the bundle-admin gateway with an
//!   already-verified principal: event links, access policy, schedules, the
//!   per-room ACL, and join-credential minting. Join tokens are bearer
//!   credentials and are therefore NEVER placed in chat.
//!
//! Roles are data (a per-room ACL of actor UUIDs), not chat roles; entity
//! references are UUIDs; kv keys use `.` (never `:`); logs carry op/outcome only.

#[cfg(test)]
use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

/// The webrtc service bundle this app is granted tools on.
pub const SERVICE: &str = "waddles.core.service.webrtc";
/// Most rooms tracked per community (index bound).
pub const MAX_ROOMS: usize = 256;
/// Longest room label.
pub const MAX_LABEL: usize = 128;

/// How a room came to exist.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Community,
    Event,
    Breakout,
    Stage,
    GreenRoom,
}

/// Access role, from the per-room ACL (never from chat roles).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Guest,
    Member,
    Moderator,
    Admin,
}

/// Optional open window (unix seconds); `close_at` exclusive.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Schedule {
    pub open_at: i64,
    pub close_at: i64,
}

/// One room record (kv `room.<id>`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Room {
    /// The webrtc room UUID (the only identifier used across boundaries).
    pub id: String,
    pub kind: Kind,
    pub label: String,
    /// Parent room for virtual rooms (breakout / stage / green room).
    pub parent: Option<String>,
    /// Calendar event public UUID reference (data link only).
    pub event_ref: Option<String>,
    /// Whether unauthenticated-to-the-community guests may subscribe.
    pub allow_guests: bool,
    pub schedule: Option<Schedule>,
    /// Host-closed (tool `room.close` succeeded).
    pub closed: bool,
    pub created_by: String,
}

/// What can go wrong; every variant maps to one closed user/admin message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RoomsError {
    Usage(String),
    NotFound,
    Denied(String),
    Conflict(String),
    Tool(ToolFail),
    Store(String),
}

/// A failed webrtc tool call (mirrors the SDK `tool-error`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolFail {
    NotGranted,
    Forbidden,
    InvalidArgs(String),
    Unavailable,
}

/// `tool.invoke` against the webrtc service bundle.
pub trait Tools {
    /// Calls `tool` with `args`; writes pass an idempotency key.
    fn invoke(&self, tool: &str, args: &Value, idem: Option<&str>) -> Result<Value, ToolFail>;
}

/// Community-scoped kv.
pub trait Store {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    fn set(&self, key: &str, value: &[u8]) -> Result<(), String>;
    fn delete(&self, key: &str) -> Result<(), String>;
}

/// The host-verified actor behind this invocation.
pub trait Ident {
    fn actor(&self) -> Result<String, String>;
}

fn is_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit() && !c.is_ascii_uppercase(),
        })
}

fn need_uuid(what: &str, s: &str) -> Result<(), RoomsError> {
    if is_uuid(s) {
        Ok(())
    } else {
        Err(RoomsError::Usage(format!(
            "{what} must be a lowercase UUID"
        )))
    }
}

fn label_ok(l: &str) -> Result<(), RoomsError> {
    if l.is_empty() || l.len() > MAX_LABEL || l.chars().any(char::is_control) {
        Err(RoomsError::Usage(format!(
            "label must be 1..={MAX_LABEL} printable characters"
        )))
    } else {
        Ok(())
    }
}

/// Replay-safe idempotency key for one (message, op).
pub fn idem_key(message_id: &str, op: &str) -> String {
    let mut h = Sha256::new();
    h.update(format!("rooms|{message_id}|{op}").as_bytes());
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

// ---------------------------------------------------------------- storage

fn kv_json<T: for<'de> Deserialize<'de>>(
    s: &dyn Store,
    key: &str,
) -> Result<Option<T>, RoomsError> {
    match s.get(key).map_err(RoomsError::Store)? {
        None => Ok(None),
        Some(b) => serde_json::from_slice(&b)
            .map(Some)
            .map_err(|_| RoomsError::Store(format!("corrupt record under {}", key_label(key)))),
    }
}

/// Key family only -- keys embed UUIDs which are fine, but keep errors terse.
fn key_label(key: &str) -> &str {
    key.split('.').next().unwrap_or("kv")
}

fn put_json<T: Serialize>(s: &dyn Store, key: &str, v: &T) -> Result<(), RoomsError> {
    let b = serde_json::to_vec(v).map_err(|e| RoomsError::Store(e.to_string()))?;
    s.set(key, &b).map_err(RoomsError::Store)
}

fn room_key(id: &str) -> String {
    format!("room.{id}")
}
fn acl_key(room: &str, actor: &str) -> String {
    format!("acl.{room}.{actor}")
}
const INDEX_KEY: &str = "rooms.index";

fn index(s: &dyn Store) -> Result<Vec<String>, RoomsError> {
    // Absent key == no rooms created yet (an explicit, valid state); corrupt data errors in kv_json.
    match kv_json::<Vec<String>>(s, INDEX_KEY)? {
        Some(ids) => Ok(ids),
        None => Ok(Vec::new()),
    }
}

fn load(s: &dyn Store, id: &str) -> Result<Room, RoomsError> {
    need_uuid("room id", id)?;
    kv_json(s, &room_key(id))?.ok_or(RoomsError::NotFound)
}

fn save(s: &dyn Store, r: &Room) -> Result<(), RoomsError> {
    put_json(s, &room_key(&r.id), r)
}

fn list_rooms(s: &dyn Store) -> Result<Vec<Room>, RoomsError> {
    let mut out = Vec::new();
    for id in index(s)? {
        if let Some(r) = kv_json::<Room>(s, &room_key(&id))? {
            out.push(r);
        }
    }
    Ok(out)
}

// ------------------------------------------------------------ access policy

/// Publish/subscribe grants a `role` gets in `room` at `now`, or why not.
/// Pure: the single source of access truth for join tokens.
pub fn grants_for(room: &Room, role: Role, now: i64) -> Result<(bool, bool), RoomsError> {
    if room.closed {
        return Err(RoomsError::Denied("room is closed".into()));
    }
    let staff = role >= Role::Moderator;
    if let Some(s) = room.schedule
        && !staff
        && !(s.open_at <= now && now < s.close_at)
    {
        return Err(RoomsError::Denied(
            "room is outside its scheduled window".into(),
        ));
    }
    match (room.kind, role) {
        (Kind::GreenRoom, r) if r < Role::Moderator => {
            Err(RoomsError::Denied("green room is for moderators".into()))
        }
        (_, Role::Guest) if !room.allow_guests => Err(RoomsError::Denied(
            "guests are not allowed in this room".into(),
        )),
        (_, Role::Guest) => Ok((false, true)),
        (Kind::Stage, r) if r < Role::Moderator => Ok((false, true)),
        _ => Ok((true, true)),
    }
}

fn role_of(s: &dyn Store, room: &str, actor: &str) -> Result<Role, RoomsError> {
    Ok(kv_json::<Role>(s, &acl_key(room, actor))?.unwrap_or(Role::Member))
}

// ------------------------------------------------------------- webrtc calls

fn tool_err(e: ToolFail) -> RoomsError {
    RoomsError::Tool(e)
}

fn whitelist_room(v: &Value) -> Result<String, RoomsError> {
    v.get("room_id")
        .and_then(Value::as_str)
        .filter(|s| is_uuid(s))
        .map(str::to_string)
        .ok_or(RoomsError::Tool(ToolFail::Unavailable))
}

/// Every argument is a distinct, already-validated fact of one room; a struct would only rename them.
#[allow(clippy::too_many_arguments)]
fn create_room(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    actor: &str,
    label: &str,
    kind: Kind,
    parent: Option<&str>,
    event_ref: Option<&str>,
) -> Result<Room, RoomsError> {
    label_ok(label)?;
    let mut idx = index(store)?;
    if idx.len() >= MAX_ROOMS {
        return Err(RoomsError::Conflict(format!(
            "room limit reached ({MAX_ROOMS})"
        )));
    }
    if let Some(p) = parent {
        let parent_room = load(store, p)?;
        if parent_room.parent.is_some() {
            return Err(RoomsError::Usage(
                "virtual rooms cannot be nested further".into(),
            ));
        }
    }
    if let Some(e) = event_ref {
        need_uuid("event id", e)?;
        if store
            .get(&format!("event.{e}"))
            .map_err(RoomsError::Store)?
            .is_some()
        {
            return Err(RoomsError::Conflict("that event already has a room".into()));
        }
    }
    let out = tools
        .invoke(
            "room.create",
            &json!({"label": label}),
            Some(&idem_key(msg, "room.create")),
        )
        .map_err(tool_err)?;
    let id = whitelist_room(&out)?;
    let room = Room {
        id: id.clone(),
        kind,
        label: label.to_string(),
        parent: parent.map(str::to_string),
        event_ref: event_ref.map(str::to_string),
        allow_guests: false,
        schedule: None,
        closed: false,
        created_by: actor.to_string(),
    };
    save(store, &room)?;
    idx.push(id.clone());
    put_json(store, INDEX_KEY, &idx)?;
    if let Some(e) = event_ref {
        store
            .set(&format!("event.{e}"), id.as_bytes())
            .map_err(RoomsError::Store)?;
    }
    Ok(room)
}

fn close_room(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    id: &str,
) -> Result<Room, RoomsError> {
    let mut room = load(store, id)?;
    if room.closed {
        return Ok(room);
    }
    // Children first so a closed parent never leaves joinable virtual rooms.
    for child in list_rooms(store)?
        .into_iter()
        .filter(|r| r.parent.as_deref() == Some(id))
    {
        close_room(tools, store, msg, &child.id)?;
    }
    tools
        .invoke(
            "room.close",
            &json!({"room_id": id}),
            Some(&idem_key(&format!("{msg}|{id}"), "room.close")),
        )
        .map_err(tool_err)?;
    room.closed = true;
    save(store, &room)?;
    Ok(room)
}

/// Mints join credentials for `actor` (role from the ACL unless `guest`).
fn join(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    id: &str,
    actor: &str,
    guest: bool,
    now: i64,
) -> Result<Value, RoomsError> {
    let room = load(store, id)?;
    let role = if guest {
        Role::Guest
    } else {
        role_of(store, id, actor)?
    };
    let (publish, subscribe) = grants_for(&room, role, now)?;
    let mut out =
        json!({"room_id": room.id, "role": role, "publish": publish, "subscribe": subscribe});
    let key = |op: &str| idem_key(&format!("{msg}|{id}|{actor}"), op);
    let args = json!({"room_id": id, "participant_id": actor});
    if publish {
        let e = tools
            .invoke("whip.endpoint", &args, Some(&key("whip")))
            .map_err(tool_err)?;
        out["publish_endpoint"] = pick_endpoint(&e)?;
    }
    if subscribe {
        let e = tools
            .invoke("whep.endpoint", &args, Some(&key("whep")))
            .map_err(tool_err)?;
        out["subscribe_endpoint"] = pick_endpoint(&e)?;
    }
    Ok(out)
}

fn pick_endpoint(v: &Value) -> Result<Value, RoomsError> {
    let get = |k: &str| {
        v.get(k)
            .cloned()
            .ok_or(RoomsError::Tool(ToolFail::Unavailable))
    };
    Ok(
        json!({"url": get("url")?, "token": get("token")?, "token_id": get("token_id")?, "expires_at": get("expires_at")?}),
    )
}

// ------------------------------------------------------------ admin actions

/// Admin operations for a host-delivered `admin.action` event.
///
/// Nothing here trusts the event: `actor` must be the HOST-VERIFIED actor
/// (`identity.resolve_actor`), `args.actor` is rejected outright, and every op
/// except `join_token` first passes a host-enforced `community:admin` check (the
/// `admin.verify` tool carries `required_scope: community:admin`). Credentials
/// are never produced on this path -- the result is emitted as an event, and
/// events are queued and logged -- so `join_token` is refused here.
pub fn admin_for_event(
    op: &str,
    args: &Value,
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    now: i64,
    actor: &str,
) -> Result<Value, RoomsError> {
    admin_inner(op, args, tools, store, msg, now, actor, false)
}

/// Shared implementation. `allow_credentials` is `true` only for a direct,
/// non-event result channel (not yet provided by the host).
#[allow(clippy::too_many_arguments)]
fn admin_inner(
    op: &str,
    args: &Value,
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    now: i64,
    actor: &str,
    allow_credentials: bool,
) -> Result<Value, RoomsError> {
    if args.get("actor").is_some() {
        return Err(RoomsError::Denied(
            "actor is derived by the host and is not accepted as an argument".into(),
        ));
    }
    need_uuid("verified actor", actor)?;
    if op == "join_token" {
        if !allow_credentials {
            return Err(RoomsError::Denied(
                "join credentials are not returned over events".into(),
            ));
        }
    } else {
        // Host-enforced: refused unless the verified actor holds community:admin.
        tools
            .invoke("admin.verify", &json!({}), None)
            .map_err(tool_err)?;
    }
    let s = |k: &str| args.get(k).and_then(Value::as_str);
    let req = |k: &str| s(k).ok_or_else(|| RoomsError::Usage(format!("{k} is required")));
    match op {
        "room.list" => {
            Ok(json!({"rooms": list_rooms(store)?.iter().map(view).collect::<Vec<_>>()}))
        }
        "room.get" => Ok(view(&load(store, req("room_id")?)?)),
        "room.create" => {
            let kind = match s("kind").unwrap_or("community") {
                "community" => Kind::Community,
                "event" => Kind::Event,
                other => {
                    return Err(RoomsError::Usage(format!(
                        "kind {other:?} is not creatable here; use room.virtual"
                    )));
                }
            };
            let event = s("event_id");
            if kind == Kind::Event && event.is_none() {
                return Err(RoomsError::Usage("event rooms need event_id".into()));
            }
            let r = create_room(tools, store, msg, actor, req("label")?, kind, None, event)?;
            Ok(view(&r))
        }
        "room.virtual" => {
            let kind = parse_virtual(req("kind")?)?;
            let r = create_room(
                tools,
                store,
                msg,
                actor,
                req("label")?,
                kind,
                Some(req("parent")?),
                None,
            )?;
            Ok(view(&r))
        }
        "room.close" => Ok(view(&close_room(tools, store, msg, req("room_id")?)?)),
        "room.link_event" => {
            let (id, ev) = (req("room_id")?, req("event_id")?);
            need_uuid("event id", ev)?;
            let mut r = load(store, id)?;
            let ek = format!("event.{ev}");
            if let Some(existing) = store.get(&ek).map_err(RoomsError::Store)?
                && existing != id.as_bytes()
            {
                return Err(RoomsError::Conflict("that event already has a room".into()));
            }
            if let Some(old) = r.event_ref.take() {
                store
                    .delete(&format!("event.{old}"))
                    .map_err(RoomsError::Store)?;
            }
            store.set(&ek, id.as_bytes()).map_err(RoomsError::Store)?;
            r.event_ref = Some(ev.to_string());
            save(store, &r)?;
            Ok(view(&r))
        }
        "room.unlink_event" => {
            let mut r = load(store, req("room_id")?)?;
            if let Some(old) = r.event_ref.take() {
                store
                    .delete(&format!("event.{old}"))
                    .map_err(RoomsError::Store)?;
            }
            save(store, &r)?;
            Ok(view(&r))
        }
        "room.set_access" => {
            let mut r = load(store, req("room_id")?)?;
            r.allow_guests = args
                .get("allow_guests")
                .and_then(Value::as_bool)
                .ok_or_else(|| RoomsError::Usage("allow_guests (bool) is required".into()))?;
            save(store, &r)?;
            Ok(view(&r))
        }
        "room.set_schedule" => {
            let mut r = load(store, req("room_id")?)?;
            r.schedule = match (args.get("open_at"), args.get("close_at")) {
                (Some(Value::Null) | None, Some(Value::Null) | None) => None,
                (Some(o), Some(c)) => {
                    let (o, c) = (
                        o.as_i64().ok_or_else(|| {
                            RoomsError::Usage("open_at must be unix seconds".into())
                        })?,
                        c.as_i64().ok_or_else(|| {
                            RoomsError::Usage("close_at must be unix seconds".into())
                        })?,
                    );
                    if o >= c || o < 0 {
                        return Err(RoomsError::Usage("need 0 <= open_at < close_at".into()));
                    }
                    Some(Schedule {
                        open_at: o,
                        close_at: c,
                    })
                }
                _ => {
                    return Err(RoomsError::Usage(
                        "give both open_at and close_at, or neither".into(),
                    ));
                }
            };
            save(store, &r)?;
            Ok(view(&r))
        }
        "acl.set" => {
            let (id, who) = (req("room_id")?, req("member")?);
            need_uuid("member", who)?;
            load(store, id)?;
            let role: Role = serde_json::from_value(
                args.get("role").cloned().unwrap_or(Value::Null),
            )
            .map_err(|_| RoomsError::Usage("role must be guest|member|moderator|admin".into()))?;
            put_json(store, &acl_key(id, who), &role)?;
            Ok(json!({"room_id": id, "member": who, "role": role}))
        }
        "acl.remove" => {
            let (id, who) = (req("room_id")?, req("member")?);
            need_uuid("member", who)?;
            load(store, id)?;
            store.delete(&acl_key(id, who)).map_err(RoomsError::Store)?;
            Ok(json!({"room_id": id, "member": who, "role": Role::Member}))
        }
        "join_token" => {
            let guest = args.get("guest").and_then(Value::as_bool).unwrap_or(false);
            join(tools, store, msg, req("room_id")?, actor, guest, now)
        }
        other => Err(RoomsError::Usage(format!(
            "unknown admin op {:?}",
            other.chars().take(32).collect::<String>()
        ))),
    }
}

fn parse_virtual(k: &str) -> Result<Kind, RoomsError> {
    match k {
        "breakout" => Ok(Kind::Breakout),
        "stage" => Ok(Kind::Stage),
        "green_room" | "green-room" | "greenroom" => Ok(Kind::GreenRoom),
        _ => Err(RoomsError::Usage(
            "virtual kind is breakout|stage|green_room".into(),
        )),
    }
}

/// Public room view (no actor ids beyond the creator's opaque UUID).
fn view(r: &Room) -> Value {
    json!({"room_id": r.id, "kind": r.kind, "label": r.label, "parent": r.parent,
           "event_ref": r.event_ref, "allow_guests": r.allow_guests, "schedule": r.schedule,
           "closed": r.closed})
}

// ------------------------------------------------------------- chat commands

/// Usage text for `!room`.
pub const USAGE: &str = "Usage: !room [list] | info <room> | join [<room>] | create <label> [--event <event-uuid>] | \
close <room> | virtual <parent-room> <breakout|stage|green_room> <label>";

/// Handles one chat message; `None` when it is not a rooms command. The reply is
/// plain text and never contains a credential.
pub fn handle_chat(
    text: &str,
    msg: &str,
    tools: &dyn Tools,
    store: &dyn Store,
    ident: &dyn Ident,
    join_base_url: Option<&str>,
) -> Option<String> {
    let mut it = text.split_whitespace();
    let head = it.next()?.to_ascii_lowercase();
    if head != "!room" && head != "!rooms" {
        return None;
    }
    let args: Vec<&str> = it.collect();
    Some(
        match run_chat(&args, msg, tools, store, ident, join_base_url) {
            Ok(r) => r,
            Err(e) => user_message(&e),
        },
    )
}

fn run_chat(
    args: &[&str],
    msg: &str,
    tools: &dyn Tools,
    store: &dyn Store,
    ident: &dyn Ident,
    join_base_url: Option<&str>,
) -> Result<String, RoomsError> {
    let verb = args
        .first()
        .map(|v| v.to_ascii_lowercase())
        .unwrap_or_else(|| "list".into());
    let rest = if args.is_empty() { &[][..] } else { &args[1..] };
    match verb.as_str() {
        "help" | "--help" | "-h" => Ok(USAGE.to_string()),
        "list" => {
            let rooms: Vec<Room> = list_rooms(store)?
                .into_iter()
                .filter(|r| !r.closed && r.parent.is_none())
                .collect();
            if rooms.is_empty() {
                return Ok("No open rooms.".into());
            }
            Ok(format!(
                "Rooms: {}",
                rooms
                    .iter()
                    .map(|r| format!("{} [{}]", r.label, r.id))
                    .collect::<Vec<_>>()
                    .join(", ")
            ))
        }
        "info" => {
            let r = load(store, one(rest, "info <room>")?)?;
            let kids = list_rooms(store)?
                .into_iter()
                .filter(|c| c.parent.as_deref() == Some(&r.id) && !c.closed)
                .count();
            Ok(format!(
                "{} [{}]: {:?}{}{} , {} open virtual room(s)",
                r.label,
                r.id,
                r.kind,
                if r.closed { ", closed" } else { "" },
                if r.event_ref.is_some() {
                    ", linked to an event"
                } else {
                    ""
                },
                kids
            ))
        }
        "join" => {
            let id = match rest {
                [id] => (*id).to_string(),
                [] => {
                    let open: Vec<Room> = list_rooms(store)?
                        .into_iter()
                        .filter(|r| !r.closed && r.parent.is_none())
                        .collect();
                    match open.as_slice() {
                        [only] => only.id.clone(),
                        [] => return Err(RoomsError::NotFound),
                        _ => {
                            return Err(RoomsError::Usage(
                                "several rooms are open; name one: !room join <room>".into(),
                            ));
                        }
                    }
                }
                _ => return Err(RoomsError::Usage(USAGE.into())),
            };
            let actor = ident
                .actor()
                .map_err(|_| RoomsError::Denied("could not verify you as a member".into()))?;
            // Access is checked now so the user hears "no" in chat; the credential
            // itself is minted by the authenticated gateway, never posted here.
            let room = load(store, &id)?;
            let role = role_of(store, &id, &actor)?;
            grants_for(&room, role, 0).or_else(|e| match e {
                RoomsError::Denied(m) if m.contains("scheduled") => Ok((false, false)),
                other => Err(other),
            })?;
            Ok(match join_base_url {
                Some(base) => format!(
                    "Join \"{}\" here (sign in required): {}/{}",
                    room.label,
                    base.trim_end_matches('/'),
                    room.id
                ),
                None => format!(
                    "You may join \"{}\"; open it from the web app to get your join link.",
                    room.label
                ),
            })
        }
        "create" => {
            let (event, label) = parse_create(rest)?;
            let actor = ident
                .actor()
                .map_err(|_| RoomsError::Denied("could not verify you".into()))?;
            let kind = if event.is_some() {
                Kind::Event
            } else {
                Kind::Community
            };
            let r = create_room(
                tools,
                store,
                msg,
                &actor,
                &label,
                kind,
                None,
                event.as_deref(),
            )?;
            Ok(format!("Created room \"{}\" [{}].", r.label, r.id))
        }
        "close" => {
            let r = close_room(tools, store, msg, one(rest, "close <room>")?)?;
            Ok(format!("Closed room \"{}\".", r.label))
        }
        "virtual" => match rest {
            [parent, kind, label @ ..] if !label.is_empty() => {
                let actor = ident
                    .actor()
                    .map_err(|_| RoomsError::Denied("could not verify you".into()))?;
                let r = create_room(
                    tools,
                    store,
                    msg,
                    &actor,
                    &label.join(" "),
                    parse_virtual(kind)?,
                    Some(parent),
                    None,
                )?;
                Ok(format!(
                    "Created {:?} room \"{}\" [{}].",
                    r.kind, r.label, r.id
                ))
            }
            _ => Err(RoomsError::Usage(USAGE.into())),
        },
        _ => Err(RoomsError::Usage(USAGE.into())),
    }
}

fn one<'a>(rest: &[&'a str], usage: &str) -> Result<&'a str, RoomsError> {
    match rest {
        [x] => Ok(x),
        _ => Err(RoomsError::Usage(format!("Usage: !room {usage}"))),
    }
}

fn parse_create(rest: &[&str]) -> Result<(Option<String>, String), RoomsError> {
    let mut event = None;
    let mut label = Vec::new();
    let mut i = 0;
    while i < rest.len() {
        if rest[i] == "--event" {
            let v = rest
                .get(i + 1)
                .ok_or_else(|| RoomsError::Usage("--event needs an event UUID".into()))?;
            need_uuid("event id", v)?;
            event = Some((*v).to_string());
            i += 2;
        } else {
            label.push(rest[i]);
            i += 1;
        }
    }
    Ok((event, label.join(" ")))
}

/// Closed user-facing text per failure -- never host detail.
pub fn user_message(e: &RoomsError) -> String {
    match e {
        RoomsError::Usage(m) => m.clone(),
        RoomsError::NotFound => "No such room.".into(),
        RoomsError::Denied(m) => format!("Not allowed: {m}."),
        RoomsError::Conflict(m) => format!("Conflict: {m}."),
        RoomsError::Tool(ToolFail::NotGranted | ToolFail::Forbidden) => {
            "Rooms request refused (admin permission required).".into()
        }
        RoomsError::Tool(ToolFail::InvalidArgs(m)) => format!("Rooms request refused ({m})."),
        RoomsError::Tool(ToolFail::Unavailable) => {
            "The rooms service is unavailable right now.".into()
        }
        RoomsError::Store(_) => "Rooms storage error; try again later.".into(),
    }
}

/// Ordered ACL snapshot for tests.
#[cfg(test)]
fn acl_snapshot(
    store: &dyn Store,
    room: &str,
    members: &[&str],
) -> Result<BTreeMap<String, Role>, RoomsError> {
    let mut m = BTreeMap::new();
    for who in members {
        m.insert((*who).to_string(), role_of(store, room, who)?);
    }
    Ok(m)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::collections::HashMap;

    const ADMIN: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    const USER: &str = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
    const EVENT: &str = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee";
    const EVENT2: &str = "ffffffff-ffff-ffff-ffff-ffffffffffff";

    #[derive(Default)]
    struct Kv(RefCell<HashMap<String, Vec<u8>>>);
    impl Store for Kv {
        fn get(&self, k: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(self.0.borrow().get(k).cloned())
        }
        fn set(&self, k: &str, v: &[u8]) -> Result<(), String> {
            assert!(!k.contains(':'), "kv keys must not contain ':'");
            self.0.borrow_mut().insert(k.into(), v.to_vec());
            Ok(())
        }
        fn delete(&self, k: &str) -> Result<(), String> {
            self.0.borrow_mut().remove(k);
            Ok(())
        }
    }

    /// Fake webrtc service: mints sequential room ids, records every call.
    #[derive(Default)]
    struct Svc {
        calls: RefCell<Vec<(String, Value, Option<String>)>>,
        n: RefCell<u32>,
        fail: RefCell<Option<ToolFail>>,
        deny_admin: RefCell<bool>,
    }
    impl Tools for Svc {
        fn invoke(&self, tool: &str, args: &Value, idem: Option<&str>) -> Result<Value, ToolFail> {
            self.calls
                .borrow_mut()
                .push((tool.into(), args.clone(), idem.map(str::to_string)));
            if tool == "admin.verify" {
                return if *self.deny_admin.borrow() {
                    Err(ToolFail::Forbidden)
                } else {
                    Ok(json!({"verified": true}))
                };
            }
            if let Some(f) = self.fail.borrow().clone() {
                return Err(f);
            }
            match tool {
                "room.create" => {
                    *self.n.borrow_mut() += 1;
                    Ok(
                        json!({"room_id": format!("00000000-0000-0000-0000-{:012}", *self.n.borrow())}),
                    )
                }
                "room.close" => Ok(json!({"room_id": args["room_id"]})),
                "whip.endpoint" | "whep.endpoint" => Ok(
                    json!({"url": format!("https://m/{tool}"), "token": "T", "token_id": USER, "expires_at": 9, "extra": 1}),
                ),
                other => panic!("unexpected tool {other}"),
            }
        }
    }
    struct Who(&'static str);
    impl Ident for Who {
        fn actor(&self) -> Result<String, String> {
            Ok(self.0.to_string())
        }
    }
    fn admin(
        op: &str,
        args: &Value,
        t: &dyn Tools,
        k: &dyn Store,
        msg: &str,
        now: i64,
    ) -> Result<Value, RoomsError> {
        admin_inner(op, args, t, k, msg, now, ADMIN, true)
    }
    fn admin_as(
        who: &str,
        op: &str,
        args: &Value,
        t: &dyn Tools,
        k: &dyn Store,
        msg: &str,
        now: i64,
    ) -> Result<Value, RoomsError> {
        admin_inner(op, args, t, k, msg, now, who, true)
    }
    fn chat(t: &str, svc: &Svc, kv: &Kv, who: &'static str) -> String {
        handle_chat(t, "m1", svc, kv, &Who(who), Some("https://app/rooms")).unwrap()
    }

    #[test]
    fn non_room_text_is_ignored() {
        let (s, k) = (Svc::default(), Kv::default());
        assert!(handle_chat("hello", "m", &s, &k, &Who(USER), None).is_none());
        assert!(handle_chat("!roomba", "m", &s, &k, &Who(USER), None).is_none());
        assert!(s.calls.borrow().is_empty());
    }

    #[test]
    fn create_list_info_close_roundtrip_and_replay_keys() {
        let (s, k) = (Svc::default(), Kv::default());
        assert_eq!(chat("!room", &s, &k, ADMIN), "No open rooms.");
        let r = chat("!room create Game Night", &s, &k, ADMIN);
        assert!(
            r.contains("Game Night") && r.contains("00000000-0000-0000-0000-000000000001"),
            "{r}"
        );
        assert!(chat("!room list", &s, &k, USER).contains("Game Night"));
        let (tool, args, idem) = s.calls.borrow()[0].clone();
        assert_eq!(
            (tool.as_str(), args),
            ("room.create", json!({"label": "Game Night"}))
        );
        assert_eq!(
            idem.unwrap(),
            idem_key("m1", "room.create"),
            "replay-safe key from the message id"
        );
        assert!(
            chat(
                "!room info 00000000-0000-0000-0000-000000000001",
                &s,
                &k,
                USER
            )
            .contains("Community")
        );
        chat(
            "!room close 00000000-0000-0000-0000-000000000001",
            &s,
            &k,
            ADMIN,
        );
        assert_eq!(chat("!room list", &s, &k, USER), "No open rooms.");
    }

    #[test]
    fn host_refusal_is_a_permission_message_not_a_state_change() {
        let (s, k) = (Svc::default(), Kv::default());
        *s.fail.borrow_mut() = Some(ToolFail::Forbidden);
        let r = chat("!room create Nope", &s, &k, USER);
        assert!(r.contains("admin permission required"), "{r}");
        assert!(
            list_rooms(&k).unwrap().is_empty(),
            "no room recorded when the host refused"
        );
    }

    #[test]
    fn event_rooms_link_by_uuid_and_conflict() {
        let (s, k) = (Svc::default(), Kv::default());
        let r = chat(
            &format!("!room create Watch Party --event {EVENT}"),
            &s,
            &k,
            ADMIN,
        );
        assert!(r.contains("Created"), "{r}");
        let again = chat(
            &format!("!room create Other --event {EVENT}"),
            &s,
            &k,
            ADMIN,
        );
        assert!(again.contains("already has a room"), "{again}");
        assert!(chat("!room create X --event not-a-uuid", &s, &k, ADMIN).contains("UUID"));
        // admin relink moves the link and frees the old event
        let id = "00000000-0000-0000-0000-000000000001";
        admin(
            "room.link_event",
            &json!({"room_id": id, "event_id": EVENT2}),
            &s,
            &k,
            "m",
            0,
        )
        .unwrap();
        assert!(k.get(&format!("event.{EVENT}")).unwrap().is_none());
        assert_eq!(
            k.get(&format!("event.{EVENT2}")).unwrap().unwrap(),
            id.as_bytes()
        );
        admin("room.unlink_event", &json!({"room_id": id}), &s, &k, "m", 0).unwrap();
        assert!(k.get(&format!("event.{EVENT2}")).unwrap().is_none());
    }

    #[test]
    fn virtual_rooms_nest_one_level_and_close_with_parent() {
        let (s, k) = (Svc::default(), Kv::default());
        chat("!room create Summit", &s, &k, ADMIN);
        let p = "00000000-0000-0000-0000-000000000001";
        let b = chat(
            &format!("!room virtual {p} breakout Side Chat"),
            &s,
            &k,
            ADMIN,
        );
        assert!(b.contains("Breakout"), "{b}");
        let c = "00000000-0000-0000-0000-000000000002";
        assert!(chat(&format!("!room virtual {c} stage Deeper"), &s, &k, ADMIN).contains("nested"));
        assert!(
            chat(&format!("!room virtual {p} lounge X"), &s, &k, ADMIN)
                .contains("breakout|stage|green_room")
        );
        chat(&format!("!room close {p}"), &s, &k, ADMIN);
        assert!(load(&k, c).unwrap().closed, "child closed with parent");
        let closes = s
            .calls
            .borrow()
            .iter()
            .filter(|c| c.0 == "room.close")
            .count();
        assert_eq!(closes, 2);
    }

    fn room(kind: Kind) -> Room {
        Room {
            id: "x".into(),
            kind,
            label: "r".into(),
            parent: None,
            event_ref: None,
            allow_guests: false,
            schedule: None,
            closed: false,
            created_by: ADMIN.into(),
        }
    }

    #[test]
    fn grants_follow_role_kind_guest_schedule_and_closed() {
        assert_eq!(
            grants_for(&room(Kind::Community), Role::Member, 0).unwrap(),
            (true, true)
        );
        assert!(
            grants_for(&room(Kind::Community), Role::Guest, 0).is_err(),
            "guests off by default"
        );
        let mut g = room(Kind::Community);
        g.allow_guests = true;
        assert_eq!(
            grants_for(&g, Role::Guest, 0).unwrap(),
            (false, true),
            "guests only watch"
        );
        assert_eq!(
            grants_for(&room(Kind::Stage), Role::Member, 0).unwrap(),
            (false, true)
        );
        assert_eq!(
            grants_for(&room(Kind::Stage), Role::Moderator, 0).unwrap(),
            (true, true)
        );
        assert!(grants_for(&room(Kind::GreenRoom), Role::Member, 0).is_err());
        assert_eq!(
            grants_for(&room(Kind::GreenRoom), Role::Admin, 0).unwrap(),
            (true, true)
        );
        let mut sc = room(Kind::Community);
        sc.schedule = Some(Schedule {
            open_at: 100,
            close_at: 200,
        });
        assert!(grants_for(&sc, Role::Member, 99).is_err());
        assert!(grants_for(&sc, Role::Member, 200).is_err());
        assert!(grants_for(&sc, Role::Member, 150).is_ok());
        assert!(
            grants_for(&sc, Role::Moderator, 99).is_ok(),
            "staff bypass the window"
        );
        let mut cl = room(Kind::Community);
        cl.closed = true;
        assert!(grants_for(&cl, Role::Admin, 0).is_err());
    }

    #[test]
    fn join_token_mints_only_granted_endpoints_with_idempotency() {
        let (s, k) = (Svc::default(), Kv::default());
        chat("!room create Hall", &s, &k, ADMIN);
        let id = "00000000-0000-0000-0000-000000000001";
        admin(
            "room.virtual",
            &json!({"label": "Main", "kind": "stage", "parent": id}),
            &s,
            &k,
            "m2",
            0,
        )
        .unwrap();
        let stage = "00000000-0000-0000-0000-000000000002";
        s.calls.borrow_mut().clear();
        let j = admin_as(
            USER,
            "join_token",
            &json!({"room_id": stage}),
            &s,
            &k,
            "m3",
            0,
        )
        .unwrap();
        assert_eq!(j["publish"], false);
        assert!(j.get("publish_endpoint").is_none());
        assert_eq!(j["subscribe_endpoint"]["token"], "T");
        assert!(
            j["subscribe_endpoint"].get("extra").is_none(),
            "endpoint fields are whitelisted"
        );
        assert_eq!(s.calls.borrow().len(), 1);
        // promote to moderator -> publish too
        admin(
            "acl.set",
            &json!({"room_id": stage, "member": USER, "role": "moderator"}),
            &s,
            &k,
            "m4",
            0,
        )
        .unwrap();
        let j = admin_as(
            USER,
            "join_token",
            &json!({"room_id": stage}),
            &s,
            &k,
            "m5",
            0,
        )
        .unwrap();
        assert_eq!(j["publish"], true);
        assert!(
            j["publish_endpoint"]["url"]
                .as_str()
                .unwrap()
                .contains("whip")
        );
        admin(
            "acl.remove",
            &json!({"room_id": stage, "member": USER}),
            &s,
            &k,
            "m6",
            0,
        )
        .unwrap();
        assert_eq!(
            acl_snapshot(&k, stage, &[USER]).unwrap()[USER],
            Role::Member
        );
        // closed room refuses
        admin("room.close", &json!({"room_id": stage}), &s, &k, "m7", 0).unwrap();
        assert!(matches!(
            admin_as(
                USER,
                "join_token",
                &json!({"room_id": stage}),
                &s,
                &k,
                "m8",
                0
            ),
            Err(RoomsError::Denied(_))
        ));
    }

    #[test]
    fn tokens_never_appear_in_chat_replies() {
        let (s, k) = (Svc::default(), Kv::default());
        chat("!room create Hall", &s, &k, ADMIN);
        let r = chat("!room join", &s, &k, USER);
        assert!(
            r.contains("https://app/rooms/00000000-0000-0000-0000-000000000001"),
            "{r}"
        );
        assert!(!r.contains("token"), "no credential in chat");
        assert!(
            s.calls
                .borrow()
                .iter()
                .all(|c| c.0 != "whip.endpoint" && c.0 != "whep.endpoint"),
            "chat never mints tokens"
        );
    }

    #[test]
    fn admin_ops_validate_input_loudly() {
        let (s, k) = (Svc::default(), Kv::default());
        chat("!room create Hall", &s, &k, ADMIN);
        let id = "00000000-0000-0000-0000-000000000001";
        for (op, args) in [
            ("room.get", json!({"room_id": "nope"})),
            ("room.set_access", json!({"room_id": id})),
            (
                "room.set_schedule",
                json!({"room_id": id, "open_at": 5, "close_at": 5}),
            ),
            ("room.set_schedule", json!({"room_id": id, "open_at": 5})),
            (
                "acl.set",
                json!({"room_id": id, "member": USER, "role": "owner"}),
            ),
            (
                "acl.set",
                json!({"room_id": id, "member": "x", "role": "member"}),
            ),
            ("room.create", json!({"label": "", "kind": "community"})),
            ("room.create", json!({"label": "a", "kind": "event"})),
            ("room.create", json!({"label": "a", "kind": "stage"})),
            ("bogus", json!({})),
        ] {
            assert!(
                matches!(admin(op, &args, &s, &k, "m", 0), Err(RoomsError::Usage(_))),
                "{op} {args}"
            );
        }
        assert!(matches!(
            admin(
                "room.get",
                &json!({"room_id": "99999999-9999-9999-9999-999999999999"}),
                &s,
                &k,
                "m",
                0
            ),
            Err(RoomsError::NotFound)
        ));
        let v = admin(
            "room.set_schedule",
            &json!({"room_id": id, "open_at": 1, "close_at": 9}),
            &s,
            &k,
            "m",
            0,
        )
        .unwrap();
        assert_eq!(v["schedule"]["close_at"], 9);
        let v = admin("room.set_schedule", &json!({"room_id": id}), &s, &k, "m", 0).unwrap();
        assert!(v["schedule"].is_null(), "neither bound clears the schedule");
    }

    #[test]
    fn limits_and_corruption_fail_loud() {
        let (s, k) = (Svc::default(), Kv::default());
        put_json(
            &k,
            INDEX_KEY,
            &(0..MAX_ROOMS).map(|i| format!("{i}")).collect::<Vec<_>>(),
        )
        .unwrap();
        assert!(chat("!room create More", &s, &k, ADMIN).contains("limit"));
        let k2 = Kv::default();
        k2.set("rooms.index", b"not json").unwrap();
        assert!(chat("!room list", &s, &k2, USER).contains("storage error"));
        assert!(
            chat(
                &format!("!room create {}", "x".repeat(200)),
                &s,
                &Kv::default(),
                ADMIN
            )
            .contains("label")
        );
    }

    #[test]
    fn user_messages_never_echo_host_detail() {
        assert!(!user_message(&RoomsError::Store("secret dsn".into())).contains("dsn"));
        assert!(!user_message(&RoomsError::Tool(ToolFail::Unavailable)).contains("webrtc"));
    }

    /// Contract fixture: the exact (tool, args) shapes this app sends to the webrtc
    /// service. `scripts/ci/tests/test_rooms_webrtc_contract.py` validates the same
    /// file against the webrtc manifest's input schemas, so a drift on either side
    /// fails in CI.
    #[test]
    fn consumed_tool_calls_match_the_contract_fixture() {
        let fixture: Value =
            serde_json::from_str(include_str!("../tests/contract_calls.json")).unwrap();
        let (s, k) = (Svc::default(), Kv::default());
        chat("!room create Game Night", &s, &k, ADMIN);
        let id = "00000000-0000-0000-0000-000000000001";
        admin("room.list", &json!({}), &s, &k, "m1", 0).unwrap();
        admin_as(USER, "join_token", &json!({"room_id": id}), &s, &k, "m2", 0).unwrap();
        chat(&format!("!room close {id}"), &s, &k, ADMIN);
        let mut seen: HashMap<String, Value> = HashMap::new();
        for (tool, args, _) in s.calls.borrow().iter() {
            seen.insert(tool.clone(), args.clone());
        }
        let want = fixture.as_object().unwrap();
        assert_eq!(
            seen.len(),
            want.len(),
            "tool set drifted: {:?}",
            seen.keys()
        );
        for (tool, args) in want {
            assert_eq!(seen.get(tool), Some(args), "{tool}");
        }
    }

    #[test]
    fn forged_admin_action_from_a_non_admin_is_refused_and_changes_nothing() {
        let (s, k) = (Svc::default(), Kv::default());
        chat("!room create Hall", &s, &k, ADMIN);
        let id = "00000000-0000-0000-0000-000000000001";
        // The host refuses `admin.verify` for a non-admin actor.
        *s.deny_admin.borrow_mut() = true;
        for (op, args) in [
            ("room.close", json!({"room_id": id})),
            (
                "room.set_access",
                json!({"room_id": id, "allow_guests": true}),
            ),
            (
                "acl.set",
                json!({"room_id": id, "member": USER, "role": "admin"}),
            ),
            ("room.link_event", json!({"room_id": id, "event_id": EVENT})),
            ("room.list", json!({})),
        ] {
            let r = admin_for_event(op, &args, &s, &k, "m9", 0, USER);
            assert!(
                matches!(r, Err(RoomsError::Tool(ToolFail::Forbidden))),
                "{op}: {r:?}"
            );
        }
        let room = load(&k, id).unwrap();
        assert!(!room.closed && !room.allow_guests && room.event_ref.is_none());
        assert_eq!(
            role_of(&k, id, USER).unwrap(),
            Role::Member,
            "ACL untouched"
        );
        assert!(
            s.calls.borrow().iter().all(|c| c.0 != "room.close"),
            "no mutating tool ran"
        );
    }

    #[test]
    fn actor_in_args_is_rejected_outright_even_for_a_real_admin() {
        let (s, k) = (Svc::default(), Kv::default());
        chat("!room create Hall", &s, &k, ADMIN);
        let id = "00000000-0000-0000-0000-000000000001";
        for op in ["room.create", "room.list", "acl.set", "join_token"] {
            let r = admin_for_event(
                op,
                &json!({"room_id": id, "actor": ADMIN}),
                &s,
                &k,
                "m",
                0,
                USER,
            );
            assert!(matches!(r, Err(RoomsError::Denied(_))), "{op}: {r:?}");
        }
        assert!(admin_for_event("room.list", &json!({}), &s, &k, "m", 0, "not-a-uuid").is_err());
    }

    #[test]
    fn event_path_never_returns_credentials() {
        let (s, k) = (Svc::default(), Kv::default());
        chat("!room create Hall", &s, &k, ADMIN);
        let id = "00000000-0000-0000-0000-000000000001";
        s.calls.borrow_mut().clear();
        let r = admin_for_event("join_token", &json!({"room_id": id}), &s, &k, "m", 0, USER);
        assert!(matches!(r, Err(RoomsError::Denied(m)) if m.contains("not returned over events")));
        assert!(
            s.calls
                .borrow()
                .iter()
                .all(|c| c.0 != "whip.endpoint" && c.0 != "whep.endpoint"),
            "no credential was even minted on the event path"
        );
        // Every success the event path can produce is credential-free.
        for (op, args) in [
            ("room.list", json!({})),
            ("room.get", json!({"room_id": id})),
            (
                "room.set_access",
                json!({"room_id": id, "allow_guests": true}),
            ),
            (
                "acl.set",
                json!({"room_id": id, "member": USER, "role": "moderator"}),
            ),
        ] {
            let out = admin_for_event(op, &args, &s, &k, "m", 0, ADMIN)
                .unwrap()
                .to_string();
            for needle in ["token", "endpoint", "\"url\""] {
                assert!(!out.contains(needle), "{op} result leaks {needle}: {out}");
            }
        }
    }
}
