//! The `GET /api/v1/distribution/bundles?stage=action` poll (spec §6.7):
//! resolves which bundle/digest/manifest this pod should run, gated by
//! `ACTION_APP_ID` (spec §16 M3 row's distribution-poll deliverable).
//!
//! This landing polls the endpoint, caches the row for the configured
//! `ACTION_APP_ID` in a [`BundleCatalog`] (consulted by `crate::egress`'s
//! `http` capability for the bundle's `egress` allowlist), and -- once an
//! executor connection is live -- sends `load` for a newly observed digest
//! so the dispatch loop's `invoke` has something to invoke. What remains a
//! documented seam: full multi-bundle scheduling, hot-swap draining of a
//! previous digest (spec §7.6 steps 6-7), and the stream-grants list a
//! `stage=process` row also carries (irrelevant to this stage) are all
//! `TODO(M3+)` -- this landing is single-bundle (`ACTION_APP_ID`), matching
//! every other seam in this crate that is scoped the same way.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use serde::Deserialize;
use thiserror::Error;

use crate::host_api::{ConnectionRegistry, HostApiError};
use crate::service_jwt::{ServiceJwtConfig, ServiceJwtError};

/// Errors fetching or parsing the distribution response.
#[derive(Debug, Error)]
pub enum DistributionError {
    #[error("http request failed: {0}")]
    Request(#[from] reqwest::Error),
    #[error("distribution API returned status {0}")]
    Status(reqwest::StatusCode),
    /// Bug this guards against: hub-api's `tenant_middleware`/
    /// `require_scope("distribution:read")` reject any request with no (or
    /// a malformed) `Authorization` header with a permanent 401 -- a poll
    /// that can't mint its own credential must never silently degrade to an
    /// unauthenticated request (`crate::service_jwt`'s module doc).
    #[error("failed to mint service jwt: {0}")]
    Jwt(#[from] ServiceJwtError),
}

/// One `egress[]` entry as carried in the distribution response's
/// `manifest.egress` (spec §6.7/§8.1). `methods` defaults to the spec's
/// six-verb default when the field is absent/null, matching §8.1: "methods
/// defaults to GET, HEAD, POST, PUT, PATCH, DELETE".
#[derive(Debug, Clone, Deserialize)]
struct RawEgressRule {
    host: String,
    #[serde(default)]
    methods: Option<Vec<String>>,
}

/// The six-verb default `egress[].methods` applies when the manifest entry
/// omits it (spec §8.1).
pub const DEFAULT_EGRESS_METHODS: &[&str] = &["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"];

#[derive(Debug, Clone, Default, Deserialize)]
struct RawLimits {
    #[serde(default)]
    egress_rps: Option<u32>,
}

#[derive(Debug, Clone, Default, Deserialize)]
struct RawManifest {
    #[serde(default)]
    egress: Vec<RawEgressRule>,
    #[serde(default)]
    limits: RawLimits,
}

#[derive(Debug, Clone, Deserialize)]
struct RawBundleRow {
    #[serde(rename = "appId")]
    app_id: String,
    #[serde(rename = "artifactVersion", default)]
    artifact_version: Option<String>,
    #[serde(rename = "artifactDigest", default)]
    artifact_digest: Option<String>,
    #[serde(default)]
    config: serde_json::Value,
    #[serde(default)]
    manifest: RawManifest,
}

#[derive(Debug, Clone, Deserialize)]
struct DistributionResponse {
    #[serde(default)]
    bundles: Vec<RawBundleRow>,
}

/// One bundle's resolved dispatch-relevant state (spec §6.7): the digest to
/// `load`/`invoke`, the bucket keys the `load` frame needs (derived from
/// `app_id`/`version`/digest per §7.6 step 3's naming convention -- the
/// distribution response itself does not carry them separately), the
/// `egress` allowlist `crate::egress::EgressGuard` enforces, and the
/// activation `config` JSON `crate::dispatch::invoke_dispatch` passes to
/// the bundle's `dispatch` export.
#[derive(Debug, Clone, PartialEq)]
pub struct BundleRow {
    pub app_id: String,
    pub version: String,
    /// `sha256:<64 hex>`, or `None` for a registration with no compiled
    /// artifact yet (spec §6.7: "skipped by the stage").
    pub artifact_digest: Option<String>,
    pub component_key: String,
    pub sidecar_key: String,
    /// `(host_pattern, methods)` -- byte-identical shape to
    /// `penguin_bundle_host::manifest::Manifest::egress`.
    pub egress: Vec<(String, Vec<String>)>,
    pub egress_rps: Option<u32>,
    pub config_json: String,
    /// Symbolic secret-reference name -> the actual environment variable
    /// name it resolves to (spec §8.3: "an environment-variable *name*
    /// held in the activation config, resolved at call time"; mirrors
    /// `waddle_transports.signing.resolve_secret`'s Python precedent,
    /// where `secret_ref` is likewise sourced from trusted `config`, never
    /// from bundle-runtime-supplied `payload`). Parsed from this row's own
    /// `config.secret_refs` object -- hub-api/admin-controlled activation
    /// config, per app_id -- **never** from anything a bundle's `http.send`
    /// `args` carry at call time. `crate::egress::EgressGuard` validates a
    /// bundle-supplied `secret_refs` symbolic name against this map before
    /// resolving anything from the process environment: a name that is not
    /// a key here is refused (`secret_not_granted`), which is what makes
    /// arbitrary-env-var-name injection via `http.send` impossible even
    /// though the bundle picks the symbolic name per call (security review
    /// finding, post-M3-capabilities landing).
    pub granted_secret_refs: HashMap<String, String>,
}

/// Derives the `load` frame's `component_key`/`sidecar_key` bucket paths
/// from `app_id`/`version`/digest (spec §7.6 step 3: `bundles/{app_id}/
/// {version}/{sha256}.wasm` and `.json`) -- the distribution response
/// itself carries only the digest, not these paths, so the stage computes
/// them by the documented naming convention rather than expecting a
/// server-supplied key.
fn bucket_keys(app_id: &str, version: &str, digest: &str) -> (String, String) {
    let sha256_hex = digest.strip_prefix("sha256:").unwrap_or(digest);
    (
        format!("bundles/{app_id}/{version}/{sha256_hex}.wasm"),
        format!("bundles/{app_id}/{version}/{sha256_hex}.json"),
    )
}

/// Extracts the `secret_refs` object from this row's raw activation
/// `config` (if present) into a `{symbolic_name: env_var_name}` map --
/// non-string values and a missing/non-object `secret_refs` key are
/// treated as "no grants" (empty map) rather than an error, matching this
/// module's general leniency on optional config shape.
fn extract_granted_secret_refs(config: &serde_json::Value) -> HashMap<String, String> {
    config
        .get("secret_refs")
        .and_then(|v| v.as_object())
        .map(|obj| {
            obj.iter()
                .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string())))
                .collect()
        })
        .unwrap_or_default()
}

