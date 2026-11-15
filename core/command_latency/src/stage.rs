//! Stage-side helpers: the stamp transitions each service applies and the single outbound-confirm
//! report. Pure functions over the tracestate string plus the metric/log emission, so each service
//! wires one call per hop and the behaviour is tested once, here.

use std::time::Duration;

use crate::metrics::{self, Hop, Outcome};
use crate::stamp::{elapsed_ms, entry_id_ms, now_ms, Implausible, Stamps};

/// Records the queue-wait hop for an entry just read from a stream: `now - XADD time` (the Valkey
/// entry-id millisecond prefix). `hop` is [`Hop::QueueWait`] at process and [`Hop::ActionQueueWait`]
/// at action. Returns the milliseconds recorded, `None` when the id is unparseable or the instant
/// implausible (cross-clock skew or a very old redelivery -- counted, never recorded).
pub fn record_queue_wait(hop: Hop, entry_id: &str, now: u64) -> Option<u64> {
    if crate::switch::is_disabled() {
        return None;
    }
    let published = entry_id_ms(entry_id)?;
    match elapsed_ms(published, now) {
        Ok(ms) => {
            metrics::metrics().record_hop(hop, Duration::from_millis(ms));
            tracing::debug!(hop = hop.label(), elapsed_ms = ms, "queue wait recorded");
            Some(ms)
        }
        Err(why) => {
            metrics::metrics().record_marker_invalid(why.label());
            None
        }
    }
}

/// The tracestate the process stage puts on the action-stream envelope: the inbound stamps plus the
/// ingest-stream publish time (from the entry id), this stage's consume and hand-off instants and
/// the REGISTERED command (never user text). Records the `process_stage` hop. Without a stamp on
/// the inbound envelope (kill switch was ON at ingest, or a pre-upgrade producer) it returns the
/// inbound tracestate unchanged and records nothing.
pub fn process_handoff_tracestate(
    inbound: Option<&str>,
    entry_id: &str,
    consumed_ms: u64,
    handoff_ms: u64,
    command: Option<String>,
) -> Option<String> {
    if crate::switch::is_disabled() {
        return inbound.map(str::to_string);
    }
    let mut s = Stamps::from_tracestate(inbound);
    if s.t0_ms.is_none() {
        return inbound.map(str::to_string);
    }
    s.publish_ms = entry_id_ms(entry_id);
    s.consume_ms = Some(consumed_ms);
    s.handoff_ms = Some(handoff_ms);
    s.command = command;
    metrics::metrics().record_hop(
        Hop::ProcessStage,
        Duration::from_millis(handoff_ms.saturating_sub(consumed_ms)),
    );
    Some(s.merge_into_tracestate(inbound))
}

/// The tracestate the action stage forwards into the executor invoke: the stamps plus the
/// action-consume and invoke-sent instants. Records `dispatch_to_invoke`.
pub fn action_invoke_tracestate(
    inbound: Option<&str>,
    action_consume_ms: u64,
    invoke_ms: u64,
) -> Option<String> {
    if crate::switch::is_disabled() {
        return inbound.map(str::to_string);
    }
    let mut s = Stamps::from_tracestate(inbound);
    if s.t0_ms.is_none() {
        return inbound.map(str::to_string);
    }
    s.action_consume_ms = Some(action_consume_ms);
    s.invoke_ms = Some(invoke_ms);
    metrics::metrics().record_hop(
        Hop::DispatchToInvoke,
        Duration::from_millis(invoke_ms.saturating_sub(action_consume_ms)),
    );
    Some(s.merge_into_tracestate(inbound))
}

/// What a confirmed outbound op is accounted as.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// A real user command: recorded in `waddles_command_e2e_seconds`.
    User,
    /// The synthetic canary: recorded in `waddles_canary_e2e_seconds` only, so it never pollutes
    /// the user histograms.
    Canary,
}

