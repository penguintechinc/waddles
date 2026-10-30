//! Integration test (own process): drives the REAL telemetry pipeline
//! (`telemetry::init` -> `penguin-logging`'s global meter provider with its
//! Prometheus reader -> the `/metrics` registry) and proves the stage's OTel
//! instruments and the `metrics`-facade bridge actually surface on the scrape
//! surface under the exact names dashboards query.
//!
//! Lives in its own file because `penguin_logging::init` installs
//! process-global state (tracing subscriber, meter provider) that only one
//! call per process may claim -- every file under `tests/` is its own binary.

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};
use penguin_spine::SpineMetrics;
use svc_action::capabilities::{CapabilityHandler, InvokeScope, RelayQueue, StageCapabilities};
use svc_action::distribution::BundleCatalog;
use svc_action::egress::{EgressGuard, EgressLimits, ReqwestTransport};
use svc_action::stage_metrics::{self, METRIC_NAMES};
use svc_action::usage::UsageBatcher;

/// A relay queue that accepts nothing -- this test never reaches `relay`.
struct NoRelayQueue;

impl RelayQueue for NoRelayQueue {
    fn lpush<'a>(
        &'a self,
        _key: &'a str,
        _value: String,
    ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
        Box::pin(async { Err("no relay in this test".to_string()) })
    }
}

/// The value of the first scrape line starting with `prefix` and containing
/// every `needle` -- e.g. `sample(&text, "x_count{", &["result=\"ok\""])`.
fn sample(rendered: &str, prefix: &str, needles: &[&str]) -> Option<f64> {
    rendered
        .lines()
        .filter(|line| line.starts_with(prefix) && needles.iter().all(|n| line.contains(n)))
        .find_map(|line| line.rsplit(' ').next()?.parse::<f64>().ok())
}

fn scope() -> InvokeScope {
    InvokeScope {
        tenant: "acme".to_string(),
        community: Some("main".to_string()),
        app_id: "waddles.scrape.test".to_string(),
        origin_channel_id: None,
        tenant_id: 7,
        community_id: 3,
        app_version: 1,
        tool_chain: Vec::new(),
        deadline_at: None,
        trace: None,
    }
}

fn gate_granting_clock() -> Arc<CapabilityGate> {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: 7,
            community_id: 3,
            app_id: "waddles.scrape.test".to_string(),
            app_version: 1,
        },
        GrantSet {
            permission_snapshot_hash: "test".to_string(),
            grants: std::collections::HashMap::from([(
                "platform.clock".to_string(),
                GrantedPermission {
                    permission_id: "platform.clock".to_string(),
                    params: serde_json::json!({}),
                },
            )]),
        },
    );
    Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ))
}

fn egress() -> Arc<EgressGuard> {
    Arc::new(EgressGuard::new(
        Arc::new(ReqwestTransport::new()),
        EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        Arc::new(BundleCatalog::new()),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("scrape_test_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition"),
        svc_action::flags::boxed(svc_action::flags::StaticFlag(true)),
    ))
}

