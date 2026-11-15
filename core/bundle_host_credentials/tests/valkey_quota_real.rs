//! Real-path test of the Valkey quota store. Requires `VALKEY_URL`; unset FAILS
//! (no silent skip).

use std::sync::Arc;

use bundle_host_credentials::ValkeyQuotaStore;
use bundle_host_http::credentials::{QuotaGuard, QuotaPolicy, QuotaStore};
use std::collections::HashMap;

async fn store() -> ValkeyQuotaStore {
    let url = std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: quota tests need a REAL Valkey (no silent skip)");
    ValkeyQuotaStore::lazy(&url).expect("valkey url")
}

fn unique(tag: &str) -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("test.{tag}.{nanos}")
}

#[tokio::test]
async fn global_day_budget_is_atomic_under_concurrency() {
    let s: Arc<dyn QuotaStore> = Arc::new(store().await);
    let r = unique("conc");
    let g = Arc::new(
        QuotaGuard::new(
            HashMap::from([(
                r.clone(),
                QuotaPolicy {
                    per_day: Some(10),
                    ..Default::default()
                },
            )]),
            s,
        )
        .unwrap(),
    );
    let mut tasks = Vec::new();
    for i in 0..40 {
        let g = Arc::clone(&g);
        let r = r.clone();
        tasks.push(tokio::spawn(
            async move { g.charge(&r, i % 4).await.is_ok() },
        ));
    }
    let mut allowed = 0;
    for t in tasks {
        if t.await.unwrap() {
            allowed += 1;
        }
    }
    assert_eq!(allowed, 10, "exactly the budget is granted, no more");
}

#[tokio::test]
async fn community_share_and_refusal_does_not_consume_global() {
    let s: Arc<dyn QuotaStore> = Arc::new(store().await);
    let r = unique("share");
    let g = QuotaGuard::new(
        HashMap::from([(
            r.clone(),
            QuotaPolicy {
                per_day: Some(10),
                community_day_fraction: Some(0.2),
                ..Default::default()
            },
        )]),
        s,
    )
    .unwrap();
    g.charge(&r, 1).await.unwrap();
    g.charge(&r, 1).await.unwrap();
    for _ in 0..5 {
        // Refused for community 1; must NOT burn the global counter.
        assert_eq!(g.charge(&r, 1).await.unwrap_err().code, "quota_exhausted");
    }
    // Global has 8 left: community 2 and 3 can take 2 each (their share).
    for c in [2, 3] {
        g.charge(&r, c).await.unwrap();
        g.charge(&r, c).await.unwrap();
    }
    for c in [4, 5] {
        g.charge(&r, c).await.unwrap();
        g.charge(&r, c).await.unwrap();
    }
    assert_eq!(g.charge(&r, 6).await.unwrap_err().code, "quota_exhausted");
}
