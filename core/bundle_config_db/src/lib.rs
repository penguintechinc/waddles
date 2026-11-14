//! Direct read-only database source for live per-community bundle config (issue #1306).
//!
//! A separate crate (like `bundle_active_set`) so `hub_client` -- depended on by many crates that
//! only need hub-api gRPC -- does not pull sea-orm/sqlx into every dependency graph.
//!
//! hub-api is the control-plane **writer** (`bundle_community_config`, append-only, highest
//! version live); the data plane (svc-process, svc-action) reads the database itself -- from the
//! read-only replica -- under its own column-scoped role and never calls hub-api on the hot path,
//! so config keeps being served while hub-api is down. This is the same shape as svc-ingest's
//! display-name reader (`core/svc_ingest/src/display_names.rs`, alembic `0101`).
//!
//! **What is read.** For every requested `(tenant_id, community_id, app_id)` ONE statement
//! ([`CONFIG_SQL`]) answers, exactly as hub-api's `get_runtime_config` did:
//! * the community must belong to the tenant (`community_id = 0` is the tenant-wide scope);
//! * the app must be active for the scope (a community activation beats the tenant-wide one);
//! * the manifest `community_config_schema.properties.*.default` values overlaid with the latest
//!   saved config (a saved key always wins).
//!
//! A scope that fails the first two checks is simply absent from the answer (lazy-load `get`
//! reports it as `NOT_FOUND`, like hub-api for an inactive bundle).
//!
//! **Batching.** [`DbConfigSource::batch_get`] sends all scopes as three parallel arrays into a
//! single `unnest(...)` query -- one round trip for the whole warm-up, never one per scope.
//!
//! **Failure behaviour (documented, no panic).** A connect/query failure is returned as an
//! `UNAVAILABLE` status; the [`hub_client::bundle_config::BundleConfigCache`] then serves the
//! last-known value (ERROR log + `waddles_bundle_config_load_errors`), and a scope never loaded
//! falls through to the registered manifest defaults, else a loud ERROR. This module also logs
//! ERROR with the error kind and a DSN-redacted message and bumps
//! `waddles_bundle_config_db_reads{outcome="error"}`. Nothing is silently defaulted.
//!
//! **Read path selection.** [`RoutedConfigSource`] picks this DB path unless the opt-out kill
//! switch [`DISABLE_DB_READS_FLAG`] (`waddles.disable-db-bundle-config-reads`) is ON, in which
//! case the legacy hub-api gRPC path serves. Unseen/OFF/unreachable flag = DB path (a core
//! platform mechanism, so a missing PostHog key never strands a deployment).
//!
//! **Connection.** `DATABASE_REPLICA_URL` (the read-only replica DSN); when unset the primary
//! `DATABASE_URL` is used and a WARN is logged. The DSN carries the role credential and is never
//! logged. The pool is lazy: a database that is down at startup never disables reads for the life
//! of the process -- the next lookup reconnects.
//!
//! Logs/spans/metric labels carry numeric ids and outcomes only -- never a config body.

use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::KeyValue;
use sea_orm::{
    ConnectOptions, ConnectionTrait, Database, DatabaseConnection, DbBackend, Statement,
};
use tracing::Instrument;

use hub_client::bundle_config::{
    install_global, BundleConfigCache, BundleConfigClient, CacheConfig, ConfigKey, ConfigSource,
    ConfigValue, ReadPathGate, ValkeyVersionProbe, VersionProbe,
};
use hub_client::HubClientError;

/// Env var holding the read-only replica DSN (preferred).
pub const REPLICA_URL_ENV: &str = "DATABASE_REPLICA_URL";
/// Env var holding the primary DSN (fallback only, logged at WARN).
pub const PRIMARY_URL_ENV: &str = "DATABASE_URL";
/// PostHog opt-out kill switch: ON = serve through the legacy hub-api gRPC path instead of the
/// database. Unseen/OFF = database path.
pub const DISABLE_DB_READS_FLAG: &str = "waddles.disable-db-bundle-config-reads";
/// Per-statement and connect deadline.
pub const DB_TIMEOUT: Duration = Duration::from_secs(5);