#[tokio::test]
async fn stage_metrics_and_gate_counters_surface_on_the_prometheus_scrape() {
    // SAFETY: the only test in this binary/process; no OTLP endpoint so the
    // pipeline is Prometheus-only (the reader `penguin-logging` always attaches).
    unsafe { std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT") };
    let (mut guard, registry) = svc_action::telemetry::init("svc-action-scrape-test");

    // The production wiring order: instruments strictly after init, facade
    // recorder installed once.
    let metrics = stage_metrics::stage_metrics();
    assert!(
        svc_action::metrics_facade::install(),
        "the facade recorder must be installable in a fresh process"
    );

    // Drive every instrument through its real recording API.
    metrics.record_stage("waddles.scrape.test", "success", Duration::from_millis(40));
    metrics.record_executor_call(
        "waddles.scrape.test",
        "dispatch",
        "ok",
        Duration::from_millis(3),
    );
    metrics.record_host_call("kv", "get", "ok", Duration::from_micros(300));
    metrics.record_tool_call(
        "waddles.e2e.svc.one",
        "echo",
        "ok",
        Duration::from_micros(500),
    );
    metrics.record_bundle_load("waddles.scrape.test", "load", "ok", Duration::from_secs(1));
    metrics.record_attempts("waddles.scrape.test", "success", 1);
    metrics.record_batch(7);
    metrics.record_sandbox_trip("waddles.scrape.test", "timeout");
    metrics.stream_event_written("twitch", "tw-some-channel");
    metrics.stream_trimmed("waddles:t:acme:c:main:app:x:action");
    metrics.stream_claimed("waddles.scrape.test");
    metrics.consumer_skipped("waddles.scrape.test", "filter");
    metrics.dlq_written("action", "bundle_error");
    metrics.group_lag("waddles.scrape.test", "s", Some(3));
    metrics.group_pending("waddles.scrape.test", "s", 2);
    metrics.insecure_transport("valkey", "tls", false);
    metrics.tenant_boundary_violation("action", "mac_mismatch");

    // And the real capability gate, through the real host-call handler: one
    // authorized call (clock), one denied (http to an ungranted host).
    let caps = StageCapabilities::<NoRelayQueue>::new(
        NoRelayQueue,
        egress(),
        Arc::new(Mutex::new(UsageBatcher::new())),
        gate_granting_clock(),
    );
    let call = |capability, op: &str, args| HostCallBody {
        app_id: "waddles.scrape.test".to_string(),
        capability,
        op: op.to_string(),
        args,
        call_id: 1,
    };
    caps.handle(
        &scope(),
        call(CapabilityKind::Clock, "now-millis", serde_json::json!({})),
    )
    .await
    .expect("clock is granted");
    caps.handle(
        &scope(),
        call(
            CapabilityKind::Http,
            "send",
            serde_json::json!({"url": "https://hostile-1.example/x"}),
        ),
    )
    .await
    .expect_err("ungranted http is denied");

    let text = svc_action::telemetry::render_metrics(&registry).expect("registry must encode");

    // The Prometheus exporter appends `_total`/`_seconds` itself: instruments
    // registered with those suffixes would scrape as `x_total_total` /
    // `x_seconds_seconds`, which no dashboard queries.
    for doubled in ["_total_total", "_seconds_seconds"] {
        assert!(
            !text.contains(doubled),
            "double-suffixed metric name ({doubled}) in the scrape:\n{text}"
        );
    }

    // Histograms: second-scaled boundaries, count/sum/bucket families.
    let latency = "waddles_stage_latency_seconds";
    assert_eq!(
        sample(
            &text,
            &format!("{latency}_count{{"),
            &[
                "app_id=\"waddles.scrape.test\"",
                "result=\"success\"",
                "stage=\"action\""
            ]
        ),
        Some(1.0),
        "stage latency count missing from scrape:\n{text}"
    );
    assert!(
        text.contains(&format!("{latency}_bucket{{")) && text.contains("le=\"0.05\""),
        "stage latency must expose second-scale buckets"
    );
    assert!(sample(&text, &format!("{latency}_sum{{"), &["result=\"success\""]).is_some());
    assert_eq!(
        sample(
            &text,
            "waddles_executor_call_seconds_count{",
            &["export=\"dispatch\"", "result=\"ok\""]
        ),
        Some(1.0)
    );
    assert_eq!(
        sample(
            &text,
            "waddles_host_call_latency_seconds_count{",
            &["capability=\"clock\"", "op=\"now-millis\"", "result=\"ok\""]
        ),
        Some(1.0),
        "the real handler must have timed the clock call"
    );
    assert_eq!(
        sample(
            &text,
            "waddles_host_call_latency_seconds_count{",
            &["capability=\"http\"", "op=\"send\"", "result=\"denied\""]
        ),
        Some(1.0),
        "the gate-denied http call must be labeled denied"
    );
    assert_eq!(
        sample(
            &text,
            "waddles_action_dispatch_attempts_count{",
            &["result=\"success\""]
        ),
        Some(1.0)
    );
    assert_eq!(
        sample(
            &text,
            "waddles_spine_batch_size_count{",
            &["stage=\"action\""]
        ),
        Some(1.0)
    );

    // Counters and gauges.
    assert_eq!(
        sample(
            &text,
            "waddles_sandbox_trip_total{",
            &["limit=\"timeout\"", "app_id=\"waddles.scrape.test\""]
        ),
        Some(1.0)
    );
    assert_eq!(
        sample(
            &text,
            "waddles_spine_dlq_total{",
            &["stage=\"action\"", "reason=\"bundle_error\""]
        ),
        Some(1.0)
    );
    assert_eq!(
        sample(
            &text,
            "waddles_insecure_transport{",
            &["component=\"valkey\"", "aspect=\"tls\""]
        ),
        Some(0.0),
        "a secure transport must scrape as an explicit 0"
    );

    // The capability gate's own audit counters: previously dropped by the
    // `metrics` facade's no-op recorder, now scraped. The denied host is
    // folded to its family -- never a per-host series.
    assert!(
        sample(
            &text,
            "waddles_bundle_capability_authorized_total{",
            &[
                "app_id=\"waddles.scrape.test\"",
                "permission=\"platform.clock\""
            ]
        )
        .is_some_and(|v| v >= 1.0),
        "gate authorized counter missing from scrape:\n{text}"
    );
    assert_eq!(
        sample(
            &text,
            "waddles_bundle_capability_denied_total{",
            &[
                "app_id=\"waddles.scrape.test\"",
                "permission=\"net.http.fqdn\"",
                "reason=\"not_granted\""
            ]
        ),
        Some(1.0),
        "gate denied counter missing from scrape:\n{text}"
    );
    assert!(
        !text.contains("hostile-1.example"),
        "a destination host must never reach a label"
    );

    // PII/cardinality guard on the whole scrape.
    for forbidden in ["tw-some-channel", "waddles:t:acme"] {
        assert!(!text.contains(forbidden), "{forbidden} leaked into labels");
    }

    // Every documented StageMetrics name is present (denominator printed).
    let missing: Vec<&str> = METRIC_NAMES
        .iter()
        .copied()
        .filter(|name| !text.contains(name))
        .collect();
    assert!(
        missing.is_empty(),
        "documented metrics absent from the scrape: {missing:?} (examined {} names)",
        METRIC_NAMES.len()
    );
    guard.shutdown();
}
