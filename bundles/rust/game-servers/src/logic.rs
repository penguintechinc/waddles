//! Host-independent `game-servers` command logic.
//!
//! The app is pure orchestration: it parses chat commands, resolves a
//! user-typed server ALIAS (the label, never a reference) to the service's
//! server UUID, calls the `gameserver` SERVICE bundle's tools through the
//! [`Host`] seam (host-mediated, grant-checked, verified-actor-scoped), and
//! renders a short reply. It never holds an address, a secret or a socket,
//! and it never decides authorization: the service re-checks the verified
//! actor's scope on every call and the app just reports a refusal.
//!
//! Contract consumed (see `tests/` contract fixtures in this module):
//! `gameserver.server.list`, `gameserver.status.query`, `gameserver.rcon.exec`.

use serde_json::{Value, json};

/// The service bundle that owns the tools this app invokes.
pub const SERVICE: &str = "waddles.core.service.gameserver";
pub const TOOL_LIST: &str = "gameserver.server.list";
pub const TOOL_STATUS: &str = "gameserver.status.query";
pub const TOOL_EXEC: &str = "gameserver.rcon.exec";

/// Most servers summarised in one reply / one scheduled post.
pub const MAX_STATUS_LINES: usize = 5;
/// Longest RCON output echoed to chat.
pub const MAX_REPLY_OUTPUT: usize = 400;

/// Why a tool call failed (mirrors the WIT `tool-error`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolFail {
    NotGranted,
    Forbidden,
    Timeout,
    Unavailable,
    Invalid(String),
}

/// The host seam.
pub trait Host {
    /// Invokes `tool` on the gameserver service; writes carry a key.
    fn invoke(&self, tool: &str, args: &Value, idem: Option<&str>) -> Result<Value, ToolFail>;
    /// PII-free structured log line (op/code/counts only).
    fn log(&self, message: &str, fields: &Value);
}

/// One registered, enabled server as the list tool reports it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Server {
    pub id: String,
    pub label: String,
}

/// Normalizes a label/alias for matching: lowercase, spaces to `-`.
pub fn alias(label: &str) -> String {
    label
        .split_whitespace()
        .collect::<Vec<_>>()
        .join("-")
        .to_lowercase()
}

/// Friendly, code-stable refusal text.
pub fn fail_text(f: &ToolFail) -> String {
    match f {
        ToolFail::NotGranted => "Game server access is not approved for this community.".into(),
        ToolFail::Forbidden => "You are not allowed to do that.".into(),
        ToolFail::Timeout => "The game server did not answer in time.".into(),
        ToolFail::Unavailable => "The game server service is unavailable right now.".into(),
        ToolFail::Invalid(code) => match code.as_str() {
            "rate_limited" => "Slow down: rate limit reached, try again in a minute.".into(),
            "command_not_allowed" => "That command is not on this server's allowlist.".into(),
            "duplicate_request" => "That command was already sent.".into(),
            "server_disabled" => "That server is disabled.".into(),
            "not_registered" => "That server is not registered.".into(),
            "unreachable" => "Could not reach the game server.".into(),
            "timeout" => "The game server did not answer in time.".into(),
            "auth_failed" => "The game server rejected the stored RCON login.".into(),
            "bad_server_response" | "unsupported_server_response" => {
                "The game server sent a reply this bot cannot read.".into()
            }
            c if c.starts_with("denied") => "The platform blocked that connection.".into(),
            c => format!(
                "Request refused ({}).",
                c.chars().take(48).collect::<String>()
            ),
        },
    }
}

const USAGE: &str = "Usage: !server [status|players|list] [name] | !rcon <name> <command>";

/// Lists enabled servers.
pub fn list_servers(host: &dyn Host) -> Result<Vec<Server>, ToolFail> {
    let v = host.invoke(TOOL_LIST, &json!({}), None)?;
    let rows = v
        .get("servers")
        .and_then(Value::as_array)
        .ok_or_else(|| ToolFail::Invalid("bad_server_response".into()))?;
    Ok(rows
        .iter()
        .filter(|r| r.get("enabled").and_then(Value::as_bool).unwrap_or(false))
        .filter_map(|r| {
            Some(Server {
                id: r.get("server")?.as_str()?.to_string(),
                label: r.get("label")?.as_str()?.to_string(),
            })
        })
        .collect())
}

