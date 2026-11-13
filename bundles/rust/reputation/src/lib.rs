//! Reputation SERVICE bundle (v1.1.1).
//!
//! An always-on `kind: service` bundle on `world stage-tools`: it exports
//! `tool-provider` (the surface other bundles reach through `tool.invoke`) and
//! wraps the `reputation` host capability. All tool logic lives in [`logic`]
//! (host-testable behind three seams); this file only supplies the real wasm
//! implementations of those seams -- the generated `reputation`/`kv`/`flags`/
//! `tool`/`log` imports -- and routes `tool-provider.call` through the SDK
//! registry. Unknown tools fail LOUD with a typed `invalid-args`.
#[allow(warnings)]
mod bindings;
mod logic;

use std::sync::{Arc, OnceLock};

use bindings::exports::waddle::bundle::tool_provider::Guest;
use bindings::waddle::bundle::tool::ToolError as WitError;
use bindings::waddle::bundle::{flags, kv, log, reputation, tool};
use logic::{
    Applied, ConfigStore, Entry, Env, HistoryRow, HostError, Platform, RepHost, TenantPolicy,
    TenantScore,
};
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

fn applied(o: reputation::ApplyOutcome) -> Applied {
    Applied {
        new_score: o.new_score,
        applied: o.applied,
    }
}

impl RepHost for WasmHost {
    /// Scores via the host's single-round-trip batch read.
    fn get_many(&self, users: &[String]) -> Result<Vec<Entry>, HostError> {
        reputation::get_many(users).map(entries).map_err(host_err)
    }

    /// Activity-class idempotent delta.
    fn apply(&self, user: &str, delta: i32, reason: &str) -> Result<Applied, HostError> {
        reputation::apply(user, delta, reason)
            .map(applied)
            .map_err(host_err)
    }

    /// Moderation-class idempotent delta (needs `reputation.moderation.write`).
    fn apply_moderation(&self, user: &str, delta: i32, reason: &str) -> Result<Applied, HostError> {
        reputation::apply_moderation(user, delta, reason)
            .map(applied)
            .map_err(host_err)
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

    /// The user's tenant aggregate.
    fn get_tenant(&self, user: &str) -> Result<TenantScore, HostError> {
        reputation::get_tenant(user)
            .map(|t| TenantScore {
                score: t.score,
                communities: t.communities,
            })
            .map_err(host_err)
    }

    /// The tenant's auto-ban policy.
    fn tenant_policy(&self) -> Result<Option<TenantPolicy>, HostError> {
        reputation::get_tenant_policy()
            .map(|p| {
                p.map(|p| TenantPolicy {
                    auto_ban_enabled: p.auto_ban_enabled,
                    auto_ban_floor: p.auto_ban_floor,
                    at_risk_threshold: p.at_risk_threshold,
                })
            })
            .map_err(host_err)
    }

    /// Newest-first ledger page.
    fn history(
        &self,
        user: &str,
        limit: u32,
        before: Option<i64>,
    ) -> Result<Vec<HistoryRow>, HostError> {
        reputation::history(user, limit, before)
            .map(|rows| {
                rows.into_iter()
                    .map(|r| HistoryRow {
                        id: r.id,
                        delta: r.delta,
                        reason_code: r.reason_code,
                        kind: r.kind,
                        balance_after: r.balance_after,
                        occurred_at: r.occurred_at,
                    })
                    .collect()
            })
            .map_err(host_err)
    }

    /// Members strictly below `threshold`.
    fn below_threshold(&self, threshold: i64, limit: u32) -> Result<Vec<Entry>, HostError> {
        reputation::below_threshold(threshold, limit)
            .map(entries)
            .map_err(host_err)
    }

    /// Soft-ban lookup.
    fn is_soft_banned(&self, user: &str) -> Result<bool, HostError> {
        reputation::is_soft_banned(user).map_err(host_err)
    }

    /// Records a soft ban.
    fn soft_ban(&self, user: &str, reason: &str) -> Result<bool, HostError> {
        reputation::soft_ban(user, reason).map_err(host_err)
    }

    /// Lifts a soft ban recorded with exactly `only_reason`.
    fn lift_soft_ban(&self, user: &str, only_reason: &str) -> Result<bool, HostError> {
        reputation::lift_soft_ban(user, only_reason).map_err(host_err)
    }
}

/// The bundle's own kv (host-scoped to this community + app).
struct WasmKv;

impl ConfigStore for WasmKv {
    /// Raw kv read; the error never carries host detail.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        kv::get(key).map_err(|_| "kv unavailable".to_string())
    }

    /// Raw kv write; the error never carries host detail.
    fn set(&self, key: &str, value: &[u8], ttl_seconds: u32) -> Result<(), String> {
        kv::set(key, value, ttl_seconds).map_err(|_| "kv unavailable".to_string())
    }
}

/// Flags + the platform ban tool.
struct WasmPlatform;

impl Platform for WasmPlatform {
    /// Feature flag, default OFF.
    fn flag_enabled(&self, key: &str) -> bool {
        flags::enabled(key, false)
    }

    /// Calls `waddles.core.service.<platform>`'s `<platform>.moderation.ban`
    /// tool; only a stable error code is returned.
    fn ban(&self, platform: &str, user: &str, reason: &str, key: &str) -> Result<(), String> {
        let target = format!("waddles.core.service.{platform}");
        let tool_name = format!("{platform}.moderation.ban");
        let args = serde_json::json!({ "user": user, "reason": reason }).to_string();
        tool::invoke(&target, &tool_name, args.as_bytes(), Some(key))
            .map(|_| ())
            .map_err(|e| match e {
                WitError::NotGranted => "not_granted".to_string(),
                WitError::Forbidden => "forbidden".to_string(),
                WitError::Timeout => "timeout".to_string(),
                WitError::DepthExceeded => "depth_exceeded".to_string(),
                WitError::PoolExhausted => "pool_exhausted".to_string(),
                WitError::TargetUnavailable => "target_unavailable".to_string(),
                WitError::InvalidArgs(_) => "invalid_args".to_string(),
            })
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

/// Emits one PII-free line: a known tool label (see `logic::tool_log_label`) and
/// outcome code only -- never args, never a caller-supplied unknown name.
fn log_outcome(tool: &str, outcome: &str) {
    let fields = serde_json::json!({ "tool": tool, "outcome": outcome }).to_string();
    log::write(log::Level::Debug, "reputation tool call", &fields);
}

static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

fn registry() -> &'static ToolRegistry {
    REGISTRY.get_or_init(|| {
        logic::build_registry(Env {
            host: Arc::new(WasmHost),
            cfg: Arc::new(WasmKv),
            platform: Arc::new(WasmPlatform),
        })
    })
}

impl Guest for Component {
    /// Routes `name` through the tool table; unknown tools are a typed error.
    fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
        let result = registry().call(&name, &args);
        log_outcome(
            logic::tool_log_label(&name),
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
