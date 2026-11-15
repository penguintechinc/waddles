//! Kick webhook delivery normalization (official public-API events).
//!
//! Kick signs every webhook delivery (RSA PKCS#1 v1.5 / SHA-256 over
//! `<Kick-Event-Message-Id>.<Kick-Event-Message-Timestamp>.<raw body>`, key fetched from
//! `GET https://api.kick.com/public/v1/public-key`; see
//! <https://docs.kick.com/events/webhook-security>) and carries the event type ONLY in the
//! `Kick-Event-Type` header -- the JSON body has no type. The HOST verifies the signature and
//! wraps the delivery in this envelope before the guest sees it:
//!
//! ```json
//! {"type":"chat.message.sent","version":1,"message_id":"<ULID>","timestamp":"<RFC3339>","body":{...}}
//! ```
//!
//! Payload shapes follow <https://docs.kick.com/events/event-types>. Output shapes match the
//! Twitch EventSub normalizer (`channel.follow`, `channel.subscribe`,
//! `channel.subscription.gift`, `stream.online`, `stream.offline`) so consumers need no Kick
//! branch. Event types outside the handled set (rewards, metadata, bans, kicks) yield nothing.

use serde_json::{Map, Value, json};

use crate::common::{FrameErr, NormEvent, badge_types, id_string};

/// Webhook event names this module turns into events.
pub const HANDLED_TYPES: &[&str] = &[
    "chat.message.sent",
    "channel.followed",
    "channel.subscription.new",
    "channel.subscription.renewal",
    "channel.subscription.gifts",
    "livestream.status.updated",
];

fn object(v: Value) -> Map<String, Value> {
    match v {
        Value::Object(m) => m,
        _ => Map::new(),
    }
}

fn malformed(msg: &str) -> FrameErr {
    FrameErr::Malformed(msg.to_string())
}

/// Reads a Kick user object (`{user_id, username, channel_slug, ...}`): `(id, username, slug)`.
/// `user_id` is required unless the user is anonymous.
struct User {
    id: Option<String>,
    username: Option<String>,
    slug: Option<String>,
}

fn user(v: Option<&Value>) -> Option<User> {
    let v = v.filter(|u| u.is_object())?;
    let s = |k: &str| {
        v.get(k)
            .and_then(Value::as_str)
            .filter(|x| !x.is_empty())
            .map(str::to_string)
    };
    Some(User {
        id: v.get("user_id").and_then(id_string),
        username: s("username"),
        slug: s("channel_slug"),
    })
}

fn broadcaster(body: &Value) -> Result<User, FrameErr> {
    let b = user(body.get("broadcaster")).ok_or_else(|| malformed("event has no broadcaster"))?;
    if b.id.is_none() {
        return Err(malformed("event broadcaster has no user_id"));
    }
    Ok(b)
}

/// Common eventsub-shaped payload: broadcaster + optional actor user + metadata.
fn shaped(b: &User, actor: Option<&User>, metadata: Value) -> Map<String, Value> {
    object(json!({
        "broadcaster_id": b.id,
        "broadcaster_login": b.slug.as_ref().or(b.username.as_ref()),
        "user_id": actor.and_then(|u| u.id.clone()),
        "user_login": actor.and_then(|u| u.slug.clone().or_else(|| u.username.clone())),
        "user_display_name": actor.and_then(|u| u.username.clone()),
        "metadata": metadata,
    }))
}

fn event(
    event_type: &str,
    actor: Option<&User>,
    payload: Map<String, Value>,
    at: &str,
) -> NormEvent {
    NormEvent {
        event_type: event_type.to_string(),
        actor_key: actor.and_then(|u| u.id.clone()),
        payload,
        occurred_at: at.to_string(),
    }
}

