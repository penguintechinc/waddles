//! Kick events subscription: registers the webhook events a community's connection receives.
//!
//! Kick delivers webhooks to the ONE callback URL configured on the Kick application, and only
//! for events the broadcaster's authorization has subscribed to
//! (<https://docs.kick.com/events/subscribe-to-events>). This module is the host-side call that
//! subscribes a community's broadcaster to the events the Kick bundle understands:
//!
//! ```text
//!  KickSubscriber::ensure(community_id)
//!    1. KickCredentialSource::binding -> OAuth token + the connection's broadcaster_user_id
//!    2. GET  /public/v1/events/subscriptions?broadcaster_user_id=<id>   (what already exists)
//!    3. POST /public/v1/events/subscriptions {"broadcaster_user_id":<int>,"events":[missing..],"method":"webhook"}
//! ```
//!
//! Idempotent (only missing events are created), fail loud (a per-event error or a non-2xx is a
//! typed [`SubscribeError`], never "assumed subscribed"), and secret-safe: the bearer token is
//! resolved per call from the community's own connection, lives only in a zeroizing value, and
//! appears in no log, error or metric. The host is the constant [`API_BASE`] (https-only, no
//! redirects, bounded body and time).

use std::collections::BTreeSet;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use connection_resolver::{
    BindingPage, ConnectionRef, ConnectionResolve, ResolveError, ResolvedConnection,
};
use serde_json::{json, Value};
use zeroize::Zeroizing;

use super::webhook::BoxFut;
use crate::outbound_creds::CredError;
use crate::outbound_creds_kick::KickCredentialSource;

/// Kick public API base (host constant; never from binding or guest data).
pub const API_BASE: &str = "https://api.kick.com/public/v1";
/// Path of the subscription collection under the base.
const SUBSCRIPTIONS_PATH: &str = "/events/subscriptions";
/// The webhook events the Kick bundle normalizes (`bundles/rust/kick/src/events.rs`).
pub const EVENTS: [(&str, u32); 6] = [
    ("chat.message.sent", 1),
    ("channel.followed", 1),
    ("channel.subscription.new", 1),
    ("channel.subscription.renewal", 1),
    ("channel.subscription.gifts", 1),
    ("livestream.status.updated", 1),
];
/// Bound on each Kick call.
const CALL_TIMEOUT: Duration = Duration::from_secs(10);
/// Largest accepted Kick response body.
const MAX_RESPONSE_BYTES: usize = 256 * 1024;
/// Attempts made by [`KickSubscriber::spawn_ensure`] before giving up loudly.
pub const MAX_ATTEMPTS: u32 = 5;
/// First retry delay (doubles each attempt).
const FIRST_BACKOFF: Duration = Duration::from_secs(2);

/// Why a subscription could not be ensured. Carries no response content or credential.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum SubscribeError {
    /// The community's Kick credential could not be resolved (`reason` is the stable code).
    #[error("kick credential unavailable: {0}")]
    Credential(&'static str),
    /// The binding carries no valid `broadcaster_user_id`.
    #[error("the kick binding has no valid broadcaster_user_id")]
    NoBroadcaster,
    /// Transport failure or timeout.
    #[error("kick events api unreachable")]
    Unreachable,
    /// Kick answered a non-success status.
    #[error("kick events api answered http {0}")]
    Status(u16),
    /// The reply was not the documented shape or exceeded the size bound.
    #[error("kick events api reply was malformed")]
    Malformed,
    /// Kick refused at least one event subscription.
    #[error("kick refused {0} event subscription(s)")]
    Refused(usize),
}

impl SubscribeError {
    /// `true` when a later attempt can succeed without anyone changing configuration.
    #[must_use]
    pub fn retryable(self) -> bool {
        match self {
            Self::Credential(r) => matches!(r, "credential_unavailable" | "token_expired"),
            Self::Unreachable | Self::Malformed => true,
            Self::Status(s) => s == 429 || s >= 500,
            Self::NoBroadcaster | Self::Refused(_) => false,
        }
    }

    /// Stable low-cardinality metric / log code.
    #[must_use]
    pub fn code(self) -> &'static str {
        match self {
            Self::Credential(_) => "credential",
            Self::NoBroadcaster => "no_broadcaster",
            Self::Unreachable => "unreachable",
            Self::Status(_) => "status",
            Self::Malformed => "malformed",
            Self::Refused(_) => "refused",
        }
    }
}

/// What one successful [`KickSubscriber::ensure`] did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Ensured {
    /// Events already subscribed.
    pub existing: usize,
    /// Events created by this call.
    pub created: usize,
}

/// One Kick request. The token is zeroizing and never `Debug`-printed.
pub struct KickRequest {
    /// `GET`, `POST` or `DELETE`.
    pub method: &'static str,
    /// Absolute URL.
    pub url: String,
    /// Bearer token (without the `Bearer ` prefix).
    pub token: Zeroizing<String>,
    /// JSON body, when the call has one.
    pub body: Option<Vec<u8>>,
}

impl std::fmt::Debug for KickRequest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("KickRequest")
            .field("method", &self.method)
            .field("url", &"[REDACTED]")
            .field("token", &"[REDACTED]")
            .finish_non_exhaustive()
    }
}

/// The one egress seam of the subscriber.
pub trait KickEventsHttp: Send + Sync {
    /// Sends `req`; returns the status and the (bounded) body.
    fn send(&self, req: KickRequest) -> BoxFut<'_, Result<(u16, Vec<u8>), SubscribeError>>;
}

/// Production [`KickEventsHttp`] over `reqwest` (rustls, https-only, no redirects).
pub struct HttpKickEvents {
    client: reqwest::Client,
}