/// Picks the server named by `name` (alias match), or the only one.
fn resolve<'a>(servers: &'a [Server], name: Option<&str>) -> Result<&'a Server, String> {
    match name {
        Some(n) => {
            let want = alias(n);
            servers
                .iter()
                .find(|s| alias(&s.label) == want)
                .ok_or_else(|| format!("Unknown server. Known: {}", names(servers)))
        }
        None if servers.len() == 1 => Ok(&servers[0]),
        None if servers.is_empty() => Err("No game servers are registered.".into()),
        None => Err(format!("Which server? Known: {}", names(servers))),
    }
}

fn names(servers: &[Server]) -> String {
    if servers.is_empty() {
        return "none".into();
    }
    servers
        .iter()
        .map(|s| alias(&s.label))
        .collect::<Vec<_>>()
        .join(", ")
}

/// One-line status summary from a `gameserver.status.query` result.
pub fn status_line(label: &str, st: &Value, with_players: bool) -> String {
    let n = |k: &str| st.get(k).and_then(Value::as_u64).unwrap_or(0);
    let mut s = format!("{label}: {}/{} players", n("players"), n("max_players"));
    if let Some(m) = st
        .get("map")
        .and_then(Value::as_str)
        .filter(|m| !m.is_empty())
    {
        s.push_str(&format!(", map {m}"));
    }
    if let Some(v) = st
        .get("version")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
    {
        s.push_str(&format!(", v{v}"));
    }
    if with_players {
        let names: Vec<&str> = st
            .get("player_names")
            .and_then(Value::as_array)
            .map_or_else(Vec::new, |a| a.iter().filter_map(Value::as_str).collect());
        if names.is_empty() {
            s.push_str(" (no player names reported)");
        } else {
            s.push_str(&format!(" [{}]", names.join(", ")));
        }
    }
    s
}

fn query(host: &dyn Host, s: &Server, players: bool) -> Result<String, ToolFail> {
    let st = host.invoke(
        TOOL_STATUS,
        &json!({"server": s.id, "include_players": players}),
        None,
    )?;
    Ok(status_line(&alias(&s.label), &st, players))
}

/// Status of one named server, or of every enabled server (capped).
fn cmd_status(host: &dyn Host, name: Option<&str>) -> Result<String, ToolFail> {
    let servers = list_servers(host)?;
    if name.is_none() && servers.len() > 1 {
        let mut lines = Vec::new();
        for s in servers.iter().take(MAX_STATUS_LINES) {
            lines.push(match query(host, s, false) {
                Ok(l) => l,
                Err(f) => format!("{}: {}", alias(&s.label), fail_text(&f)),
            });
        }
        return Ok(lines.join(" | "));
    }
    match resolve(&servers, name) {
        Ok(s) => query(host, s, false),
        Err(msg) => Ok(msg),
    }
}

fn cmd_players(host: &dyn Host, name: Option<&str>) -> Result<String, ToolFail> {
    let servers = list_servers(host)?;
    match resolve(&servers, name) {
        Ok(s) => query(host, s, true),
        Err(msg) => Ok(msg),
    }
}

/// Trims RCON output into one chat-sized line.
pub fn reply_output(out: &Value) -> String {
    let text = out.get("output").and_then(Value::as_str).unwrap_or("");
    let one: String = text
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>()
        .join(" | ");
    let mut res: String = one.chars().take(MAX_REPLY_OUTPUT).collect();
    if res.is_empty() {
        res = "(no output)".into();
    }
    if out
        .get("truncated")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        || one.chars().count() > MAX_REPLY_OUTPUT
    {
        res.push_str(" (truncated)");
    }
    res
}

