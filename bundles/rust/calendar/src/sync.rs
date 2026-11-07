//! Event sync engine: pushes calendar events to external targets by CALLING service-bundle tools
//! (`discord.scheduled_event.*`, `twitch.schedule.*`, `wix.event.*`). The calendar is the source
//! of truth (push-only). Target adapters only build tool payloads and read remote ids; the engine
//! owns idempotency, the event<->remote mapping, bounded retry and reconcile, generically.
//!
//! Idempotency key = sha256(community : event public_id : op : event version); an applied key is
//! recorded in kv and a replay is a no-op. A Create is additionally guarded by the mapping's
//! `remote_id` so a retry can never double-create.

use serde_json::{Value, json};
use waddle_sdk::tool::ToolError;

use crate::host::{CalError, Ctx};
use crate::model::{Config, Event, SyncOp, SyncRecord, SyncStatus, Target, sha256_hex};
use crate::store;
use crate::timeutil::{to_rfc3339, truncate};

/// Inline attempts per call (kept inside the host tool budget).
const INLINE_ATTEMPTS: u32 = 2;
/// Lifetime attempts before a pending op is parked as a permanent error.
const MAX_ATTEMPTS: u32 = 8;

/// A fully built tool invocation.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    pub tool: String,
    pub args: Value,
}

/// Why a payload could not be built (permanent: the event cannot be represented on the target).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BuildError(pub String);

/// A sync target: builds payloads for its service bundle's tools, nothing else.
pub trait SyncAdapter {
    /// The target this adapter serves.
    fn target(&self) -> Target;
    /// `app_id` of the service bundle that owns the tools.
    fn app_id(&self) -> &'static str;
    /// Builds the tool call for `op`; `remote_id` is the mapped remote object, if any.
    fn build(
        &self,
        op: SyncOp,
        ev: &Event,
        remote_id: Option<&str>,
        cfg: &Config,
    ) -> Result<ToolCall, BuildError>;
    /// Extracts the remote object id from a tool result.
    fn remote_id(&self, result: &Value) -> Option<String>;
    /// True when `Cancel` is implemented as a remote delete (the remote id is then gone).
    fn cancel_clears_remote(&self) -> bool {
        false
    }
}

fn id_field(result: &Value, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|k| match result.get(*k) {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        Some(Value::Number(n)) => Some(n.to_string()),
        _ => None,
    })
}

/// Discord scheduled events (`discord.scheduled_event.*`, schema requires name/start/end/location).
pub struct DiscordAdapter;

impl SyncAdapter for DiscordAdapter {
    fn target(&self) -> Target {
        Target::Discord
    }
    fn app_id(&self) -> &'static str {
        "waddles.core.service.discord"
    }
    fn build(
        &self,
        op: SyncOp,
        ev: &Event,
        remote_id: Option<&str>,
        cfg: &Config,
    ) -> Result<ToolCall, BuildError> {
        let rid = || {
            remote_id
                .map(str::to_string)
                .ok_or_else(|| BuildError("missing remote event id".into()))
        };
        let full = |with_id: bool| -> Result<Value, BuildError> {
            let location = ev
                .location
                .clone()
                .filter(|l| !l.is_empty())
                .unwrap_or_else(|| cfg.default_location.clone());
            let mut v = json!({
                "name": truncate(&ev.title, 100),
                "scheduled_start_time": to_rfc3339(ev.start).map_err(|e| BuildError(e.to_string()))?,
                "scheduled_end_time": to_rfc3339(ev.end).map_err(|e| BuildError(e.to_string()))?,
                "location": truncate(&location, 100),
            });
            if !ev.description.is_empty() {
                v["description"] = json!(truncate(&ev.description, 1000));
            }
            if with_id {
                v["event_id"] = json!(rid()?);
            }
            Ok(v)
        };
        let (tool, args) = match op {
            SyncOp::Create => ("discord.scheduled_event.create", full(false)?),
            SyncOp::Update => ("discord.scheduled_event.update", full(true)?),
            SyncOp::Cancel => (
                "discord.scheduled_event.cancel",
                json!({"event_id": rid()?}),
            ),
            SyncOp::Delete => (
                "discord.scheduled_event.delete",
                json!({"event_id": rid()?}),
            ),
        };
        Ok(ToolCall {
            tool: tool.to_string(),
            args,
        })
    }
    fn remote_id(&self, result: &Value) -> Option<String> {
        id_field(result, &["event_id", "id"])
    }
}