impl From<RawBundleRow> for BundleRow {
    fn from(raw: RawBundleRow) -> Self {
        let version = raw.artifact_version.unwrap_or_default();
        let (component_key, sidecar_key) = match &raw.artifact_digest {
            Some(digest) => bucket_keys(&raw.app_id, &version, digest),
            None => (String::new(), String::new()),
        };
        let egress = raw
            .manifest
            .egress
            .into_iter()
            .map(|rule| {
                let methods = rule.methods.unwrap_or_else(|| {
                    DEFAULT_EGRESS_METHODS
                        .iter()
                        .map(|m| m.to_string())
                        .collect()
                });
                (rule.host, methods)
            })
            .collect();
        let granted_secret_refs = extract_granted_secret_refs(&raw.config);
        Self {
            app_id: raw.app_id,
            version,
            artifact_digest: raw.artifact_digest,
            component_key,
            sidecar_key,
            egress,
            egress_rps: raw.manifest.limits.egress_rps,
            config_json: raw.config.to_string(),
            granted_secret_refs,
        }
    }
}

/// GETs `{hub_api_url}/api/v1/distribution/bundles?stage={stage}` and
/// parses every row (spec §6.7). Never filters by app_id here -- the
/// caller decides which rows matter, keeping this function reusable by a
/// future multi-bundle scheduler.
///
/// Presents a freshly-minted `Authorization: Bearer <jwt>` header
/// (`jwt_config.mint()`) on every call -- hub-api's `tenant_middleware` +
/// `require_scope("distribution:read")` (`hub_api/blueprints/v1/
/// distribution.py`) reject an unauthenticated request with a permanent
/// 401, which is exactly what silently starved the bundle catalog before
/// this was wired up. See `crate::service_jwt`'s module doc for why a fresh
/// token is minted per call rather than cached/refreshed.
pub async fn fetch_bundles(
    client: &reqwest::Client,
    hub_api_url: &str,
    stage: &str,
    jwt_config: &ServiceJwtConfig,
) -> Result<Vec<BundleRow>, DistributionError> {
    let url = format!("{hub_api_url}/api/v1/distribution/bundles?stage={stage}");
    let jwt = jwt_config.mint()?;
    let resp = client
        .get(&url)
        .header(reqwest::header::AUTHORIZATION, format!("Bearer {jwt}"))
        .send()
        .await?;
    if !resp.status().is_success() {
        return Err(DistributionError::Status(resp.status()));
    }
    let parsed: DistributionResponse = resp.json().await?;
    Ok(parsed.bundles.into_iter().map(BundleRow::from).collect())
}

