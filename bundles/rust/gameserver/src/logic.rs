//! Host-independent `gameserver` tool logic.
//!
//! Every tool is a function over one seam, [`Env`] (kv + log + clock + the
//! `gameserver-net` capability), so the whole table is unit-tested on the host
//! and the wasm glue in `lib.rs` only supplies the real implementations.
//!
//! Safety shape (this bundle reaches ARBITRARY customer game servers):
//! * a server must be REGISTERED (its UUID is the host connection id; the host
//!   holds host:port + RCON password and runs the SSRF guard) -- the guest
//!   never sees an address or a secret and cannot name a free-form host:port;
//! * `rcon.exec` is deny-by-default: the command must match the per-server
//!   allowlist (exact line or `verb *`), is single-line, <= 256 bytes;
//! * the caller must be a verified moderator/admin (`ToolContext`, never args);
//! * per-community AND per-server fixed-window rate limits (kv TTL counters);
//! * every exec attempt -- allowed or refused -- is audit-logged (command text,
//!   actor UUID, outcome; never a secret, never the server's output);
//! * output is capped and sanitized; unavailable capability = loud error.

use std::collections::BTreeSet;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use waddle_sdk::tool::{ToolContext, ToolError, ToolRegistry};

use crate::net::{GameNet, NetError, Scheme};
use crate::proto::{self, ProtoError};

/// Tool names (the catalog contract).
pub const T_EXEC: &str = "gameserver.rcon.exec";
pub const T_STATUS: &str = "gameserver.status.query";
pub const T_LIST: &str = "gameserver.server.list";
pub const T_GET: &str = "gameserver.server.get";
pub const T_REGISTER: &str = "gameserver.server.register";
pub const T_REMOVE: &str = "gameserver.server.remove";
pub const T_AUDIT: &str = "gameserver.audit.list";

/// Scope a moderator-level action needs (admins also pass).
pub const SCOPE_MOD: &str = "community:moderate";
/// Scope an admin action needs (host enforces too via `required_scope`).
pub const SCOPE_ADMIN: &str = "community:admin";

pub const MAX_SERVERS: usize = 50;
pub const MAX_ALLOWLIST: usize = 64;
pub const MAX_ENTRY_LEN: usize = 128;
pub const MAX_COMMAND_LEN: usize = 256;
pub const MAX_LABEL_LEN: usize = 64;
pub const DEFAULT_MAX_OUTPUT: usize = 2048;
pub const HARD_MAX_OUTPUT: usize = 8192;
pub const DEFAULT_SERVER_RATE: u32 = 10;
pub const HARD_MAX_SERVER_RATE: u32 = 60;
/// Community-wide ceilings per minute.
pub const COMMUNITY_EXEC_RATE: i64 = 30;
pub const COMMUNITY_STATUS_RATE: i64 = 60;
pub const RATE_WINDOW_S: u32 = 60;
pub const AUDIT_RING: i64 = 200;
pub const AUDIT_LIST_MAX: usize = 50;
pub const MAX_PLAYERS_LISTED: usize = 32;

const KEY_INDEX: &str = "servers";
const KEY_AUDIT_SEQ: &str = "audit.seq";

fn server_key(id: &str) -> String {
    format!("server.{id}")
}

/// Log severity passed to the host.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Info,
    Warn,
}

/// Everything the tools need from the host.
pub trait Env: Send + Sync {
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    fn kv_set(&self, key: &str, value: &[u8]) -> Result<(), String>;
    fn kv_delete(&self, key: &str) -> Result<(), String>;
    /// Atomic add; `ttl_s` applies when the key is created.
    fn kv_incr(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, String>;
    fn log(&self, level: Level, message: &str, fields: &Value);
    fn now_millis(&self) -> u64;
    fn net(&self) -> &dyn GameNet;
}

/// Game family of a registered server (selects protocols).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Game {
    /// Valve Source/GoldSrc family: Source RCON + A2S.
    Source,
    /// Minecraft Java: Minecraft RCON + Server List Ping.
    Minecraft,
}

impl Game {
    fn scheme(self) -> Scheme {
        match self {
            Game::Source => Scheme::SourceRcon,
            Game::Minecraft => Scheme::MinecraftRcon,
        }
    }
}

/// Non-secret per-server policy stored in the bundle kv (community-scoped).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServerRecord {
    pub label: String,
    pub game: Game,
    pub enabled: bool,
    pub allowlist: Vec<String>,
    pub max_output: usize,
    pub rate_per_min: u32,
}

fn invalid(m: impl Into<String>) -> ToolError {
    ToolError::InvalidArgs(m.into())
}

/// Canonicalizes a UUID string (lowercase) or `InvalidArgs`.
pub fn parse_uuid(raw: &str, what: &str) -> Result<String, ToolError> {
    let b = raw.as_bytes();
    let ok = b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit(),
        });
    if ok {
        Ok(raw.to_ascii_lowercase())
    } else {
        Err(invalid(format!("{what} must be a UUID")))
    }
}

fn only_fields(args: &Value, allowed: &[&str]) -> Result<(), ToolError> {
    let obj = args
        .as_object()
        .ok_or_else(|| invalid("args must be an object"))?;
    match obj.keys().find(|k| !allowed.contains(&k.as_str())) {
        Some(k) => Err(invalid(format!("unknown field {k}"))),
        None => Ok(()),
    }
}