/// Twitch stream schedule segments (`twitch.schedule.*`, Helix `/schedule/segment`). Waddles only
/// pushes single (non-recurring) instances; cancel is a segment delete.
pub struct TwitchAdapter;

impl SyncAdapter for TwitchAdapter {
    fn target(&self) -> Target {
        Target::Twitch
    }
    fn app_id(&self) -> &'static str {
        "waddles.core.service.twitch"
    }
    fn build(
        &self,
        op: SyncOp,
        ev: &Event,
        remote_id: Option<&str>,
        _cfg: &Config,
    ) -> Result<ToolCall, BuildError> {
        let rid = || {
            remote_id
                .map(str::to_string)
                .ok_or_else(|| BuildError("missing remote segment id".into()))
        };
        let body = |with_id: bool| -> Result<Value, BuildError> {
            let minutes = ((ev.end - ev.start) / 60).clamp(30, 1380);
            let mut v = json!({
                "start_time": to_rfc3339(ev.start).map_err(|e| BuildError(e.to_string()))?,
                "timezone": ev.tz,
                "duration": minutes,
                "title": truncate(&ev.title, 140),
                "is_recurring": false,
            });
            if with_id {
                v["segment_id"] = json!(rid()?);
            }
            Ok(v)
        };
        let (tool, args) = match op {
            SyncOp::Create => ("twitch.schedule.create", body(false)?),
            SyncOp::Update => ("twitch.schedule.update", body(true)?),
            SyncOp::Cancel | SyncOp::Delete => {
                ("twitch.schedule.delete", json!({"segment_id": rid()?}))
            }
        };
        Ok(ToolCall {
            tool: tool.to_string(),
            args,
        })
    }
    fn remote_id(&self, result: &Value) -> Option<String> {
        id_field(result, &["segment_id", "id"])
    }
    fn cancel_clears_remote(&self) -> bool {
        true
    }
}

/// Wix Events (`wix.event.{create,update,cancel}`); there is no delete tool, so delete == cancel.
pub struct WixAdapter;

impl SyncAdapter for WixAdapter {
    fn target(&self) -> Target {
        Target::Wix
    }
    fn app_id(&self) -> &'static str {
        "waddles.core.service.wix"
    }
    fn build(
        &self,
        op: SyncOp,
        ev: &Event,
        remote_id: Option<&str>,
        cfg: &Config,
    ) -> Result<ToolCall, BuildError> {
        let rid = || {
            remote_id
                .map(str::to_string)
                .ok_or_else(|| BuildError("missing remote event id".into()))
        };
        let body = |with_id: bool| -> Result<Value, BuildError> {
            let location = ev
                .location
                .clone()
                .filter(|l| !l.is_empty())
                .unwrap_or_else(|| cfg.default_location.clone());
            let mut v = json!({
                "title": truncate(&ev.title, 100),
                "description": truncate(&ev.description, 1000),
                "start_date": to_rfc3339(ev.start).map_err(|e| BuildError(e.to_string()))?,
                "end_date": to_rfc3339(ev.end).map_err(|e| BuildError(e.to_string()))?,
                "timezone": ev.tz,
                "location": truncate(&location, 100),
            });
            if with_id {
                v["event_id"] = json!(rid()?);
            }
            Ok(v)
        };
        let (tool, args) = match op {
            SyncOp::Create => ("wix.event.create", body(false)?),
            SyncOp::Update => ("wix.event.update", body(true)?),
            SyncOp::Cancel | SyncOp::Delete => ("wix.event.cancel", json!({"event_id": rid()?})),
        };
        Ok(ToolCall {
            tool: tool.to_string(),
            args,
        })
    }
    fn remote_id(&self, result: &Value) -> Option<String> {
        id_field(result, &["event_id", "id"])
    }
}

/// The adapter for `t`.
pub fn adapter_for(t: Target) -> &'static dyn SyncAdapter {
    match t {
        Target::Discord => &DiscordAdapter,
        Target::Twitch => &TwitchAdapter,
        Target::Wix => &WixAdapter,
    }
}