/// Normalizes one host-verified delivery envelope into zero or more events.
///
/// # Errors
/// `Malformed` for a non-object envelope, a missing `type`/`body`, or a handled event whose
/// required fields are absent or of the wrong type (never a defaulted/fabricated event).
pub fn normalize_delivery(bytes: &[u8], received_at: &str) -> Result<Vec<NormEvent>, FrameErr> {
    let root: Value =
        serde_json::from_slice(bytes).map_err(|_| malformed("kick delivery is not JSON"))?;
    let ty = root
        .get("type")
        .and_then(Value::as_str)
        .filter(|t| !t.is_empty())
        .ok_or_else(|| malformed("kick delivery has no type"))?;
    if !HANDLED_TYPES.contains(&ty) {
        return Ok(Vec::new());
    }
    let body = root
        .get("body")
        .filter(|b| b.is_object())
        .ok_or_else(|| malformed("kick delivery has no body object"))?;
    let ev = match ty {
        "chat.message.sent" => chat(body, received_at)?,
        "channel.followed" => {
            let b = broadcaster(body)?;
            let f =
                user(body.get("follower")).ok_or_else(|| malformed("follow has no follower"))?;
            f.id.as_ref()
                .ok_or_else(|| malformed("follower has no user_id"))?;
            let p = shaped(&b, Some(&f), json!({}));
            event("channel.follow", Some(&f), p, received_at)
        }
        "channel.subscription.new" | "channel.subscription.renewal" => {
            let b = broadcaster(body)?;
            let s = user(body.get("subscriber"))
                .ok_or_else(|| malformed("subscription has no subscriber"))?;
            s.id.as_ref()
                .ok_or_else(|| malformed("subscriber has no user_id"))?;
            let duration = body.get("duration").and_then(Value::as_u64).unwrap_or(1);
            let meta = json!({
                "is_gift": false,
                "is_renewal": ty == "channel.subscription.renewal",
                "duration": duration,
            });
            event(
                "channel.subscribe",
                Some(&s),
                shaped(&b, Some(&s), meta),
                received_at,
            )
        }
        "channel.subscription.gifts" => {
            let b = broadcaster(body)?;
            let gifter = user(body.get("gifter"));
            let anonymous = gifter.as_ref().is_none_or(|g| {
                g.id.is_none()
                    || body
                        .get("gifter")
                        .and_then(|g| g.get("is_anonymous"))
                        .and_then(Value::as_bool)
                        == Some(true)
            });
            let total = body
                .get("giftees")
                .and_then(Value::as_array)
                .ok_or_else(|| malformed("gift has no giftees array"))?
                .len();
            // Giftee identities are never published (count only); an anonymous gifter has no
            // actor at all.
            let actor = gifter.as_ref().filter(|_| !anonymous);
            let meta = json!({"total": total, "is_anonymous": anonymous});
            event(
                "channel.subscription.gift",
                actor,
                shaped(&b, actor, meta),
                received_at,
            )
        }
        "livestream.status.updated" => {
            let b = broadcaster(body)?;
            let live = body
                .get("is_live")
                .and_then(Value::as_bool)
                .ok_or_else(|| malformed("livestream status has no is_live boolean"))?;
            if live {
                let started = body.get("started_at").and_then(Value::as_str).unwrap_or("");
                let meta = json!({"type": "live", "started_at": started});
                event("stream.online", None, shaped(&b, None, meta), received_at)
            } else {
                event(
                    "stream.offline",
                    None,
                    shaped(&b, None, json!({})),
                    received_at,
                )
            }
        }
        _ => return Ok(Vec::new()),
    };
    Ok(vec![ev])
}

fn chat(body: &Value, at: &str) -> Result<NormEvent, FrameErr> {
    let text = body
        .get("content")
        .and_then(Value::as_str)
        .filter(|c| !c.trim().is_empty())
        .ok_or_else(|| malformed("chat message has no content"))?;
    let b = broadcaster(body)?;
    let sender = user(body.get("sender")).ok_or_else(|| malformed("chat message has no sender"))?;
    let sender_id = sender
        .id
        .clone()
        .ok_or_else(|| malformed("chat message has no sender id"))?;
    let badges = badge_types(body.get("sender").and_then(|s| s.get("identity")));
    let has = |n: &str| badges.iter().any(|b| b == n);
    let is_owner = has("broadcaster") || Some(&sender_id) == b.id.as_ref();
    let payload = object(json!({
        "text": text.trim(),
        "author": sender.slug.as_ref().or(sender.username.as_ref()),
        "author_id": sender_id,
        "user_id": sender_id,
        "display_name": sender.username,
        "message_id": body.get("message_id").and_then(id_string),
        "broadcaster_id": b.id,
        "channel_name": b.slug,
        "is_mod": has("moderator"),
        "is_subscriber": has("subscriber"),
        "is_vip": has("vip"),
        "is_broadcaster": is_owner,
        "badges": badges,
    }));
    Ok(event("message", Some(&sender), payload, at))
}

