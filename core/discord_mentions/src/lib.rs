//! Host-level `allowed_mentions` backstop for every Discord create-message
//! REST body the data plane emits.
//!
//! **Why this exists.** Discord parses `@everyone` / `@here` / `<@&role>`
//! out of a message's `content` and notifies the whole guild or role unless
//! the request's `allowed_mentions` object says otherwise. Message text
//! reaches the send path from bundle output (and, through bundles, from
//! forged or user-controlled content), so without a host-level guard any one
//! of the ~84 bundles -- or any content a bundle echoes -- can mass-ping a
//! community. Setting the guard here, once, in the host means no bundle has
//! to remember it and none can opt out of it.
//!
//! **Policy (the only one this crate can express): `{"parse": ["users"]}`.**
//! - `everyone` is never in `parse`, so `@everyone` / `@here` render as plain
//!   text and notify nobody.
//! - `roles` is never in `parse`, and no explicit `roles` id list is ever
//!   emitted, so `<@&role>` pings nobody.
//! - `users` stays in `parse`, so a legitimate single-user mention (a
//!   bundle-authored `<@user_id>`) still notifies that user. (A `{user:<token>}`
//!   detokenized display name is escaped to plain text by
//!   `egress_detokenizer` and was never a ping to begin with.)
//! - `replied_user` is deliberately left unset: Discord's default is `false`
//!   (a reply does not ping its target). No data-plane path sends a
//!   `message_reference` today; a future reply path inherits the no-ping
//!   default and would have to set it explicitly, in review, to change it.
//!
//! **Role/everyone pings are not a default and have no constructor here.** If
//! a legitimate feature (e.g. an admin announcement) ever needs one, it must
//! add a separately-named, reviewed opt-in for that one call path -- never
//! widen [`allowed_mentions`]. [`is_guarded`] fails any body that allows
//! `everyone` or `roles`, so a widened default is caught by this crate's own
//! tests and by every consumer's send-path tests.
//!
//! **Scope.** Covers every Discord create-message body the data-plane *hosts*
//! build: svc-action's bot-token relay send and webhook-body helper, and
//! svc-ingest's `chat.send` / `dm.send`. It does **not** rewrite bytes a bundle
//! assembles itself and sends through the generic `http` capability to a
//! Discord URL (it would need a Discord-aware body rewrite inside the shared
//! egress guard); no bundle declares a Discord egress host today, and a bundle
//! that did would have to be reviewed for exactly this reason.
//!
//! **Use:** build every create-message body with [`message_body`] (or stamp an
//! existing JSON-object body with [`guard_body`]); never hand-write a
//! `{"content": ...}` literal for a Discord send.

use serde_json::{json, Map, Value};

/// JSON key Discord reads the mention policy from.
pub const ALLOWED_MENTIONS_FIELD: &str = "allowed_mentions";

/// The `allowed_mentions` value every data-plane Discord send carries:
/// `{"parse": ["users"]}` -- user mentions notify, `@everyone` / `@here` /
/// role mentions never do. See the module doc for the full policy.
#[must_use]
pub fn allowed_mentions() -> Value {
    json!({ "parse": ["users"] })
}

/// Builds a Discord create-message body (`POST /channels/{id}/messages`,
/// webhook execute) for `content`, with the mass-mention guard attached.
#[must_use]
pub fn message_body(content: &str) -> Value {
    let mut body = json!({ "content": content });
    guard_body(&mut body);
    body
}

/// Stamps the mass-mention guard onto `body`, **overwriting** any
/// `allowed_mentions` it already carries (a caller-supplied permissive value
/// must never survive). A non-object `body` is left untouched -- Discord
/// rejects it anyway, and the guard has nowhere to live.
pub fn guard_body(body: &mut Value) {
    if let Value::Object(map) = body {
        stamp(map);
    }
}

fn stamp(map: &mut Map<String, Value>) {
    map.insert(ALLOWED_MENTIONS_FIELD.to_string(), allowed_mentions());
}

