//! Process-wide, single-flight memo for the inbound PII-tokenization pass
//! (`crate::pii_tokenize`), so ONE ingest message is tokenized -- and its
//! identities minted at hub-api -- exactly once per process no matter how
//! many per-app source-binding consumers read it.
//!
//! **Why:** `crate::source_supervisor` runs one consumer per `(app, source)`
//! binding, and many apps bind the same source, so every consumer reads every
//! message from the shared ingest-source stream. Before this module each of
//! them independently called `tokenize_event_with_mentions`, i.e. one hub-api
//! `MintEphemeralPseudonyms` gRPC call (plus a service-JWT issue and an audit
//! record in hub-api) per consumer per message -- ~160 calls for one Discord
//! message on alpha, which exhausted hub-api's per-identity rate limit, then
//! saturated the pod and opened the `hub_client` circuit breaker.
//!
//! **Why sharing the result is correct:** the tokenization output is a pure
//! function of `(tenant, event)`. hub-api mints a *stable* per-tenant UUID per
//! `(tenant, platform, platform_user_id)` ("mint-or-return"), never per app, so
//! every consumer would have received the identical pseudonyms anyway; nothing
//! app-specific enters the pass. The cached value is the finished
//! [`TokenizedEvent`] (tokenized event + host-side mention bindings).
//!
//! **Key (tenant-scoped, never shared across tenants):** the tenant string the
//! mint is made for, the numeric tenant/community scope, the stream key
//! (platform + source + tenant/community segments), the stream entry id, the
//! envelope event id, and a SHA-256 of the raw event content. The content
//! digest makes a stale or colliding id harmless: different content is a
//! different key. The digest is held in memory only and is never logged.
//!
//! **Fail-closed is preserved exactly:** a failed tokenization is returned to
//! the in-flight leader AND to every consumer that joined it (they share the
//! one outcome, so there is no retry stampede against an already-struggling
//! hub-api), and the caller dead-letters as before. A failure is never cached
//! beyond that in-flight call: the next lookup replaces the entry and
//! tokenizes afresh, so redelivery retries normally. No raw PII is ever
//! forwarded -- this module only ever hands back a finished
//! [`TokenizedEvent`] or a [`TokenizeError`].
//!
//! **Bounded:** entries expire [`DEFAULT_TTL`] after completion and the map is
//! capped at [`DEFAULT_MAX_ENTRIES`] (both env-tunable, see
//! [`TokenizeMemo::from_env`]); at capacity, expired/failed entries are swept
//! first and then the oldest are evicted. A cached entry holds the tokenized
//! event and the mention bindings (which carry raw mention references), so the
//! short TTL also bounds how long that stays in process memory.
//!
//! **Observability:** `waddles_process_tokenize_memo_total{result}` and
//! `waddles_process_tokenize_mint_calls_total{outcome}`
//! (`crate::stage_metrics`), plus DEBUG logs for every decision.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, PoisonError};
use std::time::{Duration, Instant};

use penguin_spine::{Delivered, PlatformEvent};
use sha2::{Digest, Sha256};
use tokio::sync::OnceCell;

use crate::pii_tokenize::{
    tokenize_event_with_mentions, IdentityMinter, MintItem, MintResult, TokenizeError,
    TokenizedEvent,
};
use crate::stage_metrics::{stage_metrics, StageMetrics};

/// How long a successfully tokenized message stays reusable after it
/// completes. Consumers of one message read it within moments of each other; a
/// consumer further behind than this simply re-tokenizes (one extra, correct,
/// mint call).
pub const DEFAULT_TTL: Duration = Duration::from_secs(60);

/// Hard cap on memo entries (completed + in flight). Each entry is one small
/// tokenized event, so this bounds the memo to a few MiB.
pub const DEFAULT_MAX_ENTRIES: usize = 8192;

/// Env var overriding [`DEFAULT_TTL`], in whole seconds (clamped 1..=600).
pub const TTL_ENV: &str = "PROCESS_TOKENIZE_MEMO_TTL_SECS";

/// Env var overriding [`DEFAULT_MAX_ENTRIES`] (clamped 16..=1_000_000).
pub const MAX_ENTRIES_ENV: &str = "PROCESS_TOKENIZE_MEMO_MAX_ENTRIES";

