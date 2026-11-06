//! Job store + per-community limiter. Valkey in production (community-scoped
//! keys, TTL), in-memory for tests. A job record carries its owning `app_id`; a
//! lookup from another bundle/community is indistinguishable from "no such job".

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;

use async_trait::async_trait;
use redis::aio::MultiplexedConnection;
use serde::{Deserialize, Serialize};

use crate::types::{MediaError, MediaKind, Scope};

/// Persisted job state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "lowercase")]
pub enum RecState {
    /// Running.
    Pending,
    /// Finished with an object handle.
    Done {
        /// Rendered [`crate::types::Handle`].
        handle: String,
    },
    /// Failed.
    Failed {
        /// [`MediaError::code`].
        code: String,
        /// [`MediaError::detail`].
        detail: String,
    },
}

/// One persisted job.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JobRecord {
    /// Owning bundle.
    pub app_id: String,
    /// What is being generated.
    pub kind: MediaKind,
    /// Epoch-ms after which a still-pending job reads as `timeout`.
    pub deadline_ms: u64,
    /// State.
    #[serde(flatten)]
    pub state: RecState,
}

/// Job persistence and limits.
#[async_trait]
pub trait JobStore: Send + Sync {
    /// Upserts a record with `ttl_secs` expiry.
    async fn put(
        &self,
        scope: &Scope,
        job_id: &str,
        rec: &JobRecord,
        ttl_secs: u64,
    ) -> Result<(), MediaError>;
    /// Reads a record scoped to (tenant, community).
    async fn get(&self, scope: &Scope, job_id: &str) -> Result<Option<JobRecord>, MediaError>;
    /// Fixed-window per-community submit limiter; `false` = over the limit.
    async fn acquire_rate(
        &self,
        scope: &Scope,
        max_per_minute: u32,
        now_ms: u64,
    ) -> Result<bool, MediaError>;
    /// Registers an in-flight job unless the per-community cap is reached.
    async fn inflight_start(
        &self,
        scope: &Scope,
        job_id: &str,
        deadline_ms: u64,
        now_ms: u64,
        cap: u32,
    ) -> Result<bool, MediaError>;
    /// Releases an in-flight slot.
    async fn inflight_end(&self, scope: &Scope, job_id: &str) -> Result<(), MediaError>;
}

fn job_key(s: &Scope, id: &str) -> String {
    format!("media:job:{}:{}:{}", s.tenant_id, s.community_id, id)
}
fn rate_key(s: &Scope, window: u64) -> String {
    format!("media:rate:{}:{}:{}", s.tenant_id, s.community_id, window)
}
fn inflight_key(s: &Scope) -> String {
    format!("media:inflight:{}:{}", s.tenant_id, s.community_id)
}

fn unavailable(e: impl std::fmt::Display) -> MediaError {
    tracing::warn!(error = %e, "media job store error");
    MediaError::Unavailable("job store".into())
}

/// Valkey-backed store.
#[derive(Clone)]
pub struct ValkeyJobStore {
    conn: MultiplexedConnection,
}

impl ValkeyJobStore {
    /// Wraps a shared multiplexed connection.
    pub fn new(conn: MultiplexedConnection) -> Self {
        Self { conn }
    }
}

const INFLIGHT_LUA: &str = r#"
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"#;

#[async_trait]
impl JobStore for ValkeyJobStore {
    async fn put(
        &self,
        scope: &Scope,
        job_id: &str,
        rec: &JobRecord,
        ttl_secs: u64,
    ) -> Result<(), MediaError> {
        let body = serde_json::to_string(rec).map_err(unavailable)?;
        let mut c = self.conn.clone();
        redis::cmd("SET")
            .arg(job_key(scope, job_id))
            .arg(body)
            .arg("EX")
            .arg(ttl_secs)
            .query_async::<()>(&mut c)
            .await
            .map_err(unavailable)
    }

