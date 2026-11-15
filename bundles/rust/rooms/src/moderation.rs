//! Room moderation: raise-hand queue plus moderator controls.
//!
//! Ported from the retired Go `module_rtc` call features. Two halves:
//!
//! * the **hand queue** is app state -- an ordered list of participant UUIDs in
//!   the community-scoped kv (`hands.<room>`, never a `:` in a key);
//! * **moderation** (kick/leave/mute/unmute/mute-all/lock/unlock and the live
//!   participant/room lists) fronts the `webrtc` service tools, which forward to
//!   the svc-streaming moderation host API (`core/svc_streaming/docs/
//!   rooms-moderation-host-api.md`).
//!
//! AuthZ is the existing per-room ACL role model (`guest < member < moderator <
//! admin`, [`crate::logic::role_of`]); the role is also sent to the service as
//! `actor_role` for defense in depth. This is interim and migrates to the SDK
//! `caller_can` check once that lands on the release branch. Everything is keyed
//! by UUID (never a username); logs and metrics carry op/outcome labels only.
//!
//! Commands (verbs from the shared vocabulary; a bare noun reads):
//! `hand [list] <room>` | `hand add <room>` | `hand remove <room> [<member>]` |
//! `hand set ack <room> <member>` | `hand reset <room>` | `lock [<room>]` |
//! `lock enable|disable <room>` | `mute <room>` | `mute enable <room> <member|all>` |
//! `mute disable <room> <member>` | `participants [list] <room>` |
//! `participant remove <room> [<member>]` | `rooms [list]`.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::logic::{
    Ident, Role, Room, RoomsError, Store, ToolFail, Tools, idem_key, kv_json, load, need_uuid,
    put_json, role_of, save, tool_err,
};

/// Feature flag for every moderation command (defaults OFF until validated).
pub const FLAG: &str = "rooms.moderation";
/// Longest hand queue kept per room.
pub const MAX_HANDS: usize = 256;

/// One raised hand: who, and the moderator who acknowledged it (if any).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Hand {
    /// Participant UUID (never a username).
    pub member: String,
    /// Moderator UUID that acknowledged the hand.
    #[serde(default)]
    pub acked_by: Option<String>,
}

/// The kv key of a room's hand queue.
fn hands_key(room: &str) -> String {
    format!("hands.{room}")
}

/// Loads a room's ordered hand queue (absent = nobody has raised a hand).
fn hands(store: &dyn Store, room: &str) -> Result<Vec<Hand>, RoomsError> {
    Ok(kv_json::<Vec<Hand>>(store, &hands_key(room))?.unwrap_or_default())
}

/// Persists the queue; an empty queue deletes the key.
fn put_hands(store: &dyn Store, room: &str, h: &[Hand]) -> Result<(), RoomsError> {
    if h.is_empty() {
        return store.delete(&hands_key(room)).map_err(RoomsError::Store);
    }
    put_json(store, &hands_key(room), &h)
}

/// The closed metrics/log label for a chat command, or `None` when the verb is
/// not a moderation command. Never echoes user text.
pub fn op_label(args: &[&str]) -> Option<&'static str> {
    let verb = args.first()?.to_ascii_lowercase();
    let sub = args.get(1).map(|s| s.to_ascii_lowercase());
    let sub = sub.as_deref();
    Some(match (verb.as_str(), sub) {
        ("hand", Some("add")) => "hand.add",
        ("hand", Some("remove")) => "hand.remove",
        ("hand", Some("set")) => "hand.ack",
        ("hand", Some("reset")) => "hand.reset",
        ("hand", _) => "hand.list",
        ("lock", Some("enable")) => "lock.enable",
        ("lock", Some("disable")) => "lock.disable",
        ("lock", _) => "lock.read",
        ("mute", Some("enable")) => "mute.enable",
        ("mute", Some("disable")) => "mute.disable",
        ("mute", _) => "mute.read",
        ("participants", _) => "participants.list",
        ("participant", Some("remove")) => "participant.remove",
        ("participant", _) => "participant.usage",
        ("rooms", _) => "rooms.list",
        _ => return None,
    })
}

