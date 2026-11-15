//! Host-side stateful layer of the Spectrum connector (gh #486).
//!
//! The pinned Spectrum component is stateless, so everything that needs memory
//! lives here, wrapped around the generic [`PollTransport`]:
//!
//! * **Seen-id dedupe + priming** (`lobby` / `forum`): the first successful
//!   poll of a source only PRIMES the seen set (no history flood); afterwards
//!   only ids not yet seen are forwarded, and an id is remembered only after
//!   its frame was published (a dropped frame is re-yielded).
//! * **Snapshot diff** (`roster` / `events`): every cycle pages through the
//!   whole snapshot (page numbers are written by the HOST into the request
//!   body, never by a guest), diffs it against the stored snapshot, and yields
//!   one raw change frame per delta. The snapshot advances only after every
//!   change was published (at-least-once).
//! * **Envelope policy** matching the legacy receiver: a failure envelope or a
//!   changed endpoint shape is a loud terminal error, never an empty poll.
//!
//! State lives behind [`SpectrumStore`] (Valkey in production). Keys are
//! community-scoped and numeric (`..:{tenant}:{community}:{kind}:{opaque}`);
//! values are opaque RSI ids and fingerprints only -- no names, no text, no
//! handles.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{Map, Value};

use super::poll::PollTransport;
use super::secrets::TransportSecretSource;
use super::spectrum_org::{
    count_of, diff_events, diff_roster, event_frame, parse_events, parse_members, roster_frame,
    s_of, DiffError, Parsed,
};
use super::transport::{FrameDropReason, FrameOutcome, Transport, TransportError};
use super::transport_common::FrameRuleMetrics;
use super::types::{ConnectionCtx, Handshake};
use super::webhook::BoxFut;

/// RSI envelope `code` values meaning "your session is not valid".
const AUTH_CODES: [&str; 3] = [
    "ErrApiLoginRequired",
    "ErrApiUnauthorized",
    "ErrApiForbidden",
];
/// Per-store-call deadline.
const STORE_TIMEOUT: Duration = Duration::from_secs(2);

/// What a connection polls.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpectrumKind {
    /// A lobby's recent messages (append-only, deduped).
    Lobby,
    /// A forum channel's threads/replies (append-only, deduped).
    Forum,
    /// An organization's member roster (snapshot, diffed).
    Roster,
    /// An organization's event list (snapshot, diffed).
    Events,
}

impl SpectrumKind {
    /// Stable slug (key component and metric label).
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Lobby => "lobby",
            Self::Forum => "forum",
            Self::Roster => "roster",
            Self::Events => "events",
        }
    }

    /// True for the snapshot-diffed kinds.
    #[must_use]
    pub fn is_org(self) -> bool {
        matches!(self, Self::Roster | Self::Events)
    }
}

/// Host-owned tunables of one Spectrum source (all profile constants).
#[derive(Debug, Clone)]
pub struct SpectrumSpec {
    /// What is polled.
    pub kind: SpectrumKind,
    /// Store key base (`waddles:ingest:spectrum:{tenant}:{community}:{kind}:{opaque}`).
    pub key: String,
    /// Courtesy delay between snapshot pages.
    pub page_delay: Duration,
    /// Page ceiling; still returning data past it is a loud error.
    pub max_pages: u32,
    /// Shrink ratio past which a snapshot is an anomaly.
    pub guard_ratio: f64,
    /// Roster size below which the ratio guard is skipped.
    pub guard_min_size: usize,
    /// Seen ids remembered per source (oldest evicted).
    pub seen_cap: usize,
    /// Store entry lifetime (refreshed on every write).
    pub ttl: Duration,
    /// Consecutive drops of one window before its ids are remembered anyway.
    pub max_drop_attempts: u32,
}