/// The latest-known-good distribution snapshot, keyed by `app_id` --
/// consulted by `crate::egress::EgressGuard` (the `http` capability's
/// allowlist) and the poll loop's own load-on-digest-change logic. A fetch
/// failure never clears this: "last-known-good" survives a hub-api outage
/// (spec §6.7: "a hub-api outage degrades gracefully to the last-known-good
/// bundle set rather than raising").
#[derive(Default)]
pub struct BundleCatalog {
    rows: RwLock<HashMap<String, BundleRow>>,
}

impl BundleCatalog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn update(&self, rows: Vec<BundleRow>) {
        let mut guard = self.rows.write().unwrap_or_else(|e| e.into_inner());
        for row in rows {
            guard.insert(row.app_id.clone(), row);
        }
    }

    pub fn get(&self, app_id: &str) -> Option<BundleRow> {
        self.rows
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(app_id)
            .cloned()
    }
}

/// Everything [`run_poll_loop`] needs, bundled to keep its own signature
/// under clippy's argument-count lint (and, unlike a long parameter list,
/// self-documenting at every call site).
pub struct PollLoopConfig {
    pub client: reqwest::Client,
    pub hub_api_url: String,
    pub stage: &'static str,
    pub poll_interval: std::time::Duration,
    pub catalog: Arc<BundleCatalog>,
    pub connections: Arc<ConnectionRegistry>,
    pub action_app_id: String,
    pub load_limits: penguin_bundle_host::wire::LoadLimits,
    /// Credentials `fetch_bundles` mints a fresh service JWT from on every
    /// poll tick (see that function's doc for why this is required at all).
    pub jwt_config: ServiceJwtConfig,
}

