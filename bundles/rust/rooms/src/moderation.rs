//! Room moderation: raise-hand queue plus moderator controls.
//!
//! Ported from the retired Go `module_rtc` call features. Everything here fronts
//! the `webrtc` service tools, which forward to svc-streaming's host API
//! (`core/svc_streaming/docs/rooms-moderation-host-api.md`):
//!
//! * the **hand queue** is AUTHORITATIVE in svc-streaming's `RoomService`
//!   (`room.hand.raise|lower|list|ack|reset`); this app keeps NO copy of it -- no
//!   kv key, no cache -- and the server clears a participant's hand when they
//!   leave, are kicked, or the room is deleted;
//! * **moderation** (kick/leave/mute/unmute/mute-all/lock/unlock and the live
//!   participant/room lists) likewise forwards to the service tools.
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

use serde_json::{Value, json};

use crate::logic::{
    Ident, Role, Room, RoomsError, Store, ToolFail, Tools, idem_key, load, need_uuid, role_of,
    save, tool_err,
};

/// Feature flag for every moderation command (defaults OFF until validated).
pub const FLAG: &str = "rooms.moderation";
/// The contract version every hand tool call states explicitly.
const API_VERSION: &str = "v1";

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
        ("hand", ["add", room]) => hand_add(tools, store, msg, room, &actor()?)?,
        ("hand", ["remove", room]) => {
            let a = actor()?;
            hand_remove(tools, store, msg, room, &a, &a)?
        }
        ("hand", ["remove", room, member]) => {
            hand_remove(tools, store, msg, room, &actor()?, member)?
        }
        ("hand", ["set", "ack", room, member]) => {
            hand_ack(tools, store, msg, room, &actor()?, member)?
        }
        ("hand", ["reset", room]) => hand_reset(tools, store, msg, room, &actor()?)?,
        ("hand", ["list", room] | [room]) => hand_list(tools, store, room, &actor()?)?,
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

/// Raises the actor's own hand (idempotent in the service, which keeps arrival order).
fn hand_add(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    room: &str,
    actor: &str,
) -> Result<String, RoomsError> {
    authorize(store, room, actor, Role::Member)?;
    // No actor in the args: the webrtc tool takes the host-verified actor from its
    // context, so a hand can only ever be raised for the person who asked.
    tools
        .invoke(
            "room.hand.raise",
            &json!({"api_version": API_VERSION, "room_id": room}),
            Some(&idem_key(&format!("{msg}|{room}"), "room.hand.raise")),
        )
        .map_err(tool_err)?;
    Ok("Hand raised.".into())
}

/// Lowers `member`'s hand: always allowed for self, moderators for anyone.
fn hand_remove(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
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
    // Own hand: `lower` (member scope, no participant arg). Someone else's:
    // `lower_other` (moderate scope, verified by the host before dispatch).
    let (tool, args) = if member == actor {
        (
            "room.hand.lower",
            json!({"api_version": API_VERSION, "room_id": room}),
        )
    } else {
        (
            "room.hand.lower_other",
            json!({"api_version": API_VERSION, "room_id": room, "participant_id": member}),
        )
    };
    let out = tools
        .invoke(
            tool,
            &args,
            Some(&idem_key(&format!("{msg}|{room}|{member}"), tool)),
        )
        .map_err(tool_err)?;
    match out.get("lowered").and_then(Value::as_bool) {
        Some(true) => Ok("Hand lowered.".into()),
        Some(false) => Ok("No raised hand to lower.".into()),
        None => Err(RoomsError::Tool(ToolFail::Unavailable)),
    }
}

