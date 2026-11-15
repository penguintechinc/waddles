//! The verifying-key cache state machine. See the crate docs for the behaviour contract.

use std::collections::HashMap;
use std::sync::{Arc, PoisonError, RwLock};
use std::time::{Duration, SystemTime};

use opentelemetry::trace::{Span, Status, Tracer};
use opentelemetry::KeyValue;
use tokio::sync::{watch, Mutex};
use tokio::task::JoinHandle;

use crate::metrics::{Metrics, Reason};
use crate::{Clock, KeyError, KeyLoader, KeyRecord, KeyState, SetupError, SystemClock};

/// Longest kid accepted. Longer ids cannot exist (hub-api issues short ids) and are refused
/// without touching the database or the negative cache, which bounds attacker-chosen memory.
const MAX_KID_LEN: usize = 128;
/// Longest kid echoed into a log line.
const LOG_KID_LEN: usize = 64;

/// Tunables of the cache. [`CacheConfig::new`] sets the design defaults.
#[derive(Debug, Clone)]
pub struct CacheConfig {
    /// Key type codes that must each have a usable key before the cache reports ready.
    pub required_types: Vec<i16>,
    /// How long an unknown kid stays negatively cached (design: 30 s).
    pub negative_ttl: Duration,
    /// Interval of the background snapshot refresh (design: 60 s).
    pub poll_interval: Duration,
    /// Verify window added to `rotated_at` to get `verify_until` for verify-only keys.
    pub verify_window: Duration,
    /// A verify-only key is refused when the last successful store read is older than this
    /// (security review M6: 15 min).
    pub max_staleness: Duration,
    /// First delay of the seed backoff.
    pub seed_backoff_initial: Duration,
    /// Ceiling of the seed backoff.
    pub seed_backoff_max: Duration,
    /// Maximum negative-cache entries held.
    pub negative_cap: usize,
}

impl CacheConfig {
    /// Design defaults for `required_types` with the given verify window.
    #[must_use]
    pub fn new(required_types: Vec<i16>, verify_window: Duration) -> Self {
        Self {
            required_types,
            negative_ttl: Duration::from_secs(30),
            poll_interval: Duration::from_secs(60),
            verify_window,
            max_staleness: Duration::from_secs(15 * 60),
            seed_backoff_initial: Duration::from_millis(100),
            seed_backoff_max: Duration::from_secs(5),
            negative_cap: 1024,
        }
    }
}

type Slot = (i16, String);

#[derive(Default)]
struct State {
    keys: HashMap<Slot, KeyRecord>,
    negative: HashMap<Slot, SystemTime>,
    /// Time of the last successful store read (refresh or lazy load).
    last_success: Option<SystemTime>,
}

/// Cache of verifying keys read from the replica view. Cheap to share behind an `Arc`.
pub struct KeyCache {
    loader: Arc<dyn KeyLoader>,
    cfg: CacheConfig,
    clock: Arc<dyn Clock>,
    metrics: Metrics,
    state: RwLock<State>,
    /// Serialises lazy loads so a burst of one unknown kid costs one query.
    load_gate: Mutex<()>,
}

/// Replaces control/unusual characters so an attacker-chosen kid cannot forge log lines.
fn log_kid(kid: &str) -> String {
    kid.chars()
        .take(LOG_KID_LEN)
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | ':') {
                c
            } else {
                '?'
            }
        })
        .collect()
}

/// Drops unusable rows and keeps the highest version per (type, kid).
fn index_rows(rows: Vec<KeyRecord>) -> HashMap<Slot, KeyRecord> {
    let mut out: HashMap<Slot, KeyRecord> = HashMap::with_capacity(rows.len());
    for r in rows {
        if r.kid.is_empty() || r.kid.len() > MAX_KID_LEN || r.public_material.is_empty() {
            tracing::warn!(
                key_type = r.key_type,
                version = r.version,
                "skipping unusable key row"
            );
            continue;
        }
        let slot = (r.key_type, r.kid.clone());
        match out.get(&slot) {
            Some(existing) if existing.version >= r.version => {}
            _ => {
                out.insert(slot, r);
            }
        }
    }
    out
}

impl KeyCache {
    /// Builds a cache over `loader` using the global OTel meter and the system clock.
    pub fn new(loader: Arc<dyn KeyLoader>, cfg: CacheConfig) -> Result<Self, SetupError> {
        let meter = opentelemetry::global::meter("platform_keys");
        Self::with_parts(loader, cfg, Arc::new(SystemClock), &meter)
    }

