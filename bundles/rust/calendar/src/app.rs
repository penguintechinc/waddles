//! Stage-level behaviour: `transform` (parse chat into a command payload), chat execution, and
//! the admin-action surface. Host-testable; the wasm glue in `lib.rs` only adapts host imports.

use serde_json::{Value, json};

use crate::cmd::{self, Command, USAGE};
use crate::host::{CalError, Caller, Ctx};
use crate::model::{Config, Event, Status, Target};
use crate::service::{self, NewEvent, Patch};
use crate::store;
use crate::sync;
use crate::timeutil::{chat_safe, parse_tz, parse_when, render_local};

/// Platform/event type the hub-api bundle-admin gateway uses to deliver admin actions.
pub const ADMIN_PLATFORM: &str = "hub";
pub const ADMIN_EVENT_TYPE: &str = "bundle.admin";
const LIST_SHOW: usize = 8;
const RECONCILE_BATCH: usize = 3;

/// Parses a normalized `chat.message` payload into a command payload (None = not ours).
pub fn transform_chat(payload: &Value) -> Option<Value> {
    let text = payload.get("text")?.as_str()?;
    let (head, rest) = cmd::split_head(text)?;
    let command = cmd::parse(&head, &rest);
    let mut out = serde_json::to_value(&command).ok()?;
    let obj = out.as_object_mut()?;
    obj.insert(
        "channel_id".into(),
        payload.get("channel_id").cloned().unwrap_or(Value::Null),
    );
    // Badge signals are normalized booleans from the connector; anything else counts as false.
    for k in ["is_mod", "is_broadcaster"] {
        obj.insert(k.into(), json!(payload.get(k) == Some(&Value::Bool(true))));
    }
    Some(out)
}

/// Canonical lower-case UUID if `actor` parses as one (never a raw handle).
pub fn actor_uuid(actor: Option<&str>) -> Option<String> {
    let a = actor?.trim().to_ascii_lowercase();
    let b = a.as_bytes();
    let ok = b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit(),
        });
    ok.then_some(a)
}

fn fmt_event(e: &Event) -> String {
    let tz = parse_tz(&e.tz).unwrap_or(chrono_tz::UTC);
    let mut s = format!(
        "#{} {} -- {}",
        e.id,
        chat_safe(&e.title),
        render_local(e.start, &tz)
    );
    if e.status != Status::Scheduled {
        s.push_str(&format!(" [{}]", e.status.label()));
    }
    s
}

fn err_text(e: &CalError) -> String {
    match e {
        CalError::Validation(m) | CalError::Conflict(m) => chat_safe(m),
        CalError::NotFound => "No such event".into(),
        CalError::Forbidden => "You are not allowed to do that".into(),
        CalError::Storage(_) => {
            "The calendar is temporarily unavailable, try again shortly.".into()
        }
    }
}

/// Result of one chat command: the reply text plus PII-free log facts.
pub struct ChatReply {
    pub text: String,
    pub op: &'static str,
    pub event_id: Option<u64>,
    pub ok: bool,
}

fn parse_new(ctx_cfg: &Config, c: &Command) -> Result<NewEvent, CalError> {
    let Command::Add {
        title,
        when,
        tz,
        end,
        location,
    } = c
    else {
        return Err(CalError::Validation(USAGE.into()));
    };
    let tz_name = tz.clone().unwrap_or_else(|| ctx_cfg.default_tz.clone());
    let zone = parse_tz(&tz_name)?;
    let start = parse_when(when, &zone)?;
    let end = end.as_deref().map(|e| parse_when(e, &zone)).transpose()?;
    Ok(NewEvent {
        title: title.clone(),
        description: String::new(),
        start,
        end,
        tz: Some(tz_name),
        location: location.clone(),
        channel: None,
    })
}