impl HttpKickEvents {
    /// The production client: HTTPS to [`API_BASE`] only.
    ///
    /// # Errors
    /// [`SubscribeError::Unreachable`] if the HTTP client cannot be built.
    pub fn new() -> Result<Self, SubscribeError> {
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(CALL_TIMEOUT)
            .https_only(true)
            .build()
            .map_err(|_| SubscribeError::Unreachable)?;
        Ok(Self { client })
    }

    /// Test seam: plain http allowed (a local fake Kick). Production never calls this; the URL
    /// the subscriber builds is still derived from its configured base.
    ///
    /// # Errors
    /// [`SubscribeError::Unreachable`] if the HTTP client cannot be built.
    pub fn insecure_for_tests() -> Result<Self, SubscribeError> {
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(CALL_TIMEOUT)
            .build()
            .map_err(|_| SubscribeError::Unreachable)?;
        Ok(Self { client })
    }
}

impl KickEventsHttp for HttpKickEvents {
    fn send(&self, req: KickRequest) -> BoxFut<'_, Result<(u16, Vec<u8>), SubscribeError>> {
        Box::pin(async move {
            let method = match req.method {
                "GET" => reqwest::Method::GET,
                "POST" => reqwest::Method::POST,
                "DELETE" => reqwest::Method::DELETE,
                _ => return Err(SubscribeError::Malformed),
            };
            let mut b = self
                .client
                .request(method, &req.url)
                .header("accept", "application/json")
                .header("authorization", format!("Bearer {}", req.token.as_str()));
            if let Some(body) = req.body {
                b = b.header("content-type", "application/json").body(body);
            }
            let mut resp = b.send().await.map_err(|_| SubscribeError::Unreachable)?;
            let status = resp.status().as_u16();
            let mut body = Vec::new();
            while let Some(chunk) = resp
                .chunk()
                .await
                .map_err(|_| SubscribeError::Unreachable)?
            {
                if body.len() + chunk.len() > MAX_RESPONSE_BYTES {
                    return Err(SubscribeError::Malformed);
                }
                body.extend_from_slice(&chunk);
            }
            Ok((status, body))
        })
    }
}

static OUTCOMES: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_kick_subscribe_total",
            "Kick events-subscription attempts by outcome (ok or a fixed error code)",
        ),
        &["outcome"],
    )
    .expect("valid metric definition")
});

/// Registers the subscription outcome counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(OUTCOMES.clone()))
}

/// Attempts counted for `outcome` (test/observability helper).
#[must_use]
pub fn outcome_count(outcome: &str) -> u64 {
    OUTCOMES.with_label_values(&[outcome]).get()
}

/// Subscribes a community's Kick broadcaster to the bundle's webhook events.
pub struct KickSubscriber<C: KickCredentialSource, H: KickEventsHttp> {
    creds: C,
    http: H,
    base: String,
}

impl<C: KickCredentialSource, H: KickEventsHttp> KickSubscriber<C, H> {
    /// The production subscriber: [`API_BASE`].
    #[must_use]
    pub fn new(creds: C, http: H) -> Self {
        Self {
            creds,
            http,
            base: API_BASE.to_owned(),
        }
    }

    /// Test seam: a different API base (a local fake Kick).
    #[must_use]
    pub fn with_base(mut self, base: impl Into<String>) -> Self {
        self.base = base.into();
        self
    }

    fn url(&self) -> String {
        format!("{}{SUBSCRIPTIONS_PATH}", self.base)
    }

    /// Ensures every event in [`EVENTS`] is subscribed for `community_id`'s broadcaster.
    ///
    /// # Errors
    /// [`SubscribeError`]; a partially-created set is reported as [`SubscribeError::Refused`].
    pub async fn ensure(&self, community_id: i64) -> Result<Ensured, SubscribeError> {
        self.ensure_tracked(community_id, &|_| {}).await
    }

    /// [`Self::ensure`], reporting the ids of every subscription this call CREATED to
    /// `on_created` the moment the create reply is parsed (even when part of the batch was
    /// refused), so the caller can delete exactly those when the route goes away.
    ///
    /// # Errors
    /// As [`Self::ensure`].
    pub async fn ensure_tracked(
        &self,
        community_id: i64,
        on_created: &(dyn Fn(Vec<String>) + Send + Sync),
    ) -> Result<Ensured, SubscribeError> {
        let result = self.ensure_inner(community_id, on_created).await;
        let outcome = match &result {
            Ok(_) => "ok",
            Err(e) => e.code(),
        };
        OUTCOMES.with_label_values(&[outcome]).inc();
        match &result {
            Ok(e) => tracing::info!(
                community_id,
                existing = e.existing,
                created = e.created,
                "kick events subscribed"
            ),
            Err(e) => tracing::warn!(
                community_id,
                code = e.code(),
                retryable = e.retryable(),
                error = %e,
                "kick events subscription failed"
            ),
        }
        result
    }

