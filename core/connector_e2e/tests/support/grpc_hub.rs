//! A REAL in-process hub-api for the resolver-backed e2e tests.
//!
//! The per-send connection resolver reads hub-api over internal gRPC (`ProviderConnectionService`
//! for bindings/credentials, `ServiceAuthService` for the machine token), so the fakes here are
//! real tonic servers on loopback -- nothing between the production `ConnectionResolver` and the
//! wire is mocked. They are scripted from the same camelCase JSON views the retired REST routes
//! used, so the per-test fixtures read exactly as before.

#![allow(dead_code, clippy::unwrap_used, clippy::expect_used)]

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use std::time::Duration;

use connection_resolver::test_support::{json_hub, Hub, Running};
use connection_resolver::{ConnectionKind, ConnectionResolver, ResolverConfig, ResolverMetrics};
use service_auth::test_support::{MintReply, MockHub};

/// The resolver's metrics sink; these tests assert behavior, not instruments.
struct NoMetrics;

impl ResolverMetrics for NoMetrics {
    fn resolve(&self, _: ConnectionKind, _: &'static str, _: Duration) {}
    fn list(&self, _: &'static str) {}
}

/// The running fake hub plus the synthetic service-account token file the resolver reads
/// (removed on drop; one file per hub because tests run in parallel inside one process).
pub struct FakeHub {
    running: Option<Running>,
    /// Number of `ResolveConnection` calls that reached the hub.
    pub resolve_calls: Arc<AtomicUsize>,
    /// Number of `ListBindings` calls that reached the hub.
    pub list_calls: Arc<AtomicUsize>,
    sa: PathBuf,
}

impl FakeHub {
    /// `ResolveConnection` calls so far.
    pub fn resolves(&self) -> usize {
        self.resolve_calls.load(Ordering::SeqCst)
    }

    /// `ListBindings` calls so far.
    pub fn lists(&self) -> usize {
        self.list_calls.load(Ordering::SeqCst)
    }
}

impl Drop for FakeHub {
    fn drop(&mut self) {
        if let Err(e) = std::fs::remove_file(&self.sa) {
            eprintln!("synthetic sa token cleanup failed: {e}");
        }
        // The server task ends with the test's runtime; there is no async context to await in.
        drop(self.running.take());
    }
}

/// Starts a fake hub answering every list with `list` and every resolve with `resolve` (both in
/// the camelCase JSON view), and a production [`ConnectionResolver`] pointed at it. `tag` keeps
/// the SA token file unique per hub.
pub async fn start(
    list: &serde_json::Value,
    resolve: &serde_json::Value,
    tag: &str,
) -> (FakeHub, ConnectionResolver) {
    start_with(json_hub(list, resolve), tag).await
}

/// [`start`] over a caller-scripted [`Hub`] (e.g. a resolve reply that echoes the requested
/// community/connection, so a test can serve several communities).
pub async fn start_with(hub: Hub, tag: &str) -> (FakeHub, ConnectionResolver) {
    let resolve_calls = Arc::clone(&hub.resolve_calls);
    let list_calls = Arc::clone(&hub.list_calls);
    let running = connection_resolver::test_support::start(&hub).await;
    // The machine-token mint is gRPC too: a real in-process `ServiceAuthService`.
    let mint_endpoint = MockHub::builder()
        .mint(vec![MintReply::when(|_| true, "machine-tok", 3600)])
        .start_detached()
        .await;
    let sa = std::env::temp_dir().join(format!(
        "e2e-hub-sa-{}-{tag}-{}",
        std::process::id(),
        uuid_like()
    ));
    std::fs::write(&sa, "synthetic-sa").unwrap();
    let resolver = ConnectionResolver::new(
        ResolverConfig::new(
            running.endpoint.clone(),
            "alpha",
            mint_endpoint,
            sa.to_string_lossy().to_string(),
        ),
        Arc::new(NoMetrics),
    )
    .unwrap();
    (
        FakeHub {
            running: Some(running),
            resolve_calls,
            list_calls,
            sa,
        },
        resolver,
    )
}

/// Process-unique suffix (a counter, no PII, no randomness needed).
fn uuid_like() -> usize {
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    NEXT.fetch_add(1, Ordering::SeqCst)
}
