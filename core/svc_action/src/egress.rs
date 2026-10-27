//! Thin re-export shim over the shared `bundle_host_http` crate
//! (`core/bundle_host_http`) -- the full `EgressGuard`/SSRF/DNS-pinning/
//! redirect-recheck/rate-limit pipeline (connector spec §8.2) that used to
//! live entirely in this module was extracted there (PR #459 follow-up)
//! so `core/svc_process` can enforce the identical policy for its own
//! `http` bundle capability without a second ~2000-line copy. **Zero
//! behavior change for this crate**: every type this crate's other
//! modules (`crate::capabilities`, `crate::host_api`, `crate::lib`,
//! `crate::senders`) reference via `crate::egress::*` is re-exported here
//! unchanged, and [`crate::distribution::BundleCatalog`] implements the
//! shared crate's [`bundle_host_http::egress::EgressRuleSource`] trait
//! below so it plugs into [`EgressGuard::new`] exactly as it did before
//! this extraction (Rust's unsized-coercion at the call site turns
//! `Arc<BundleCatalog>` into `Arc<dyn EgressRuleSource>` with no cast
//! needed).
//!
//! The shared crate's own test suite (moved there in full) covers every
//! generic guard/SSRF/redirect/secret-substitution property; this module
//! keeps only the one test that is genuinely specific to this crate's own
//! wiring (`crate::senders`/`crate::distribution::BundleCatalog`).

pub use bundle_host_http::egress::{
    boxed, is_forbidden_address, ClusterCidrDenylist, CredentialBroker, EgressGuard, EgressLimits,
    EgressRuleRow, EgressRuleSource, EnvCredentialBroker, FeatureFlag, HttpTransport,
    InstanceEgressPolicy, ReqwestTransport, SecretHandle, TransportRequest, TransportResponse,
};

impl EgressRuleSource for crate::distribution::BundleCatalog {
    fn resolve(&self, app_id: &str) -> Option<EgressRuleRow> {
        self.get(app_id).map(|row| {
            EgressRuleRow::from_legacy_patterns(row.egress, row.egress_rps, row.granted_secret_refs)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::distribution::{BundleCatalog, BundleRow};
    use std::collections::HashMap;
    use std::future::Future;
    use std::pin::Pin;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    fn test_metrics() -> prometheus::IntCounterVec {
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_egress_denied_total_shim", "test"),
            &["app_id", "reason"],
        )
        .unwrap()
    }

    fn default_limits() -> EgressLimits {
        EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        }
    }

    #[derive(Default)]
    struct FakeTransport {
        responses:
            Mutex<Vec<Result<TransportResponse, penguin_bundle_host::wire::HostResultError>>>,
        requests: Mutex<Vec<TransportRequest>>,
    }

    impl FakeTransport {
        fn queue(
            self,
            resp: Result<TransportResponse, penguin_bundle_host::wire::HostResultError>,
        ) -> Self {
            self.responses.lock().unwrap().push(resp);
            self
        }
    }

    impl HttpTransport for FakeTransport {
        fn send<'a>(
            &'a self,
            req: TransportRequest,
            _timeout: Duration,
            _max_response_bytes: usize,
        ) -> Pin<
            Box<
                dyn Future<
                        Output = Result<
                            TransportResponse,
                            penguin_bundle_host::wire::HostResultError,
                        >,
                    > + Send
                    + 'a,
            >,
        > {
            self.requests.lock().unwrap().push(req);
            let next = self.responses.lock().unwrap().pop().unwrap_or_else(|| {
                Ok(TransportResponse {
                    status: 200,
                    headers: vec![],
                    body: b"{}".to_vec(),
                    truncated: false,
                })
            });
            Box::pin(async move { next })
        }
    }

    /// End-to-end proof of the M3 "http capability -> Discord REST send"
    /// deliverable: the webhook URL is sourced via `crate::senders::
    /// discord_webhook_url_from_config` from this bundle's own
    /// `BundleRow.config_json` (hub-api-controlled activation config) --
    /// never a literal the test (standing in for a bundle) invents
    /// directly -- then built into the bundle-shaped call
    /// `crate::senders::discord_webhook_args` sends over the wire, routed
    /// through the real (now shared) `EgressGuard` pipeline
    /// (allowlist/method/SSRF/rate-limit all genuinely evaluated), landing
    /// on a fake transport standing in for the live TLS connection to
    /// `discord.com`. This is the one test in this crate's own
    /// `crate::egress` module -- every other `EgressGuard` property is
    /// covered by `bundle_host_http::egress`'s own moved test suite.
    #[tokio::test]
    async fn discord_webhook_args_reach_the_transport_through_the_full_guard() {
        let transport = Arc::new(FakeTransport::default().queue(Ok(TransportResponse {
            status: 204,
            headers: vec![],
            body: vec![],
            truncated: false,
        })));
        let catalog = Arc::new(BundleCatalog::new());
        catalog.update(vec![BundleRow {
            app_id: "waddles.socials.discord.default".to_string(),
            version: "1.0.0".to_string(),
            artifact_digest: Some("sha256:00".to_string()),
            component_key: "k".to_string(),
            sidecar_key: "s".to_string(),
            egress: vec![("discord.com".to_string(), vec!["POST".to_string()])],
            egress_rps: None,
            config_json: r#"{"discord_webhook_url": "https://discord.com/api/webhooks/1/abc"}"#
                .to_string(),
            granted_secret_refs: HashMap::new(),
        }]);
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            Arc::clone(&catalog) as Arc<dyn EgressRuleSource>,
            test_metrics(),
            boxed(crate::flags::StaticFlag(true)),
        );

        let row = catalog
            .get("waddles.socials.discord.default")
            .expect("row was just inserted");
        let webhook_url = crate::senders::discord_webhook_url_from_config(&row.config_json)
            .expect("this bundle's activation config carries discord_webhook_url");
        let args = crate::senders::discord_webhook_args(&webhook_url, "hello from waddles");
        let result = guard
            .send("waddles.socials.discord.default", &args)
            .await
            .expect("discord webhook send reaches the transport");
        assert_eq!(result["status"], 204);

        {
            let sent = transport.requests.lock().unwrap();
            assert_eq!(sent[0].method, "POST");
            assert_eq!(sent[0].url, "https://discord.com/api/webhooks/1/abc");
            let body = sent[0].body.as_ref().expect("body present");
            let parsed: serde_json::Value = serde_json::from_slice(body).unwrap();
            assert_eq!(parsed["content"], "hello from waddles");
        }

        // `FakeTransport`'s un-queued default response (200, empty `{}`
        // body) -- exercised by a second send with nothing left queued,
        // distinct from the explicit 204 queued above.
        let result2 = guard
            .send("waddles.socials.discord.default", &args)
            .await
            .expect("default un-queued response still succeeds");
        assert_eq!(result2["status"], 200);
    }
}