/// Runs a moderation command; `Ok(None)` when `verb` is not one of ours.
/// With the flag off every moderation verb fails loud, never silently no-ops.
pub fn run(
    verb: &str,
    rest: &[&str],
    msg: &str,
    tools: &dyn Tools,
    store: &dyn Store,
    ident: &dyn Ident,
    enabled: bool,
) -> Result<Option<String>, RoomsError> {
    let mut full = vec![verb];
    full.extend_from_slice(rest);
    if op_label(&full).is_none() {
        return Ok(None);
    }
    if !enabled {
        return Err(RoomsError::Usage(
            "Room moderation is not enabled for this community".into(),
        ));
    }
    let actor = || {
        ident
            .actor()
            .map_err(|_| RoomsError::Denied("could not verify you as a member".into()))
    };
    let out = match (verb, rest) {
        ("hand", ["add", room]) => hand_add(store, room, &actor()?)?,
        ("hand", ["remove", room]) => {
            let a = actor()?;
            hand_remove(store, room, &a, &a)?
        }
        ("hand", ["remove", room, member]) => hand_remove(store, room, &actor()?, member)?,
        ("hand", ["set", "ack", room, member]) => hand_ack(store, room, &actor()?, member)?,
        ("hand", ["reset", room]) => hand_reset(store, room, &actor()?)?,
        ("hand", ["list", room] | [room]) => hand_list(store, room, &actor()?)?,
        ("lock", ["enable", room]) => lock(tools, store, msg, room, &actor()?, true)?,
        ("lock", ["disable", room]) => lock(tools, store, msg, room, &actor()?, false)?,
        ("lock", [room]) => lock_read(store, room, &actor()?)?,
        ("mute", ["enable", room, "all"]) => mute_all(tools, store, msg, room, &actor()?)?,
        ("mute", ["enable", room, member]) => {
            mute(tools, store, msg, room, &actor()?, member, true)?
        }
        ("mute", ["disable", room, member]) => {
            mute(tools, store, msg, room, &actor()?, member, false)?
        }
        ("mute", [room]) => muted_list(tools, store, room, &actor()?)?,
        ("participants", ["list", room] | [room]) => participants(tools, store, room, &actor()?)?,
        ("participant", ["remove", room]) => {
            let a = actor()?;
            remove_participant(tools, store, msg, room, &a, &a)?
        }
        ("participant", ["remove", room, member]) => {
            remove_participant(tools, store, msg, room, &actor()?, member)?
        }
        ("rooms", [] | ["list"]) => rooms_list(tools, &actor()?)?,
        _ => return Err(RoomsError::Usage(USAGE.into())),
    };
    Ok(Some(out))
}

/// Usage text for the moderation verbs.
pub const USAGE: &str = "Usage: !room hand [list] <room> | hand add <room> | hand remove <room> [<member>] | \
hand set ack <room> <member> | hand reset <room> | lock [<room>] | lock enable|disable <room> | \
mute <room> | mute enable <room> <member|all> | mute disable <room> <member> | \
participants [list] <room> | participant remove <room> [<member>] | rooms [list]";

/// Loads `room` and checks `actor` holds at least `min` on its ACL. Closed rooms
/// refuse every moderation action. Returns the room and the actor's role.
fn authorize(
    store: &dyn Store,
    room: &str,
    actor: &str,
    min: Role,
) -> Result<(Room, Role), RoomsError> {
    need_uuid("verified actor", actor)?;
    let r = load(store, room)?;
    if r.closed {
        return Err(RoomsError::Denied("room is closed".into()));
    }
    let role = role_of(store, room, actor)?;
    if role < min {
        let need = if min >= Role::Moderator {
            "moderator role required"
        } else {
            "membership required"
        };
        return Err(RoomsError::Denied(need.into()));
    }
    Ok((r, role))
}

