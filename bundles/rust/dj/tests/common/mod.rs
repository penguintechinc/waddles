//! Shared test host: in-memory kv + a contract-enforcing fake for the tool
//! services. Every consumed-tool call is validated against
//! `tests/fixtures/contract.json` (args AND the fake's own result), so a
//! schema drift on either side fails here.

use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;

use serde_json::{Value, json};
use waddle_bundle_dj_app::engine::{Ctx, Engine};
use waddle_bundle_dj_app::host::{Host, Level, ToolErr};

pub const USER_A: &str = "11111111-1111-4111-8111-111111111111";
pub const USER_B: &str = "22222222-2222-4222-8222-222222222222";

/// Subset JSON-schema validator (type, enum, required, properties,
/// additionalProperties, items, min/max, maxLength, maxItems, minItems, $ref).
pub fn validate(schema: &Value, defs: &Value, v: &Value, path: &str) -> Result<(), String> {
    if let Some(r) = schema.get("$ref").and_then(Value::as_str) {
        return validate(&defs[r], defs, v, path);
    }
    if let Some(t) = schema.get("type") {
        let types: Vec<&str> = match t {
            Value::String(s) => vec![s.as_str()],
            Value::Array(a) => a.iter().filter_map(Value::as_str).collect(),
            _ => vec![],
        };
        let ok = types.iter().any(|t| match *t {
            "object" => v.is_object(),
            "array" => v.is_array(),
            "string" => v.is_string(),
            "integer" => v.is_i64() || v.is_u64(),
            "number" => v.is_number(),
            "boolean" => v.is_boolean(),
            "null" => v.is_null(),
            _ => false,
        });
        if !ok {
            return Err(format!("{path}: expected {types:?}, got {v}"));
        }
    }
    if let Some(e) = schema.get("enum").and_then(Value::as_array)
        && !e.contains(v)
    {
        return Err(format!("{path}: {v} not in enum"));
    }
    if let (Some(n), Some(min)) = (v.as_f64(), schema.get("minimum").and_then(Value::as_f64))
        && n < min
    {
        return Err(format!("{path}: {n} < {min}"));
    }
    if let (Some(n), Some(max)) = (v.as_f64(), schema.get("maximum").and_then(Value::as_f64))
        && n > max
    {
        return Err(format!("{path}: {n} > {max}"));
    }
    if let (Some(s), Some(m)) = (v.as_str(), schema.get("maxLength").and_then(Value::as_u64))
        && s.chars().count() as u64 > m
    {
        return Err(format!("{path}: string longer than {m}"));
    }
    if let Some(a) = v.as_array() {
        if let Some(m) = schema.get("maxItems").and_then(Value::as_u64)
            && a.len() as u64 > m
        {
            return Err(format!("{path}: more than {m} items"));
        }
        if let Some(m) = schema.get("minItems").and_then(Value::as_u64)
            && (a.len() as u64) < m
        {
            return Err(format!("{path}: fewer than {m} items"));
        }
        if let Some(items) = schema.get("items") {
            for (i, x) in a.iter().enumerate() {
                validate(items, defs, x, &format!("{path}[{i}]"))?;
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
                return Err(format!("{path}: missing required '{r}'"));
            }
        }
        let props = schema.get("properties").and_then(Value::as_object);
        for (k, x) in o {
            match props.and_then(|p| p.get(k)) {
                Some(s) => validate(s, defs, x, &format!("{path}.{k}"))?,
                None if schema.get("additionalProperties") == Some(&json!(false)) => {
                    return Err(format!("{path}: unexpected property '{k}'"));
                }
                None => {}
            }
        }
    }
    Ok(())
}

/// A catalog row served by the fake music/ai services.
pub fn track(
    provider: &str,
    id: &str,
    title: &str,
    artist: &str,
    channel: &str,
    dur: u32,
) -> Value {
    json!({"provider": provider, "id": id, "title": title, "artist": artist, "channel_id": channel, "duration_s": dur, "explicit": false})
}

/// One recorded tool call: (target, tool, args, idempotency key).
pub type Call = (String, String, Value, Option<String>);

pub struct FakeHost {
    pub kv: RefCell<BTreeMap<String, Vec<u8>>>,
    pub now: Cell<u64>,
    /// provider -> tracks searchable/resolvable.
    pub catalog: RefCell<Vec<Value>>,
    pub calls: RefCell<Vec<Call>>,
    pub logs: RefCell<Vec<String>>,
    /// Scripted `ai.complete` text outputs (popped front).
    pub ai_complete: RefCell<Vec<String>>,
    /// `ai.classify`: (label, confidence) or None => ToolErr::TargetUnavailable.
    pub ai_classify: RefCell<Option<(String, f64)>>,
    pub fail_tools: RefCell<Vec<String>>,
    pub balance: Cell<i64>,
    contract: Value,
}

impl FakeHost {
    pub fn new() -> Self {
        let contract: Value = serde_json::from_str(include_str!("../fixtures/contract.json"))
            .expect("contract fixture parses");
        FakeHost {
            kv: RefCell::default(),
            now: Cell::new(1_000_000),
            catalog: RefCell::default(),
            calls: RefCell::default(),
            logs: RefCell::default(),
            ai_complete: RefCell::default(),
            ai_classify: RefCell::new(Some(("allowed".into(), 0.95))),
            fail_tools: RefCell::default(),
            balance: Cell::new(100),
            contract,
        }
    }

    pub fn add(&self, t: Value) {
        self.catalog.borrow_mut().push(t);
    }

