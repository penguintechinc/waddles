//! Database-fed refresh of the bounded label registries ([`crate::registry`]).
//!
//! The data plane never calls hub-api and never reads the primary for this: a [`LabelSource`]
//! (implemented over the read replica in `bundle_config_db`) returns the registered command names
//! and app bundles, and [`spawn_refresher`] keeps [`crate::registry::global`] current:
//!
//! * **seed** -- one read at start (retried with backoff until it succeeds; labels are `other` /
//!   `unknown` meanwhile, never an error);
//! * **periodic refresh** -- every `interval`;
//! * **lazy-load on miss** -- a registry miss ([`CommandRegistry::miss_signal`]) triggers a re-read,
//!   rate limited to one per `min_gap` so a stream of unknown `!foo` messages cannot hammer the replica.
//!
//! A failed read keeps the last good snapshot (stale labels beat none) and logs; it is metadata, so it
//! never fails or slows a request. Behind the telemetry kill switch the refresher reads nothing.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use crate::registry::CommandRegistry;

/// What to load: each stage reads only what its database role may see (least privilege).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Wanted {
    /// Registered command names (`app_commands.command`), for the `command` label.
    pub commands: bool,
    /// Registered bundles and their language (`app_versions.app_id/language`), for `bundle`/`bundle_lang`.
    pub bundles: bool,
}

/// One consistent read of the label vocabularies. `None` = not requested (leave the set untouched).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Snapshot {
    /// Registered command names, or `None` when commands were not requested.
    pub commands: Option<Vec<String>>,
    /// `(app_id, language)` of every registered bundle, or `None` when bundles were not requested.
    pub bundles: Option<Vec<(String, String)>>,
}

/// A boxed future, so the trait stays object safe without a macro dependency.
pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Where snapshots come from (the read replica in production, a stub in tests).
pub trait LabelSource: Send + Sync + 'static {
    /// Reads the wanted vocabularies; the error string must carry no credential.
    fn load(&self, wanted: Wanted) -> BoxFut<'_, Result<Snapshot, String>>;
}

/// Refresh timing.
#[derive(Clone, Copy, Debug)]
pub struct Timing {
    /// Periodic refresh.
    pub interval: Duration,
    /// Minimum gap between two reads (bounds miss-triggered reads and retry-after-failure).
    pub min_gap: Duration,
}

impl Default for Timing {
    fn default() -> Self {
        Self {
            interval: Duration::from_secs(300),
            min_gap: Duration::from_secs(15),
        }
    }
}

/// Applies a snapshot to a registry (only the parts that were requested).
pub fn apply(reg: &CommandRegistry, snap: Snapshot) {
    if let Some(c) = snap.commands {
        reg.replace_commands(c);
    }
    if let Some(b) = snap.bundles {
        reg.replace_bundles(b);
    }
}

/// One refresh attempt; `true` on success. Skipped (counted as success) behind the kill switch.
pub async fn refresh_once(source: &dyn LabelSource, reg: &CommandRegistry, wanted: Wanted) -> bool {
    if crate::switch::is_disabled() {
        return true;
    }
    match source.load(wanted).await {
        Ok(snap) => {
            apply(reg, snap);
            tracing::debug!(
                commands = reg.command_count(),
                bundles = reg.bundle_count(),
                "command-latency label registry refreshed from the database replica"
            );
            true
        }
        Err(err) => {
            tracing::warn!(
                error = %err,
                "command-latency label registry refresh failed; keeping the last snapshot"
            );
            false
        }
    }
}