/// A store failure: a fixed class, never backend text.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("spectrum state store failed: {0}")]
pub struct StoreError(pub &'static str);

/// Where dedupe and snapshot state lives. A failure is ALWAYS an error, never
/// read as "nothing stored" (which would re-prime and silently skip changes).
pub trait SpectrumStore: Send + Sync {
    /// Whether `key` has been primed.
    fn is_primed(&self, key: String) -> BoxFut<'_, Result<bool, StoreError>>;
    /// The subset of `ids` not yet seen, in order.
    fn unseen(&self, key: String, ids: Vec<String>) -> BoxFut<'_, Result<Vec<String>, StoreError>>;
    /// Remembers `ids` (capped, oldest evicted) and marks `key` primed.
    fn remember(
        &self,
        key: String,
        ids: Vec<String>,
        cap: usize,
        ttl: Duration,
    ) -> BoxFut<'_, Result<(), StoreError>>;
    /// The stored snapshot JSON, if any.
    fn load_snapshot(&self, key: String) -> BoxFut<'_, Result<Option<String>, StoreError>>;
    /// Stores the snapshot JSON.
    fn save_snapshot(
        &self,
        key: String,
        json: String,
        ttl: Duration,
    ) -> BoxFut<'_, Result<(), StoreError>>;
}

#[derive(Default)]
struct MemEntry {
    primed: bool,
    seen: VecDeque<String>,
    snapshot: Option<String>,
}

/// In-process [`SpectrumStore`] (tests / single-replica fallback): state is
/// lost on restart. Can be told to fail, to exercise the error paths.
#[derive(Default)]
pub struct MemorySpectrumStore {
    data: Mutex<HashMap<String, MemEntry>>,
    fail: Mutex<bool>,
}

impl MemorySpectrumStore {
    /// An empty store.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Makes every later call fail (`true`) or work again (`false`).
    pub fn set_failing(&self, failing: bool) {
        if let Ok(mut f) = self.fail.lock() {
            *f = failing;
        }
    }

    fn check(&self) -> Result<(), StoreError> {
        match self.fail.lock() {
            Ok(f) if !*f => Ok(()),
            Ok(_) => Err(StoreError("unavailable")),
            Err(_) => Err(StoreError("poisoned")),
        }
    }

    fn with<T>(
        &self,
        f: impl FnOnce(&mut HashMap<String, MemEntry>) -> T,
    ) -> Result<T, StoreError> {
        self.check()?;
        let mut g = self.data.lock().map_err(|_| StoreError("poisoned"))?;
        Ok(f(&mut g))
    }
}

impl SpectrumStore for MemorySpectrumStore {
    fn is_primed(&self, key: String) -> BoxFut<'_, Result<bool, StoreError>> {
        Box::pin(async move { self.with(|d| d.get(&key).is_some_and(|e| e.primed)) })
    }

    fn unseen(&self, key: String, ids: Vec<String>) -> BoxFut<'_, Result<Vec<String>, StoreError>> {
        Box::pin(async move {
            self.with(|d| {
                let e = d.entry(key).or_default();
                ids.into_iter().filter(|i| !e.seen.contains(i)).collect()
            })
        })
    }

    fn remember(
        &self,
        key: String,
        ids: Vec<String>,
        cap: usize,
        _ttl: Duration,
    ) -> BoxFut<'_, Result<(), StoreError>> {
        Box::pin(async move {
            self.with(|d| {
                let e = d.entry(key).or_default();
                e.primed = true;
                for i in ids {
                    if !e.seen.contains(&i) {
                        e.seen.push_back(i);
                    }
                }
                while e.seen.len() > cap {
                    e.seen.pop_front();
                }
            })
        })
    }

    fn load_snapshot(&self, key: String) -> BoxFut<'_, Result<Option<String>, StoreError>> {
        Box::pin(async move { self.with(|d| d.get(&key).and_then(|e| e.snapshot.clone())) })
    }

    fn save_snapshot(
        &self,
        key: String,
        json: String,
        _ttl: Duration,
    ) -> BoxFut<'_, Result<(), StoreError>> {
        Box::pin(async move {
            self.with(|d| {
                d.entry(key).or_default().snapshot = Some(json);
            })
        })
    }
}