/// Refuses acting on a participant whose role is at or above the actor's.
fn outranks(
    store: &dyn Store,
    room: &str,
    actor_role: Role,
    target: &str,
) -> Result<(), RoomsError> {
    need_uuid("member", target)?;
    if role_of(store, room, target)? >= actor_role {
        return Err(RoomsError::Denied(
            "you cannot moderate a participant of equal or higher role".into(),
        ));
    }
    Ok(())
}

/// Raises the actor's hand (idempotent; the queue keeps arrival order).
fn hand_add(store: &dyn Store, room: &str, actor: &str) -> Result<String, RoomsError> {
    authorize(store, room, actor, Role::Member)?;
    let mut q = hands(store, room)?;
    if let Some(pos) = q.iter().position(|h| h.member == actor) {
        return Ok(format!(
            "Your hand is already raised (position {}).",
            pos + 1
        ));
    }
    if q.len() >= MAX_HANDS {
        return Err(RoomsError::Conflict("hand queue is full".into()));
    }
    q.push(Hand {
        member: actor.to_string(),
        acked_by: None,
    });
    put_hands(store, room, &q)?;
    Ok(format!("Hand raised (position {}).", q.len()))
}

/// Lowers `member`'s hand: always allowed for self, moderators for anyone.
fn hand_remove(
    store: &dyn Store,
    room: &str,
    actor: &str,
    member: &str,
) -> Result<String, RoomsError> {
    need_uuid("member", member)?;
    let min = if member == actor {
        Role::Member
    } else {
        Role::Moderator
    };
    authorize(store, room, actor, min)?;
    let mut q = hands(store, room)?;
    let before = q.len();
    q.retain(|h| h.member != member);
    if q.len() == before {
        return Ok("No raised hand to lower.".into());
    }
    put_hands(store, room, &q)?;
    Ok("Hand lowered.".into())
}

/// Moderator acknowledges a raised hand (kept in the queue, marked).
fn hand_ack(
    store: &dyn Store,
    room: &str,
    actor: &str,
    member: &str,
) -> Result<String, RoomsError> {
    need_uuid("member", member)?;
    authorize(store, room, actor, Role::Moderator)?;
    let mut q = hands(store, room)?;
    let Some(h) = q.iter_mut().find(|h| h.member == member) else {
        return Err(RoomsError::NotFound);
    };
    h.acked_by = Some(actor.to_string());
    put_hands(store, room, &q)?;
    Ok("Hand acknowledged.".into())
}

/// Moderator clears the whole queue.
fn hand_reset(store: &dyn Store, room: &str, actor: &str) -> Result<String, RoomsError> {
    authorize(store, room, actor, Role::Moderator)?;
    put_hands(store, room, &[])?;
    Ok("All hands lowered.".into())
}

/// Lists the ordered queue (UUIDs only; `*` marks acknowledged hands).
fn hand_list(store: &dyn Store, room: &str, actor: &str) -> Result<String, RoomsError> {
    authorize(store, room, actor, Role::Member)?;
    let q = hands(store, room)?;
    if q.is_empty() {
        return Ok("No raised hands.".into());
    }
    Ok(format!(
        "Raised hands: {}",
        q.iter()
            .enumerate()
            .map(|(i, h)| format!(
                "{}. {}{}",
                i + 1,
                h.member,
                if h.acked_by.is_some() { " *" } else { "" }
            ))
            .collect::<Vec<_>>()
            .join(", ")
    ))
}

/// Locks/unlocks a room: host first (it enforces join refusal), then the kv flag
/// the token policy reads, so a host failure never leaves a half-locked room.
fn lock(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    room: &str,
    actor: &str,
    on: bool,
) -> Result<String, RoomsError> {
    let (mut r, role) = authorize(store, room, actor, Role::Moderator)?;
    let op = if on { "room.lock" } else { "room.unlock" };
    tools
        .invoke(
            op,
            &json!({"room_id": room, "actor_id": actor, "actor_role": role}),
            Some(&idem_key(&format!("{msg}|{room}"), op)),
        )
        .map_err(tool_err)?;
    r.locked = on;
    save(store, &r)?;
    Ok(if on { "Room locked." } else { "Room unlocked." }.into())
}

