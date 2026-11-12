//! Dispatcher tests: the sweep state machine against a stateful fake outbox and a scripted
//! sender -- the confirmed-result rule, lease discipline, retry/dead/refused handling and the
//! metrics. The outbox SQL itself is proven on a real Postgres in
//! `alembic/tests/test_0098_reputation_floor_ban.py`; the command the sender receives is proven
//! in `capabilities::floor_ban_tests` and `core/connector_e2e/tests/floor_ban_outbound.rs`.

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;

use super::*;
use crate::stage_metrics::test_support::Harness;

const USER: &str = "7a1d2c3e-1111-4222-8333-444455556666";

fn row(id: i64) -> ClaimedFloorBan {
    ClaimedFloorBan {
        id,
        tenant_id: 7,
        community_id: 3,
        user: USER.parse().unwrap(),
        scope: 1,
        platform: "discord".to_string(),
        attempt: 1,
        lease: Uuid::from_u128(u128::try_from(id).unwrap() + 1000),
    }
}

/// A stateful fake outbox: the recorded completions and canned answers.
#[derive(Default)]
struct FakeStore {
    claim: Mutex<Vec<ClaimedFloorBan>>,
    dead: Mutex<Vec<DeadFloorBan>>,
    recheck: Mutex<HashMap<i64, Recheck>>,
    /// What `complete` answers, per outcome kind.
    answer: Mutex<HashMap<&'static str, Completed>>,
    completions: Mutex<Vec<(i64, Uuid, Completion, Option<String>)>>,
    rechecks: Mutex<Vec<i64>>,
    fail_claim: bool,
    fail_complete: bool,
}

impl FakeStore {
    fn with_rows(rows: Vec<ClaimedFloorBan>) -> Self {
        Self {
            claim: Mutex::new(rows),
            ..Self::default()
        }
    }
    fn completions(&self) -> Vec<(i64, Uuid, Completion, Option<String>)> {
        let mut c = self.completions.lock().unwrap().clone();
        c.sort_by_key(|(id, ..)| *id);
        c
    }
}

impl FloorBanStore for FakeStore {
    fn reap(&self, _limit: u32) -> StoreFuture<'_, Vec<DeadFloorBan>> {
        Box::pin(async { Ok(std::mem::take(&mut *self.dead.lock().unwrap())) })
    }
    fn claim(&self, _limit: u32, _lease: u32) -> StoreFuture<'_, Vec<ClaimedFloorBan>> {
        Box::pin(async {
            if self.fail_claim {
                return Err("db down".to_string());
            }
            Ok(std::mem::take(&mut *self.claim.lock().unwrap()))
        })
    }
    fn recheck(&self, id: i64, _lease: Uuid) -> StoreFuture<'_, Recheck> {
        Box::pin(async move {
            self.rechecks.lock().unwrap().push(id);
            Ok(self
                .recheck
                .lock()
                .unwrap()
                .get(&id)
                .copied()
                .unwrap_or(Recheck::Breached))
        })
    }
    fn complete<'a>(
        &'a self,
        id: i64,
        lease: Uuid,
        outcome: &'a Completion,
        op_id: Option<&'a str>,
    ) -> StoreFuture<'a, Completed> {
        Box::pin(async move {
            if self.fail_complete {
                return Err("db down".to_string());
            }
            self.completions.lock().unwrap().push((
                id,
                lease,
                outcome.clone(),
                op_id.map(str::to_string),
            ));
            let kind = match outcome {
                Completion::Done => "done",
                Completion::Failed(_) => "failed",
                Completion::Refused(_) => "refused",
            };
            Ok(self
                .answer
                .lock()
                .unwrap()
                .get(kind)
                .copied()
                .unwrap_or(match kind {
                    "done" => Completed::Done,
                    "failed" => Completed::Retrying,
                    _ => Completed::Refused,
                }))
        })
    }
}

/// A sender scripted per row id; records what it was asked to send.
#[derive(Default)]
struct FakeSender {
    script: Mutex<HashMap<i64, VecDeque<Result<String, FloorSendFailure>>>>,
    sent: Mutex<Vec<FloorBanCommand>>,
}