    /// Builds a cache with an explicit clock and meter (tests, custom providers).
    /// Rejects an empty required-type set: a readiness gate over zero types proves nothing.
    pub fn with_parts(
        loader: Arc<dyn KeyLoader>,
        cfg: CacheConfig,
        clock: Arc<dyn Clock>,
        meter: &opentelemetry::metrics::Meter,
    ) -> Result<Self, SetupError> {
        if cfg.required_types.is_empty() {
            return Err(SetupError::Config(
                "required_types must name at least one key type".to_string(),
            ));
        }
        Ok(Self {
            loader,
            cfg,
            clock,
            metrics: Metrics::new(meter),
            state: RwLock::new(State::default()),
            load_gate: Mutex::new(()),
        })
    }

    fn read(&self) -> std::sync::RwLockReadGuard<'_, State> {
        self.state.read().unwrap_or_else(PoisonError::into_inner)
    }

    fn write(&self) -> std::sync::RwLockWriteGuard<'_, State> {
        self.state.write().unwrap_or_else(PoisonError::into_inner)
    }

    /// Applies the verify-window and staleness rules to a cached record.
    fn check_usable(&self, state: &State, rec: &KeyRecord) -> Result<(), KeyError> {
        if rec.state == KeyState::Active {
            return Ok(());
        }
        let now = self.clock.now();
        match rec.verify_until(self.cfg.verify_window) {
            Some(until) if now <= until => {}
            _ => return Err(KeyError::Expired),
        }
        let fresh = state.last_success.is_some_and(|t| {
            now.duration_since(t).unwrap_or(Duration::ZERO) <= self.cfg.max_staleness
        });
        if fresh {
            Ok(())
        } else {
            Err(KeyError::Stale)
        }
    }

    /// `Some` when the key is cached (usable or not), `None` when absent. Records hit/reject.
    fn lookup_cached(&self, slot: &Slot) -> Option<Result<KeyRecord, KeyError>> {
        let state = self.read();
        let rec = state.keys.get(slot)?;
        let res = self.check_usable(&state, rec).map(|()| rec.clone());
        match &res {
            Ok(_) => self.metrics.hit(slot.0),
            Err(KeyError::Expired) => self.metrics.rejected(slot.0, Reason::Expired),
            Err(_) => self.metrics.rejected(slot.0, Reason::Stale),
        }
        if let Err(e) = &res {
            tracing::warn!(key_type = slot.0, kid = %log_kid(&slot.1), version = rec.version, error = %e, "verifying key refused");
        }
        Some(res)
    }

    fn negative_fresh(&self, slot: &Slot) -> bool {
        let now = self.clock.now();
        self.read().negative.get(slot).is_some_and(|exp| *exp > now)
    }

    fn refuse_unknown(&self, key_type: i16, kid: &str) -> KeyError {
        self.metrics.unknown_kid(key_type);
        self.metrics.rejected(key_type, Reason::UnknownKid);
        tracing::warn!(key_type, kid = %log_kid(kid), "unknown key id refused");
        KeyError::UnknownKid
    }

    /// Looks up the verifying key for (`key_type`, `kid`). Fails closed: an unknown kid, an
    /// expired or stale verify-only key, or an unreadable store with no cached copy are all
    /// errors, and the caller must reject the signature.
    pub async fn get(&self, key_type: i16, kid: &str) -> Result<KeyRecord, KeyError> {
        if kid.is_empty() || kid.len() > MAX_KID_LEN {
            self.metrics.miss(key_type);
            return Err(self.refuse_unknown(key_type, kid));
        }
        let slot: Slot = (key_type, kid.to_owned());
        if let Some(res) = self.lookup_cached(&slot) {
            return res;
        }
        self.metrics.miss(key_type);
        if self.negative_fresh(&slot) {
            return Err(self.refuse_unknown(key_type, kid));
        }
        let _gate = self.load_gate.lock().await;
        if let Some(res) = self.lookup_cached(&slot) {
            return res;
        }
        if self.negative_fresh(&slot) {
            return Err(self.refuse_unknown(key_type, kid));
        }
        match self.loader.load_kid(kid).await {
            Ok(rows) => {
                let loaded = index_rows(rows.into_iter().filter(|r| r.kid == kid).collect());
                {
                    let mut st = self.write();
                    st.last_success = Some(self.clock.now());
                    st.keys.extend(loaded);
                }
                if let Some(res) = self.lookup_cached(&slot) {
                    return res;
                }
                self.set_negative(slot);
                Err(self.refuse_unknown(key_type, kid))
            }
            Err(e) => {
                self.metrics.refresh_failure();
                self.metrics.rejected(key_type, Reason::Unavailable);
                tracing::warn!(key_type, kid = %log_kid(kid), error = %e, "lazy key load failed");
                Err(KeyError::Unavailable)
            }
        }
    }

    fn set_negative(&self, slot: Slot) {
        let now = self.clock.now();
        let expiry = now.checked_add(self.cfg.negative_ttl).unwrap_or(now);
        let mut st = self.write();
        if st.negative.len() >= self.cfg.negative_cap {
            st.negative.retain(|_, exp| *exp > now);
            if st.negative.len() >= self.cfg.negative_cap {
                st.negative.clear();
            }
        }
        st.negative.insert(slot, expiry);
    }

    /// Replaces the snapshot from the store. On failure the last-known keys are KEPT (never
    /// cleared) and the failure is counted; staleness then bounds how long verify-only keys live.
    /// Returns the number of keys now cached.
    pub async fn refresh(&self) -> Result<usize, KeyError> {
        let started = std::time::Instant::now();
        let tracer = opentelemetry::global::tracer("platform_keys");
        let mut span = tracer.start("platform_keys.refresh");
        let res = self.loader.load_all().await;
        let out = match res {
            Ok(rows) => {
                let fresh = index_rows(rows);
                let n = fresh.len();
                {
                    let mut st = self.write();
                    st.negative.retain(|slot, _| !fresh.contains_key(slot));
                    st.keys = fresh;
                    st.last_success = Some(self.clock.now());
                }
                self.metrics.refreshed(started.elapsed().as_secs_f64());
                span.set_attribute(KeyValue::new(
                    "platform_keys.cached",
                    i64::try_from(n).unwrap_or(i64::MAX),
                ));
                tracing::debug!(cached = n, "platform key snapshot refreshed");
                Ok(n)
            }
            Err(e) => {
                self.metrics.refresh_failure();
                span.set_status(Status::error("refresh failed"));
                tracing::warn!(error = %e, "platform key refresh failed; keeping last-known keys");
                Err(KeyError::Unavailable)
            }
        };
        span.end();
        out
    }

    /// Required key types that currently have no usable key (empty = ready).
    #[must_use]
    pub fn missing_types(&self) -> Vec<i16> {
        let st = self.read();
        self.cfg
            .required_types
            .iter()
            .copied()
            .filter(|t| {
                !st.keys
                    .values()
                    .any(|r| r.key_type == *t && self.check_usable(&st, r).is_ok())
            })
            .collect()
    }

    /// Readiness probe: true once every required key type has a usable key.
    #[must_use]
    pub fn ready(&self) -> bool {
        self.missing_types().is_empty()
    }

    /// Seeds the cache at startup, retrying with bounded exponential backoff until every required
    /// type is present or `max_wait` elapses. Never serves from anything but the store: a `db`
    /// source that cannot seed returns an error (fail loud), it does not fall back.
    pub async fn seed(&self, max_wait: Duration) -> Result<(), SetupError> {
        let deadline = tokio::time::Instant::now() + max_wait;
        let mut delay = self.cfg.seed_backoff_initial;
        let mut attempts = 0_u32;
        loop {
            attempts += 1;
            // A failed attempt is already logged and counted by `refresh`.
            let _ = self.refresh().await;
            let missing = self.missing_types();
            if missing.is_empty() {
                return Ok(());
            }
            let now = tokio::time::Instant::now();
            if now >= deadline {
                return Err(SetupError::NotReady { missing, attempts });
            }
            tokio::time::sleep(delay.min(deadline - now)).await;
            delay = (delay * 2).min(self.cfg.seed_backoff_max);
        }
    }

    /// Spawns the periodic refresh. Send `true` on the returned sender (or drop it) to stop it.
    pub fn spawn_poller(self: &Arc<Self>) -> (watch::Sender<bool>, JoinHandle<()>) {
        let (tx, mut rx) = watch::channel(false);
        let cache = Arc::clone(self);
        let handle = tokio::spawn(async move {
            loop {
                tokio::select! {
                    () = tokio::time::sleep(cache.cfg.poll_interval) => {
                        let _ = cache.refresh().await;
                    }
                    changed = rx.changed() => {
                        if changed.is_err() || *rx.borrow() {
                            break;
                        }
                    }
                }
            }
        });
        (tx, handle)
    }
}
