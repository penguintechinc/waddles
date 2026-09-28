//! Mints ephemeral pseudonyms for unknown/unlinked platform identities by
//! calling hub-api's `POST /api/v1/internal/identities/ephemeral` --
//! **inside the PII boundary**, never locally computable.
//!
//! Security review fix to the original `crate::pii_tokenize` landing: the
//! ephemeral pseudonym was `UUIDv5(FIXED_PUBLIC_NAMESPACE, "platform:
//! handle")` -- a *public*, fixed-namespace derivation anyone can
//! recompute for a known handle by dictionary attack (enumerate common
//! handles, hash each, compare against observed tokens -- a linkability
//! break masquerading as pseudonymization). hub-api now mints the
//! pseudonym server-side as `HMAC-SHA256(<per-tenant secret, hub-api-only>,
//! "platform:platform_user_id")`, formatted as a UUID -- the secret never
//! leaves hub-api, so this crate cannot even in principle recompute a
//! pseudonym it hasn't been handed.
//!
//! Batched per event (`crate::pii_tokenize::tokenize_platform_event`
//! collects every distinct unresolved identity from one event into a
//! single request; a chat event realistically names at most a handful of
//! users), cached per `(tenant_id, platform, platform_user_id)` with a
//! short TTL (repeated mentions of the same unknown user within the TTL
//! window cost zero extra hub-api calls), and circuit-broken (a run of
//! consecutive failures opens the circuit for a cooldown window so a
//! degraded/unreachable hub-api never turns into a per-event blocking
//! retry storm). On any failure -- network error, non-2xx, timeout, or an
//! open circuit -- the fallback is a **fresh random** token
//! (`Uuid::new_v4()`), never derived from the handle and never cached:
//! exactly as safe as a real ephemeral pseudonym for the PII-boundary
//! invariant (a bundle still never sees raw PII), just not stable across
//! repeated mentions during the outage.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Deserialize;
use uuid::Uuid;

/// Mints (or looks up) the ephemeral pseudonym for one unknown/unlinked
/// platform identity. **Always succeeds** -- see this module's doc for
/// the random-token fallback contract; a caller never has to handle "no
/// pseudonym available at all".
pub trait EphemeralIdentityMinter: Send + Sync {
    fn mint<'a>(
        &'a self,
        tenant_id: i32,
        platform: &'a str,
        platform_user_id: &'a str,
        handle: Option<&'a str>,
    ) -> Pin<Box<dyn Future<Output = Uuid> + Send + 'a>>;
}

/// Always mints a fresh, uncached, unlinkable random token -- the legacy
/// env-driven process loop's resolver (`crate::try_start_process_loop`
/// has no numeric `tenant_id` at all, only the hardcoded `"global"`
/// tenant-wide slug, so it can never call the tenant-scoped hub-api mint
/// endpoint) and the circuit-open/failure fallback path both funnel
/// through this same, deliberately non-deterministic, mechanism.
pub struct RandomTokenMinter;

impl EphemeralIdentityMinter for RandomTokenMinter {
    fn mint<'a>(
        &'a self,
        _tenant_id: i32,
        _platform: &'a str,
        _platform_user_id: &'a str,
        _handle: Option<&'a str>,
    ) -> Pin<Box<dyn Future<Output = Uuid> + Send + 'a>> {
        Box::pin(async move { Uuid::new_v4() })
    }
}

#[derive(Deserialize)]
struct MintResponseEnvelope {
    success: bool,
    data: Option<MintResponseData>,
}

#[derive(Deserialize)]
struct MintResponseData {
    pseudonym: Uuid,
}

/// Consecutive-failure circuit breaker: after [`FAILURE_THRESHOLD`]
/// back-to-back failures the circuit opens for [`OPEN_DURATION`], during
/// which every `mint()` call skips the HTTP round trip entirely and goes
/// straight to the random-token fallback -- a degraded hub-api must never
/// turn into a per-event blocking retry storm on this crate's hot path.
struct CircuitBreaker {
    consecutive_failures: AtomicU32,
    open_until_epoch_ms: AtomicU64,
}

const FAILURE_THRESHOLD: u32 = 3;
const OPEN_DURATION: Duration = Duration::from_secs(30);

impl CircuitBreaker {
    fn new() -> Self {
        Self {
            consecutive_failures: AtomicU32::new(0),
            open_until_epoch_ms: AtomicU64::new(0),
        }
    }

    fn is_open(&self) -> bool {
        now_epoch_ms() < self.open_until_epoch_ms.load(Ordering::Relaxed)
    }

    fn record_success(&self) {
        self.consecutive_failures.store(0, Ordering::Relaxed);
    }

    fn record_failure(&self) {
        let failures = self.consecutive_failures.fetch_add(1, Ordering::Relaxed) + 1;
        if failures >= FAILURE_THRESHOLD {
            let open_until = now_epoch_ms() + OPEN_DURATION.as_millis() as u64;
            self.open_until_epoch_ms
                .store(open_until, Ordering::Relaxed);
        }
    }
}

fn now_epoch_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// `(tenant_id, platform, platform_user_id)` -- deliberately never the
/// handle, which is neither part of the cache key nor logged here.
type CacheKey = (i32, String, String);

const CACHE_TTL: Duration = Duration::from_secs(300);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(2);

