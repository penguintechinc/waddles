//! Concurrency behaviour over a host kv that has only get/set/delete/increment (no CAS, no
//! transactions, no listing): RSVPs are lost-update-free, event commits are version-checked, and
//! the shared index/queue lists self-heal a clobbering writer.

mod common;

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use common::*;
use serde_json::Value;
use waddle_bundle_calendar_rust::host::{CalError, Ctx, Kv};
use waddle_bundle_calendar_rust::model::*;
use waddle_bundle_calendar_rust::service::{self, NewEvent};
use waddle_bundle_calendar_rust::store;

/// Thread-safe kv: every single op is atomic (as on the real host), nothing spans ops.
#[derive(Clone, Default)]
struct SharedKv(Arc<Mutex<HashMap<String, Vec<u8>>>>);

impl Kv for SharedKv {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, CalError> {
        Ok(self.0.lock().unwrap().get(key).cloned())
    }
    fn set(&self, key: &str, value: &[u8], _ttl: u32) -> Result<(), CalError> {
        self.0.lock().unwrap().insert(key.into(), value.to_vec());
        Ok(())
    }
    fn delete(&self, key: &str) -> Result<(), CalError> {
        self.0.lock().unwrap().remove(key);
        Ok(())
    }
    fn increment(&self, key: &str, delta: i64) -> Result<i64, CalError> {
        let mut m = self.0.lock().unwrap();
        let cur: i64 = m
            .get(key)
            .and_then(|v| String::from_utf8(v.clone()).ok())
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        m.insert(key.into(), (cur + delta).to_string().into_bytes());
        Ok(cur + delta)
    }
}

fn with_ctx<R>(kv: &dyn Kv, f: impl FnOnce(&Ctx) -> R) -> R {
    let tools = FakeTools::default();
    let clock = FixedClock(NOW);
    let log = CapLog::default();
    let ctx = Ctx {
        kv,
        tools: &tools,
        clock: &clock,
        log: &log,
        tenant: "t",
        community: "7",
    };
    f(&ctx)
}

fn uid(n: u32) -> String {
    format!("{n:08x}-0000-4000-8000-000000000000")
}

fn new_ev() -> NewEvent {
    NewEvent {
        title: "Race".into(),
        description: String::new(),
        start: NOW + 86_400,
        end: None,
        tz: Some("UTC".into()),
        location: None,
        channel: None,
    }
}

#[test]
fn concurrent_rsvps_from_many_users_never_lose_an_update() {
    let kv = SharedKv::default();
    let ev = with_ctx(&kv, |c| {
        service::create(c, &Config::default(), &approver(), new_ev())
    })
    .unwrap();
    const THREADS: u32 = 16;
    const PER: u32 = 25;
    let handles: Vec<_> = (0..THREADS)
        .map(|t| {
            let kv = kv.clone();
            std::thread::spawn(move || {
                for i in 0..PER {
                    let who = member(&uid(t * PER + i + 1));
                    let first = if i % 2 == 0 { Rsvp::Yes } else { Rsvp::Maybe };
                    with_ctx(&kv, |c| service::rsvp(c, &who, ev.id, first)).unwrap();
                    // Every third user changes their answer to "no".
                    if i % 3 == 0 {
                        with_ctx(&kv, |c| service::rsvp(c, &who, ev.id, Rsvp::No)).unwrap();
                    }
                }
            })
        })
        .collect();
    for h in handles {
        h.join().unwrap();
    }
    let total = u64::from(THREADS * PER);
    let (mut y, mut n, mut m) = (0, 0, 0);
    for t in 0..THREADS {
        for i in 0..PER {
            let ans = if i % 3 == 0 {
                Rsvp::No
            } else if i % 2 == 0 {
                Rsvp::Yes
            } else {
                Rsvp::Maybe
            };
            match ans {
                Rsvp::Yes => y += 1,
                Rsvp::No => n += 1,
                Rsvp::Maybe => m += 1,
            }
            let stored = store::rsvp_get(&kv, ev.id, &uid(t * PER + i + 1)).unwrap();
            assert_eq!(stored, Some(ans), "user key lost an update");
        }
    }
    let counts = store::rsvp_counts(&kv, ev.id).unwrap();
    assert_eq!(counts, (y, n, m));
    assert_eq!(
        counts.0 + counts.1 + counts.2,
        total,
        "no voter lost or double counted"
    );
}

#[test]
fn rsvp_cap_is_enforced_atomically_and_refused_loudly() {
    let kv = SharedKv::default();
    let ev = with_ctx(&kv, |c| {
        service::create(c, &Config::default(), &approver(), new_ev())
    })
    .unwrap();
    for n in 1..=store::MAX_RSVPS as u32 {
        with_ctx(&kv, |c| {
            service::rsvp(c, &member(&uid(n)), ev.id, Rsvp::Yes)
        })
        .unwrap();
    }
    let over = with_ctx(&kv, |c| {
        service::rsvp(c, &member(&uid(9999)), ev.id, Rsvp::Yes)
    });
    assert!(matches!(over, Err(CalError::Conflict(_))));
    // An existing voter can still change their answer at the cap; counters stay exact.
    with_ctx(&kv, |c| service::rsvp(c, &member(&uid(1)), ev.id, Rsvp::No)).unwrap();
    let (y, n, m) = store::rsvp_counts(&kv, ev.id).unwrap();
    assert_eq!((y, n, m), (store::MAX_RSVPS as u64 - 1, 1, 0));
}

