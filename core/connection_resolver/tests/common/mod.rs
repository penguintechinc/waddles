//! Shared real-path fixtures: a real in-process tonic `ProviderConnectionService` server on
//! loopback (optionally with real TLS) and a fake gRPC mint (`MockHub`). The resolver under
//! test talks to both over real sockets -- nothing between it and the wire is mocked.

#![allow(dead_code)] // each test binary uses a different subset

use std::sync::Mutex;
use std::time::Duration;

pub use connection_resolver::test_support::*;
use connection_resolver::{ConnectionKind, ResolverConfig, ResolverMetrics};
use service_auth::test_support::{MintReply, MockHub};

/// A real in-process gRPC `ServiceAuthService` (the machine-JWT mint): answers
/// `tok-{community}-{connection}` for a bound request and `list-tok` for the unbound list
/// scope; counts mints.
pub async fn token_server() -> MockHub {
    MockHub::builder()
        .mint(vec![MintReply::With(std::sync::Arc::new(|call| {
            let token = match (call.community_id, call.connection_id) {
                (Some(c), Some(n)) => format!("tok-{c}-{n}"),
                _ => "list-tok".to_string(),
            };
            Ok((token, 3600))
        }))])
        .start()
        .await
}

/// Resolver config for `endpoint` with a throwaway SA token file and the fake mint.
pub fn cfg(endpoint: &str, tokens: &MockHub) -> ResolverConfig {
    let port = tokens.endpoint.rsplit(':').next().unwrap_or("0");
    let sa = std::env::temp_dir().join(format!("conn-resolver-sa-{}-{port}", std::process::id(),));
    std::fs::write(&sa, "projected-sa-token").unwrap();
    let mut c = ResolverConfig::new(
        endpoint,
        "alpha",
        tokens.endpoint.clone(),
        sa.to_string_lossy().to_string(),
    );
    c.request_timeout = Duration::from_millis(500);
    c.breaker_threshold = 1000;
    c
}

/// Count mints the fake mint served.
pub async fn mints(tokens: &MockHub) -> usize {
    tokens.mint_hits()
}

/// Metrics sink recording every call as `kind:outcome` strings.
#[derive(Default)]
pub struct Rec {
    pub calls: Mutex<Vec<String>>,
}

impl ResolverMetrics for Rec {
    fn resolve(&self, kind: ConnectionKind, outcome: &'static str, _l: Duration) {
        self.calls
            .lock()
            .unwrap()
            .push(format!("resolve:{}:{outcome}", kind.code()));
    }
    fn resolve_failed(&self, outcome: &'static str, _l: Duration) {
        self.calls
            .lock()
            .unwrap()
            .push(format!("resolve_failed:{outcome}"));
    }
    fn list(&self, outcome: &'static str) {
        self.calls.lock().unwrap().push(format!("list:{outcome}"));
    }
}