/// Moderator acknowledges a raised hand (kept in the queue, marked).
fn hand_ack(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    room: &str,
    actor: &str,
    member: &str,
) -> Result<String, RoomsError> {
    need_uuid("member", member)?;
    authorize(store, room, actor, Role::Moderator)?;
    tools
        .invoke(
            "room.hand.ack",
            &json!({"api_version": API_VERSION, "room_id": room, "participant_id": member}),
            Some(&idem_key(
                &format!("{msg}|{room}|{member}"),
                "room.hand.ack",
            )),
        )
        .map_err(|e| match e {
            // The service answers "not found" for a participant without a raised hand.
            ToolFail::InvalidArgs(m) if m.contains("not found") => {
                RoomsError::Usage("That participant has no raised hand.".into())
            }
            other => tool_err(other),
        })?;
    Ok("Hand acknowledged.".into())
}

/// Moderator clears the whole queue.
fn hand_reset(
    tools: &dyn Tools,
    store: &dyn Store,
    msg: &str,
    room: &str,
    actor: &str,
) -> Result<String, RoomsError> {
    authorize(store, room, actor, Role::Moderator)?;
    let out = tools
        .invoke(
            "room.hand.reset",
            &json!({"api_version": API_VERSION, "room_id": room}),
            Some(&idem_key(&format!("{msg}|{room}"), "room.hand.reset")),
        )
        .map_err(tool_err)?;
    let n = out
        .get("cleared")
        .and_then(Value::as_u64)
        .ok_or(RoomsError::Tool(ToolFail::Unavailable))?;
    Ok(format!("All hands lowered ({n} cleared)."))
}