/// The single batched statement. `$1..$3` are parallel arrays (tenant ids, community ids, app
/// ids); numeric ids only. Output columns: `tenant_id, community_id, app_id, version,
/// config_json` -- `config_json` is the defaults object `||` the latest saved object (jsonb `||`
/// = a saved top-level key wins, i.e. hub-api's `merged.update(saved)`).
pub const CONFIG_SQL: &str = "\
WITH req AS (
    SELECT k.tenant_id, k.community_id, k.app_id
      FROM unnest($1::bigint[], $2::bigint[], $3::text[]) AS k(tenant_id, community_id, app_id)
)
SELECT r.tenant_id::bigint AS tenant_id,
       r.community_id::bigint AS community_id,
       r.app_id::text AS app_id,
       COALESCE(cfg.version, 0)::bigint AS version,
       (COALESCE(defs.d, '{}'::jsonb)
         || CASE WHEN jsonb_typeof(cfg.config) = 'object' THEN cfg.config ELSE '{}'::jsonb END
       )::text AS config_json
  FROM req r
  JOIN LATERAL (
        SELECT u.manifest_json -> 'community_config_schema' -> 'properties' AS props
          FROM app_active_versions a
          JOIN app_versions v ON v.id = a.version_id
          JOIN app_version_uploads u ON u.app_version_id = v.id
         WHERE a.app_id = r.app_id
           AND a.tenant_id = r.tenant_id
           AND a.community_id IN (r.community_id, 0)
         ORDER BY a.community_id DESC, u.id DESC
         LIMIT 1
       ) act ON TRUE
  LEFT JOIN LATERAL (
        SELECT jsonb_object_agg(p.key, p.value -> 'default') AS d
          FROM jsonb_each(CASE WHEN jsonb_typeof(act.props) = 'object'
                               THEN act.props ELSE '{}'::jsonb END) AS p
         WHERE jsonb_typeof(p.value) = 'object' AND jsonb_exists(p.value, 'default')
       ) defs ON TRUE
  LEFT JOIN LATERAL (
        SELECT c.version, c.config
          FROM bundle_community_config c
         WHERE c.tenant_id = r.tenant_id
           AND c.community_id = r.community_id
           AND c.app_id = r.app_id
         ORDER BY c.version DESC
         LIMIT 1
       ) cfg ON TRUE
 WHERE r.community_id = 0
    OR EXISTS (SELECT 1 FROM communities co
                WHERE co.id = r.community_id AND co.tenant_id = r.tenant_id)";

/// Where the database DSN came from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DsnSource {
    /// `DATABASE_REPLICA_URL` (the read-only replica).
    Replica,
    /// `DATABASE_URL` fallback (the primary) -- WARN-logged.
    Primary,
}

/// Resolved database settings for the config reader. `url` carries the role credential and is
/// never logged.
#[derive(Clone)]
pub struct DbReadConfig {
    /// The DSN to read from; `None` when neither env var is set.
    pub url: Option<String>,
    /// Which env var supplied it (`None` iff `url` is `None`).
    pub source: Option<DsnSource>,
}

impl DbReadConfig {
    /// Resolves the DSN: a non-blank replica DSN wins; otherwise the primary DSN is used and a
    /// WARN is logged; with neither set the DB path is unavailable (the caller logs ERROR).
    #[must_use]
    pub fn from_values(replica: Option<String>, primary: Option<String>) -> Self {
        let clean = |v: Option<String>| v.filter(|u| !u.trim().is_empty());
        if let Some(url) = clean(replica) {
            return Self {
                url: Some(url),
                source: Some(DsnSource::Replica),
            };
        }
        if let Some(url) = clean(primary) {
            tracing::warn!(
                replica_env = REPLICA_URL_ENV,
                primary_env = PRIMARY_URL_ENV,
                "bundle_config: {REPLICA_URL_ENV} unset; reading bundle config from the PRIMARY \
                 database via {PRIMARY_URL_ENV} (set the read-only replica DSN)"
            );
            return Self {
                url: Some(url),
                source: Some(DsnSource::Primary),
            };
        }
        Self {
            url: None,
            source: None,
        }
    }

