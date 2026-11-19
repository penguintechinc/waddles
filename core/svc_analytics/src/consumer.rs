//! Consumer-group reader of `waddles:analytics`.
//!
//! Delivery is at-least-once (XACK only after the database commit); correctness under
//! redelivery comes from the idempotency ledger written in the SAME transaction as the
//! upserts (`analytics_ops.applied_entries`, see [`crate::db`]). Backpressure is structural:
//! one bounded batch is read, processed and acked before the next read. Poison entries
//! (contract violations, rows the database rejects, exclusion lookups that never recover)
//! are dead-lettered with a PII-free reason and acked; transient failures leave entries
//! pending and back off.

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;
use std::time::{Duration, Instant as StdInstant, SystemTime, UNIX_EPOCH};

use redis::aio::ConnectionManager;
use redis::streams::{StreamAutoClaimOptions, StreamId, StreamMaxlen, StreamReadOptions};
use redis::AsyncCommands;
use tokio::sync::watch;
use tracing::Instrument;
use uuid::Uuid;

use crate::aggregate::{Accepted, EntryId};
use crate::config::Config;
use crate::contract::{self, Reject};
use crate::db::{AnalyticsDb, DbFault, FaultKind};
use crate::flag::FlagGate;
use crate::gate::{ExclusionCache, TierCache};
use crate::health::Ready;
use crate::jobs::Jobs;
use crate::metrics::Metrics;

/// One raw stream entry.
#[derive(Debug, Clone)]
pub struct Raw {
    /// Stream entry id (`<ms>-<seq>`).
    pub id: String,
    /// Field name/value pairs.
    pub fields: Vec<(String, Vec<u8>)>,
}

impl Raw {
    fn from_stream_id(s: StreamId) -> Self {
        let mut fields: Vec<(String, Vec<u8>)> = s
            .map
            .into_iter()
            .map(|(k, v)| {
                let bytes = match v {
                    redis::Value::BulkString(b) => b,
                    redis::Value::SimpleString(t) => t.into_bytes(),
                    redis::Value::Int(i) => i.to_string().into_bytes(),
                    other => {
                        // Not a scalar the contract allows: keep the field empty so
                        // validation rejects the entry (dead-letter), and say why.
                        tracing::warn!(
                            field = %k,
                            "stream entry field has an unsupported value type"
                        );
                        let _ = other;
                        Vec::new()
                    }
                };
                (k, bytes)
            })
            .collect();
        fields.sort();
        Self { id: s.id, fields }
    }
}

/// Flattens an XREADGROUP reply into raw entries. A nil reply is the documented result of a
/// BLOCK timeout (or an empty history read) and means "no entries"; a malformed reply never
/// reaches here because the client surfaces it as an error.
fn entries_from_reply(reply: Option<redis::streams::StreamReadReply>) -> Vec<Raw> {
    match reply {
        Some(r) => r
            .keys
            .into_iter()
            .flat_map(|k| k.ids)
            .map(Raw::from_stream_id)
            .collect(),
        None => Vec::new(),
    }
}

