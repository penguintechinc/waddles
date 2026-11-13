//! Behaviour tests: events, approvals, RSVP, chat grammar, admin actions, sync idempotency,
//! mapping, retry and reconcile -- against the shared in-memory host.

mod common;

use common::*;
use serde_json::{Value, json};
use waddle_bundle_calendar_rust::app;
use waddle_bundle_calendar_rust::cmd::{self, Command};
use waddle_bundle_calendar_rust::host::CalError;
use waddle_bundle_calendar_rust::model::{self, *};
use waddle_bundle_calendar_rust::service::{self, NewEvent, Patch};
use waddle_bundle_calendar_rust::store;
use waddle_bundle_calendar_rust::sync;
use waddle_bundle_calendar_rust::timeutil;
use waddle_sdk::tool::ToolError;

fn new_ev(title: &str, start: i64) -> NewEvent {
    NewEvent {
        title: title.into(),
        description: "desc".into(),
        start,
        end: None,
        tz: Some("Europe/Berlin".into()),
        location: None,
        channel: None,
    }
}

const SOON: i64 = NOW + 86_400;

#[test]
fn create_list_view_rsvp_cancel_roundtrip() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = Config::default();
    let ev = service::create(&ctx, &cfg, &approver(), new_ev("Movie night", SOON)).unwrap();
    assert_eq!(ev.status, Status::Scheduled);
    assert_eq!(ev.end - ev.start, 3600);
    assert_eq!(ev.version, 1);
    assert_eq!(ev.public_id.len(), 36);
    assert!(w.calls().is_empty(), "sync is off by default");

    assert_eq!(service::list_upcoming(&ctx, false, 10).unwrap().len(), 1);
    service::rsvp(&ctx, &member(USER), ev.id, Rsvp::Yes).unwrap();
    service::rsvp(&ctx, &member(USER2), ev.id, Rsvp::Maybe).unwrap();
    service::rsvp(&ctx, &member(USER), ev.id, Rsvp::No).unwrap(); // re-RSVP changes answer
    let counts = service::rsvp_counts(&ctx, ev.id).unwrap();
    assert_eq!(counts, (0, 1, 1));
    assert_eq!(
        store::rsvp_get(ctx.kv, ev.id, USER).unwrap(),
        Some(Rsvp::No)
    );

    let c = service::cancel(&ctx, &cfg, &member(USER), ev.id);
    assert_eq!(
        c.unwrap_err(),
        CalError::Forbidden,
        "non-owner cannot cancel"
    );
    let c = service::cancel(&ctx, &cfg, &approver(), ev.id).unwrap();
    assert_eq!(c.status, Status::Cancelled);
    assert!(service::list_upcoming(&ctx, false, 10).unwrap().is_empty());
    assert!(matches!(
        service::cancel(&ctx, &cfg, &approver(), ev.id),
        Err(CalError::Conflict(_))
    ));
    assert_eq!(
        service::rsvp(&ctx, &member(USER), ev.id, Rsvp::Yes).unwrap_err(),
        CalError::NotFound
    );
}

#[test]
fn member_cannot_create_without_approval_mode_and_goes_pending_with_it() {
    let w = World::new();
    let ctx = w.ctx();
    let off = Config::default();
    assert_eq!(
        service::create(&ctx, &off, &member(USER), new_ev("x", SOON)).unwrap_err(),
        CalError::Forbidden
    );
    let on = Config {
        approval_required: true,
        ..Config::default()
    };
    let ev = service::create(&ctx, &on, &member(USER), new_ev("Pending one", SOON)).unwrap();
    assert_eq!(ev.status, Status::Pending);
    // Hidden from public list and from other members; visible to approvers and the owner.
    assert!(service::list_upcoming(&ctx, false, 10).unwrap().is_empty());
    assert_eq!(
        service::get(&ctx, &member(USER2), ev.id).unwrap_err(),
        CalError::NotFound
    );
    assert!(service::get(&ctx, &member(USER), ev.id).is_ok());
    assert_eq!(service::list_upcoming(&ctx, true, 10).unwrap().len(), 1);
    assert_eq!(
        service::rsvp(&ctx, &member(USER2), ev.id, Rsvp::Yes).unwrap_err(),
        CalError::NotFound
    );

    assert_eq!(
        service::approve(&ctx, &on, &member(USER), ev.id).unwrap_err(),
        CalError::Forbidden
    );
    let ok = service::approve(&ctx, &on, &approver(), ev.id).unwrap();
    assert_eq!(ok.status, Status::Scheduled);
    assert_eq!(ok.approved_by.as_deref(), Some(MOD));
    assert!(matches!(
        service::approve(&ctx, &on, &approver(), ev.id),
        Err(CalError::Conflict(_))
    ));

    let p2 = service::create(&ctx, &on, &member(USER), new_ev("Nope", SOON)).unwrap();
    let rej = service::reject(&ctx, &approver(), p2.id, Some("off topic".into())).unwrap();
    assert_eq!(rej.status, Status::Rejected);
    assert_eq!(rej.reject_reason.as_deref(), Some("off topic"));
    assert!(matches!(
        service::cancel(&ctx, &on, &approver(), p2.id),
        Err(CalError::Conflict(_))
    ));
}