impl FakeSender {
    fn will(&self, id: i64, r: Result<String, FloorSendFailure>) {
        self.script
            .lock()
            .unwrap()
            .entry(id)
            .or_default()
            .push_back(r);
    }
}

impl FloorBanSender for FakeSender {
    fn send<'a>(
        &'a self,
        cmd: &'a FloorBanCommand,
    ) -> Pin<Box<dyn Future<Output = Result<String, FloorSendFailure>> + Send + 'a>> {
        Box::pin(async move {
            self.sent.lock().unwrap().push(cmd.clone());
            self.script
                .lock()
                .unwrap()
                .get_mut(&cmd.id)
                .and_then(VecDeque::pop_front)
                .unwrap_or_else(|| Ok(format!("fb-{}-{}-x", cmd.id, cmd.attempt)))
        })
    }
}

fn metrics() -> (Harness, StageMetrics) {
    let h = Harness::new();
    let m = StageMetrics::new(&h.meter());
    (h, m)
}

fn count(h: &Harness, result: &str) -> f64 {
    h.count("waddles_floor_ban_dispatch_total", &[("result", result)])
}

#[tokio::test]
async fn a_confirmed_send_completes_the_row_done_for_the_lease_holder_with_its_op_id() {
    let store = FakeStore::with_rows(vec![row(1)]);
    let sender = FakeSender::default();
    let (h, m) = metrics();
    let r = sweep_once(&store, &sender, &m).await;
    assert_eq!(
        r,
        SweepReport {
            claimed: 1,
            done: 1,
            ..SweepReport::default()
        }
    );
    let sent = sender.sent.lock().unwrap().clone();
    assert_eq!(sent.len(), 1);
    assert_eq!(
        (sent[0].id, sent[0].community_id, sent[0].platform.as_str()),
        (1, 3, "discord")
    );
    assert_eq!(
        store.completions(),
        vec![(
            1,
            row(1).lease,
            Completion::Done,
            Some("fb-1-1-x".to_string())
        )],
        "completed with the lease token the claim minted"
    );
    assert_eq!(count(&h, "done"), 1.0);
}

#[tokio::test]
async fn a_send_that_was_not_confirmed_is_never_recorded_done() {
    let store = FakeStore::with_rows(vec![row(1), row(2)]);
    let sender = FakeSender::default();
    sender.will(
        1,
        Err(FloorSendFailure {
            error: FloorSendError::Retry("relay_unconfirmed".into()),
            op_id: Some("fb-1-1-z".into()),
        }),
    );
    sender.will(
        2,
        Err(FloorSendFailure {
            error: FloorSendError::Retry("relay_unavailable".into()),
            op_id: None,
        }),
    );
    let (h, m) = metrics();
    let r = sweep_once(&store, &sender, &m).await;
    assert_eq!((r.claimed, r.done, r.failed), (2, 0, 2));
    let c = store.completions();
    assert!(
        c.iter()
            .all(|(_, _, o, _)| matches!(o, Completion::Failed(_))),
        "no row may be marked done without the sender's confirmation: {c:?}"
    );
    assert_eq!(c[0].2, Completion::Failed("relay_unconfirmed".into()));
    assert_eq!(c[0].3.as_deref(), Some("fb-1-1-z"), "correlation kept");
    assert_eq!(count(&h, "failed"), 2.0);
    assert_eq!(count(&h, "done"), 0.0);
}

#[tokio::test]
async fn a_permanent_refusal_closes_the_row_and_exhausted_attempts_raise_the_dead_alert() {
    let store = FakeStore::with_rows(vec![row(1), row(2)]);
    store
        .answer
        .lock()
        .unwrap()
        .insert("failed", Completed::Dead);
    let sender = FakeSender::default();
    sender.will(
        1,
        Err(FloorSendFailure {
            error: FloorSendError::Permanent("protected_target".into()),
            op_id: None,
        }),
    );
    sender.will(
        2,
        Err(FloorSendFailure {
            error: FloorSendError::Retry("relay_unconfirmed".into()),
            op_id: None,
        }),
    );
    let (h, m) = metrics();
    let r = sweep_once(&store, &sender, &m).await;
    assert_eq!((r.refused, r.dead, r.done), (1, 1, 0));
    assert_eq!(
        store.completions()[0].2,
        Completion::Refused("protected_target".into())
    );
    assert_eq!(count(&h, "refused"), 1.0);
    assert_eq!(count(&h, "dead"), 1.0);
}