/// [`EphemeralIdentityMinter`] backed by hub-api's `POST /api/v1/internal/
/// identities/ephemeral`. `service_key` is `None` when `IDENTITY_SERVICE_
/// API_KEY` isn't configured -- every call then falls straight to
/// [`RandomTokenMinter`]'s behavior (fail toward safety: never send an
/// unauthenticated request, never block startup on a missing dedicated
/// credential).
pub struct HttpEphemeralIdentityMinter {
    client: reqwest::Client,
    base_url: String,
    service_key: Option<String>,
    cache: Mutex<HashMap<CacheKey, (Uuid, Instant)>>,
    circuit: CircuitBreaker,
}

impl HttpEphemeralIdentityMinter {
    pub fn new(base_url: String, service_key: Option<String>) -> Self {
        let client = reqwest::Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());
        Self {
            client,
            base_url,
            service_key,
            cache: Mutex::new(HashMap::new()),
            circuit: CircuitBreaker::new(),
        }
    }

    fn cached(&self, key: &CacheKey) -> Option<Uuid> {
        let cache = self.cache.lock().unwrap_or_else(|e| e.into_inner());
        cache
            .get(key)
            .filter(|(_, at)| at.elapsed() < CACHE_TTL)
            .map(|(uuid, _)| *uuid)
    }

    fn store(&self, key: CacheKey, uuid: Uuid) {
        let mut cache = self.cache.lock().unwrap_or_else(|e| e.into_inner());
        cache.insert(key, (uuid, Instant::now()));
    }

    async fn call_hub_api(
        &self,
        tenant_id: i32,
        platform: &str,
        platform_user_id: &str,
        handle: Option<&str>,
    ) -> Option<Uuid> {
        let service_key = self.service_key.as_ref()?;
        let url = format!("{}/api/v1/internal/identities/ephemeral", self.base_url);
        let body = serde_json::json!({
            "tenant_id": tenant_id,
            "platform": platform,
            "platform_user_id": platform_user_id,
            "handle": handle,
        });
        let resp = self
            .client
            .post(&url)
            .header("X-Service-Key", service_key.as_str())
            .json(&body)
            .send()
            .await
            .ok()?;
        if !resp.status().is_success() {
            return None;
        }
        let parsed: MintResponseEnvelope = resp.json().await.ok()?;
        if !parsed.success {
            return None;
        }
        parsed.data.map(|d| d.pseudonym)
    }
}

impl EphemeralIdentityMinter for HttpEphemeralIdentityMinter {
    fn mint<'a>(
        &'a self,
        tenant_id: i32,
        platform: &'a str,
        platform_user_id: &'a str,
        handle: Option<&'a str>,
    ) -> Pin<Box<dyn Future<Output = Uuid> + Send + 'a>> {
        Box::pin(async move {
            let key: CacheKey = (
                tenant_id,
                platform.to_string(),
                platform_user_id.to_string(),
            );
            if let Some(cached) = self.cached(&key) {
                return cached;
            }
            if self.circuit.is_open() {
                tracing::warn!(
                    tenant_id,
                    platform,
                    "ephemeral-identity mint circuit open; using a random fallback token"
                );
                return Uuid::new_v4();
            }

            match self
                .call_hub_api(tenant_id, platform, platform_user_id, handle)
                .await
            {
                Some(pseudonym) => {
                    self.circuit.record_success();
                    self.store(key, pseudonym);
                    pseudonym
                }
                None => {
                    self.circuit.record_failure();
                    tracing::warn!(
                        tenant_id,
                        platform,
                        "ephemeral-identity mint call failed; using a random fallback token \
                         (never the raw handle, never cached)"
                    );
                    Uuid::new_v4()
                }
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn random_token_minter_never_repeats_and_never_needs_configuration() {
        let minter = RandomTokenMinter;
        let a = minter.mint(1, "twitch", "999", None).await;
        let b = minter.mint(1, "twitch", "999", None).await;
        assert_ne!(a, b, "a random fallback token must never be deterministic");
    }

    #[test]
    fn circuit_breaker_opens_after_the_failure_threshold() {
        let breaker = CircuitBreaker::new();
        assert!(!breaker.is_open());
        for _ in 0..FAILURE_THRESHOLD {
            breaker.record_failure();
        }
        assert!(
            breaker.is_open(),
            "the circuit must open at the failure threshold"
        );
    }

    #[test]
    fn circuit_breaker_resets_on_success() {
        let breaker = CircuitBreaker::new();
        breaker.record_failure();
        breaker.record_failure();
        breaker.record_success();
        breaker.record_failure();
        assert!(
            !breaker.is_open(),
            "a success must reset the consecutive-failure count"
        );
    }

    #[tokio::test]
    async fn no_service_key_configured_falls_back_to_a_random_token_without_a_network_call() {
        let minter = HttpEphemeralIdentityMinter::new("http://unused.invalid".to_string(), None);
        let a = minter.mint(1, "twitch", "999", None).await;
        let b = minter.mint(1, "twitch", "999", None).await;
        assert_ne!(
            a, b,
            "with no service key configured, every call must fall back to an uncached random token"
        );
    }

    #[tokio::test]
    async fn an_unreachable_hub_api_falls_back_to_a_random_token_never_the_raw_handle() {
        let minter = HttpEphemeralIdentityMinter::new(
            "http://127.0.0.1:1".to_string(),
            Some("test-key".to_string()),
        );
        let token = minter
            .mint(1, "twitch", "999", Some("SensitiveHandle"))
            .await;
        // A real UUID was still produced (never a panic, never an empty
        // value), and by construction (Uuid::new_v4 fallback) it cannot
        // contain or encode the raw handle.
        assert_ne!(token, Uuid::nil());
    }
}
