//! Kick's webhook verification key: fetched from Kick, cached with a TTL, rotation-aware.
//!
//! Kick signs every webhook with one RSA key it publishes at
//! `GET https://api.kick.com/public/v1/public-key` and may ROTATE. The key is therefore never
//! configured or hardcoded and never comes from a request; this module is the only place that
//! obtains it.
//!
//! ```text
//!  webhook route (Scheme::Kick)
//!        |  WebhookSecretSource::resolve_for(scheme)            -> KickKeySecrets -> KickKeyCache::keys
//!        |  ... verify fails (rotation?)
//!        |  WebhookSecretSource::resolve_refreshed_for(scheme)  -> KickKeySecrets -> KickKeyCache::refreshed
//! ```
//!
//! * **TTL** ([`DEFAULT_TTL`]): a cached key is served until it ages out, then re-fetched.
//! * **Rotation overlap**: when a fetch returns a DIFFERENT key, the old one is kept as
//!   `previous` and both are offered (`Prepared::verify_any`), so deliveries signed just before the
//!   switch still verify.
//! * **Forced refresh**: a signature mismatch triggers ONE re-fetch (picks up a rotation inside
//!   the TTL). Fetches are rate-limited to one per [`MIN_REFRESH`] whatever the trigger, so a
//!   forged-signature flood cannot amplify into Kick API calls.
//! * **Outage**: a failed fetch serves the stale key for up to [`MAX_STALE`] (a Kick API blip never
//!   drops deliveries); with no usable key at all the route answers unavailable (503, Kick retries).
//!
//! The key is public, so nothing here is secret, but the fetch URL is a host constant and the
//! response is size-capped and validated as an RSA key before it is ever cached.

use std::sync::Arc;
use std::time::Duration;

use connection_resolver::{ConnectionRef, SecretString};
use tokio::sync::Mutex;
use tokio::time::Instant;

use super::kick_webhook::PUBLIC_KEY_URL;
use super::webhook::{BoxFut, SecretError, WebhookSecretSource};
use super::webhook_verify::Scheme;

/// How long a fetched key is served before it is re-fetched.
pub const DEFAULT_TTL: Duration = Duration::from_secs(3600);
/// Minimum spacing between key fetches (amplification bound).
pub const MIN_REFRESH: Duration = Duration::from_secs(30);
/// How long a stale key may keep verifying while Kick's key endpoint is unreachable.
pub const MAX_STALE: Duration = Duration::from_secs(24 * 3600);
/// Largest accepted key-endpoint response body.
const MAX_RESPONSE_BYTES: usize = 16 * 1024;
/// Bound on the whole key fetch.
const FETCH_TIMEOUT: Duration = Duration::from_secs(10);

/// Why a key fetch produced no usable key. Carries no response content.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum KeyFetchError {
    /// Transport failure, timeout or a non-success status.
    #[error("kick public key endpoint unavailable")]
    Unavailable,
    /// The endpoint answered with something that is not a usable RSA public key.
    #[error("kick public key response is not a usable RSA key")]
    Invalid,
}

/// Fetches Kick's current public key (PEM). A trait so the cache is tested without the network.
pub trait KickKeyFetch: Send + Sync {
    /// One fetch of the PEM `PUBLIC KEY`.
    fn fetch(&self) -> BoxFut<'_, Result<String, KeyFetchError>>;
}

/// Production [`KickKeyFetch`] over `reqwest` (rustls, no redirects, bounded body and time).
pub struct HttpKickKeyFetch {
    client: reqwest::Client,
    url: String,
}

impl HttpKickKeyFetch {
    /// The production fetcher: HTTPS to the host-constant [`PUBLIC_KEY_URL`] only.
    ///
    /// # Errors
    /// [`KeyFetchError::Unavailable`] if the HTTP client cannot be built.
    pub fn new() -> Result<Self, KeyFetchError> {
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(FETCH_TIMEOUT)
            .https_only(true)
            .build()
            .map_err(|_| KeyFetchError::Unavailable)?;
        Ok(Self {
            client,
            url: PUBLIC_KEY_URL.to_owned(),
        })
    }

    /// Test seam: fetch from `url` (a local fake Kick endpoint; plain http allowed). Production
    /// never calls this: the URL is a host constant.
    ///
    /// # Errors
    /// [`KeyFetchError::Unavailable`] if the HTTP client cannot be built.
    pub fn with_endpoint(url: impl Into<String>) -> Result<Self, KeyFetchError> {
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(FETCH_TIMEOUT)
            .build()
            .map_err(|_| KeyFetchError::Unavailable)?;
        Ok(Self {
            client,
            url: url.into(),
        })
    }
}