/// Stable idempotency key (hex) for (community, event, target, op, version).
pub fn idempotency_key(community: &str, ev: &Event, op: SyncOp, t: Target) -> String {
    sha256_hex(
        format!(
            "{community}:{}:{}:{}:{}",
            ev.public_id,
            t.name(),
            op.name(),
            ev.version
        )
        .as_bytes(),
    )
}

fn permanent(rec: &mut SyncRecord, code: &str) {
    rec.status = SyncStatus::Error;
    rec.last_error_code = Some(code.to_string());
    rec.pending_op = None;
}

/// Records an owed operation for every enabled target. Create/Update collapse into one
/// upsert intent resolved against the mapping at execution time.
pub fn enqueue(ctx: &Ctx, cfg: &Config, ev: &Event, op: SyncOp) -> Result<(), CalError> {
    let intent = match op {
        SyncOp::Create => SyncOp::Update,
        other => other,
    };
    let mut any = false;
    for t in Target::ALL {
        if !cfg.sync_enabled(t) {
            continue;
        }
        let mut rec = store::sync_record(ctx.kv, ev.id, t)?.unwrap_or_else(|| SyncRecord::new(t));
        // Never downgrade an owed Delete/Cancel back into an upsert.
        if matches!(rec.pending_op, Some(SyncOp::Delete)) && intent != SyncOp::Delete {
            continue;
        }
        rec.pending_op = Some(intent);
        rec.status = SyncStatus::Pending;
        rec.last_error_code = None;
        store::put_sync_record(ctx.kv, ev.id, &rec)?;
        any = true;
    }
    if any {
        let mut q = store::pending(ctx.kv)?;
        if !q.contains(&ev.id) {
            q.push(ev.id);
            store::set_pending(ctx.kv, &q)?;
        }
    }
    Ok(())
}

/// Result of one target run, for logging/metrics (`op`, `outcome` only).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunOutcome {
    pub target: Target,
    pub op: Option<SyncOp>,
    pub outcome: &'static str,
}