fn uuid_field(args: &Value, key: &str) -> Result<String, ToolError> {
    let raw = args
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("{key} must be a UUID string")))?;
    parse_uuid(raw, key)
}

fn is_mod(ctx: &ToolContext) -> bool {
    ctx.has_scope(SCOPE_MOD) || ctx.has_scope(SCOPE_ADMIN)
}

fn require_admin(ctx: &ToolContext) -> Result<(), ToolError> {
    if ctx.has_scope(SCOPE_ADMIN) {
        Ok(())
    } else {
        Err(ToolError::Forbidden)
    }
}

/// Collapses whitespace and lowercases for allowlist comparison.
pub fn normalize_command(cmd: &str) -> String {
    cmd.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

/// Validates a command line (single line, printable, bounded) and returns the
/// trimmed text that will be sent.
pub fn validate_command(cmd: &str) -> Result<String, ToolError> {
    let t = cmd.trim();
    if t.is_empty() {
        return Err(invalid("command must not be empty"));
    }
    if t.len() > MAX_COMMAND_LEN {
        return Err(invalid("command too long"));
    }
    if t.chars().any(char::is_control) {
        return Err(invalid("command must be a single printable line"));
    }
    Ok(t.to_string())
}

/// Validates one allowlist entry: exact line, or `verb *` (any arguments).
/// A bare `*` (allow everything) is refused.
pub fn validate_allow_entry(e: &str) -> Result<String, ToolError> {
    let n = validate_command(e).map_err(|_| invalid("allowlist entry must be a printable line"))?;
    if n.len() > MAX_ENTRY_LEN {
        return Err(invalid("allowlist entry too long"));
    }
    let norm = normalize_command(&n);
    if norm == "*" || norm.starts_with('*') {
        return Err(invalid("allowlist entry may not start with a wildcard"));
    }
    let body = norm.strip_suffix(" *").unwrap_or(&norm);
    if body.contains('*') {
        return Err(invalid("wildcard is only allowed as a trailing ' *'"));
    }
    Ok(norm)
}

/// `true` iff `command` is permitted by `allowlist` (empty list = deny all).
pub fn command_allowed(allowlist: &[String], command: &str) -> bool {
    let norm = normalize_command(command);
    allowlist.iter().any(|e| match e.strip_suffix(" *") {
        Some(verb) => norm == verb || norm.starts_with(&format!("{verb} ")),
        None => norm == *e,
    })
}

fn load_index(env: &dyn Env) -> Result<Vec<String>, ToolError> {
    match env.kv_get(KEY_INDEX).map_err(|_| unavailable("kv"))? {
        None => Ok(Vec::new()),
        Some(b) => serde_json::from_slice(&b).map_err(|_| invalid("corrupt server index")),
    }
}

fn save_index(env: &dyn Env, idx: &[String]) -> Result<(), ToolError> {
    let b = serde_json::to_vec(idx).map_err(|e| invalid(e.to_string()))?;
    env.kv_set(KEY_INDEX, &b).map_err(|_| unavailable("kv"))
}

fn unavailable(_what: &str) -> ToolError {
    ToolError::TargetUnavailable
}

fn load_server(env: &dyn Env, id: &str) -> Result<ServerRecord, ToolError> {
    match env.kv_get(&server_key(id)).map_err(|_| unavailable("kv"))? {
        None => Err(invalid("not_registered")),
        Some(b) => serde_json::from_slice(&b).map_err(|_| invalid("corrupt server record")),
    }
}

fn net_to_tool(e: &NetError) -> ToolError {
    match e {
        NetError::Unavailable(_) => ToolError::TargetUnavailable,
        NetError::Timeout => ToolError::Timeout,
        other => invalid(other.to_string()),
    }
}

fn proto_to_tool(e: ProtoError) -> ToolError {
    match e {
        ProtoError::Net(n) => net_to_tool(&n),
        ProtoError::Malformed(_) => invalid("bad_server_response"),
        ProtoError::Unsupported(_) => invalid("unsupported_server_response"),
    }
}

/// Stable machine code for the audit trail.
fn outcome_code(e: &ToolError) -> String {
    match e {
        ToolError::InvalidArgs(m) => m.chars().take(64).collect(),
        ToolError::Forbidden => "forbidden".into(),
        ToolError::NotGranted => "not_granted".into(),
        ToolError::Timeout => "timeout".into(),
        ToolError::TargetUnavailable => "net_unavailable".into(),
        _ => "error".into(),
    }
}

/// Fixed-window limiter: increments `key` (TTL = window) and refuses past `max`.
fn rate_limit(env: &dyn Env, key: &str, max: i64) -> Result<(), ToolError> {
    let n = env
        .kv_incr(key, 1, RATE_WINDOW_S)
        .map_err(|_| unavailable("kv"))?;
    if n > max {
        Err(invalid("rate_limited"))
    } else {
        Ok(())
    }
}

/// Appends one audit record to the log and the bounded kv ring.
fn audit(env: &dyn Env, server: &str, actor: Option<&str>, command: &str, outcome: &str) {
    let seq = env.kv_incr(KEY_AUDIT_SEQ, 1, 0).unwrap_or(-1);
    let rec = json!({
        "seq": seq,
        "at_ms": env.now_millis(),
        "server": server,
        "actor": actor,
        "command": command,
        "outcome": outcome,
    });
    if seq >= 0 {
        let slot = format!("audit.{}", seq % AUDIT_RING);
        if let Ok(b) = serde_json::to_vec(&rec)
            && env.kv_set(&slot, &b).is_err()
        {
            env.log(
                Level::Warn,
                "gameserver audit ring write failed",
                &json!({"server": server}),
            );
        }
    }
    env.log(Level::Info, "gameserver rcon audit", &rec);
}

fn server_view(id: &str, r: &ServerRecord, detail: bool) -> Value {
    let mut v = json!({"server": id, "label": r.label, "game": r.game, "enabled": r.enabled});
    if detail {
        v["allowlist"] = json!(r.allowlist);
        v["max_output"] = json!(r.max_output);
        v["rate_per_min"] = json!(r.rate_per_min);
    }
    v
}

fn tool_list(env: &dyn Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &[])?;
    let mut out = Vec::new();
    for id in load_index(env)? {
        if let Ok(r) = load_server(env, &id) {
            out.push(server_view(&id, &r, false));
        }
    }
    let _ = ctx;
    Ok(json!({ "servers": out }))
}

