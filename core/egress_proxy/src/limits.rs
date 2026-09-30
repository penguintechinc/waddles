//! Per-tenant connection and bandwidth limits (connector spec's operator
//! guardrails, applied at the network egress point rather than per-app,
//! since a single tenant may run several apps concurrently through this
//! proxy).

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Instant;

#[derive(thiserror::Error, Debug)]
pub enum LimitError {
    #[error("tenant {0:?} exceeded max concurrent connections ({1})")]
    TooManyConnections(String, u32),
}

struct TenantState {
    active_connections: u32,
    /// Simple leaky-bucket bandwidth limiter: `tokens` bytes available,
    /// refilled continuously at `bytes_per_sec`, capped at one second's
    /// worth of burst.
    tokens: f64,
    last_refill: Instant,
}

pub struct TenantLimiter {
    max_connections: u32,
    bytes_per_sec: u64,
    state: Mutex<HashMap<String, TenantState>>,
}

/// RAII guard: decrements the tenant's active-connection count on drop, so
/// a panicking or early-returning handler never leaks a permit.
pub struct ConnectionGuard {
    limiter: Arc<TenantLimiter>,
    tenant: String,
}

impl Drop for ConnectionGuard {
    fn drop(&mut self) {
        if let Ok(mut state) = self.limiter.state.lock() {
            if let Some(entry) = state.get_mut(&self.tenant) {
                entry.active_connections = entry.active_connections.saturating_sub(1);
            }
        }
    }
}

impl TenantLimiter {
    pub fn new(max_connections: u32, bytes_per_sec: u64) -> Arc<Self> {
        Arc::new(Self {
            max_connections,
            bytes_per_sec,
            state: Mutex::new(HashMap::new()),
        })
    }

    /// Reserves one connection slot for `tenant`, or `Err` if that tenant
    /// is already at its concurrent-connection ceiling.
    pub fn acquire(self: &Arc<Self>, tenant: &str) -> Result<ConnectionGuard, LimitError> {
        let mut state = self.state.lock().expect("limiter mutex poisoned");
        let entry = state
            .entry(tenant.to_string())
            .or_insert_with(|| TenantState {
                active_connections: 0,
                tokens: self.bytes_per_sec as f64,
                last_refill: Instant::now(),
            });
        if entry.active_connections >= self.max_connections {
            return Err(LimitError::TooManyConnections(
                tenant.to_string(),
                self.max_connections,
            ));
        }
        entry.active_connections += 1;
        Ok(ConnectionGuard {
            limiter: Arc::clone(self),
            tenant: tenant.to_string(),
        })
    }

    /// Blocks (async sleep) until `bytes` worth of bandwidth budget is
    /// available for `tenant`, then debits it. Called from the
    /// bidirectional-copy loop in `proxy.rs` on every chunk transferred.
    pub async fn throttle(&self, tenant: &str, bytes: usize) {
        loop {
            let wait = {
                let mut state = self.state.lock().expect("limiter mutex poisoned");
                let entry = state
                    .entry(tenant.to_string())
                    .or_insert_with(|| TenantState {
                        active_connections: 0,
                        tokens: self.bytes_per_sec as f64,
                        last_refill: Instant::now(),
                    });
                let now = Instant::now();
                let elapsed = now.duration_since(entry.last_refill).as_secs_f64();
                entry.tokens = (entry.tokens + elapsed * self.bytes_per_sec as f64)
                    .min(self.bytes_per_sec as f64);
                entry.last_refill = now;

                if entry.tokens >= bytes as f64 {
                    entry.tokens -= bytes as f64;
                    None
                } else {
                    let deficit = bytes as f64 - entry.tokens;
                    Some(deficit / self.bytes_per_sec as f64)
                }
            };
            match wait {
                None => return,
                Some(secs) => {
                    tokio::time::sleep(std::time::Duration::from_secs_f64(secs.max(0.001))).await
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn connection_limit_enforced_and_released_on_drop() {
        let limiter = TenantLimiter::new(1, 1_000_000);
        let guard = limiter
            .acquire("tenant-a")
            .expect("first connection allowed");
        assert!(
            limiter.acquire("tenant-a").is_err(),
            "second connection over the cap must be denied"
        );
        drop(guard);
        assert!(
            limiter.acquire("tenant-a").is_ok(),
            "slot freed after guard drop"
        );
    }

    #[test]
    fn tenants_are_isolated() {
        let limiter = TenantLimiter::new(1, 1_000_000);
        let _a = limiter.acquire("tenant-a").unwrap();
        assert!(
            limiter.acquire("tenant-b").is_ok(),
            "a different tenant is not affected by tenant-a's cap"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn throttle_delays_when_bucket_exhausted() {
        let limiter = TenantLimiter::new(10, 100);
        limiter.throttle("t", 100).await; // drains the full initial bucket
        let start = tokio::time::Instant::now();
        limiter.throttle("t", 50).await; // must wait ~0.5s for refill
        assert!(tokio::time::Instant::now() >= start + std::time::Duration::from_millis(400));
    }
}