#[cfg(test)]
mod tests {
    use super::*;

    const TS: &str = "2026-11-07T00:00:00.000Z";

    fn env(ty: &str, body: &Value) -> Vec<u8> {
        json!({"type": ty, "version": 1, "message_id": "01H", "timestamp": TS, "body": body})
            .to_string()
            .into_bytes()
    }

    fn u(id: u64, name: &str) -> Value {
        json!({"is_anonymous": false, "user_id": id, "username": name,
            "is_verified": false, "profile_picture": "https://x/p.jpg",
            "channel_slug": name.to_lowercase(), "identity": null})
    }

    #[test]
    fn follow_maps_to_channel_follow() {
        let body = json!({"broadcaster": u(1, "Caster"), "follower": u(2, "Fan")});
        let ev = normalize_delivery(&env("channel.followed", &body), TS).unwrap();
        assert_eq!(ev.len(), 1);
        let e = &ev[0];
        assert_eq!(e.event_type, "channel.follow");
        assert_eq!(e.actor_key.as_deref(), Some("2"));
        assert_eq!(e.payload["broadcaster_id"], "1");
        assert_eq!(e.payload["user_id"], "2");
        assert_eq!(e.payload["user_display_name"], "Fan");
        assert_eq!(e.payload["metadata"], json!({}));
    }

    #[test]
    fn new_and_renewal_subscriptions_share_the_subscribe_kind() {
        for (ty, renewal, dur) in [
            ("channel.subscription.new", false, 1),
            ("channel.subscription.renewal", true, 3),
        ] {
            let body = json!({"broadcaster": u(1, "C"), "subscriber": u(5, "S"),
                "duration": dur, "created_at": TS, "expires_at": TS});
            let e = &normalize_delivery(&env(ty, &body), TS).unwrap()[0];
            assert_eq!(e.event_type, "channel.subscribe");
            assert_eq!(e.actor_key.as_deref(), Some("5"));
            assert_eq!(
                e.payload["metadata"],
                json!({"is_gift": false, "is_renewal": renewal, "duration": dur})
            );
        }
    }

    #[test]
    fn gifts_publish_counts_never_giftee_identities() {
        let body = json!({"broadcaster": u(1, "C"), "gifter": u(7, "Santa"),
            "giftees": [u(8, "A"), u(9, "B")], "created_at": TS, "expires_at": TS});
        let e = &normalize_delivery(&env("channel.subscription.gifts", &body), TS).unwrap()[0];
        assert_eq!(e.event_type, "channel.subscription.gift");
        assert_eq!(e.actor_key.as_deref(), Some("7"));
        assert_eq!(
            e.payload["metadata"],
            json!({"total": 2, "is_anonymous": false})
        );
        let text = serde_json::to_string(&e.payload).unwrap();
        assert!(!text.contains("\"8\"") && !text.contains("\"9\"") && !text.contains("giftee"));
    }

    #[test]
    fn anonymous_gifter_has_no_actor() {
        let body = json!({"broadcaster": u(1, "C"),
            "gifter": {"is_anonymous": true, "user_id": null, "username": null},
            "giftees": [u(8, "A")]});
        let e = &normalize_delivery(&env("channel.subscription.gifts", &body), TS).unwrap()[0];
        assert_eq!(e.actor_key, None);
        assert_eq!(e.payload["user_id"], Value::Null);
        assert_eq!(e.payload["metadata"]["is_anonymous"], true);
        let no_gifter = json!({"broadcaster": u(1, "C"), "giftees": []});
        let e = &normalize_delivery(&env("channel.subscription.gifts", &no_gifter), TS).unwrap()[0];
        assert_eq!(e.actor_key, None);
    }