#[test]
fn validation_rejects_bad_input() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = Config::default();
    let mut bad = new_ev("", SOON);
    assert!(matches!(
        service::create(&ctx, &cfg, &approver(), bad.clone()),
        Err(CalError::Validation(_))
    ));
    bad = new_ev(&"t".repeat(101), SOON);
    assert!(matches!(
        service::create(&ctx, &cfg, &approver(), bad),
        Err(CalError::Validation(_))
    ));
    assert!(matches!(
        service::create(&ctx, &cfg, &approver(), new_ev("line\nbreak", SOON)),
        Err(CalError::Validation(_))
    ));
    let mut tz = new_ev("ok", SOON);
    tz.tz = Some("Mars/Base".into());
    assert!(matches!(
        service::create(&ctx, &cfg, &approver(), tz),
        Err(CalError::Validation(_))
    ));
    let mut e = new_ev("ok", SOON);
    e.end = Some(SOON - 10);
    assert!(matches!(
        service::create(&ctx, &cfg, &approver(), e),
        Err(CalError::Validation(_))
    ));
    let anon = waddle_bundle_calendar_rust::host::Caller {
        actor: None,
        approver: true,
    };
    assert!(matches!(
        service::create(&ctx, &cfg, &anon, new_ev("ok", SOON)),
        Err(CalError::Validation(_))
    ));
    assert!(store::index(ctx.kv).unwrap().is_empty());
}

#[test]
fn discord_sync_create_update_cancel_with_mapping_and_idempotency() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = cfg_sync(true);
    let ev = service::create(&ctx, &cfg, &approver(), new_ev("Sync me", SOON)).unwrap();
    let calls = w.calls();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].tool, "discord.scheduled_event.create");
    assert_eq!(calls[0].target, "waddles.core.service.discord");
    assert_eq!(
        calls[0].args["location"], "Online",
        "default location when absent"
    );
    let rec = store::sync_record(ctx.kv, ev.id, Target::Discord)
        .unwrap()
        .unwrap();
    assert_eq!(rec.remote_id.as_deref(), Some("1001"));
    assert_eq!(rec.status, SyncStatus::Synced);
    assert!(store::pending(ctx.kv).unwrap().is_empty());

    // Same payload again: skipped (hash), no call.
    sync::enqueue(&ctx, &cfg, &ev, SyncOp::Update).unwrap();
    sync::flush_event(&ctx, &cfg, &ev).unwrap();
    assert_eq!(
        w.calls().len(),
        1,
        "unchanged payload does not call the tool"
    );

    // A real edit: one update carrying the mapped id and a version-scoped key.
    let upd = service::update(
        &ctx,
        &cfg,
        &approver(),
        ev.id,
        Patch {
            title: Some("Renamed".into()),
            ..Patch::default()
        },
    )
    .unwrap();
    let calls = w.calls();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[1].tool, "discord.scheduled_event.update");
    assert_eq!(calls[1].args["event_id"], "1001");
    assert_eq!(calls[1].args["name"], "Renamed");
    assert_ne!(calls[0].key, calls[1].key, "keys differ per (op, version)");
    assert_eq!(
        calls[1].key,
        sync::idempotency_key("7", &upd, SyncOp::Update, Target::Discord)
    );

    // Replay of an applied key is a no-op even if intent is re-queued with a changed hash record.
    let mut rec = store::sync_record(ctx.kv, ev.id, Target::Discord)
        .unwrap()
        .unwrap();
    rec.last_synced_hash = None;
    store::put_sync_record(ctx.kv, ev.id, &rec).unwrap();
    sync::enqueue(&ctx, &cfg, &upd, SyncOp::Update).unwrap();
    let out = sync::flush_event(&ctx, &cfg, &upd).unwrap();
    assert_eq!(out[0].outcome, "replayed");
    assert_eq!(w.calls().len(), 2);

    service::cancel(&ctx, &cfg, &approver(), ev.id).unwrap();
    let calls = w.calls();
    assert_eq!(calls[2].tool, "discord.scheduled_event.cancel");
    assert_eq!(calls[2].args, json!({"event_id": "1001"}));
}

#[test]
fn pending_event_syncs_only_after_approval_and_reject_never_syncs() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = Config {
        approval_required: true,
        sync_discord: true,
        ..Config::default()
    };
    let a = service::create(&ctx, &cfg, &member(USER), new_ev("A", SOON)).unwrap();
    let b = service::create(&ctx, &cfg, &member(USER), new_ev("B", SOON)).unwrap();
    assert!(w.calls().is_empty());
    service::reject(&ctx, &approver(), b.id, None).unwrap();
    assert!(w.calls().is_empty());
    service::approve(&ctx, &cfg, &approver(), a.id).unwrap();
    assert_eq!(w.calls().len(), 1);
    assert_eq!(w.calls()[0].tool, "discord.scheduled_event.create");
}

