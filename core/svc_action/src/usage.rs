//! Workstream usage metering (spec §5.12, D31): batches per-`(tenant_id,
//! community_id, workstream_id, stage, app_id)` deltas and `XADD`s them to
//! `waddles:usage` at most every `METERING_FLUSH_INTERVAL_S` -- this
//! service is **write-only** on that stream (spec §11.10.2: "Stages are
//! write-only on this stream: they `XADD` and never read it back").
//!
//! Not `penguin_spine::SpineClient::append`: that method is typed to
//! `StageEnvelope`, and a usage delta is a different JSON shape entirely
//! (spec §5.12's `(tenant_id, community_id, workstream_id, stage, app_id)`
//! key plus counters, not an envelope) -- see `Cargo.toml`'s dependency
//! comment for why this crate opens a second, direct `redis` connection
//! for this and for `crate::capabilities`'s `relay` `LPUSH`.

use redis::streams::StreamMaxlen;
use redis::{AsyncCommands, IntoConnectionInfo};

/// One stage's usage delta for a single `(tenant, community, workstream,
/// app_id)` tuple, accumulated in memory and flushed periodically (spec
/// §5.12: "batched ... not per event, so a chatty channel does not
/// multiply the write rate").
#[derive(Debug, Clone, Default, PartialEq)]
pub struct UsageDelta {
    pub tenant_id: String,
    pub community_id: Option<String>,
    pub workstream_id: String,
    pub app_id: String,
    /// Always `"action"` for this service; kept as a field (not a
    /// hardcoded literal in the serialized record) so a future shared
    /// `penguin-spine` usage type can be dropped in without a shape change.
    pub stage: &'static str,
    pub actions_delivered: u64,
    /// Bundle/host-call counts keyed by kind (spec §5.12: "host calls by
    /// kind (`http`/`kv`/`db`/`relay`/`flags`/`log`)").
    pub host_calls_relay: u64,
    pub outbound_bytes: u64,
}

impl UsageDelta {
    pub fn new(
        tenant_id: impl Into<String>,
        community_id: Option<String>,
        workstream_id: impl Into<String>,
        app_id: impl Into<String>,
    ) -> Self {
        Self {
            tenant_id: tenant_id.into(),
            community_id,
            workstream_id: workstream_id.into(),
            app_id: app_id.into(),
            stage: "action",
            actions_delivered: 0,
            host_calls_relay: 0,
            outbound_bytes: 0,
        }
    }

    /// Merges `other`'s counters into `self` (same key assumed -- callers
    /// batch by key before merging).
    pub fn merge(&mut self, other: &UsageDelta) {
        self.actions_delivered += other.actions_delivered;
        self.host_calls_relay += other.host_calls_relay;
        self.outbound_bytes += other.outbound_bytes;
    }

    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "tenant_id": self.tenant_id,
            "community_id": self.community_id,
            "workstream_id": self.workstream_id,
            "app_id": self.app_id,
            "stage": self.stage,
            "actions_delivered": self.actions_delivered,
            "host_calls_relay": self.host_calls_relay,
            "outbound_bytes": self.outbound_bytes,
        })
    }
}

/// The `waddles:usage` stream key (spec §6.2).
pub const USAGE_STREAM_KEY: &str = "waddles:usage";

