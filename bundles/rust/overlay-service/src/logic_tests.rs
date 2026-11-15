//! Tests for the overlay service tools, including negative tests for the three security
//! findings (caller-chosen widget ownership, fail-open push, shared cross-community state).

use std::collections::HashMap;
use std::sync::Mutex;

use super::*;
use crate::store::KvHostStore;

const ADMIN_A: &str = "0195f3a2-8c11-7000-8000-00000000000a";
const ADMIN_B: &str = "0195f3a2-8c11-7000-8000-00000000000b";
const MEMBER: &str = "0195f3a2-8c11-7000-8000-00000000000c";

/// In-memory kv standing in for ONE community's host-scoped kv; `fail` simulates a kv outage
/// or a missing community context (the host kv errors in both cases).
#[derive(Default)]
struct FakeKv {
    map: Mutex<HashMap<String, Vec<u8>>>,
    fail: bool,
}

impl ConfigStore for FakeKv {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        if self.fail {
            return Err("kv unavailable".into());
        }
        Ok(self.map.lock().unwrap().get(key).cloned())
    }
    fn set(&self, key: &str, value: &[u8]) -> Result<(), String> {
        if self.fail {
            return Err("kv unavailable".into());
        }
        self.map
            .lock()
            .unwrap()
            .insert(key.to_string(), value.to_vec());
        Ok(())
    }
}

/// Records pushes so tests can assert what reached the transport.
#[derive(Default)]
struct FakeSink {
    pushes: Mutex<Vec<String>>,
}

impl PushSink for FakeSink {
    fn push(&self, widget_id: &str, _k: WidgetOutputKind, _f: &str) -> Result<(), HostStoreError> {
        self.pushes.lock().unwrap().push(widget_id.to_string());
        Ok(())
    }
}

fn community(kv: &Arc<FakeKv>, sink: &Arc<FakeSink>) -> ToolRegistry {
    let kv: Arc<dyn ConfigStore> = Arc::clone(kv) as Arc<dyn ConfigStore>;
    let sink: Arc<dyn PushSink> = Arc::clone(sink) as Arc<dyn PushSink>;
    build_registry(Arc::new(KvHostStore::new(kv, sink)))
}

fn fresh() -> (Arc<FakeKv>, Arc<FakeSink>, ToolRegistry) {
    let kv = Arc::new(FakeKv::default());
    let sink = Arc::new(FakeSink::default());
    let reg = community(&kv, &sink);
    (kv, sink, reg)
}

/// Calls a tool with a host-style `__ctx`; `actor = None` models a non-user-originated call.
fn call_as(
    reg: &ToolRegistry,
    name: &str,
    mut args: Value,
    actor: Option<&str>,
    admin: bool,
) -> Result<Value, ToolError> {
    if !args.is_object() {
        args = json!({});
    }
    args.as_object_mut().unwrap().insert(
        waddle_sdk::tool::CTX_ARG.to_string(),
        json!({
            "actor": actor,
            "scopes": if admin && actor.is_some() { vec![ADMIN_SCOPE] } else { vec![] }
        }),
    );
    reg.call(name, &serde_json::to_vec(&args).unwrap())
        .map(|b| serde_json::from_slice(&b).unwrap())
}

fn widget(id: &str) -> Value {
    json!({ "widget_id": id, "display_name": "W", "output_kind": "sanitized_html" })
}

fn push_args(id: &str) -> Value {
    json!({ "widget_id": id, "output_kind": "sanitized_html", "html": "<b>x</b>" })
}

#[test]
fn register_and_list_widgets_as_admin() {
    let (_kv, _s, reg) = fresh();
    let res = call_as(&reg, "register_widget", widget("dj.a"), Some(ADMIN_A), true).unwrap();
    assert_eq!(res["registered"], true);
    let list = call_as(
        &reg,
        "list_registered_widgets",
        json!({}),
        Some(ADMIN_A),
        true,
    )
    .unwrap();
    assert_eq!(list["widgets"].as_array().unwrap().len(), 1);
    // Ownership is the verified actor, never a literal like "system".
    assert_eq!(list["widgets"][0]["bundle_app_id"], ADMIN_A);
}

/// Finding 1: registration without admin scope was allowed (owner fell back to "system").
#[test]
fn register_requires_admin_scope_and_verified_actor() {
    let (_kv, _s, reg) = fresh();
    let e = call_as(&reg, "register_widget", widget("dj.a"), Some(MEMBER), false).unwrap_err();
    assert_eq!(e, ToolError::Forbidden);
    let e = call_as(&reg, "register_widget", widget("dj.a"), None, false).unwrap_err();
    assert_eq!(e, ToolError::Forbidden);
}

/// Finding 1: a caller-supplied `bundle_app_id` was trusted as the owner.
#[test]
fn register_rejects_caller_supplied_owner() {
    let (_kv, _s, reg) = fresh();
    let mut args = widget("dj.a");
    args["bundle_app_id"] = json!("waddles.core.example.music");
    let e = call_as(&reg, "register_widget", args, Some(ADMIN_A), true).unwrap_err();
    assert!(matches!(e, ToolError::InvalidArgs(_)));
}