/// The result of [`report_confirmed`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reported {
    /// First door -> confirm, milliseconds.
    pub e2e_ms: u64,
    /// `true` when the 1.0 s budget was met or exceeded.
    pub over_budget: bool,
}

/// The single end-to-end report, called where the outbound platform op completes (svc-action's
/// "discord outbound op confirmed" point): records the executor-to-outbound and outbound-call hops,
/// the e2e histogram (or the canary histogram), the over-budget counter, and -- at or over budget --
/// the structured WARN `command over latency budget` carrying the per-hop breakdown (milliseconds per
/// hop, numeric ids, registered command, bundle id; no user text, username or payload).
///
/// `None` when telemetry is disabled or the tracestate carries no usable first-door stamp (that case
/// bumps `waddles_command_latency_marker_invalid_total{reason}`).
#[allow(clippy::too_many_arguments)]
pub fn report_confirmed(
    kind: Kind,
    tracestate: Option<&str>,
    app_id: &str,
    tenant_id: i32,
    community_id: i32,
    outcome: Outcome,
    outbound_request_ms: u64,
    confirm_ms: u64,
) -> Option<Reported> {
    if crate::switch::is_disabled() {
        return None;
    }
    let m = metrics::metrics();
    let stamps = Stamps::from_tracestate(tracestate);
    let Some(t0) = stamps.t0_ms else {
        m.record_marker_invalid("missing");
        return None;
    };
    let e2e_ms = match elapsed_ms(t0, confirm_ms) {
        Ok(ms) => ms,
        Err(why @ (Implausible::Skewed | Implausible::Stale)) => {
            m.record_marker_invalid(why.label());
            return None;
        }
    };
    let parts = metrics::breakdown(&stamps, outbound_request_ms, confirm_ms);
    if let Some(iv) = stamps.invoke_ms {
        m.record_hop(
            Hop::ExecutorToOutbound,
            Duration::from_millis(outbound_request_ms.saturating_sub(iv)),
        );
    }
    m.record_hop(
        Hop::OutboundCall,
        Duration::from_millis(confirm_ms.saturating_sub(outbound_request_ms)),
    );
    let elapsed = Duration::from_millis(e2e_ms);
    let over_budget = elapsed >= metrics::BUDGET;
    match kind {
        Kind::Canary => m.record_canary(outcome, elapsed),
        Kind::User => {
            let command = stamps
                .command
                .as_deref()
                .unwrap_or(crate::registry::NO_COMMAND);
            let reg = crate::registry::global();
            let lang = reg.bundle_lang(app_id);
            let bundle = reg.bundle_label(app_id);
            m.record_e2e(
                command,
                &bundle,
                lang,
                outcome,
                stamps.door.unwrap_or(crate::Door::Publish),
                elapsed,
            );
        }
    }
    if over_budget {
        let ids = crate::span::span_ids(&tracing::Span::current());
        tracing::warn!(
            kind = ?kind,
            command = stamps.command.as_deref().unwrap_or(crate::registry::NO_COMMAND),
            app_id,
            tenant_id,
            community_id,
            outcome = outcome.label(),
            e2e_ms,
            budget_ms = 1000u64,
            slowest_hop = parts.iter().max_by_key(|(_, v)| *v).map_or("", |(h, _)| *h),
            breakdown = %metrics::render_breakdown(&parts),
            trace_id = ids.as_ref().map_or("", |i| i.0.as_str()),
            span_id = ids.as_ref().map_or("", |i| i.1.as_str()),
            "command over latency budget"
        );
    }
    Some(Reported {
        e2e_ms,
        over_budget,
    })
}

tokio::task_local! {
    static ACTION_CONSUMED_MS: u64;
}

/// Runs `fut` with the action stage's consume instant as the task's current one, so the executor
/// invoke (several calls deep, no spare parameter) can stamp `dispatch_to_invoke` without widening
/// `invoke_dispatch`'s signature.
pub async fn with_action_consumed<F: std::future::Future>(consumed_ms: u64, fut: F) -> F::Output {
    ACTION_CONSUMED_MS.scope(consumed_ms, fut).await
}