fn cmd_rcon(host: &dyn Host, rest: &[&str], message_id: &str) -> Result<String, ToolFail> {
    if rest.len() < 2 {
        return Ok("Usage: !rcon <name> <command>".into());
    }
    if message_id.is_empty() {
        return Ok("Cannot run an RCON command without a message id.".into());
    }
    let servers = list_servers(host)?;
    let s = match resolve(&servers, Some(rest[0])) {
        Ok(s) => s,
        Err(msg) => return Ok(msg),
    };
    let command = rest[1..].join(" ");
    let key = format!("game-servers.rcon.{message_id}");
    let out = host.invoke(
        TOOL_EXEC,
        &json!({"server": s.id, "command": command}),
        Some(&key),
    )?;
    Ok(reply_output(&out))
}

/// Handles one chat line. `None` = not our command (no reply, no work).
pub fn handle_chat(host: &dyn Host, text: &str, message_id: &str) -> Option<String> {
    let toks: Vec<&str> = text.split_whitespace().collect();
    let head = toks.first()?.to_lowercase();
    let result = match head.as_str() {
        "!rcon" => cmd_rcon(host, &toks[1..], message_id),
        "!server" => match toks.get(1).map(|t| t.to_lowercase()).as_deref() {
            None | Some("status") => cmd_status(host, toks.get(2).copied()),
            Some("players") => cmd_players(host, toks.get(2).copied()),
            Some("list") => list_servers(host).map(|s| format!("Servers: {}", names(&s))),
            Some(_) => Ok(USAGE.to_string()),
        },
        _ => return None,
    };
    Some(match result {
        Ok(t) => t,
        Err(f) => {
            host.log(
                "game-servers command refused",
                &json!({"command": head, "code": fail_code(&f)}),
            );
            fail_text(&f)
        }
    })
}

/// Machine code for logs (never user text).
pub fn fail_code(f: &ToolFail) -> String {
    match f {
        ToolFail::NotGranted => "not_granted".into(),
        ToolFail::Forbidden => "forbidden".into(),
        ToolFail::Timeout => "timeout".into(),
        ToolFail::Unavailable => "unavailable".into(),
        ToolFail::Invalid(c) => c.chars().take(48).collect(),
    }
}

/// Scheduled status-post settings from the bundle config.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PostConfig {
    pub platform: String,
    pub channel_id: String,
    /// Server aliases to include (empty = every enabled server).
    pub servers: Vec<String>,
}

/// Reads the scheduled-post config. `Ok(None)` = disabled; enabled but
/// incomplete is a loud error, never a silent skip.
pub fn post_config(config: &Value) -> Result<Option<PostConfig>, String> {
    if !config
        .get("status_post_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return Ok(None);
    }
    let get = |k: &str| {
        config
            .get(k)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
    };
    let platform = get("status_post_platform").ok_or("status_post_platform required")?;
    if !matches!(platform.as_str(), "twitch" | "discord") {
        return Err("status_post_platform must be twitch|discord".into());
    }
    let channel_id = get("status_post_channel_id").ok_or("status_post_channel_id required")?;
    let servers = config
        .get("status_post_servers")
        .and_then(Value::as_array)
        .map_or_else(Vec::new, |a| {
            a.iter().filter_map(Value::as_str).map(alias).collect()
        });
    Ok(Some(PostConfig {
        platform,
        channel_id,
        servers,
    }))
}