fn tool_get(env: &dyn Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["server"])?;
    let id = uuid_field(&args, "server")?;
    let r = load_server(env, &id)?;
    Ok(server_view(&id, &r, is_mod(ctx)))
}

fn tool_register(env: &dyn Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    require_admin(ctx)?;
    only_fields(
        &args,
        &[
            "server",
            "label",
            "game",
            "enabled",
            "allowlist",
            "max_output",
            "rate_per_min",
        ],
    )?;
    let id = uuid_field(&args, "server")?;
    let existing = load_server(env, &id).ok();
    let label = match args.get("label") {
        Some(Value::String(s)) => s.trim().to_string(),
        Some(_) => return Err(invalid("label must be a string")),
        None => existing
            .as_ref()
            .map(|r| r.label.clone())
            .ok_or_else(|| invalid("label required"))?,
    };
    if label.is_empty() || label.len() > MAX_LABEL_LEN || label.chars().any(char::is_control) {
        return Err(invalid("label must be 1..=64 printable characters"));
    }
    let game = match args.get("game") {
        Some(g) => serde_json::from_value::<Game>(g.clone())
            .map_err(|_| invalid("game must be source|minecraft"))?,
        None => existing
            .as_ref()
            .map(|r| r.game)
            .ok_or_else(|| invalid("game required"))?,
    };
    let enabled = match args.get("enabled") {
        Some(Value::Bool(b)) => *b,
        Some(_) => return Err(invalid("enabled must be a boolean")),
        None => existing.as_ref().map(|r| r.enabled).unwrap_or(true),
    };
    let allowlist: Vec<String> = match args.get("allowlist") {
        Some(Value::Array(a)) => {
            if a.len() > MAX_ALLOWLIST {
                return Err(invalid("allowlist too long"));
            }
            let mut set = BTreeSet::new();
            for e in a {
                let s = e
                    .as_str()
                    .ok_or_else(|| invalid("allowlist entries must be strings"))?;
                set.insert(validate_allow_entry(s)?);
            }
            set.into_iter().collect()
        }
        Some(_) => return Err(invalid("allowlist must be an array")),
        None => existing
            .as_ref()
            .map(|r| r.allowlist.clone())
            .unwrap_or_default(),
    };
    let max_output = match args.get("max_output") {
        Some(v) => {
            let n = v
                .as_u64()
                .ok_or_else(|| invalid("max_output must be an integer"))?
                as usize;
            if !(64..=HARD_MAX_OUTPUT).contains(&n) {
                return Err(invalid("max_output out of range"));
            }
            n
        }
        None => existing
            .as_ref()
            .map(|r| r.max_output)
            .unwrap_or(DEFAULT_MAX_OUTPUT),
    };
    let rate_per_min = match args.get("rate_per_min") {
        Some(v) => {
            let n = v
                .as_u64()
                .ok_or_else(|| invalid("rate_per_min must be an integer"))?
                as u32;
            if !(1..=HARD_MAX_SERVER_RATE).contains(&n) {
                return Err(invalid("rate_per_min out of range"));
            }
            n
        }
        None => existing
            .as_ref()
            .map(|r| r.rate_per_min)
            .unwrap_or(DEFAULT_SERVER_RATE),
    };
    let rec = ServerRecord {
        label,
        game,
        enabled,
        allowlist,
        max_output,
        rate_per_min,
    };
    let mut idx = load_index(env)?;
    if !idx.contains(&id) {
        if idx.len() >= MAX_SERVERS {
            return Err(invalid("too_many_servers"));
        }
        idx.push(id.clone());
    }
    let b = serde_json::to_vec(&rec).map_err(|e| invalid(e.to_string()))?;
    env.kv_set(&server_key(&id), &b)
        .map_err(|_| unavailable("kv"))?;
    save_index(env, &idx)?;
    env.log(
        Level::Info,
        "gameserver registered",
        &json!({"server": id, "actor": ctx.actor, "allowlist_len": rec.allowlist.len(), "created": existing.is_none()}),
    );
    Ok(server_view(&id, &rec, true))
}

