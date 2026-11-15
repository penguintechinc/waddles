//! Builds the production [`CredentialSources`] for a data-plane service's
//! egress guard from environment configuration.
//!
//! Failure posture (no silent fallback): a source that cannot be built is
//! left out and its grants then fail `secret_unavailable`; metered refs with
//! no quota backend fail closed `quota_unavailable`.

use std::collections::HashMap;
use std::sync::Arc;

use bundle_host_http::credentials::{
    AppTokenMinter, BoxFut, CredentialSources, QuotaCharge, QuotaGuard, QuotaPolicy, QuotaStore,
    QuotaVerdict,
};
use bundle_host_http::grants::{default_app_credentials, default_quota_policies};
use connection_resolver::{
    ConnectionKind, ConnectionResolver, ResolverConfig, ResolverMetrics,
    SCOPE_CONNECTIONS_RESOLVE_STATIC,
};
use serde_json::Value;

use crate::{ResolverConnections, ValkeyQuotaStore};

/// Env var holding operator quota overrides: `{"<ref>": {"per_minute":n,
/// "per_day":n,"community_day_fraction":f}}`, merged over the defaults.
pub const QUOTAS_ENV: &str = "BUNDLE_SECRET_QUOTAS_JSON";

struct NoMetrics;
impl ResolverMetrics for NoMetrics {
    fn resolve(&self, _: ConnectionKind, _: &'static str, _: std::time::Duration) {}
    fn list(&self, _: &'static str) {}
}

/// Quota backend used when no Valkey client exists: every metered charge is
/// refused (`quota_unavailable`) rather than silently unmetered.
struct UnavailableStore;
impl QuotaStore for UnavailableStore {
    fn charge<'a>(&'a self, _: &'a QuotaCharge) -> BoxFut<'a, Result<QuotaVerdict, ()>> {
        Box::pin(async { Err(()) })
    }
}

/// Parses [`QUOTAS_ENV`] overrides over `base`. Malformed JSON or an invalid
/// policy is an error (loud), never ignored.
pub fn merge_quota_overrides(
    mut base: HashMap<String, QuotaPolicy>,
    raw: Option<&str>,
) -> Result<HashMap<String, QuotaPolicy>, String> {
    let Some(raw) = raw.filter(|r| !r.trim().is_empty()) else {
        return Ok(base);
    };
    let v: Value = serde_json::from_str(raw).map_err(|e| format!("{QUOTAS_ENV}: {e}"))?;
    let obj = v
        .as_object()
        .ok_or_else(|| format!("{QUOTAS_ENV}: must be a JSON object"))?;
    for (name, p) in obj {
        let get = |k: &str| p.get(k).and_then(Value::as_u64);
        let policy = QuotaPolicy {
            per_minute: get("per_minute"),
            per_day: get("per_day"),
            community_day_fraction: p.get("community_day_fraction").and_then(Value::as_f64),
        };
        policy
            .validate()
            .ok_or_else(|| format!("{QUOTAS_ENV}: invalid policy for {name:?}"))?;
        base.insert(name.clone(), policy);
    }
    Ok(base)
}

/// Builds the sources. `get` reads configuration (injectable for tests);
/// `quota_client` is the service's already-built (TLS-aware) Valkey client.
pub fn build_credential_sources(
    get: &dyn Fn(&str) -> Option<String>,
    quota_client: Option<redis::Client>,
) -> CredentialSources {
    let non_empty = |k: &str| get(k).filter(|v| !v.trim().is_empty());

    let connections = match (
        non_empty("HUB_API_URL"),
        non_empty("SERVICE_JWT_TOKEN_ENDPOINT"),
    ) {
        (Some(hub), Some(token_endpoint)) => {
            let mut cfg = ResolverConfig::new(
                hub,
                non_empty("DEPLOYMENT_TIER").unwrap_or_else(|| "alpha".to_string()),
                token_endpoint,
                non_empty("SERVICE_JWT_SA_TOKEN_PATH").unwrap_or_else(|| {
                    "/var/run/secrets/kubernetes.io/serviceaccount/token".to_string()
                }),
            );
            // The http-egress host (and ONLY it -- never svc-ingest generally)
            // needs static-secret resolves: Wix API credentials (scheme 6),
            // SoundCloud client secret, YouTube API key. hub-api grants this
            // scope per service identity; without the grant every static
            // resolve fails closed (`secret_unavailable`).
            cfg.resolve_scope = SCOPE_CONNECTIONS_RESOLVE_STATIC.to_string();
            match ConnectionResolver::new(cfg, Arc::new(NoMetrics)) {
                Ok(r) => Some(Arc::new(ResolverConnections::new(r))
                    as Arc<dyn bundle_host_http::credentials::ConnectionCredentials>),
                Err(e) => {
                    tracing::error!(error = %e, "bundle http: connection resolver unavailable; connection grants will be denied");
                    None
                }
            }
        }
        _ => {
            tracing::warn!("bundle http: HUB_API_URL/SERVICE_JWT_TOKEN_ENDPOINT unset; connection grants will be denied");
            None
        }
    };

    let apps = match AppTokenMinter::new(default_app_credentials()) {
        Ok(m) => Some(Arc::new(m)),
        Err(e) => {
            tracing::error!(error = %e, "bundle http: app credentials unavailable");
            None
        }
    };

    let policies =
        match merge_quota_overrides(default_quota_policies(), non_empty(QUOTAS_ENV).as_deref()) {
            Ok(p) => p,
            Err(e) => {
                tracing::error!(error = %e, "bundle http: quota overrides invalid; using defaults");
                default_quota_policies()
            }
        };
    let store: Arc<dyn QuotaStore> = match quota_client {
        Some(client) => Arc::new(ValkeyQuotaStore::from_client(client)),
        None => {
            tracing::error!("bundle http: no Valkey client; metered credentials fail closed");
            Arc::new(UnavailableStore)
        }
    };
    let quota = match QuotaGuard::new(policies, store) {
        Ok(g) => Some(Arc::new(g)),
        Err(e) => {
            tracing::error!(error = %e, "bundle http: quota guard invalid");
            None
        }
    };

    CredentialSources {
        connections,
        apps,
        quota,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn overrides_merge_and_bad_input_is_an_error() {
        let base = default_quota_policies();
        let merged = merge_quota_overrides(
            base.clone(),
            Some(r#"{"YOUTUBE_API_KEY":{"per_day":5,"community_day_fraction":0.5}}"#),
        )
        .unwrap();
        assert_eq!(merged["YOUTUBE_API_KEY"].per_day, Some(5));
        assert_eq!(merged["spotify.app"], base["spotify.app"]);
        for bad in [
            "not json",
            "[]",
            r#"{"x":{"per_day":0}}"#,
            r#"{"x":{"community_day_fraction":0.5}}"#,
        ] {
            assert!(
                merge_quota_overrides(base.clone(), Some(bad)).is_err(),
                "{bad}"
            );
        }
        assert_eq!(
            merge_quota_overrides(base.clone(), Some("  "))
                .unwrap()
                .len(),
            base.len()
        );
    }

    #[tokio::test]
    async fn no_valkey_means_metered_refs_fail_closed() {
        let s = build_credential_sources(&|_| None, None);
        let q = s.quota.expect("quota guard built");
        assert_eq!(
            q.charge("YOUTUBE_API_KEY", 1).await.unwrap_err().code,
            "quota_unavailable"
        );
        q.charge("unmetered.ref", 1).await.unwrap();
        assert!(s.connections.is_none());
    }
}