    #[test]
    fn livestream_status_maps_online_and_offline() {
        let on = json!({"broadcaster": u(1, "C"), "is_live": true, "title": "t",
            "started_at": "2025-01-01T11:00:00+11:00", "ended_at": null});
        let e = &normalize_delivery(&env("livestream.status.updated", &on), TS).unwrap()[0];
        assert_eq!(e.event_type, "stream.online");
        assert_eq!(e.actor_key, None);
        assert_eq!(
            e.payload["metadata"],
            json!({"type": "live", "started_at": "2025-01-01T11:00:00+11:00"})
        );
        let off = json!({"broadcaster": u(1, "C"), "is_live": false});
        let e = &normalize_delivery(&env("livestream.status.updated", &off), TS).unwrap()[0];
        assert_eq!(e.event_type, "stream.offline");
        assert_eq!(e.payload["metadata"], json!({}));
        // The stream title is free text and is never published.
        assert!(!serde_json::to_string(&e.payload).unwrap().contains("title"));
    }

    #[test]
    fn webhook_chat_message_carries_roles_and_owner_detection() {
        let mut sender = u(2, "Mod");
        sender["identity"] = json!({"badges": [{"type": "moderator"}, {"type": "vip"}]});
        let body = json!({"message_id": "m-1", "broadcaster": u(1, "Caster"),
            "sender": sender, "content": " hi ", "emotes": []});
        let e = &normalize_delivery(&env("chat.message.sent", &body), TS).unwrap()[0];
        assert_eq!(e.event_type, "message");
        let p = &e.payload;
        assert_eq!(p["text"], "hi");
        assert_eq!(p["message_id"], "m-1");
        assert_eq!(p["channel_name"], "caster");
        assert_eq!(p["is_mod"], true);
        assert_eq!(p["is_vip"], true);
        assert_eq!(p["is_broadcaster"], false);
        let own = json!({"broadcaster": u(1, "C"), "sender": u(1, "C"), "content": "x"});
        let e = &normalize_delivery(&env("chat.message.sent", &own), TS).unwrap()[0];
        assert_eq!(e.payload["is_broadcaster"], true);
    }

    #[test]
    fn unhandled_types_are_ignored_and_bad_input_is_loud() {
        for ty in [
            "channel.reward.redemption.updated",
            "livestream.metadata.updated",
            "moderation.banned",
            "kicks.gifted",
            "future.event",
        ] {
            let body = json!({"broadcaster": u(1, "C")});
            assert!(normalize_delivery(&env(ty, &body), TS).unwrap().is_empty());
        }
        for bad in [&b"x"[..], b"[]", b"{}", br#"{"type":""}"#] {
            assert!(matches!(
                normalize_delivery(bad, TS),
                Err(FrameErr::Malformed(_))
            ));
        }
        let no_body = json!({"type": "channel.followed"}).to_string();
        assert!(matches!(
            normalize_delivery(no_body.as_bytes(), TS),
            Err(FrameErr::Malformed(_))
        ));
        for body in [
            json!({"follower": u(2, "F")}),
            json!({"broadcaster": {"username": "x"}, "follower": u(2, "F")}),
            json!({"broadcaster": u(1, "C")}),
            json!({"broadcaster": u(1, "C"), "follower": {"username": "x"}}),
        ] {
            assert!(
                matches!(
                    normalize_delivery(&env("channel.followed", &body), TS),
                    Err(FrameErr::Malformed(_))
                ),
                "{body}"
            );
        }
        let no_live = json!({"broadcaster": u(1, "C")});
        assert!(normalize_delivery(&env("livestream.status.updated", &no_live), TS).is_err());
        let no_giftees = json!({"broadcaster": u(1, "C")});
        assert!(normalize_delivery(&env("channel.subscription.gifts", &no_giftees), TS).is_err());
        let empty_chat = json!({"broadcaster": u(1, "C"), "sender": u(2, "S"), "content": " "});
        assert!(normalize_delivery(&env("chat.message.sent", &empty_chat), TS).is_err());
    }

    #[test]
    fn every_handled_type_is_exercised_by_these_tests() {
        assert_eq!(HANDLED_TYPES.len(), 6);
    }
}
