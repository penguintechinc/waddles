//! Real-Valkey tests for the APP/SERVICE bundle reconcile scheduler: two replicas race for each
//! slot and exactly one publishes a verified `hub`/`bundle.reconcile` envelope. Own test binary.
//! Needs `VALKEY_URL` (a real Valkey); an unset var FAILS, never skips.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::{Arc, Mutex};
use std::time::Duration;

use penguin_spine::{verify_binding, KeyRing, SpineError, SpineMetrics, StageEnvelope};
use svc_ingest::connector::bundle_schedule::{
    BundleScheduleTarget, ScheduleRunner, SpineReconcileSink, TickOutcome, ValkeyScheduleLease,
    RECONCILE_EVENT_TYPE,
};
use svc_ingest::connector::reconcile_hook::ReconcileSpec;
use svc_ingest::identity_crypto::{Dek, DekProvider, DekUnavailableError};
use svc_ingest::publish::EventAppender;
use svc_ingest::scope_resolver::ResolvedScope;

fn url() -> String {
    std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: scheduler tests need a REAL Valkey (no silent skip)")
}

async fn conn() -> redis::aio::MultiplexedConnection {
    let client = redis::Client::open(url()).expect("client");
    let cfg =
        redis::AsyncConnectionConfig::new().set_response_timeout(Some(Duration::from_secs(2)));
    client
        .get_multiplexed_async_connection_with_config(&cfg)
        .await
        .expect("connect")
}

/// Records every `XADD` the sink makes (stream + envelope).
#[derive(Default, Clone)]
struct Recorder(Arc<Mutex<Vec<(String, StageEnvelope)>>>);

impl EventAppender for Recorder {
    async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
        self.0
            .lock()
            .unwrap()
            .push((stream.to_owned(), env.clone()));
        Ok("1-0".to_owned())
    }
}

struct NoDek;

impl DekProvider for NoDek {
    async fn get_dek(&self, _t: i64) -> Result<(Dek, u32), DekUnavailableError> {
        panic!("a bundle.reconcile event carries no identity field, so no DEK may be requested");
    }
}

struct NoMetrics;

impl SpineMetrics for NoMetrics {}

fn ring() -> Arc<KeyRing> {
    Arc::new(KeyRing::new(vec![("k1".to_owned(), vec![9u8; 32])]))
}

fn runner(
    replica: &str,
    appender: &Recorder,
) -> ScheduleRunner<ValkeyScheduleLease, SpineReconcileSink<Recorder, NoDek>> {
    let lease = ValkeyScheduleLease::with_replica_id(futures_conn(), replica.to_owned());
    let sink = SpineReconcileSink {
        appender: Arc::new(appender.clone()),
        metrics: Arc::new(NoMetrics),
        keyring: ring(),
        active_kid: "k1".to_owned(),
        dek: Arc::new(NoDek),
        identity_metrics: prometheus::IntCounterVec::new(
            prometheus::Opts::new("sched_test_identity_total", "test"),
            &["tenant", "status"],
        )
        .unwrap(),
    };
    ScheduleRunner::new(lease, sink)
}

fn futures_conn() -> redis::aio::MultiplexedConnection {
    // Connections are opened once per test via `block_in_place` so `runner` stays synchronous.
    tokio::task::block_in_place(|| tokio::runtime::Handle::current().block_on(conn()))
}

fn target(app: &str, interval_s: u64) -> BundleScheduleTarget {
    BundleScheduleTarget::new(
        ResolvedScope {
            tenant_id: 4,
            community_id: 9,
            tenant_slug: "acme".to_owned(),
            community_name: Some("main".to_owned()),
        },
        app,
        ReconcileSpec::new(Duration::from_secs(interval_s)).unwrap(),
    )
    .unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn two_replicas_publish_exactly_one_verified_reconcile_event_per_slot() {
    let app = format!("waddles.test.sched.{}", uuid::Uuid::new_v4().simple());
    let t = target(&app, 30);
    let rec = Recorder::default();
    let (a, b) = (runner("replica-a", &rec), runner("replica-b", &rec));

    let (oa, ob) = tokio::join!(a.tick(&t), b.tick(&t));
    let fired = [oa, ob]
        .iter()
        .filter(|o| **o == TickOutcome::Fired)
        .count();
    let skipped = [oa, ob]
        .iter()
        .filter(|o| **o == TickOutcome::SkippedLease)
        .count();
    println!(
        "slot 1: fired={fired} skipped_lease={skipped} appends={}",
        rec.0.lock().unwrap().len()
    );
    assert_eq!((fired, skipped), (1, 1), "{oa:?} {ob:?}");

    let appended = rec.0.lock().unwrap().clone();
    assert_eq!(appended.len(), 1, "exactly one XADD across both replicas");
    let (stream, env) = &appended[0];
    assert_eq!(env.event.platform, "hub");
    assert_eq!(env.event.event_type, RECONCILE_EVENT_TYPE);
    assert_eq!((env.tenant_id, env.community_id), (Some(4), Some(9)));
    verify_binding(&ring(), env).expect("the envelope's binding MAC verifies under the keyring");
    assert!(
        stream.contains(":hub:"),
        "published on a `hub` source stream, got {stream}"
    );

    // The slot is still held: a later tick by EITHER replica within the interval does not fire.
    assert_eq!(a.tick(&t).await, TickOutcome::SkippedLease);
    assert_eq!(b.tick(&t).await, TickOutcome::SkippedLease);
    assert_eq!(rec.0.lock().unwrap().len(), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn distinct_bundles_and_communities_are_independent_slots() {
    let rec = Recorder::default();
    let a = runner("replica-a", &rec);
    let t1 = target(
        &format!("waddles.test.sched.{}", uuid::Uuid::new_v4().simple()),
        30,
    );
    let t2 = target(
        &format!("waddles.test.sched.{}", uuid::Uuid::new_v4().simple()),
        30,
    );
    assert_eq!(a.tick(&t1).await, TickOutcome::Fired);
    assert_eq!(a.tick(&t2).await, TickOutcome::Fired);
    assert_eq!(rec.0.lock().unwrap().len(), 2);
}
