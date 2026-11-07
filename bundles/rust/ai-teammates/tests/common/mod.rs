//! Test support: a stateful in-memory [`Host`] (kv with TTL on a controllable clock, identity,
//! recorded tool calls) plus a minimal JSON-Schema validator. Every tool call the real logic
//! makes is validated against the `contracts/*.json` fixtures, request AND response.
#![allow(dead_code, clippy::collapsible_if, clippy::type_complexity)]

use std::cell::{Cell, RefCell};
use std::collections::HashMap;

use serde_json::{Value, json};
use waddle_bundle_ai_teammates::host::{Ctx, Host, HostError, Level};
use waddle_sdk::PlatformEvent;
use waddle_sdk::tool::ToolError;

/// Loads a contract fixture by tool name.
pub fn contract(tool: &str) -> Value {
    let path = format!("{}/contracts/{tool}.json", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(
        &std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("no contract for {tool}: {e}")),
    )
    .expect("contract is JSON")
}

/// Minimal JSON-Schema subset validator: type, required, properties, additionalProperties,
/// enum, minimum, maximum, maxLength, minItems, maxItems, items.
pub fn validate(schema: &Value, v: &Value, path: &str) -> Result<(), String> {
    let ty = schema.get("type").and_then(Value::as_str);
    let type_ok = match ty {
        Some("object") => v.is_object(),
        Some("string") => v.is_string(),
        Some("integer") => v.is_i64() || v.is_u64(),
        Some("number") => v.is_number(),
        Some("boolean") => v.is_boolean(),
        Some("array") => v.is_array(),
        None => true,
        Some(other) => return Err(format!("{path}: unsupported schema type {other}")),
    };
    if !type_ok {
        return Err(format!("{path}: expected {ty:?}, got {v}"));
    }
    if let Some(e) = schema.get("enum").and_then(Value::as_array) {
        if !e.contains(v) {
            return Err(format!("{path}: {v} not in enum"));
        }
    }
    if let Some(n) = v.as_f64() {
        if schema
            .get("minimum")
            .and_then(Value::as_f64)
            .is_some_and(|m| n < m)
            || schema
                .get("maximum")
                .and_then(Value::as_f64)
                .is_some_and(|m| n > m)
        {
            return Err(format!("{path}: {n} out of range"));
        }
    }
    if let (Some(s), Some(max)) = (v.as_str(), schema.get("maxLength").and_then(Value::as_u64)) {
        if s.chars().count() as u64 > max {
            return Err(format!("{path}: string longer than {max}"));
        }
    }
    if let Some(a) = v.as_array() {
        if schema
            .get("minItems")
            .and_then(Value::as_u64)
            .is_some_and(|m| (a.len() as u64) < m)
            || schema
                .get("maxItems")
                .and_then(Value::as_u64)
                .is_some_and(|m| a.len() as u64 > m)
        {
            return Err(format!("{path}: array length {} out of range", a.len()));
        }
        if let Some(items) = schema.get("items") {
            for (i, x) in a.iter().enumerate() {
                validate(items, x, &format!("{path}[{i}]"))?;
            }
        }
    }
    if let Some(o) = v.as_object() {
        for r in schema
            .get("required")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            if !o.contains_key(r) {
                return Err(format!("{path}: missing required `{r}`"));
            }
        }
        let props = schema.get("properties").and_then(Value::as_object);
        for (k, x) in o {
            match props.and_then(|p| p.get(k)) {
                Some(s) => validate(s, x, &format!("{path}.{k}"))?,
                None if schema.get("additionalProperties") == Some(&Value::Bool(false)) => {
                    return Err(format!("{path}: unexpected field `{k}`"));
                }
                None => {}
            }
        }
    }
    Ok(())
}

type Handler = Box<dyn Fn(&Value) -> Result<Value, ToolError>>;

pub struct FakeHost {
    pub now: Cell<u64>,
    kv: RefCell<HashMap<String, (Vec<u8>, Option<u64>)>>,
    pub actor: RefCell<Result<String, HostError>>,
    handlers: RefCell<HashMap<String, Handler>>,
    pub calls: RefCell<Vec<(String, Value)>>,
    pub logs: RefCell<Vec<String>>,
    pub kv_down: Cell<bool>,
}

impl FakeHost {
    pub fn new() -> Self {
        Self {
            now: Cell::new(1_800_000_000_000),
            kv: RefCell::default(),
            actor: RefCell::new(Ok("uuid-actor-1".to_string())),
            handlers: RefCell::default(),
            calls: RefCell::default(),
            logs: RefCell::default(),
            kv_down: Cell::new(false),
        }
    }

    pub fn advance_min(&self, m: u64) {
        self.now.set(self.now.get() + m * 60_000);
    }

    pub fn on(&self, tool: &str, f: impl Fn(&Value) -> Result<Value, ToolError> + 'static) {
        self.handlers
            .borrow_mut()
            .insert(tool.to_string(), Box::new(f));
    }

    pub fn calls_to(&self, tool: &str) -> Vec<Value> {
        self.calls
            .borrow()
            .iter()
            .filter(|(t, _)| t == tool)
            .map(|(_, a)| a.clone())
            .collect()
    }

