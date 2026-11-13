//! Unit tests for the accrual logic over an in-memory [`Host`].

use super::*;
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};

const U1: &str = "11111111-1111-1111-1111-111111111111";
const U2: &str = "22222222-2222-2222-2222-222222222222";

/// One recorded tool call: target, tool, args, idempotency key.
type Call = (String, String, Value, Option<String>);

/// In-memory host recording every tool call, log and metric.
#[derive(Default)]
struct Fake {
    flags: HashSet<String>,
    actor: RefCell<Option<String>>,
    kv: RefCell<HashMap<String, Vec<u8>>>,
    counters: RefCell<HashMap<String, i64>>,
    calls: RefCell<Vec<Call>>,
    reply: RefCell<Option<Result<Value, ToolErr>>>,
    logs: RefCell<Vec<(Level, Value)>>,
    metrics: RefCell<Vec<(String, String)>>,
    /// Tokens the host-verified mention bindings resolve (token -> uuid).
    mentions: RefCell<HashMap<String, String>>,
}

impl Fake {
    fn on() -> Self {
        let f = Self::default();
        let mut f = f;
        f.flags.insert(FLAG_ENABLE.to_string());
        *f.actor.borrow_mut() = Some(U1.to_string());
        f.mentions
            .borrow_mut()
            .insert(U2.to_string(), U2.to_string());
        f
    }
    fn config(&self, json: &str) {
        self.kv
            .borrow_mut()
            .insert(CONFIG_KEY.to_string(), json.as_bytes().to_vec());
    }
    fn reply(&self, r: Result<Value, ToolErr>) {
        *self.reply.borrow_mut() = Some(r);
    }
    fn n_calls(&self) -> usize {
        self.calls.borrow().len()
    }
    fn warns(&self) -> usize {
        self.logs
            .borrow()
            .iter()
            .filter(|(l, _)| *l == Level::Warn)
            .count()
    }
}

impl Host for Fake {
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        Ok(self.kv.borrow().get(key).cloned())
    }
    fn kv_increment(&self, key: &str, delta: i64, _ttl: u32) -> Result<i64, String> {
        let mut c = self.counters.borrow_mut();
        let e = c.entry(key.to_string()).or_insert(0);
        *e += delta;
        Ok(*e)
    }
    fn flag(&self, key: &str) -> bool {
        self.flags.contains(key)
    }
    fn resolve_actor(&self) -> Option<String> {
        self.actor.borrow().clone()
    }
    fn resolve_mention(&self, token: &str) -> Option<String> {
        self.mentions.borrow().get(token).cloned()
    }
    fn monotonic_nanos(&self) -> u64 {
        1
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
            idem.map(str::to_string),
        ));
        self.reply
            .borrow()
            .clone()
            .unwrap_or_else(|| Ok(json!({"score": 600, "applied": true})))
    }
    fn log(&self, level: Level, _m: &str, fields: Value) {
        self.logs.borrow_mut().push((level, fields));
    }
    fn metric(&self, outcome: &str, reason_class: &str, _ms: f64) {
        self.metrics
            .borrow_mut()
            .push((outcome.to_string(), reason_class.to_string()));
    }
}

fn input<'a>(platform: &'a str, et: &'a str, payload: &'a str, id: &'a str) -> Input<'a> {
    Input {
        platform,
        event_type: et,
        payload_json: payload,
        occurred_at: "2026-11-11T00:00:00.000Z",
        message_id: id,
    }
}

fn run_ev(h: &Fake, et: &str, payload: &str) -> Outcome {
    process(h, &input("twitch", et, payload, "100-0"))
}

/// Delta sent in the single recorded call.
fn last_delta(h: &Fake) -> i64 {
    h.calls.borrow()[0].2["delta"].as_i64().unwrap()
}