fn run_target(
    ctx: &Ctx,
    cfg: &Config,
    ev: &Event,
    rec: &mut SyncRecord,
) -> Result<RunOutcome, CalError> {
    let adapter = adapter_for(rec.target);
    let target = rec.target;
    let Some(intent) = rec.pending_op else {
        return Ok(RunOutcome {
            target,
            op: None,
            outcome: "idle",
        });
    };
    if matches!(intent, SyncOp::Create | SyncOp::Update)
        && ev.status != crate::model::Status::Scheduled
    {
        // Approval gate inside the engine: unapproved content never reaches a remote target.
        // The intent is dropped; approval re-queues it.
        rec.pending_op = None;
        return Ok(RunOutcome {
            target,
            op: Some(intent),
            outcome: "dropped_unapproved",
        });
    }
    let mut recreated = false;
    loop {
        let op = match (intent, rec.remote_id.is_some()) {
            (SyncOp::Create | SyncOp::Update, false) => SyncOp::Create,
            (SyncOp::Create | SyncOp::Update, true) => SyncOp::Update,
            (SyncOp::Cancel, true) => SyncOp::Cancel,
            (SyncOp::Delete, true) => SyncOp::Delete,
            (SyncOp::Cancel | SyncOp::Delete, false) => {
                // Nothing exists remotely: the owed removal is already satisfied.
                rec.pending_op = None;
                rec.status = SyncStatus::Synced;
                return Ok(RunOutcome {
                    target,
                    op: Some(intent),
                    outcome: "noop",
                });
            }
        };
        let call = match adapter.build(op, ev, rec.remote_id.as_deref(), cfg) {
            Ok(c) => c,
            Err(_) => {
                permanent(rec, "payload_invalid");
                return Ok(RunOutcome {
                    target,
                    op: Some(op),
                    outcome: "payload_invalid",
                });
            }
        };
        // Hash the event content only (not the remote-id field) so create and update compare.
        let mut hashed = call.args.clone();
        if let Some(o) = hashed.as_object_mut() {
            o.remove("event_id");
            o.remove("segment_id");
        }
        let payload_hash = sha256_hex(hashed.to_string().as_bytes());
        if matches!(op, SyncOp::Update)
            && rec.last_synced_hash.as_deref() == Some(payload_hash.as_str())
        {
            rec.pending_op = None;
            rec.status = SyncStatus::Synced;
            return Ok(RunOutcome {
                target,
                op: Some(op),
                outcome: "unchanged",
            });
        }
        let key = idempotency_key(ctx.community, ev, op, target);
        if store::idem_applied(ctx.kv, &key)? && rec.remote_id.is_some() == (op != SyncOp::Create) {
            rec.pending_op = None;
            rec.status = SyncStatus::Synced;
            return Ok(RunOutcome {
                target,
                op: Some(op),
                outcome: "replayed",
            });
        }

        let mut last_err: Option<ToolError> = None;
        let mut result: Option<Value> = None;
        for _ in 0..INLINE_ATTEMPTS {
            match ctx
                .tools
                .invoke(adapter.app_id(), &call.tool, &call.args, &key)
            {
                Ok(v) => {
                    result = Some(v);
                    break;
                }
                Err(
                    e @ (ToolError::Timeout
                    | ToolError::PoolExhausted
                    | ToolError::TargetUnavailable),
                ) => {
                    last_err = Some(e);
                }
                Err(e) => {
                    last_err = Some(e);
                    break;
                }
            }
        }
        rec.attempts += 1;

        let Some(res) = result else {
            let err = last_err.unwrap_or(ToolError::TargetUnavailable);
            return Ok(match err {
                ToolError::Timeout | ToolError::PoolExhausted | ToolError::TargetUnavailable => {
                    if rec.attempts >= MAX_ATTEMPTS {
                        permanent(rec, "retries_exhausted");
                        RunOutcome {
                            target,
                            op: Some(op),
                            outcome: "retries_exhausted",
                        }
                    } else {
                        rec.status = SyncStatus::Pending;
                        rec.last_error_code = Some("transient".into());
                        RunOutcome {
                            target,
                            op: Some(op),
                            outcome: "transient",
                        }
                    }
                }
                ToolError::NotGranted => {
                    permanent(rec, "not_granted");
                    RunOutcome {
                        target,
                        op: Some(op),
                        outcome: "not_granted",
                    }
                }
                ToolError::Forbidden | ToolError::DepthExceeded | ToolError::NoTransport => {
                    permanent(rec, "forbidden");
                    RunOutcome {
                        target,
                        op: Some(op),
                        outcome: "forbidden",
                    }
                }
                ToolError::InvalidArgs(m) => {
                    let gone =
                        m.contains("404") || m.contains("not_found") || m.contains("unknown");
                    if gone && op != SyncOp::Create {
                        match op {
                            // Already gone remotely: removal satisfied.
                            SyncOp::Cancel | SyncOp::Delete => {
                                rec.remote_id = None;
                                rec.pending_op = None;
                                rec.status = SyncStatus::Synced;
                                return Ok(RunOutcome {
                                    target,
                                    op: Some(op),
                                    outcome: "remote_gone",
                                });
                            }
                            // Drifted: recreate exactly once.
                            _ if !recreated => {
                                recreated = true;
                                rec.remote_id = None;
                                rec.last_synced_hash = None;
                                continue;
                            }
                            _ => {}
                        }
                    }
                    permanent(rec, "invalid_args");
                    RunOutcome {
                        target,
                        op: Some(op),
                        outcome: "invalid_args",
                    }
                }
            });
        };

        let remote = adapter.remote_id(&res);
        match op {
            SyncOp::Create => match remote {
                Some(id) => rec.remote_id = Some(id),
                None => {
                    // The create may have happened remotely; never blind-retry (duplicate risk).
                    store::mark_idem(ctx.kv, &key)?;
                    permanent(rec, "remote_id_missing");
                    return Ok(RunOutcome {
                        target,
                        op: Some(op),
                        outcome: "remote_id_missing",
                    });
                }
            },
            SyncOp::Update => {
                if let Some(id) = remote {
                    rec.remote_id = Some(id);
                }
            }
            SyncOp::Cancel => {
                if adapter.cancel_clears_remote() {
                    rec.remote_id = None;
                }
            }
            SyncOp::Delete => rec.remote_id = None,
        }
        if matches!(op, SyncOp::Create | SyncOp::Update) {
            rec.last_synced_hash = Some(payload_hash);
        } else {
            rec.last_synced_hash = None;
        }
        store::mark_idem(ctx.kv, &key)?;
        rec.pending_op = None;
        rec.status = SyncStatus::Synced;
        rec.last_error_code = None;
        return Ok(RunOutcome {
            target,
            op: Some(op),
            outcome: "ok",
        });
    }
}