/// Valkey-backed [`SpectrumStore`]: a sorted set of seen ids (score = insert
/// time, oldest evicted by rank), a `primed` flag and the snapshot string, all
/// under one base key with a refreshed TTL.
pub struct RedisSpectrumStore {
    conn: redis::aio::ConnectionManager,
}

impl RedisSpectrumStore {
    /// Wraps a connected Valkey connection.
    #[must_use]
    pub fn new(conn: redis::aio::ConnectionManager) -> Self {
        Self { conn }
    }
}

/// Runs `fut` under the store deadline, mapping every failure to a fixed
/// class (redis error text can embed the address; never surfaced).
async fn bounded<T>(
    fut: impl std::future::Future<Output = redis::RedisResult<T>>,
) -> Result<T, StoreError> {
    match tokio::time::timeout(STORE_TIMEOUT, fut).await {
        Err(_) => Err(StoreError("timeout")),
        Ok(Err(e)) if e.is_timeout() => Err(StoreError("timeout")),
        Ok(Err(e)) if e.is_connection_refusal() || e.is_connection_dropped() => {
            Err(StoreError("connection"))
        }
        Ok(Err(_)) => Err(StoreError("command")),
        Ok(Ok(v)) => Ok(v),
    }
}

fn ttl_secs(ttl: Duration) -> u64 {
    ttl.as_secs().max(1)
}

impl SpectrumStore for RedisSpectrumStore {
    fn is_primed(&self, key: String) -> BoxFut<'_, Result<bool, StoreError>> {
        Box::pin(async move {
            let mut c = self.conn.clone();
            let n: i64 = bounded(
                redis::cmd("EXISTS")
                    .arg(format!("{key}:primed"))
                    .query_async(&mut c),
            )
            .await?;
            Ok(n > 0)
        })
    }

    fn unseen(&self, key: String, ids: Vec<String>) -> BoxFut<'_, Result<Vec<String>, StoreError>> {
        Box::pin(async move {
            if ids.is_empty() {
                return Ok(ids);
            }
            let mut c = self.conn.clone();
            let mut cmd = redis::cmd("ZMSCORE");
            cmd.arg(format!("{key}:seen"));
            for i in &ids {
                cmd.arg(i);
            }
            let scores: Vec<Option<f64>> = bounded(cmd.query_async(&mut c)).await?;
            if scores.len() != ids.len() {
                return Err(StoreError("reply_shape"));
            }
            Ok(ids
                .into_iter()
                .zip(scores)
                .filter_map(|(i, s)| s.is_none().then_some(i))
                .collect())
        })
    }

    fn remember(
        &self,
        key: String,
        ids: Vec<String>,
        cap: usize,
        ttl: Duration,
    ) -> BoxFut<'_, Result<(), StoreError>> {
        Box::pin(async move {
            let mut c = self.conn.clone();
            let seen = format!("{key}:seen");
            let secs = ttl_secs(ttl);
            let now_ms = chrono::Utc::now().timestamp_millis();
            let mut pipe = redis::pipe();
            pipe.atomic();
            for i in &ids {
                pipe.cmd("ZADD")
                    .arg(&seen)
                    .arg("NX")
                    .arg(now_ms)
                    .arg(i)
                    .ignore();
            }
            let keep = i64::try_from(cap).map_err(|_| StoreError("cap"))?;
            pipe.cmd("ZREMRANGEBYRANK")
                .arg(&seen)
                .arg(0)
                .arg(-(keep + 1))
                .ignore();
            pipe.cmd("EXPIRE").arg(&seen).arg(secs).ignore();
            pipe.cmd("SET")
                .arg(format!("{key}:primed"))
                .arg("1")
                .arg("EX")
                .arg(secs)
                .ignore();
            bounded(pipe.query_async::<()>(&mut c)).await
        })
    }

    fn load_snapshot(&self, key: String) -> BoxFut<'_, Result<Option<String>, StoreError>> {
        Box::pin(async move {
            let mut c = self.conn.clone();
            bounded(
                redis::cmd("GET")
                    .arg(format!("{key}:snap"))
                    .query_async(&mut c),
            )
            .await
        })
    }

    fn save_snapshot(
        &self,
        key: String,
        json: String,
        ttl: Duration,
    ) -> BoxFut<'_, Result<(), StoreError>> {
        Box::pin(async move {
            let mut c = self.conn.clone();
            bounded(
                redis::cmd("SET")
                    .arg(format!("{key}:snap"))
                    .arg(json)
                    .arg("EX")
                    .arg(ttl_secs(ttl))
                    .query_async::<()>(&mut c),
            )
            .await
        })
    }
}

