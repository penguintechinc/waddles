//! Shared real-path fixtures: a real in-process tonic `ProviderConnectionService` server on
//! loopback (optionally with real TLS) and a fake machine-token endpoint. The resolver under
//! test talks to both over real sockets -- nothing between it and the wire is mocked.

#![allow(dead_code)] // each test binary uses a different subset

use std::sync::Mutex;
use std::time::Duration;

pub use connection_resolver::test_support::*;
use connection_resolver::{ConnectionKind, ResolverConfig, ResolverMetrics};
use wiremock::{Mock, MockServer, Request as WireRequest, Respond, ResponseTemplate};

/// Answers the machine-token mint: `tok-{community}-{connection}` for a bound request,
/// `list-tok` for the unbound list scope; counts mints.
struct Mint;

impl Respond for Mint {
    fn respond(&self, request: &WireRequest) -> ResponseTemplate {
        let body: serde_json::Value = serde_json::from_slice(&request.body).unwrap_or_default();
        let token = match (body.get("community_id"), body.get("connection_id")) {
            (Some(c), Some(n)) => format!("tok-{c}-{n}"),
            _ => "list-tok".to_string(),
        };
        ResponseTemplate::new(200)
            .set_body_json(serde_json::json!({"token": token, "expires_in": 3600}))
    }
}

/// A fake `/internal/service-token` endpoint.
pub async fn token_server() -> MockServer {
    let server = MockServer::start().await;
    Mock::given(wiremock::matchers::method("POST"))
        .and(wiremock::matchers::path("/internal/service-token"))
        .respond_with(Mint)
        .mount(&server)
        .await;
    server
}

/// Resolver config for `endpoint` with a throwaway SA token file and the fake token server.
pub fn cfg(endpoint: &str, tokens: &MockServer) -> ResolverConfig {
    let sa = std::env::temp_dir().join(format!(
        "conn-resolver-sa-{}-{}",
        std::process::id(),
        tokens.address().port()
    ));
    std::fs::write(&sa, "projected-sa-token").unwrap();
    let mut c = ResolverConfig::new(
        endpoint,
        "alpha",
        format!("{}/internal/service-token", tokens.uri()),
        sa.to_string_lossy().to_string(),
    );
    c.request_timeout = Duration::from_millis(500);
    c.breaker_threshold = 1000;
    c
}

/// Count mints the fake token endpoint served.
pub async fn mints(tokens: &MockServer) -> usize {
    tokens.received_requests().await.unwrap().len()
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
