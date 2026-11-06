//! Real-path test for the bundle `media` capability stage side: the REAL
//! capability gate (grant, quota), the REAL `StageCapabilities::handle` dispatch
//! (`media.*` ops), the REAL `bundle_host_media::MediaHost`, the REAL Valkey job
//! store and a REAL SeaweedFS bucket; only the Gemini API is faked (an in-process
//! server speaking the documented Interactions shapes). The wire args used are
//! exactly the ones `core/bundle_executor/tests/stage_next_media.rs` proves a
//! compiled guest emits through the real executor.
#![allow(clippy::unwrap_used, clippy::expect_used)]

#[path = "../../bundle_host_media/tests/common/mod.rs"]
mod common;
#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;

use std::collections::HashMap;
use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_media::host::{MediaConfig, MediaHost, SystemClock};
use bundle_host_media::jobs::{InMemoryJobStore, JobStore, ValkeyJobStore};
use bundle_host_media::store::{InMemoryStore, MediaStore, ObjectStoreMediaStore};
use bundle_host_media::Tier;
use common::{fake_gemini, provider, FixedCreds, FixedTier, GOOD_KEY};
use docker_container::start_container;
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};
use svc_process::capabilities::{CapabilityHandler, HttpEgressCatalog, StageCapabilities};
use svc_process::license::FeatureGate;
use svc_process::media::MediaWiring;

struct Flag(bool);
impl FeatureGate for Flag {
    fn enabled<'a>(
        &'a self,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = bool> + Send + 'a>> {
        let v = self.0;
        Box::pin(async move { v })
    }
}

const TENANT: i32 = 7;
const COMMUNITY: i32 = 42;

fn caps(
    app: &str,
    granted: bool,
    community: i32,
    wiring: Option<MediaWiring>,
) -> StageCapabilities {
    let snapshot = InMemoryGrantSnapshot::new();
    if granted {
        snapshot.set(
            GrantScopeKey {
                tenant_id: TENANT,
                community_id: community,
                app_id: app.to_string(),
                app_version: 1,
            },
            GrantSet {
                permission_snapshot_hash: "t".to_string(),
                grants: HashMap::from([(
                    "media.generate".to_string(),
                    GrantedPermission {
                        permission_id: "media.generate".to_string(),
                        params: serde_json::json!({}),
                    },
                )]),
            },
        );
    }
    let gate = Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ));
    let egress = Arc::new(bundle_host_http::egress::EgressGuard::new(
        Arc::new(bundle_host_http::egress::ReqwestTransport::new()),
        bundle_host_http::egress::EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        HttpEgressCatalog::new(),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("media_e2e_egress_denied_total", "t"),
            &["app_id", "reason"],
        )
        .unwrap(),
        bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(true)),
    ));
    let c = StageCapabilities::<redis::aio::MultiplexedConnection>::new(
        "acme".into(),
        Some("main".into()),
        app.to_string(),
        TENANT,
        community,
        1,
        egress,
        gate,
    );
    match wiring {
        Some(w) => c.with_media(w),
        None => c,
    }
}

fn call(app: &str, op: &str, args: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: app.to_string(),
        capability: CapabilityKind::Db,
        op: op.to_string(),
        args,
        call_id: 1,
    }
}

struct Backends {
    store: Arc<dyn MediaStore>,
    jobs: Arc<dyn JobStore>,
}

fn wiring(
    b: &Backends,
    base: String,
    tier: Tier,
    key: Option<&'static str>,
    flag: bool,
) -> MediaWiring {
    let host = MediaHost::new(
        Arc::new(FixedTier(Ok(tier))),
        Arc::new(FixedCreds(key)),
        Arc::new(provider(base, Duration::from_secs(10))),
        b.store.clone(),
        b.jobs.clone(),
        Arc::new(SystemClock),
        MediaConfig {
            poll_interval: Duration::from_millis(20),
            ..MediaConfig::default()
        },
    );
    MediaWiring {
        host: Arc::new(host),
        flag: Arc::new(Flag(flag)),
    }
}

fn memory() -> Backends {
    Backends {
        store: Arc::new(InMemoryStore::default()),
        jobs: Arc::new(InMemoryJobStore::default()),
    }
}

