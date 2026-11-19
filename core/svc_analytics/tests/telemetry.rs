//! Telemetry emission gate: one batch through the consumer must produce >=1 log record,
//! >=1 metric data point, >=1 histogram and >=1 span (counts printed), and nothing PII.

mod support;

use std::sync::Arc;

use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use support::*;
use svc_analytics::consumer::{now_ms, Consumer};
use svc_analytics::flag::FlagGate;
use svc_analytics::health::Ready;
use svc_analytics::metrics::Metrics;

#[tokio::test]
async fn consumer_emits_logs_metrics_histograms_and_spans_without_pii() {
    let tel = penguin_logging::testing::init_test_telemetry("svc-analytics-test");
    let stream = unique_stream();
    let mut r = ConnectionManager::new(redis::Client::open(valkey_url()).unwrap())
        .await
        .unwrap();
    let db = Arc::new(FakeDb::default());
    let c = Consumer::new(
        test_config(&stream),
        Shared(db.clone()),
        r.clone(),
        FlagGate::fixed(true),
        Metrics::new(),
        Ready::new(),
    );
    let who = user(42);
    let ok: Vec<(&str, String)> = vec![
        ("v", "1".into()),
        ("tenant_id", "1".into()),
        ("community_id", "10".into()),
        ("user_uuid", who.to_string()),
        ("platform_code", "2".into()),
        ("kind_code", "1".into()),
        ("ts_ms", now_ms().to_string()),
        ("provenance_code", "2".into()),
    ];
    let _: String = r.xadd(&stream, "*", &ok).await.unwrap();
    let mut bad = ok.clone();
    bad[0].1 = "7".into();
    let _: String = r.xadd(&stream, "*", &bad).await.unwrap();
    assert_eq!(c.cycle().await.unwrap(), 2);
    let counts = tel.counts();
    println!("telemetry counts: {counts:?}");
    assert!(counts.log_records >= 1, "{counts:?}");
    assert!(counts.metric_data_points >= 1, "{counts:?}");
    assert!(counts.histogram_metrics >= 1, "{counts:?}");
    assert!(counts.spans >= 1, "{counts:?}");
    let logs = format!("{:?}", tel.logs.get_emitted_logs().unwrap());
    assert!(
        !logs.contains(&who.to_string()),
        "user token must never be logged"
    );
}