    async fn ensure_inner(
        &self,
        community_id: i64,
        on_created: &(dyn Fn(Vec<String>) + Send + Sync),
    ) -> Result<Ensured, SubscribeError> {
        let binding = self
            .creds
            .binding(Some(community_id))
            .await
            .map_err(|e: CredError| SubscribeError::Credential(e.reason()))?;
        let broadcaster: u64 = binding
            .broadcaster_user_id
            .as_deref()
            .and_then(|b| b.parse().ok())
            .ok_or(SubscribeError::NoBroadcaster)?;
        let token = || Zeroizing::new(binding.oauth_token.expose().to_owned());

        let (status, body) = self
            .http
            .send(KickRequest {
                method: "GET",
                url: format!("{}?broadcaster_user_id={broadcaster}", self.url()),
                token: token(),
                body: None,
            })
            .await?;
        if !(200..300).contains(&status) {
            return Err(SubscribeError::Status(status));
        }
        let have = existing_events(&body, broadcaster)?;
        let missing: Vec<Value> = EVENTS
            .iter()
            .filter(|(name, _)| !have.contains(*name))
            .map(|(name, version)| json!({ "name": name, "version": version }))
            .collect();
        let existing = EVENTS.len() - missing.len();
        if missing.is_empty() {
            return Ok(Ensured {
                existing,
                created: 0,
            });
        }
        let created_n = missing.len();
        let payload = json!({
            "broadcaster_user_id": broadcaster,
            "events": missing,
            "method": "webhook",
        });
        let (status, body) = self
            .http
            .send(KickRequest {
                method: "POST",
                url: self.url(),
                token: token(),
                body: Some(payload.to_string().into_bytes()),
            })
            .await?;
        if !(200..300).contains(&status) {
            return Err(SubscribeError::Status(status));
        }
        let (refused, ids) = parse_create_reply(&body, created_n)?;
        // Reported BEFORE the refusal check: the ones that did succeed must still be deletable.
        on_created(ids);
        if refused > 0 {
            return Err(SubscribeError::Refused(refused));
        }
        Ok(Ensured {
            existing,
            created: created_n,
        })
    }

    /// Deletes subscriptions THIS host created (`DELETE /events/subscriptions?id=..&id=..`),
    /// resolving the community's own token fresh. Ids are shape-checked before they reach a URL.
    ///
    /// # Errors
    /// [`SubscribeError`]; a malformed id is [`SubscribeError::Malformed`] and nothing is sent.
    pub async fn unsubscribe(
        &self,
        community_id: i64,
        ids: &[String],
    ) -> Result<(), SubscribeError> {
        if ids.is_empty() {
            return Ok(());
        }
        let result = self.unsubscribe_inner(community_id, ids).await;
        let outcome = match &result {
            Ok(()) => "unsubscribe_ok",
            Err(_) => "unsubscribe_failed",
        };
        OUTCOMES.with_label_values(&[outcome]).inc();
        if let Err(e) = &result {
            tracing::error!(
                community_id,
                code = e.code(),
                count = ids.len(),
                "kick events unsubscribe failed; subscriptions may remain until Kick expires them"
            );
        }
        result
    }

    async fn unsubscribe_inner(
        &self,
        community_id: i64,
        ids: &[String],
    ) -> Result<(), SubscribeError> {
        if ids.iter().any(|i| !valid_subscription_id(i)) {
            return Err(SubscribeError::Malformed);
        }
        let binding = self
            .creds
            .binding(Some(community_id))
            .await
            .map_err(|e: CredError| SubscribeError::Credential(e.reason()))?;
        let query: Vec<String> = ids.iter().map(|i| format!("id={i}")).collect();
        let (status, _) = self
            .http
            .send(KickRequest {
                method: "DELETE",
                url: format!("{}?{}", self.url(), query.join("&")),
                token: Zeroizing::new(binding.oauth_token.expose().to_owned()),
                body: None,
            })
            .await?;
        if (200..300).contains(&status) {
            Ok(())
        } else {
            Err(SubscribeError::Status(status))
        }
    }

    /// Runs [`Self::ensure_tracked`] in the background with bounded exponential backoff
    /// ([`MAX_ATTEMPTS`]); only retryable errors retry, and the final failure is logged loudly
    /// and counted. The task is NEVER aborted mid-request (that could lose the ids of
    /// subscriptions Kick just created): cancel it with [`Cancel::cancel`] and await the handle;
    /// it finishes the in-flight attempt (bounded by the HTTP timeout) and stops retrying.
    pub fn spawn_ensure(
        self: &Arc<Self>,
        community_id: i64,
        created: Arc<std::sync::Mutex<Vec<String>>>,
        cancel: Arc<Cancel>,
    ) -> tokio::task::JoinHandle<()>
    where
        C: 'static,
        H: 'static,
    {
        let this = Arc::clone(self);
        tokio::spawn(async move {
            let record = move |ids: Vec<String>| {
                if let Ok(mut all) = created.lock() {
                    all.extend(ids);
                }
            };
            let mut delay = FIRST_BACKOFF;
            for attempt in 1..=MAX_ATTEMPTS {
                if cancel.is_cancelled() {
                    return;
                }
                match this.ensure_tracked(community_id, &record).await {
                    Ok(_) => return,
                    Err(e) if e.retryable() && attempt < MAX_ATTEMPTS => {
                        tokio::select! {
                            () = tokio::time::sleep(delay) => {}
                            () = cancel.cancelled() => return,
                        }
                        delay = delay.saturating_mul(2);
                    }
                    Err(e) => {
                        tracing::error!(
                            community_id,
                            code = e.code(),
                            attempt,
                            "kick events subscription gave up; webhooks will not arrive until the connection restarts"
                        );
                        return;
                    }
                }
            }
        })
    }
}

/// Cooperative cancellation of a [`KickSubscriber::spawn_ensure`] task.
#[derive(Default)]
pub struct Cancel {
    flag: std::sync::atomic::AtomicBool,
    notify: tokio::sync::Notify,
}

impl Cancel {
    /// Requests cancellation (sticky; wakes a sleeping retry).
    pub fn cancel(&self) {
        self.flag.store(true, std::sync::atomic::Ordering::SeqCst);
        self.notify.notify_one();
    }

    /// `true` once [`Self::cancel`] was called.
    #[must_use]
    pub fn is_cancelled(&self) -> bool {
        self.flag.load(std::sync::atomic::Ordering::SeqCst)
    }

    async fn cancelled(&self) {
        if !self.is_cancelled() {
            self.notify.notified().await;
        }
    }
}