/// Builds the scheduled status text (one line per server, capped).
pub fn scheduled_status(host: &dyn Host, cfg: &PostConfig) -> Result<String, ToolFail> {
    let servers: Vec<Server> = list_servers(host)?
        .into_iter()
        .filter(|s| cfg.servers.is_empty() || cfg.servers.contains(&alias(&s.label)))
        .take(MAX_STATUS_LINES)
        .collect();
    if servers.is_empty() {
        return Ok("No game servers to report.".into());
    }
    let lines: Vec<String> = servers
        .iter()
        .map(|s| match query(host, s, false) {
            Ok(l) => l,
            Err(f) => format!("{}: {}", alias(&s.label), fail_text(&f)),
        })
        .collect();
    Ok(lines.join(" | "))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    /// Scripted service: answers by tool name, records every call.
    struct Fake {
        servers: Value,
        status: Result<Value, ToolFail>,
        exec: Result<Value, ToolFail>,
        calls: RefCell<Vec<(String, Value, Option<String>)>>,
        logs: RefCell<Vec<Value>>,
    }

    impl Fake {
        fn new() -> Self {
            Fake {
                servers: json!({"servers": [
                    {"server": "11111111-2222-3333-4444-555555555555", "label": "Main Arena", "game": "source", "enabled": true},
                    {"server": "22222222-2222-3333-4444-555555555555", "label": "Creative", "game": "minecraft", "enabled": true},
                    {"server": "33333333-2222-3333-4444-555555555555", "label": "Old", "game": "source", "enabled": false}
                ]}),
                status: Ok(
                    json!({"players": 3, "max_players": 16, "map": "de_dust2", "version": "1.0", "player_names": ["a", "b"]}),
                ),
                exec: Ok(json!({"output": "hostname: x\nplayers: 3", "truncated": false})),
                calls: RefCell::new(vec![]),
                logs: RefCell::new(vec![]),
            }
        }
    }

    impl Host for Fake {
        fn invoke(&self, tool: &str, args: &Value, idem: Option<&str>) -> Result<Value, ToolFail> {
            self.calls
                .borrow_mut()
                .push((tool.into(), args.clone(), idem.map(Into::into)));
            match tool {
                TOOL_LIST => Ok(self.servers.clone()),
                TOOL_STATUS => self.status.clone(),
                TOOL_EXEC => self.exec.clone(),
                other => panic!("unexpected tool {other}"),
            }
        }
        fn log(&self, _: &str, f: &Value) {
            self.logs.borrow_mut().push(f.clone());
        }
    }

    #[test]
    fn contract_fixture_calls_use_the_catalog_tool_names_and_arg_shapes() {
        let h = Fake::new();
        handle_chat(&h, "!server players main-arena", "m1").unwrap();
        handle_chat(&h, "!rcon creative say hi", "m2").unwrap();
        let c = h.calls.borrow();
        assert_eq!(c[1].0, "gameserver.status.query");
        assert_eq!(
            c[1].1,
            json!({"server": "11111111-2222-3333-4444-555555555555", "include_players": true})
        );
        assert_eq!(c[3].0, "gameserver.rcon.exec");
        assert_eq!(
            c[3].1,
            json!({"server": "22222222-2222-3333-4444-555555555555", "command": "say hi"})
        );
        assert_eq!(c[3].2.as_deref(), Some("game-servers.rcon.m2"));
        assert_eq!(SERVICE, "waddles.core.service.gameserver");
    }

    #[test]
    fn non_commands_are_ignored_without_any_tool_call() {
        let h = Fake::new();
        assert!(handle_chat(&h, "hello world", "m").is_none());
        assert!(handle_chat(&h, "", "m").is_none());
        assert!(handle_chat(&h, "!serverfoo", "m").is_none());
        assert!(h.calls.borrow().is_empty());
    }

    #[test]
    fn status_with_many_servers_summarises_all_enabled() {
        let h = Fake::new();
        let r = handle_chat(&h, "!server", "m").unwrap();
        assert!(
            r.contains("main-arena: 3/16 players, map de_dust2, v1.0"),
            "{r}"
        );
        assert!(r.contains("creative:"));
        assert!(!r.contains("old"));
    }

    #[test]
    fn named_status_and_unknown_name() {
        let h = Fake::new();
        assert!(
            handle_chat(&h, "!server status Main-Arena", "m")
                .unwrap()
                .starts_with("main-arena:")
        );
        let r = handle_chat(&h, "!server status nope", "m").unwrap();
        assert_eq!(r, "Unknown server. Known: main-arena, creative");
    }

    #[test]
    fn players_requires_a_name_when_ambiguous_and_lists_names() {
        let h = Fake::new();
        assert!(
            handle_chat(&h, "!server players", "m")
                .unwrap()
                .starts_with("Which server?")
        );
        let r = handle_chat(&h, "!server players creative", "m").unwrap();
        assert!(r.ends_with("[a, b]"), "{r}");
    }

    #[test]
    fn list_and_usage() {
        let h = Fake::new();
        assert_eq!(
            handle_chat(&h, "!server list", "m").unwrap(),
            "Servers: main-arena, creative"
        );
        assert!(
            handle_chat(&h, "!server frobnicate", "m")
                .unwrap()
                .starts_with("Usage:")
        );
    }

    #[test]
    fn rcon_needs_args_and_a_message_id() {
        let h = Fake::new();
        assert!(handle_chat(&h, "!rcon", "m").unwrap().starts_with("Usage:"));
        assert!(
            handle_chat(&h, "!rcon main-arena", "m")
                .unwrap()
                .starts_with("Usage:")
        );
        assert!(
            handle_chat(&h, "!rcon main-arena status", "")
                .unwrap()
                .contains("message id")
        );
        assert!(h.calls.borrow().iter().all(|c| c.0 != TOOL_EXEC));
    }

    #[test]
    fn rcon_output_is_one_line_and_truncation_flagged() {
        let h = Fake::new();
        assert_eq!(
            handle_chat(&h, "!rcon main-arena status", "m").unwrap(),
            "hostname: x | players: 3"
        );
        let mut h = Fake::new();
        h.exec = Ok(json!({"output": "x".repeat(900), "truncated": true}));
        let r = handle_chat(&h, "!rcon main-arena status", "m").unwrap();
        assert!(r.ends_with("(truncated)") && r.len() < 460);
        h.exec = Ok(json!({"output": "", "truncated": false}));
        assert_eq!(
            handle_chat(&h, "!rcon main-arena x", "m2").unwrap(),
            "(no output)"
        );
    }

    #[test]
    fn service_refusals_become_friendly_replies_and_are_logged_by_code() {
        for (fail, want) in [
            (ToolFail::Forbidden, "You are not allowed"),
            (ToolFail::NotGranted, "not approved"),
            (ToolFail::Unavailable, "unavailable"),
            (ToolFail::Invalid("command_not_allowed".into()), "allowlist"),
            (ToolFail::Invalid("rate_limited".into()), "rate limit"),
            (
                ToolFail::Invalid("duplicate_request".into()),
                "already sent",
            ),
            (
                ToolFail::Invalid("denied:blocked_address".into()),
                "blocked",
            ),
            (ToolFail::Invalid("weird".into()), "Request refused (weird)"),
        ] {
            let mut h = Fake::new();
            h.exec = Err(fail.clone());
            let r = handle_chat(&h, "!rcon main-arena status", "m").unwrap();
            assert!(r.contains(want), "{r} !~ {want}");
            assert_eq!(h.logs.borrow().len(), 1);
            // The log carries a code, never the typed command text.
            assert!(!h.logs.borrow()[0].to_string().contains("status"));
        }
    }

    #[test]
    fn a_failing_server_does_not_break_the_summary() {
        let mut h = Fake::new();
        h.status = Err(ToolFail::Unavailable);
        let r = handle_chat(&h, "!server", "m").unwrap();
        assert!(
            r.contains("main-arena: The game server service is unavailable"),
            "{r}"
        );
    }

    #[test]
    fn post_config_is_loud_when_incomplete() {
        assert_eq!(post_config(&json!({})).unwrap(), None);
        assert!(post_config(&json!({"status_post_enabled": true})).is_err());
        assert!(post_config(&json!({"status_post_enabled": true, "status_post_platform": "irc", "status_post_channel_id": "1"})).is_err());
        let c = post_config(&json!({"status_post_enabled": true, "status_post_platform": "discord", "status_post_channel_id": "42", "status_post_servers": ["Main Arena"]})).unwrap().unwrap();
        assert_eq!(c.servers, vec!["main-arena".to_string()]);
    }

    #[test]
    fn scheduled_status_filters_to_configured_servers() {
        let h = Fake::new();
        let cfg = PostConfig {
            platform: "discord".into(),
            channel_id: "1".into(),
            servers: vec!["creative".into()],
        };
        let t = scheduled_status(&h, &cfg).unwrap();
        assert!(t.starts_with("creative:") && !t.contains('|'), "{t}");
        let none = PostConfig {
            servers: vec!["ghost".into()],
            ..cfg
        };
        assert_eq!(
            scheduled_status(&h, &none).unwrap(),
            "No game servers to report."
        );
    }
}