#[test]
fn transient_failure_is_retried_on_next_touch_not_duplicated() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = cfg_sync(true);
    // Both inline attempts fail transiently.
    w.tools
        .script
        .borrow_mut()
        .push_back(Err(ToolError::Timeout));
    w.tools
        .script
        .borrow_mut()
        .push_back(Err(ToolError::TargetUnavailable));
    let ev = service::create(&ctx, &cfg, &approver(), new_ev("Retry", SOON)).unwrap();
    let rec = store::sync_record(ctx.kv, ev.id, Target::Discord)
        .unwrap()
        .unwrap();
    assert_eq!(rec.status, SyncStatus::Pending);
    assert_eq!(rec.remote_id, None);
    assert_eq!(store::pending(ctx.kv).unwrap(), vec![ev.id]);

    // Any later touch reconciles and creates exactly once.
    let out = sync::reconcile(&ctx, &cfg, 5).unwrap();
    assert_eq!(out[0].outcome, "ok");
    let creates = w
        .calls()
        .iter()
        .filter(|c| c.tool.ends_with(".create"))
        .count();
    assert_eq!(creates, 3, "2 failed attempts + 1 success");
    assert!(store::pending(ctx.kv).unwrap().is_empty());
    assert!(sync::reconcile(&ctx, &cfg, 5).unwrap().is_empty());
}

#[test]
fn permanent_errors_park_and_do_not_retry() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = cfg_sync(true);
    w.tools
        .script
        .borrow_mut()
        .push_back(Err(ToolError::NotGranted));
    let ev = service::create(&ctx, &cfg, &approver(), new_ev("Denied", SOON)).unwrap();
    let rec = store::sync_record(ctx.kv, ev.id, Target::Discord)
        .unwrap()
        .unwrap();
    assert_eq!(rec.status, SyncStatus::Error);
    assert_eq!(rec.last_error_code.as_deref(), Some("not_granted"));
    assert!(store::pending(ctx.kv).unwrap().is_empty());
    let n = w.calls().len();
    sync::reconcile(&ctx, &cfg, 5).unwrap();
    assert_eq!(w.calls().len(), n);
}

#[test]
fn create_without_remote_id_fails_loud_and_never_blind_retries() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = cfg_sync(true);
    w.tools
        .script
        .borrow_mut()
        .push_back(Ok(json!({"validated": true, "op": "event.create"})));
    let ev = service::create(&ctx, &cfg, &approver(), new_ev("NoId", SOON)).unwrap();
    let rec = store::sync_record(ctx.kv, ev.id, Target::Discord)
        .unwrap()
        .unwrap();
    assert_eq!(rec.status, SyncStatus::Error);
    assert_eq!(rec.last_error_code.as_deref(), Some("remote_id_missing"));
    sync::reconcile(&ctx, &cfg, 5).unwrap();
    assert_eq!(w.calls().len(), 1, "no duplicate create risk");
}

#[test]
fn drifted_remote_is_recreated_once_and_cancel_of_gone_remote_is_satisfied() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = cfg_sync(true);
    let ev = service::create(&ctx, &cfg, &approver(), new_ev("Drift", SOON)).unwrap();
    w.tools
        .script
        .borrow_mut()
        .push_back(Err(ToolError::InvalidArgs("remote 404".into())));
    let upd = service::update(
        &ctx,
        &cfg,
        &approver(),
        ev.id,
        Patch {
            title: Some("Drift2".into()),
            ..Patch::default()
        },
    )
    .unwrap();
    let tools: Vec<String> = w.calls().iter().map(|c| c.tool.clone()).collect();
    assert_eq!(
        tools,
        [
            "discord.scheduled_event.create",
            "discord.scheduled_event.update",
            "discord.scheduled_event.create"
        ]
    );
    let rec = store::sync_record(ctx.kv, ev.id, Target::Discord)
        .unwrap()
        .unwrap();
    assert_eq!(rec.remote_id.as_deref(), Some("1002"));
    assert_eq!(upd.version, 2);

    w.tools
        .script
        .borrow_mut()
        .push_back(Err(ToolError::InvalidArgs("not_found".into())));
    service::cancel(&ctx, &cfg, &approver(), ev.id).unwrap();
    let rec = store::sync_record(ctx.kv, ev.id, Target::Discord)
        .unwrap()
        .unwrap();
    assert_eq!(rec.status, SyncStatus::Synced);
    assert_eq!(rec.remote_id, None);
}

