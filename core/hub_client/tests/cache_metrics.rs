//! The stampede-protection counters through the REAL global meter provider:
//! `waddles_hub_dek_cache_total{result=hit|miss|inflight_wait|negative}` (stream-DEK cache,
//! `hub_client::dek`) and `waddles_hub_service_jwt_total{result=reuse|refresh|refresh_failed}`
//! (machine-JWT client, `service_auth`).
//!
//! One test function on purpose: the global provider is process-wide, may only be set once,
//! and both instruments bind to it on first use.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use hub_client::dek::{
    BrokeredDek, CachedDekProvider, Dek, DekBroker, DekProvider, DekUnavailableError,
};
use service_auth::test_support::{Capture, MintReply, MockHub};
use service_auth::MachineJwtClient;
use zeroize::Zeroizing;

/// A broker that counts calls, answers after a delay, and fails for tenant 3.
struct SlowBroker {
    calls: AtomicU32,
}

impl DekBroker for SlowBroker {
    async fn fetch(
        &self,
        tenant_id: i64,
        version: u32,
    ) -> Result<BrokeredDek, DekUnavailableError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        tokio::time::sleep(Duration::from_millis(250)).await;
        if tenant_id == 3 {
            return Err(DekUnavailableError {
                tenant_id,
                dek_version: (version != 0).then_some(version),
                reason: "scripted broker failure".to_string(),
            });
        }
        let dek: Dek = Zeroizing::new([7u8; 32]);
        Ok(BrokeredDek {
            dek,
            version: 1,
            ttl: Duration::from_secs(600),
        })
    }
}

/// Sum of the counter's data points per `result` label.
fn by_result(capture: &Capture, name: &str) -> BTreeMap<String, u64> {
    let mut out = BTreeMap::new();
    for point in capture.points(name) {
        let result = point.attrs.get("result").cloned().unwrap_or_default();
        *out.entry(result).or_insert(0) += point.value;
    }
    out
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn dek_cache_and_service_jwt_counters_carry_the_documented_results() {
    let capture = Capture::new();
    capture.install_global();

    // --- stream-DEK cache -------------------------------------------------------------
    let provider = CachedDekProvider::new(
        SlowBroker {
            calls: AtomicU32::new(0),
        },
        Duration::from_secs(600),
    )
    .with_negative_backoff(Duration::from_secs(30));

    // 40 concurrent cold callers of tenant 2: one miss (the leader), the rest wait or hit.
    let mut set = tokio::task::JoinSet::new();
    for _ in 0..40 {
        let p = provider.clone();
        set.spawn(async move { p.get_dek_version(2, 1).await.map(|_| ()) });
    }
    while let Some(joined) = set.join_next().await {
        joined.unwrap().unwrap();
    }
    // A failing tenant: one miss, then the shared failure is `negative`.
    assert!(provider.get_dek_version(3, 1).await.is_err());
    assert!(provider.get_dek_version(3, 1).await.is_err());
    assert!(provider.get_dek_version(3, 1).await.is_err());

    let dek = by_result(&capture, "waddles_hub_dek_cache");
    assert_eq!(
        dek.get("miss"),
        Some(&2),
        "tenant 2 leader + tenant 3 leader: {dek:?}"
    );
    assert!(
        dek.get("inflight_wait").copied().unwrap_or(0) >= 1,
        "40 overlapping callers must record waiters: {dek:?}"
    );
    assert_eq!(
        dek.get("inflight_wait").copied().unwrap_or(0) + dek.get("hit").copied().unwrap_or(0),
        39,
        "every non-leader of tenant 2 is a hit or an in-flight wait: {dek:?}"
    );
    assert_eq!(dek.get("negative"), Some(&2), "{dek:?}");

    // --- machine-JWT client -----------------------------------------------------------
    let hub = MockHub::builder()
        .mint(vec![MintReply::token("tok", 900)])
        .start()
        .await;
    let sa = std::env::temp_dir().join(format!("hub-client-metrics-sa-{}", std::process::id()));
    std::fs::write(&sa, "sa").unwrap();
    let client = Arc::new(MachineJwtClient::new(
        hub.endpoint.clone(),
        sa.to_string_lossy().to_string(),
        "key:stream-dek:read",
    ));
    for _ in 0..4 {
        client.get_token().await.unwrap();
    }
    let jwt = by_result(&capture, "waddles_hub_service_jwt");
    assert_eq!(jwt.get("refresh"), Some(&1), "{jwt:?}");
    assert_eq!(jwt.get("reuse"), Some(&3), "{jwt:?}");
    let scopes: Vec<String> = capture
        .points("waddles_hub_service_jwt")
        .iter()
        .filter_map(|p| p.attrs.get("scope").cloned())
        .collect();
    assert!(
        scopes.iter().all(|s| s == "key:stream-dek:read"),
        "{scopes:?}"
    );
    assert_eq!(hub.mint_hits(), 1);
}