    pub fn kv_raw(&self, key: &str) -> Option<Vec<u8>> {
        self.kv_get(key).ok().flatten()
    }

    fn live(&self, key: &str) -> Option<Vec<u8>> {
        let now = self.now.get();
        let kv = self.kv.borrow();
        kv.get(key)
            .filter(|(_, exp)| exp.is_none_or(|e| e > now))
            .map(|(v, _)| v.clone())
    }
}

impl Host for FakeHost {
    fn now_millis(&self) -> u64 {
        self.now.get()
    }
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, HostError> {
        assert!(!key.contains(':'), "kv key {key:?} must not contain ':'");
        if self.kv_down.get() {
            return Err(HostError::Backend("down".into()));
        }
        Ok(self.live(key))
    }
    fn kv_set(&self, key: &str, value: &[u8], ttl: u32) -> Result<(), HostError> {
        assert!(!key.contains(':'), "kv key {key:?} must not contain ':'");
        if self.kv_down.get() {
            return Err(HostError::Backend("down".into()));
        }
        let exp = (ttl > 0).then(|| self.now.get() + u64::from(ttl) * 1000);
        self.kv
            .borrow_mut()
            .insert(key.to_string(), (value.to_vec(), exp));
        Ok(())
    }
    fn kv_incr(&self, key: &str, delta: i64, ttl: u32) -> Result<i64, HostError> {
        assert!(!key.contains(':'), "kv key {key:?} must not contain ':'");
        if self.kv_down.get() {
            return Err(HostError::Backend("down".into()));
        }
        let cur = self
            .live(key)
            .and_then(|b| String::from_utf8(b).ok())
            .and_then(|s| s.parse::<i64>().ok());
        let exp_existing = self.kv.borrow().get(key).and_then(|(_, e)| *e);
        let next = cur.unwrap_or(0) + delta;
        let exp = if cur.is_some() {
            exp_existing
        } else {
            (ttl > 0).then(|| self.now.get() + u64::from(ttl) * 1000)
        };
        self.kv
            .borrow_mut()
            .insert(key.to_string(), (next.to_string().into_bytes(), exp));
        Ok(next)
    }
    fn resolve_actor(&self) -> Result<String, HostError> {
        self.actor.borrow().clone()
    }
    fn invoke(
        &self,
        target: &str,
        tool: &str,
        args: &[u8],
        _idem: Option<&str>,
    ) -> Result<Vec<u8>, ToolError> {
        let c = contract(tool);
        assert_eq!(
            c["target"],
            json!(target),
            "{tool} called on the wrong service"
        );
        let args: Value = serde_json::from_slice(args).expect("args are JSON");
        validate(&c["request"], &args, "request")
            .unwrap_or_else(|e| panic!("{tool} request violates contract: {e}"));
        self.calls
            .borrow_mut()
            .push((tool.to_string(), args.clone()));
        let handlers = self.handlers.borrow();
        let handler = handlers
            .get(tool)
            .unwrap_or_else(|| panic!("no handler for {tool}"));
        let out = handler(&args)?;
        validate(&c["response"], &out, "response")
            .unwrap_or_else(|e| panic!("{tool} fake response violates contract: {e}"));
        Ok(serde_json::to_vec(&out).expect("json"))
    }
    fn log(&self, _level: Level, _msg: &str, fields_json: &str) {
        self.logs.borrow_mut().push(fields_json.to_string());
    }
}

pub fn ctx(config_json: &str) -> Ctx {
    Ctx {
        community: Some("7".into()),
        message_id: "msg-1".into(),
        config_json: config_json.into(),
    }
}

pub fn chat(platform: &str, channel: &str, text: &str, moderator: bool) -> PlatformEvent {
    let mut p = json!({"text": text, "channel_id": channel});
    if moderator {
        p["is_mod"] = json!(true);
    }
    PlatformEvent {
        platform: platform.into(),
        event_type: "chat.message".into(),
        actor: Some("uuid-actor-1".into()),
        payload_json: p.to_string(),
        occurred_at: "2026-11-07T00:00:00.000Z".into(),
    }
}

pub fn event(platform: &str, ty: &str, payload: Value) -> PlatformEvent {
    PlatformEvent {
        platform: platform.into(),
        event_type: ty.into(),
        actor: None,
        payload_json: payload.to_string(),
        occurred_at: "2026-11-07T00:00:00.000Z".into(),
    }
}

/// Registers happy-path handlers for every consumed tool.
pub fn happy(h: &FakeHost) {
    h.on("ai.complete", |_| {
        Ok(json!({"text": "The hangar opens at noon."}))
    });
    h.on("ai.classify", |_| {
        Ok(json!({"label": "safe", "score": 0.97}))
    });
    h.on("ai.rag.query", |_| Ok(json!({"matches": [{"text": "The hangar opens at noon on event days.", "source": "faq", "score": 0.9}]})));
    for p in ["twitch", "discord", "youtube"] {
        h.on(&format!("{p}.live.channels"), |_| Ok(json!({"channels": [{"channel_id": "chan1", "live": true, "title": "Friday night run", "game": "Star Game"}]})));
    }
}