/// Why a cycle did not complete normally (the loop backs off and retries).
#[derive(Debug, thiserror::Error)]
pub enum CycleError {
    /// Valkey failure.
    #[error("valkey: {0}")]
    Valkey(String),
    /// Database failure.
    #[error("{0}")]
    Db(#[from] DbFault),
    /// Entries were left pending (transient write fault / dead-letter failure / lookup outage).
    #[error("entries left pending: {0}")]
    Pending(&'static str),
}

impl From<redis::RedisError> for CycleError {
    fn from(e: redis::RedisError) -> Self {
        // The error kind only: server error text can echo arguments.
        Self::Valkey(format!("{:?}", e.kind()))
    }
}

/// Wall clock in epoch ms.
pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// The consumer.
pub struct Consumer<D: AnalyticsDb> {
    cfg: Config,
    db: D,
    redis: ConnectionManager,
    flag: FlagGate,
    /// Instruments.
    pub metrics: Metrics,
    ready: Ready,
    excl: ExclusionCache,
    tiers: TierCache,
    attempts: Mutex<HashMap<String, u32>>,
    premium_seen: Mutex<HashSet<i32>>,
    seeded: Mutex<bool>,
    group_ready: Mutex<bool>,
    last_claim: Mutex<Option<StdInstant>>,
}

const PREMIUM_SEEN_CAP: usize = 10_000;
const ATTEMPTS_CAP: usize = 10_000;

impl<D: AnalyticsDb> Consumer<D> {
    /// Builds a consumer.
    pub fn new(
        cfg: Config,
        db: D,
        redis: ConnectionManager,
        flag: FlagGate,
        metrics: Metrics,
        ready: Ready,
    ) -> Self {
        let excl = ExclusionCache::new(cfg.exclusion_ttl, cfg.exclusion_cap);
        let tiers = TierCache::new(cfg.tier_ttl, cfg.tier_cap);
        Self {
            cfg,
            db,
            redis,
            flag,
            metrics,
            ready,
            excl,
            tiers,
            attempts: Mutex::new(HashMap::new()),
            premium_seen: Mutex::new(HashSet::new()),
            seeded: Mutex::new(false),
            group_ready: Mutex::new(false),
            last_claim: Mutex::new(None),
        }
    }

    /// The database handle (maintenance jobs and tests).
    pub fn db(&self) -> &D {
        &self.db
    }

    /// The exclusion cache (tests).
    pub fn exclusions(&self) -> &ExclusionCache {
        &self.excl
    }

    fn flagged<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
        m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Creates the consumer group (and stream) if absent.
    ///
    /// # Errors
    /// Valkey failure other than "group exists".
    pub async fn ensure_group(&self) -> Result<(), CycleError> {
        let mut c = self.redis.clone();
        let r: redis::RedisResult<()> = c
            .xgroup_create_mkstream(&self.cfg.stream, &self.cfg.group, "0")
            .await;
        match r {
            Ok(()) => Ok(()),
            Err(e) if e.code() == Some("BUSYGROUP") => Ok(()),
            Err(e) => Err(e.into()),
        }
    }

    async fn read(&self, id: &str, block: bool) -> Result<Vec<Raw>, CycleError> {
        let mut opts = StreamReadOptions::default()
            .group(&self.cfg.group, &self.cfg.consumer)
            .count(self.cfg.batch_size);
        if block {
            opts = opts.block(self.cfg.block.as_millis() as usize);
        }
        let mut c = self.redis.clone();
        let reply: Option<redis::streams::StreamReadReply> =
            match c.xread_options(&[&self.cfg.stream], &[id], &opts).await {
                Ok(r) => r,
                Err(e) => {
                    // Fail closed: nothing was read, so nothing is acked; the cycle fails and
                    // the loop backs off and retries (readiness drops after repeated failures).
                    Metrics::inc(&self.metrics.read_failures, 1, "kind", "xreadgroup");
                    tracing::error!(
                        kind = ?e.kind(),
                        stream = %self.cfg.stream,
                        group = %self.cfg.group,
                        "XREADGROUP failed or returned a malformed reply; cycle aborted"
                    );
                    return Err(e.into());
                }
            };
        Ok(entries_from_reply(reply))
    }

    async fn claim_idle(&self) -> Result<Vec<Raw>, CycleError> {
        let mut c = self.redis.clone();
        let reply: redis::streams::StreamAutoClaimReply = c
            .xautoclaim_options(
                &self.cfg.stream,
                &self.cfg.group,
                &self.cfg.consumer,
                self.cfg.claim_idle.as_millis() as usize,
                "0-0",
                StreamAutoClaimOptions::default().count(self.cfg.batch_size),
            )
            .await?;
        Ok(reply.claimed.into_iter().map(Raw::from_stream_id).collect())
    }

    async fn ack(&self, ids: &[String]) {
        if ids.is_empty() {
            return;
        }
        let mut c = self.redis.clone();
        let r: redis::RedisResult<usize> = c.xack(&self.cfg.stream, &self.cfg.group, ids).await;
        if let Err(e) = r {
            // Redelivery is safe: the idempotency ledger makes the replay a no-op.
            Metrics::inc(&self.metrics.ack_failures, 1, "kind", "xack");
            tracing::warn!(kind = ?e.kind(), "XACK failed; entries will be redelivered");
        }
        let mut a = Self::flagged(&self.attempts);
        for id in ids {
            a.remove(id);
        }
    }

    /// Dead-letters `id` with a fixed reason code (never payload fields).
    async fn dead_letter(&self, id: &str, reason: &'static str) -> Result<(), CycleError> {
        let mut c = self.redis.clone();
        let items: [(&str, String); 4] = [
            ("orig_id", id.to_string()),
            ("reason", reason.to_string()),
            ("at_ms", now_ms().to_string()),
            ("consumer", self.cfg.consumer.clone()),
        ];
        let _: Option<String> = c
            .xadd_maxlen(
                &self.cfg.dlq_stream,
                StreamMaxlen::Approx(self.cfg.dlq_maxlen),
                "*",
                &items,
            )
            .await?;
        Metrics::inc(&self.metrics.dlq, 1, "reason", reason);
        tracing::warn!(entry = id, reason, "entry dead-lettered");
        Ok(())
    }

    fn bump_attempts(&self, ids: &[String]) -> Vec<String> {
        let mut a = Self::flagged(&self.attempts);
        if a.len() > ATTEMPTS_CAP {
            a.clear();
        }
        ids.iter()
            .filter(|id| {
                let n = a.entry((*id).clone()).or_insert(0);
                *n += 1;
                *n >= self.cfg.max_attempts
            })
            .cloned()
            .collect()
    }

    fn note_premium(&self, set: &HashSet<i32>) {
        let mut p = Self::flagged(&self.premium_seen);
        if p.len() + set.len() > PREMIUM_SEEN_CAP {
            p.clear();
        }
        p.extend(set.iter().copied());
    }

    /// Premium communities seen recently (cohort refresh input).
    pub fn premium_seen(&self) -> Vec<i32> {
        Self::flagged(&self.premium_seen).iter().copied().collect()
    }

    /// Processes one batch of raw entries; returns `Ok` only when every entry was either
    /// written or dead-lettered/dropped AND acked.
    ///
    /// # Errors
    /// [`CycleError::Pending`] when entries were intentionally left pending for retry.
    pub async fn process(&self, raws: Vec<Raw>, now_ms: i64) -> Result<(), CycleError> {
        let started = StdInstant::now();
        Metrics::inc(&self.metrics.events, raws.len() as u64, "outcome", "read");
        self.metrics.batch_size.record(raws.len() as u64, &[]);
        let mut ack_ids: Vec<String> = Vec::new();
        let mut held: Option<&'static str> = None;

        // 1. strict contract validation
        let mut valid: Vec<(String, EntryId, contract::Event)> = Vec::new();
        for raw in raws {
            let parsed = match EntryId::parse(&raw.id) {
                None => Err("bad_entry_id"),
                Some(eid) => contract::parse(&raw.fields, now_ms)
                    .map(|e| (eid, e))
                    .map_err(Reject::code),
            };
            match parsed {
                Ok((eid, ev)) => valid.push((raw.id, eid, ev)),
                Err(reason) => {
                    if reason != "bad_entry_id" {
                        Metrics::inc(&self.metrics.rejected, 1, "reason", reason);
                    }
                    Metrics::inc(&self.metrics.events, 1, "outcome", "rejected");
                    match self.dead_letter(&raw.id, reason).await {
                        Ok(()) => ack_ids.push(raw.id),
                        Err(_) => held = Some("dlq_unavailable"),
                    }
                }
            }
        }

        // 2. exclusion check BEFORE any layer A write (fail closed)
        let users: Vec<Uuid> = valid
            .iter()
            .map(|(_, _, e)| e.user_uuid)
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        let excluded = if users.is_empty() {
            HashSet::new()
        } else {
            match self.excl.resolve(&self.db, &users).await {
                Ok((ex, stats)) => {
                    Metrics::inc(&self.metrics.cache, stats.hits, "result", "exclusion_hit");
                    Metrics::inc(
                        &self.metrics.cache,
                        stats.misses,
                        "result",
                        "exclusion_miss",
                    );
                    ex
                }
                Err(fault) => {
                    Metrics::inc(&self.metrics.exclusion_lookup_failures, 1, "kind", "lookup");
                    tracing::error!(fault = %fault, entries = valid.len(), "exclusion lookup unavailable; failing closed (nothing written)");
                    let ids: Vec<String> = valid.iter().map(|(id, _, _)| id.clone()).collect();
                    let give_up = self.bump_attempts(&ids);
                    for id in give_up {
                        if self.dead_letter(&id, "exclusion_unavailable").await.is_ok() {
                            Metrics::inc(
                                &self.metrics.dropped,
                                1,
                                "reason",
                                "exclusion_unavailable",
                            );
                            ack_ids.push(id);
                        }
                    }
                    self.ack(&ack_ids).await;
                    return Err(CycleError::Pending("exclusion_unavailable"));
                }
            }
        };
        let mut candidates: Vec<(String, Accepted)> = Vec::new();
        for (id, eid, ev) in valid {
            if excluded.contains(&ev.user_uuid) {
                Metrics::inc(&self.metrics.dropped, 1, "reason", "excluded");
                Metrics::inc(&self.metrics.events, 1, "outcome", "excluded");
                ack_ids.push(id);
            } else {
                candidates.push((
                    id,
                    Accepted {
                        entry: eid,
                        event: ev,
                        premium: false,
                    },
                ));
            }
        }

        // 3. tier gate (Free on any doubt)
        if !candidates.is_empty() {
            let comms: Vec<i32> = candidates
                .iter()
                .map(|(_, a)| a.event.community_id)
                .collect::<HashSet<_>>()
                .into_iter()
                .collect();
            let (premium, stats, fault) = self.tiers.resolve(&self.db, &comms).await;
            Metrics::inc(&self.metrics.cache, stats.hits, "result", "tier_hit");
            Metrics::inc(&self.metrics.cache, stats.misses, "result", "tier_miss");
            if let Some(f) = fault {
                Metrics::inc(&self.metrics.tier_lookup_failures, 1, "kind", "lookup");
                tracing::warn!(fault = %f, "tier lookup failed; unresolved communities treated as Free");
            }
            self.note_premium(&premium);
            for (_, a) in &mut candidates {
                a.premium = premium.contains(&a.event.community_id);
            }
        }

        // 4. write
        if !candidates.is_empty() {
            let accepted: Vec<Accepted> = candidates.iter().map(|(_, a)| a.clone()).collect();
            match self.db.apply(&accepted).await {
                Ok(rep) => {
                    self.record_apply(&rep);
                    ack_ids.extend(candidates.iter().map(|(id, _)| id.clone()));
                }
                Err(f) if f.kind == FaultKind::Transient => {
                    Metrics::inc(&self.metrics.db_faults, 1, "kind", "transient");
                    tracing::warn!(fault = %f, "batch write failed (transient); entries stay pending");
                    held = Some("write_transient");
                }
                Err(f) => {
                    Metrics::inc(&self.metrics.db_faults, 1, "kind", "data");
                    tracing::warn!(fault = %f, "batch write rejected by database; isolating entries");
                    for (id, a) in &candidates {
                        match self.db.apply(std::slice::from_ref(a)).await {
                            Ok(rep) => {
                                self.record_apply(&rep);
                                ack_ids.push(id.clone());
                            }
                            Err(e) if e.kind == FaultKind::Data => {
                                match self.dead_letter(id, "write_rejected").await {
                                    Ok(()) => ack_ids.push(id.clone()),
                                    Err(_) => held = Some("dlq_unavailable"),
                                }
                            }
                            Err(_) => held = Some("write_transient"),
                        }
                    }
                }
            }
        }

        self.ack(&ack_ids).await;
        self.metrics
            .batch_seconds
            .record(started.elapsed().as_secs_f64(), &[]);
        match held {
            Some(why) => Err(CycleError::Pending(why)),
            None => Ok(()),
        }
    }

    fn record_apply(&self, rep: &crate::db::ApplyReport) {
        let m = &self.metrics;
        Metrics::inc(&m.events, rep.applied, "outcome", "written");
        Metrics::inc(&m.events, rep.duplicates, "outcome", "duplicate");
        Metrics::inc(&m.dropped, rep.duplicates, "reason", "duplicate");
        Metrics::inc(&m.dropped, rep.late_excluded, "reason", "late_excluded");
        Metrics::inc(&m.sessions, rep.sessions_opened, "outcome", "opened");
        Metrics::inc(&m.sessions, rep.sessions_closed, "outcome", "closed");
        Metrics::inc(&m.sessions, rep.duplicate_starts, "outcome", "duplicate");
        Metrics::inc(&m.sessions, rep.orphan_ends, "outcome", "orphan_end");
    }

    /// One read/process cycle: own pending entries first, then idle claims from dead
    /// consumers, then new entries. Returns entries handled.
    ///
    /// # Errors
    /// Setup, Valkey, database or pending-retry failures; the loop backs off.
    pub async fn cycle(&self) -> Result<usize, CycleError> {
        if !*Self::flagged(&self.group_ready) {
            self.ensure_group().await?;
            *Self::flagged(&self.group_ready) = true;
        }
        if !*Self::flagged(&self.seeded) {
            let n = self.excl.seed(&self.db, self.cfg.exclusion_cap).await?;
            tracing::info!(seeded = n, "exclusion cache seeded");
            *Self::flagged(&self.seeded) = true;
        }
        let mut raws = self.read("0", false).await?;
        if raws.is_empty() {
            let due =
                Self::flagged(&self.last_claim).is_none_or(|t| t.elapsed() >= self.cfg.claim_idle);
            if due {
                *Self::flagged(&self.last_claim) = Some(StdInstant::now());
                raws = self.claim_idle().await?;
            }
        }
        if raws.is_empty() {
            raws = self.read(">", true).await?;
        }
        let n = raws.len();
        if n > 0 {
            self.process(raws, now_ms())
                .instrument(tracing::info_span!("analytics.batch", entries = n))
                .await?;
        }
        self.refresh_pending_gauge().await;
        Ok(n)
    }

    async fn refresh_pending_gauge(&self) {
        let mut c = self.redis.clone();
        if let Ok(p) = c
            .xpending::<_, _, redis::streams::StreamPendingReply>(&self.cfg.stream, &self.cfg.group)
            .await
        {
            self.metrics.pending.record(p.count() as u64, &[]);
        }
    }

    /// Runs until `shutdown` flips to `true`.
    pub async fn run(&self, mut shutdown: watch::Receiver<bool>) {
        let mut jobs = Jobs::default();
        let mut failures: u32 = 0;
        self.metrics.up.record(1, &[]);
        loop {
            if *shutdown.borrow() {
                break;
            }
            let enabled = self.flag.enabled().await;
            self.metrics.flag_enabled.record(u64::from(enabled), &[]);
            let now_s = now_ms() / 1000;
            jobs.tick(
                &self.db,
                &self.cfg,
                &self.metrics,
                &self.premium_seen(),
                now_s,
            )
            .await;
            let wait = if !enabled {
                self.ready.set(true);
                failures = 0;
                self.cfg.idle_poll
            } else {
                match self.cycle().await {
                    Ok(_) => {
                        failures = 0;
                        self.ready.set(true);
                        Duration::ZERO
                    }
                    Err(e) => {
                        failures = failures.saturating_add(1);
                        self.ready.set(failures < 3);
                        tracing::warn!(error = %e, failures, "consume cycle failed; backing off");
                        Duration::from_millis(
                            250u64.saturating_mul(1 << failures.min(5)).min(10_000),
                        )
                    }
                }
            };
            if !wait.is_zero() {
                tokio::select! {
                    () = tokio::time::sleep(wait) => {}
                    _ = shutdown.changed() => {}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use redis::streams::{StreamId, StreamKey, StreamReadReply};

    #[test]
    fn nil_reply_is_explicitly_no_entries() {
        assert!(entries_from_reply(None).is_empty());
    }

    #[test]
    fn reply_entries_are_flattened_and_odd_field_types_become_empty() {
        let mut map = HashMap::new();
        map.insert("a".to_string(), redis::Value::BulkString(b"x".to_vec()));
        map.insert("n".to_string(), redis::Value::Int(7));
        map.insert("bad".to_string(), redis::Value::Nil);
        let reply = StreamReadReply {
            keys: vec![StreamKey {
                key: "s".into(),
                ids: vec![StreamId {
                    id: "1-0".into(),
                    map,
                    ..Default::default()
                }],
            }],
        };
        let raws = entries_from_reply(Some(reply));
        assert_eq!(raws.len(), 1);
        assert_eq!(raws[0].id, "1-0");
        assert!(raws[0].fields.contains(&("n".into(), b"7".to_vec())));
        assert!(raws[0].fields.contains(&("bad".into(), Vec::new())));
    }
}