fn tool_remove(env: &dyn Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    require_admin(ctx)?;
    only_fields(&args, &["server"])?;
    let id = uuid_field(&args, "server")?;
    load_server(env, &id)?;
    let mut idx = load_index(env)?;
    idx.retain(|x| x != &id);
    save_index(env, &idx)?;
    env.kv_delete(&server_key(&id))
        .map_err(|_| unavailable("kv"))?;
    env.log(
        Level::Info,
        "gameserver removed",
        &json!({"server": id, "actor": ctx.actor}),
    );
    Ok(json!({"server": id, "removed": true}))
}

/// Replay-guard retention.
pub const IDEMPOTENCY_TTL_S: u32 = 900;

/// Maps a caller key to a kv slot (`[a-z0-9._-]` only: kv keys never carry ':').
fn idempotency_slot(key: &str) -> Result<String, ToolError> {
    if key.is_empty() || key.len() > 200 {
        return Err(invalid("idempotency key must be 1..=200 bytes"));
    }
    let clean: String = key
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '.' || c == '-' {
                c.to_ascii_lowercase()
            } else {
                '_'
            }
        })
        .collect();
    Ok(format!("idem.{clean}"))
}

fn exec_inner(
    env: &dyn Env,
    ctx: &ToolContext,
    id: &str,
    command: &str,
) -> Result<Value, ToolError> {
    if !is_mod(ctx) {
        return Err(ToolError::Forbidden);
    }
    let rec = load_server(env, id)?;
    if !rec.enabled {
        return Err(invalid("server_disabled"));
    }
    if !command_allowed(&rec.allowlist, command) {
        return Err(invalid("command_not_allowed"));
    }
    // A command is NOT idempotent (kick/ban/say): a redelivered request must never
    // run twice, so the caller's key is mandatory and a replay is refused loudly.
    let key = ctx
        .idempotency_key
        .as_deref()
        .ok_or_else(|| invalid("idempotency_key_required"))?;
    let slot = idempotency_slot(key)?;
    if env
        .kv_incr(&slot, 1, IDEMPOTENCY_TTL_S)
        .map_err(|_| unavailable("kv"))?
        > 1
    {
        return Err(invalid("duplicate_request"));
    }
    rate_limit(env, "rl.exec", COMMUNITY_EXEC_RATE)?;
    rate_limit(env, &format!("rl.s.{id}"), i64::from(rec.rate_per_min))?;
    let mut stream = env
        .net()
        .rcon_open(id, rec.game.scheme())
        .map_err(|e| net_to_tool(&e))?;
    let out = proto::rcon_exec(stream.as_mut(), rec.game.scheme(), command, rec.max_output)
        .map_err(proto_to_tool)?;
    Ok(json!({"server": id, "output": out.text, "truncated": out.truncated}))
}

fn tool_exec(env: &dyn Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["server", "command"])?;
    let id = uuid_field(&args, "server")?;
    let raw = args
        .get("command")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("command must be a string"))?;
    let command = validate_command(raw)?;
    let res = exec_inner(env, ctx, &id, &command);
    let outcome = match &res {
        Ok(v) if v["truncated"] == json!(true) => "ok_truncated".to_string(),
        Ok(_) => "ok".to_string(),
        Err(e) => format!("refused:{}", outcome_code(e)),
    };
    audit(env, &id, ctx.actor.as_deref(), &command, &outcome);
    res
}

fn tool_status(env: &dyn Env, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["server", "include_players"])?;
    let id = uuid_field(&args, "server")?;
    let want_players = match args.get("include_players") {
        None => false,
        Some(Value::Bool(b)) => *b,
        Some(_) => return Err(invalid("include_players must be a boolean")),
    };
    let rec = load_server(env, &id)?;
    if !rec.enabled {
        return Err(invalid("server_disabled"));
    }
    rate_limit(env, "rl.status", COMMUNITY_STATUS_RATE)?;
    match rec.game {
        Game::Source => {
            let info = match proto::a2s_info(env.net(), &id) {
                Ok(i) => i,
                Err(e) => return Err(proto_to_tool(e)),
            };
            let players = if want_players {
                let rows = proto::a2s_players(env.net(), &id, MAX_PLAYERS_LISTED)
                    .map_err(proto_to_tool)?;
                Some(rows.into_iter().map(|p| p.name).collect::<Vec<_>>())
            } else {
                None
            };
            Ok(json!({
                "server": id, "game": "source", "online": true, "name": info.name,
                "map": info.map, "version": info.version, "players": info.players,
                "max_players": info.max_players, "bots": info.bots, "player_names": players,
            }))
        }
        Game::Minecraft => {
            let mut stream = env.net().tcp_open(&id).map_err(|e| net_to_tool(&e))?;
            let st = proto::mc_ping(stream.as_mut()).map_err(proto_to_tool)?;
            let names = want_players.then(|| {
                st.sample
                    .iter()
                    .take(MAX_PLAYERS_LISTED)
                    .cloned()
                    .collect::<Vec<_>>()
            });
            Ok(json!({
                "server": id, "game": "minecraft", "online": true, "name": st.motd,
                "map": Value::Null, "version": st.version, "players": st.players,
                "max_players": st.max_players, "bots": 0, "player_names": names,
            }))
        }
    }
}