/// Reads the lock state recorded for a room.
fn lock_read(store: &dyn Store, room: &str, actor: &str) -> Result<String, RoomsError> {
    let (r, _) = authorize(store, room, actor, Role::Member)?;
    Ok(if r.locked {
        "Room is locked."
    } else {
        "Room is unlocked."
    }
    .into())
}

/// Mutes (`on`) or unmutes one participant.
fn mute(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    room: &str,
    actor: &str,
    member: &str,
    on: bool,
) -> Result<String, RoomsError> {
    need_uuid("member", member)?;
    let (_, role) = authorize(store, room, actor, Role::Moderator)?;
    if member != actor {
        outranks(store, room, role, member)?;
    }
    let op = if on { "room.mute" } else { "room.unmute" };
    tools
        .invoke(
            op,
            &json!({"room_id": room, "participant_id": member, "actor_id": actor, "actor_role": role}),
            Some(&idem_key(&format!("{msg}|{room}|{member}"), op)),
        )
        .map_err(tool_err)?;
    Ok(if on {
        "Participant muted."
    } else {
        "Participant unmuted."
    }
    .into())
}

/// Mutes every publisher except the acting moderator.
fn mute_all(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    room: &str,
    actor: &str,
) -> Result<String, RoomsError> {
    let (_, role) = authorize(store, room, actor, Role::Moderator)?;
    let out = tools
        .invoke(
            "room.mute_all",
            &json!({"room_id": room, "actor_id": actor, "actor_role": role}),
            Some(&idem_key(&format!("{msg}|{room}"), "room.mute_all")),
        )
        .map_err(tool_err)?;
    let n = out
        .get("muted")
        .and_then(Value::as_u64)
        .ok_or(RoomsError::Tool(ToolFail::Unavailable))?;
    Ok(format!("Muted {n} participant(s)."))
}

/// Calls the read-only participant listing for a room the actor may see.
fn fetch_participants(
    tools: &dyn Tools,
    store: &dyn Store,
    room: &str,
    actor: &str,
) -> Result<Vec<Value>, RoomsError> {
    authorize(store, room, actor, Role::Member)?;
    let out = tools
        .invoke("room.participants", &json!({"room_id": room}), None)
        .map_err(tool_err)?;
    out.get("participants")
        .and_then(Value::as_array)
        .cloned()
        .ok_or(RoomsError::Tool(ToolFail::Unavailable))
}

/// Formats one participant as `uuid [pub|sub|muted]`.
fn fmt_participant(p: &Value) -> Option<String> {
    let id = p.get("participant_id")?.as_str()?;
    let flag = |k: &str, label: &'static str| {
        p.get(k)
            .and_then(Value::as_bool)
            .unwrap_or(false)
            .then_some(label)
    };
    let tags: Vec<&str> = [
        flag("publisher", "pub"),
        flag("subscriber", "sub"),
        flag("muted", "muted"),
    ]
    .into_iter()
    .flatten()
    .collect();
    Some(format!("{id} [{}]", tags.join("|")))
}

/// Lists the live participants (UUIDs and media flags only).
fn participants(
    tools: &dyn Tools,
    store: &dyn Store,
    room: &str,
    actor: &str,
) -> Result<String, RoomsError> {
    let list = fetch_participants(tools, store, room, actor)?;
    if list.is_empty() {
        return Ok("No participants.".into());
    }
    let lines: Option<Vec<String>> = list.iter().map(fmt_participant).collect();
    let lines = lines.ok_or(RoomsError::Tool(ToolFail::Unavailable))?;
    Ok(format!(
        "Participants ({}): {}",
        lines.len(),
        lines.join(", ")
    ))
}

