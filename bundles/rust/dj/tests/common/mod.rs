#![allow(dead_code)]
//! Shared test host: in-memory kv + a contract-enforcing fake for the tool
//! services. Schemas are DERIVED at compile time from the shared contract
//! files (`bundles/contracts/{music-tool-contract,ai-tools}.v1.json`) via
//! `include_str!`: every consumed-tool call is validated (args AND the fake's
//! own result) against them, so a contract change breaks this build instead of
//! drifting silently. Spotify player + economy have no shared contract file;
//! their minimal schemas (taken from the producers' manifests) are inline.

use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;

use serde_json::{Value, json};
use waddle_bundle_dj_app::engine::{Ctx, Engine};
use waddle_bundle_dj_app::host::{Host, Level, ToolErr};

pub const USER_A: &str = "11111111-1111-4111-8111-111111111111";
pub const USER_B: &str = "22222222-2222-4222-8222-222222222222";

pub const MUSIC_CONTRACT: &str = include_str!("../../../../contracts/music-tool-contract.v1.json");
pub const AI_CONTRACT: &str = include_str!("../../../../contracts/ai-tools.v1.json");

/// Subset JSON-schema validator (type, enum, required, properties,
/// additionalProperties, items, min/max, min/maxLength, min/maxItems,
/// uniqueItems, local `#/definitions/..` $ref).
pub fn validate(schema: &Value, root: &Value, v: &Value, path: &str) -> Result<(), String> {
    if let Some(r) = schema.get("$ref").and_then(Value::as_str) {
        let name = r.trim_start_matches("#/definitions/");
        return validate(&root["definitions"][name], root, v, path);
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
    if let Some(s) = v.as_str() {
        let len = s.chars().count() as u64;
        if let Some(m) = schema.get("maxLength").and_then(Value::as_u64)
            && len > m
        {
            return Err(format!("{path}: string longer than {m}"));
        }
        if let Some(m) = schema.get("minLength").and_then(Value::as_u64)
            && len < m
        {
            return Err(format!("{path}: string shorter than {m}"));
        }
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
        if schema.get("uniqueItems") == Some(&json!(true)) {
            for (i, x) in a.iter().enumerate() {
                if a[..i].contains(x) {
                    return Err(format!("{path}: duplicate item {x}"));
                }
            }
        }
        if let Some(items) = schema.get("items") {
            for (i, x) in a.iter().enumerate() {
                validate(items, root, x, &format!("{path}[{i}]"))?;
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
                Some(s) => validate(s, root, x, &format!("{path}.{k}"))?,
                None if schema.get("additionalProperties") == Some(&json!(false)) => {
                    return Err(format!("{path}: unexpected property '{k}'"));
                }
                None => {}
            }
        }
    }
    Ok(())
}

/// A catalog row in the provider wire shape (`music-tool-contract.v1`);
/// `channel_id` is an optional extra provider field.
pub fn track(
    provider: &str,
    id: &str,
    title: &str,
    artist: &str,
    channel: &str,
    dur: u32,
) -> Value {
    json!({
        "provider": provider, "id": id, "title": title, "artist": artist,
        "duration_ms": u64::from(dur) * 1000, "thumbnail": null, "explicit": false,
        "source_url": format!("https://example.test/{id}"), "channel_id": channel
    })
}

fn capability() -> Value {
    json!({"playback": "embed", "server_audio": false, "queue_add": false})
}

/// One recorded tool call: (target, tool, args, idempotency key).
pub type Call = (String, String, Value, Option<String>);

pub struct FakeHost {
    pub kv: RefCell<BTreeMap<String, Vec<u8>>>,
    pub now: Cell<u64>,
    /// Tracks searchable/resolvable (all providers).
    pub catalog: RefCell<Vec<Value>>,
    pub calls: RefCell<Vec<Call>>,
    pub logs: RefCell<Vec<String>>,
    /// Scripted `ai.complete` text outputs (popped front).
    pub ai_complete: RefCell<Vec<String>>,
    /// `ai.classify`: (label, confidence) or None => ToolErr::TargetUnavailable.
    pub ai_classify: RefCell<Option<(String, f64)>>,
    pub fail_tools: RefCell<Vec<String>>,
    pub balance: Cell<i64>,
    /// When > 0, `playlist.get` serves pages of this many tracks.
    pub page_size: Cell<usize>,
    music: Value,
    ai: Value,
}

impl FakeHost {
    pub fn new() -> Self {
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
            page_size: Cell::new(0),
            music: serde_json::from_str(MUSIC_CONTRACT).expect("music contract parses"),
            ai: serde_json::from_str(AI_CONTRACT).expect("ai contract parses"),
        }
    }

    pub fn add(&self, t: Value) {
        self.catalog.borrow_mut().push(t);
    }

    fn check_schema(&self, root: &Value, schema: &Value, what: &str, v: &Value) {
        if let Err(e) = validate(schema, root, v, what) {
            panic!("contract violation ({what}): {e}\nvalue: {v}");
        }
    }

    fn check_music(&self, op: &str, part: &str, v: &Value) {
        let s = &self.music["tools"][op][format!("{part}_schema")];
        assert!(!s.is_null(), "no music contract for op {op}");
        self.check_schema(&self.music, s, &format!("music.{op}.{part}"), v);
    }

    fn check_ai(&self, tool: &str, part: &str, v: &Value) {
        let s = &self.ai["tools"][tool][format!("{part}_schema")];
        assert!(!s.is_null(), "no ai contract for {tool}");
        self.check_schema(&self.ai, s, &format!("{tool}.{part}"), v);
    }

    pub fn calls_to(&self, tool: &str) -> usize {
        self.calls.borrow().iter().filter(|c| c.1 == tool).count()
    }

    fn music_call(&self, provider: &str, op: &str, args: &Value) -> Result<Value, ToolErr> {
        self.check_music(op, "input", args);
        let all: Vec<Value> = self
            .catalog
            .borrow()
            .iter()
            .filter(|t| t["provider"] == provider)
            .cloned()
            .collect();
        let result = match op {
            "search" => {
                let q = args["query"].as_str().unwrap().to_lowercase();
                let hits: Vec<Value> = all
                    .into_iter()
                    .filter(|t| {
                        q.split(' ').all(|w| {
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
                    .collect();
                json!({"tracks": hits, "capability": capability()})
            }
            "resolve" => {
                let u = args["url"].as_str().unwrap();
                match all.iter().find(|t| u.contains(t["id"].as_str().unwrap())) {
                    Some(t) => json!({"track": t, "capability": capability()}),
                    None => return Err(ToolErr::InvalidArgs("not_found".into())),
                }
            }
            "playlist.get" => {
                let size = self.page_size.get();
                let page: usize = args
                    .get("page_token")
                    .and_then(Value::as_str)
                    .and_then(|t| t.strip_prefix('p'))
                    .and_then(|n| n.parse().ok())
                    .unwrap_or(0);
                if size == 0 {
                    json!({"tracks": all, "next_page_token": null, "capability": capability()})
                } else {
                    let chunk: Vec<Value> =
                        all.iter().skip(page * size).take(size).cloned().collect();
                    let next = if (page + 1) * size < all.len() {
                        json!(format!("p{}", page + 1))
                    } else {
                        Value::Null
                    };
                    json!({"tracks": chunk, "next_page_token": next, "capability": capability()})
                }
            }
            other => panic!("dj must not call music op {other}"),
        };
        self.check_music(op, "output", &result);
        Ok(result)
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
        if let Some(rest) = tool.strip_prefix("music.") {
            let (provider, op) = rest.split_once('.').expect("music.<provider>.<op>");
            assert_eq!(target, format!("waddles.core.service.music-{provider}"));
            if provider == "spotify" && op == "player.play" {
                assert!(
                    idem.is_some(),
                    "player.play is a write: needs an idempotency key"
                );
                let uris = args["uris"].as_array().expect("uris array");
                assert!(
                    uris.iter()
                        .all(|u| u.as_str().unwrap().starts_with("spotify:track:"))
                );
                return Ok(json!({"ok": true, "action": "play", "capability": capability()}));
            }
            return self.music_call(provider, op, args);
        }
        let result = match tool {
            "ai.complete" => {
                assert_eq!(target, "waddles.core.service.ai");
                self.check_ai("ai.complete", "input", args);
                let text = self.ai_complete.borrow_mut().remove(0);
                json!({"text": text})
            }
            "ai.classify" => {
                assert_eq!(target, "waddles.core.service.ai");
                self.check_ai("ai.classify", "input", args);
                match self.ai_classify.borrow().clone() {
                    Some((l, c)) => json!({"label": l, "confidence": c, "reason": "scripted"}),
                    None => return Err(ToolErr::TargetUnavailable),
                }
            }
            "adjust_balance" => {
                // Producer: bundles/rust/economy (`adjust_balance {user, delta}`; delta<0 debits,
                // insufficient funds is a tool error; writes need an idempotency key).
                assert_eq!(target, "waddles.core.service.economy");
                assert!(idem.is_some(), "writes need an idempotency key");
                let keys: Vec<&String> = args.as_object().unwrap().keys().collect();
                assert_eq!(keys.len(), 2, "only user+delta: {keys:?}");
                let d = args["delta"].as_i64().unwrap();
                assert!(d != 0 && args["user"].as_str().unwrap().len() == 36);
                if self.balance.get() + d < 0 {
                    return Err(ToolErr::InvalidArgs("insufficient_funds".into()));
                }
                self.balance.set(self.balance.get() + d);
                json!({"user": args["user"], "balance": self.balance.get(), "alias": "wb"})
            }
            _ => panic!("unexpected tool call {target} {tool}"),
        };
        if tool.starts_with("ai.") {
            self.check_ai(tool, "output", &result);
        }
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