fn tool_audit(env: &dyn Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    require_admin(ctx)?;
    only_fields(&args, &["limit"])?;
    let limit = match args.get("limit") {
        None => 20,
        Some(v) => v
            .as_u64()
            .filter(|n| (1..=AUDIT_LIST_MAX as u64).contains(n))
            .ok_or_else(|| invalid("limit out of range"))? as usize,
    };
    let seq = match env.kv_get(KEY_AUDIT_SEQ).map_err(|_| unavailable("kv"))? {
        None => 0,
        Some(b) => String::from_utf8_lossy(&b)
            .trim()
            .parse::<i64>()
            .unwrap_or(0),
    };
    let mut entries = Vec::new();
    let mut i = seq;
    while i > 0 && entries.len() < limit && seq - i < AUDIT_RING {
        if let Some(b) = env
            .kv_get(&format!("audit.{}", i % AUDIT_RING))
            .map_err(|_| unavailable("kv"))?
            && let Ok(v) = serde_json::from_slice::<Value>(&b)
            && v["seq"] == json!(i)
        {
            entries.push(v);
        }
        i -= 1;
    }
    Ok(json!({ "entries": entries }))
}

/// Stable log label for a tool name (never a caller-supplied unknown name).
pub fn tool_log_label(name: &str) -> &'static str {
    [
        T_EXEC, T_STATUS, T_LIST, T_GET, T_REGISTER, T_REMOVE, T_AUDIT,
    ]
    .into_iter()
    .find(|t| *t == name)
    .unwrap_or("unknown")
}