/// Errors `UsageSink` operations raise.
#[derive(Debug, thiserror::Error)]
pub enum UsageError {
    #[error("valkey command failed: {0}")]
    Redis(#[from] redis::RedisError),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
}

/// Where a usage delta is written -- narrow trait so `crate::dispatch`'s
/// flush logic is unit-testable against a fake sink without a live Valkey
/// server.
pub trait UsageSink: Send + Sync {
    fn write(
        &self,
        delta: &UsageDelta,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), UsageError>> + Send + '_>>;
}

/// The real sink: `XADD waddles:usage MAXLEN ~ {maxlen} * usage {json}`
/// (spec §6.2's entry-payload convention -- one field per stream, here
/// named `usage` since `env`/`rec` are already spoken for by the envelope
/// and DLQ streams respectively).
#[derive(Debug)]
pub struct RedisUsageSink {
    conn: redis::aio::MultiplexedConnection,
    maxlen: u64,
}

impl RedisUsageSink {
    pub fn new(conn: redis::aio::MultiplexedConnection, maxlen: u64) -> Self {
        Self { conn, maxlen }
    }
}

impl UsageSink for RedisUsageSink {
    fn write(
        &self,
        delta: &UsageDelta,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), UsageError>> + Send + '_>>
    {
        let json = delta.to_json().to_string();
        let mut conn = self.conn.clone();
        let maxlen = StreamMaxlen::Approx(self.maxlen as usize);
        Box::pin(async move {
            let _id: Option<String> = conn
                .xadd_maxlen(USAGE_STREAM_KEY, maxlen, "*", &[("usage", json.as_str())])
                .await?;
            Ok(())
        })
    }
}

/// Installs the process-level rustls `CryptoProvider` (`ring` backend)
/// exactly once. `redis`'s `tls-rustls` feature enables `dep:rustls` but
/// requests no default provider itself -- the first TLS handshake this
/// connection attempts panics without one. Duplicates
/// `penguin_spine::config::ensure_crypto_provider_installed` byte-for-byte
/// (that helper is `pub(crate)` there, unreachable from this crate) -- see
/// the module doc for why this crate opens its own, second Valkey
/// connection instead of reusing `penguin_spine::SpineClient`'s.
static CRYPTO_PROVIDER_INIT: std::sync::Once = std::sync::Once::new();

fn ensure_crypto_provider_installed() {
    CRYPTO_PROVIDER_INIT.call_once(|| {
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

/// Opens the direct Valkey connection [`RedisUsageSink`] writes through,
/// built from the same `VALKEY_URL`/username/password/TLS/CA-file settings
/// `penguin_spine::SpineClient` connects with (spec §12.7 delegates spine
/// tuning to `penguin_spine::SpineConfig::from_env` rather than
/// re-declaring it here) -- never through `SpineClient` itself, since
/// `XADD waddles:usage` is not envelope-typed (module doc).
pub async fn connect(
    cfg: &penguin_spine::SpineConfig,
) -> Result<redis::aio::MultiplexedConnection, UsageError> {
    let info: redis::ConnectionInfo = cfg.valkey_url.as_str().into_connection_info()?;
    let mut settings = info.redis_settings().clone();
    if let Some(username) = &cfg.valkey_username {
        settings = settings.set_username(username);
    }
    if let Some(password) = &cfg.valkey_password {
        settings = settings.set_password(password);
    }
    let info = info.set_redis_settings(settings);

    let client = if cfg.security_transport_tls {
        ensure_crypto_provider_installed();
        let root_cert = std::fs::read(&cfg.valkey_ca_file).ok();
        redis::Client::build_with_tls(
            info,
            redis::TlsCertificates {
                client_tls: None,
                root_cert,
            },
        )?
    } else {
        redis::Client::open(info)?
    };
    Ok(client.get_multiplexed_async_connection().await?)
}

/// Convenience wrapper around [`connect`] that also builds the
/// [`RedisUsageSink`], bounded by the same `SPINE_STREAM_MAXLEN` every
/// other spine-adjacent stream uses (spec §6.2's usage-stream row) rather
/// than a separate, undocumented limit.
pub async fn connect_sink(cfg: &penguin_spine::SpineConfig) -> Result<RedisUsageSink, UsageError> {
    let conn = connect(cfg).await?;
    Ok(RedisUsageSink::new(conn, cfg.stream_maxlen))
}

/// Accumulates deltas by key in memory and flushes them (merged, one
/// `XADD` per key) on demand -- the batching half of spec §5.12's "Deltas
/// are batched and `XADD`ed ... at most every `METERING_FLUSH_INTERVAL_S`
/// ... per stage replica". The interval-driven flush loop itself is
/// `crate::dispatch`'s responsibility (a `tokio::time::interval` calling
/// [`UsageBatcher::flush`]); this type owns only the accumulate/merge/drain
/// logic so it is trivially unit-testable.
#[derive(Default)]
pub struct UsageBatcher {
    pending: std::collections::HashMap<(String, Option<String>, String, String), UsageDelta>,
}

impl UsageBatcher {
    pub fn new() -> Self {
        Self::default()
    }

    /// Records one action delivery against its `(tenant, community,
    /// workstream, app_id)` key, merging into any already-pending delta
    /// for that key.
    pub fn record_action_delivered(
        &mut self,
        tenant_id: &str,
        community_id: Option<&str>,
        workstream_id: &str,
        app_id: &str,
    ) {
        let key = (
            tenant_id.to_string(),
            community_id.map(str::to_string),
            workstream_id.to_string(),
            app_id.to_string(),
        );
        let entry = self.pending.entry(key).or_insert_with(|| {
            UsageDelta::new(
                tenant_id,
                community_id.map(str::to_string),
                workstream_id,
                app_id,
            )
        });
        entry.actions_delivered += 1;
    }

    /// Records one `relay` host call against its key (spec §5.12: "host
    /// calls by kind").
    pub fn record_relay_call(
        &mut self,
        tenant_id: &str,
        community_id: Option<&str>,
        workstream_id: &str,
        app_id: &str,
        bytes: u64,
    ) {
        let key = (
            tenant_id.to_string(),
            community_id.map(str::to_string),
            workstream_id.to_string(),
            app_id.to_string(),
        );
        let entry = self.pending.entry(key).or_insert_with(|| {
            UsageDelta::new(
                tenant_id,
                community_id.map(str::to_string),
                workstream_id,
                app_id,
            )
        });
        entry.host_calls_relay += 1;
        entry.outbound_bytes += bytes;
    }

    /// Number of distinct keys with a pending, unflushed delta.
    pub fn pending_len(&self) -> usize {
        self.pending.len()
    }

    /// Drains every pending delta and writes it through `sink`, returning
    /// the count successfully flushed. A single key's write failure is
    /// logged and that key's delta is dropped rather than retried
    /// indefinitely -- usage metering is best-effort accounting (spec
    /// §5.12: "there is no charging, quota or enforcement wired to it"),
    /// never a reason to block or fail the dispatch path it instruments.
    pub async fn flush(&mut self, sink: &dyn UsageSink) -> usize {
        let pending = std::mem::take(&mut self.pending);
        let mut flushed = 0;
        for (_key, delta) in pending {
            match sink.write(&delta).await {
                Ok(()) => flushed += 1,
                Err(err) => {
                    tracing::warn!(
                        error = %err,
                        tenant_id = %delta.tenant_id,
                        app_id = %delta.app_id,
                        "usage delta flush failed, dropping (best-effort metering)"
                    );
                }
            }
        }
        flushed
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// A syntactically valid [`penguin_spine::SpineConfig`] that never
    /// actually connects -- `security_transport_tls`/`_auth` disabled so
    /// `valkey_url` needs no credentials, and port `1` (privileged, no
    /// listener in any CI/dev sandbox) refuses the TCP connection
    /// immediately rather than timing out. Field-for-field identical to
    /// `core/svc_process/src/spine.rs`'s own `unreachable_spine_config`
    /// fixture (same pinned `penguin-spine` rev, same rationale).
    fn unreachable_spine_config() -> penguin_spine::SpineConfig {
        penguin_spine::SpineConfig {
            valkey_url: "redis://127.0.0.1:1/".to_string(),
            valkey_username: None,
            valkey_password: None,
            valkey_ca_file: std::path::PathBuf::from("/nonexistent-ca.crt"),
            security_transport_tls: false,
            security_transport_auth: false,
            consumer_id: "test-consumer".to_string(),
            stream_maxlen: 100,
            read_count: 1,
            block_ms: 1_000,
            claim_idle_ms: 30_000,
            claim_interval_ms: 15_000,
            stats_interval_ms: 10_000,
            pel_alert: 5_000,
            dlq_maxlen: 100,
            max_deliveries: 5,
            drain_socket_timeout_s: 65,
            relay_block_timeout_s: 30,
        }
    }

    #[tokio::test]
    async fn connect_fails_fast_against_an_unreachable_valkey() {
        let cfg = unreachable_spine_config();
        let err = connect(&cfg).await.unwrap_err();
        assert!(matches!(err, UsageError::Redis(_)));
    }

    #[tokio::test]
    async fn connect_sink_propagates_the_same_connect_failure() {
        let cfg = unreachable_spine_config();
        let err = connect_sink(&cfg).await.unwrap_err();
        assert!(matches!(err, UsageError::Redis(_)));
    }

    #[derive(Default)]
    struct FakeSink {
        written: Mutex<Vec<UsageDelta>>,
        fail_for_app: Option<String>,
    }

    impl UsageSink for FakeSink {
        fn write(
            &self,
            delta: &UsageDelta,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), UsageError>> + Send + '_>>
        {
            let delta = delta.clone();
            Box::pin(async move {
                if self.fail_for_app.as_deref() == Some(delta.app_id.as_str()) {
                    return Err(UsageError::Json(
                        serde_json::from_str::<serde_json::Value>("{").unwrap_err(),
                    ));
                }
                self.written.lock().unwrap().push(delta);
                Ok(())
            })
        }
    }

    #[test]
    fn usage_delta_to_json_has_expected_shape() {
        let mut delta = UsageDelta::new("acme", Some("main".to_string()), "ws-1", "waddles.a.b.c");
        delta.actions_delivered = 3;
        let json = delta.to_json();
        assert_eq!(json["tenant_id"], "acme");
        assert_eq!(json["community_id"], "main");
        assert_eq!(json["workstream_id"], "ws-1");
        assert_eq!(json["stage"], "action");
        assert_eq!(json["actions_delivered"], 3);
    }

    #[test]
    fn usage_delta_merge_sums_counters() {
        let mut a = UsageDelta::new("acme", None, "ws-1", "app");
        a.actions_delivered = 2;
        a.host_calls_relay = 1;
        let mut b = UsageDelta::new("acme", None, "ws-1", "app");
        b.actions_delivered = 3;
        b.host_calls_relay = 4;
        a.merge(&b);
        assert_eq!(a.actions_delivered, 5);
        assert_eq!(a.host_calls_relay, 5);
    }

    #[test]
    fn batcher_merges_multiple_records_for_the_same_key() {
        let mut batcher = UsageBatcher::new();
        batcher.record_action_delivered("acme", Some("main"), "ws-1", "waddles.a.b.c");
        batcher.record_action_delivered("acme", Some("main"), "ws-1", "waddles.a.b.c");
        assert_eq!(batcher.pending_len(), 1);
    }

    #[test]
    fn batcher_tracks_distinct_keys_separately() {
        let mut batcher = UsageBatcher::new();
        batcher.record_action_delivered("acme", Some("main"), "ws-1", "waddles.a.b.c");
        batcher.record_action_delivered("acme", Some("other"), "ws-2", "waddles.a.b.c");
        assert_eq!(batcher.pending_len(), 2);
    }

    #[tokio::test]
    async fn flush_writes_every_pending_delta_and_drains_the_batch() {
        let mut batcher = UsageBatcher::new();
        batcher.record_action_delivered("acme", Some("main"), "ws-1", "waddles.a.b.c");
        batcher.record_relay_call("acme", Some("main"), "ws-1", "waddles.a.b.c", 42);
        let sink = FakeSink::default();
        let flushed = batcher.flush(&sink).await;
        assert_eq!(flushed, 1);
        assert_eq!(batcher.pending_len(), 0);
        let written = sink.written.lock().unwrap();
        assert_eq!(written.len(), 1);
        assert_eq!(written[0].actions_delivered, 1);
        assert_eq!(written[0].host_calls_relay, 1);
        assert_eq!(written[0].outbound_bytes, 42);
    }

    #[tokio::test]
    async fn flush_on_empty_batcher_writes_nothing() {
        let mut batcher = UsageBatcher::new();
        let sink = FakeSink::default();
        assert_eq!(batcher.flush(&sink).await, 0);
    }

    #[tokio::test]
    async fn flush_drops_a_failed_key_without_blocking_the_others() {
        let mut batcher = UsageBatcher::new();
        batcher.record_action_delivered("acme", None, "ws-1", "waddles.bad.app");
        batcher.record_action_delivered("acme", None, "ws-2", "waddles.good.app");
        let sink = FakeSink {
            fail_for_app: Some("waddles.bad.app".to_string()),
            ..Default::default()
        };
        let flushed = batcher.flush(&sink).await;
        assert_eq!(flushed, 1);
        let written = sink.written.lock().unwrap();
        assert_eq!(written[0].app_id, "waddles.good.app");
    }
}