#[test]
fn backfill_queues_upcoming_scheduled_events_only_and_needs_hub_enabled_sync() {
    let w = World::new();
    let ctx = w.ctx();
    let off = Config::default();
    let a = service::create(&ctx, &off, &approver(), new_ev("Upcoming", SOON)).unwrap();
    let b = service::create(&ctx, &off, &approver(), new_ev("Cancelled", SOON)).unwrap();
    service::cancel(&ctx, &off, &approver(), b.id).unwrap();
    let admin = |cfg: &Config, args: Value| app::run_admin(&ctx, cfg, Some(MOD), "backfill", &args);
    // Sync disabled in hub config: refused, nothing queued.
    assert!(matches!(
        admin(&off, json!({"target": "discord"})),
        Err(CalError::Conflict(_))
    ));
    assert!(matches!(
        admin(&cfg_sync(true), json!({"target": "myspace"})),
        Err(CalError::Validation(_))
    ));
    let on = cfg_sync(true);
    assert_eq!(
        admin(&on, json!({"target": "discord"})).unwrap(),
        json!({"queued": 1})
    );
    assert_eq!(
        w.calls().len(),
        1,
        "reconcile after the touch pushed the queued event"
    );
    assert!(
        store::sync_record(ctx.kv, a.id, Target::Discord)
            .unwrap()
            .unwrap()
            .remote_id
            .is_some()
    );
}

#[test]
fn twitch_and_wix_adapters_follow_their_contracts_end_to_end() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = Config {
        sync_twitch: true,
        sync_wix: true,
        ..Config::default()
    };
    let ev = service::create(&ctx, &cfg, &approver(), new_ev("Multi", SOON)).unwrap();
    let tools: Vec<String> = w.calls().iter().map(|c| c.tool.clone()).collect();
    assert_eq!(tools, ["twitch.schedule.create", "wix.event.create"]);
    assert_eq!(w.calls()[0].args["duration"], 60);
    assert_eq!(w.calls()[0].args["timezone"], "Europe/Berlin");
    service::update(
        &ctx,
        &cfg,
        &approver(),
        ev.id,
        Patch {
            title: Some("M2".into()),
            ..Patch::default()
        },
    )
    .unwrap();
    service::cancel(&ctx, &cfg, &approver(), ev.id).unwrap();
    let tools: Vec<String> = w.calls().iter().map(|c| c.tool.clone()).collect();
    assert_eq!(
        &tools[2..],
        [
            "twitch.schedule.update",
            "wix.event.update",
            "twitch.schedule.delete",
            "wix.event.cancel"
        ]
    );
    let t = store::sync_record(ctx.kv, ev.id, Target::Twitch)
        .unwrap()
        .unwrap();
    assert_eq!(t.remote_id, None, "twitch cancel is a delete");
    let x = store::sync_record(ctx.kv, ev.id, Target::Wix)
        .unwrap()
        .unwrap();
    assert_eq!(x.remote_id.as_deref(), Some(common::wix_id(1002).as_str()));
    let upd = w
        .calls()
        .into_iter()
        .find(|c| c.tool == "wix.event.update")
        .unwrap();
    assert_eq!(
        upd.args["revision"], 1,
        "update carries the revision create reported"
    );
    assert_eq!(
        x.remote_revision,
        Some(2),
        "revision advances from the update result"
    );
}

#[test]
fn hard_delete_removes_remote_then_local() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = cfg_sync(true);
    let ev = service::create(&ctx, &cfg, &approver(), new_ev("Gone", SOON)).unwrap();
    service::delete(&ctx, &cfg, &approver(), ev.id).unwrap();
    assert_eq!(
        w.calls().last().unwrap().tool,
        "discord.scheduled_event.delete"
    );
    assert!(store::event(ctx.kv, ev.id).unwrap().is_none());
    assert!(store::index(ctx.kv).unwrap().is_empty());
    assert_eq!(
        service::delete(&ctx, &cfg, &member(USER), ev.id).unwrap_err(),
        CalError::Forbidden
    );
}

// ---- chat grammar + execution ----

fn chat(w: &World, cfg: &Config, actor: &str, text: &str, badge: bool) -> String {
    let payload = json!({"text": text, "channel_id": "chan1", "is_mod": badge});
    let cmd = app::transform_chat(&payload).expect("recognized");
    app::run_chat(&w.ctx(), cfg, Some(actor), &cmd).text
}

#[test]
fn grammar_parses_all_verbs() {
    use Command::{Add, Approve, List, Reject, Remove, Set, Usage, View};
    assert_eq!(cmd::parse("!calendar", ""), List);
    assert_eq!(cmd::parse("!event", "list"), List);
    assert_eq!(cmd::parse("!calendar", "view #4"), View { id: 4 });
    assert_eq!(cmd::parse("!calendar", "remove 3"), Remove { id: 3 });
    assert_eq!(cmd::parse("!calendar", "approve 3"), Approve { id: 3 });
    assert_eq!(
        cmd::parse("!calendar", "reject 3 too loud"),
        Reject {
            id: 3,
            reason: Some("too loud".into())
        }
    );
    assert_eq!(
        cmd::parse("!rsvp", "5 yes"),
        Command::Rsvp {
            id: 5,
            answer: Some(model::Rsvp::Yes)
        }
    );
    assert_eq!(
        cmd::parse("!rsvp", "5"),
        Command::Rsvp {
            id: 5,
            answer: None
        }
    );
    assert_eq!(
        cmd::parse("!calendar", "rsvp 5 maybe"),
        Command::Rsvp {
            id: 5,
            answer: Some(model::Rsvp::Maybe)
        }
    );
    assert_eq!(
        cmd::parse("!calendar", "set 2 location \"The Bar\""),
        Set {
            id: 2,
            field: "location".into(),
            value: "The Bar".into()
        }
    );
    assert_eq!(
        cmd::parse(
            "!calendar",
            "add Game Night @ 2027-02-01 19:00 tz=Europe/Berlin loc=\"The Bar\" end=\"2027-02-01 21:00\""
        ),
        Add {
            title: "Game Night".into(),
            when: "2027-02-01 19:00".into(),
            tz: Some("Europe/Berlin".into()),
            end: Some("2027-02-01 21:00".into()),
            location: Some("The Bar".into())
        }
    );
    for bad in [
        "frobnicate",
        "view x",
        "view 0",
        "add nothing",
        "rsvp 3 purple",
        "enable sync discord",
        "disable sync wix",
        "remove 99999999999",
        "set 1 title",
        "list extra",
    ] {
        assert_eq!(cmd::parse("!calendar", bad), Usage, "{bad}");
    }
    assert_eq!(cmd::parse("!rsvp", "a b"), Usage);
    assert!(cmd::split_head("hello there").is_none());
    assert!(cmd::split_head("!calendarx").is_none());
    assert!(app::transform_chat(&json!({"text": 5})).is_none());
    assert!(app::transform_chat(&json!({"text": "!ping"})).is_none());
}