/// Builds the tool table over `env`.
pub fn build_registry(env: Arc<dyn Env>) -> ToolRegistry {
    let mut reg = ToolRegistry::new();
    type Tool = fn(&dyn Env, &ToolContext, Value) -> Result<Value, ToolError>;
    let table: [(&str, Tool); 7] = [
        (T_EXEC, tool_exec),
        (T_STATUS, tool_status),
        (T_LIST, tool_list),
        (T_GET, tool_get),
        (T_REGISTER, tool_register),
        (T_REMOVE, tool_remove),
        (T_AUDIT, tool_audit),
    ];
    for (name, f) in table {
        let env = env.clone();
        reg.register(name, move |ctx, args| f(env.as_ref(), ctx, args));
    }
    reg
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::net::testing::{ScriptedNet, TcpAdapter, spawn_rcon_server};
    use crate::net::{Stream, UnavailableNet};
    use std::collections::HashMap;
    use std::sync::Mutex;

    const SRV: &str = "11111111-2222-3333-4444-555555555555";

    struct FakeEnv {
        kv: Mutex<HashMap<String, Vec<u8>>>,
        logs: Mutex<Vec<(Level, String, Value)>>,
        net: Box<dyn GameNet + Send + Sync>,
    }

    /// Connects `rcon_open` to a real loopback server.
    struct LoopNet(std::net::SocketAddr);
    impl GameNet for LoopNet {
        fn rcon_open(&self, _: &str, _: Scheme) -> Result<Box<dyn Stream>, NetError> {
            let s = std::net::TcpStream::connect(self.0).map_err(|_| NetError::Unreachable)?;
            Ok(Box::new(TcpAdapter(s)))
        }
        fn tcp_open(&self, _: &str) -> Result<Box<dyn Stream>, NetError> {
            Err(NetError::Unreachable)
        }
        fn udp_exchange(&self, _: &str, _: &[u8], _: usize) -> Result<Vec<u8>, NetError> {
            Err(NetError::Unreachable)
        }
    }

    impl Env for FakeEnv {
        fn kv_get(&self, k: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(self.kv.lock().unwrap().get(k).cloned())
        }
        fn kv_set(&self, k: &str, v: &[u8]) -> Result<(), String> {
            self.kv.lock().unwrap().insert(k.into(), v.to_vec());
            Ok(())
        }
        fn kv_delete(&self, k: &str) -> Result<(), String> {
            self.kv.lock().unwrap().remove(k);
            Ok(())
        }
        fn kv_incr(&self, k: &str, d: i64, _: u32) -> Result<i64, String> {
            let mut m = self.kv.lock().unwrap();
            let cur = m
                .get(k)
                .map(|b| String::from_utf8_lossy(b).parse::<i64>().unwrap_or(0))
                .unwrap_or(0);
            let n = cur + d;
            m.insert(k.into(), n.to_string().into_bytes());
            Ok(n)
        }
        fn log(&self, l: Level, m: &str, f: &Value) {
            self.logs.lock().unwrap().push((l, m.into(), f.clone()));
        }
        fn now_millis(&self) -> u64 {
            1_700_000_000_000
        }
        fn net(&self) -> &dyn GameNet {
            self.net.as_ref()
        }
    }

    fn env(net: Box<dyn GameNet + Send + Sync>) -> Arc<FakeEnv> {
        Arc::new(FakeEnv {
            kv: Mutex::new(HashMap::new()),
            logs: Mutex::new(vec![]),
            net,
        })
    }

    fn call(
        reg: &ToolRegistry,
        tool: &str,
        args: Value,
        actor: Option<(&str, &[&str])>,
    ) -> Result<Value, ToolError> {
        use std::sync::atomic::{AtomicU64, Ordering};
        static N: AtomicU64 = AtomicU64::new(0);
        let mut a = args;
        if tool == T_EXEC && a.get("__idempotency_key").is_none() {
            a["__idempotency_key"] = json!(format!("t-{}", N.fetch_add(1, Ordering::Relaxed)));
        }
        if let Some((u, scopes)) = actor {
            a["__ctx"] = json!({"actor": u, "scopes": scopes});
        }
        reg.call(tool, &serde_json::to_vec(&a).unwrap())
            .map(|b| serde_json::from_slice(&b).unwrap())
    }

    const ADMIN: Option<(&str, &[&str])> =
        Some(("aaaaaaaa-0000-0000-0000-000000000001", &["community:admin"]));
    const MOD: Option<(&str, &[&str])> = Some((
        "aaaaaaaa-0000-0000-0000-000000000002",
        &["community:moderate"],
    ));
    const USER: Option<(&str, &[&str])> = Some((
        "aaaaaaaa-0000-0000-0000-000000000003",
        &["community:member"],
    ));

    fn registered(e: &Arc<FakeEnv>, allow: &[&str]) -> ToolRegistry {
        let reg = build_registry(e.clone());
        call(
            &reg,
            T_REGISTER,
            json!({"server": SRV, "label": "Main", "game": "source", "allowlist": allow}),
            ADMIN,
        )
        .unwrap();
        reg
    }

    fn err_msg(r: Result<Value, ToolError>) -> String {
        match r.unwrap_err() {
            ToolError::InvalidArgs(m) => m,
            other => format!("{other:?}"),
        }
    }

    #[test]
    fn allowlist_matching_is_exact_or_trailing_wildcard() {
        let al = vec!["status".to_string(), "kick *".to_string()];
        assert!(command_allowed(&al, "STATUS"));
        assert!(command_allowed(&al, "  status "));
        assert!(!command_allowed(&al, "status extra"));
        assert!(command_allowed(&al, "kick bob"));
        assert!(command_allowed(&al, "kick"));
        assert!(!command_allowed(&al, "kickall"));
        assert!(!command_allowed(&[], "status"));
    }

    #[test]
    fn allow_entries_reject_global_and_mid_wildcards() {
        assert!(validate_allow_entry("*").is_err());
        assert!(validate_allow_entry("* *").is_err());
        assert!(validate_allow_entry("a*b").is_err());
        assert!(validate_allow_entry("say\nquit").is_err());
        assert_eq!(validate_allow_entry("Kick  *").unwrap(), "kick *");
    }

    #[test]
    fn command_validation_blocks_multiline_and_oversize() {
        assert!(validate_command("status\nquit").is_err());
        assert!(validate_command("a\u{0}b").is_err());
        assert!(validate_command(&"a".repeat(MAX_COMMAND_LEN + 1)).is_err());
        assert!(validate_command("   ").is_err());
    }

    #[test]
    fn register_requires_admin_and_validates() {
        let e = env(Box::new(UnavailableNet));
        let reg = build_registry(e.clone());
        let args =
            json!({"server": SRV, "label": "Main", "game": "source", "allowlist": ["status"]});
        assert_eq!(
            call(&reg, T_REGISTER, args.clone(), MOD).unwrap_err(),
            ToolError::Forbidden
        );
        assert_eq!(
            call(&reg, T_REGISTER, args.clone(), None).unwrap_err(),
            ToolError::Forbidden
        );
        assert!(
            call(
                &reg,
                T_REGISTER,
                json!({"server": SRV, "label": "x", "game": "quake"}),
                ADMIN
            )
            .is_err()
        );
        assert!(
            call(
                &reg,
                T_REGISTER,
                json!({"server": SRV, "label": "x", "game": "source", "host": "1.2.3.4"}),
                ADMIN
            )
            .is_err()
        );
        assert!(
            call(
                &reg,
                T_REGISTER,
                json!({"server": SRV, "label": "x", "game": "source", "allowlist": ["*"]}),
                ADMIN
            )
            .is_err()
        );
        let ok = call(&reg, T_REGISTER, args, ADMIN).unwrap();
        assert_eq!(ok["label"], "Main");
        let listed = call(&reg, T_LIST, json!({}), USER).unwrap();
        assert_eq!(listed["servers"].as_array().unwrap().len(), 1);
        assert!(listed["servers"][0].get("allowlist").is_none());
        // Re-register updates in place, never duplicates.
        call(
            &reg,
            T_REGISTER,
            json!({"server": SRV, "enabled": false}),
            ADMIN,
        )
        .unwrap();
        let listed = call(&reg, T_LIST, json!({}), USER).unwrap();
        assert_eq!(listed["servers"].as_array().unwrap().len(), 1);
        assert_eq!(listed["servers"][0]["enabled"], false);
    }

    #[test]
    fn get_hides_policy_from_non_mods_and_remove_works() {
        let e = env(Box::new(UnavailableNet));
        let reg = registered(&e, &["status"]);
        assert!(
            call(&reg, T_GET, json!({"server": SRV}), USER)
                .unwrap()
                .get("allowlist")
                .is_none()
        );
        assert!(
            call(&reg, T_GET, json!({"server": SRV}), MOD)
                .unwrap()
                .get("allowlist")
                .is_some()
        );
        assert_eq!(
            call(&reg, T_REMOVE, json!({"server": SRV}), MOD).unwrap_err(),
            ToolError::Forbidden
        );
        call(&reg, T_REMOVE, json!({"server": SRV}), ADMIN).unwrap();
        assert_eq!(
            err_msg(call(&reg, T_GET, json!({"server": SRV}), USER)),
            "not_registered"
        );
        assert_eq!(
            call(&reg, T_LIST, json!({}), USER).unwrap()["servers"]
                .as_array()
                .unwrap()
                .len(),
            0
        );
    }

    #[test]
    fn exec_refuses_non_mod_unregistered_and_disallowed_and_audits_each() {
        let e = env(Box::new(UnavailableNet));
        let reg = registered(&e, &["status"]);
        assert_eq!(
            call(
                &reg,
                T_EXEC,
                json!({"server": SRV, "command": "status"}),
                USER
            )
            .unwrap_err(),
            ToolError::Forbidden
        );
        assert_eq!(
            err_msg(call(
                &reg,
                T_EXEC,
                json!({"server": SRV, "command": "quit"}),
                MOD
            )),
            "command_not_allowed"
        );
        let other = "99999999-2222-3333-4444-555555555555";
        assert_eq!(
            err_msg(call(
                &reg,
                T_EXEC,
                json!({"server": other, "command": "status"}),
                MOD
            )),
            "not_registered"
        );
        // Capability absent => loud, typed, never a silent success.
        assert_eq!(
            call(
                &reg,
                T_EXEC,
                json!({"server": SRV, "command": "status"}),
                MOD
            )
            .unwrap_err(),
            ToolError::TargetUnavailable
        );
        let audit_n = e
            .logs
            .lock()
            .unwrap()
            .iter()
            .filter(|l| l.1 == "gameserver rcon audit")
            .count();
        assert_eq!(audit_n, 4);
        let entries = call(&reg, T_AUDIT, json!({}), ADMIN).unwrap();
        let arr = entries["entries"].as_array().unwrap();
        assert_eq!(arr.len(), 4);
        assert_eq!(arr[0]["outcome"], "refused:net_unavailable");
        assert_eq!(arr[3]["command"], "status");
        assert_eq!(
            call(&reg, T_AUDIT, json!({}), MOD).unwrap_err(),
            ToolError::Forbidden
        );
    }

    #[test]
    fn exec_runs_over_a_real_loopback_rcon_socket_and_audits() {
        let (addr, seen) = spawn_rcon_server("hostname: test\nplayers: 2 humans");
        let e = env(Box::new(LoopNet(addr)));
        let reg = registered(&e, &["status", "say *"]);
        let out = call(
            &reg,
            T_EXEC,
            json!({"server": SRV, "command": "status"}),
            MOD,
        )
        .unwrap();
        assert_eq!(out["output"], "hostname: test\nplayers: 2 humans");
        assert_eq!(out["truncated"], false);
        assert_eq!(seen.lock().unwrap().as_slice(), ["status"]);
        let logs = e.logs.lock().unwrap();
        let rec = logs
            .iter()
            .find(|l| l.1 == "gameserver rcon audit")
            .unwrap();
        assert_eq!(rec.2["command"], "status");
        assert_eq!(rec.2["outcome"], "ok");
        assert_eq!(rec.2["actor"], "aaaaaaaa-0000-0000-0000-000000000002");
        // Output text is never logged.
        assert!(!rec.2.to_string().contains("hostname"));
    }

    #[test]
    fn exec_output_is_capped_per_server() {
        let (addr, _) = spawn_rcon_server(
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        );
        let e = env(Box::new(LoopNet(addr)));
        let reg = build_registry(e.clone());
        call(&reg, T_REGISTER, json!({"server": SRV, "label": "m", "game": "source", "allowlist": ["status"], "max_output": 64}), ADMIN).unwrap();
        let out = call(
            &reg,
            T_EXEC,
            json!({"server": SRV, "command": "status"}),
            MOD,
        )
        .unwrap();
        assert_eq!(out["output"].as_str().unwrap().len(), 64);
        assert_eq!(out["truncated"], true);
    }

    #[test]
    fn exec_is_rate_limited_per_server_and_audited() {
        let e = env(Box::new(UnavailableNet));
        let reg = build_registry(e.clone());
        call(&reg, T_REGISTER, json!({"server": SRV, "label": "m", "game": "source", "allowlist": ["status"], "rate_per_min": 2}), ADMIN).unwrap();
        for _ in 0..2 {
            assert_eq!(
                call(
                    &reg,
                    T_EXEC,
                    json!({"server": SRV, "command": "status"}),
                    MOD
                )
                .unwrap_err(),
                ToolError::TargetUnavailable
            );
        }
        assert_eq!(
            err_msg(call(
                &reg,
                T_EXEC,
                json!({"server": SRV, "command": "status"}),
                MOD
            )),
            "rate_limited"
        );
    }

    #[test]
    fn exec_is_rate_limited_community_wide() {
        let e = env(Box::new(UnavailableNet));
        let reg = registered(&e, &["status"]);
        e.kv.lock().unwrap().insert(
            "rl.exec".into(),
            COMMUNITY_EXEC_RATE.to_string().into_bytes(),
        );
        assert_eq!(
            err_msg(call(
                &reg,
                T_EXEC,
                json!({"server": SRV, "command": "status"}),
                MOD
            )),
            "rate_limited"
        );
    }

    #[test]
    fn disabled_server_refuses_exec_and_status() {
        let e = env(Box::new(UnavailableNet));
        let reg = registered(&e, &["status"]);
        call(
            &reg,
            T_REGISTER,
            json!({"server": SRV, "enabled": false}),
            ADMIN,
        )
        .unwrap();
        assert_eq!(
            err_msg(call(
                &reg,
                T_EXEC,
                json!({"server": SRV, "command": "status"}),
                MOD
            )),
            "server_disabled"
        );
        assert_eq!(
            err_msg(call(&reg, T_STATUS, json!({"server": SRV}), USER)),
            "server_disabled"
        );
    }

    #[test]
    fn status_source_via_a2s_and_unavailable_fails_loud() {
        let e = env(Box::new(UnavailableNet));
        let reg = registered(&e, &[]);
        assert_eq!(
            call(&reg, T_STATUS, json!({"server": SRV}), USER).unwrap_err(),
            ToolError::TargetUnavailable
        );
        let mut r = vec![0xff, 0xff, 0xff, 0xff, b'I', 17];
        for s in ["Srv", "map1", "dir", "Game"] {
            r.extend_from_slice(s.as_bytes());
            r.push(0);
        }
        r.extend_from_slice(&[0, 0, 3, 16, 0, b'd', b'l', 0, 0]);
        r.extend_from_slice(b"1.0\0");
        let e = env(Box::new(ScriptedNet::udp(vec![r])));
        let reg = registered(&e, &[]);
        let st = call(&reg, T_STATUS, json!({"server": SRV}), USER).unwrap();
        assert_eq!(st["players"], 3);
        assert_eq!(st["max_players"], 16);
        assert_eq!(st["map"], "map1");
    }

    #[test]
    fn status_minecraft_via_server_list_ping() {
        let json_s = r#"{"version":{"name":"1.21"},"players":{"max":10,"online":1,"sample":[{"name":"Alex"}]},"description":"Hello"}"#;
        let mut inner = vec![0x00, json_s.len() as u8];
        inner.extend_from_slice(json_s.as_bytes());
        let mut wire = vec![inner.len() as u8];
        wire.extend(inner);
        let net = ScriptedNet::udp(vec![]);
        *net.tcp.lock().unwrap() = Some(wire);
        let e = env(Box::new(net));
        let reg = build_registry(e.clone());
        call(
            &reg,
            T_REGISTER,
            json!({"server": SRV, "label": "mc", "game": "minecraft"}),
            ADMIN,
        )
        .unwrap();
        let st = call(
            &reg,
            T_STATUS,
            json!({"server": SRV, "include_players": true}),
            USER,
        )
        .unwrap();
        assert_eq!(st["name"], "Hello");
        assert_eq!(st["players"], 1);
        assert_eq!(st["player_names"][0], "Alex");
    }

    #[test]
    fn unknown_fields_and_bad_uuids_are_rejected() {
        let e = env(Box::new(UnavailableNet));
        let reg = registered(&e, &["status"]);
        assert!(
            call(
                &reg,
                T_EXEC,
                json!({"server": SRV, "command": "status", "host": "10.0.0.1"}),
                MOD
            )
            .is_err()
        );
        assert!(
            call(
                &reg,
                T_EXEC,
                json!({"server": "10.0.0.1:27015", "command": "status"}),
                MOD
            )
            .is_err()
        );
        assert!(call(&reg, T_STATUS, json!({"server": "not-a-uuid"}), USER).is_err());
        assert!(reg.call("nope", b"{}").is_err());
    }

    #[test]
    fn exec_requires_a_key_and_refuses_replays_without_rerunning() {
        let (addr, seen) = spawn_rcon_server("ok");
        let e = env(Box::new(LoopNet(addr)));
        let reg = registered(&e, &["status"]);
        let args = |k: Option<&str>| {
            let mut a = json!({"server": SRV, "command": "status"});
            if let Some(k) = k {
                a["__idempotency_key"] = json!(k);
            }
            reg.call(T_EXEC, &serde_json::to_vec(&{
                let mut a = a;
                a["__ctx"] = json!({"actor": "aaaaaaaa-0000-0000-0000-000000000002", "scopes": ["community:moderate"]});
                a
            }).unwrap())
        };
        // `call()` injects keys, so go through the registry directly here.
        assert_eq!(
            args(None).unwrap_err(),
            ToolError::InvalidArgs("idempotency_key_required".into())
        );
        assert!(args(Some("msg:1")).is_ok());
        assert_eq!(
            args(Some("msg:1")).unwrap_err(),
            ToolError::InvalidArgs("duplicate_request".into())
        );
        assert_eq!(
            seen.lock().unwrap().len(),
            1,
            "replay must not reach the server"
        );
    }

    #[test]
    fn log_label_is_a_known_tool_or_unknown() {
        assert_eq!(tool_log_label(T_EXEC), T_EXEC);
        assert_eq!(tool_log_label("../etc"), "unknown");
    }
}
