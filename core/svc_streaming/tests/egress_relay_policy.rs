//! Integration coverage for `RelayPolicy` destination-count enforcement as
//! exercised through `RelaySink::start` (`OutputSink`), plus a direct unit
//! check of the bitrate cap -- the design-doc FREE_LIMITS cap#3 (3
//! destinations / 6000 kbps) from `docs/plans/2026-08-31-svc-streaming-
//! design.md` §2.

use svc_streaming::egress::relay::{RelayPolicy, RelayPolicyError, RelaySink};
use svc_streaming::egress::{OutputSink, SinkError};
use svc_streaming::pipeline::model::OutputSpec;
use svc_streaming::store::SecretRef;
use uuid::Uuid;

struct EnvVarGuard(&'static str);

impl EnvVarGuard {
    fn set(name: &'static str, value: &str) -> Self {
        // SAFETY: each test in this file uses a distinct env var name.
        unsafe { std::env::set_var(name, value) };
        Self(name)
    }
}

impl Drop for EnvVarGuard {
    fn drop(&mut self) {
        // SAFETY: see `set`.
        unsafe { std::env::remove_var(self.0) };
    }
}

fn rtmp_spec(var: &str) -> OutputSpec {
    OutputSpec::RtmpPush {
        url_secret_ref: SecretRef::Env {
            var: var.to_string(),
        },
        profile: None,
    }
}

#[tokio::test]
async fn free_tier_pipeline_accepts_exactly_three_destinations() {
    let guards: Vec<EnvVarGuard> = [
        (
            "SVC_STREAMING_RELAY_POLICY_A",
            "rtmp://a.example.com/app/k1",
        ),
        (
            "SVC_STREAMING_RELAY_POLICY_B",
            "rtmp://b.example.com/app/k2",
        ),
        (
            "SVC_STREAMING_RELAY_POLICY_C",
            "rtmp://c.example.com/app/k3",
        ),
    ]
    .iter()
    .map(|(k, v)| EnvVarGuard::set(k, v))
    .collect();

    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    for var in [
        "SVC_STREAMING_RELAY_POLICY_A",
        "SVC_STREAMING_RELAY_POLICY_B",
        "SVC_STREAMING_RELAY_POLICY_C",
    ] {
        sink.start(pipeline_id, rtmp_spec(var))
            .await
            .expect("within free tier limit");
    }
    assert_eq!(sink.tee_slaves(pipeline_id).await.len(), 3);
    drop(guards);
}

#[tokio::test]
async fn free_tier_pipeline_rejects_a_fourth_destination_with_the_documented_message() {
    let guards: Vec<EnvVarGuard> = [
        (
            "SVC_STREAMING_RELAY_POLICY4_A",
            "rtmp://a.example.com/app/k1",
        ),
        (
            "SVC_STREAMING_RELAY_POLICY4_B",
            "rtmp://b.example.com/app/k2",
        ),
        (
            "SVC_STREAMING_RELAY_POLICY4_C",
            "rtmp://c.example.com/app/k3",
        ),
        (
            "SVC_STREAMING_RELAY_POLICY4_D",
            "rtmp://d.example.com/app/k4",
        ),
    ]
    .iter()
    .map(|(k, v)| EnvVarGuard::set(k, v))
    .collect();

    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    for var in [
        "SVC_STREAMING_RELAY_POLICY4_A",
        "SVC_STREAMING_RELAY_POLICY4_B",
        "SVC_STREAMING_RELAY_POLICY4_C",
    ] {
        sink.start(pipeline_id, rtmp_spec(var)).await.unwrap();
    }

    let err = sink
        .start(pipeline_id, rtmp_spec("SVC_STREAMING_RELAY_POLICY4_D"))
        .await
        .unwrap_err();
    match err {
        SinkError::Other(inner) => {
            assert_eq!(
                inner.to_string(),
                "relay limit: max 3 destinations on this tier"
            );
        }
        other => panic!("expected SinkError::Other, got {other:?}"),
    }
    // Rejected destination must not have been added -- still exactly 3.
    assert_eq!(sink.tee_slaves(pipeline_id).await.len(), 3);
    drop(guards);
}

#[tokio::test]
async fn professional_policy_pipeline_accepts_a_fourth_destination() {
    let guards: Vec<EnvVarGuard> = [
        ("SVC_STREAMING_RELAY_PRO_A", "rtmp://a.example.com/app/k1"),
        ("SVC_STREAMING_RELAY_PRO_B", "rtmp://b.example.com/app/k2"),
        ("SVC_STREAMING_RELAY_PRO_C", "rtmp://c.example.com/app/k3"),
        ("SVC_STREAMING_RELAY_PRO_D", "rtmp://d.example.com/app/k4"),
    ]
    .iter()
    .map(|(k, v)| EnvVarGuard::set(k, v))
    .collect();

    let sink: RelaySink = RelaySink::with_policy(RelayPolicy::PROFESSIONAL);
    let pipeline_id = Uuid::new_v4();
    for var in [
        "SVC_STREAMING_RELAY_PRO_A",
        "SVC_STREAMING_RELAY_PRO_B",
        "SVC_STREAMING_RELAY_PRO_C",
        "SVC_STREAMING_RELAY_PRO_D",
    ] {
        sink.start(pipeline_id, rtmp_spec(var))
            .await
            .expect("within professional limit");
    }
    assert_eq!(sink.tee_slaves(pipeline_id).await.len(), 4);
    drop(guards);
}

#[tokio::test]
async fn separate_pipelines_have_independent_destination_counts() {
    let guards: Vec<EnvVarGuard> = [
        ("SVC_STREAMING_RELAY_ISO_A", "rtmp://a.example.com/app/k1"),
        ("SVC_STREAMING_RELAY_ISO_B", "rtmp://b.example.com/app/k2"),
        ("SVC_STREAMING_RELAY_ISO_C", "rtmp://c.example.com/app/k3"),
    ]
    .iter()
    .map(|(k, v)| EnvVarGuard::set(k, v))
    .collect();

    let sink: RelaySink = RelaySink::new();
    let pipeline_a = Uuid::new_v4();
    let pipeline_b = Uuid::new_v4();

    for var in [
        "SVC_STREAMING_RELAY_ISO_A",
        "SVC_STREAMING_RELAY_ISO_B",
        "SVC_STREAMING_RELAY_ISO_C",
    ] {
        sink.start(pipeline_a, rtmp_spec(var)).await.unwrap();
    }
    // A fresh pipeline is unaffected by pipeline_a already being at the cap.
    sink.start(pipeline_b, rtmp_spec("SVC_STREAMING_RELAY_ISO_A"))
        .await
        .expect("a different pipeline starts fresh at zero destinations");

    assert_eq!(sink.tee_slaves(pipeline_a).await.len(), 3);
    assert_eq!(sink.tee_slaves(pipeline_b).await.len(), 1);
    drop(guards);
}

#[test]
fn bitrate_cap_is_enforced_independently_of_destination_count() {
    let policy = RelayPolicy::FREE;
    assert!(policy.check_bitrate_kbps(6000).is_ok());
    let err = policy.check_bitrate_kbps(6001).unwrap_err();
    assert!(matches!(
        err,
        RelayPolicyError::BitrateExceeded {
            requested_kbps: 6001,
            max_kbps: 6000
        }
    ));
}