/// `true` when `body` carries an `allowed_mentions` object that can not
/// mass-mention: `parse` is an array containing neither `"everyone"` nor
/// `"roles"`, and no explicit `roles` id list is present. A missing field,
/// a non-object value, or a malformed `parse` is **not** guarded (Discord's
/// default for an absent field is to parse everything). Used as the shared
/// test oracle for every send path, and available as a runtime assertion.
#[must_use]
pub fn is_guarded(body: &Value) -> bool {
    let Some(am) = body.get(ALLOWED_MENTIONS_FIELD).and_then(Value::as_object) else {
        return false;
    };
    let Some(parse) = am.get("parse").and_then(Value::as_array) else {
        return false;
    };
    if am.contains_key("roles") {
        return false;
    }
    parse
        .iter()
        .all(|p| matches!(p.as_str(), Some(kind) if kind != "everyone" && kind != "roles"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn policy_is_users_only() {
        assert_eq!(allowed_mentions(), json!({"parse": ["users"]}));
    }

    #[test]
    fn message_body_carries_content_and_the_guard() {
        let body = message_body("hello");
        assert_eq!(body["content"], "hello");
        assert_eq!(body[ALLOWED_MENTIONS_FIELD], json!({"parse": ["users"]}));
        assert!(is_guarded(&body));
    }

    /// An `@everyone` / `@here` / role-mention payload keeps its text verbatim
    /// (the guard never rewrites content) and serializes WITH the guard, so
    /// Discord renders it inert instead of pinging.
    #[test]
    fn mass_mention_payload_serializes_with_the_guard() {
        for content in [
            "@everyone free stuff",
            "@here look",
            "<@&123456789012345678> role ping",
            "hi <@123456789012345678> and @everyone",
        ] {
            let wire = serde_json::to_string(&message_body(content)).unwrap();
            let parsed: Value = serde_json::from_str(&wire).unwrap();
            assert_eq!(parsed["content"], content);
            let parse = parsed["allowed_mentions"]["parse"].as_array().unwrap();
            assert!(parse.iter().all(|p| p != "everyone" && p != "roles"));
            assert!(parsed["allowed_mentions"].get("roles").is_none());
            assert!(is_guarded(&parsed), "{wire}");
        }
    }

    #[test]
    fn user_mentions_stay_allowed() {
        let parse = &message_body("<@1>")["allowed_mentions"]["parse"];
        assert_eq!(parse, &json!(["users"]));
    }

    #[test]
    fn guard_body_overwrites_a_permissive_caller_value() {
        let mut body = json!({
            "content": "x",
            "allowed_mentions": {"parse": ["everyone", "roles", "users"], "roles": ["1"]},
        });
        assert!(!is_guarded(&body));
        guard_body(&mut body);
        assert_eq!(body["allowed_mentions"], json!({"parse": ["users"]}));
        assert!(is_guarded(&body));
    }

    #[test]
    fn guard_body_leaves_non_objects_alone() {
        let mut body = json!("not an object");
        guard_body(&mut body);
        assert_eq!(body, json!("not an object"));
    }

    #[test]
    fn is_guarded_rejects_every_unsafe_shape() {
        let cases = [
            json!({"content": "x"}),
            json!({"allowed_mentions": null}),
            json!({"allowed_mentions": "users"}),
            json!({"allowed_mentions": {}}),
            json!({"allowed_mentions": {"parse": "users"}}),
            json!({"allowed_mentions": {"parse": ["everyone"]}}),
            json!({"allowed_mentions": {"parse": ["roles"]}}),
            json!({"allowed_mentions": {"parse": ["users", "everyone"]}}),
            json!({"allowed_mentions": {"parse": [1]}}),
            json!({"allowed_mentions": {"parse": ["users"], "roles": ["1"]}}),
        ];
        for case in cases {
            assert!(!is_guarded(&case), "{case} must not count as guarded");
        }
    }

    #[test]
    fn is_guarded_accepts_the_empty_and_users_only_policies() {
        assert!(is_guarded(&json!({"allowed_mentions": {"parse": []}})));
        assert!(is_guarded(
            &json!({"allowed_mentions": {"parse": ["users"]}})
        ));
        assert!(is_guarded(
            &json!({"allowed_mentions": {"parse": [], "users": ["1"]}})
        ));
    }
}