/// What the last yielded frame was, so its outcome commits the right state.
enum Pending {
    /// Nothing outstanding (priming/empty windows consumed inline).
    None,
    /// A deduped message window carrying these (new) ids.
    Messages(Vec<String>),
    /// One org change frame of the current cycle.
    Org,
}

/// The snapshot to save once every change of a cycle was published.
struct OrgCommit {
    snapshot: String,
    outstanding: usize,
    all_published: bool,
}

/// Poll transport with host-side dedupe/priming (lobby, forum) or paged
/// snapshot diffing (roster, events). See the module docs.
pub struct SpectrumTransport<S: TransportSecretSource + 'static> {
    inner: PollTransport<S>,
    store: Arc<dyn SpectrumStore>,
    spec: SpectrumSpec,
    source_id: String,
    queue: VecDeque<Vec<u8>>,
    pending: Pending,
    commit: Option<OrgCommit>,
    drop_streak: u32,
    metrics: Option<FrameRuleMetrics>,
}

impl<S: TransportSecretSource + 'static> SpectrumTransport<S> {
    /// Wraps a configured poll transport. Org kinds need paging configured on
    /// the poll request; this is verified on the first cycle (fatal if absent).
    #[must_use]
    pub fn new(inner: PollTransport<S>, store: Arc<dyn SpectrumStore>, spec: SpectrumSpec) -> Self {
        let source_id = inner.source_id().to_owned();
        Self {
            inner,
            store,
            spec,
            source_id,
            queue: VecDeque::new(),
            pending: Pending::None,
            commit: None,
            drop_streak: 0,
            metrics: None,
        }
    }

    /// Attaches the frame-rule counters (also given to the poll transport).
    #[must_use]
    pub fn with_metrics(mut self, m: FrameRuleMetrics) -> Self {
        self.inner = self.inner.with_metrics(m.clone());
        self.metrics = Some(m);
        self
    }

    fn count(&self, rule: &str, outcome: &str) {
        if let Some(m) = &self.metrics {
            m.add(self.inner_platform(), rule, outcome, 1);
        }
    }

    fn inner_platform(&self) -> &str {
        Transport::platform(&self.inner)
    }

    fn store_err(&self, op: &'static str, e: StoreError) -> TransportError {
        self.count("spectrum_store", op);
        tracing::error!(
            kind = self.spec.kind.as_str(),
            op,
            class = e.0,
            "spectrum state store failed"
        );
        TransportError::Read("spectrum state store failed".into())
    }

    /// One message-kind poll: dedupe, prime, filter. `Ok(None)` = nothing to
    /// forward this round (primed / all seen).
    async fn next_messages(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        let Some(body) = self.inner.next_frame().await? else {
            return Ok(None);
        };
        let mut v: Value = serde_json::from_slice(&body)
            .map_err(|_| TransportError::Fatal("spectrum endpoint returned non-JSON".into()))?;
        let list_key = if self.spec.kind == SpectrumKind::Forum {
            "threads"
        } else {
            "messages"
        };
        let data = envelope_data(&v)?;
        let list = data
            .get(list_key)
            .and_then(Value::as_array)
            .ok_or_else(|| TransportError::Fatal("spectrum endpoint list missing".into()))?;
        // Entries without an id cannot be deduped or published: dropped, counted.
        let mut kept: Vec<(String, Value)> = Vec::new();
        for item in list {
            match item.as_object().and_then(|o| s_of(o.get("id"))) {
                Some(id) => kept.push((id, item.clone())),
                None => self.count("spectrum_dedupe", "no_id"),
            }
        }
        let ids: Vec<String> = kept.iter().map(|(i, _)| i.clone()).collect();
        let key = self.spec.key.clone();
        let primed = self
            .store
            .is_primed(key.clone())
            .await
            .map_err(|e| self.store_err("is_primed", e))?;
        if !primed {
            self.store
                .remember(key, ids.clone(), self.spec.seen_cap, self.spec.ttl)
                .await
                .map_err(|e| self.store_err("prime", e))?;
            self.count("spectrum_dedupe", "primed");
            tracing::info!(
                kind = self.spec.kind.as_str(),
                primed = ids.len(),
                "gateway.spectrum_ready: history suppressed"
            );
            self.inner.on_frame_outcome(FrameOutcome::Published).await;
            return Ok(None);
        }
        let fresh: HashSet<String> = self
            .store
            .unseen(key, ids)
            .await
            .map_err(|e| self.store_err("unseen", e))?
            .into_iter()
            .collect();
        if fresh.is_empty() {
            self.inner.on_frame_outcome(FrameOutcome::Published).await;
            return Ok(None);
        }
        let mut new_ids = Vec::new();
        let mut out = Vec::new();
        for (id, item) in kept {
            if fresh.contains(&id) && !new_ids.contains(&id) {
                new_ids.push(id);
                out.push(item);
            }
        }
        if let Some(d) = v.get_mut("data").and_then(Value::as_object_mut) {
            d.insert(list_key.to_owned(), Value::Array(out));
        }
        let frame = serde_json::to_vec(&v)
            .map_err(|_| TransportError::Fatal("spectrum frame not serializable".into()))?;
        self.count("spectrum_dedupe", "fresh");
        self.pending = Pending::Messages(new_ids);
        Ok(Some(frame))
    }

    /// Collects every page of a snapshot (or `None` when a page was
    /// unavailable -- the cycle is abandoned, never diffed partially).
    async fn collect_pages<T>(
        &mut self,
        list_keys: &[&str],
        parse: impl Fn(&[Value]) -> Parsed<T>,
        key_of: impl Fn(&T) -> &str,
    ) -> Result<Option<Vec<T>>, TransportError> {
        let mut found: Vec<T> = Vec::new();
        let mut index: HashMap<String, usize> = HashMap::new();
        let mut total: Option<u64> = None;
        for page in 1..=self.spec.max_pages {
            let Some(body) = self
                .inner
                .fetch_page(page, page == 1, self.spec.page_delay)
                .await?
            else {
                self.count("spectrum_org", "page_unavailable");
                tracing::warn!(
                    kind = self.spec.kind.as_str(),
                    page,
                    "snapshot page unavailable; cycle abandoned"
                );
                return Ok(None);
            };
            let v: Value = serde_json::from_slice(&body)
                .map_err(|_| TransportError::Fatal("spectrum endpoint returned non-JSON".into()))?;
            let data = envelope_data(&v)?;
            let Some(raw) = list_keys
                .iter()
                .find_map(|k| data.get(*k).and_then(Value::as_array))
            else {
                return Err(TransportError::Fatal(
                    "spectrum endpoint list missing".into(),
                ));
            };
            total = count_of(data.get("total")).or(total);
            if raw.is_empty() {
                return Ok(Some(found));
            }
            let parsed = parse(raw);
            if parsed.items.is_empty() {
                return Err(TransportError::Fatal(
                    "spectrum entries none parseable".into(),
                ));
            }
            if parsed.dropped > 0 {
                self.count("spectrum_org", "entries_unparseable");
                tracing::warn!(
                    kind = self.spec.kind.as_str(),
                    page,
                    dropped = parsed.dropped,
                    "snapshot entries dropped"
                );
            }
            let mut fresh = 0usize;
            for item in parsed.items {
                match index.get(key_of(&item)) {
                    Some(i) => found[*i] = item,
                    None => {
                        index.insert(key_of(&item).to_owned(), found.len());
                        found.push(item);
                        fresh += 1;
                    }
                }
            }
            if fresh == 0 || total.is_some_and(|t| found.len() as u64 >= t) {
                return Ok(Some(found));
            }
        }
        Err(TransportError::Fatal(
            "spectrum endpoint still returning data past the page cap; refusing a partial snapshot"
                .into(),
        ))
    }

    /// One snapshot cycle: collect, diff, queue the change frames.
    async fn org_cycle(&mut self) -> Result<(), TransportError> {
        let now = chrono::Utc::now();
        let observed_at = now.to_rfc3339();
        let key = self.spec.key.clone();
        let (frames, snapshot, primed, discarded): (Vec<Vec<u8>>, Value, bool, bool);
        let previous = self.load_previous().await?;
        let prev_ref = previous.as_ref();
        let diff_err = |s: &Self, e: DiffError| {
            s.count("spectrum_org", "anomaly");
            tracing::error!(kind = s.spec.kind.as_str(), reason = %e, "snapshot anomaly; snapshot NOT advanced");
            TransportError::Read("spectrum snapshot anomaly".into())
        };
        if self.spec.kind == SpectrumKind::Roster {
            let Some(members) = self
                .collect_pages(&["members", "member_list"], parse_members, |m| &m.member_id)
                .await?
            else {
                return Ok(());
            };
            let d = diff_roster(
                prev_ref,
                &members,
                self.spec.guard_ratio,
                self.spec.guard_min_size,
            )
            .map_err(|e| diff_err(self, e))?;
            frames = d
                .changes
                .iter()
                .map(|c| roster_frame(c, &self.source_id, &observed_at))
                .collect();
            (snapshot, primed, discarded) = (d.snapshot, d.primed, d.previous_discarded);
        } else {
            let Some(events) = self
                .collect_pages(&["events"], parse_events, |e| &e.event_id)
                .await?
            else {
                return Ok(());
            };
            let d = diff_events(
                prev_ref,
                &events,
                now.timestamp() as f64,
                self.spec.guard_ratio,
            )
            .map_err(|e| diff_err(self, e))?;
            frames = d
                .changes
                .iter()
                .map(|c| event_frame(c, &self.source_id, &observed_at))
                .collect();
            (snapshot, primed, discarded) = (d.snapshot, d.primed, d.previous_discarded);
        }
        if discarded {
            self.count("spectrum_org", "snapshot_reset");
            tracing::error!(
                kind = self.spec.kind.as_str(),
                "stored snapshot unusable; discarded, re-priming"
            );
        }
        if primed {
            self.count("spectrum_org", "primed");
            tracing::info!(
                kind = self.spec.kind.as_str(),
                "gateway.spectrum_ready: snapshot primed, no backlog emitted"
            );
        }
        let json = snapshot.to_string();
        if frames.is_empty() {
            return self.save(key, json).await;
        }
        self.count("spectrum_org", "changes");
        self.commit = Some(OrgCommit {
            snapshot: json,
            outstanding: frames.len(),
            all_published: true,
        });
        self.queue.extend(frames);
        Ok(())
    }

    async fn load_previous(&self) -> Result<Option<Value>, TransportError> {
        let raw = self
            .store
            .load_snapshot(self.spec.key.clone())
            .await
            .map_err(|e| self.store_err("load_snapshot", e))?;
        match raw {
            None => Ok(None),
            Some(s) => match serde_json::from_str::<Value>(&s) {
                Ok(v) => Ok(Some(v)),
                Err(_) => {
                    // Unreadable: re-prime loudly (usable_section flags it).
                    Ok(Some(Value::Null))
                }
            },
        }
    }

    async fn save(&self, key: String, json: String) -> Result<(), TransportError> {
        self.store
            .save_snapshot(key, json, self.spec.ttl)
            .await
            .map_err(|e| self.store_err("save_snapshot", e))
    }
}