/// Extracts and validates the PEM from Kick's `{"data":{"public_key":"<pem>"}}` response.
fn parse_response(body: &[u8]) -> Result<String, KeyFetchError> {
    let v: serde_json::Value = serde_json::from_slice(body).map_err(|_| KeyFetchError::Invalid)?;
    let pem = v
        .get("data")
        .and_then(|d| d.get("public_key"))
        .and_then(serde_json::Value::as_str)
        .map(str::trim)
        .filter(|p| {
            p.starts_with("-----BEGIN PUBLIC KEY-----") && p.ends_with("-----END PUBLIC KEY-----")
        })
        .ok_or(KeyFetchError::Invalid)?;
    // Parse it as an RSA key now, so a garbage key is never cached over a good one.
    super::wix_jwt::public_key_pkcs1(pem).map_err(|_| KeyFetchError::Invalid)?;
    Ok(pem.to_owned())
}

impl KickKeyFetch for HttpKickKeyFetch {
    fn fetch(&self) -> BoxFut<'_, Result<String, KeyFetchError>> {
        Box::pin(async move {
            let mut resp = self
                .client
                .get(&self.url)
                .header("accept", "application/json")
                .send()
                .await
                .map_err(|_| KeyFetchError::Unavailable)?;
            if !resp.status().is_success() {
                return Err(KeyFetchError::Unavailable);
            }
            let mut body = Vec::new();
            while let Some(chunk) = resp.chunk().await.map_err(|_| KeyFetchError::Unavailable)? {
                if body.len() + chunk.len() > MAX_RESPONSE_BYTES {
                    return Err(KeyFetchError::Invalid);
                }
                body.extend_from_slice(&chunk);
            }
            parse_response(&body)
        })
    }
}

struct Entry {
    pem: String,
    fetched: Instant,
}

#[derive(Default)]
struct State {
    current: Option<Entry>,
    previous: Option<String>,
    last_attempt: Option<Instant>,
}

impl State {
    fn joined(&self) -> Option<String> {
        let cur = self.current.as_ref()?;
        Some(match &self.previous {
            Some(prev) => format!("{}\n{}", cur.pem, prev),
            None => cur.pem.clone(),
        })
    }
}

/// TTL + rotation cache over a [`KickKeyFetch`]. One async lock serializes fetches (single
/// flight): concurrent deliveries share one fetch instead of stampeding Kick.
pub struct KickKeyCache<F: KickKeyFetch> {
    fetch: F,
    ttl: Duration,
    min_refresh: Duration,
    max_stale: Duration,
    state: Mutex<State>,
}

impl<F: KickKeyFetch> KickKeyCache<F> {
    /// A cache with the production bounds ([`DEFAULT_TTL`], [`MIN_REFRESH`], [`MAX_STALE`]).
    #[must_use]
    pub fn new(fetch: F) -> Self {
        Self::with_bounds(fetch, DEFAULT_TTL, MIN_REFRESH, MAX_STALE)
    }

    /// A cache with explicit bounds (tests).
    #[must_use]
    pub fn with_bounds(
        fetch: F,
        ttl: Duration,
        min_refresh: Duration,
        max_stale: Duration,
    ) -> Self {
        Self {
            fetch,
            ttl,
            min_refresh,
            max_stale,
            state: Mutex::new(State::default()),
        }
    }

    /// The PEM(s) to verify against: the cached key, re-fetched when older than the TTL.
    ///
    /// # Errors
    /// [`SecretError::Unavailable`] when no usable key can be had (never cached, fetch failing).
    pub async fn keys(&self) -> Result<String, SecretError> {
        self.load(false).await
    }

    /// As [`Self::keys`] but re-fetches even inside the TTL (a signature just failed: has Kick
    /// rotated?). Still bounded by [`MIN_REFRESH`].
    ///
    /// # Errors
    /// As [`Self::keys`].
    pub async fn refreshed(&self) -> Result<String, SecretError> {
        self.load(true).await
    }

    async fn load(&self, force: bool) -> Result<String, SecretError> {
        let mut st = self.state.lock().await;
        let now = Instant::now();
        let age = st
            .current
            .as_ref()
            .map(|e| now.saturating_duration_since(e.fetched));
        let fresh = age.is_some_and(|a| a < self.ttl);
        let may_fetch = st
            .last_attempt
            .is_none_or(|t| now.saturating_duration_since(t) >= self.min_refresh);
        if fresh && !(force && may_fetch) {
            return st.joined().ok_or(SecretError::Unavailable);
        }
        if may_fetch || st.current.is_none() {
            st.last_attempt = Some(now);
            match self.fetch.fetch().await {
                Ok(pem) => {
                    let changed = st.current.as_ref().is_some_and(|c| c.pem != pem);
                    if changed {
                        st.previous = st.current.take().map(|c| c.pem);
                        tracing::info!(
                            "kick webhook public key rotated; previous key kept for overlap"
                        );
                    }
                    st.current = Some(Entry { pem, fetched: now });
                    return st.joined().ok_or(SecretError::Unavailable);
                }
                Err(e) => tracing::warn!(error = %e, "kick public key fetch failed"),
            }
        }
        // Fetch failed or was rate-limited: a not-too-stale key keeps verifying.
        match age {
            Some(a) if a < self.max_stale => st.joined().ok_or(SecretError::Unavailable),
            _ => Err(SecretError::Unavailable),
        }
    }
}

