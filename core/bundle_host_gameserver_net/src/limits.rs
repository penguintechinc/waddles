//! Fixed-window rate limits and concurrency permits (per connection, per community, global).

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::config::GameNetConfig;
use crate::error::NetError;

const WINDOW: Duration = Duration::from_secs(60);
const MAX_TRACKED: usize = 10_000;

/// Held for a session's lifetime; dropping it frees both concurrency slots.
pub(crate) struct Permits {
    _global: OwnedSemaphorePermit,
    _community: OwnedSemaphorePermit,
}

pub(crate) struct Limits {
    windows: Mutex<HashMap<String, (Instant, u32)>>,
    per_conn: u32,
    per_community: u32,
    global: Arc<Semaphore>,
    communities: Mutex<HashMap<i64, Arc<Semaphore>>>,
    per_community_concurrent: usize,
}

impl Limits {
    pub(crate) fn new(cfg: &GameNetConfig) -> Self {
        Self {
            windows: Mutex::new(HashMap::new()),
            per_conn: cfg.per_connection_per_min,
            per_community: cfg.per_community_per_min,
            global: Arc::new(Semaphore::new(cfg.max_concurrent_global)),
            communities: Mutex::new(HashMap::new()),
            per_community_concurrent: cfg.max_concurrent_per_community,
        }
    }

    fn hit(&self, key: String, max: u32, now: Instant) -> bool {
        let mut map = match self.windows.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        if map.len() >= MAX_TRACKED {
            map.retain(|_, (start, _)| now.duration_since(*start) < WINDOW);
        }
        let entry = map.entry(key).or_insert((now, 0));
        if now.duration_since(entry.0) >= WINDOW {
            *entry = (now, 0);
        }
        if entry.1 >= max {
            return false;
        }
        entry.1 += 1;
        true
    }

    /// Charges one open against the per-connection AND per-community windows.
    pub(crate) fn check_rate(&self, community: i64, server: &str) -> Result<(), NetError> {
        let now = Instant::now();
        if !self.hit(format!("c:{community}"), self.per_community, now)
            || !self.hit(format!("s:{community}:{server}"), self.per_conn, now)
        {
            return Err(NetError::Denied("rate_limited"));
        }
        Ok(())
    }

    /// Takes a global + per-community concurrency slot or refuses.
    pub(crate) fn acquire(&self, community: i64) -> Result<Permits, NetError> {
        let community_sem = {
            let mut map = match self.communities.lock() {
                Ok(g) => g,
                Err(poisoned) => poisoned.into_inner(),
            };
            Arc::clone(
                map.entry(community)
                    .or_insert_with(|| Arc::new(Semaphore::new(self.per_community_concurrent))),
            )
        };
        let community_permit = community_sem
            .try_acquire_owned()
            .map_err(|_| NetError::Denied("too_many_connections"))?;
        let global = Arc::clone(&self.global)
            .try_acquire_owned()
            .map_err(|_| NetError::Denied("too_many_connections"))?;
        Ok(Permits {
            _global: global,
            _community: community_permit,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> GameNetConfig {
        GameNetConfig {
            per_connection_per_min: 2,
            per_community_per_min: 3,
            max_concurrent_per_community: 1,
            max_concurrent_global: 2,
            ..GameNetConfig::default()
        }
    }

    #[test]
    fn per_connection_window_is_enforced_independently() {
        let l = Limits::new(&cfg());
        assert!(l.check_rate(1, "a").is_ok());
        assert!(l.check_rate(1, "a").is_ok());
        assert_eq!(l.check_rate(1, "a"), Err(NetError::Denied("rate_limited")));
    }

    #[test]
    fn per_community_window_spans_connections_and_isolates_communities() {
        let l = Limits::new(&cfg());
        assert!(l.check_rate(1, "a").is_ok());
        assert!(l.check_rate(1, "b").is_ok());
        assert!(l.check_rate(1, "c").is_ok());
        assert_eq!(l.check_rate(1, "d"), Err(NetError::Denied("rate_limited")));
        assert!(l.check_rate(2, "a").is_ok());
    }

    #[test]
    fn concurrency_caps_release_on_drop() {
        let l = Limits::new(&cfg());
        let p = l.acquire(1).unwrap();
        assert_eq!(
            l.acquire(1).err(),
            Some(NetError::Denied("too_many_connections"))
        );
        let _other = l.acquire(2).unwrap();
        assert_eq!(
            l.acquire(3).err(),
            Some(NetError::Denied("too_many_connections")),
            "global cap of 2 reached"
        );
        drop(p);
        assert!(l.acquire(1).is_ok());
    }
}