/// Starts and stops the background subscription of one webhook route. The webhook attach seam
/// holds this trait object so it never depends on the credential/HTTP type parameters.
pub trait SubscribeOnAttach: Send + Sync {
    /// Begins ensuring the subscription for `community_id`, tracked under `route_key`.
    fn start(&self, community_id: i64, route_key: &str);
    /// Cancels the in-flight attempt for `route_key` and DELETEs every subscription it created
    /// (the route was detached).
    fn stop(&self, route_key: &str);
}

/// One tracked route: its background task and the subscription ids this host created for it.
struct Tracked {
    task: tokio::task::JoinHandle<()>,
    community_id: i64,
    created: Arc<std::sync::Mutex<Vec<String>>>,
    cancel: Arc<Cancel>,
}

/// [`SubscribeOnAttach`] over a [`KickSubscriber`]: one tracked background task per route. On
/// `stop` the attempt is cancelled cooperatively, awaited to its end, and every subscription it
/// created is DELETEd (with bounded retry), so a detached route never leaves live Kick
/// subscriptions behind.
pub struct BackgroundSubscriber<C: KickCredentialSource, H: KickEventsHttp> {
    inner: Arc<KickSubscriber<C, H>>,
    tasks: std::sync::Mutex<std::collections::HashMap<String, Tracked>>,
}

impl<C: KickCredentialSource + 'static, H: KickEventsHttp + 'static> BackgroundSubscriber<C, H> {
    /// Wraps `inner`.
    #[must_use]
    pub fn new(inner: KickSubscriber<C, H>) -> Self {
        Self {
            inner: Arc::new(inner),
            tasks: std::sync::Mutex::new(std::collections::HashMap::new()),
        }
    }

    /// Routes with a tracked task (test/observability helper).
    #[must_use]
    pub fn tracked(&self) -> usize {
        self.tasks.lock().map_or(0, |t| t.len())
    }

    /// Cancels `entry`, waits for its in-flight attempt to finish (so no created id is lost),
    /// then deletes everything it created with the same bounded backoff as the subscribe path.
    fn retire(&self, entry: Tracked) {
        entry.cancel.cancel();
        let inner = Arc::clone(&self.inner);
        tokio::spawn(async move {
            if entry.task.await.is_err() {
                tracing::error!("kick subscription task panicked; its subscriptions may remain");
            }
            let ids = entry.created.lock().map(|c| c.clone()).unwrap_or_default();
            let mut delay = FIRST_BACKOFF;
            for attempt in 1..=MAX_ATTEMPTS {
                match inner.unsubscribe(entry.community_id, &ids).await {
                    Ok(()) => return,
                    Err(e) if e.retryable() && attempt < MAX_ATTEMPTS => {
                        tokio::time::sleep(delay).await;
                        delay = delay.saturating_mul(2);
                    }
                    Err(_) => return,
                }
            }
        });
    }
}

impl<C: KickCredentialSource + 'static, H: KickEventsHttp + 'static> SubscribeOnAttach
    for BackgroundSubscriber<C, H>
{
    fn start(&self, community_id: i64, route_key: &str) {
        let created = Arc::new(std::sync::Mutex::new(Vec::new()));
        let cancel = Arc::new(Cancel::default());
        let task = self
            .inner
            .spawn_ensure(community_id, Arc::clone(&created), Arc::clone(&cancel));
        let entry = Tracked {
            task,
            community_id,
            created,
            cancel,
        };
        match self.tasks.lock() {
            Ok(mut t) => {
                if let Some(old) = t.insert(route_key.to_owned(), entry) {
                    self.retire(old);
                }
            }
            // A poisoned map means a panic elsewhere: fail loud rather than run untracked
            // (an untracked route could never be unsubscribed).
            Err(_) => {
                self.retire(entry);
                tracing::error!("kick subscription task table poisoned; subscription cancelled");
            }
        }
    }

    fn stop(&self, route_key: &str) {
        let removed = self.tasks.lock().ok().and_then(|mut t| t.remove(route_key));
        if let Some(entry) = removed {
            self.retire(entry);
        }
    }
}

/// The event names already subscribed (method `webhook`) for `broadcaster` in a list reply.
fn existing_events(body: &[u8], broadcaster: u64) -> Result<BTreeSet<String>, SubscribeError> {
    let v: Value = serde_json::from_slice(body).map_err(|_| SubscribeError::Malformed)?;
    let items = v
        .get("data")
        .and_then(Value::as_array)
        .ok_or(SubscribeError::Malformed)?;
    Ok(items
        .iter()
        .filter(|i| {
            i.get("broadcaster_user_id").and_then(Value::as_u64) == Some(broadcaster)
                && i.get("method").and_then(Value::as_str) == Some("webhook")
        })
        .filter_map(|i| i.get("event").and_then(Value::as_str))
        .map(str::to_owned)
        .collect())
}