/// Runs the poll loop until `shutdown` resolves: fetches every
/// `poll_interval`, merges the result into `catalog`, and -- once an
/// executor connection is live and the configured `action_app_id`'s digest
/// has changed since the last successful `load` -- sends `load` for it
/// (spec §7.6's reconciliation, single-bundle-scoped: see the module doc
/// for what full reconciliation still needs). A fetch failure is logged at
/// WARN and never stops the loop (spec §6.7: "degrades gracefully").
pub async fn run_poll_loop(
    config: PollLoopConfig,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) {
    let PollLoopConfig {
        client,
        hub_api_url,
        stage,
        poll_interval,
        catalog,
        connections,
        action_app_id,
        load_limits,
        jwt_config,
    } = config;
    let mut loaded_digest: Option<String> = None;
    let mut interval = tokio::time::interval(poll_interval);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = &mut shutdown => return,
            _ = interval.tick() => {
                match fetch_bundles(&client, &hub_api_url, stage, &jwt_config).await {
                    Ok(rows) => catalog.update(rows),
                    Err(err) => {
                        tracing::warn!(error = %err, "distribution poll failed, serving last-known-good");
                        continue;
                    }
                }
                let Some(row) = catalog.get(&action_app_id) else { continue };
                let Some(digest) = row.artifact_digest.clone() else {
                    tracing::debug!(app_id = %action_app_id, "distribution row has no compiled artifact yet");
                    continue;
                };
                if loaded_digest.as_deref() == Some(digest.as_str()) {
                    continue;
                }
                let Some(connection) = connections.active() else {
                    tracing::debug!(app_id = %action_app_id, digest, "new digest observed, no executor connection yet");
                    continue;
                };
                match crate::dispatch::ensure_loaded(
                    &connection,
                    &row.app_id,
                    &row.version,
                    &digest,
                    &row.component_key,
                    &row.sidecar_key,
                    load_limits.clone(),
                )
                .await
                {
                    Ok(_) => {
                        tracing::info!(app_id = %action_app_id, digest, "bundle loaded");
                        loaded_digest = Some(digest);
                    }
                    Err(err) => {
                        tracing::warn!(app_id = %action_app_id, digest, error = %err, "bundle load failed");
                    }
                }
            }
        }
    }
}