/// Finding 1: any caller could overwrite another bundle's widget_id (hijack).
#[test]
fn register_refuses_overwriting_another_owners_widget() {
    let (_kv, _s, reg) = fresh();
    call_as(&reg, "register_widget", widget("dj.a"), Some(ADMIN_A), true).unwrap();
    let e = call_as(&reg, "register_widget", widget("dj.a"), Some(ADMIN_B), true).unwrap_err();
    assert_eq!(e, ToolError::Forbidden);
    // The owner may still update its own widget.
    call_as(&reg, "register_widget", widget("dj.a"), Some(ADMIN_A), true).unwrap();
}

#[test]
fn register_validates_widget_id_charset() {
    let (_kv, _s, reg) = fresh();
    for bad in ["", "Has Space", "UPPER", "a:b", "-lead"] {
        let e = call_as(&reg, "register_widget", widget(bad), Some(ADMIN_A), true).unwrap_err();
        assert!(matches!(e, ToolError::InvalidArgs(_)), "{bad:?}");
    }
}

/// Finding 2: pushing to an unregistered widget was allowed.
#[test]
fn push_unregistered_widget_is_denied() {
    let (_kv, sink, reg) = fresh();
    let e = call_as(&reg, "push_widget", push_args("nope"), Some(ADMIN_A), true).unwrap_err();
    assert_eq!(e, ToolError::Forbidden);
    assert!(sink.pushes.lock().unwrap().is_empty());
}

/// Finding 2: with no verified actor the ownership check was skipped entirely.
#[test]
fn push_without_verified_actor_is_denied() {
    let (_kv, sink, reg) = fresh();
    call_as(&reg, "register_widget", widget("dj.a"), Some(ADMIN_A), true).unwrap();
    let e = call_as(&reg, "push_widget", push_args("dj.a"), None, false).unwrap_err();
    assert_eq!(e, ToolError::Forbidden);
    assert!(sink.pushes.lock().unwrap().is_empty());
}

/// Finding 2: a non-owner, non-admin actor must not push.
#[test]
fn push_by_non_owner_is_denied_owner_and_admin_allowed() {
    let (_kv, sink, reg) = fresh();
    call_as(&reg, "register_widget", widget("dj.a"), Some(ADMIN_A), true).unwrap();
    let e = call_as(&reg, "push_widget", push_args("dj.a"), Some(MEMBER), false).unwrap_err();
    assert_eq!(e, ToolError::Forbidden);
    call_as(&reg, "push_widget", push_args("dj.a"), Some(ADMIN_A), false).unwrap();
    call_as(&reg, "push_widget", push_args("dj.a"), Some(ADMIN_B), true).unwrap();
    assert_eq!(sink.pushes.lock().unwrap().len(), 2);
}

/// Finding 2: a lookup error was swallowed (`if let Ok(..)`) and the push went through.
#[test]
fn push_fails_closed_on_lookup_error() {
    let kv = Arc::new(FakeKv {
        fail: true,
        ..Default::default()
    });
    let sink = Arc::new(FakeSink::default());
    let reg = community(&kv, &sink);
    let e = call_as(&reg, "push_widget", push_args("dj.a"), Some(ADMIN_A), true).unwrap_err();
    assert_eq!(e, ToolError::TargetUnavailable);
    assert!(sink.pushes.lock().unwrap().is_empty());
}

#[test]
fn push_enforces_registered_output_kind_and_html_cap() {
    let (_kv, _s, reg) = fresh();
    call_as(&reg, "register_widget", widget("dj.a"), Some(ADMIN_A), true).unwrap();
    let mut mismatched = push_args("dj.a");
    mismatched["output_kind"] = json!("text");
    let e = call_as(&reg, "push_widget", mismatched, Some(ADMIN_A), true).unwrap_err();
    assert!(matches!(e, ToolError::InvalidArgs(_)));
    let mut big = push_args("dj.a");
    big["html"] = json!("x".repeat(MAX_WIDGET_HTML_BYTES + 1));
    let e = call_as(&reg, "push_widget", big, Some(ADMIN_A), true).unwrap_err();
    assert!(matches!(e, ToolError::InvalidArgs(_)));
}

