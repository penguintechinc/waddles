//! In-memory `Host` fake. Every tool call it sees is validated against the contract fixture
//! (`contract/twitch-clip-tools.json`): arguments against `input_schema`, canned replies against
//! `output_schema`, so a drifting bundle OR a drifting test script fails loudly.
#![allow(dead_code)]

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, VecDeque};

use jsonschema::Validator;
use serde_json::{Value, json};
use waddle_bundle_twitch_clips::logic::{EventIn, Host, HostError, IdentityError};
use waddle_sdk::tool::ToolError;

pub const ACTOR: &str = "0b9d2a6e-7c1f-4a55-9d0e-2a4f6b8c1d3e";
pub const CHANNEL: &str = "12345";
pub const DISCORD_CH: &str = "123456789012345678";

fn contract() -> Value {
    serde_json::from_str(include_str!("../../contract/twitch-clip-tools.json"))
        .expect("fixture parses")
}

pub fn schema_for(tool: &str, which: &str) -> Validator {
    let c = contract();
    let s = c["tools"][tool][which].clone();
    assert!(s.is_object(), "no {which} for {tool} in contract fixture");
    jsonschema::validator_for(&s).expect("schema compiles")
}

pub type Reply = Result<Value, ToolError>;

pub struct FakeHost {
    pub kv: RefCell<HashMap<String, Vec<u8>>>,
    pub now: Cell<u64>,
    pub slept: RefCell<Vec<u64>>,
    pub calls: RefCell<Vec<(String, Value, Option<String>)>>,
    pub replies: RefCell<HashMap<String, VecDeque<Reply>>>,
    pub identity: RefCell<Result<String, IdentityError>>,
    pub logs: RefCell<Vec<String>>,
    pub fail_kv: Cell<bool>,
}

impl FakeHost {
    pub fn new() -> Self {
        Self {
            kv: RefCell::default(),
            now: Cell::new(1_000_000),
            slept: RefCell::default(),
            calls: RefCell::default(),
            replies: RefCell::default(),
            identity: RefCell::new(Ok(ACTOR.to_string())),
            logs: RefCell::default(),
            fail_kv: Cell::new(false),
        }
    }

    /// Queues a reply (validated against the contract at queue time when it is `Ok`).
    pub fn push_reply(&self, tool: &str, r: Reply) {
        if let Ok(v) = &r {
            let val = schema_for(tool, "output_schema");
            let errs: Vec<String> = val.iter_errors(v).map(|e| e.to_string()).collect();
            assert!(
                errs.is_empty(),
                "canned reply for {tool} violates contract: {errs:?}"
            );
        }
        self.replies
            .borrow_mut()
            .entry(tool.to_string())
            .or_default()
            .push_back(r);
    }

    /// Queues a reply WITHOUT contract validation (to simulate a misbehaving service).
    pub fn push_raw(&self, tool: &str, r: Reply) {
        self.replies
            .borrow_mut()
            .entry(tool.to_string())
            .or_default()
            .push_back(r);
    }

    pub fn calls_to(&self, tool: &str) -> Vec<Value> {
        self.calls
            .borrow()
            .iter()
            .filter(|c| c.0 == tool)
            .map(|c| c.1.clone())
            .collect()
    }
}

impl Host for FakeHost {
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, HostError> {
        if self.fail_kv.get() {
            return Err(HostError("down".into()));
        }
        assert!(!key.contains(':'), "kv key must not contain ':' -> {key}");
        Ok(self.kv.borrow().get(key).cloned())
    }
    fn kv_set(&self, key: &str, value: &[u8], _ttl: u32) -> Result<(), HostError> {
        assert!(!key.contains(':'), "kv key must not contain ':' -> {key}");
        self.kv.borrow_mut().insert(key.to_string(), value.to_vec());
        Ok(())
    }
    fn kv_delete(&self, key: &str) -> Result<(), HostError> {
        self.kv.borrow_mut().remove(key);
        Ok(())
    }
    fn kv_increment(&self, key: &str, delta: i64, _ttl: u32) -> Result<i64, HostError> {
        if self.fail_kv.get() {
            return Err(HostError("down".into()));
        }
        assert!(!key.contains(':'), "kv key must not contain ':' -> {key}");
        let mut kv = self.kv.borrow_mut();
        let cur = kv
            .get(key)
            .and_then(|b| String::from_utf8(b.clone()).ok())
            .and_then(|s| s.parse::<i64>().ok())
            .unwrap_or(0);
        let next = cur + delta;
        kv.insert(key.to_string(), next.to_string().into_bytes());
        Ok(next)
    }
    fn now_millis(&self) -> u64 {
        self.now.get()
    }
    fn sleep_ms(&self, ms: u64) {
        self.slept.borrow_mut().push(ms);
        self.now.set(self.now.get() + ms);
    }
    fn resolve_actor(&self) -> Result<String, IdentityError> {
        self.identity.borrow().clone()
    }
    fn invoke_tool(
        &self,
        tool: &str,
        args: &Value,
        idem: Option<&str>,
    ) -> Result<Value, ToolError> {
        let val = schema_for(tool, "input_schema");
        let errs: Vec<String> = val.iter_errors(args).map(|e| e.to_string()).collect();
        assert!(
            errs.is_empty(),
            "bundle sent {tool} args violating contract: {errs:?} args={args}"
        );
        let write = contract()["tools"][tool]["access"] == "write";
        assert_eq!(
            write,
            idem.is_some(),
            "idempotency key iff write tool ({tool})"
        );
        self.calls
            .borrow_mut()
            .push((tool.to_string(), args.clone(), idem.map(str::to_string)));
        self.replies
            .borrow_mut()
            .get_mut(tool)
            .and_then(VecDeque::pop_front)
            .unwrap_or_else(|| panic!("no canned reply left for {tool}"))
    }
    fn log(&self, level: &str, op: &str, outcome: &str) {
        self.logs
            .borrow_mut()
            .push(format!("{level}:{op}:{outcome}"));
    }
}

pub fn chat(text: &str, mid: &str, badges: Value) -> EventIn {
    let mut p = json!({"text": text, "channel_id": CHANNEL, "is_mod": false, "is_broadcaster": false,
                       "is_vip": false, "is_subscriber": false});
    for (k, v) in badges.as_object().cloned().unwrap_or_default() {
        p[k] = v;
    }
    EventIn {
        platform: "twitch".into(),
        event_type: "chat.message".into(),
        actor: Some(ACTOR.into()),
        payload: p,
        message_id: mid.into(),
        config_json: "{}".into(),
    }
}

pub fn mod_chat(text: &str, mid: &str) -> EventIn {
    chat(text, mid, json!({"is_mod": true}))
}

pub fn admin(action: &str, args: Value, cfg: &str) -> EventIn {
    EventIn {
        platform: "hub".into(),
        event_type: "bundle.admin".into(),
        actor: Some(ACTOR.into()),
        payload: json!({"action": action, "args": args}),
        message_id: "admin-1".into(),
        config_json: cfg.into(),
    }
}

pub fn clip(id: &str) -> Value {
    json!({"clip_id": id, "url": format!("https://clips.twitch.tv/{id}"), "title": "Nice", "created_at": "2026-11-07T00:00:00Z"})
}

pub fn ready(id: &str) -> Value {
    json!({"ready": true, "clip": clip(id)})
}

pub fn not_ready() -> Value {
    json!({"ready": false})
}