/// Lists the queue. Moderators see every UUID in order (`*` marks acknowledged
/// hands); members only see their own place in it -- the tool filters, this just
/// formats what it was given.
fn hand_list(
    tools: &dyn Tools,
    store: &dyn Store,
    room: &str,
    actor: &str,
) -> Result<String, RoomsError> {
    let (_, role) = authorize(store, room, actor, Role::Member)?;
    // Moderators ask for the whole queue (`list_all`, moderate scope); everyone else
    // only ever gets their own entry (`list`, member scope).
    let tool = if role >= Role::Moderator {
        "room.hand.list_all"
    } else {
        "room.hand.list"
    };
    let out = tools
        .invoke(
            tool,
            &json!({"api_version": API_VERSION, "room_id": room}),
            None,
        )
        .map_err(tool_err)?;
    let count = out
        .get("count")
        .and_then(Value::as_u64)
        .ok_or(RoomsError::Tool(ToolFail::Unavailable))?;
    let hands = out
        .get("hands")
        .and_then(Value::as_array)
        .ok_or(RoomsError::Tool(ToolFail::Unavailable))?;
    if count == 0 {
        return Ok("No raised hands.".into());
    }
    let unavailable = || RoomsError::Tool(ToolFail::Unavailable);
    if tool == "room.hand.list" {
        return Ok(match hands.first() {
            Some(h) => format!(
                "Your hand is raised (position {} of {count}).",
                h.get("position")
                    .and_then(Value::as_u64)
                    .ok_or_else(unavailable)?
            ),
            None => format!("Your hand is not raised ({count} in the queue)."),
        });
    }
    let lines: Option<Vec<String>> = hands
        .iter()
        .map(|h| {
            let who = h.get("participant_id")?.as_str()?;
            let pos = h.get("position")?.as_u64()?;
            let acked = h.get("acknowledged_at").is_some();
            Some(format!("{pos}. {who}{}", if acked { " *" } else { "" }))
        })
        .collect();
    Ok(format!(
        "Raised hands: {}",
        lines.ok_or_else(unavailable)?.join(", ")
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

/// Leaves (`member == actor`) or kicks another participant; the server clears the
/// removed participant's raised hand.
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
    // No hand cleanup here on purpose: the queue lives in svc-streaming, which
    // drops a participant's hand itself on leave/kick, so there is no second copy
    // to keep in step.
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
    use crate::logic::{Kind, grants_for, handle_chat_ex, put_json};
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

    /// The host + webrtc service as the rooms app sees it. The raised-hand queue lives
    /// HERE (as in svc-streaming's `RoomService`): idempotent raise, scope-gated
    /// moderator tools, members see only their own place in `list`, leave/kick drop
    /// the participant's hand. The actor is the host-VERIFIED context (`actor` +
    /// community scopes derived from the role), exactly as in production -- hand
    /// calls may never carry `actor_id`/`actor_role` arguments, and the double
    /// refuses any that do.
    #[derive(Default)]
    struct Svc {
        calls: RefCell<Vec<(String, Value)>>,
        fail: RefCell<Option<ToolFail>>,
        /// The verified actor of the chat message being handled (set by `say`).
        actor: RefCell<&'static str>,
        /// (participant, acknowledged-by) in arrival order.
        queue: RefCell<Vec<(String, Option<String>)>>,
    }
    impl Svc {
        /// Community scopes the host derives for `who` (moderators and admins hold
        /// `community:moderate`; everyone else is a plain member).
        fn is_staff(who: &str) -> bool {
            who == MOD || who == ADMIN
        }
        fn hand(m: &str, acked: &Option<String>) -> Value {
            match acked {
                None => json!({"participant_id": m, "raised_at": 1}),
                Some(by) => json!({"participant_id": m, "raised_at": 1,
                                   "acknowledged_at": 2, "acknowledged_by": by}),
            }
        }
        fn hand_op(&self, tool: &str, args: &Value) -> Result<Value, ToolFail> {
            assert_eq!(args["api_version"], "v1", "{tool} must state v1");
            let o = args.as_object().unwrap();
            assert!(
                !o.contains_key("actor_id") && !o.contains_key("actor_role"),
                "{tool} must not carry a caller-asserted actor: {args}"
            );
            let me: &str = *self.actor.borrow();
            // The manifest `required_scope` gate the host applies before dispatch.
            let moderator_tool = matches!(
                tool,
                "room.hand.lower_other"
                    | "room.hand.list_all"
                    | "room.hand.ack"
                    | "room.hand.reset"
            );
            if moderator_tool && !Self::is_staff(me) {
                return Err(ToolFail::NotGranted);
            }
            let mut q = self.queue.borrow_mut();
            match tool {
                "room.hand.raise" => {
                    assert!(!o.contains_key("participant_id"), "raise is self-only");
                    if !q.iter().any(|(m, _)| m == me) {
                        q.push((me.into(), None));
                    }
                    Ok(json!({"room_id": ROOM, "hand": Self::hand(me, &None)}))
                }
                "room.hand.lower" | "room.hand.lower_other" => {
                    let who = if tool == "room.hand.lower" {
                        assert!(!o.contains_key("participant_id"), "lower is self-only");
                        me
                    } else {
                        args["participant_id"].as_str().unwrap()
                    };
                    let before = q.len();
                    q.retain(|(m, _)| m != who);
                    Ok(
                        json!({"room_id": ROOM, "participant_id": who, "lowered": q.len() != before}),
                    )
                }
                "room.hand.list" | "room.hand.list_all" => {
                    let all: Vec<Value> = q
                        .iter()
                        .enumerate()
                        .filter(|(_, (m, _))| tool == "room.hand.list_all" || m == me)
                        .map(|(i, (m, a))| {
                            let mut h = Self::hand(m, a);
                            h["position"] = json!(i + 1);
                            h
                        })
                        .collect();
                    Ok(json!({"room_id": ROOM, "count": q.len(), "hands": all}))
                }
                "room.hand.ack" => {
                    let who = args["participant_id"].as_str().unwrap();
                    match q.iter_mut().find(|(m, _)| m == who) {
                        Some(e) => {
                            e.1 = Some(me.into());
                            Ok(json!({"room_id": ROOM, "hand": Self::hand(who, &e.1)}))
                        }
                        None => Err(ToolFail::InvalidArgs("room or source not found".into())),
                    }
                }
                _ => {
                    let n = q.len();
                    q.clear();
                    Ok(json!({"room_id": ROOM, "cleared": n}))
                }
            }
        }
    }
    impl Tools for Svc {
        fn invoke(&self, tool: &str, args: &Value, _: Option<&str>) -> Result<Value, ToolFail> {
            self.calls.borrow_mut().push((tool.into(), args.clone()));
            if let Some(f) = self.fail.borrow().clone() {
                return Err(f);
            }
            if tool.starts_with("room.hand.") {
                return self.hand_op(tool, args);
            }
            if matches!(tool, "room.leave" | "room.kick") {
                // The server drops a leaving/kicked participant's hand itself.
                let who = args["participant_id"].as_str().unwrap();
                self.queue.borrow_mut().retain(|(m, _)| m != who);
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
        *s.actor.borrow_mut() = who;
        handle_chat_ex(text, "m1", s, k, &Who(who), None, true)
            .unwrap()
            .reply
    }

    /// Every kv key currently stored (to prove the app keeps no hand state).
    fn kv_keys(k: &Kv) -> Vec<String> {
        let mut v: Vec<String> = k.0.borrow().keys().cloned().collect();
        v.sort();
        v
    }

    #[test]
    fn hand_verbs_call_the_webrtc_hand_tools_with_v1_and_no_actor_arguments() {
        let (s, k) = world();
        let kv_before = kv_keys(&k);
        let calls = |n: usize| s.calls.borrow()[n].clone();
        assert_eq!(
            say(&format!("!room hand add {ROOM}"), USER, &s, &k),
            "Hand raised."
        );
        assert_eq!(
            calls(0),
            (
                "room.hand.raise".to_string(),
                json!({"api_version": "v1", "room_id": ROOM})
            )
        );
        say(&format!("!room hand add {ROOM}"), USER, &s, &k);
        assert_eq!(s.queue.borrow().len(), 1, "raising twice is a no-op");
        assert_eq!(
            say(&format!("!room hand remove {ROOM}"), USER, &s, &k),
            "Hand lowered."
        );
        assert_eq!(
            calls(2),
            (
                "room.hand.lower".to_string(),
                json!({"api_version": "v1", "room_id": ROOM})
            )
        );
        assert!(say(&format!("!room hand remove {ROOM}"), USER, &s, &k).contains("No raised hand"));
        say(&format!("!room hand add {ROOM}"), USER, &s, &k);
        say(&format!("!room hand set ack {ROOM} {USER}"), MOD, &s, &k);
        assert_eq!(
            s.calls.borrow().last().unwrap().clone(),
            (
                "room.hand.ack".to_string(),
                json!({"api_version": "v1", "room_id": ROOM, "participant_id": USER})
            )
        );
        say(&format!("!room hand remove {ROOM} {USER}"), MOD, &s, &k);
        assert_eq!(
            s.calls.borrow().last().unwrap().clone(),
            (
                "room.hand.lower_other".to_string(),
                json!({"api_version": "v1", "room_id": ROOM, "participant_id": USER})
            )
        );
        say(&format!("!room hand reset {ROOM}"), ADMIN, &s, &k);
        assert_eq!(
            s.calls.borrow().last().unwrap().clone(),
            (
                "room.hand.reset".to_string(),
                json!({"api_version": "v1", "room_id": ROOM})
            )
        );
        assert_eq!(kv_keys(&k), kv_before, "hands never touch the app kv");
    }

    #[test]
    fn list_picks_the_tool_by_role_so_scope_decides_the_view() {
        let (s, k) = world();
        say(&format!("!room hand list {ROOM}"), USER, &s, &k);
        say(&format!("!room hand list {ROOM}"), MOD, &s, &k);
        let tools: Vec<String> = s.calls.borrow().iter().map(|c| c.0.clone()).collect();
        assert_eq!(tools, ["room.hand.list", "room.hand.list_all"]);
    }

    #[test]
    fn no_hand_call_ever_carries_an_actor_or_a_role() {
        let (s, k) = world();
        for (c, who) in [
            (format!("!room hand add {ROOM}"), USER),
            (format!("!room hand add {ROOM}"), USER2),
            (format!("!room hand list {ROOM}"), USER),
            (format!("!room hand list {ROOM}"), MOD),
            (format!("!room hand set ack {ROOM} {USER}"), MOD),
            (format!("!room hand remove {ROOM}"), USER),
            (format!("!room hand remove {ROOM} {USER2}"), ADMIN),
            (format!("!room hand reset {ROOM}"), ADMIN),
        ] {
            say(&c, who, &s, &k);
        }
        let hand_calls: Vec<_> = s
            .calls
            .borrow()
            .iter()
            .filter(|c| c.0.starts_with("room.hand."))
            .cloned()
            .collect();
        assert!(
            hand_calls.len() >= 8,
            "only {} hand calls examined",
            hand_calls.len()
        );
        for (tool, args) in hand_calls {
            let o = args.as_object().unwrap();
            assert!(
                !o.contains_key("actor_id") && !o.contains_key("actor_role"),
                "{tool} carries a caller-asserted actor"
            );
        }
    }

    #[test]
    fn an_acl_moderator_without_the_moderate_scope_is_refused_by_the_host() {
        // The room ACL says moderator, but the host-derived community scope does not:
        // the scope is authoritative, so the moderator-only tools are refused.
        let (s, k) = world();
        put_json(&k, &format!("acl.{ROOM}.{USER}"), &Role::Moderator).unwrap();
        say(&format!("!room hand add {ROOM}"), USER2, &s, &k);
        for c in [
            format!("!room hand reset {ROOM}"),
            format!("!room hand set ack {ROOM} {USER2}"),
            format!("!room hand remove {ROOM} {USER2}"),
            format!("!room hand list {ROOM}"),
        ] {
            let out = say(&c, USER, &s, &k);
            assert!(!out.contains(USER2), "{c}: leaked the queue: {out}");
        }
        assert_eq!(s.queue.borrow().len(), 1, "nothing was changed");
        assert!(say(&format!("!room hand reset {ROOM}"), USER, &s, &k).contains("refused"));
    }

    #[test]
    fn the_app_keeps_no_second_copy_of_hand_state() {
        let (s, k) = world();
        let kv_before = kv_keys(&k);
        for c in [
            format!("!room hand add {ROOM}"),
            format!("!room hand list {ROOM}"),
            format!("!room hand set ack {ROOM} {USER}"),
            format!("!room hand remove {ROOM} {USER}"),
            format!("!room hand reset {ROOM}"),
        ] {
            say(
                &c,
                if c.contains("set ack") || c.contains("reset") {
                    MOD
                } else {
                    USER
                },
                &s,
                &k,
            );
        }
        // Leave and kick: the server clears the hand, the app writes nothing and
        // issues no extra `room.hand.*` call to do it.
        say(&format!("!room hand add {ROOM}"), USER2, &s, &k);
        say(&format!("!room hand add {ROOM}"), USER, &s, &k);
        let n = s.calls.borrow().len();
        say(&format!("!room participant remove {ROOM}"), USER, &s, &k);
        say(
            &format!("!room participant remove {ROOM} {USER2}"),
            MOD,
            &s,
            &k,
        );
        let after: Vec<String> = s.calls.borrow()[n..].iter().map(|c| c.0.clone()).collect();
        assert_eq!(
            after,
            ["room.leave", "room.kick"],
            "no hand tool on leave/kick"
        );
        assert!(s.queue.borrow().is_empty(), "the server cleared both hands");
        assert_eq!(kv_keys(&k), kv_before, "no kv write, no `hands.*` key");
        assert!(kv_keys(&k).iter().all(|key| !key.starts_with("hands.")));
    }

    #[test]
    fn list_gives_moderators_the_queue_and_members_only_their_own_place() {
        let (s, k) = world();
        say(&format!("!room hand add {ROOM}"), USER, &s, &k);
        say(&format!("!room hand add {ROOM}"), USER2, &s, &k);
        say(&format!("!room hand set ack {ROOM} {USER2}"), MOD, &s, &k);
        let l = say(&format!("!room hand list {ROOM}"), MOD, &s, &k);
        assert!(l.find(USER).unwrap() < l.find(USER2).unwrap(), "{l}");
        assert!(l.contains(&format!("2. {USER2} *")), "{l}");
        assert_eq!(
            say(&format!("!room hand {ROOM}"), MOD, &s, &k),
            l,
            "bare reads"
        );
        let own = say(&format!("!room hand list {ROOM}"), USER2, &s, &k);
        assert_eq!(own, "Your hand is raised (position 2 of 2).");
        assert!(!own.contains(USER), "a member never sees other UUIDs");
        say(&format!("!room hand remove {ROOM}"), USER2, &s, &k);
        assert_eq!(
            say(&format!("!room hand list {ROOM}"), USER2, &s, &k),
            "Your hand is not raised (1 in the queue)."
        );
        say(&format!("!room hand reset {ROOM}"), MOD, &s, &k);
        assert_eq!(
            say(&format!("!room hand list {ROOM}"), MOD, &s, &k),
            "No raised hands."
        );
    }

    #[test]
    fn acknowledge_and_reset_need_a_moderator_and_members_cannot_lower_others() {
        let (s, k) = world();
        say(&format!("!room hand add {ROOM}"), USER, &s, &k);
        let n = s.calls.borrow().len();
        let ack = format!("!room hand set ack {ROOM} {USER}");
        assert!(say(&ack, USER2, &s, &k).contains("moderator role required"));
        assert!(
            say(&format!("!room hand remove {ROOM} {USER}"), USER2, &s, &k).contains("moderator")
        );
        assert!(say(&format!("!room hand reset {ROOM}"), USER, &s, &k).contains("moderator"));
        assert_eq!(
            s.calls.borrow().len(),
            n,
            "refusals never reach the service"
        );
        assert_eq!(say(&ack, MOD, &s, &k), "Hand acknowledged.");
        assert_eq!(
            say(&format!("!room hand remove {ROOM} {USER}"), MOD, &s, &k),
            "Hand lowered."
        );
        assert!(
            say(&format!("!room hand set ack {ROOM} {USER}"), MOD, &s, &k)
                .contains("no raised hand")
        );
        say(&format!("!room hand add {ROOM}"), USER, &s, &k);
        assert_eq!(
            say(&format!("!room hand reset {ROOM}"), MOD, &s, &k),
            "All hands lowered (1 cleared)."
        );
    }

    #[test]
    fn hand_tool_failures_surface_as_closed_messages_and_never_succeed_silently() {
        let (s, k) = world();
        *s.fail.borrow_mut() = Some(ToolFail::Unavailable);
        assert!(say(&format!("!room hand add {ROOM}"), USER, &s, &k).contains("unavailable"));
        assert!(say(&format!("!room hand list {ROOM}"), USER, &s, &k).contains("unavailable"));
        *s.fail.borrow_mut() = Some(ToolFail::Forbidden);
        assert!(say(&format!("!room hand reset {ROOM}"), MOD, &s, &k).contains("refused"));
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
    fn leave_is_self_service_and_the_server_clears_the_hand_on_kick() {
        let (s, k) = world();
        say(&format!("!room hand add {ROOM}"), USER, &s, &k);
        say(&format!("!room hand add {ROOM}"), USER2, &s, &k);
        assert_eq!(
            say(&format!("!room participant remove {ROOM}"), USER, &s, &k),
            "You left the room."
        );
        assert_eq!(s.calls.borrow()[2].0, "room.leave");
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
        assert!(
            s.calls.borrow().is_empty() && kv_keys(&k).iter().all(|x| !x.starts_with("hands."))
        );
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
}