/// Finding 3: a process-wide static registry/store was shared by every community. State must
/// follow the (per-community) kv: two communities are isolated, and a registry rebuilt over the
/// same kv sees the same state (so nothing lives in guest memory).
#[test]
fn state_is_isolated_per_community_kv_and_not_held_in_guest_memory() {
    let sink = Arc::new(FakeSink::default());
    let kv_a = Arc::new(FakeKv::default());
    let kv_b = Arc::new(FakeKv::default());
    let reg_a = community(&kv_a, &sink);
    let reg_b = community(&kv_b, &sink);

    call_as(
        &reg_a,
        "register_widget",
        widget("dj.a"),
        Some(ADMIN_A),
        true,
    )
    .unwrap();
    call_as(
        &reg_a,
        "set_canvas_theme",
        json!({"resolution": "4k"}),
        Some(ADMIN_A),
        true,
    )
    .unwrap();

    let list_b = call_as(
        &reg_b,
        "list_registered_widgets",
        json!({}),
        Some(ADMIN_B),
        true,
    )
    .unwrap();
    assert!(list_b["widgets"].as_array().unwrap().is_empty());
    let layout_b = call_as(&reg_b, "list_components", json!({}), Some(ADMIN_B), true).unwrap();
    assert_ne!(layout_b["canvas"]["resolution"], "4k");
    // B cannot push to A's widget id: it does not exist in B's community.
    let e = call_as(
        &reg_b,
        "push_widget",
        push_args("dj.a"),
        Some(ADMIN_B),
        true,
    )
    .unwrap_err();
    assert_eq!(e, ToolError::Forbidden);

    let reg_a2 = community(&kv_a, &sink);
    let list_a = call_as(
        &reg_a2,
        "list_registered_widgets",
        json!({}),
        Some(ADMIN_A),
        true,
    )
    .unwrap();
    assert_eq!(list_a["widgets"].as_array().unwrap().len(), 1);
}

/// Finding 3: no community context (host kv errors) must fail closed on every state tool.
#[test]
fn no_community_context_fails_closed() {
    let kv = Arc::new(FakeKv {
        fail: true,
        ..Default::default()
    });
    let reg = community(&kv, &Arc::new(FakeSink::default()));
    for (tool, args) in [
        ("register_widget", widget("dj.a")),
        ("list_registered_widgets", json!({})),
        ("list_components", json!({})),
        ("clear_layout", json!({})),
        ("set_canvas_theme", json!({"theme": "x"})),
    ] {
        let e = call_as(&reg, tool, args, Some(ADMIN_A), true).unwrap_err();
        assert_eq!(e, ToolError::TargetUnavailable, "{tool}");
    }
}

#[test]
fn add_component_requires_admin_scope() {
    let (_kv, _s, reg) = fresh();
    call_as(&reg, "register_widget", widget("dj.a"), Some(ADMIN_A), true).unwrap();
    let add = json!({ "widget_id": "dj.a", "x": 48, "y": 860, "width": 460, "height": 160 });
    let e = call_as(&reg, "add_component", add.clone(), Some(MEMBER), false).unwrap_err();
    assert_eq!(e, ToolError::Forbidden);
    let comp = call_as(&reg, "add_component", add, Some(ADMIN_A), true).unwrap();
    assert_eq!(comp["widget_id"], "dj.a");
    assert_eq!(comp["status"], "active");
}

#[test]
fn component_lifecycle_update_and_remove() {
    let (_kv, _s, reg) = fresh();
    call_as(
        &reg,
        "register_widget",
        widget("alerts.box"),
        Some(ADMIN_A),
        true,
    )
    .unwrap();
    let comp = call_as(
        &reg,
        "add_component",
        json!({ "widget_id": "alerts.box", "x": 660, "y": 120, "width": 600, "height": 220 }),
        Some(ADMIN_A),
        true,
    )
    .unwrap();
    let id = comp["instance_id"].as_str().unwrap().to_string();

    let upd = call_as(
        &reg,
        "update_component",
        json!({ "instance_id": id, "x": 700, "visible": false }),
        Some(ADMIN_A),
        true,
    )
    .unwrap();
    assert_eq!(upd["component"]["x"], 700);
    assert_eq!(upd["component"]["status"], "hidden");

    let visible = call_as(
        &reg,
        "list_components",
        json!({"include_hidden": false}),
        Some(ADMIN_A),
        true,
    )
    .unwrap();
    assert!(visible["components"].as_array().unwrap().is_empty());

    let rm = call_as(
        &reg,
        "remove_component",
        json!({ "instance_id": id }),
        Some(ADMIN_A),
        true,
    )
    .unwrap();
    assert_eq!(rm["removed"], true);
    let all = call_as(&reg, "list_components", json!({}), Some(ADMIN_A), true).unwrap();
    assert!(all["components"].as_array().unwrap().is_empty());
}

#[test]
fn set_canvas_theme_resolution_configuration() {
    let (_kv, _s, reg) = fresh();
    let r = call_as(
        &reg,
        "set_canvas_theme",
        json!({"resolution": "2k", "theme": "cyberpunk"}),
        Some(ADMIN_A),
        true,
    )
    .unwrap();
    assert_eq!(r["canvas"]["width"], 2560);
    assert_eq!(r["canvas"]["height"], 1440);
    let r = call_as(
        &reg,
        "set_canvas_theme",
        json!({"resolution": "4k"}),
        Some(ADMIN_A),
        true,
    )
    .unwrap();
    assert_eq!(r["canvas"]["width"], 3840);
    let e = call_as(
        &reg,
        "set_canvas_theme",
        json!({"resolution": "8k"}),
        Some(ADMIN_A),
        true,
    )
    .unwrap_err();
    assert!(matches!(e, ToolError::InvalidArgs(_)));
    let e = call_as(
        &reg,
        "set_canvas_theme",
        json!({"resolution": "4k"}),
        Some(MEMBER),
        false,
    )
    .unwrap_err();
    assert_eq!(e, ToolError::Forbidden);
}