    /// [`Self::from_values`] over the process environment.
    #[must_use]
    pub fn from_env() -> Self {
        Self::from_values(
            std::env::var(REPLICA_URL_ENV).ok(),
            std::env::var(PRIMARY_URL_ENV).ok(),
        )
    }
}

/// OTel instruments of the database read path (process-global meter).
struct Instruments {
    reads: Counter<u64>,
    read_seconds: Histogram<f64>,
    path: Counter<u64>,
}

/// The lazily-built [`Instruments`].
fn instruments() -> &'static Instruments {
    static I: OnceLock<Instruments> = OnceLock::new();
    I.get_or_init(|| {
        let meter = opentelemetry::global::meter("bundle_config_db");
        Instruments {
            reads: meter
                .u64_counter("waddles_bundle_config_db_reads")
                .with_description("Bundle community-config database reads by op and outcome.")
                .build(),
            read_seconds: meter
                .f64_histogram("waddles_bundle_config_db_read_seconds")
                .with_unit("s")
                .with_description("Bundle community-config database read latency.")
                .with_boundaries(vec![
                    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
                ])
                .build(),
            path: meter
                .u64_counter("waddles_bundle_config_read_path")
                .with_description("Which read path served a bundle-config load (db | hub_api).")
                .build(),
        }
    })
}

/// The error's variant name only (stable label / log field).
fn error_kind(e: &sea_orm::DbErr) -> &'static str {
    match e {
        sea_orm::DbErr::Conn(_) => "connection",
        sea_orm::DbErr::ConnectionAcquire(_) => "connection_acquire",
        sea_orm::DbErr::Query(_) => "query",
        sea_orm::DbErr::Exec(_) => "exec",
        sea_orm::DbErr::Type(_) | sea_orm::DbErr::TryIntoErr { .. } => "type",
        _ => "other",
    }
}

/// Removes the DSN (and the password embedded in it) from an error message before it is logged.
fn redact(message: &str, url: &str) -> String {
    let mut out = message.replace(url, "<dsn>");
    // `scheme://user:password@host/...` -> also scrub a bare password echo.
    if let Some(rest) = url.split("://").nth(1) {
        if let Some((creds, _)) = rest.split_once('@') {
            if let Some((_, password)) = creds.split_once(':') {
                if !password.is_empty() {
                    out = out.replace(password, "<redacted>");
                }
            }
        }
    }
    out
}

/// [`ConfigSource`] over a lazily-connected, small read-only Postgres pool.
pub struct DbConfigSource {
    url: String,
    timeout: Duration,
    conn: tokio::sync::Mutex<Option<DatabaseConnection>>,
    queries: AtomicU64,
    /// After a failed read, further reads fail fast until this instant so an outage costs one
    /// timeout per window -- not one per bundle invocation on the hot path.
    backoff: Duration,
    retry_after: Mutex<Option<Instant>>,
}

/// Default fast-fail window after a failed database read.
pub const DB_FAILURE_BACKOFF: Duration = Duration::from_secs(2);

impl DbConfigSource {
    /// A source that has not connected yet. `url` carries the role's credential and is never
    /// logged.
    #[must_use]
    pub fn new(url: String, timeout: Duration) -> Self {
        Self {
            url,
            timeout,
            conn: tokio::sync::Mutex::new(None),
            queries: AtomicU64::new(0),
            backoff: DB_FAILURE_BACKOFF,
            retry_after: Mutex::new(None),
        }
    }

    /// Overrides the fast-fail window after a failed read (tests).
    #[must_use]
    pub fn with_failure_backoff(mut self, backoff: Duration) -> Self {
        self.backoff = backoff;
        self
    }

    /// True while the fast-fail window after the last failed read is still open.
    fn backing_off(&self) -> bool {
        self.retry_after
            .lock()
            .ok()
            .and_then(|g| *g)
            .is_some_and(|until| Instant::now() < until)
    }

    /// Opens (`failed`) or clears the fast-fail window.
    fn note_outcome(&self, failed: bool) {
        if let Ok(mut g) = self.retry_after.lock() {
            *g = failed.then(|| Instant::now() + self.backoff);
        }
    }