/// A Kick subscription id: `[A-Za-z0-9_-]{1,64}` (it is sent back as a query value).
fn valid_subscription_id(id: &str) -> bool {
    (1..=64).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// `(refused, created_ids)` of a create reply: an item with a non-empty `error` is refused, every
/// other item must carry a well-formed `subscription_id`; the reply must answer every event asked
/// for.
fn parse_create_reply(body: &[u8], asked: usize) -> Result<(usize, Vec<String>), SubscribeError> {
    let v: Value = serde_json::from_slice(body).map_err(|_| SubscribeError::Malformed)?;
    let items = v
        .get("data")
        .and_then(Value::as_array)
        .ok_or(SubscribeError::Malformed)?;
    if items.len() != asked {
        return Err(SubscribeError::Malformed);
    }
    let (mut refused, mut ids) = (0, Vec::new());
    for i in items {
        let errored = i
            .get("error")
            .is_some_and(|e| e.as_str().is_none_or(|s| !s.is_empty()));
        if errored {
            refused += 1;
            continue;
        }
        let id = i
            .get("subscription_id")
            .and_then(Value::as_str)
            .filter(|id| valid_subscription_id(id))
            .ok_or(SubscribeError::Malformed)?;
        ids.push(id.to_owned());
    }
    Ok((refused, ids))
}

/// How long a positive owner check is trusted (a reconcile tick is far shorter).
const OWNER_TTL: Duration = Duration::from_secs(600);

/// Proves a binding's `broadcaster_user_id` is the owner of the community's OWN Kick token
/// (`GET /public/v1/users` with no id returns the token's user), so editable binding metadata can
/// never aim a webhook lane (or a subscription) at another channel. Only a positive answer is
/// cached; a mismatch or any failure is `false` (fail closed) and is re-checked next reconcile.
pub struct KickOwnerVerifier<C: KickCredentialSource, H: KickEventsHttp> {
    creds: C,
    http: H,
    base: String,
    verified: std::sync::Mutex<std::collections::HashMap<(i64, String), std::time::Instant>>,
}

impl<C: KickCredentialSource, H: KickEventsHttp> KickOwnerVerifier<C, H> {
    /// The production verifier: [`API_BASE`].
    #[must_use]
    pub fn new(creds: C, http: H) -> Self {
        Self {
            creds,
            http,
            base: API_BASE.to_owned(),
            verified: std::sync::Mutex::new(std::collections::HashMap::new()),
        }
    }

    /// Test seam: a different API base (a local fake Kick).
    #[must_use]
    pub fn with_base(mut self, base: impl Into<String>) -> Self {
        self.base = base.into();
        self
    }

    /// `true` only when `broadcaster` is the user id of `community_id`'s own token.
    pub async fn verify_owner(&self, community_id: i64, broadcaster: &str) -> bool {
        let key = (community_id, broadcaster.to_owned());
        if self
            .verified
            .lock()
            .ok()
            .and_then(|m| m.get(&key).copied())
            .is_some_and(|at| at.elapsed() < OWNER_TTL)
        {
            return true;
        }
        let outcome = self.check(community_id, broadcaster).await;
        let label = match outcome {
            Ok(true) => "owner_ok",
            Ok(false) => "owner_mismatch",
            Err(_) => "owner_error",
        };
        OUTCOMES.with_label_values(&[label]).inc();
        match outcome {
            Ok(true) => {
                if let Ok(mut m) = self.verified.lock() {
                    m.insert(key, std::time::Instant::now());
                }
                true
            }
            Ok(false) => {
                tracing::warn!(community_id, "kick broadcaster is not the token owner");
                false
            }
            Err(e) => {
                tracing::warn!(community_id, code = e.code(), "kick owner check failed");
                false
            }
        }
    }

    async fn check(&self, community_id: i64, broadcaster: &str) -> Result<bool, SubscribeError> {
        let want: u64 = broadcaster
            .parse()
            .map_err(|_| SubscribeError::NoBroadcaster)?;
        let binding = self
            .creds
            .binding(Some(community_id))
            .await
            .map_err(|e: CredError| SubscribeError::Credential(e.reason()))?;
        let (status, body) = self
            .http
            .send(KickRequest {
                method: "GET",
                url: format!("{}/users", self.base),
                token: Zeroizing::new(binding.oauth_token.expose().to_owned()),
                body: None,
            })
            .await?;
        if !(200..300).contains(&status) {
            return Err(SubscribeError::Status(status));
        }
        let v: Value = serde_json::from_slice(&body).map_err(|_| SubscribeError::Malformed)?;
        let items = v
            .get("data")
            .and_then(Value::as_array)
            .ok_or(SubscribeError::Malformed)?;
        // Exactly one user (the token's); anything else is not an owner proof.
        let [one] = items.as_slice() else {
            return Err(SubscribeError::Malformed);
        };
        let got = one
            .get("user_id")
            .and_then(Value::as_u64)
            .ok_or(SubscribeError::Malformed)?;
        Ok(got == want)
    }
}

impl<C: KickCredentialSource + 'static, H: KickEventsHttp + 'static>
    crate::connector_runtime::feed::WebhookLaneVerifier for KickOwnerVerifier<C, H>
{
    fn verify<'a>(
        &'a self,
        d: &'a crate::connector_runtime::feed::DesiredConnection,
    ) -> BoxFut<'a, bool> {
        Box::pin(async move {
            use crate::connector_runtime::feed::TransportPlan;
            let TransportPlan::Webhook(w) = &d.plan else {
                return false;
            };
            let super::webhook_verify::Scheme::Kick(spec) = &w.route.scheme else {
                return false;
            };
            self.verify_owner(d.key.community_id, &spec.broadcaster_id)
                .await
        })
    }
}

/// Lets a runtime that holds `Arc<R>` build the production [`KickCredentialSource`]
/// (`ResolverKickCredentials<SharedResolver<R>>`) without `R` being `Clone`.
pub struct SharedResolver<R>(pub Arc<R>);

impl<R: ConnectionResolve> ConnectionResolve for SharedResolver<R> {
    fn resolve(
        &self,
        r: ConnectionRef,
    ) -> impl std::future::Future<Output = Result<ResolvedConnection, ResolveError>> + Send {
        self.0.resolve(r)
    }

