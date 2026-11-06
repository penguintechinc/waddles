//! Fair, never-silent queue for DENIAL audit records. Denials are attacker-triggerable
//! (a bundle can spam cross-prefix calls it is not granted), so they must (a) never
//! compete with the fail-closed ALLOWED-path audits for the sink's pool -- ONE worker
//! drains sequentially -- and (b) never let one principal crowd out another's records.
//!
//! * Each principal `(app_id, tenant)` has its own bounded queue
//!   ([`PER_PRINCIPAL_QUOTA`] pending records); the worker drains round-robin, one
//!   record per principal per pass, so a flooder cannot delay or evict anyone else.
//! * A principal over its quota is NOT silently dropped: its overflow is COALESCED
//!   into a single summary row ("N denials dropped for app A / tenant T, window W")
//!   written on the next drain, and the principal FAILS CLOSED
//!   ([`DenialQueue::is_blocked`] -> the host refuses its storage ops with
//!   `rate_limited`) until that summary is durably recorded.
//! * `waddles_bundle_storage_audit_dropped_total{app,tenant}` counts coalesced
//!   records. ALERT on `rate(...) > 0`: it means a principal is being refused for
//!   audit backlog (a flooding bundle, or a stuck audit sink -- check the sink first).

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::sync::{oneshot, Notify};

use crate::audit::{AuditEvent, AuditOutcome, AuditSink};
use crate::metrics;

/// Pending denial records one principal may have queued before coalescing starts.
pub const PER_PRINCIPAL_QUOTA: usize = 16;

type Key = (String, u64);

#[derive(Default)]
struct Principal {
    queue: VecDeque<AuditEvent>,
    /// Overflowed denials not yet covered by a durably written summary row.
    dropped: u64,
    first_drop: Option<Instant>,
}

#[derive(Default)]
struct State {
    principals: HashMap<Key, Principal>,
    flush_waiters: Vec<oneshot::Sender<()>>,
    closed: bool,
}

struct Inner {
    state: Mutex<State>,
    wake: Notify,
}

/// Handle to the denial-audit worker.
pub struct DenialQueue {
    inner: Arc<Inner>,
}

impl Drop for DenialQueue {
    fn drop(&mut self) {
        if let Ok(mut st) = self.inner.state.lock() {
            st.closed = true;
        }
        self.inner.wake.notify_one();
    }
}

struct Batch {
    events: Vec<AuditEvent>,
    /// `(key, count taken, summary event)`.
    summaries: Vec<(Key, u64, AuditEvent)>,
}

fn take_batch(st: &mut State) -> Batch {
    let mut events = Vec::new();
    let mut summaries = Vec::new();
    for (key, p) in st.principals.iter_mut() {
        if let Some(e) = p.queue.pop_front() {
            events.push(e);
        }
        if p.dropped > 0 {
            let window = p.first_drop.map_or(0, |t| t.elapsed().as_secs());
            summaries.push((
                key.clone(),
                p.dropped,
                AuditEvent {
                    caller: key.0.clone(),
                    owner: key.0.clone(),
                    tenant: key.1,
                    community: 0,
                    op: "denials_dropped",
                    key: "-".into(),
                    outcome: AuditOutcome::Denied,
                    reason: format!(
                        "{} denials dropped for app {} / tenant {}, window {}s",
                        p.dropped, key.0, key.1, window
                    ),
                    request_id: "-".into(),
                },
            ));
        }
    }
    Batch { events, summaries }
}

async fn run(inner: Arc<Inner>, sink: Arc<dyn AuditSink>) {
    loop {
        let batch = {
            let mut st = match inner.state.lock() {
                Ok(g) => g,
                Err(_) => return,
            };
            let b = take_batch(&mut st);
            if b.events.is_empty() && b.summaries.is_empty() {
                st.principals
                    .retain(|_, p| !p.queue.is_empty() || p.dropped > 0);
                for w in st.flush_waiters.drain(..) {
                    // Flusher gone == nothing to notify.
                    let _ = w.send(());
                }
                if st.closed {
                    return;
                }
                None
            } else {
                Some(b)
            }
        };
        let Some(batch) = batch else {
            inner.wake.notified().await;
            continue;
        };
        for e in batch.events {
            if let Err(err) = sink.record(e).await {
                tracing::error!(error = %err, "bundle storage denial audit write failed");
            }
        }
        let mut backoff = false;
        for (key, count, summary) in batch.summaries {
            match sink.record(summary).await {
                Ok(()) => {
                    if let Ok(mut st) = inner.state.lock() {
                        if let Some(p) = st.principals.get_mut(&key) {
                            p.dropped = p.dropped.saturating_sub(count);
                            if p.dropped == 0 {
                                p.first_drop = None;
                            }
                        }
                    }
                }
                Err(err) => {
                    // Principal stays blocked (fail closed); retried next pass.
                    tracing::error!(error = %err, app = %key.0, tenant = key.1,
                        "bundle storage denial-summary audit write failed; principal stays blocked");
                    backoff = true;
                }
            }
        }
        if backoff {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }
}

impl DenialQueue {
    /// Spawns the worker on the current Tokio runtime.
    pub fn new(sink: Arc<dyn AuditSink>) -> Self {
        let inner = Arc::new(Inner {
            state: Mutex::new(State::default()),
            wake: Notify::new(),
        });
        tokio::spawn(run(Arc::clone(&inner), sink));
        Self { inner }
    }

    /// `true` while the principal has overflowed denials not yet recorded -- the host
    /// must refuse its storage ops (`rate_limited`): fail CLOSED, never silent.
    pub fn is_blocked(&self, app_id: &str, tenant: u64) -> bool {
        match self.inner.state.lock() {
            Ok(st) => st
                .principals
                .get(&(app_id.to_string(), tenant))
                .is_some_and(|p| p.dropped > 0),
            // A poisoned queue can no longer guarantee auditing: block.
            Err(_) => true,
        }
    }

    /// Enqueues without awaiting. Over-quota -> coalesce into the principal's pending
    /// summary (counted + the principal is blocked until it is written).
    pub fn enqueue(&self, event: AuditEvent) {
        let key = (event.caller.clone(), event.tenant);
        let Ok(mut st) = self.inner.state.lock() else {
            metrics::audit_dropped(&key.0, key.1);
            return;
        };
        let p = st.principals.entry(key.clone()).or_default();
        if p.queue.len() >= PER_PRINCIPAL_QUOTA {
            p.dropped += 1;
            p.first_drop.get_or_insert_with(Instant::now);
            drop(st);
            metrics::audit_dropped(&key.0, key.1);
            tracing::warn!(app = %key.0, tenant = key.1,
                "bundle storage denial audit over per-principal quota; coalescing + refusing until recorded");
        } else {
            p.queue.push_back(event);
            drop(st);
        }
        self.inner.wake.notify_one();
    }

    /// Resolves once every record enqueued before this call (and any pending summary)
    /// has been written (tests/shutdown).
    pub async fn flush(&self) {
        let (done, wait) = oneshot::channel();
        match self.inner.state.lock() {
            Ok(mut st) => st.flush_waiters.push(done),
            Err(_) => return,
        }
        self.inner.wake.notify_one();
        // Worker gone only on shutdown; either way we are done waiting.
        let _ = wait.await;
    }
}