    /// Number of config statements sent to Postgres so far (one per `get`/`batch_get`
    /// regardless of how many scopes it carried) -- the warm-up batching proof in tests.
    #[must_use]
    pub fn query_count(&self) -> u64 {
        self.queries.load(Ordering::SeqCst)
    }

    /// The pooled connection, connecting on first use and again after a failed connect.
    async fn connection(&self) -> Result<DatabaseConnection, String> {
        let mut slot = self.conn.lock().await;
        if let Some(c) = slot.as_ref() {
            return Ok(c.clone());
        }
        let mut opts = ConnectOptions::new(self.url.clone());
        opts.max_connections(4)
            .min_connections(0)
            .connect_timeout(self.timeout)
            .acquire_timeout(self.timeout)
            .sqlx_logging(false);
        let conn = Database::connect(opts)
            .await
            .map_err(|e| format!("connect: {}", error_kind(&e)))?;
        *slot = Some(conn.clone());
        Ok(conn)
    }

    /// Runs [`CONFIG_SQL`] once for `keys` (deduplicated). Errors carry only a kind string.
    async fn query(&self, keys: &[ConfigKey]) -> Result<Vec<ConfigValue>, String> {
        let mut seen: HashSet<&ConfigKey> = HashSet::with_capacity(keys.len());
        let mut tenants: Vec<i64> = Vec::with_capacity(keys.len());
        let mut communities: Vec<i64> = Vec::with_capacity(keys.len());
        let mut apps: Vec<String> = Vec::with_capacity(keys.len());
        for k in keys {
            if seen.insert(k) {
                tenants.push(k.tenant_id);
                communities.push(k.community_id);
                apps.push(k.app_id.clone());
            }
        }
        if tenants.is_empty() {
            return Ok(Vec::new());
        }
        let conn = self.connection().await?;
        self.queries.fetch_add(1, Ordering::SeqCst);
        let rows = tokio::time::timeout(
            self.timeout,
            conn.query_all_raw(Statement::from_sql_and_values(
                DbBackend::Postgres,
                CONFIG_SQL,
                [tenants.into(), communities.into(), apps.into()],
            )),
        )
        .await
        .map_err(|_| "config query timed out".to_string())?
        .map_err(|e| {
            format!(
                "config query: {}: {}",
                error_kind(&e),
                redact(&e.to_string(), &self.url)
            )
        })?;
        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let tenant_id: i64 = row
                .try_get_by_index(0)
                .map_err(|e| format!("decode tenant_id: {}", error_kind(&e)))?;
            let community_id: i64 = row
                .try_get_by_index(1)
                .map_err(|e| format!("decode community_id: {}", error_kind(&e)))?;
            let app_id: String = row
                .try_get_by_index(2)
                .map_err(|e| format!("decode app_id: {}", error_kind(&e)))?;
            let version: i64 = row
                .try_get_by_index(3)
                .map_err(|e| format!("decode version: {}", error_kind(&e)))?;
            let config_json: String = row
                .try_get_by_index(4)
                .map_err(|e| format!("decode config_json: {}", error_kind(&e)))?;
            out.push(ConfigValue {
                key: ConfigKey::new(tenant_id, community_id, app_id),
                version,
                config_json,
            });
        }
        Ok(out)
    }

    /// Instrumented wrapper: span, latency histogram, outcome counter, ERROR log on failure.
    async fn fetch(
        &self,
        op: &'static str,
        keys: &[ConfigKey],
    ) -> Result<Vec<ConfigValue>, HubClientError> {
        if self.backing_off() {
            instruments().reads.add(
                1,
                &[KeyValue::new("op", op), KeyValue::new("outcome", "backoff")],
            );
            return Err(HubClientError::Grpc(tonic::Status::unavailable(
                "bundle-config database read failed recently; failing fast",
            )));
        }
        let started = Instant::now();
        let span = tracing::info_span!(
            "bundle_config_db.read",
            db.system = "postgresql",
            op,
            scopes = keys.len(),
            outcome = tracing::field::Empty,
        );
        let recorder = span.clone();
        let result = self.query(keys).instrument(span).await;
        let i = instruments();
        i.read_seconds
            .record(started.elapsed().as_secs_f64(), &[KeyValue::new("op", op)]);
        self.note_outcome(result.is_err());
        match result {
            Ok(values) => {
                recorder.record("outcome", "ok");
                i.reads.add(
                    1,
                    &[KeyValue::new("op", op), KeyValue::new("outcome", "ok")],
                );
                Ok(values)
            }
            Err(kind) => {
                recorder.record("outcome", "error");
                i.reads.add(
                    1,
                    &[KeyValue::new("op", op), KeyValue::new("outcome", "error")],
                );
                tracing::error!(
                    op,
                    scopes = keys.len(),
                    error = %kind,
                    "bundle_config: database read failed"
                );
                Err(HubClientError::Grpc(tonic::Status::unavailable(
                    "bundle-config database read failed",
                )))
            }
        }
    }
}