/// Spawns the seed + periodic + miss-triggered refresher onto the current Tokio runtime and returns
/// its handle (dropping it does not stop the task; abort it on shutdown if needed).
pub fn spawn_refresher(
    source: Arc<dyn LabelSource>,
    reg: &'static CommandRegistry,
    wanted: Wanted,
    timing: Timing,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        // Seed: retry until the first read succeeds, never faster than `min_gap`.
        while !refresh_once(source.as_ref(), reg, wanted).await {
            tokio::time::sleep(timing.min_gap).await;
        }
        loop {
            tokio::select! {
                () = tokio::time::sleep(timing.interval) => {}
                () = reg.miss_signal().notified() => {}
            }
            refresh_once(source.as_ref(), reg, wanted).await;
            // Rate limit: whatever woke us, the next read is at least `min_gap` away. A miss during
            // the gap leaves one pending wake, serviced right after it.
            tokio::time::sleep(timing.min_gap).await;
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    struct Stub {
        calls: AtomicUsize,
        fail_first: usize,
        snap: Mutex<Snapshot>,
    }

    impl LabelSource for Stub {
        fn load(&self, _w: Wanted) -> BoxFut<'_, Result<Snapshot, String>> {
            Box::pin(async move {
                let n = self.calls.fetch_add(1, Ordering::SeqCst);
                if n < self.fail_first {
                    return Err("replica down".into());
                }
                Ok(self.snap.lock().unwrap().clone())
            })
        }
    }

    fn snap(cmds: &[&str]) -> Snapshot {
        Snapshot {
            commands: Some(cmds.iter().map(|s| (*s).to_string()).collect()),
            bundles: Some(vec![("waddles.core.ping".into(), "rust".into())]),
        }
    }

    #[tokio::test]
    async fn refresh_applies_and_a_failure_keeps_the_last_snapshot() {
        let reg = CommandRegistry::new();
        let stub = Stub {
            calls: AtomicUsize::new(0),
            fail_first: 1,
            snap: Mutex::new(snap(&["ping"])),
        };
        let w = Wanted {
            commands: true,
            bundles: true,
        };
        assert!(!refresh_once(&stub, &reg, w).await, "first read fails");
        assert_eq!(reg.command_count(), 0);
        assert!(refresh_once(&stub, &reg, w).await);
        assert!(reg.contains("ping"));
        assert_eq!(reg.bundle_lang("waddles.core.ping"), "rust");
        let failing = Stub {
            calls: AtomicUsize::new(0),
            fail_first: usize::MAX,
            snap: Mutex::new(Snapshot::default()),
        };
        assert!(!refresh_once(&failing, &reg, w).await);
        assert!(reg.contains("ping"), "stale labels beat none");
    }

    #[tokio::test]
    async fn only_requested_parts_are_replaced() {
        let reg = CommandRegistry::new();
        reg.register("keep");
        apply(
            &reg,
            Snapshot {
                commands: None,
                bundles: Some(vec![("a".into(), "go".into())]),
            },
        );
        assert!(
            reg.contains("keep"),
            "commands untouched when not requested"
        );
        assert_eq!(reg.bundle_lang("a"), "go");
    }

    #[tokio::test(start_paused = true)]
    async fn seed_retries_then_miss_triggers_a_rate_limited_reload() {
        static REG: std::sync::OnceLock<CommandRegistry> = std::sync::OnceLock::new();
        let reg = REG.get_or_init(CommandRegistry::new);
        let stub = Arc::new(Stub {
            calls: AtomicUsize::new(0),
            fail_first: 2,
            snap: Mutex::new(snap(&["ping"])),
        });
        let timing = Timing {
            interval: Duration::from_secs(3600),
            min_gap: Duration::from_secs(10),
        };
        let _h = spawn_refresher(
            stub.clone(),
            reg,
            Wanted {
                commands: true,
                bundles: true,
            },
            timing,
        );
        tokio::time::sleep(Duration::from_secs(25)).await;
        assert_eq!(stub.calls.load(Ordering::SeqCst), 3, "2 failures + seed");
        assert!(reg.contains("ping"));

        *stub.snap.lock().unwrap() = snap(&["ping", "roll"]);
        // A burst of misses coalesces into ONE re-read.
        for _ in 0..50 {
            assert_eq!(reg.detect("!roll"), None);
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
        assert_eq!(stub.calls.load(Ordering::SeqCst), 4);
        assert!(reg.contains("roll"), "lazy-loaded on miss");
        for _ in 0..50 {
            reg.detect("!another");
        }
        tokio::time::sleep(Duration::from_secs(5)).await;
        assert_eq!(
            stub.calls.load(Ordering::SeqCst),
            4,
            "rate limited by min_gap"
        );
        tokio::time::sleep(Duration::from_secs(10)).await;
        assert_eq!(
            stub.calls.load(Ordering::SeqCst),
            5,
            "pending wake serviced after the gap"
        );
    }
}
