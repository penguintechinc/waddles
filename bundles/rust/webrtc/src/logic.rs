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

/// Every tool name this bundle exposes (also the only labels ever logged).
pub const TOOL_NAMES: [&str; 12] = [
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
}