/// Identity of one tokenization unit: the same message for the same tenant
/// scope with the same content. Two consumers produce an equal key iff they
/// would have asked hub-api for the identical thing. Built with
/// [`MemoKey::from_delivered`] (or [`MemoKey::new`] in tests).
#[derive(Clone, PartialEq, Eq, Hash)]
pub struct MemoKey {
    tenant: String,
    tenant_id: Option<i64>,
    community_id: Option<i32>,
    stream: String,
    entry_id: String,
    event_id: String,
    content: [u8; 32],
}

impl std::fmt::Debug for MemoKey {
    /// Redacted: shows only the numeric scope and entry id, never the content digest.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MemoKey")
            .field("tenant_id", &self.tenant_id)
            .field("community_id", &self.community_id)
            .field("entry_id", &self.entry_id)
            .finish_non_exhaustive()
    }
}

impl MemoKey {
    /// Builds a key from its parts: `tenant` is the string the mint is made for,
    /// `stream`/`entry_id` locate the message on the ingest-source stream and
    /// `event` is the raw (decrypted) event whose content is digested.
    ///
    /// # Errors
    /// Only if the event cannot be serialized for digesting; the caller then
    /// bypasses the memo rather than keying on a degenerate digest.
    pub fn new(
        tenant: &str,
        tenant_id: Option<i64>,
        community_id: Option<i32>,
        stream: &str,
        entry_id: &str,
        event_id: &str,
        event: &PlatformEvent,
    ) -> Result<Self, serde_json::Error> {
        let bytes = serde_json::to_vec(event)?;
        Ok(Self {
            tenant: tenant.to_string(),
            tenant_id,
            community_id,
            stream: stream.to_string(),
            entry_id: entry_id.to_string(),
            event_id: event_id.to_string(),
            content: Sha256::digest(&bytes).into(),
        })
    }

    /// Builds the key for one delivered entry and its decrypted `event`.
    ///
    /// # Errors
    /// See [`MemoKey::new`].
    pub fn from_delivered(d: &Delivered, event: &PlatformEvent) -> Result<Self, serde_json::Error> {
        Self::new(
            &d.env.tenant,
            d.env.tenant_id,
            d.env.community_id,
            &d.stream,
            &d.entry_id,
            &d.env.event_id,
            event,
        )
    }
}

/// One memoized tokenization: a single-assignment cell every consumer of the
/// message awaits, plus the instant it completed (for TTL).
struct Slot {
    cell: OnceCell<Result<Arc<TokenizedEvent>, TokenizeError>>,
    created: Instant,
    done_at: OnceLock<Instant>,
}

impl Slot {
    /// A fresh, unresolved slot.
    fn new() -> Self {
        Self {
            cell: OnceCell::new(),
            created: Instant::now(),
            done_at: OnceLock::new(),
        }
    }

    /// True when this slot can still serve a new lookup: in flight (and not
    /// wedged past `ttl`), or completed successfully within `ttl`. A failed
    /// slot is never reusable -- failures are not cached past the in-flight call.
    fn usable(&self, ttl: Duration) -> bool {
        match self.cell.get() {
            None => self.created.elapsed() < ttl,
            Some(Err(_)) => false,
            Some(Ok(_)) => self.done_at.get().is_some_and(|t| t.elapsed() < ttl),
        }
    }
}

/// A map entry: the shared slot plus an insertion sequence number (eviction order).
struct Entry {
    slot: Arc<Slot>,
    seq: u64,
}

/// The mutable memo state behind the lock.
struct State {
    entries: HashMap<MemoKey, Entry>,
    next_seq: u64,
}

/// Wraps an [`IdentityMinter`] to count every hub-api mint call it issues
/// (`waddles_process_tokenize_mint_calls_total`), whichever path made it.
struct CountingMinter<'m> {
    inner: &'m dyn IdentityMinter,
    metrics: &'m StageMetrics,
}

impl IdentityMinter for CountingMinter<'_> {
    /// Delegates to the wrapped minter and records the call's outcome.
    fn mint_many<'a>(&'a self, tenant_id: &'a str, items: Vec<MintItem>) -> MintResult<'a> {
        Box::pin(async move {
            let result = self.inner.mint_many(tenant_id, items).await;
            self.metrics
                .tokenize_mint_call(if result.is_ok() { "ok" } else { "error" });
            result
        })
    }
}