#[async_trait]
impl ConfigSource for DbConfigSource {
    /// Lazy-load of one scope: one read-only statement; an absent scope is `NOT_FOUND`.
    async fn get(&self, key: &ConfigKey) -> Result<ConfigValue, HubClientError> {
        let rows = self.fetch("get", std::slice::from_ref(key)).await?;
        rows.into_iter().next().ok_or_else(|| {
            HubClientError::Grpc(tonic::Status::not_found("bundle not active for scope"))
        })
    }

    /// Warm-up load: ONE statement for every scope in `keys`.
    async fn batch_get(&self, keys: &[ConfigKey]) -> Result<Vec<ConfigValue>, HubClientError> {
        self.fetch("batch_get", keys).await
    }
}

/// Chooses per call between the database source and the legacy hub-api gRPC source.
///
/// The database serves unless [`DISABLE_DB_READS_FLAG`] is ON (or no database source exists, an
/// ERROR-logged misconfiguration); the legacy source then serves when configured. A failure of
/// the chosen path is returned as-is -- there is no silent per-call fallback to the other path.
pub struct RoutedConfigSource {
    db: Option<Arc<dyn ConfigSource>>,
    legacy: Option<Arc<dyn ConfigSource>>,
    gate: OnceLock<Arc<dyn ReadPathGate>>,
}

impl RoutedConfigSource {
    /// Routes between `db` (preferred) and `legacy`. At least one should be `Some`.
    #[must_use]
    pub fn new(db: Option<Arc<dyn ConfigSource>>, legacy: Option<Arc<dyn ConfigSource>>) -> Self {
        Self {
            db,
            legacy,
            gate: OnceLock::new(),
        }
    }

    /// The source for this call, and its `path` label.
    async fn pick(&self) -> Result<(&Arc<dyn ConfigSource>, &'static str), HubClientError> {
        let db_on = match self.gate.get() {
            Some(g) => g.db_reads_enabled().await,
            None => true,
        };
        if db_on {
            if let Some(db) = &self.db {
                return Ok((db, "db"));
            }
            tracing::error!(
                "bundle_config: database read path is selected but no {REPLICA_URL_ENV}/\
                 {PRIMARY_URL_ENV} is configured"
            );
        }
        match &self.legacy {
            Some(legacy) => Ok((legacy, "hub_api")),
            None => Err(HubClientError::Grpc(tonic::Status::unavailable(
                "no bundle-config read path available (database path disabled or unconfigured, \
                 hub-api not configured)",
            ))),
        }
    }
}

#[async_trait]
impl ConfigSource for RoutedConfigSource {
    /// Lazy-load through the selected read path.
    async fn get(&self, key: &ConfigKey) -> Result<ConfigValue, HubClientError> {
        let (src, path) = self.pick().await?;
        instruments().path.add(1, &[KeyValue::new("path", path)]);
        src.get(key).await
    }

    /// Warm-up load through the selected read path.
    async fn batch_get(&self, keys: &[ConfigKey]) -> Result<Vec<ConfigValue>, HubClientError> {
        let (src, path) = self.pick().await?;
        instruments().path.add(1, &[KeyValue::new("path", path)]);
        src.batch_get(keys).await
    }

    /// Installs the opt-out kill switch (once; later calls are ignored).
    fn set_read_path_gate(&self, gate: Arc<dyn ReadPathGate>) {
        let _ = self.gate.set(gate);
    }
}