    fn check(&self, key: &str, part: &str, v: &Value) {
        let s = &self.contract["tools"][key][part];
        assert!(!s.is_null(), "no contract for {key}");
        if let Err(e) = validate(s, &self.contract["defs"], v, key) {
            panic!("contract violation ({key}.{part}): {e}\nvalue: {v}");
        }
    }

    pub fn calls_to(&self, tool: &str) -> usize {
        self.calls.borrow().iter().filter(|c| c.1 == tool).count()
    }
}

impl Host for FakeHost {
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        assert!(!key.contains(':'), "kv key must not contain ':' : {key}");
        Ok(self.kv.borrow().get(key).cloned())
    }
    fn kv_set(&self, key: &str, value: &[u8], _ttl: u32) -> Result<(), String> {
        assert!(!key.contains(':'), "kv key must not contain ':' : {key}");
        self.kv.borrow_mut().insert(key.to_string(), value.to_vec());
        Ok(())
    }
    fn kv_delete(&self, key: &str) -> Result<(), String> {
        self.kv.borrow_mut().remove(key);
        Ok(())
    }
    fn now_ms(&self) -> u64 {
        self.now.get()
    }
    fn invoke(
        &self,
        target: &str,
        tool: &str,
        args: &Value,
        idem: Option<&str>,
    ) -> Result<Value, ToolErr> {
        self.calls.borrow_mut().push((
            target.to_string(),
            tool.to_string(),
            args.clone(),
            idem.map(String::from),
        ));
        if self
            .fail_tools
            .borrow()
            .iter()
            .any(|f| tool.starts_with(f.as_str()))
        {
            return Err(ToolErr::TargetUnavailable);
        }
        let mut parts = tool.splitn(3, '.');
        let (ns, provider, op) = (
            parts.next().unwrap_or(""),
            parts.next().unwrap_or(""),
            parts.next().unwrap_or(""),
        );
        let (key, result) = match (ns, op) {
            ("music", "search") => {
                assert_eq!(target, format!("waddles.core.service.music-{provider}"));
                self.check("music.search", "args", args);
                let q = args["query"].as_str().unwrap().to_lowercase();
                let hits: Vec<Value> = self
                    .catalog
                    .borrow()
                    .iter()
                    .filter(|t| {
                        t["provider"] == provider
                            && q.split(' ').all(|w| {
                                format!(
                                    "{} {}",
                                    t["title"].as_str().unwrap(),
                                    t["artist"].as_str().unwrap()
                                )
                                .to_lowercase()
                                .contains(w)
                            })
                    })
                    .take(args["limit"].as_u64().unwrap() as usize)
                    .cloned()
                    .collect();
                ("music.search", json!({"tracks": hits}))
            }
            ("music", "track") => {
                self.check("music.track", "args", args);
                let r = args["ref"].as_str().unwrap();
                let t = self
                    .catalog
                    .borrow()
                    .iter()
                    .find(|t| t["provider"] == provider && r.contains(t["id"].as_str().unwrap()))
                    .cloned();
                ("music.track", json!({"track": t}))
            }
            ("music", "playlist.get") => {
                self.check("music.playlist.get", "args", args);
                let all: Vec<Value> = self
                    .catalog
                    .borrow()
                    .iter()
                    .filter(|t| t["provider"] == provider)
                    .cloned()
                    .collect();
                ("music.playlist.get", json!({"tracks": all, "title": "pl"}))
            }
            ("music", "playback") => {
                self.check("music.playback", "args", args);
                ("music.playback", json!({"capability": {"embed": true}}))
            }
            ("ai", _) if provider == "complete" => {
                assert_eq!(target, "waddles.core.service.ai");
                self.check("ai.complete", "args", args);
                let text = self.ai_complete.borrow_mut().remove(0);
                ("ai.complete", json!({"text": text}))
            }
            ("ai", _) if provider == "classify" => {
                self.check("ai.classify", "args", args);
                match self.ai_classify.borrow().clone() {
                    Some((l, c)) => ("ai.classify", json!({"label": l, "confidence": c})),
                    None => return Err(ToolErr::TargetUnavailable),
                }
            }
            _ if tool == "adjust_balance" => {
                assert_eq!(target, "waddles.core.service.economy");
                assert!(idem.is_some(), "writes need an idempotency key");
                self.check("economy.adjust_balance", "args", args);
                let d = args["delta"].as_i64().unwrap();
                if self.balance.get() + d < 0 {
                    return Err(ToolErr::InvalidArgs("insufficient_funds".into()));
                }
                self.balance.set(self.balance.get() + d);
                (
                    "economy.adjust_balance",
                    json!({"balance": self.balance.get()}),
                )
            }
            _ => panic!("unexpected tool call {target} {tool}"),
        };
        self.check(key, "result", &result);
        Ok(result)
    }
    fn log(&self, _l: Level, message: &str, fields: Value) {
        self.logs.borrow_mut().push(format!("{message} {fields}"));
    }
}

pub fn user(actor: &str, is_mod: bool, msg: &str) -> Ctx {
    Ctx {
        actor: Some(actor.to_string()),
        is_mod,
        message_id: msg.to_string(),
    }
}

/// Runs one chat line as `ctx` and returns the reply (panics on infra error).
pub fn say(h: &FakeHost, ctx: &Ctx, text: &str) -> String {
    Engine::new(h, ctx)
        .handle(text)
        .expect("no infra error")
        .expect("a dj command")
}