/// Lists only the muted participants (the read form of `mute`).
fn muted_list(
    tools: &dyn Tools,
    store: &dyn Store,
    room: &str,
    actor: &str,
) -> Result<String, RoomsError> {
    let list = fetch_participants(tools, store, room, actor)?;
    let muted: Vec<String> = list
        .iter()
        .filter(|p| p.get("muted").and_then(Value::as_bool).unwrap_or(false))
        .filter_map(|p| {
            p.get("participant_id")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    if muted.is_empty() {
        return Ok("Nobody is muted.".into());
    }
    Ok(format!("Muted: {}", muted.join(", ")))
}

/// Leaves (`member == actor`) or kicks another participant; a kick also lowers
/// the removed participant's hand.
fn remove_participant(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    room: &str,
    actor: &str,
    member: &str,
) -> Result<String, RoomsError> {
    need_uuid("member", member)?;
    let selfie = member == actor;
    let (_, role) = authorize(
        store,
        room,
        actor,
        if selfie {
            Role::Member
        } else {
            Role::Moderator
        },
    )?;
    if !selfie {
        outranks(store, room, role, member)?;
    }
    let op = if selfie { "room.leave" } else { "room.kick" };
    tools
        .invoke(
            op,
            &json!({"room_id": room, "participant_id": member, "actor_id": actor, "actor_role": role}),
            Some(&idem_key(&format!("{msg}|{room}|{member}"), op)),
        )
        .map_err(tool_err)?;
    // The host already removed them; a stale hand must not outlive the session.
    let mut q = hands(store, room)?;
    let before = q.len();
    q.retain(|h| h.member != member);
    if q.len() != before {
        put_hands(store, room, &q)?;
    }
    Ok(if selfie {
        "You left the room."
    } else {
        "Participant removed."
    }
    .into())
}

/// Lists live rooms from the media plane (UUID, label, counts, lock state).
fn rooms_list(tools: &dyn Tools, actor: &str) -> Result<String, RoomsError> {
    need_uuid("verified actor", actor)?;
    let out = tools
        .invoke("room.list", &json!({}), None)
        .map_err(tool_err)?;
    let rooms = out
        .get("rooms")
        .and_then(Value::as_array)
        .ok_or(RoomsError::Tool(ToolFail::Unavailable))?;
    if rooms.is_empty() {
        return Ok("No live rooms.".into());
    }
    let lines: Vec<String> = rooms
        .iter()
        .filter_map(|r| {
            let id = r.get("room_id")?.as_str()?;
            let subs = r
                .get("subscriber_count")
                .and_then(Value::as_u64)
                .unwrap_or(0);
            let locked = r.get("locked").and_then(Value::as_bool).unwrap_or(false);
            Some(format!(
                "{id} ({subs} watching{})",
                if locked { ", locked" } else { "" }
            ))
        })
        .collect();
    Ok(format!("Live rooms: {}", lines.join(", ")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::logic::{Kind, grants_for, handle_chat_ex};
    use std::cell::RefCell;
    use std::collections::HashMap;

    const ROOM: &str = "00000000-0000-0000-0000-000000000001";
    const ADMIN: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    const MOD: &str = "cccccccc-cccc-cccc-cccc-cccccccccccc";
    const USER: &str = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
    const USER2: &str = "dddddddd-dddd-dddd-dddd-dddddddddddd";

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

    #[derive(Default)]
    struct Svc {
        calls: RefCell<Vec<(String, Value)>>,
        fail: RefCell<Option<ToolFail>>,
    }
    impl Tools for Svc {
        fn invoke(&self, tool: &str, args: &Value, _: Option<&str>) -> Result<Value, ToolFail> {
            self.calls.borrow_mut().push((tool.into(), args.clone()));
            if let Some(f) = self.fail.borrow().clone() {
                return Err(f);
            }
            Ok(match tool {
                "room.participants" => json!({"participants": [
                    {"participant_id": USER, "publisher": true, "subscriber": true, "muted": true},
                    {"participant_id": USER2, "publisher": false, "subscriber": true, "muted": false}]}),
                "room.list" => {
                    json!({"rooms": [{"room_id": ROOM, "subscriber_count": 2, "locked": true}]})
                }
                "room.mute_all" => json!({"room_id": ROOM, "muted": 2}),
                _ => json!({"room_id": ROOM}),
            })
        }
    }
    struct Who(&'static str);
    impl Ident for Who {
        fn actor(&self) -> Result<String, String> {
            Ok(self.0.to_string())
        }
    }

    /// A store with one open room, ADMIN as admin, MOD as moderator.
    fn world() -> (Svc, Kv) {
        let k = Kv::default();
        let room = Room {
            id: ROOM.into(),
            kind: Kind::Community,
            label: "r".into(),
            parent: None,
            event_ref: None,
            allow_guests: false,
            schedule: None,
            closed: false,
            created_by: ADMIN.into(),
            locked: false,
        };
        save(&k, &room).unwrap();
        put_json(&k, &format!("acl.{ROOM}.{ADMIN}"), &Role::Admin).unwrap();
        put_json(&k, &format!("acl.{ROOM}.{MOD}"), &Role::Moderator).unwrap();
        (Svc::default(), k)
    }
    fn say(text: &str, who: &'static str, s: &Svc, k: &Kv) -> String {
        handle_chat_ex(text, "m1", s, k, &Who(who), None, true)
            .unwrap()
            .reply
    }

    #[test]
    fn hand_queue_keeps_arrival_order_and_is_idempotent() {
        let (s, k) = world();
        assert!(say(&format!("!room hand add {ROOM}"), USER, &s, &k).contains("position 1"));
        assert!(say(&format!("!room hand add {ROOM}"), USER2, &s, &k).contains("position 2"));
        assert!(say(&format!("!room hand add {ROOM}"), USER, &s, &k).contains("already raised"));
        let l = say(&format!("!room hand list {ROOM}"), USER, &s, &k);
        assert!(l.find(USER).unwrap() < l.find(USER2).unwrap(), "{l}");
        assert_eq!(
            say(&format!("!room hand {ROOM}"), USER, &s, &k),
            l,
            "bare reads"
        );
        assert_eq!(
            say(&format!("!room hand remove {ROOM}"), USER, &s, &k),
            "Hand lowered."
        );
        assert!(say(&format!("!room hand remove {ROOM}"), USER, &s, &k).contains("No raised hand"));
        assert!(s.calls.borrow().is_empty(), "hands are app state only");
    }

    #[test]
    fn acknowledge_and_reset_need_a_moderator_and_members_cannot_lower_others() {
        let (s, k) = world();
        say(&format!("!room hand add {ROOM}"), USER, &s, &k);
        let ack = format!("!room hand set ack {ROOM} {USER}");
        assert!(say(&ack, USER2, &s, &k).contains("moderator role required"));
        assert_eq!(say(&ack, MOD, &s, &k), "Hand acknowledged.");
        assert!(say(&format!("!room hand {ROOM}"), USER, &s, &k).ends_with(" *"));
        assert!(
            say(&format!("!room hand remove {ROOM} {USER}"), USER2, &s, &k).contains("moderator")
        );
        assert_eq!(
            say(&format!("!room hand remove {ROOM} {USER}"), MOD, &s, &k),
            "Hand lowered."
        );
        say(&format!("!room hand add {ROOM}"), USER, &s, &k);
        assert!(say(&format!("!room hand reset {ROOM}"), USER, &s, &k).contains("moderator"));
        say(&format!("!room hand reset {ROOM}"), MOD, &s, &k);
        assert_eq!(
            say(&format!("!room hand {ROOM}"), MOD, &s, &k),
            "No raised hands."
        );
        assert!(
            !k.0.borrow().contains_key(&hands_key(ROOM)),
            "empty queue deletes the key"
        );
        assert!(say(&format!("!room hand set ack {ROOM} {USER}"), MOD, &s, &k).contains("No such"));
    }

    #[test]
    fn guests_cannot_raise_hands_and_closed_rooms_refuse_everything() {
        let (s, k) = world();
        put_json(&k, &format!("acl.{ROOM}.{USER}"), &Role::Guest).unwrap();
        assert!(say(&format!("!room hand add {ROOM}"), USER, &s, &k).contains("membership"));
        let mut r = load(&k, ROOM).unwrap();
        r.closed = true;
        save(&k, &r).unwrap();
        for c in [
            "hand add",
            "lock enable",
            "participant remove",
            "participants list",
        ] {
            let out = say(&format!("!room {c} {ROOM}"), ADMIN, &s, &k);
            assert!(out.contains("room is closed"), "{c}: {out}");
        }
        assert!(s.calls.borrow().is_empty());
    }

    #[test]
    fn non_moderators_are_refused_before_any_host_call() {
        let (s, k) = world();
        for c in [
            format!("!room mute enable {ROOM} {USER2}"),
            format!("!room mute disable {ROOM} {USER2}"),
            format!("!room mute enable {ROOM} all"),
            format!("!room participant remove {ROOM} {USER2}"),
            format!("!room lock enable {ROOM}"),
            format!("!room lock disable {ROOM}"),
        ] {
            let out = say(&c, USER, &s, &k);
            assert!(
                out.contains("Not allowed: moderator role required"),
                "{c}: {out}"
            );
        }
        assert!(
            s.calls.borrow().is_empty(),
            "regression: no tool call without the role"
        );
        assert!(!load(&k, ROOM).unwrap().locked);
    }

    #[test]
    fn moderators_forward_actor_and_role_and_cannot_outrank_peers() {
        let (s, k) = world();
        assert_eq!(
            say(
                &format!("!room participant remove {ROOM} {USER}"),
                MOD,
                &s,
                &k
            ),
            "Participant removed."
        );
        assert_eq!(
            s.calls.borrow()[0],
            (
                "room.kick".to_string(),
                json!({"room_id": ROOM, "participant_id": USER, "actor_id": MOD, "actor_role": "moderator"})
            )
        );
        let n = s.calls.borrow().len();
        let out = say(
            &format!("!room participant remove {ROOM} {ADMIN}"),
            MOD,
            &s,
            &k,
        );
        assert!(out.contains("equal or higher role"), "{out}");
        let out = say(&format!("!room mute enable {ROOM} {ADMIN}"), MOD, &s, &k);
        assert!(out.contains("equal or higher role"), "{out}");
        assert_eq!(s.calls.borrow().len(), n, "refusals never reach the host");
        assert_eq!(
            say(&format!("!room mute enable {ROOM} all"), MOD, &s, &k),
            "Muted 2 participant(s)."
        );
        assert_eq!(s.calls.borrow().last().unwrap().0, "room.mute_all");
        assert!(say(&format!("!room mute disable {ROOM} {USER}"), MOD, &s, &k).contains("unmuted"));
    }

    #[test]
    fn leave_is_self_service_and_kick_lowers_the_hand() {
        let (s, k) = world();
        say(&format!("!room hand add {ROOM}"), USER, &s, &k);
        say(&format!("!room hand add {ROOM}"), USER2, &s, &k);
        assert_eq!(
            say(&format!("!room participant remove {ROOM}"), USER, &s, &k),
            "You left the room."
        );
        assert_eq!(s.calls.borrow()[0].0, "room.leave");
        say(
            &format!("!room participant remove {ROOM} {USER2}"),
            MOD,
            &s,
            &k,
        );
        assert_eq!(
            say(&format!("!room hand {ROOM}"), MOD, &s, &k),
            "No raised hands."
        );
    }

    #[test]
    fn lock_updates_state_only_after_the_host_agrees_and_gates_joins() {
        let (s, k) = world();
        *s.fail.borrow_mut() = Some(ToolFail::Unavailable);
        assert!(say(&format!("!room lock enable {ROOM}"), MOD, &s, &k).contains("unavailable"));
        assert!(
            !load(&k, ROOM).unwrap().locked,
            "host failure leaves it unlocked"
        );
        *s.fail.borrow_mut() = None;
        assert_eq!(
            say(&format!("!room lock enable {ROOM}"), MOD, &s, &k),
            "Room locked."
        );
        assert_eq!(
            say(&format!("!room lock {ROOM}"), USER, &s, &k),
            "Room is locked."
        );
        let r = load(&k, ROOM).unwrap();
        assert!(matches!(
            grants_for(&r, Role::Member, 0),
            Err(RoomsError::Denied(m)) if m == "room is locked"
        ));
        assert!(
            grants_for(&r, Role::Moderator, 0).is_ok(),
            "staff may still join"
        );
        assert_eq!(
            say(&format!("!room lock disable {ROOM}"), MOD, &s, &k),
            "Room unlocked."
        );
        assert!(grants_for(&load(&k, ROOM).unwrap(), Role::Member, 0).is_ok());
    }

    #[test]
    fn read_forms_list_participants_muted_and_live_rooms() {
        let (s, k) = world();
        let p = say(&format!("!room participants {ROOM}"), USER, &s, &k);
        assert!(
            p.contains("Participants (2)") && p.contains("[pub|sub|muted]"),
            "{p}"
        );
        assert_eq!(
            say(&format!("!room mute {ROOM}"), USER, &s, &k),
            format!("Muted: {USER}")
        );
        let r = say("!room rooms list", USER, &s, &k);
        assert!(r.contains("2 watching, locked"), "{r}");
        assert_eq!(say("!room rooms", USER, &s, &k), r);
    }

    #[test]
    fn flag_off_fails_loud_and_changes_nothing() {
        let (s, k) = world();
        for c in [
            format!("!room hand add {ROOM}"),
            format!("!room lock enable {ROOM}"),
            "!room rooms list".to_string(),
        ] {
            let o = handle_chat_ex(&c, "m", &s, &k, &Who(MOD), None, false).unwrap();
            assert!(!o.ok && o.reply.contains("not enabled"), "{c}: {}", o.reply);
        }
        assert!(s.calls.borrow().is_empty() && !k.0.borrow().contains_key(&hands_key(ROOM)));
        // Legacy verbs are unaffected by the flag.
        assert!(
            handle_chat_ex("!room list", "m", &s, &k, &Who(USER), None, false)
                .unwrap()
                .ok
        );
    }

    #[test]
    fn usernames_and_malformed_ids_are_rejected_not_forwarded() {
        let (s, k) = world();
        for c in [
            format!("!room participant remove {ROOM} alice"),
            format!("!room mute enable {ROOM} alice"),
            format!("!room hand remove {ROOM} @bob"),
            format!("!room hand set ack {ROOM} bob"),
            "!room hand add not-a-uuid".to_string(),
        ] {
            let o = say(&c, MOD, &s, &k);
            assert!(o.ends_with("must be a lowercase UUID"), "{c}: {o}");
        }
        assert!(s.calls.borrow().is_empty());
    }

    #[test]
    fn op_labels_are_closed_and_unknown_verbs_fall_through() {
        assert_eq!(op_label(&["hand", "add", "x"]), Some("hand.add"));
        assert_eq!(op_label(&["hand", "set", "ack"]), Some("hand.ack"));
        assert_eq!(op_label(&["MUTE", "enable"]), Some("mute.enable"));
        assert_eq!(op_label(&["rooms"]), Some("rooms.list"));
        assert_eq!(op_label(&["create", "hand"]), None);
        assert_eq!(op_label(&[]), None);
        let (s, k) = world();
        assert!(
            run("list", &[], "m", &s, &k, &Who(USER), true)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn regression_old_room_records_without_locked_still_load() {
        let (_, k) = world();
        let legacy = json!({"id": ROOM, "kind": "community", "label": "r", "parent": null,
            "event_ref": null, "allow_guests": false, "schedule": null, "closed": false,
            "created_by": ADMIN});
        k.set(&format!("room.{ROOM}"), legacy.to_string().as_bytes())
            .unwrap();
        assert!(!load(&k, ROOM).unwrap().locked);
    }

    #[test]
    fn hand_queue_is_bounded() {
        let (s, k) = world();
        let full: Vec<Hand> = (0..MAX_HANDS)
            .map(|i| Hand {
                member: format!("{i:08x}-0000-0000-0000-000000000000"),
                acked_by: None,
            })
            .collect();
        put_hands(&k, ROOM, &full).unwrap();
        assert!(say(&format!("!room hand add {ROOM}"), USER, &s, &k).contains("queue is full"));
    }
}