/// Builds, installs and returns the stage's cache over the database read path (preferred) and the
/// legacy hub-api gRPC path (behind the `waddles.disable-db-bundle-config-reads` kill switch).
///
/// **Non-fatal**: with neither source configured it logs (ERROR when a configured source failed)
/// and returns `None`, leaving `config_json` as before this feature (`{}`) -- never a stage start
/// failure. With the database configured the stage keeps serving config while hub-api is down or
/// unconfigured: a hub-api connect failure only removes the legacy fallback.
pub async fn connect_and_install(
    endpoint: &str,
    token_endpoint: &str,
    sa_token_path: &str,
    ca_cert_path: Option<&str>,
    valkey: Option<redis::aio::MultiplexedConnection>,
    db: &DbReadConfig,
) -> Option<Arc<BundleConfigCache>> {
    let db_source: Option<Arc<dyn ConfigSource>> = db
        .url
        .clone()
        .map(|url| Arc::new(DbConfigSource::new(url, DB_TIMEOUT)) as Arc<dyn ConfigSource>);
    let legacy: Option<Arc<dyn ConfigSource>> = if endpoint.is_empty() || token_endpoint.is_empty()
    {
        tracing::info!(
            "bundle_config: HUB_API_GRPC_ENDPOINT/SERVICE_JWT_TOKEN_ENDPOINT unset; no legacy \
             hub-api config path"
        );
        None
    } else {
        match BundleConfigClient::connect(endpoint, token_endpoint, sa_token_path, ca_cert_path)
            .await
        {
            Ok(client) => Some(Arc::new(client) as Arc<dyn ConfigSource>),
            Err(err) => {
                tracing::error!(
                    error = %err,
                    "bundle_config: hub-api connect failed; the legacy hub-api config path is \
                     unavailable until the stage restarts"
                );
                None
            }
        }
    };
    if db_source.is_none() && legacy.is_none() {
        tracing::error!(
            "bundle_config: no config source (set {REPLICA_URL_ENV} or {PRIMARY_URL_ENV}, or \
             configure hub-api); bundles receive an EMPTY config_json"
        );
        return None;
    }
    tracing::info!(
        db = db_source.is_some(),
        db_source = ?db.source,
        legacy_hub_api = legacy.is_some(),
        "bundle_config: live community config sources"
    );
    let probe = valkey.map(|c| Arc::new(ValkeyVersionProbe::new(c)) as Arc<dyn VersionProbe>);
    let cache = Arc::new(BundleConfigCache::new(
        Arc::new(RoutedConfigSource::new(db_source, legacy)),
        probe,
        CacheConfig::default(),
    ));
    install_global(Arc::clone(&cache));
    tracing::info!("bundle_config: live community config cache installed");
    Some(cache)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use hub_client::bundle_config::{StaticConfigSource, StaticReadPathGate};

    fn key() -> ConfigKey {
        ConfigKey::new(1, 7, "waddles.core.app.calendar")
    }

    fn source(json: &str) -> Arc<dyn ConfigSource> {
        let s = StaticConfigSource::new();
        s.set(key(), 1, json);
        Arc::new(s)
    }

    #[test]
    fn replica_dsn_wins_and_primary_is_the_fallback() {
        let both = DbReadConfig::from_values(Some("r".into()), Some("p".into()));
        assert_eq!(
            (both.url.as_deref(), both.source),
            (Some("r"), Some(DsnSource::Replica))
        );
        let primary = DbReadConfig::from_values(Some("  ".into()), Some("p".into()));
        assert_eq!(
            (primary.url.as_deref(), primary.source),
            (Some("p"), Some(DsnSource::Primary))
        );
        let none = DbReadConfig::from_values(None, Some(String::new()));
        assert!(none.url.is_none() && none.source.is_none());
    }

    #[test]
    fn redact_scrubs_the_dsn_and_the_password() {
        let url = "postgresql://role:s3cretpw@db.internal:5432/waddles";
        let msg = format!("error connecting to {url}: password s3cretpw rejected");
        let out = redact(&msg, url);
        assert!(!out.contains("s3cretpw") && !out.contains("role:"), "{out}");
    }

    /// Unset gate uses the database; the kill switch sends calls to hub-api.
    #[tokio::test]
    async fn router_prefers_db_and_honours_the_kill_switch() {
        let routed = RoutedConfigSource::new(
            Some(source(r#"{"via":"db"}"#)),
            Some(source(r#"{"via":"hub"}"#)),
        );
        assert_eq!(
            routed.get(&key()).await.unwrap().config_json,
            r#"{"via":"db"}"#
        );
        routed.set_read_path_gate(Arc::new(StaticReadPathGate(false)));
        assert_eq!(
            routed.get(&key()).await.unwrap().config_json,
            r#"{"via":"hub"}"#
        );
        assert_eq!(
            routed.batch_get(&[key()]).await.unwrap()[0].config_json,
            r#"{"via":"hub"}"#
        );
    }

    /// No database source (misconfigured) falls to the legacy path; neither -> a loud error.
    #[tokio::test]
    async fn router_without_a_db_source_uses_legacy_else_errors() {
        let legacy_only = RoutedConfigSource::new(None, Some(source(r#"{"via":"hub"}"#)));
        assert_eq!(
            legacy_only.get(&key()).await.unwrap().config_json,
            r#"{"via":"hub"}"#
        );
        let none = RoutedConfigSource::new(None, None);
        assert!(none.get(&key()).await.is_err());
        let db_only = RoutedConfigSource::new(Some(source("{}")), None);
        db_only.set_read_path_gate(Arc::new(StaticReadPathGate(false)));
        assert!(
            db_only.get(&key()).await.is_err(),
            "kill switch ON with no hub-api: an error, not a silent DB read"
        );
    }
    /// Install: neither source -> `None`; DB only (hub-api unset, or configured but unreachable)
    /// -> a cache that serves from the database path; the unreachable hub-api never blocks it.
    #[tokio::test]
    async fn install_is_non_fatal_and_database_only_when_hub_api_is_unavailable() {
        let none = DbReadConfig::from_values(None, None);
        assert!(connect_and_install("", "", "", None, None, &none)
            .await
            .is_none());
        // hub-api configured but unreachable (connection refused) and no database: still `None`.
        assert!(connect_and_install(
            "https://127.0.0.1:1",
            "https://127.0.0.1:1/token",
            "/nonexistent/sa-token",
            None,
            None,
            &none,
        )
        .await
        .is_none());
        // A database DSN (never connected until first use) installs a cache with or without hub-api.
        let db = DbReadConfig::from_values(Some("postgresql://u:p@127.0.0.1:1/x".into()), None);
        let cache = connect_and_install("", "", "", None, None, &db).await;
        assert!(cache.is_some_and(|c| c.is_empty()));
        let cache = connect_and_install(
            "https://127.0.0.1:1",
            "https://127.0.0.1:1/token",
            "/nonexistent/sa-token",
            None,
            None,
            &db,
        )
        .await;
        assert!(cache.is_some());
    }

    /// The fast-fail window opens after a failed read and clears after a good one.
    #[test]
    fn failure_backoff_opens_and_clears() {
        let src = DbConfigSource::new(
            "postgresql://u:p@127.0.0.1:1/x".into(),
            Duration::from_secs(1),
        )
        .with_failure_backoff(Duration::from_secs(60));
        assert!(!src.backing_off());
        src.note_outcome(true);
        assert!(src.backing_off());
        src.note_outcome(false);
        assert!(!src.backing_off());
    }

    /// An unreachable database is an `UNAVAILABLE` status (never a panic), and the second call
    /// inside the backoff window fails fast without another connect attempt.
    #[tokio::test]
    async fn unreachable_database_is_unavailable_then_fails_fast() {
        let src = DbConfigSource::new(
            "postgresql://u:p@127.0.0.1:1/x".into(),
            Duration::from_millis(500),
        )
        .with_failure_backoff(Duration::from_secs(60));
        let key = ConfigKey::new(1, 7, "waddles.core.app.calendar");
        let first = src.get(&key).await.unwrap_err();
        let second = src.batch_get(&[key]).await.unwrap_err();
        assert!(
            first.to_string().contains("database read failed"),
            "{first}"
        );
        assert!(second.to_string().contains("failing fast"), "{second}");
        assert_eq!(src.query_count(), 0, "no statement ever reached a server");
    }
}