fn parse_patch(field: &str, value: &str, tz_of_event: &str) -> Result<Patch, CalError> {
    let mut p = Patch::default();
    match field {
        "title" => p.title = Some(value.to_string()),
        "description" | "desc" => p.description = Some(value.to_string()),
        "start" | "when" => p.start = Some(service::parse_time(value, tz_of_event)?),
        "end" => p.end = Some(service::parse_time(value, tz_of_event)?),
        "tz" | "timezone" => p.tz = Some(value.to_string()),
        "location" | "loc" => p.location = Some(value.to_string()),
        _ => {
            return Err(CalError::Validation(
                "field must be title, description, start, end, tz or location".into(),
            ));
        }
    }
    Ok(p)
}

fn run_command(
    ctx: &Ctx,
    cfg: &Config,
    caller: &Caller,
    c: Command,
) -> Result<ChatReply, CalError> {
    let r = |text: String, op: &'static str, id: Option<u64>| ChatReply {
        text,
        op,
        event_id: id,
        ok: true,
    };
    match c {
        Command::Usage => Ok(r(USAGE.into(), "usage", None)),
        Command::List => {
            let evs = service::list_upcoming(ctx, caller.approver, LIST_SHOW)?;
            if evs.is_empty() {
                return Ok(r("No upcoming events.".into(), "list", None));
            }
            let parts: Vec<String> = evs.iter().map(fmt_event).collect();
            Ok(r(format!("Upcoming: {}", parts.join(" | ")), "list", None))
        }
        Command::View { id } => {
            let ev = service::get(ctx, caller, id)?;
            let (y, n, m) = service::rsvp_counts(ctx, id)?;
            let loc = ev
                .location
                .as_deref()
                .map(|l| format!(" @ {}", chat_safe(l)))
                .unwrap_or_default();
            Ok(r(
                format!(
                    "{}{loc} -- yes: {y}, no: {n}, maybe: {m} (!rsvp {id} yes|no|maybe)",
                    fmt_event(&ev)
                ),
                "view",
                Some(id),
            ))
        }
        Command::Add { .. } => {
            let n = parse_new(cfg, &c)?;
            let ev = service::create(ctx, cfg, caller, n)?;
            let note = if ev.status == Status::Pending {
                " (awaiting approval)"
            } else {
                ""
            };
            Ok(r(
                format!("Created {}{note}", fmt_event(&ev)),
                "add",
                Some(ev.id),
            ))
        }
        Command::Set { id, field, value } => {
            let existing = service::get(ctx, caller, id)?;
            let p = parse_patch(&field, &value, &existing.tz)?;
            let ev = service::update(ctx, cfg, caller, id, p)?;
            Ok(r(format!("Updated {}", fmt_event(&ev)), "set", Some(id)))
        }
        Command::Remove { id } => {
            let ev = service::cancel(ctx, cfg, caller, id)?;
            Ok(r(
                format!("Cancelled #{} {}", ev.id, chat_safe(&ev.title)),
                "remove",
                Some(id),
            ))
        }
        Command::Approve { id } => {
            let ev = service::approve(ctx, cfg, caller, id)?;
            Ok(r(
                format!("Approved {}", fmt_event(&ev)),
                "approve",
                Some(id),
            ))
        }
        Command::Reject { id, reason } => {
            let ev = service::reject(ctx, caller, id, reason)?;
            Ok(r(format!("Rejected #{}", ev.id), "reject", Some(id)))
        }
        Command::Rsvp {
            id,
            answer: Some(a),
        } => {
            service::rsvp(ctx, caller, id, a)?;
            Ok(r(
                format!("RSVP recorded for #{id}: {}", a.label()),
                "rsvp",
                Some(id),
            ))
        }
        Command::Rsvp { id, answer: None } => {
            let ev = service::get(ctx, caller, id)?;
            let (y, n, m) = service::rsvp_counts(ctx, id)?;
            Ok(r(
                format!("{} -- yes: {y}, no: {n}, maybe: {m}", fmt_event(&ev)),
                "rsvp_view",
                Some(id),
            ))
        }
    }
}