/// The `data` object of a successful RSI envelope; a failure envelope or a
/// shape change is terminal (auth codes name the session as rejected).
fn envelope_data(v: &Value) -> Result<&Map<String, Value>, TransportError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TransportError::Fatal("spectrum envelope is not an object".into()))?;
    let ok = match obj.get("success") {
        Some(Value::Number(n)) => n.as_i64() == Some(1),
        Some(Value::Bool(b)) => *b,
        _ => false,
    };
    if !ok {
        let auth = obj
            .get("code")
            .and_then(Value::as_str)
            .is_some_and(|c| AUTH_CODES.contains(&c));
        return Err(TransportError::Fatal(
            if auth {
                "spectrum rejected the session"
            } else {
                "spectrum endpoint reported failure"
            }
            .into(),
        ));
    }
    obj.get("data")
        .and_then(Value::as_object)
        .ok_or_else(|| TransportError::Fatal("spectrum envelope has no data object".into()))
}

impl<S: TransportSecretSource + 'static> Transport for SpectrumTransport<S> {
    fn platform(&self) -> &str {
        self.inner_platform()
    }

    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        // A reconnect re-polls from the committed store state, never from
        // half-delivered in-memory state.
        self.queue.clear();
        self.pending = Pending::None;
        self.commit = None;
        self.inner.connect().await
    }

    async fn send_handshake(&mut self, handshake: Handshake) -> Result<(), TransportError> {
        self.inner.send_handshake(handshake).await
    }

    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        loop {
            if let Some(f) = self.queue.pop_front() {
                self.pending = Pending::Org;
                return Ok(Some(f));
            }
            if self.spec.kind.is_org() {
                self.org_cycle().await?;
            } else if let Some(f) = self.next_messages().await? {
                return Ok(Some(f));
            }
        }
    }

    async fn close(&mut self, reason: &str) {
        self.queue.clear();
        self.pending = Pending::None;
        self.commit = None;
        self.inner.close(reason).await;
    }

    async fn on_frame_outcome(&mut self, outcome: FrameOutcome) {
        match std::mem::replace(&mut self.pending, Pending::None) {
            Pending::None => {}
            Pending::Messages(ids) => {
                let published = matches!(outcome, FrameOutcome::Published);
                if published {
                    self.drop_streak = 0;
                } else {
                    self.drop_streak += 1;
                }
                let poison = !published && self.drop_streak >= self.spec.max_drop_attempts;
                if poison {
                    self.drop_streak = 0;
                    self.count("spectrum_dedupe", "poison_skipped");
                    tracing::error!(
                        kind = self.spec.kind.as_str(),
                        "spectrum window dropped repeatedly; skipping its ids"
                    );
                }
                if published || poison {
                    if let Err(e) = self
                        .store
                        .remember(
                            self.spec.key.clone(),
                            ids,
                            self.spec.seen_cap,
                            self.spec.ttl,
                        )
                        .await
                    {
                        // The frame is already out: the ids will re-emit once
                        // (at-least-once); loud, never swallowed.
                        let _ = self.store_err("remember", e);
                    }
                }
                self.inner.on_frame_outcome(outcome).await;
            }
            Pending::Org => {
                let Some(c) = self.commit.as_mut() else {
                    return;
                };
                c.outstanding = c.outstanding.saturating_sub(1);
                if let FrameOutcome::Dropped(reason) = outcome {
                    c.all_published = false;
                    tracing::warn!(kind = self.spec.kind.as_str(), reason = ?reason_label(reason), "org change frame dropped; snapshot will not advance");
                }
                if c.outstanding == 0 {
                    if let Some(c) = self.commit.take() {
                        if c.all_published {
                            if let Err(e) = self.save(self.spec.key.clone(), c.snapshot).await {
                                tracing::error!(kind = self.spec.kind.as_str(), error = %e, "snapshot not advanced; changes will re-emit");
                            }
                        } else {
                            self.count("spectrum_org", "snapshot_held");
                        }
                    }
                }
            }
        }
    }
}

fn reason_label(r: FrameDropReason) -> &'static str {
    match r {
        FrameDropReason::Oversize => "oversize",
        FrameDropReason::GuestError => "guest_error",
        FrameDropReason::ConnectorFault => "connector_fault",
        FrameDropReason::PublishIncomplete => "publish_incomplete",
    }
}

#[cfg(test)]
mod tests;