#[tokio::test]
async fn m2_rows_reaped_as_dead_are_alerted_with_a_metric_each() {
    let store = FakeStore::default();
    *store.dead.lock().unwrap() = vec![
        DeadFloorBan {
            id: 8,
            tenant_id: 7,
            community_id: 3,
        },
        DeadFloorBan {
            id: 9,
            tenant_id: 7,
            community_id: 4,
        },
    ];
    let (h, m) = metrics();
    let r = sweep_once(&store, &FakeSender::default(), &m).await;
    assert_eq!((r.dead, r.claimed), (2, 0));
    assert_eq!(count(&h, "dead"), 2.0);
}

#[tokio::test]
async fn the_breach_is_rechecked_right_before_sending_and_a_recovered_score_cancels_the_ban() {
    let store = FakeStore::with_rows(vec![row(1), row(2)]);
    store
        .recheck
        .lock()
        .unwrap()
        .insert(1, Recheck::NotBreached);
    store.recheck.lock().unwrap().insert(2, Recheck::LeaseLost);
    let sender = FakeSender::default();
    let (h, m) = metrics();
    let r = sweep_once(&store, &sender, &m).await;
    assert_eq!((r.refused, r.lease_lost, r.done), (1, 1, 0));
    assert!(
        sender.sent.lock().unwrap().is_empty(),
        "nothing is sent for a cancelled or foreign row"
    );
    assert!(
        store.completions().is_empty(),
        "the SQL already closed the cancelled row; a foreign lease is never completed"
    );
    assert_eq!(store.rechecks.lock().unwrap().len(), 2);
    assert_eq!(count(&h, "refused"), 1.0);
    assert_eq!(count(&h, "lease_lost"), 1.0);
}

#[tokio::test]
async fn l2_a_completion_with_a_lost_lease_is_counted_and_never_double_recorded() {
    let store = FakeStore::with_rows(vec![row(1)]);
    store
        .answer
        .lock()
        .unwrap()
        .insert("done", Completed::LeaseLost);
    let (h, m) = metrics();
    let r = sweep_once(&store, &FakeSender::default(), &m).await;
    assert_eq!((r.lease_lost, r.done), (1, 0));
    assert_eq!(count(&h, "lease_lost"), 1.0);
}

#[tokio::test]
async fn store_failures_are_loud_and_leave_the_lease_to_expire() {
    // Claim failure: nothing sent, an error counted.
    let store = FakeStore {
        fail_claim: true,
        ..FakeStore::default()
    };
    let (h, m) = metrics();
    let r = sweep_once(&store, &FakeSender::default(), &m).await;
    assert_eq!((r.claimed, r.store_errors), (0, 1));
    assert_eq!(count(&h, "store_error"), 1.0);

    // Completion failure after a confirmed send: counted, not silently dropped.
    let store = FakeStore {
        fail_complete: true,
        claim: Mutex::new(vec![row(1)]),
        ..FakeStore::default()
    };
    let (h, m) = metrics();
    let r = sweep_once(&store, &FakeSender::default(), &m).await;
    assert_eq!((r.claimed, r.done, r.store_errors), (1, 0, 1));
    assert_eq!(count(&h, "store_error"), 1.0);
}

#[tokio::test]
async fn the_sweeper_loop_runs_until_shutdown_and_reports_nothing_lost() {
    let store = Arc::new(FakeStore::with_rows(vec![row(1), row(2), row(3)]));
    let sender = Arc::new(FakeSender::default());
    let (_h, m) = metrics();
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    let (s2, t2) = (Arc::clone(&store), Arc::clone(&sender));
    let task = tokio::spawn(async move {
        run_floor_ban_sweeper(s2, t2, Arc::new(m), async {
            let _ = rx.await;
        })
        .await;
    });
    // The first sweep happens immediately; wait for it, then shut down.
    for _ in 0..100 {
        if store.completions.lock().unwrap().len() == 3 {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    tx.send(()).unwrap();
    task.await.unwrap();
    assert_eq!(store.completions().len(), 3);
    assert_eq!(sender.sent.lock().unwrap().len(), 3);
}