/// [`WebhookSecretSource`] that answers a [`Scheme::Kick`] route from the [`KickKeyCache`] and
/// delegates every other scheme to `inner` (so a deployment with other webhook platforms keeps its
/// normal community-bound secret source). Kick's key is scheme-wide, not a community secret.
pub struct KickKeySecrets<S: WebhookSecretSource, F: KickKeyFetch> {
    inner: S,
    cache: Arc<KickKeyCache<F>>,
}

impl<S: WebhookSecretSource, F: KickKeyFetch> KickKeySecrets<S, F> {
    /// Wraps `inner` with the Kick key answer.
    #[must_use]
    pub fn new(inner: S, cache: Arc<KickKeyCache<F>>) -> Self {
        Self { inner, cache }
    }
}

impl<S: WebhookSecretSource, F: KickKeyFetch + 'static> WebhookSecretSource
    for KickKeySecrets<S, F>
{
    fn resolve(&self, secret_ref: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
        self.inner.resolve(secret_ref)
    }

    fn resolve_for<'a>(
        &'a self,
        scheme: &'a Scheme,
        secret_ref: ConnectionRef,
    ) -> BoxFut<'a, Result<SecretString, SecretError>> {
        if !matches!(scheme, Scheme::Kick) {
            return self.inner.resolve_for(scheme, secret_ref);
        }
        Box::pin(async move { self.cache.keys().await.map(SecretString::new) })
    }

    fn resolve_refreshed_for<'a>(
        &'a self,
        scheme: &'a Scheme,
        secret_ref: ConnectionRef,
    ) -> BoxFut<'a, Result<SecretString, SecretError>> {
        if !matches!(scheme, Scheme::Kick) {
            return self.inner.resolve_refreshed_for(scheme, secret_ref);
        }
        Box::pin(async move { self.cache.refreshed().await.map(SecretString::new) })
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::collections::VecDeque;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex as StdMutex;

    use super::*;
    use crate::connector::wix_jwt::wix_sign::generate;

    struct Script {
        answers: StdMutex<VecDeque<Result<String, KeyFetchError>>>,
        calls: AtomicUsize,
    }

    impl Script {
        fn new(a: Vec<Result<String, KeyFetchError>>) -> Arc<Self> {
            Arc::new(Self {
                answers: StdMutex::new(a.into()),
                calls: AtomicUsize::new(0),
            })
        }
        fn calls(&self) -> usize {
            self.calls.load(Ordering::SeqCst)
        }
    }

    impl KickKeyFetch for Arc<Script> {
        fn fetch(&self) -> BoxFut<'_, Result<String, KeyFetchError>> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let next = self
                .answers
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or(Err(KeyFetchError::Unavailable));
            Box::pin(async move { next })
        }
    }

    fn pem(n: u8) -> String {
        format!("-----BEGIN PUBLIC KEY-----\nKEY{n}\n-----END PUBLIC KEY-----")
    }

    const TTL: Duration = Duration::from_secs(100);
    const MIN: Duration = Duration::from_secs(10);
    const STALE: Duration = Duration::from_secs(1000);

    fn cache(s: &Arc<Script>) -> KickKeyCache<Arc<Script>> {
        KickKeyCache::with_bounds(Arc::clone(s), TTL, MIN, STALE)
    }

    #[tokio::test(start_paused = true)]
    async fn a_key_is_cached_until_the_ttl_then_refetched() {
        let s = Script::new(vec![Ok(pem(1)), Ok(pem(1))]);
        let c = cache(&s);
        assert_eq!(c.keys().await.unwrap(), pem(1));
        tokio::time::advance(Duration::from_secs(99)).await;
        assert_eq!(c.keys().await.unwrap(), pem(1));
        assert_eq!(s.calls(), 1, "served from cache inside the TTL");
        tokio::time::advance(Duration::from_secs(2)).await;
        assert_eq!(c.keys().await.unwrap(), pem(1));
        assert_eq!(s.calls(), 2, "re-fetched after the TTL");
    }

    #[tokio::test(start_paused = true)]
    async fn rotation_offers_the_new_key_first_and_keeps_the_old_one_for_overlap() {
        let s = Script::new(vec![Ok(pem(1)), Ok(pem(2)), Ok(pem(3))]);
        let c = cache(&s);
        c.keys().await.unwrap();
        tokio::time::advance(MIN).await;
        let got = c.refreshed().await.unwrap();
        assert_eq!(got, format!("{}\n{}", pem(2), pem(1)));
        // A second rotation drops the oldest: only current + one previous are offered.
        tokio::time::advance(MIN).await;
        let got = c.refreshed().await.unwrap();
        assert_eq!(got, format!("{}\n{}", pem(3), pem(2)));
    }

    #[tokio::test(start_paused = true)]
    async fn forced_refreshes_are_rate_limited_so_a_forged_flood_cannot_amplify() {
        let s = Script::new(vec![Ok(pem(1)), Ok(pem(2))]);
        let c = cache(&s);
        c.keys().await.unwrap();
        for _ in 0..50 {
            c.refreshed().await.unwrap();
        }
        assert_eq!(
            s.calls(),
            1,
            "inside MIN_REFRESH of the first fetch: no re-fetch"
        );
        tokio::time::advance(MIN).await;
        for _ in 0..50 {
            c.refreshed().await.unwrap();
        }
        assert_eq!(s.calls(), 2, "exactly one re-fetch per window");
    }

    #[tokio::test(start_paused = true)]
    async fn a_failed_fetch_serves_the_stale_key_until_max_stale_then_goes_unavailable() {
        let s = Script::new(vec![Ok(pem(1))]);
        let c = cache(&s);
        c.keys().await.unwrap();
        tokio::time::advance(TTL + MIN).await;
        assert_eq!(c.keys().await.unwrap(), pem(1), "stale key keeps verifying");
        tokio::time::advance(STALE).await;
        assert_eq!(c.keys().await.unwrap_err(), SecretError::Unavailable);
    }

    #[tokio::test(start_paused = true)]
    async fn with_no_key_ever_fetched_a_failure_is_unavailable_and_retries_after_the_window() {
        let s = Script::new(vec![Err(KeyFetchError::Unavailable), Ok(pem(1))]);
        let c = cache(&s);
        assert_eq!(c.keys().await.unwrap_err(), SecretError::Unavailable);
        assert_eq!(
            c.keys().await.unwrap(),
            pem(1),
            "nothing cached: the next call may fetch"
        );
    }

    struct Inner;
    impl WebhookSecretSource for Inner {
        fn resolve(&self, _r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
            Box::pin(async { Ok(SecretString::new("inner".to_string())) })
        }
    }

    #[tokio::test(start_paused = true)]
    async fn only_the_kick_scheme_is_answered_with_the_key_everything_else_delegates() {
        let s = Script::new(vec![Ok(pem(1)), Ok(pem(2))]);
        let src = KickKeySecrets::new(Inner, Arc::new(cache(&s)));
        let r = ConnectionRef {
            community_id: 7,
            connection_id: 12,
        };
        let k = src.resolve_for(&Scheme::kick(), r).await.unwrap();
        assert_eq!(k.expose(), pem(1));
        let other = src
            .resolve_for(&Scheme::twitch_eventsub(), r)
            .await
            .unwrap();
        assert_eq!(other.expose(), "inner");
        let other = src
            .resolve_refreshed_for(&Scheme::twitch_eventsub(), r)
            .await
            .unwrap();
        assert_eq!(other.expose(), "inner");
        assert_eq!(s.calls(), 1, "non-Kick schemes never touch the key cache");
        tokio::time::advance(MIN).await;
        let k = src.resolve_refreshed_for(&Scheme::kick(), r).await.unwrap();
        assert_eq!(k.expose(), format!("{}\n{}", pem(2), pem(1)));
    }

    #[test]
    fn a_response_must_be_an_rsa_public_key_in_the_documented_shape() {
        let good = generate();
        let body = serde_json::json!({"data": {"public_key": good.public_pem}, "message": "OK"});
        assert_eq!(
            parse_response(body.to_string().as_bytes()).unwrap(),
            good.public_pem.trim()
        );
        for bad in [
            serde_json::json!({}),
            serde_json::json!({"data": {}}),
            serde_json::json!({"data": {"public_key": 5}}),
            serde_json::json!({"data": {"public_key": "plain text"}}),
            serde_json::json!({"data": {"public_key": "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----"}}),
        ] {
            assert_eq!(
                parse_response(bad.to_string().as_bytes()).unwrap_err(),
                KeyFetchError::Invalid,
                "{bad}"
            );
        }
        assert_eq!(
            parse_response(b"not json").unwrap_err(),
            KeyFetchError::Invalid
        );
    }
}