#[test]
fn chat_end_to_end_with_timezones_and_replies() {
    let w = World::new();
    let cfg = cfg_sync(true);
    let r = chat(
        &w,
        &cfg,
        MOD,
        "!calendar add Game Night @ 2027-02-01 19:00 tz=Europe/Berlin loc=\"The Bar\"",
        true,
    );
    assert!(
        r.starts_with("Created #1 Game Night -- 2027-02-01 19:00 CET"),
        "{r}"
    );
    let c = &w.calls()[0];
    assert_eq!(
        c.args["scheduled_start_time"], "2027-02-01T18:00:00Z",
        "local time converted DST-correct to UTC"
    );
    assert_eq!(c.args["location"], "The Bar");

    assert!(chat(&w, &cfg, USER, "!calendar", false).starts_with("Upcoming: #1 Game Night"));
    assert!(chat(&w, &cfg, USER, "!event view 1", false).contains("yes: 0"));
    assert_eq!(
        chat(&w, &cfg, USER, "!rsvp 1 yes", false),
        "RSVP recorded for #1: yes"
    );
    assert!(chat(&w, &cfg, USER, "!rsvp 1", false).contains("yes: 1"));
    assert_eq!(
        chat(&w, &cfg, USER, "!calendar remove 1", false),
        "You are not allowed to do that"
    );
    assert_eq!(
        chat(&w, &cfg, USER, "!calendar add X @ 2027-02-01 19:00", false),
        "You are not allowed to do that"
    );
    assert!(
        chat(
            &w,
            &cfg,
            MOD,
            "!calendar set 1 start 2027-02-02 20:00",
            true
        )
        .starts_with("Updated #1")
    );
    assert_eq!(
        w.calls().last().unwrap().tool,
        "discord.scheduled_event.update"
    );
    assert!(chat(&w, &cfg, MOD, "!calendar remove 1", true).starts_with("Cancelled #1"));
    assert_eq!(
        chat(&w, &cfg, MOD, "!calendar view 99", true),
        "No such event"
    );
    assert!(chat(&w, &cfg, MOD, "!calendar gibberish", true).starts_with("Usage:"));
    // Unlinked identity: loud, never a raw-handle fallback.
    let payload = json!({"text": "!rsvp 1 yes", "channel_id": "c"});
    let cmd = app::transform_chat(&payload).unwrap();
    let rep = app::run_chat(&w.ctx(), &cfg, Some("some_handle"), &cmd);
    assert!(rep.text.contains("linked Waddles identity"));
}

#[test]
fn echoed_text_cannot_mass_mention_and_dst_gaps_are_rejected() {
    let w = World::new();
    let cfg = Config::default();
    let r = chat(
        &w,
        &cfg,
        MOD,
        "!calendar add @everyone party @ 2027-02-01 19:00",
        true,
    );
    // "@everyone party" has no " @ " before... first " @ " splits title, so title is "@everyone party".
    assert!(!r.contains("@everyone"), "{r}");
    let gap = chat(
        &w,
        &cfg,
        MOD,
        "!calendar add Gap @ 2027-03-28 02:30 tz=Europe/Berlin",
        true,
    );
    assert!(gap.contains("does not exist"), "{gap}");
    let amb = chat(
        &w,
        &cfg,
        MOD,
        "!calendar add Amb @ 2027-10-31 02:30 tz=Europe/Berlin",
        true,
    );
    assert!(amb.contains("ambiguous"), "{amb}");
}