    fn list_bindings(
        &self,
        provider: &str,
        capability: Option<&str>,
        after: i64,
        limit: u32,
    ) -> impl std::future::Future<Output = Result<BindingPage, ResolveError>> + Send {
        self.0.list_bindings(provider, capability, after, limit)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::sync::Mutex;

    use connection_resolver::SecretString;

    use super::*;
    use crate::outbound_creds_kick::KickBinding;

    struct Creds(Result<Option<&'static str>, CredError>);

    impl KickCredentialSource for Creds {
        async fn binding(&self, c: Option<i64>) -> Result<KickBinding, CredError> {
            assert_eq!(c, Some(7), "the community scope is forwarded verbatim");
            let b = self.0?;
            Ok(KickBinding {
                oauth_token: SecretString::new("KICK-TOKEN".to_string()),
                broadcaster_user_id: b.map(str::to_string),
            })
        }
    }

    /// One recorded request: method, url, body, token.
    type Seen = (String, String, Option<String>, String);

    /// Scripted Kick: replies in order and records every request (method, url, body).
    struct Script {
        replies: Mutex<Vec<Result<(u16, String), SubscribeError>>>,
        seen: Mutex<Vec<Seen>>,
    }

    impl Script {
        fn new(r: Vec<Result<(u16, String), SubscribeError>>) -> Self {
            Self {
                replies: Mutex::new(r),
                seen: Mutex::new(vec![]),
            }
        }
    }

    impl KickEventsHttp for Arc<Script> {
        fn send(&self, req: KickRequest) -> BoxFut<'_, Result<(u16, Vec<u8>), SubscribeError>> {
            let this = Arc::clone(self);
            Box::pin(async move {
                this.seen.lock().unwrap().push((
                    req.method.to_string(),
                    req.url.clone(),
                    req.body
                        .as_ref()
                        .map(|b| String::from_utf8(b.clone()).unwrap()),
                    req.token.as_str().to_string(),
                ));
                this.replies
                    .lock()
                    .unwrap()
                    .remove(0)
                    .map(|(s, b)| (s, b.into_bytes()))
            })
        }
    }

    fn sub(
        c: Result<Option<&'static str>, CredError>,
        script: &Arc<Script>,
    ) -> KickSubscriber<Creds, Arc<Script>> {
        KickSubscriber::new(Creds(c), Arc::clone(script))
    }

    fn list(events: &[&str], broadcaster: u64, method: &str) -> String {
        let data: Vec<Value> = events
            .iter()
            .map(|e| json!({"event": e, "broadcaster_user_id": broadcaster, "method": method, "id": "x"}))
            .collect();
        json!({ "data": data }).to_string()
    }

    fn created(n: usize, err: Option<&str>) -> String {
        let data: Vec<Value> = (0..n)
            .map(|i| match err {
                Some(e) if i == 0 => json!({"name": "x", "error": e}),
                _ => json!({"name": "x", "subscription_id": format!("sub{i}")}),
            })
            .collect();
        json!({ "data": data }).to_string()
    }

    #[tokio::test]
    async fn subscribes_every_event_when_none_exist_with_a_json_integer_broadcaster() {
        let script = Arc::new(Script::new(vec![
            Ok((200, list(&[], 555, "webhook"))),
            Ok((200, created(EVENTS.len(), None))),
        ]));
        let got = sub(Ok(Some("555")), &script).ensure(7).await.unwrap();
        assert_eq!(
            got,
            Ensured {
                existing: 0,
                created: EVENTS.len()
            }
        );
        let seen = script.seen.lock().unwrap();
        assert_eq!(seen.len(), 2);
        assert_eq!(seen[0].0, "GET");
        assert_eq!(
            seen[0].1,
            "https://api.kick.com/public/v1/events/subscriptions?broadcaster_user_id=555"
        );
        assert_eq!(seen[1].0, "POST");
        assert_eq!(
            seen[1].1,
            "https://api.kick.com/public/v1/events/subscriptions"
        );
        let body: Value = serde_json::from_str(seen[1].2.as_ref().unwrap()).unwrap();
        assert_eq!(body["broadcaster_user_id"], 555);
        assert!(body["broadcaster_user_id"].is_u64());
        assert_eq!(body["method"], "webhook");
        let names: Vec<&str> = body["events"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, EVENTS.map(|(n, _)| n));
        assert!(body["events"]
            .as_array()
            .unwrap()
            .iter()
            .all(|e| e["version"] == 1));
        // The community's own token rides both calls (host-side only).
        assert!(seen.iter().all(|s| s.3 == "KICK-TOKEN"));
    }

    #[tokio::test]
    async fn only_missing_events_are_created_and_a_full_set_makes_no_post() {
        let have = ["chat.message.sent", "channel.followed"];
        let script = Arc::new(Script::new(vec![
            Ok((200, list(&have, 555, "webhook"))),
            Ok((200, created(EVENTS.len() - 2, None))),
        ]));
        let got = sub(Ok(Some("555")), &script).ensure(7).await.unwrap();
        assert_eq!((got.existing, got.created), (2, EVENTS.len() - 2));
        let body: Value =
            serde_json::from_str(script.seen.lock().unwrap()[1].2.as_ref().unwrap()).unwrap();
        let names: Vec<&str> = body["events"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["name"].as_str().unwrap())
            .collect();
        assert!(!names.contains(&"chat.message.sent") && !names.contains(&"channel.followed"));

        let all: Vec<&str> = EVENTS.iter().map(|(n, _)| *n).collect();
        let script = Arc::new(Script::new(vec![Ok((200, list(&all, 555, "webhook")))]));
        let got = sub(Ok(Some("555")), &script).ensure(7).await.unwrap();
        assert_eq!((got.existing, got.created), (EVENTS.len(), 0));
        assert_eq!(
            script.seen.lock().unwrap().len(),
            1,
            "no POST when complete"
        );
    }

    #[tokio::test]
    async fn another_broadcasters_or_a_non_webhook_subscription_does_not_count() {
        let script = Arc::new(Script::new(vec![
            Ok((200, list(&["chat.message.sent"], 999, "webhook"))),
            Ok((200, created(EVENTS.len(), None))),
        ]));
        assert_eq!(
            sub(Ok(Some("555")), &script)
                .ensure(7)
                .await
                .unwrap()
                .created,
            EVENTS.len()
        );
        let script = Arc::new(Script::new(vec![
            Ok((200, list(&["chat.message.sent"], 555, "websocket"))),
            Ok((200, created(EVENTS.len(), None))),
        ]));
        assert_eq!(
            sub(Ok(Some("555")), &script)
                .ensure(7)
                .await
                .unwrap()
                .created,
            EVENTS.len()
        );
    }

    #[tokio::test]
    async fn refusals_and_failures_are_typed_and_loud() {
        // A per-event error from Kick.
        let script = Arc::new(Script::new(vec![
            Ok((200, list(&[], 555, "webhook"))),
            Ok((200, created(EVENTS.len(), Some("forbidden")))),
        ]));
        let e = sub(Ok(Some("555")), &script).ensure(7).await.unwrap_err();
        assert_eq!(e, SubscribeError::Refused(1));
        assert!(!e.retryable());
        // A reply that does not answer every event is malformed, not success.
        let script = Arc::new(Script::new(vec![
            Ok((200, list(&[], 555, "webhook"))),
            Ok((200, created(1, None))),
        ]));
        assert_eq!(
            sub(Ok(Some("555")), &script).ensure(7).await.unwrap_err(),
            SubscribeError::Malformed
        );
        // Non-2xx on list and on create.
        for (first, second, want) in [
            (Ok((401, String::new())), None, SubscribeError::Status(401)),
            (
                Ok((200, list(&[], 555, "webhook"))),
                Some(Ok((500, String::new()))),
                SubscribeError::Status(500),
            ),
            (
                Ok((200, "not json".into())),
                None,
                SubscribeError::Malformed,
            ),
            (
                Err(SubscribeError::Unreachable),
                None,
                SubscribeError::Unreachable,
            ),
        ] {
            let mut r = vec![first];
            r.extend(second);
            let script = Arc::new(Script::new(r));
            assert_eq!(
                sub(Ok(Some("555")), &script).ensure(7).await.unwrap_err(),
                want
            );
        }
    }

    #[tokio::test]
    async fn credential_failures_and_a_missing_broadcaster_make_no_request() {
        for (c, want) in [
            (
                Err(CredError::NoBinding),
                SubscribeError::Credential(CredError::NoBinding.reason()),
            ),
            (
                Err(CredError::TokenExpired),
                SubscribeError::Credential(CredError::TokenExpired.reason()),
            ),
            (Ok(None), SubscribeError::NoBroadcaster),
        ] {
            let script = Arc::new(Script::new(vec![]));
            assert_eq!(sub(c, &script).ensure(7).await.unwrap_err(), want);
            assert!(script.seen.lock().unwrap().is_empty());
        }
        assert!(SubscribeError::Credential(CredError::TokenExpired.reason()).retryable());
        assert!(!SubscribeError::Credential(CredError::NoBinding.reason()).retryable());
        assert!(
            SubscribeError::Status(503).retryable() && !SubscribeError::Status(403).retryable()
        );
    }

    #[tokio::test(start_paused = true)]
    async fn spawn_ensure_retries_transient_errors_then_succeeds_and_stops_on_a_permanent_one() {
        let all: Vec<&str> = EVENTS.iter().map(|(n, _)| *n).collect();
        let script = Arc::new(Script::new(vec![
            Err(SubscribeError::Unreachable),
            Ok((503, String::new())),
            Ok((200, list(&all, 555, "webhook"))),
        ]));
        let s = Arc::new(sub(Ok(Some("555")), &script));
        s.spawn_ensure(7, Default::default(), Default::default())
            .await
            .unwrap();
        assert_eq!(script.seen.lock().unwrap().len(), 3);

        let script = Arc::new(Script::new(vec![Ok((403, String::new()))]));
        let s = Arc::new(sub(Ok(Some("555")), &script));
        s.spawn_ensure(7, Default::default(), Default::default())
            .await
            .unwrap();
        assert_eq!(script.seen.lock().unwrap().len(), 1, "permanent: no retry");
    }

    fn ids(prefix_count: usize) -> Vec<String> {
        (0..prefix_count).map(|i| format!("sub{i}")).collect()
    }

    #[tokio::test]
    async fn created_ids_are_reported_even_when_part_of_the_batch_is_refused() {
        let got = Arc::new(Mutex::new(Vec::<String>::new()));
        let sink = {
            let got = Arc::clone(&got);
            move |v: Vec<String>| got.lock().unwrap().extend(v)
        };
        let script = Arc::new(Script::new(vec![
            Ok((200, list(&[], 555, "webhook"))),
            Ok((200, created(EVENTS.len(), Some("forbidden")))),
        ]));
        let e = sub(Ok(Some("555")), &script)
            .ensure_tracked(7, &sink)
            .await
            .unwrap_err();
        assert_eq!(e, SubscribeError::Refused(1));
        assert_eq!(*got.lock().unwrap(), ids(EVENTS.len())[1..].to_vec());
        // A success reports every created id.
        got.lock().unwrap().clear();
        let script = Arc::new(Script::new(vec![
            Ok((200, list(&[], 555, "webhook"))),
            Ok((200, created(EVENTS.len(), None))),
        ]));
        sub(Ok(Some("555")), &script)
            .ensure_tracked(7, &sink)
            .await
            .unwrap();
        assert_eq!(*got.lock().unwrap(), ids(EVENTS.len()));
        // A success item without a well-formed id is a malformed reply, not an untracked sub.
        let bad = json!({"data": EVENTS.iter().map(|_| json!({"subscription_id": "a/b"})).collect::<Vec<_>>()});
        let script = Arc::new(Script::new(vec![
            Ok((200, list(&[], 555, "webhook"))),
            Ok((200, bad.to_string())),
        ]));
        assert_eq!(
            sub(Ok(Some("555")), &script).ensure(7).await.unwrap_err(),
            SubscribeError::Malformed
        );
    }

    #[tokio::test]
    async fn unsubscribe_deletes_exactly_the_given_ids_with_the_communitys_token() {
        let script = Arc::new(Script::new(vec![Ok((200, String::new()))]));
        sub(Ok(Some("555")), &script)
            .unsubscribe(7, &ids(2))
            .await
            .unwrap();
        {
            let seen = script.seen.lock().unwrap();
            assert_eq!(seen[0].0, "DELETE");
            assert_eq!(
                seen[0].1,
                "https://api.kick.com/public/v1/events/subscriptions?id=sub0&id=sub1"
            );
            assert_eq!(seen[0].3, "KICK-TOKEN");
        }
        // Nothing to delete: no request. A hostile id: refused, nothing sent. A non-2xx: loud.
        let script = Arc::new(Script::new(vec![Ok((500, String::new()))]));
        let s = sub(Ok(Some("555")), &script);
        s.unsubscribe(7, &[]).await.unwrap();
        assert!(script.seen.lock().unwrap().is_empty());
        assert_eq!(
            s.unsubscribe(7, &["a&b".to_string()]).await.unwrap_err(),
            SubscribeError::Malformed
        );
        assert!(script.seen.lock().unwrap().is_empty());
        assert_eq!(
            s.unsubscribe(7, &ids(1)).await.unwrap_err(),
            SubscribeError::Status(500)
        );
    }

    #[tokio::test(start_paused = true)]
    async fn stopping_a_route_deletes_the_subscriptions_it_created() {
        let script = Arc::new(Script::new(vec![
            Ok((200, list(&[], 555, "webhook"))),
            Ok((200, created(EVENTS.len(), None))),
            Ok((204, String::new())),
        ]));
        let bg = BackgroundSubscriber::new(sub(Ok(Some("555")), &script));
        bg.start(7, "route-a");
        for _ in 0..50 {
            tokio::time::sleep(Duration::from_millis(10)).await;
            if script.seen.lock().unwrap().len() >= 2 {
                break;
            }
        }
        assert_eq!(bg.tracked(), 1);
        bg.stop("route-a");
        assert_eq!(bg.tracked(), 0);
        for _ in 0..50 {
            tokio::time::sleep(Duration::from_millis(10)).await;
            if script.seen.lock().unwrap().len() >= 3 {
                break;
            }
        }
        {
            let seen = script.seen.lock().unwrap();
            assert_eq!(seen.len(), 3, "list + create + delete");
            assert_eq!(seen[2].0, "DELETE");
            for id in ids(EVENTS.len()) {
                assert!(seen[2].1.contains(&format!("id={id}")), "{id}");
            }
        }
        // Stopping an unknown route (or one that created nothing) sends nothing.
        bg.stop("nope");
        assert_eq!(script.seen.lock().unwrap().len(), 3);
    }

    fn users(user_id: Value) -> String {
        json!({"data": [{"user_id": user_id, "name": "x"}]}).to_string()
    }

    fn verifier(
        c: Result<Option<&'static str>, CredError>,
        script: &Arc<Script>,
    ) -> KickOwnerVerifier<Creds, Arc<Script>> {
        KickOwnerVerifier::new(Creds(c), Arc::clone(script))
    }

    #[tokio::test]
    async fn the_owner_check_passes_only_for_the_token_owners_own_id_and_fails_closed() {
        // Match, then served from the positive cache (no second request).
        let script = Arc::new(Script::new(vec![Ok((200, users(json!(555))))]));
        let v = verifier(Ok(None), &script);
        assert!(v.verify_owner(7, "555").await);
        assert!(v.verify_owner(7, "555").await);
        {
            let seen = script.seen.lock().unwrap();
            assert_eq!(seen.len(), 1);
            assert_eq!(seen[0].1, "https://api.kick.com/public/v1/users");
            assert_eq!(seen[0].3, "KICK-TOKEN");
        }
        // Mismatch, wrong shapes, non-2xx, transport error: all false, none cached.
        for reply in [
            Ok((200, users(json!(556)))),
            Ok((200, users(json!("555")))),
            Ok((200, json!({"data": []}).to_string())),
            Ok((
                200,
                json!({"data": [{"user_id": 555}, {"user_id": 556}]}).to_string(),
            )),
            Ok((200, "nope".to_string())),
            Ok((401, String::new())),
            Err(SubscribeError::Unreachable),
        ] {
            let script = Arc::new(Script::new(vec![reply.clone(), reply]));
            let v = verifier(Ok(None), &script);
            assert!(!v.verify_owner(7, "555").await);
            assert!(!v.verify_owner(7, "555").await, "failures are never cached");
        }
        // A non-numeric claimed id and a missing credential never reach Kick.
        let script = Arc::new(Script::new(vec![]));
        assert!(!verifier(Ok(None), &script).verify_owner(7, "abc").await);
        assert!(
            !verifier(Err(CredError::NoBinding), &script)
                .verify_owner(7, "555")
                .await
        );
        assert!(script.seen.lock().unwrap().is_empty());
    }

    #[test]
    fn requests_never_debug_print_the_token_or_url() {
        let r = KickRequest {
            method: "GET",
            url: "https://x/secret".into(),
            token: Zeroizing::new("KICK-TOKEN".into()),
            body: None,
        };
        let shown = format!("{r:?}");
        assert!(!shown.contains("KICK-TOKEN") && !shown.contains("secret"));
    }

    #[test]
    fn metrics_register_once_and_count_outcomes() {
        let reg = prometheus::Registry::new();
        register_metrics(&reg).unwrap();
        assert!(register_metrics(&reg).is_err());
        // A private label: the shared counter is also bumped by the parallel `ensure` tests.
        let before = outcome_count("probe");
        OUTCOMES.with_label_values(&["probe"]).inc();
        assert_eq!(outcome_count("probe"), before + 1);
    }
}