/// Executes a command payload produced by [`transform_chat`]; failures become user-safe replies
/// (storage failures are logged and surfaced as a retry hint, never swallowed). A bounded
/// reconcile pass runs after every touch so owed sync work retries without a periodic hook.
pub fn run_chat(ctx: &Ctx, cfg: &Config, actor: Option<&str>, payload: &Value) -> ChatReply {
    let command: Command = match serde_json::from_value(payload.clone()) {
        Ok(c) => c,
        Err(_) => {
            return ChatReply {
                text: USAGE.into(),
                op: "usage",
                event_id: None,
                ok: false,
            };
        }
    };
    let caller = Caller {
        actor: actor_uuid(actor),
        approver: payload.get("is_mod") == Some(&Value::Bool(true))
            || payload.get("is_broadcaster") == Some(&Value::Bool(true)),
    };
    let op = op_name(&command);
    let reply = match run_command(ctx, cfg, &caller, command) {
        Ok(r) => r,
        Err(e) => {
            if let CalError::Storage(detail) = &e {
                ctx.log.log(
                    "error",
                    "calendar storage failure",
                    json!({"op": op, "error": detail}),
                );
            }
            ChatReply {
                text: err_text(&e),
                op,
                event_id: None,
                ok: false,
            }
        }
    };
    after_touch(ctx, cfg);
    reply
}

fn op_name(c: &Command) -> &'static str {
    match c {
        Command::List => "list",
        Command::View { .. } => "view",
        Command::Add { .. } => "add",
        Command::Set { .. } => "set",
        Command::Remove { .. } => "remove",
        Command::Approve { .. } => "approve",
        Command::Reject { .. } => "reject",
        Command::Rsvp { .. } => "rsvp",
        Command::Usage => "usage",
    }
}

/// Bounded reconcile after a touch (outcomes are logged by the sync engine); failures are
/// logged, never raised into the user's command.
pub fn after_touch(ctx: &Ctx, cfg: &Config) {
    if let Err(e) = sync::reconcile(ctx, cfg, RECONCILE_BATCH) {
        ctx.log.log(
            "error",
            "calendar reconcile failed",
            json!({"error": e.to_string()}),
        );
    }
}

fn event_json(ctx: &Ctx, e: &Event) -> Result<Value, CalError> {
    let mut sync_rows = Vec::new();
    for t in Target::ALL {
        if let Some(r) = store::sync_record(ctx.kv, e.id, t)? {
            sync_rows.push(json!({
                "target": u8::from(t),
                "target_name": t.name(),
                "remote_id": r.remote_id,
                "status": u8::from(r.status),
                "attempts": r.attempts,
                "last_error_code": r.last_error_code,
            }));
        }
    }
    let (y, n, m) = service::rsvp_counts(ctx, e.id)?;
    Ok(json!({
        "id": e.id,
        "public_id": e.public_id,
        "title": e.title,
        "description": e.description,
        "starts_at": crate::timeutil::to_rfc3339(e.start)?,
        "ends_at": crate::timeutil::to_rfc3339(e.end)?,
        "tz": e.tz,
        "location": e.location,
        "channel": e.channel,
        "status": u8::from(e.status),
        "created_by": e.created_by,
        "approved_by": e.approved_by,
        "version": e.version,
        "sync": sync_rows,
        "rsvp": {"yes": y, "no": n, "maybe": m},
    }))
}

fn req_str(args: &Value, k: &str) -> Result<String, CalError> {
    args.get(k)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| CalError::Validation(format!("`{k}` is required")))
}

fn opt_str(args: &Value, k: &str) -> Option<String> {
    args.get(k).and_then(Value::as_str).map(str::to_string)
}

fn req_id(args: &Value) -> Result<u64, CalError> {
    args.get("id")
        .and_then(Value::as_u64)
        .filter(|n| *n >= 1)
        .ok_or_else(|| CalError::Validation("`id` must be a positive integer".into()))
}