#[test]
fn chat_cannot_disable_approval_or_enable_sync() {
    let w = World::new();
    let cfg = Config {
        approval_required: true,
        ..Config::default()
    };
    let r = chat(
        &w,
        &cfg,
        USER,
        "!calendar add Pending @ 2027-02-01 19:00",
        false,
    );
    assert!(r.contains("(awaiting approval)"), "{r}");
    // Even a mod/broadcaster cannot flip security toggles from chat: the verbs are just usage.
    for text in [
        "!calendar enable sync discord",
        "!calendar disable approval",
        "!calendar set approval_required false",
    ] {
        assert!(
            chat(&w, &cfg, MOD, text, true).starts_with("Usage:"),
            "{text}"
        );
    }
    assert!(!store::effective_config("{}").unwrap().sync_discord);
    // No kv key can alter the effective config: it is derived from hub config only.
    w.kv.0.borrow_mut().insert(
        "cal.cfg".into(),
        br#"{"sync_discord":true,"approval_required":false}"#.to_vec(),
    );
    let eff = store::effective_config(r#"{"approval_required":true}"#).unwrap();
    assert!(eff.approval_required && !eff.sync_discord);
    assert!(chat(&w, &eff, MOD, "!calendar approve 1", true).starts_with("Approved #1"));
    assert!(w.calls().is_empty(), "sync stayed off");
}

#[test]
fn owner_edit_of_approved_event_requires_reapproval_and_does_not_sync() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = Config {
        approval_required: true,
        sync_discord: true,
        ..Config::default()
    };
    let ev = service::create(&ctx, &cfg, &member(USER), new_ev("Orig", SOON)).unwrap();
    service::approve(&ctx, &cfg, &approver(), ev.id).unwrap();
    assert_eq!(w.calls().len(), 1);
    // Owner edit: back to Pending, remote untouched, hidden from the public list.
    let edited = service::update(
        &ctx,
        &cfg,
        &member(USER),
        ev.id,
        Patch {
            title: Some("Sneaky".into()),
            ..Patch::default()
        },
    )
    .unwrap();
    assert_eq!(edited.status, Status::Pending);
    assert_eq!(edited.approved_by, None);
    assert_eq!(w.calls().len(), 1, "no update pushed without re-approval");
    assert!(service::list_upcoming(&ctx, false, 10).unwrap().is_empty());
    // Re-approval pushes the edited content as an update to the same remote event.
    service::approve(&ctx, &cfg, &approver(), ev.id).unwrap();
    let calls = w.calls();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[1].tool, "discord.scheduled_event.update");
    assert_eq!(calls[1].args["name"], "Sneaky");
}

#[test]
fn approver_edit_syncs_immediately_and_owner_edit_without_approval_mode_is_unchanged() {
    let w = World::new();
    let ctx = w.ctx();
    let strict = Config {
        approval_required: true,
        sync_discord: true,
        ..Config::default()
    };
    let ev = service::create(&ctx, &strict, &approver(), new_ev("Mod made", SOON)).unwrap();
    let e = service::update(
        &ctx,
        &strict,
        &approver(),
        ev.id,
        Patch {
            title: Some("Mod edit".into()),
            ..Patch::default()
        },
    )
    .unwrap();
    assert_eq!(e.status, Status::Scheduled);
    assert_eq!(
        w.calls().last().unwrap().tool,
        "discord.scheduled_event.update"
    );

    // Approval off: only approvers can create, owners (approvers) edit as before.
    let loose = cfg_sync(true);
    let n = service::update(
        &ctx,
        &loose,
        &approver(),
        ev.id,
        Patch {
            title: Some("again".into()),
            ..Patch::default()
        },
    )
    .unwrap();
    assert_eq!(n.status, Status::Scheduled);
}

#[test]
fn storage_failure_is_logged_and_surfaced_not_swallowed() {
    let w = World::new();
    w.kv.0
        .borrow_mut()
        .insert("cal.index".into(), b"{not json".to_vec());
    let cfg = Config::default();
    let r = chat(&w, &cfg, MOD, "!calendar", true);
    assert!(r.contains("temporarily unavailable"));
    let logs = w.log.0.borrow();
    assert!(
        logs.iter()
            .any(|(l, m, _)| l == "error" && m == "calendar storage failure")
    );
}

#[test]
fn logs_never_contain_titles_or_user_ids() {
    let w = World::new();
    let cfg = cfg_sync(true);
    chat(
        &w,
        &cfg,
        MOD,
        "!calendar add SecretTitle @ 2027-02-01 19:00",
        true,
    );
    let dump = serde_json::to_string(&*w.log.0.borrow()).unwrap();
    assert!(
        !dump.contains("SecretTitle") && !dump.contains(MOD),
        "{dump}"
    );
    assert!(dump.contains("calendar sync"));
}

// ---- admin ----

