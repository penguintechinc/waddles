//! Reputation SERVICE bundle (bundle-MCP-tools P5).
//!
//! An always-on `kind: service` bundle on `world stage-tools`: it exports
//! `tool-provider` (the surface other bundles reach through `tool.invoke`) and
//! wraps the `reputation` host capability. All tool logic lives in [`logic`]
//! (host-testable behind two seams); this file only supplies the real wasm
//! implementations of those seams -- the generated `reputation`/`kv`/`log`
//! imports -- and routes `tool-provider.call` through the SDK registry.
//! Unknown tools fail LOUD with a typed `invalid-args`.
#[allow(warnings)]
mod bindings;
mod logic;

use std::sync::{Arc, OnceLock};

use bindings::exports::waddle::bundle::tool_provider::Guest;
use bindings::waddle::bundle::tool::ToolError as WitError;
use bindings::waddle::bundle::{kv, log, reputation};
use logic::{ConfigStore, Entry, HostError, RepHost};
use waddle_sdk::tool::{ToolError, ToolRegistry};

struct Component;

/// The real `reputation` host capability.
struct WasmHost;

fn host_err(e: reputation::Error) -> HostError {
    match e {
        reputation::Error::Denied(c) => HostError::Denied(c),
        reputation::Error::NotAMember => HostError::NotAMember,
        reputation::Error::DailyCapExceeded => HostError::DailyCapExceeded,
        reputation::Error::Invalid(m) => HostError::Invalid(m),
        reputation::Error::Unavailable(m) => HostError::Unavailable(m),
        reputation::Error::Backend(m) => HostError::Backend(m),
    }
}

fn entries(rows: Vec<reputation::ScoreEntry>) -> Vec<Entry> {
    rows.into_iter()
        .map(|r| Entry {
            user: r.user,
            score: r.score,
        })
        .collect()
}

impl RepHost for WasmHost {
    /// Scores via the host's single-round-trip batch read.
    fn get_many(&self, users: &[String]) -> Result<Vec<Entry>, HostError> {
        reputation::get_many(users).map(entries).map_err(host_err)
    }

    /// Batch adjust (host-keyed by event + ordinal, so replay-safe).
    fn adjust_many(&self, items: &[(String, i32)], reason: &str) -> Result<(), HostError> {
        let adjustments: Vec<reputation::Adjustment> = items
            .iter()
            .map(|(user, delta)| reputation::Adjustment {
                user: user.clone(),
                delta: *delta,
            })
            .collect();
        reputation::adjust_many(&adjustments, reason).map_err(host_err)
    }

    /// Admin override (needs the `reputation.community.admin` grant host-side).
    fn override_score(&self, user: &str, absolute: i64, reason: &str) -> Result<i64, HostError> {
        reputation::override_score(user, absolute, reason).map_err(host_err)
    }

    /// Top members by score.
    fn leaderboard(&self, limit: u32) -> Result<Vec<Entry>, HostError> {
        reputation::leaderboard(limit)
            .map(entries)
            .map_err(host_err)
    }
}

/// The bundle's own kv (host-scoped to this community + app).
struct WasmKv;

impl ConfigStore for WasmKv {
    /// Raw kv read; the error never carries host detail.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        kv::get(key).map_err(|_| "kv unavailable".to_string())
    }
}

/// Maps the SDK tool error onto the WIT one.
fn to_wit(e: ToolError) -> WitError {
    match e {
        ToolError::NotGranted => WitError::NotGranted,
        ToolError::Forbidden => WitError::Forbidden,
        ToolError::Timeout => WitError::Timeout,
        ToolError::DepthExceeded => WitError::DepthExceeded,
        ToolError::PoolExhausted => WitError::PoolExhausted,
        ToolError::TargetUnavailable => WitError::TargetUnavailable,
        ToolError::InvalidArgs(m) => WitError::InvalidArgs(m),
        ToolError::NoTransport => WitError::InvalidArgs("no tool transport installed".to_string()),
    }
}

/// Emits one PII-free line: tool name and outcome code only (never args).
fn log_outcome(tool: &str, outcome: &str) {
    let fields = serde_json::json!({ "tool": tool, "outcome": outcome }).to_string();
    log::write(log::Level::Debug, "reputation tool call", &fields);
}

static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

fn registry() -> &'static ToolRegistry {
    REGISTRY.get_or_init(|| logic::build_registry(Arc::new(WasmHost), Arc::new(WasmKv)))
}

impl Guest for Component {
    /// Routes `name` through the tool table; unknown tools are a typed error.
    fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
        let result = registry().call(&name, &args);
        log_outcome(
            &name,
            match &result {
                Ok(_) => "ok",
                Err(ToolError::InvalidArgs(_)) => "invalid_args",
                Err(ToolError::NotGranted) => "not_granted",
                Err(ToolError::Forbidden) => "forbidden",
                Err(_) => "error",
            },
        );
        result.map_err(to_wit)
    }
}

bindings::export!(Component with_types_in bindings);