/// Runs every owed op for one event on enabled targets, persisting mappings and queue state.
pub fn flush_event(ctx: &Ctx, cfg: &Config, ev: &Event) -> Result<Vec<RunOutcome>, CalError> {
    let mut out = Vec::new();
    for t in Target::ALL {
        if !cfg.sync_enabled(t) {
            continue;
        }
        let Some(mut rec) = store::sync_record(ctx.kv, ev.id, t)? else {
            continue;
        };
        if rec.pending_op.is_none() || rec.status == SyncStatus::Error {
            continue;
        }
        let o = run_target(ctx, cfg, ev, &mut rec)?;
        store::put_sync_record(ctx.kv, ev.id, &rec)?;
        let healthy = matches!(
            o.outcome,
            "ok" | "noop" | "unchanged" | "replayed" | "remote_gone" | "dropped_unapproved"
        );
        ctx.log.log(
            if healthy { "info" } else { "warn" },
            "calendar sync",
            serde_json::json!({
                "target": o.target.name(),
                "op": o.op.map(SyncOp::name),
                "outcome": o.outcome,
                "event_id": ev.id,
            }),
        );
        out.push(o);
    }
    refresh_queue(ctx, ev.id)?;
    Ok(out)
}

/// Clears waiting Create/Update (upsert) intents for an event that left `Scheduled` (kept
/// Cancel/Delete intents still run) and refreshes the queue.
pub fn drop_upserts(ctx: &Ctx, id: u64) -> Result<(), CalError> {
    for t in Target::ALL {
        if let Some(mut r) = store::sync_record(ctx.kv, id, t)?
            && matches!(r.pending_op, Some(SyncOp::Create | SyncOp::Update))
        {
            r.pending_op = None;
            if r.remote_id.is_some() {
                r.status = SyncStatus::Synced;
            }
            store::put_sync_record(ctx.kv, id, &r)?;
        }
    }
    refresh_queue(ctx, id)
}

/// Keeps the pending queue in step with the event's mappings.
fn refresh_queue(ctx: &Ctx, id: u64) -> Result<(), CalError> {
    let mut owed = false;
    for t in Target::ALL {
        if let Some(r) = store::sync_record(ctx.kv, id, t)?
            && r.pending_op.is_some()
            && r.status != SyncStatus::Error
        {
            owed = true;
        }
    }
    let mut q = store::pending(ctx.kv)?;
    let present = q.contains(&id);
    if owed && !present {
        q.push(id);
        store::set_pending(ctx.kv, &q)?;
    } else if !owed && present {
        q.retain(|x| *x != id);
        store::set_pending(ctx.kv, &q)?;
    }
    Ok(())
}

/// Reconcile pass: a bounded batch of events that still owe sync work. Called after every event
/// touch and by the periodic reconcile hook once the host exposes one. Returns outcomes.
pub fn reconcile(ctx: &Ctx, cfg: &Config, limit: usize) -> Result<Vec<RunOutcome>, CalError> {
    let mut out = Vec::new();
    for id in store::pending(ctx.kv)?.into_iter().take(limit) {
        match store::event(ctx.kv, id)? {
            Some(ev) => out.extend(flush_event(ctx, cfg, &ev)?),
            None => {
                let mut q = store::pending(ctx.kv)?;
                q.retain(|x| *x != id);
                store::set_pending(ctx.kv, &q)?;
            }
        }
    }
    Ok(out)
}

/// Queues an upsert for every upcoming scheduled event that has no mapping yet on `t`
/// (used when sync is switched on).
pub fn backfill(ctx: &Ctx, cfg: &Config, t: Target) -> Result<usize, CalError> {
    let now = ctx.clock.now_secs();
    let mut n = 0;
    for id in store::index(ctx.kv)? {
        let Some(ev) = store::event(ctx.kv, id)? else {
            continue;
        };
        if ev.status != crate::model::Status::Scheduled || ev.end < now {
            continue;
        }
        if store::sync_record(ctx.kv, id, t)?.is_some() {
            continue;
        }
        enqueue(ctx, cfg, &ev, SyncOp::Create)?;
        n += 1;
    }
    Ok(n)
}