#[test]
fn admin_actions_cover_list_get_create_update_cancel_approve_reject_delete() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = Config {
        approval_required: true,
        sync_discord: true,
        ..Config::default()
    };
    let admin = |a: &str, args: Value| app::run_admin(&ctx, &cfg, Some(MOD), a, &args);

    let c = admin("create", json!({"title": "Admin ev", "starts_at": "2027-03-01T10:00:00Z", "tz": "UTC", "location": "HQ"})).unwrap();
    assert_eq!(c["event"]["status"], 3, "approver creation is scheduled");
    assert_eq!(c["event"]["sync"][0]["remote_id"], "1001");
    let id = c["event"]["id"].as_u64().unwrap();
    assert_eq!(
        admin("get", json!({"id": id})).unwrap()["event"]["title"],
        "Admin ev"
    );
    let u = admin(
        "update",
        json!({"id": id, "title": "Renamed", "ends_at": "2027-03-01T12:00:00Z"}),
    )
    .unwrap();
    assert_eq!(u["event"]["version"], 2);
    assert_eq!(u["event"]["ends_at"], "2027-03-01T12:00:00Z");
    assert_eq!(
        admin("list", json!({"status": 3})).unwrap()["events"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        admin("list", json!({"status": 2})).unwrap()["events"]
            .as_array()
            .unwrap()
            .len(),
        0
    );
    admin("cancel", json!({"id": id})).unwrap();

    // Approve/reject path via a pending member event.
    let p = service::create(&ctx, &cfg, &member(USER), new_ev("P", SOON)).unwrap();
    let a = admin("approve", json!({"id": p.id})).unwrap();
    assert_eq!(a["event"]["approved_by"], MOD);
    let q = service::create(&ctx, &cfg, &member(USER), new_ev("Q", SOON)).unwrap();
    let r = admin("reject", json!({"id": q.id, "reason": "dup"})).unwrap();
    assert_eq!(r["event"]["status"], 5);
    assert_eq!(
        admin("delete", json!({"id": p.id})).unwrap(),
        json!({"deleted": true})
    );

    assert!(matches!(
        admin("get", json!({"id": 9999})),
        Err(CalError::NotFound)
    ));
    assert!(matches!(
        admin("get", json!({})),
        Err(CalError::Validation(_))
    ));
    assert!(matches!(
        admin("nuke", json!({})),
        Err(CalError::Validation(_))
    ));
    assert!(matches!(
        admin("list", json!({"status": 77})),
        Err(CalError::Validation(_))
    ));
    assert!(matches!(
        admin("create", json!({"title": "x"})),
        Err(CalError::Validation(_))
    ));
}

// ---- time + storage helpers ----

#[test]
fn time_helpers() {
    let berlin = timeutil::parse_tz("Europe/Berlin").unwrap();
    let a = timeutil::parse_when("2027-02-01T19:00:00+01:00", &berlin).unwrap();
    let b = timeutil::parse_when("2027-02-01T18:00:00Z", &berlin).unwrap();
    let c = timeutil::parse_when("2027-02-01 19:00", &berlin).unwrap();
    assert_eq!(a, b);
    assert_eq!(b, c);
    assert_eq!(timeutil::to_rfc3339(b).unwrap(), "2027-02-01T18:00:00Z");
    assert_eq!(timeutil::render_local(b, &berlin), "2027-02-01 19:00 CET");
    assert!(timeutil::parse_when("next tuesday", &berlin).is_err());
    assert!(timeutil::parse_tz("Not/AZone").is_err());
    assert!(timeutil::check_window(NOW, NOW + 10 * 86_400 * 366).is_err());
    assert!(timeutil::check_window(0, 3600).is_err());
    assert!(timeutil::check_description("a\nb").is_ok());
    assert!(timeutil::check_description("a\u{7}b").is_err());
    assert_eq!(timeutil::chat_safe("@here"), "@\u{200b}here");
}

#[test]
fn identity_and_public_id_helpers() {
    assert_eq!(app::actor_uuid(Some(MOD)).as_deref(), Some(MOD));
    assert_eq!(
        app::actor_uuid(Some(&MOD.to_uppercase())).as_deref(),
        Some(MOD)
    );
    assert!(app::actor_uuid(Some("handle123")).is_none());
    assert!(app::actor_uuid(None).is_none());
    let p = derive_public_id("t", "1", 5);
    assert_eq!(p, derive_public_id("t", "1", 5));
    assert_ne!(p, derive_public_id("t", "2", 5));
    assert_eq!(p.len(), 36);
}

#[test]
fn enum_codes_roundtrip_and_reject_unknown() {
    for s in [
        Status::Pending,
        Status::Scheduled,
        Status::Cancelled,
        Status::Rejected,
    ] {
        assert_eq!(Status::try_from(u8::from(s)).unwrap(), s);
    }
    for t in Target::ALL {
        assert_eq!(Target::try_from(u8::from(t)).unwrap(), t);
        assert_eq!(Target::parse(t.name()), Some(t));
    }
    for o in [
        SyncOp::Create,
        SyncOp::Update,
        SyncOp::Cancel,
        SyncOp::Delete,
    ] {
        assert_eq!(SyncOp::try_from(u8::from(o)).unwrap(), o);
    }
    for s in [SyncStatus::Pending, SyncStatus::Synced, SyncStatus::Error] {
        assert_eq!(SyncStatus::try_from(u8::from(s)).unwrap(), s);
    }
    for r in [Rsvp::Yes, Rsvp::No, Rsvp::Maybe] {
        assert_eq!(Rsvp::try_from(u8::from(r)).unwrap(), r);
        assert_eq!(Rsvp::parse(r.label()), Some(r));
    }
    assert!(Status::try_from(1).is_err() && Target::try_from(9).is_err());
    assert!(
        SyncOp::try_from(0).is_err()
            && SyncStatus::try_from(0).is_err()
            && Rsvp::try_from(0).is_err()
    );
    assert_eq!(serde_json::to_string(&Status::Scheduled).unwrap(), "3");
}