/// The action-consume instant of the current task, if set by [`with_action_consumed`].
pub fn action_consumed_ms() -> Option<u64> {
    ACTION_CONSUMED_MS.try_with(|v| *v).ok()
}

/// Convenience for callers that just need "now" for a stamp.
pub fn stamp_now() -> u64 {
    now_ms()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stamp::Door;
    use std::sync::{Arc, Mutex};
    use tracing::field::{Field, Visit};
    use tracing_subscriber::layer::{Context, SubscriberExt};
    use tracing_subscriber::Layer;

    #[derive(Default, Clone)]
    struct Captured(Arc<Mutex<Vec<(String, tracing::Level, String)>>>);

    struct Fields(String);
    impl Visit for Fields {
        fn record_debug(&mut self, f: &Field, v: &dyn std::fmt::Debug) {
            self.0.push_str(&format!("{}={:?} ", f.name(), v));
        }
    }

    impl<S: tracing::Subscriber> Layer<S> for Captured {
        fn on_event(&self, e: &tracing::Event<'_>, _: Context<'_, S>) {
            let mut v = Fields(String::new());
            e.record(&mut v);
            self.0.lock().unwrap().push((
                e.metadata().target().to_string(),
                *e.metadata().level(),
                v.0,
            ));
        }
    }

    fn stamped(command: Option<&str>) -> String {
        Stamps {
            t0_ms: Some(10_000),
            door: Some(Door::Gateway),
            publish_ms: Some(10_020),
            consume_ms: Some(10_100),
            handoff_ms: Some(10_500),
            action_consume_ms: Some(10_510),
            invoke_ms: Some(10_520),
            command: command.map(str::to_string),
        }
        .merge_into_tracestate(Some("vendor=1"))
    }

    #[test]
    fn process_handoff_carries_stamps_command_and_foreign_members() {
        let _g = crate::switch::tests::SWITCH_LOCK.blocking_lock();
        crate::switch::set_disabled(false);
        let inbound = Stamps {
            t0_ms: Some(10_000),
            door: Some(Door::Gateway),
            ..Stamps::default()
        }
        .merge_into_tracestate(Some("vendor=1"));
        let out = process_handoff_tracestate(
            Some(&inbound),
            "10020-0",
            10_100,
            10_500,
            Some("ping".into()),
        )
        .unwrap();
        let s = Stamps::from_tracestate(Some(&out));
        assert_eq!(s.t0_ms, Some(10_000));
        assert_eq!(s.publish_ms, Some(10_020));
        assert_eq!(s.consume_ms, Some(10_100));
        assert_eq!(s.handoff_ms, Some(10_500));
        assert_eq!(s.command.as_deref(), Some("ping"));
        assert!(out.ends_with(",vendor=1"));
    }

    #[test]
    fn unstamped_inbound_is_passed_through_untouched() {
        let _g = crate::switch::tests::SWITCH_LOCK.blocking_lock();
        crate::switch::set_disabled(false);
        assert_eq!(
            process_handoff_tracestate(Some("vendor=1"), "1-0", 1, 2, None).as_deref(),
            Some("vendor=1")
        );
        assert_eq!(process_handoff_tracestate(None, "1-0", 1, 2, None), None);
        assert_eq!(action_invoke_tracestate(None, 1, 2), None);
    }

    #[test]
    fn disabled_switch_changes_nothing_anywhere() {
        let _g = crate::switch::tests::SWITCH_LOCK.blocking_lock();
        crate::switch::set_disabled(true);
        let ts = stamped(Some("ping"));
        assert_eq!(
            process_handoff_tracestate(Some(&ts), "1-0", 1, 2, None).as_deref(),
            Some(ts.as_str())
        );
        assert_eq!(
            action_invoke_tracestate(Some(&ts), 1, 2).as_deref(),
            Some(ts.as_str())
        );
        assert_eq!(record_queue_wait(Hop::QueueWait, "10-0", 20), None);
        assert_eq!(
            report_confirmed(
                Kind::User,
                Some(&ts),
                "a",
                1,
                1,
                Outcome::Ok,
                10_700,
                10_800
            ),
            None
        );
        crate::switch::set_disabled(false);
    }

    #[test]
    fn queue_wait_refuses_a_forged_future_entry_id() {
        let _g = crate::switch::tests::SWITCH_LOCK.blocking_lock();
        crate::switch::set_disabled(false);
        assert_eq!(record_queue_wait(Hop::QueueWait, "1000-0", 1_080), Some(80));
        assert_eq!(
            record_queue_wait(Hop::QueueWait, "999999999999-0", 1_080),
            None
        );
        assert_eq!(record_queue_wait(Hop::QueueWait, "nope", 1_080), None);
    }

    #[test]
    fn over_budget_confirm_emits_one_warn_with_the_breakdown_and_no_text() {
        let _g = crate::switch::tests::SWITCH_LOCK.blocking_lock();
        crate::switch::set_disabled(false);
        let cap = Captured::default();
        let sub = tracing_subscriber::registry().with(cap.clone());
        let ts = stamped(Some("ping"));
        let rep = tracing::subscriber::with_default(sub, || {
            report_confirmed(
                Kind::User,
                Some(&ts),
                "waddles.core.example.ping",
                7,
                9,
                Outcome::Ok,
                10_700,
                11_200,
            )
        })
        .unwrap();
        assert_eq!(
            rep,
            Reported {
                e2e_ms: 1_200,
                over_budget: true
            }
        );
        let events = cap.0.lock().unwrap();
        let warns: Vec<_> = events
            .iter()
            .filter(|(_, l, _)| *l == tracing::Level::WARN)
            .collect();
        assert_eq!(warns.len(), 1, "exactly one WARN: {events:?}");
        let line = &warns[0].2;
        for needle in [
            "command over latency budget",
            "command=\"ping\"",
            "tenant_id=7",
            "community_id=9",
            "e2e_ms=1200",
            "slowest_hop=\"outbound_call\"",
            "process_stage=400",
        ] {
            assert!(line.contains(needle), "missing {needle:?} in {line}");
        }
    }

    #[test]
    fn under_budget_confirm_emits_no_warn_and_reports_false() {
        let _g = crate::switch::tests::SWITCH_LOCK.blocking_lock();
        crate::switch::set_disabled(false);
        let cap = Captured::default();
        let sub = tracing_subscriber::registry().with(cap.clone());
        let ts = stamped(None);
        let rep = tracing::subscriber::with_default(sub, || {
            report_confirmed(
                Kind::User,
                Some(&ts),
                "a",
                1,
                1,
                Outcome::Ok,
                10_600,
                10_700,
            )
        })
        .unwrap();
        assert!(!rep.over_budget);
        assert_eq!(rep.e2e_ms, 700);
        assert!(cap
            .0
            .lock()
            .unwrap()
            .iter()
            .all(|(_, l, _)| *l != tracing::Level::WARN));
    }

    #[test]
    fn missing_or_forged_first_door_yields_no_sample() {
        let _g = crate::switch::tests::SWITCH_LOCK.blocking_lock();
        crate::switch::set_disabled(false);
        assert_eq!(
            report_confirmed(Kind::User, None, "a", 1, 1, Outcome::Ok, 5, 6),
            None
        );
        let future = Stamps {
            t0_ms: Some(10_000_000),
            ..Stamps::default()
        }
        .merge_into_tracestate(None);
        assert_eq!(
            report_confirmed(Kind::User, Some(&future), "a", 1, 1, Outcome::Ok, 5, 6),
            None
        );
    }
}