/// MemKv wrapper that runs a hook at a chosen point to model a concurrent writer.
type GetHook = (String, Box<dyn FnOnce(&MemKv)>);

struct HookKv {
    inner: MemKv,
    after_get: RefCell<Option<GetHook>>,
    clobber_index_sets: Cell<u32>,
}

impl HookKv {
    fn new() -> Self {
        HookKv {
            inner: MemKv::default(),
            after_get: RefCell::new(None),
            clobber_index_sets: Cell::new(0),
        }
    }
}

impl Kv for HookKv {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, CalError> {
        let out = self.inner.get(key)?;
        let hit = self
            .after_get
            .borrow()
            .as_ref()
            .is_some_and(|(k, _)| k == key);
        if hit && let Some((_, hook)) = self.after_get.borrow_mut().take() {
            hook(&self.inner);
        }
        Ok(out)
    }
    fn set(&self, key: &str, value: &[u8], ttl: u32) -> Result<(), CalError> {
        self.inner.set(key, value, ttl)?;
        if key == "cal.index" && self.clobber_index_sets.get() > 0 {
            self.clobber_index_sets
                .set(self.clobber_index_sets.get() - 1);
            // A concurrent writer overwrote the list with its own (stale) copy.
            self.inner.set(key, b"[]", 0)?;
        }
        Ok(())
    }
    fn delete(&self, key: &str) -> Result<(), CalError> {
        self.inner.delete(key)
    }
    fn increment(&self, key: &str, delta: i64) -> Result<i64, CalError> {
        self.inner.increment(key, delta)
    }
}

#[test]
fn concurrent_event_mutation_is_a_loud_conflict_not_a_silent_overwrite() {
    let kv = HookKv::new();
    let ev = with_ctx(&kv, |c| {
        service::create(c, &Config::default(), &approver(), new_ev())
    })
    .unwrap();
    // Between cancel()'s load and commit, another writer cancels + bumps the version.
    *kv.after_get.borrow_mut() = Some((
        format!("cal.ev.{}", ev.id),
        Box::new(move |m: &MemKv| {
            let mut other = ev.clone();
            other.version += 1;
            other.title = "Other writer".into();
            m.set(
                &format!("cal.ev.{}", ev.id),
                &serde_json::to_vec(&other).unwrap(),
                0,
            )
            .unwrap();
        }),
    ));
    let res = with_ctx(&kv, |c| {
        service::cancel(c, &Config::default(), &approver(), 1)
    });
    assert!(matches!(res, Err(CalError::Conflict(_))), "{res:?}");
    let stored = store::event(&kv, 1).unwrap().unwrap();
    assert_eq!(stored.title, "Other writer");
    assert_eq!(
        stored.status,
        Status::Scheduled,
        "the loser did not overwrite"
    );
    // A retry on fresh state succeeds.
    assert!(
        with_ctx(&kv, |c| service::cancel(
            c,
            &Config::default(),
            &approver(),
            1
        ))
        .is_ok()
    );
}

#[test]
fn clobbered_index_is_healed_by_readback_and_persistent_loss_is_loud() {
    let kv = HookKv::new();
    kv.clobber_index_sets.set(1);
    let ev = with_ctx(&kv, |c| {
        service::create(c, &Config::default(), &approver(), new_ev())
    })
    .unwrap();
    assert_eq!(
        store::index(&kv).unwrap(),
        vec![ev.id],
        "retry restored the clobbered index"
    );

    kv.clobber_index_sets.set(100);
    let res = store::index_add(&kv, 99);
    assert!(matches!(res, Err(CalError::Conflict(_))));
}

#[test]
fn stored_values_never_leak_into_unrelated_keys() {
    // Guard for the key layout: voters, counters and events live under distinct prefixes.
    let kv = SharedKv::default();
    let ev = with_ctx(&kv, |c| {
        service::create(c, &Config::default(), &approver(), new_ev())
    })
    .unwrap();
    with_ctx(&kv, |c| service::rsvp(c, &member(USER), ev.id, Rsvp::Yes)).unwrap();
    let keys: Vec<String> = kv.0.lock().unwrap().keys().cloned().collect();
    assert!(
        keys.iter()
            .all(|k| k.starts_with("cal.") && !k.contains(':')),
        "{keys:?}"
    );
    let _: Value = serde_json::from_slice(
        kv.0.lock()
            .unwrap()
            .get(&format!("cal.ev.{}", ev.id))
            .unwrap(),
    )
    .unwrap();
}