/// Distinguishes "load succeeded/failed" from "no executor" for
/// [`run_poll_loop`]'s logging -- `crate::dispatch::InvokeError` already
/// implements `Display`, this alias just names the type at this call site.
pub type LoadError = HostApiError;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Secret;

    /// A fixed test signing secret -- every test that decodes a minted
    /// token verifies against this same value.
    const TEST_JWT_SECRET: &str = "test-signing-secret";

    fn test_jwt_config() -> ServiceJwtConfig {
        ServiceJwtConfig {
            secret: Secret::new(TEST_JWT_SECRET),
            issuer: "waddlebot".to_string(),
            audience: "waddlebot-services".to_string(),
            tenant: "global".to_string(),
        }
    }

    fn sample_response_json() -> serde_json::Value {
        serde_json::json!({
            "bundles": [
                {
                    "appId": "waddles.socials.discord.default",
                    "communityId": 42,
                    "entrypoint": "bundles.discord_send_action:dispatch",
                    "spec": {"required_config": []},
                    "config": {"webhook_ref": "DISCORD_WEBHOOK_TOKEN_REF"},
                    "artifactVersion": "1.0.0",
                    "artifactDigest": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
                    "artifactKind": "prebuilt",
                    "language": "rust",
                    "scanStatus": "scanned",
                    "manifest": {
                        "egress": [{"host": "discord.com", "methods": ["POST"]}],
                        "data": {"tables": []},
                        "limits": {"timeout_ms": 2000, "memory_mb": 64, "egress_rps": 5}
                    }
                },
                {
                    "appId": "waddles.no.artifact.yet",
                    "config": {},
                    "manifest": {}
                }
            ]
        })
    }

    async fn spawn_distribution_server(
        body: serde_json::Value,
        status: axum::http::StatusCode,
    ) -> (u16, tokio::task::JoinHandle<()>) {
        let app = axum::Router::new().route(
            "/api/v1/distribution/bundles",
            axum::routing::get(move || {
                let body = body.clone();
                async move { (status, axum::Json(body)) }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds an ephemeral port");
        let port = listener.local_addr().expect("has a local addr").port();
        let handle = tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });
        (port, handle)
    }

    #[test]
    fn bucket_keys_follow_the_spec_7_6_step_3_naming_convention() {
        let (component, sidecar) = bucket_keys(
            "waddles.a.b.c",
            "1.0.0",
            "sha256:aabbccdd00000000000000000000000000000000000000000000000000000",
        );
        assert_eq!(
            component,
            "bundles/waddles.a.b.c/1.0.0/aabbccdd00000000000000000000000000000000000000000000000000000.wasm"
        );
        assert_eq!(
            sidecar,
            "bundles/waddles.a.b.c/1.0.0/aabbccdd00000000000000000000000000000000000000000000000000000.json"
        );
    }

    #[test]
    fn raw_bundle_row_defaults_missing_methods_to_the_spec_six_verbs() {
        let raw: RawBundleRow = serde_json::from_value(serde_json::json!({
            "appId": "waddles.a.b.c",
            "manifest": {"egress": [{"host": "api.example.com"}]}
        }))
        .unwrap();
        let row: BundleRow = raw.into();
        assert_eq!(row.egress.len(), 1);
        assert_eq!(
            row.egress[0].1,
            DEFAULT_EGRESS_METHODS
                .iter()
                .map(|m| m.to_string())
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn raw_bundle_row_with_no_artifact_digest_has_no_bucket_keys() {
        let raw: RawBundleRow = serde_json::from_value(serde_json::json!({
            "appId": "waddles.a.b.c",
            "manifest": {}
        }))
        .unwrap();
        let row: BundleRow = raw.into();
        assert!(row.artifact_digest.is_none());
        assert_eq!(row.component_key, "");
    }

    /// Security-review regression: `granted_secret_refs` must come from
    /// this row's own trusted `config.secret_refs` object (hub-api/admin-
    /// controlled activation config), never be left populated by anything
    /// bundle-runtime-controlled -- `crate::egress::EgressGuard` is the
    /// consumer that enforces this at the `http.send` boundary.
    #[test]
    fn raw_bundle_row_parses_granted_secret_refs_from_activation_config() {
        let raw: RawBundleRow = serde_json::from_value(serde_json::json!({
            "appId": "waddles.socials.discord.default",
            "config": {"secret_refs": {"DISCORD_WEBHOOK_TOKEN_REF": "DISCORD_WEBHOOK_TOKEN"}},
            "manifest": {}
        }))
        .unwrap();
        let row: BundleRow = raw.into();
        assert_eq!(
            row.granted_secret_refs.get("DISCORD_WEBHOOK_TOKEN_REF"),
            Some(&"DISCORD_WEBHOOK_TOKEN".to_string())
        );
    }

    #[test]
    fn raw_bundle_row_with_no_secret_refs_config_has_no_grants() {
        let raw: RawBundleRow = serde_json::from_value(serde_json::json!({
            "appId": "waddles.a.b.c",
            "manifest": {}
        }))
        .unwrap();
        let row: BundleRow = raw.into();
        assert!(row.granted_secret_refs.is_empty());
    }

    /// A non-string value under `secret_refs` (malformed activation config)
    /// is skipped rather than accepted as a grant -- never widens the
    /// granted set on malformed input.
    #[test]
    fn raw_bundle_row_ignores_non_string_secret_ref_values() {
        let raw: RawBundleRow = serde_json::from_value(serde_json::json!({
            "appId": "waddles.a.b.c",
            "config": {"secret_refs": {"BAD_REF": 12345, "GOOD_REF": "REAL_ENV_VAR"}},
            "manifest": {}
        }))
        .unwrap();
        let row: BundleRow = raw.into();
        assert!(!row.granted_secret_refs.contains_key("BAD_REF"));
        assert_eq!(
            row.granted_secret_refs.get("GOOD_REF"),
            Some(&"REAL_ENV_VAR".to_string())
        );
    }

    #[tokio::test]
    async fn fetch_bundles_parses_a_real_response() {
        let (port, _handle) =
            spawn_distribution_server(sample_response_json(), axum::http::StatusCode::OK).await;
        let client = reqwest::Client::new();
        let rows = fetch_bundles(
            &client,
            &format!("http://127.0.0.1:{port}"),
            "action",
            &test_jwt_config(),
        )
        .await
        .expect("fetch succeeds");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].app_id, "waddles.socials.discord.default");
        assert_eq!(
            rows[0].egress,
            vec![("discord.com".to_string(), vec!["POST".to_string()])]
        );
        assert_eq!(rows[0].egress_rps, Some(5));
        assert!(rows[1].artifact_digest.is_none());
    }

    #[tokio::test]
    async fn fetch_bundles_reports_non_success_status() {
        let (port, _handle) = spawn_distribution_server(
            serde_json::json!({"bundles": []}),
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
        )
        .await;
        let client = reqwest::Client::new();
        let err = fetch_bundles(
            &client,
            &format!("http://127.0.0.1:{port}"),
            "action",
            &test_jwt_config(),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, DistributionError::Status(_)));
    }

    #[tokio::test]
    async fn fetch_bundles_fails_against_an_unreachable_host() {
        let client = reqwest::Client::new();
        let err = fetch_bundles(&client, "http://127.0.0.1:1", "action", &test_jwt_config())
            .await
            .unwrap_err();
        assert!(matches!(err, DistributionError::Request(_)));
    }

    /// Regression for the auth bug this module was fixed for: hub-api's
    /// `tenant_middleware` + `require_scope("distribution:read")` reject
    /// any poll request with no `Authorization` header with a permanent
    /// 401 -- before this fix, `fetch_bundles` sent none at all, so the
    /// bundle catalog never populated. Asserts the header is present,
    /// well-formed, and decodes to a token hub-api's own verification path
    /// (`libs/flask_core/flask_core/auth.py::verify_jwt_token`) would
    /// accept: signed with the shared secret, carrying the exact
    /// `distribution:read` scope and the configured tenant.
    #[tokio::test]
    async fn fetch_bundles_sends_a_bearer_authorization_header_hub_api_accepts() {
        let captured_auth = Arc::new(std::sync::Mutex::new(None::<String>));
        let captured_auth_clone = Arc::clone(&captured_auth);
        let body = sample_response_json();
        let app = axum::Router::new().route(
            "/api/v1/distribution/bundles",
            axum::routing::get(move |headers: axum::http::HeaderMap| {
                let captured_auth = Arc::clone(&captured_auth_clone);
                let body = body.clone();
                async move {
                    let auth = headers
                        .get(axum::http::header::AUTHORIZATION)
                        .and_then(|v| v.to_str().ok())
                        .map(str::to_string);
                    *captured_auth.lock().unwrap_or_else(|e| e.into_inner()) = auth;
                    (axum::http::StatusCode::OK, axum::Json(body))
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds an ephemeral port");
        let port = listener.local_addr().expect("has a local addr").port();
        tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });

        let client = reqwest::Client::new();
        fetch_bundles(
            &client,
            &format!("http://127.0.0.1:{port}"),
            "action",
            &test_jwt_config(),
        )
        .await
        .expect("fetch succeeds");

        let auth_header = captured_auth
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
            .expect("Authorization header must be present on the poll request");
        let token = auth_header
            .strip_prefix("Bearer ")
            .expect("Authorization header must be a Bearer token");

        let mut validation = jsonwebtoken::Validation::new(jsonwebtoken::Algorithm::HS256);
        validation.set_issuer(&["waddlebot"]);
        validation.set_audience(&["waddlebot-services"]);
        let decoded = jsonwebtoken::decode::<serde_json::Value>(
            token,
            &jsonwebtoken::DecodingKey::from_secret(TEST_JWT_SECRET.as_bytes()),
            &validation,
        )
        .expect("hub-api's shared-secret verification would accept this token");
        assert_eq!(decoded.claims["scope"], "distribution:read");
        assert_eq!(decoded.claims["tenant"], "global");
        assert_eq!(decoded.claims["sub"], "svc-action");
    }

    #[test]
    fn bundle_catalog_get_returns_none_before_any_update() {
        let catalog = BundleCatalog::new();
        assert!(catalog.get("waddles.a.b.c").is_none());
    }

    #[test]
    fn bundle_catalog_update_then_get_round_trips() {
        let catalog = BundleCatalog::new();
        let row = BundleRow {
            app_id: "waddles.a.b.c".to_string(),
            version: "1.0.0".to_string(),
            artifact_digest: Some("sha256:00".to_string()),
            component_key: "k".to_string(),
            sidecar_key: "s".to_string(),
            egress: vec![],
            egress_rps: None,
            config_json: "{}".to_string(),
            granted_secret_refs: HashMap::new(),
        };
        catalog.update(vec![row.clone()]);
        assert_eq!(catalog.get("waddles.a.b.c"), Some(row));
    }

    #[test]
    fn bundle_catalog_update_replaces_the_row_for_the_same_app_id() {
        let catalog = BundleCatalog::new();
        let mut row = BundleRow {
            app_id: "waddles.a.b.c".to_string(),
            version: "1.0.0".to_string(),
            artifact_digest: Some("sha256:00".to_string()),
            component_key: "k".to_string(),
            sidecar_key: "s".to_string(),
            egress: vec![],
            egress_rps: None,
            config_json: "{}".to_string(),
            granted_secret_refs: HashMap::new(),
        };
        catalog.update(vec![row.clone()]);
        row.artifact_digest = Some("sha256:11".to_string());
        catalog.update(vec![row.clone()]);
        assert_eq!(
            catalog.get("waddles.a.b.c").unwrap().artifact_digest,
            Some("sha256:11".to_string())
        );
    }

    #[tokio::test]
    async fn run_poll_loop_stops_promptly_once_shutdown_resolves() {
        let (port, _handle) =
            spawn_distribution_server(sample_response_json(), axum::http::StatusCode::OK).await;
        let (tx, rx) = tokio::sync::oneshot::channel();
        tx.send(()).unwrap();
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            run_poll_loop(
                PollLoopConfig {
                    client: reqwest::Client::new(),
                    hub_api_url: format!("http://127.0.0.1:{port}"),
                    stage: "action",
                    poll_interval: std::time::Duration::from_millis(50),
                    catalog: Arc::new(BundleCatalog::new()),
                    connections: Arc::new(ConnectionRegistry::new()),
                    action_app_id: "waddles.a.b.c".to_string(),
                    load_limits: penguin_bundle_host::wire::LoadLimits {
                        timeout_ms: 2000,
                        memory_mb: 64,
                    },
                    jwt_config: test_jwt_config(),
                },
                rx,
            ),
        )
        .await;
        assert!(
            result.is_ok(),
            "run_poll_loop must return promptly once shutdown resolves"
        );
    }

    #[tokio::test]
    async fn run_poll_loop_populates_the_catalog_without_an_executor() {
        let (port, _handle) =
            spawn_distribution_server(sample_response_json(), axum::http::StatusCode::OK).await;
        let catalog = Arc::new(BundleCatalog::new());
        let (tx, rx) = tokio::sync::oneshot::channel();
        let catalog_clone = Arc::clone(&catalog);
        let handle = tokio::spawn(run_poll_loop(
            PollLoopConfig {
                client: reqwest::Client::new(),
                hub_api_url: format!("http://127.0.0.1:{port}"),
                stage: "action",
                poll_interval: std::time::Duration::from_millis(20),
                catalog: catalog_clone,
                connections: Arc::new(ConnectionRegistry::new()),
                action_app_id: "waddles.socials.discord.default".to_string(),
                load_limits: penguin_bundle_host::wire::LoadLimits {
                    timeout_ms: 2000,
                    memory_mb: 64,
                },
                jwt_config: test_jwt_config(),
            },
            rx,
        ));
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        let _ = tx.send(());
        let _ = tokio::time::timeout(std::time::Duration::from_secs(5), handle).await;
        assert!(catalog.get("waddles.socials.discord.default").is_some());
    }
}