/// Admin actions delivered by the hub-api bundle-admin gateway (already authorized by OIDC
/// scope + tenant + community there): list/get/create/update/cancel/approve/reject/delete.
pub fn run_admin(
    ctx: &Ctx,
    cfg: &Config,
    actor: Option<&str>,
    action: &str,
    args: &Value,
) -> Result<Value, CalError> {
    let caller = Caller {
        actor: actor_uuid(actor),
        approver: true,
    };
    let out = match action {
        "list" => {
            let status = match args.get("status").and_then(Value::as_u64) {
                None => None,
                Some(n) => Some(
                    u8::try_from(n)
                        .ok()
                        .and_then(|c| Status::try_from(c).ok())
                        .ok_or_else(|| CalError::Validation("unknown status code".into()))?,
                ),
            };
            let limit = args
                .get("limit")
                .and_then(Value::as_u64)
                .unwrap_or(50)
                .clamp(1, 100);
            let evs = service::list_all(ctx, status, usize::try_from(limit).unwrap_or(50))?;
            let rows = evs
                .iter()
                .map(|e| event_json(ctx, e))
                .collect::<Result<Vec<_>, _>>()?;
            json!({"events": rows})
        }
        "get" => {
            let ev = service::get(ctx, &caller, req_id(args)?)?;
            json!({"event": event_json(ctx, &ev)?})
        }
        "create" => {
            let tz = opt_str(args, "tz");
            let zone = parse_tz(tz.as_deref().unwrap_or(&cfg.default_tz))?;
            let n = NewEvent {
                title: req_str(args, "title")?,
                description: opt_str(args, "description").unwrap_or_default(),
                start: parse_when(&req_str(args, "starts_at")?, &zone)?,
                end: opt_str(args, "ends_at")
                    .map(|e| parse_when(&e, &zone))
                    .transpose()?,
                tz,
                location: opt_str(args, "location"),
                channel: opt_str(args, "channel"),
            };
            let ev = service::create(ctx, cfg, &caller, n)?;
            json!({"event": event_json(ctx, &ev)?})
        }
        "update" => {
            let id = req_id(args)?;
            let existing = service::get(ctx, &caller, id)?;
            let tz = opt_str(args, "tz");
            let zone = parse_tz(tz.as_deref().unwrap_or(&existing.tz))?;
            let p = Patch {
                title: opt_str(args, "title"),
                description: opt_str(args, "description"),
                start: opt_str(args, "starts_at")
                    .map(|s| parse_when(&s, &zone))
                    .transpose()?,
                end: opt_str(args, "ends_at")
                    .map(|s| parse_when(&s, &zone))
                    .transpose()?,
                tz,
                location: opt_str(args, "location"),
                channel: opt_str(args, "channel"),
            };
            let ev = service::update(ctx, cfg, &caller, id, p)?;
            json!({"event": event_json(ctx, &ev)?})
        }
        "cancel" => {
            let ev = service::cancel(ctx, cfg, &caller, req_id(args)?)?;
            json!({"event": event_json(ctx, &ev)?})
        }
        "approve" => {
            let ev = service::approve(ctx, cfg, &caller, req_id(args)?)?;
            json!({"event": event_json(ctx, &ev)?})
        }
        "reject" => {
            let ev = service::reject(ctx, &caller, req_id(args)?, opt_str(args, "reason"))?;
            json!({"event": event_json(ctx, &ev)?})
        }
        "backfill" => {
            let t = Target::parse(&req_str(args, "target")?)
                .ok_or_else(|| CalError::Validation("unknown sync target".into()))?;
            if !cfg.sync_enabled(t) {
                return Err(CalError::Conflict(format!(
                    "{} sync is not enabled in the bundle config",
                    t.name()
                )));
            }
            let n = sync::backfill(ctx, cfg, t)?;
            json!({"queued": n})
        }
        "delete" => {
            service::delete(ctx, cfg, &caller, req_id(args)?)?;
            json!({"deleted": true})
        }
        other => {
            return Err(CalError::Validation(format!(
                "unknown admin action `{}`",
                crate::timeutil::truncate(other, 32)
            )));
        }
    };
    after_touch(ctx, cfg);
    Ok(out)
}