#[test]
fn effective_config_comes_from_hub_json_only() {
    assert!(store::effective_config("not json").is_err());
    let c = store::effective_config(r#"{"default_tz":"Europe/Berlin","sync_wix":true}"#).unwrap();
    assert_eq!(c.default_tz, "Europe/Berlin");
    assert!(c.sync_wix && !c.sync_discord && !c.approval_required);
    assert_eq!(store::effective_config("").unwrap(), Config::default());
}

#[test]
fn calendar_full_purges_oldest_terminal_event_then_refuses() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = Config::default();
    let first = service::create(&ctx, &cfg, &approver(), new_ev("e0", SOON)).unwrap();
    for i in 1..store::MAX_EVENTS {
        service::create(&ctx, &cfg, &approver(), new_ev(&format!("e{i}"), SOON)).unwrap();
    }
    assert!(matches!(
        service::create(&ctx, &cfg, &approver(), new_ev("overflow", SOON)),
        Err(CalError::Conflict(_))
    ));
    service::cancel(&ctx, &cfg, &approver(), first.id).unwrap();
    let n = service::create(&ctx, &cfg, &approver(), new_ev("fits", SOON)).unwrap();
    assert!(
        store::event(ctx.kv, first.id).unwrap().is_none(),
        "oldest terminal event purged"
    );
    assert_eq!(store::index(ctx.kv).unwrap().len(), store::MAX_EVENTS);
    assert!(n.id > first.id);
}

#[test]
fn queued_upsert_cannot_push_unapproved_edit_after_pending_reset() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = Config {
        approval_required: true,
        sync_discord: true,
        ..Config::default()
    };
    // Approved + synced once (remote exists).
    let ev = service::create(&ctx, &cfg, &member(USER), new_ev("Orig", SOON)).unwrap();
    service::approve(&ctx, &cfg, &approver(), ev.id).unwrap();
    assert_eq!(w.calls().len(), 1);
    // An approver edit whose update fails transiently stays queued.
    w.tools
        .script
        .borrow_mut()
        .push_back(Err(ToolError::Timeout));
    w.tools
        .script
        .borrow_mut()
        .push_back(Err(ToolError::Timeout));
    service::update(
        &ctx,
        &cfg,
        &approver(),
        ev.id,
        Patch {
            title: Some("Approved edit".into()),
            ..Patch::default()
        },
    )
    .unwrap();
    let before = w.calls().len();
    assert_eq!(store::pending(ctx.kv).unwrap(), vec![ev.id]);
    // Non-approver edit: back to Pending, queue cleared, reconcile pushes NOTHING.
    service::update(
        &ctx,
        &cfg,
        &member(USER),
        ev.id,
        Patch {
            title: Some("Sneaky".into()),
            ..Patch::default()
        },
    )
    .unwrap();
    assert!(store::pending(ctx.kv).unwrap().is_empty());
    assert!(sync::reconcile(&ctx, &cfg, 5).unwrap().is_empty());
    assert_eq!(w.calls().len(), before, "zero tool calls while pending");
    // Re-approval pushes exactly one update carrying the edited content.
    service::approve(&ctx, &cfg, &approver(), ev.id).unwrap();
    let calls = w.calls();
    assert_eq!(calls.len(), before + 1);
    assert_eq!(calls.last().unwrap().tool, "discord.scheduled_event.update");
    assert_eq!(calls.last().unwrap().args["name"], "Sneaky");
}

#[test]
fn engine_refuses_upserts_for_non_scheduled_events_even_if_queued() {
    let w = World::new();
    let ctx = w.ctx();
    let cfg = cfg_sync(true);
    let mut ev = service::create(&ctx, &cfg, &approver(), new_ev("E", SOON)).unwrap();
    assert_eq!(w.calls().len(), 1);
    // Force a stale queued upsert, then flip the event to Pending behind the engine's back.
    sync::enqueue(&ctx, &cfg, &ev, SyncOp::Update).unwrap();
    ev.status = Status::Pending;
    ev.title = "Unapproved".into();
    let out = sync::flush_event(&ctx, &cfg, &ev).unwrap();
    assert_eq!(out[0].outcome, "dropped_unapproved");
    assert_eq!(w.calls().len(), 1);
    assert!(store::pending(ctx.kv).unwrap().is_empty());
    // A queued Cancel still runs for a non-scheduled (cancelled) event.
    ev.status = Status::Cancelled;
    sync::enqueue(&ctx, &cfg, &ev, SyncOp::Cancel).unwrap();
    sync::flush_event(&ctx, &cfg, &ev).unwrap();
    assert_eq!(
        w.calls().last().unwrap().tool,
        "discord.scheduled_event.cancel"
    );
}