/// The process-wide tokenization memo; share ONE `Arc<TokenizeMemo>` across
/// every consumer (`crate::source_supervisor::SupervisorDeps::tokenize_memo`).
pub struct TokenizeMemo {
    state: Mutex<State>,
    ttl: Duration,
    max_entries: usize,
    metrics: Arc<StageMetrics>,
}

impl Default for TokenizeMemo {
    fn default() -> Self {
        Self::new(DEFAULT_TTL, DEFAULT_MAX_ENTRIES, stage_metrics())
    }
}

/// Parses one clamped env integer, warning (and using `default`) on garbage.
fn env_value<T>(get: &impl Fn(&str) -> Option<String>, name: &str, default: T, lo: T, hi: T) -> T
where
    T: std::str::FromStr + PartialOrd + Copy,
{
    match get(name) {
        None => default,
        Some(raw) => match raw.trim().parse::<T>() {
            Ok(v) if v < lo => lo,
            Ok(v) if v > hi => hi,
            Ok(v) => v,
            Err(_) => {
                tracing::warn!(env = name, "invalid tokenize-memo setting; using default");
                default
            }
        },
    }
}

impl TokenizeMemo {
    /// A memo with explicit bounds, recording to `metrics`.
    #[must_use]
    pub fn new(ttl: Duration, max_entries: usize, metrics: Arc<StageMetrics>) -> Self {
        Self {
            state: Mutex::new(State {
                entries: HashMap::new(),
                next_seq: 0,
            }),
            ttl,
            max_entries: max_entries.max(1),
            metrics,
        }
    }

    /// The production memo: [`DEFAULT_TTL`]/[`DEFAULT_MAX_ENTRIES`] unless
    /// [`TTL_ENV`]/[`MAX_ENTRIES_ENV`] override them, on the global metrics.
    #[must_use]
    pub fn from_env() -> Self {
        Self::from_env_with(|k| std::env::var(k).ok())
    }

    /// [`TokenizeMemo::from_env`] with an injectable environment lookup.
    #[must_use]
    pub fn from_env_with(get: impl Fn(&str) -> Option<String>) -> Self {
        let ttl = env_value(&get, TTL_ENV, DEFAULT_TTL.as_secs(), 1, 600);
        let max = env_value(&get, MAX_ENTRIES_ENV, DEFAULT_MAX_ENTRIES, 16, 1_000_000);
        tracing::debug!(
            ttl_secs = ttl,
            max_entries = max,
            "tokenization memo configured"
        );
        Self::new(Duration::from_secs(ttl), max, stage_metrics())
    }

    /// Number of entries currently held (completed + in flight).
    #[must_use]
    pub fn len(&self) -> usize {
        self.lock().entries.len()
    }

    /// True when the memo holds no entries.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Locks the state; a poisoned lock is recovered (the map is only ever
    /// mutated by short, panic-free critical sections), never propagated.
    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Tokenizes `raw_event` for one delivered entry, once per message per
    /// process: the first consumer runs the real tokenization, concurrent and
    /// later consumers of the same message reuse its outcome. Identical
    /// semantics (and fail-closed behavior) to calling
    /// [`tokenize_event_with_mentions`] directly. If the event cannot be
    /// keyed, the memo is bypassed and the pass runs directly.
    ///
    /// # Errors
    /// The tokenization's own [`TokenizeError`] -- shared with every consumer
    /// that joined the same in-flight call, and never cached afterwards.
    pub async fn tokenize_delivered(
        &self,
        d: &Delivered,
        raw_event: &PlatformEvent,
        tenant: &str,
        minter: &dyn IdentityMinter,
    ) -> Result<TokenizedEvent, TokenizeError> {
        match MemoKey::from_delivered(d, raw_event) {
            Ok(key) => self.tokenize(key, raw_event, tenant, minter).await,
            Err(err) => {
                self.metrics.tokenize_memo("bypass");
                tracing::debug!(
                    event_id = %d.env.event_id,
                    error = %err,
                    "tokenization memo bypassed: event could not be keyed"
                );
                self.run(raw_event, tenant, minter).await
            }
        }
    }