#[tokio::test]
async fn refusal_order_gate_then_wiring_then_flag_then_scope_then_pii_then_tier_then_byok() {
    let (_f, base) = fake_gemini().await;
    let b = memory();
    let args = serde_json::json!({"prompt": "a cat"});
    let op = "media.generate_image";
    // Not granted -> gate denial first, even with nothing wired.
    let e = caps("a", false, COMMUNITY, None)
        .handle(call("a", op, args.clone()))
        .await
        .unwrap_err();
    assert_eq!(e.code, "not_granted");
    // Granted but unwired -> not_implemented (fail loud).
    let e = caps("a", true, COMMUNITY, None)
        .handle(call("a", op, args.clone()))
        .await
        .unwrap_err();
    assert_eq!(e.code, "not_implemented");
    // Flag OFF.
    let e = caps(
        "a",
        true,
        COMMUNITY,
        Some(wiring(
            &b,
            base.clone(),
            Tier::Enterprise,
            Some(GOOD_KEY),
            false,
        )),
    )
    .handle(call("a", op, args.clone()))
    .await
    .unwrap_err();
    assert_eq!(e.code, "feature_disabled");
    // Tenant-wide activation (community 0) is refused: tier + BYOK are per community.
    let e = caps(
        "a",
        true,
        0,
        Some(wiring(
            &b,
            base.clone(),
            Tier::Enterprise,
            Some(GOOD_KEY),
            true,
        )),
    )
    .handle(call("a", op, args.clone()))
    .await;
    assert_eq!(e.unwrap_err().code, "invalid_args");
    // PII in the prompt is rejected before the host/provider.
    let on = || {
        caps(
            "a",
            true,
            COMMUNITY,
            Some(wiring(
                &b,
                base.clone(),
                Tier::Professional,
                Some(GOOD_KEY),
                true,
            )),
        )
    };
    let e = on()
        .handle(call(
            "a",
            op,
            serde_json::json!({"prompt": "mail dave@example.com"}),
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code, "contains_pii");
    // Unknown argument keys (a guest naming a model / key / url) are refused.
    for extra in ["model", "api_key", "url", "provider", "tenant_id"] {
        let e = on()
            .handle(call(
                "a",
                op,
                serde_json::json!({"prompt": "x", extra: "y"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(e.code, "invalid_args", "{extra}");
    }
    let e = on()
        .handle(call("a", "media.bogus", args.clone()))
        .await
        .unwrap_err();
    assert_eq!(e.code, "unknown_op");
    // Tier: free refused with the professional requirement; unknown license refused.
    let e = caps(
        "a",
        true,
        COMMUNITY,
        Some(wiring(&b, base.clone(), Tier::Free, Some(GOOD_KEY), true)),
    )
    .handle(call("a", op, args.clone()))
    .await
    .unwrap_err();
    assert_eq!(
        (e.code.as_str(), e.message.as_str()),
        ("tier_required", "professional")
    );
    // BYOK missing.
    let e = caps(
        "a",
        true,
        COMMUNITY,
        Some(wiring(&b, base.clone(), Tier::Professional, None, true)),
    )
    .handle(call("a", op, args.clone()))
    .await
    .unwrap_err();
    assert_eq!(
        (e.code.as_str(), e.message.as_str()),
        ("byok_missing", "gemini")
    );
    // The fake provider was never reached by any refusal above.
    assert!(_f.posts.lock().unwrap().is_empty());
}

#[tokio::test]
async fn gate_quota_stops_the_seventh_call_in_a_minute() {
    let (_f, base) = fake_gemini().await;
    let b = memory();
    let base2 = base.clone();
    let c = caps(
        "a",
        true,
        COMMUNITY,
        Some(wiring(&b, base, Tier::Professional, Some(GOOD_KEY), true)),
    );
    for _ in 0..6 {
        c.handle(call(
            "a",
            "media.generate_image",
            serde_json::json!({"prompt": "cat"}),
        ))
        .await
        .unwrap();
    }
    let e = c
        .handle(call(
            "a",
            "media.generate_image",
            serde_json::json!({"prompt": "cat"}),
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code, "rate_limited");
    // Polling and URL resolution never spend the generation budget.
    for _ in 0..30 {
        let e = c
            .handle(call(
                "a",
                "media.job_status",
                serde_json::json!({"job_id": "a".repeat(32)}),
            ))
            .await
            .unwrap_err();
        assert_eq!(e.code, "not_found");
    }
    // ...but still need the grant.
    let ungranted = caps(
        "a",
        false,
        COMMUNITY,
        Some(wiring(
            &b,
            base2.clone(),
            Tier::Professional,
            Some(GOOD_KEY),
            true,
        )),
    );
    let e = ungranted
        .handle(call(
            "a",
            "media.job_status",
            serde_json::json!({"job_id": "a".repeat(32)}),
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code, "not_granted");
}

fn seaweed_name() -> String {
    format!(
        "media-e2e-sw-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    )
}

struct Seaweed(String);
impl Drop for Seaweed {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "-f", "-v", &self.0])
            .output();
    }
}

#[tokio::test]
async fn image_and_video_job_end_to_end_on_valkey_and_seaweedfs_with_bundle_isolation() {
    let valkey = start_container(
        "valkey/valkey:8-alpine",
        6379,
        &[],
        &["valkey-cli", "ping"],
        Duration::from_secs(120),
    )
    .await;
    let conn = redis::Client::open(format!("redis://{}:{}/", valkey.host(), valkey.port()))
        .unwrap()
        .get_multiplexed_async_connection()
        .await
        .unwrap();
    let name = seaweed_name();
    let out = Command::new("docker")
        .args(["run", "-d", "--name", &name, "-p", "127.0.0.1::8333", "chrislusf/seaweedfs@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d", "server", "-s3", "-dir=/data", "-volume.max=2"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let _sw = Seaweed(name.clone());
    let port_out = Command::new("docker")
        .args(["port", &name, "8333/tcp"])
        .output()
        .unwrap();
    let port: u16 = String::from_utf8_lossy(&port_out.stdout)
        .lines()
        .find_map(|l| l.rsplit(':').next()?.trim().parse().ok())
        .unwrap();
    let http = reqwest::Client::new();
    let mut ready = false;
    for _ in 0..240 {
        if let Ok(r) = http
            .put(format!("http://127.0.0.1:{port}/media-bucket"))
            .send()
            .await
        {
            if r.status().is_success() || r.status().as_u16() == 409 {
                ready = true;
                break;
            }
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    assert!(ready, "seaweedfs ready");

    let (fake, base) = fake_gemini().await;
    fake.pending_polls
        .store(2, std::sync::atomic::Ordering::SeqCst);
    let b = Backends {
        store: Arc::new(
            ObjectStoreMediaStore::new(
                &format!("http://127.0.0.1:{port}"),
                "media-bucket",
                "us-east-1",
                "k",
                "s",
            )
            .unwrap(),
        ),
        jobs: Arc::new(ValkeyJobStore::new(conn)),
    };
    let w = wiring(&b, base, Tier::Enterprise, Some(GOOD_KEY), true);
    let owner = caps("app-owner", true, COMMUNITY, Some(w.clone()));
    let other = caps("app-other", true, COMMUNITY, Some(w));

    // Image (sync) -> handle -> presigned URL serving the real bytes.
    let img = owner
        .handle(call(
            "app-owner",
            "media.generate_image",
            serde_json::json!({"prompt": "a cat"}),
        ))
        .await
        .unwrap();
    let handle = img["handle"].as_str().unwrap().to_string();
    assert!(handle.starts_with("media://7/42/") && handle.ends_with(".png"));
    let url = owner
        .handle(call(
            "app-owner",
            "media.object_url",
            serde_json::json!({"handle": handle}),
        ))
        .await
        .unwrap()["url"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(
        reqwest::get(&url)
            .await
            .unwrap()
            .bytes()
            .await
            .unwrap()
            .to_vec(),
        common::png()
    );

    // Video job: submit -> poll pending -> done.
    let sub = owner
        .handle(call(
            "app-owner",
            "media.submit_video",
            serde_json::json!({"prompt": "a clip"}),
        ))
        .await
        .unwrap();
    let job = sub["job_id"].as_str().unwrap().to_string();
    let mut done = None;
    for _ in 0..300 {
        let st = owner
            .handle(call(
                "app-owner",
                "media.job_status",
                serde_json::json!({"job_id": job}),
            ))
            .await
            .unwrap();
        if st["state"] != "pending" {
            done = Some(st);
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let done = done.expect("job finished");
    assert_eq!(done["state"], "done");
    let vh = done["handle"].as_str().unwrap().to_string();
    let vurl = owner
        .handle(call(
            "app-owner",
            "media.object_url",
            serde_json::json!({"handle": vh}),
        ))
        .await
        .unwrap()["url"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(
        reqwest::get(&vurl)
            .await
            .unwrap()
            .bytes()
            .await
            .unwrap()
            .to_vec(),
        common::mp4()
    );

    // Another bundle, same community, media.generate GRANTED: cannot see the
    // job or resolve either handle -- only the tag authorizes.
    let e = other
        .handle(call(
            "app-other",
            "media.job_status",
            serde_json::json!({"job_id": job}),
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code, "not_found");
    for h in [&handle, &vh] {
        let e = other
            .handle(call(
                "app-other",
                "media.object_url",
                serde_json::json!({"handle": h}),
            ))
            .await
            .unwrap_err();
        assert_eq!(e.code, "not_found");
    }
}
