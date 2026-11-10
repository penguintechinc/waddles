//! Shared in-memory host for the integration tests. The kv, clock and log are in-memory
//! stand-ins for HOST capabilities; the tool invoker validates every call against the consumed
//! tool's contract fixture and records it, so a payload the real service would refuse fails here.
#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::{HashMap, VecDeque};

use regex::Regex;
use serde_json::{Value, json};
use waddle_bundle_calendar_rust::host::{CalError, Caller, Clock, Ctx, Kv, Logger, ToolInvoker};
use waddle_bundle_calendar_rust::model::Config;
use waddle_sdk::tool::ToolError;

pub const MOD: &str = "11111111-1111-4111-8111-111111111111";
pub const USER: &str = "22222222-2222-4222-8222-222222222222";
pub const USER2: &str = "33333333-3333-4333-8333-333333333333";
pub const NOW: i64 = 1_800_000_000; // 2027-01-15

#[derive(Default)]
pub struct MemKv(pub RefCell<HashMap<String, Vec<u8>>>);

impl Kv for MemKv {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, CalError> {
        assert!(!key.contains(':'), "kv key must not contain ':' -> {key}");
        Ok(self.0.borrow().get(key).cloned())
    }
    fn set(&self, key: &str, value: &[u8], _ttl_seconds: u32) -> Result<(), CalError> {
        assert!(!key.contains(':'), "kv key must not contain ':' -> {key}");
        self.0.borrow_mut().insert(key.to_string(), value.to_vec());
        Ok(())
    }
    fn delete(&self, key: &str) -> Result<(), CalError> {
        self.0.borrow_mut().remove(key);
        Ok(())
    }
    fn increment(&self, key: &str, delta: i64) -> Result<i64, CalError> {
        let mut m = self.0.borrow_mut();
        let cur: i64 = m
            .get(key)
            .and_then(|v| String::from_utf8(v.clone()).ok())
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        let n = cur + delta;
        m.insert(key.to_string(), n.to_string().into_bytes());
        Ok(n)
    }
}

pub struct FixedClock(pub i64);
impl Clock for FixedClock {
    fn now_secs(&self) -> i64 {
        self.0
    }
}

#[derive(Default)]
pub struct CapLog(pub RefCell<Vec<(String, String, Value)>>);
impl Logger for CapLog {
    fn log(&self, level: &str, msg: &str, fields: Value) {
        self.0.borrow_mut().push((level.into(), msg.into(), fields));
    }
}

#[derive(Debug, Clone)]
pub struct Call {
    pub target: String,
    pub tool: String,
    pub args: Value,
    pub key: String,
}

pub struct FakeTools {
    pub calls: RefCell<Vec<Call>>,
    pub script: RefCell<VecDeque<Result<Value, ToolError>>>,
    pub next_remote: RefCell<u64>,
}

impl Default for FakeTools {
    fn default() -> Self {
        FakeTools {
            calls: RefCell::default(),
            script: RefCell::default(),
            next_remote: RefCell::new(1000),
        }
    }
}