    /// The memoized single-flight core: see [`TokenizeMemo::tokenize_delivered`].
    ///
    /// # Errors
    /// See [`TokenizeMemo::tokenize_delivered`].
    pub async fn tokenize(
        &self,
        key: MemoKey,
        raw_event: &PlatformEvent,
        tenant: &str,
        minter: &dyn IdentityMinter,
    ) -> Result<TokenizedEvent, TokenizeError> {
        let (slot, was_done) = self.slot_for(&key);

        let mut ran = false;
        let outcome = slot
            .cell
            .get_or_init(|| async {
                ran = true;
                let result = self.run(raw_event, tenant, minter).await.map(Arc::new);
                let _ = slot.done_at.set(Instant::now());
                result
            })
            .await;

        let label = if ran {
            "miss"
        } else if was_done {
            "hit"
        } else {
            "inflight_wait"
        };
        self.metrics.tokenize_memo(label);
        tracing::debug!(
            result = label,
            ok = outcome.is_ok(),
            tenant_id = ?key.tenant_id,
            community_id = ?key.community_id,
            entry_id = %key.entry_id,
            "tokenization memo decision"
        );

        if ran && outcome.is_err() {
            // Eagerly drop the failed entry so it never lingers: the next
            // lookup retries afresh (waiters already hold this outcome).
            self.remove_if_same(&key, &slot);
        }

        match outcome {
            Ok(tokenized) => Ok(TokenizedEvent::clone(tokenized)),
            Err(err) => Err(err.clone()),
        }
    }

    /// Runs the real tokenization against `minter`, counting its mint calls.
    async fn run(
        &self,
        raw_event: &PlatformEvent,
        tenant: &str,
        minter: &dyn IdentityMinter,
    ) -> Result<TokenizedEvent, TokenizeError> {
        let counting = CountingMinter {
            inner: minter,
            metrics: &self.metrics,
        };
        tokenize_event_with_mentions(raw_event, tenant, &counting).await
    }

    /// Finds or creates the slot for `key` under the lock (never held across an
    /// await). Returns the slot and whether it had already completed.
    fn slot_for(&self, key: &MemoKey) -> (Arc<Slot>, bool) {
        let mut state = self.lock();
        if let Some(entry) = state.entries.get(key) {
            if entry.slot.usable(self.ttl) {
                let was_done = entry.slot.cell.initialized();
                return (Arc::clone(&entry.slot), was_done);
            }
            state.entries.remove(key);
        }
        if state.entries.len() >= self.max_entries {
            self.make_room(&mut state);
        }
        let slot = Arc::new(Slot::new());
        let seq = state.next_seq;
        state.next_seq += 1;
        state.entries.insert(
            key.clone(),
            Entry {
                slot: Arc::clone(&slot),
                seq,
            },
        );
        (slot, false)
    }

    /// Frees space at capacity: drop every unusable entry, and if the memo is
    /// still full evict the oldest ~1/8 (at least one) so a burst does not
    /// re-scan on every insert.
    fn make_room(&self, state: &mut State) {
        let ttl = self.ttl;
        let before = state.entries.len();
        state.entries.retain(|_, e| e.slot.usable(ttl));
        if state.entries.len() >= self.max_entries {
            let mut order: Vec<(u64, MemoKey)> = state
                .entries
                .iter()
                .map(|(k, e)| (e.seq, k.clone()))
                .collect();
            order.sort_unstable_by_key(|(seq, _)| *seq);
            let drop_n = (self.max_entries / 8).max(1);
            for (_, key) in order.into_iter().take(drop_n) {
                state.entries.remove(&key);
            }
        }
        tracing::debug!(
            before,
            after = state.entries.len(),
            max_entries = self.max_entries,
            "tokenization memo at capacity; evicted"
        );
    }