    async fn get(&self, scope: &Scope, job_id: &str) -> Result<Option<JobRecord>, MediaError> {
        let mut c = self.conn.clone();
        let raw: Option<String> = redis::cmd("GET")
            .arg(job_key(scope, job_id))
            .query_async(&mut c)
            .await
            .map_err(unavailable)?;
        match raw {
            None => Ok(None),
            Some(s) => serde_json::from_str(&s).map(Some).map_err(unavailable),
        }
    }

    async fn acquire_rate(
        &self,
        scope: &Scope,
        max_per_minute: u32,
        now_ms: u64,
    ) -> Result<bool, MediaError> {
        let key = rate_key(scope, now_ms / 60_000);
        let mut c = self.conn.clone();
        let n: i64 = redis::cmd("INCR")
            .arg(&key)
            .query_async(&mut c)
            .await
            .map_err(unavailable)?;
        if n == 1 {
            redis::cmd("EXPIRE")
                .arg(&key)
                .arg(120)
                .query_async::<()>(&mut c)
                .await
                .map_err(unavailable)?;
        }
        Ok(n <= i64::from(max_per_minute))
    }

    async fn inflight_start(
        &self,
        scope: &Scope,
        job_id: &str,
        deadline_ms: u64,
        now_ms: u64,
        cap: u32,
    ) -> Result<bool, MediaError> {
        let mut c = self.conn.clone();
        let ok: i64 = redis::Script::new(INFLIGHT_LUA)
            .key(inflight_key(scope))
            .arg(now_ms)
            .arg(cap)
            .arg(deadline_ms)
            .arg(job_id)
            .arg(3600)
            .invoke_async(&mut c)
            .await
            .map_err(unavailable)?;
        Ok(ok == 1)
    }

    async fn inflight_end(&self, scope: &Scope, job_id: &str) -> Result<(), MediaError> {
        let mut c = self.conn.clone();
        redis::cmd("ZREM")
            .arg(inflight_key(scope))
            .arg(job_id)
            .query_async::<()>(&mut c)
            .await
            .map_err(unavailable)
    }
}

/// In-memory store for tests.
#[derive(Default)]
pub struct InMemoryJobStore {
    jobs: Mutex<HashMap<String, JobRecord>>,
    rate: Mutex<HashMap<String, u32>>,
    inflight: Mutex<HashMap<String, HashSet<String>>>,
}

fn poisoned<T>(_: T) -> MediaError {
    MediaError::Unavailable("job store poisoned".into())
}

#[async_trait]
impl JobStore for InMemoryJobStore {
    async fn put(
        &self,
        scope: &Scope,
        job_id: &str,
        rec: &JobRecord,
        _ttl: u64,
    ) -> Result<(), MediaError> {
        self.jobs
            .lock()
            .map_err(poisoned)?
            .insert(job_key(scope, job_id), rec.clone());
        Ok(())
    }
    async fn get(&self, scope: &Scope, job_id: &str) -> Result<Option<JobRecord>, MediaError> {
        Ok(self
            .jobs
            .lock()
            .map_err(poisoned)?
            .get(&job_key(scope, job_id))
            .cloned())
    }
    async fn acquire_rate(&self, scope: &Scope, max: u32, now_ms: u64) -> Result<bool, MediaError> {
        let mut g = self.rate.lock().map_err(poisoned)?;
        let n = g.entry(rate_key(scope, now_ms / 60_000)).or_insert(0);
        *n += 1;
        Ok(*n <= max)
    }
    async fn inflight_start(
        &self,
        scope: &Scope,
        job_id: &str,
        _deadline: u64,
        _now: u64,
        cap: u32,
    ) -> Result<bool, MediaError> {
        let mut g = self.inflight.lock().map_err(poisoned)?;
        let set = g.entry(inflight_key(scope)).or_default();
        if set.len() >= cap as usize {
            return Ok(false);
        }
        set.insert(job_id.to_string());
        Ok(true)
    }
    async fn inflight_end(&self, scope: &Scope, job_id: &str) -> Result<(), MediaError> {
        if let Some(set) = self
            .inflight
            .lock()
            .map_err(poisoned)?
            .get_mut(&inflight_key(scope))
        {
            set.remove(job_id);
        }
        Ok(())
    }
}