#[test]
fn default_event_to_delta_table() {
    let cases: &[(&str, &str, i64)] = &[
        ("chat.message", r#"{"text":"hello"}"#, 1),
        ("chat.message", r#"{"text":"!sr song"}"#, 1),
        ("channel.follow", "{}", 1),
        ("channel.followed", "{}", 1),
        ("channel.subscribe", r#"{"metadata":{"tier":"1000"}}"#, 5),
        ("channel.subscribe", r#"{"metadata":{"tier":"2000"}}"#, 5),
        ("channel.subscribe", r#"{"metadata":{"tier":2}}"#, 5),
        ("channel.subscribe", r#"{"metadata":{"tier":"3000"}}"#, 5),
        (
            "channel.subscription.gift",
            r#"{"metadata":{"total":3}}"#,
            5,
        ),
        ("channel.subscription.gifts", "{}", 3),
        ("channel.cheer", r#"{"metadata":{"bits":250}}"#, 3),
        ("channel.cheer", r#"{"metadata":{"bits":150}}"#, 2),
        ("channel.cheer", r#"{"metadata":{"bits":100}}"#, 1),
        ("channel.raid", "{}", 2),
        ("donation", r#"{"amount":3}"#, 3),
        ("tip", r#"{"amount":"2.4"}"#, 2),
        ("boost", "{}", 5),
        ("server_boost", "{}", 5),
        ("giveaway.entry", "{}", -1),
        ("raffle.entry", "{}", -1),
    ];
    for (et, payload, want) in cases {
        let h = Fake::on();
        let o = run_ev(&h, et, payload);
        assert_eq!(o.outcome, "applied", "{et} {payload}");
        assert_eq!(last_delta(&h), *want, "{et} {payload}");
        assert_eq!(h.calls.borrow()[0].1, "adjust");
        assert_eq!(h.calls.borrow()[0].0, TARGET);
    }
}

#[test]
fn tier_reasons_and_gift_multiplier_unclamped_small() {
    let h = Fake::on();
    run_ev(&h, "channel.subscribe", r#"{"metadata":{"tier":"3000"}}"#);
    assert_eq!(h.calls.borrow()[0].2["reason"], "subscription.tier3");
    let h = Fake::on();
    h.config(r#"{"weights":{"gift_subscription":1}}"#);
    run_ev(
        &h,
        "channel.subscription.gift",
        r#"{"metadata":{"total":3}}"#,
    );
    assert_eq!(last_delta(&h), 3);
}

#[test]
fn cheer_under_fifty_bits_rounds_to_zero_and_skips() {
    let h = Fake::on();
    let o = run_ev(&h, "channel.cheer", r#"{"metadata":{"bits":40}}"#);
    assert_eq!(o.outcome, "zero_delta");
    assert_eq!(h.n_calls(), 0);
}

#[test]
fn chat_and_command_cooldowns_are_independent() {
    let h = Fake::on();
    assert_eq!(
        run_ev(&h, "chat.message", r#"{"text":"a"}"#).outcome,
        "applied"
    );
    assert_eq!(
        run_ev(&h, "chat.message", r#"{"text":"b"}"#).outcome,
        "cooldown"
    );
    assert_eq!(
        run_ev(&h, "chat.message", r#"{"text":"!x"}"#).outcome,
        "applied"
    );
    assert_eq!(
        run_ev(&h, "chat.message", r#"{"text":"!y"}"#).outcome,
        "cooldown"
    );
    assert_eq!(h.n_calls(), 2);
    let keys: Vec<String> = h.counters.borrow().keys().cloned().collect();
    assert!(keys.iter().all(|k| !k.contains(':') && k.contains(U1)));
}

#[test]
fn cooldowns_are_per_user() {
    let h = Fake::on();
    run_ev(&h, "chat.message", r#"{"text":"a"}"#);
    *h.actor.borrow_mut() = Some(U2.to_string());
    assert_eq!(
        run_ev(&h, "chat.message", r#"{"text":"a"}"#).outcome,
        "applied"
    );
    assert_eq!(h.n_calls(), 2);
}

#[test]
fn cooldown_zero_disables() {
    let h = Fake::on();
    h.config(r#"{"cooldowns":{"chat_message":0}}"#);
    assert_eq!(
        run_ev(&h, "chat.message", r#"{"text":"a"}"#).outcome,
        "applied"
    );
    assert_eq!(
        run_ev(&h, "chat.message", r#"{"text":"a"}"#).outcome,
        "applied"
    );
    assert_eq!(h.n_calls(), 2);
}

#[test]
fn idempotency_key_stable_per_event_and_distinct_across_events() {
    let h = Fake::on();
    process(&h, &input("twitch", "channel.follow", "{}", "100-0"));
    process(&h, &input("twitch", "channel.follow", "{}", "100-0"));
    process(&h, &input("twitch", "channel.follow", "{}", "101-0"));
    let c = h.calls.borrow();
    assert_eq!(c[0].3.as_deref(), Some("100-0:rep"));
    assert_eq!(c[0].3, c[1].3);
    assert_ne!(c[0].3, c[2].3);
}

#[test]
fn idempotency_key_falls_back_to_stable_hash_without_message_id() {
    let a = idempotency_key(&input("twitch", "channel.follow", "{}", ""), U1);
    let b = idempotency_key(&input("twitch", "channel.follow", "{}", ""), U1);
    let c = idempotency_key(&input("twitch", "channel.follow", "{}", ""), U2);
    assert_eq!(a, b);
    assert_ne!(a, c);
    assert!(a.ends_with(":rep") && a.len() <= 128);
}

#[test]
fn flag_off_makes_no_calls() {
    let mut h = Fake::on();
    h.flags.clear();
    assert_eq!(run_ev(&h, "channel.follow", "{}").outcome, "flag_off");
    assert_eq!(h.n_calls(), 0);
}

#[test]
fn kill_switch_wins_over_enable_flag() {
    let mut h = Fake::on();
    h.flags.insert(FLAG_KILL.to_string());
    assert_eq!(run_ev(&h, "channel.follow", "{}").outcome, "kill_switch");
    assert_eq!(h.n_calls(), 0);
}

#[test]
fn malformed_payloads_are_ignored() {
    for (et, p) in [
        ("chat.message", "not json"),
        ("chat.message", "[1,2]"),
        ("chat.message", "{}"),
        ("chat.message", r#"{"text":5}"#),
        ("channel.cheer", "{}"),
        ("donation", r#"{"amount":"abc"}"#),
        ("something.else", "{}"),
    ] {
        let h = Fake::on();
        assert_eq!(run_ev(&h, et, p).outcome, "ignored", "{et} {p}");
        assert_eq!(h.n_calls(), 0);
    }
}

#[test]
fn no_resolvable_user_is_skipped() {
    let h = Fake::on();
    *h.actor.borrow_mut() = None;
    assert_eq!(run_ev(&h, "channel.follow", "{}").outcome, "no_user");
    *h.actor.borrow_mut() = Some("not-a-uuid".into());
    assert_eq!(run_ev(&h, "channel.follow", "{}").outcome, "no_user");
    assert_eq!(h.n_calls(), 0);
}

#[test]
fn only_verified_moderation_outcomes_apply_moderation_on_the_moderated_user() {
    for (et, want) in [("moderation.timeout", -50), ("moderation.ban", -200)] {
        let h = Fake::on();
        let p = format!(r#"{{"target_user_id":"{U2}"}}"#);
        assert_eq!(run_ev(&h, et, &p).outcome, "applied");
        let c = h.calls.borrow();
        assert_eq!(c[0].1, "apply_moderation");
        assert_eq!(c[0].2["delta"], want);
        assert_eq!(c[0].2["user"], U2);
        assert_ne!(c[0].2["user"], U1);
    }
}

#[test]
fn warn_and_kick_have_no_verified_source_so_they_are_skipped_and_counted() {
    for et in ["moderation.warn", "moderation.kick"] {
        let h = Fake::on();
        let p = format!(r#"{{"target_user_id":"{U2}"}}"#);
        assert_eq!(run_ev(&h, et, &p).outcome, "unverified_moderation", "{et}");
        assert_eq!(h.n_calls(), 0, "{et}");
        assert_eq!(h.metrics.borrow().len(), 1, "counted");
    }
}

#[test]
fn a_payload_token_is_never_trusted_it_must_resolve_through_the_host_bindings() {
    // A well-formed UUID in the payload that the host did NOT bind to this event.
    let h = Fake::on();
    let forged = "99999999-9999-9999-9999-999999999999";
    let p = format!(r#"{{"target_user_id":"{forged}"}}"#);
    assert_eq!(run_ev(&h, "moderation.ban", &p).outcome, "no_user");
    assert_eq!(h.n_calls(), 0);
}

#[test]
fn moderation_without_target_is_no_user_never_the_actor() {
    let h = Fake::on();
    assert_eq!(run_ev(&h, "moderation.ban", "{}").outcome, "no_user");
    assert_eq!(h.n_calls(), 0);
}

#[test]
fn a_positive_moderation_weight_is_a_loud_config_error_and_never_a_score_raise() {
    for field in ["warn", "timeout", "kick", "ban"] {
        let h = Fake::on();
        h.config(&format!(r#"{{"weights":{{"{field}":50}}}}"#));
        let p = format!(r#"{{"target_user_id":"{U2}"}}"#);
        let out = run_ev(&h, "moderation.ban", &p);
        assert_eq!(out.outcome, "config_invalid", "{field}");
        assert_eq!(h.n_calls(), 0, "{field}");
        let h = Fake::on();
        h.config(&format!(r#"{{"weights":{{"{field}":0}}}}"#));
        assert_eq!(
            run_ev(&h, "moderation.ban", &p).outcome,
            "config_invalid",
            "{field}=0"
        );
    }
}

#[test]
fn compute_delta_forces_moderation_deltas_negative_even_if_a_weight_slipped_through() {
    let mut w = Weights {
        ban: 90,
        timeout: 7,
        ..Weights::default()
    };
    assert_eq!(compute_delta(Class::Moderation(Mod::Ban), &w), -90);
    assert_eq!(compute_delta(Class::Moderation(Mod::Timeout), &w), -7);
    w.ban = 900;
    assert_eq!(compute_delta(Class::Moderation(Mod::Ban), &w), -200);
}

#[test]
fn host_cap_results_are_capped_not_errors() {
    for m in ["daily_cap_exceeded", "quota_exceeded", "rate_limited"] {
        let h = Fake::on();
        h.reply(Err(ToolErr::InvalidArgs(m.into())));
        assert_eq!(run_ev(&h, "channel.follow", "{}").outcome, "capped");
    }
    let h = Fake::on();
    h.reply(Ok(json!({"score": 600, "applied": false})));
    assert_eq!(run_ev(&h, "channel.follow", "{}").outcome, "capped");
}

#[test]
fn other_tool_errors_are_errors_with_label_only_log() {
    let h = Fake::on();
    h.reply(Err(ToolErr::NotGranted));
    assert_eq!(run_ev(&h, "channel.follow", "{}").outcome, "error");
    let logs = h.logs.borrow();
    assert!(
        logs.iter()
            .any(|(l, f)| *l == Level::Error && f["error"] == "not_granted")
    );
}

#[test]
fn invalid_config_is_a_loud_skip_without_calls() {
    for bad in [
        "not json",
        r#"{"unknown":1}"#,
        r#"{"weights":{"nope":1}}"#,
        r#"{"weights":{"follow":99999}}"#,
        r#"{"cooldowns":{"chat_message":-1}}"#,
        r#"{"low_score_notice_at":100}"#,
        r#"{"low_score_notice_at":400,"low_score_hard_floor":400}"#,
    ] {
        let h = Fake::on();
        h.config(bad);
        assert_eq!(
            run_ev(&h, "channel.follow", "{}").outcome,
            "config_invalid",
            "{bad}"
        );
        assert_eq!(h.n_calls(), 0);
        assert!(h.logs.borrow().iter().any(|(l, _)| *l == Level::Error));
    }
}

#[test]
fn weight_override_still_clamps_to_activity_cap() {
    let h = Fake::on();
    h.config(r#"{"weights":{"follow":3}}"#);
    run_ev(&h, "channel.follow", "{}");
    assert_eq!(last_delta(&h), 3);
    let h = Fake::on();
    h.config(r#"{"weights":{"follow":50}}"#);
    run_ev(&h, "channel.follow", "{}");
    assert_eq!(last_delta(&h), 5);
}

#[test]
fn moderation_weight_clamps_to_200() {
    let h = Fake::on();
    h.config(r#"{"weights":{"ban":-900}}"#);
    let p = format!(r#"{{"target_user_id":"{U2}"}}"#);
    run_ev(&h, "moderation.ban", &p);
    assert_eq!(last_delta(&h), -200);
}

#[test]
fn low_score_notice_fires_once_per_window() {
    let h = Fake::on();
    h.reply(Ok(json!({"score": 430, "applied": true})));
    run_ev(&h, "channel.follow", "{}");
    run_ev(&h, "channel.follow", "{}");
    assert_eq!(h.warns(), 1);
    // The warn carries only an outcome label, no identity.
    let logs = h.logs.borrow();
    let w = logs.iter().find(|(l, _)| *l == Level::Warn).unwrap();
    assert_eq!(w.1, json!({"outcome": "low_score_notice"}));
}

#[test]
fn low_score_notice_skips_healthy_and_hard_floor_scores() {
    for score in [451, 600, 400, 350] {
        let h = Fake::on();
        h.reply(Ok(json!({"score": score, "applied": true})));
        run_ev(&h, "channel.follow", "{}");
        assert_eq!(h.warns(), 0, "score {score}");
    }
}

#[test]
fn platform_is_passed_so_the_service_can_ban() {
    let h = Fake::on();
    process(&h, &input("discord", "channel.follow", "{}", "1-0"));
    assert_eq!(h.calls.borrow()[0].2["platform"], "discord");
    let h = Fake::on();
    process(&h, &input("kick", "channel.follow", "{}", "1-0"));
    assert!(h.calls.borrow()[0].2.get("platform").is_none());
}

#[test]
fn no_floor_or_ban_logic_calls_only_adjust() {
    let h = Fake::on();
    h.reply(Ok(json!({"score": 300, "applied": true})));
    run_ev(&h, "channel.follow", "{}");
    let c = h.calls.borrow();
    assert_eq!(c.len(), 1);
    assert_eq!(c[0].1, "adjust");
}

#[test]
fn metrics_emitted_for_every_outcome() {
    let h = Fake::on();
    run_ev(&h, "channel.follow", "{}");
    run_ev(&h, "nope", "{}");
    let m = h.metrics.borrow();
    assert_eq!(m[0], ("applied".to_string(), "follow".to_string()));
    assert_eq!(m[1], ("ignored".to_string(), "unknown".to_string()));
}

#[test]
fn reasons_match_service_charset() {
    let classes = [
        Class::Chat,
        Class::Command,
        Class::Follow,
        Class::Subscription(1),
        Class::Subscription(2),
        Class::Subscription(3),
        Class::Gift(1.0),
        Class::Cheer(1.0),
        Class::Raid,
        Class::Donation(1.0),
        Class::Boost,
        Class::Giveaway,
        Class::Moderation(Mod::Warn),
        Class::Moderation(Mod::Timeout),
        Class::Moderation(Mod::Kick),
        Class::Moderation(Mod::Ban),
    ];
    for c in classes {
        let r = c.reason();
        assert!(!r.is_empty() && r.len() <= 100);
        assert!(
            r.bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._:-".contains(&b))
        );
    }
}