/// Minimal JSON-schema subset validator (type/required/properties/additionalProperties/
/// minLength/maxLength/minimum/maximum/pattern) -- exactly what the contracts use.
pub fn validate(schema: &Value, v: &Value, path: &str) -> Result<(), String> {
    let ty = schema["type"].as_str().unwrap_or("object");
    match ty {
        "object" => {
            let o = v.as_object().ok_or(format!("{path}: not an object"))?;
            for r in schema["required"].as_array().into_iter().flatten() {
                let r = r.as_str().unwrap();
                if !o.contains_key(r) {
                    return Err(format!("{path}: missing required `{r}`"));
                }
            }
            let props = schema["properties"].as_object();
            for (k, val) in o {
                match props.and_then(|p| p.get(k)) {
                    Some(s) => validate(s, val, &format!("{path}.{k}"))?,
                    None if schema["additionalProperties"] == json!(false) => {
                        return Err(format!("{path}: unexpected property `{k}`"));
                    }
                    None => {}
                }
            }
            Ok(())
        }
        "string" => {
            let s = v.as_str().ok_or(format!("{path}: not a string"))?;
            let n = s.chars().count() as u64;
            if let Some(m) = schema["minLength"].as_u64()
                && n < m
            {
                return Err(format!("{path}: shorter than {m}"));
            }
            if let Some(m) = schema["maxLength"].as_u64()
                && n > m
            {
                return Err(format!("{path}: longer than {m}"));
            }
            if let Some(p) = schema["pattern"].as_str()
                && !Regex::new(p).unwrap().is_match(s)
            {
                return Err(format!("{path}: does not match {p}"));
            }
            Ok(())
        }
        "integer" => {
            let n = v.as_i64().ok_or(format!("{path}: not an integer"))?;
            if let Some(m) = schema["minimum"].as_i64()
                && n < m
            {
                return Err(format!("{path}: below minimum"));
            }
            if let Some(m) = schema["maximum"].as_i64()
                && n > m
            {
                return Err(format!("{path}: above maximum"));
            }
            Ok(())
        }
        "boolean" => v
            .is_boolean()
            .then_some(())
            .ok_or(format!("{path}: not a boolean")),
        other => Err(format!("{path}: unsupported schema type {other}")),
    }
}

pub fn contract(tool: &str) -> Value {
    let p = format!(
        "{}/contracts/{tool}.schema.json",
        env!("CARGO_MANIFEST_DIR")
    );
    let raw =
        std::fs::read_to_string(&p).unwrap_or_else(|_| panic!("no contract fixture for {tool}"));
    let v: Value = serde_json::from_str(&raw).unwrap();
    v["input_schema"].clone()
}

impl ToolInvoker for FakeTools {
    fn invoke(
        &self,
        target: &str,
        tool: &str,
        args: &Value,
        key: &str,
    ) -> Result<Value, ToolError> {
        validate(&contract(tool), args, "$")
            .unwrap_or_else(|e| panic!("contract violation for {tool}: {e}"));
        self.calls.borrow_mut().push(Call {
            target: target.into(),
            tool: tool.into(),
            args: args.clone(),
            key: key.into(),
        });
        if let Some(r) = self.script.borrow_mut().pop_front() {
            return r;
        }
        if tool.ends_with(".create") {
            let mut n = self.next_remote.borrow_mut();
            *n += 1;
            return Ok(if tool.starts_with("twitch") {
                json!({"segment_id": format!("seg{n}")})
            } else if tool.starts_with("wix") {
                json!({"event_id": format!("wix{n}")})
            } else {
                json!({"event_id": n.to_string()})
            });
        }
        Ok(json!({}))
    }
}

pub struct World {
    pub kv: MemKv,
    pub tools: FakeTools,
    pub clock: FixedClock,
    pub log: CapLog,
}

impl World {
    pub fn new() -> World {
        World {
            kv: MemKv::default(),
            tools: FakeTools::default(),
            clock: FixedClock(NOW),
            log: CapLog::default(),
        }
    }
    pub fn ctx(&self) -> Ctx<'_> {
        Ctx {
            kv: &self.kv,
            tools: &self.tools,
            clock: &self.clock,
            log: &self.log,
            tenant: "tenant-a",
            community: "7",
        }
    }
    pub fn calls(&self) -> Vec<Call> {
        self.tools.calls.borrow().clone()
    }
}

pub fn approver() -> Caller {
    Caller {
        actor: Some(MOD.into()),
        approver: true,
    }
}
pub fn member(uuid: &str) -> Caller {
    Caller {
        actor: Some(uuid.into()),
        approver: false,
    }
}
pub fn cfg_sync(discord: bool) -> Config {
    Config {
        sync_discord: discord,
        ..Config::default()
    }
}