    /// Removes `key` only if it still maps to `slot` (a newer entry for the
    /// same key must survive).
    fn remove_if_same(&self, key: &MemoKey, slot: &Arc<Slot>) {
        let mut state = self.lock();
        if state
            .entries
            .get(key)
            .is_some_and(|e| Arc::ptr_eq(&e.slot, slot))
        {
            state.entries.remove(key);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap as Map;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    use crate::stage_metrics::test_support::Harness;

    /// Counts calls, optionally sleeps (so concurrent consumers overlap) and
    /// optionally fails; the minted pseudonym embeds the tenant so a test can
    /// prove which tenant a result was minted for.
    struct CountingFake {
        calls: AtomicUsize,
        delay: Duration,
        fail: AtomicBool,
    }

    impl CountingFake {
        fn new(delay_ms: u64) -> Arc<Self> {
            Arc::new(Self {
                calls: AtomicUsize::new(0),
                delay: Duration::from_millis(delay_ms),
                fail: AtomicBool::new(false),
            })
        }
        fn calls(&self) -> usize {
            self.calls.load(Ordering::SeqCst)
        }
    }

    impl IdentityMinter for CountingFake {
        fn mint_many<'a>(&'a self, tenant_id: &'a str, items: Vec<MintItem>) -> MintResult<'a> {
            Box::pin(async move {
                self.calls.fetch_add(1, Ordering::SeqCst);
                tokio::time::sleep(self.delay).await;
                if self.fail.load(Ordering::SeqCst) {
                    return Err(TokenizeError::ResolutionUnavailable("hub down".to_string()));
                }
                Ok(items
                    .into_iter()
                    .map(|i| {
                        let token = format!("{tenant_id}-{}", i.platform_user_id);
                        (i.platform_user_id, token)
                    })
                    .collect::<Map<_, _>>())
            })
        }
    }

    fn event(user_id: &str, handle: &str, text: &str) -> PlatformEvent {
        let payload = serde_json::json!({ "user_id": user_id, "text": text });
        PlatformEvent {
            platform: "discord".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some(handle.to_string()),
            payload: payload.as_object().cloned().unwrap_or_default(),
            occurred_at: "2026-11-13T00:00:00.000Z".to_string(),
            source: None,
        }
    }

    fn key(tenant: &str, entry: &str, ev: &PlatformEvent) -> MemoKey {
        MemoKey::new(
            tenant,
            Some(7),
            Some(3),
            "waddles:t:acme:c:main:src:discord:g1:events",
            entry,
            "evt-1",
            ev,
        )
        .unwrap()
    }

    fn memo(ttl_ms: u64, max: usize) -> (Arc<TokenizeMemo>, Harness, Arc<StageMetrics>) {
        let h = Harness::new();
        let m = Arc::new(StageMetrics::new(&h.meter()));
        (
            Arc::new(TokenizeMemo::new(
                Duration::from_millis(ttl_ms),
                max,
                Arc::clone(&m),
            )),
            h,
            m,
        )
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn one_message_160_consumers_mints_once_and_all_get_the_same_event() {
        let (memo, h, _m) = memo(60_000, 1024);
        let fake = CountingFake::new(150);
        let ev = event("111", "alice", "hello <@222>");
        let k = key("acme", "1-0", &ev);

        let mut tasks = Vec::new();
        for _ in 0..160 {
            let (memo, fake, ev, k) = (Arc::clone(&memo), Arc::clone(&fake), ev.clone(), k.clone());
            tasks.push(tokio::spawn(async move {
                memo.tokenize(k, &ev, "acme", fake.as_ref()).await
            }));
        }
        let mut results = Vec::new();
        for t in tasks {
            results.push(t.await.unwrap().expect("tokenization succeeds"));
        }

        assert_eq!(results.len(), 160, "examined 160 consumers");
        assert_eq!(fake.calls(), 1, "exactly one mint call for one message");
        let first = &results[0];
        assert!(results
            .iter()
            .all(|r| r.event == first.event && r.mentions == first.mentions));
        // The bundle sees tokens, never the raw handle or the raw mention.
        let rendered = serde_json::to_string(&first.event).unwrap();
        assert!(!rendered.contains("alice") && !rendered.contains("<@222>"));
        assert!(rendered.contains("{user:acme-111}") && rendered.contains("{user:acme-222}"));

        let f = |r: &str| h.count("waddles_process_tokenize_memo_total", &[("result", r)]);
        assert_eq!(f("miss"), 1.0);
        assert_eq!(f("hit") + f("inflight_wait"), 159.0);
        assert!(
            f("inflight_wait") >= 1.0,
            "consumers must have joined the in-flight call"
        );
        assert_eq!(
            h.count(
                "waddles_process_tokenize_mint_calls_total",
                &[("outcome", "ok")]
            ),
            1.0
        );
    }

    #[tokio::test]
    async fn completed_entry_serves_later_consumers_as_hits() {
        let (memo, h, _m) = memo(60_000, 1024);
        let fake = CountingFake::new(0);
        let ev = event("111", "alice", "hi");
        for _ in 0..5 {
            memo.tokenize(key("acme", "1-0", &ev), &ev, "acme", fake.as_ref())
                .await
                .unwrap();
        }
        assert_eq!(fake.calls(), 1);
        assert_eq!(
            h.count("waddles_process_tokenize_memo_total", &[("result", "hit")]),
            4.0
        );
    }

    #[tokio::test]
    async fn different_messages_are_separate_calls() {
        let (memo, _h, _m) = memo(60_000, 1024);
        let fake = CountingFake::new(0);
        let ev = event("111", "alice", "hi");
        for entry in ["1-0", "2-0"] {
            memo.tokenize(key("acme", entry, &ev), &ev, "acme", fake.as_ref())
                .await
                .unwrap();
        }
        assert_eq!(fake.calls(), 2);
    }

    #[tokio::test]
    async fn same_message_id_for_two_tenants_never_shares() {
        let (memo, _h, _m) = memo(60_000, 1024);
        let fake = CountingFake::new(0);
        let ev = event("111", "alice", "hi");
        let a = memo
            .tokenize(key("acme", "1-0", &ev), &ev, "acme", fake.as_ref())
            .await
            .unwrap();
        let b = memo
            .tokenize(key("globex", "1-0", &ev), &ev, "globex", fake.as_ref())
            .await
            .unwrap();
        assert_eq!(fake.calls(), 2, "one mint per tenant");
        assert_eq!(a.event.actor.as_deref(), Some("{user:acme-111}"));
        assert_eq!(b.event.actor.as_deref(), Some("{user:globex-111}"));
    }

    #[tokio::test]
    async fn same_ids_with_different_content_never_share() {
        let (memo, _h, _m) = memo(60_000, 1024);
        let fake = CountingFake::new(0);
        let e1 = event("111", "alice", "one");
        let e2 = event("111", "alice", "two");
        let a = memo
            .tokenize(key("acme", "1-0", &e1), &e1, "acme", fake.as_ref())
            .await
            .unwrap();
        let b = memo
            .tokenize(key("acme", "1-0", &e2), &e2, "acme", fake.as_ref())
            .await
            .unwrap();
        assert_eq!(fake.calls(), 2);
        assert_ne!(a.event.payload.get("text"), b.event.payload.get("text"));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn failure_is_shared_by_waiters_once_and_never_cached() {
        let (memo, h, _m) = memo(60_000, 1024);
        let fake = CountingFake::new(150);
        fake.fail.store(true, Ordering::SeqCst);
        let ev = event("111", "alice", "hi");
        let k = key("acme", "1-0", &ev);

        let mut tasks = Vec::new();
        for _ in 0..160 {
            let (memo, fake, ev, k) = (Arc::clone(&memo), Arc::clone(&fake), ev.clone(), k.clone());
            tasks.push(tokio::spawn(async move {
                memo.tokenize(k, &ev, "acme", fake.as_ref()).await
            }));
        }
        let mut errors = 0;
        for t in tasks {
            match t.await.unwrap() {
                Err(TokenizeError::ResolutionUnavailable(_)) => errors += 1,
                other => panic!("every consumer must fail closed, got {other:?}"),
            }
        }
        assert_eq!(errors, 160, "examined 160 consumers, all errored");
        assert_eq!(
            fake.calls(),
            1,
            "waiters share the one failed call (no stampede)"
        );
        assert_eq!(
            h.count(
                "waddles_process_tokenize_mint_calls_total",
                &[("outcome", "error")]
            ),
            1.0
        );
        assert_eq!(memo.len(), 0, "a failed entry is not retained");

        // Hub recovers: the very next lookup retries normally and succeeds...
        fake.fail.store(false, Ordering::SeqCst);
        memo.tokenize(k.clone(), &ev, "acme", fake.as_ref())
            .await
            .expect("retry after failure succeeds");
        assert_eq!(fake.calls(), 2);
        // ...and that success is then cached.
        memo.tokenize(k, &ev, "acme", fake.as_ref()).await.unwrap();
        assert_eq!(fake.calls(), 2);
    }

    #[tokio::test]
    async fn schema_rejection_is_not_cached_either() {
        let (memo, _h, _m) = memo(60_000, 1024);
        let fake = CountingFake::new(0);
        let mut ev = event("111", "alice", "hi");
        ev.payload
            .insert("totally_unvetted_field".into(), serde_json::json!("x"));
        let err = memo
            .tokenize(key("acme", "1-0", &ev), &ev, "acme", fake.as_ref())
            .await
            .expect_err("unclassified field fails closed");
        assert!(matches!(err, TokenizeError::UnclassifiedField { .. }));
        assert_eq!(fake.calls(), 0, "never reached the minter");
        assert!(memo.is_empty());
    }

    #[tokio::test]
    async fn entries_expire_after_the_ttl() {
        let (memo, _h, _m) = memo(60, 1024);
        let fake = CountingFake::new(0);
        let ev = event("111", "alice", "hi");
        let k = key("acme", "1-0", &ev);
        memo.tokenize(k.clone(), &ev, "acme", fake.as_ref())
            .await
            .unwrap();
        memo.tokenize(k.clone(), &ev, "acme", fake.as_ref())
            .await
            .unwrap();
        assert_eq!(fake.calls(), 1, "within the TTL it is reused");
        tokio::time::sleep(Duration::from_millis(120)).await;
        memo.tokenize(k, &ev, "acme", fake.as_ref()).await.unwrap();
        assert_eq!(fake.calls(), 2, "past the TTL it is tokenized afresh");
        assert_eq!(
            memo.len(),
            1,
            "the expired entry was replaced, not accumulated"
        );
    }

    #[tokio::test]
    async fn the_memo_never_exceeds_its_entry_cap() {
        let (memo, _h, _m) = memo(60_000, 16);
        let fake = CountingFake::new(0);
        let ev = event("111", "alice", "hi");
        for i in 0..500 {
            memo.tokenize(
                key("acme", &format!("{i}-0"), &ev),
                &ev,
                "acme",
                fake.as_ref(),
            )
            .await
            .unwrap();
            assert!(
                memo.len() <= 16,
                "len {} exceeded the cap at insert {i}",
                memo.len()
            );
        }
        assert_eq!(fake.calls(), 500, "examined 500 distinct messages");
        assert!(memo.len() > 1, "recent entries are retained");
    }

    #[test]
    fn env_overrides_are_clamped_and_garbage_falls_back() {
        let env = |pairs: &'static [(&'static str, &'static str)]| {
            move |k: &str| {
                pairs
                    .iter()
                    .find(|(n, _)| *n == k)
                    .map(|(_, v)| v.to_string())
            }
        };
        let m = TokenizeMemo::from_env_with(env(&[]));
        assert_eq!((m.ttl, m.max_entries), (DEFAULT_TTL, DEFAULT_MAX_ENTRIES));
        let m = TokenizeMemo::from_env_with(env(&[(TTL_ENV, "5"), (MAX_ENTRIES_ENV, "100")]));
        assert_eq!((m.ttl, m.max_entries), (Duration::from_secs(5), 100));
        let m = TokenizeMemo::from_env_with(env(&[(TTL_ENV, "0"), (MAX_ENTRIES_ENV, "1")]));
        assert_eq!((m.ttl, m.max_entries), (Duration::from_secs(1), 16));
        let m = TokenizeMemo::from_env_with(env(&[(TTL_ENV, "99999"), (MAX_ENTRIES_ENV, "x")]));
        assert_eq!(
            (m.ttl, m.max_entries),
            (Duration::from_secs(600), DEFAULT_MAX_ENTRIES)
        );
    }

    #[test]
    fn memo_key_debug_never_shows_the_content_digest() {
        let ev = event("111", "alice", "secret text");
        let k = key("acme", "1-0", &ev);
        let dbg = format!("{k:?}");
        assert!(!dbg.contains("secret") && !dbg.contains("alice") && !dbg.contains("content"));
    }
}
