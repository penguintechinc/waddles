//! Host-side interaction intake: capture the reply token, ack within Discord's 3 seconds,
//! and dedupe, BEFORE any bundle sees the interaction.
//!
//! Two doors, one store ([`crate::interaction_store`]):
//!
//! * **Gateway** (`INTERACTION_CREATE` frames): the gateway session is the authenticated
//!   channel, but Discord expects an explicit initial response, so [`GatewayTap`] stores the
//!   record (`SET NX` = idempotency gate) and then POSTs the deferred ack
//!   (`/interactions/{id}/{token}/callback`, type 5 / 6 / 8) itself. The driver then forwards
//!   the frame to the connector only on [`TapVerdict::Pass`].
//! * **Webhook** (HTTP interactions): the generic route verifies the Ed25519 signature
//!   (`webhook_verify`) and answers the deferred ack inline; [`store_verified`] then stores the
//!   record from the VERIFIED body. An unverified body never reaches it (an attacker must not
//!   be able to plant a record keyed by an id they choose).
//!
//! Fail closed: a store fault or a failed ack means NO routing (a reply could never be
//! sent), reported loudly with a fixed reason. The token is never logged, `Debug`-printed or
//! placed in an error; the ack URL is built per call and dropped.

use std::sync::{Arc, LazyLock};
use std::time::Duration;

use bundle_host_http::guarded::{GuardedClient, GuardedLimits};

use crate::commands_reconcile::BoxFut;
use crate::interaction_store::{
    capture, CaptureError, Captured, InteractionStore, InteractionType,
};

/// Discord REST base the host pins egress to.
pub const API_BASE: &str = "https://discord.com/api/v10";
/// Initial-response budget (Discord allows 3 s; leave margin).
pub const ACK_TIMEOUT: Duration = Duration::from_millis(2500);
const PLATFORM: &str = "discord";

static TAP_TOTAL: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_interaction_intake_total",
            "Discord interaction intake outcomes (fixed reason codes)",
        ),
        &["door", "outcome"],
    )
    .expect("valid metric definition")
});

/// Registers the intake counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(TAP_TOTAL.clone()))
}

/// Count of one outcome (test/observability helper).
#[must_use]
pub fn intake_count(door: &str, outcome: &str) -> u64 {
    TAP_TOTAL.with_label_values(&[door, outcome]).get()
}

fn note(door: &'static str, outcome: &'static str) {
    TAP_TOTAL.with_label_values(&[door, outcome]).inc();
}

/// What the driver does with a frame after the tap saw it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TapVerdict {
    /// Not an interaction, or a freshly stored + acked one: forward to the connector.
    Pass,
    /// A redelivery of an interaction already handled: do NOT forward (report delivered).
    Duplicate,
    /// Could not store/ack (fixed reason): do NOT forward, do not report delivered.
    Refuse(&'static str),
}

/// Sees every frame of a platform's connection before the connector does.
pub trait FrameTap: Send + Sync {
    /// Inspects `bytes`; only `platform`s the tap owns are touched.
    fn on_frame<'a>(&'a self, platform: &'a str, bytes: &'a [u8]) -> BoxFut<'a, TapVerdict>;
}

/// Why an ack could not be delivered (never carries the URL or token).
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum AckError {
    /// Egress failed or timed out.
    #[error("interaction ack transport failure")]
    Transport,
    /// Discord answered non-2xx (token expired/unknown or already acked).
    #[error("interaction ack refused by discord")]
    Refused,
}

/// Posts the initial interaction response through the SSRF-guarded client.
pub struct InteractionAcker {
    client: GuardedClient,
    api_base: String,
}

impl InteractionAcker {
    /// Production: the real REST base through a default (empty-allowlist) guarded client.
    #[must_use]
    pub fn new() -> Self {
        Self::with_endpoint(
            GuardedClient::new(
                GuardedLimits {
                    max_redirects: 0,
                    timeout: ACK_TIMEOUT,
                    max_response_bytes: 16 * 1024,
                },
                Vec::new(),
            ),
            API_BASE.to_string(),
        )
    }

    /// Explicit client + REST base (tests point it at a local fake Discord).
    #[must_use]
    pub fn with_endpoint(client: GuardedClient, api_base: String) -> Self {
        Self { client, api_base }
    }

    /// Sends the type-appropriate deferred ack for `c`.
    ///
    /// # Errors
    /// [`AckError`], with no URL/token content.
    pub async fn ack(&self, c: &Captured) -> Result<(), AckError> {
        let body = serde_json::to_vec(&c.kind.ack_body()).map_err(|_| AckError::Transport)?;
        let url = format!(
            "{}/interactions/{}/{}/callback",
            self.api_base, c.id, c.record.token
        );
        let resp = tokio::time::timeout(
            ACK_TIMEOUT,
            self.client.fetch(
                reqwest::Method::POST,
                &url,
                vec![("Content-Type".to_string(), "application/json".to_string())],
                Some(body),
            ),
        )
        .await
        .map_err(|_| AckError::Transport)?
        .map_err(|_| AckError::Transport)?;
        if (200..300).contains(&resp.status) {
            Ok(())
        } else {
            Err(AckError::Refused)
        }
    }
}

impl Default for InteractionAcker {
    fn default() -> Self {
        Self::new()
    }
}

/// The gateway door: store, dedupe, ack.
pub struct GatewayTap {
    store: InteractionStore,
    acker: Arc<InteractionAcker>,
}

impl GatewayTap {
    /// Builds the tap over a store and an acker.
    #[must_use]
    pub fn new(store: InteractionStore, acker: Arc<InteractionAcker>) -> Self {
        Self { store, acker }
    }

    async fn handle(&self, bytes: &[u8]) -> TapVerdict {
        let c = match capture(bytes) {
            Ok(c) => c,
            Err(CaptureError::NotAnInteraction) => return TapVerdict::Pass,
            // Forward the unsupported type: the connector reports it as the typed error it is.
            Err(CaptureError::UnsupportedType(_)) => return TapVerdict::Pass,
            Err(CaptureError::Malformed(field)) => {
                note("gateway", "malformed");
                tracing::warn!(
                    platform = PLATFORM,
                    field,
                    "interaction frame malformed; refused"
                );
                return TapVerdict::Refuse("interaction_malformed");
            }
        };
        if c.kind == InteractionType::Ping {
            // A gateway session never receives PING; nothing to ack or route.
            return TapVerdict::Duplicate;
        }
        match self.store.put_new(&c.id, &c.record).await {
            Ok(true) => {}
            Ok(false) => {
                note("gateway", "duplicate");
                return TapVerdict::Duplicate;
            }
            Err(_) => {
                note("gateway", "store_unavailable");
                tracing::error!(
                    platform = PLATFORM,
                    "interaction store unavailable; interaction refused (no reply possible)"
                );
                return TapVerdict::Refuse("interaction_store_unavailable");
            }
        }
        match self.acker.ack(&c).await {
            Ok(()) => {
                note("gateway", "acked");
                TapVerdict::Pass
            }
            Err(e) => {
                note("gateway", "ack_failed");
                tracing::warn!(platform = PLATFORM, error = %e, "interaction ack failed; not routed");
                TapVerdict::Refuse("interaction_ack_failed")
            }
        }
    }
}

impl FrameTap for GatewayTap {
    fn on_frame<'a>(&'a self, platform: &'a str, bytes: &'a [u8]) -> BoxFut<'a, TapVerdict> {
        Box::pin(async move {
            if platform != PLATFORM {
                return TapVerdict::Pass;
            }
            self.handle(bytes).await
        })
    }
}

/// `true` for a PING interaction body (type 1): answered inline, nothing to store.
#[must_use]
pub fn is_ping(body: &[u8]) -> bool {
    serde_json::from_slice::<serde_json::Value>(body)
        .ok()
        .and_then(|v| v.get("type").and_then(serde_json::Value::as_u64))
        == Some(1)
}

/// The webhook door: stores a signature-VERIFIED interaction body (the inline response
/// already acked it). `Ok(true)` = first sighting, `Ok(false)` = duplicate. PING and
/// non-interaction bodies are a no-op `Ok(false)`.
///
/// # Errors
/// A fixed reason when the body is malformed or the store is unavailable; the HTTP layer then
/// answers 503 rather than leave a reply-less interaction.
pub async fn store_verified(
    store: &InteractionStore,
    verified_body: &[u8],
) -> Result<bool, &'static str> {
    // PING is answered inline and carries nothing to store; decide before the strict parse.
    if is_ping(verified_body) {
        return Ok(false);
    }
    let c = match capture(verified_body) {
        Ok(c) => c,
        Err(CaptureError::NotAnInteraction | CaptureError::UnsupportedType(_)) => {
            return Ok(false);
        }
        Err(CaptureError::Malformed(_)) => {
            note("webhook", "malformed");
            return Err("interaction_malformed");
        }
    };
    if c.kind == InteractionType::Ping {
        return Ok(false);
    }
    match store.put_new(&c.id, &c.record).await {
        Ok(first) => {
            note("webhook", if first { "stored" } else { "duplicate" });
            Ok(first)
        }
        Err(_) => {
            note("webhook", "store_unavailable");
            Err("interaction_store_unavailable")
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use serde_json::json;
    use std::net::{IpAddr, Ipv4Addr};
    use wiremock::matchers::{body_json, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    const TOKEN: &str = "tapTOKEN-secret.1";

    async fn store() -> InteractionStore {
        let url = std::env::var("VALKEY_URL").expect("VALKEY_URL (a local valkey) is required");
        InteractionStore::new(
            redis::Client::open(url)
                .unwrap()
                .get_connection_manager()
                .await
                .unwrap(),
        )
    }

    fn unique() -> String {
        let n = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        format!("5{n}")
    }

    fn frame(id: &str, t: u64) -> Vec<u8> {
        serde_json::to_vec(&json!({"op":0,"s":1,"t":"INTERACTION_CREATE","d":{
            "id": id, "application_id":"42","type":t,"token":TOKEN,"guild_id":"111",
            "data":{"name":"ping"}}}))
        .unwrap()
    }

    fn acker(server: &MockServer) -> Arc<InteractionAcker> {
        Arc::new(InteractionAcker::with_endpoint(
            GuardedClient::new(
                GuardedLimits {
                    max_redirects: 0,
                    timeout: Duration::from_secs(5),
                    max_response_bytes: 16 * 1024,
                },
                vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
            ),
            format!("{}/api/v10", server.uri()),
        ))
    }

    #[tokio::test]
    async fn a_command_is_stored_acked_with_type_5_and_forwarded_once() {
        let server = MockServer::start().await;
        let id = unique();
        Mock::given(method("POST"))
            .and(path(format!("/api/v10/interactions/{id}/{TOKEN}/callback")))
            .and(body_json(json!({"type":5})))
            .respond_with(ResponseTemplate::new(204))
            .expect(1)
            .mount(&server)
            .await;
        let s = store().await;
        let tap = GatewayTap::new(s.clone(), acker(&server));
        assert_eq!(
            tap.on_frame("discord", &frame(&id, 2)).await,
            TapVerdict::Pass
        );
        let rec = s.get(&id).await.unwrap().unwrap();
        assert_eq!(rec.token, TOKEN);
        assert_eq!(rec.guild_id.as_deref(), Some("111"));
        // Redelivery (resume): no second ack, no second routing.
        assert_eq!(
            tap.on_frame("discord", &frame(&id, 2)).await,
            TapVerdict::Duplicate
        );
        // `expect(1)` is verified when the server drops.
    }

    #[tokio::test]
    async fn component_modal_and_autocomplete_use_their_own_ack_types() {
        for (t, body) in [
            (3, json!({"type":6})),
            (5, json!({"type":6})),
            (4, json!({"type":8,"data":{"choices":[]}})),
        ] {
            let server = MockServer::start().await;
            let id = unique();
            Mock::given(method("POST"))
                .and(path(format!("/api/v10/interactions/{id}/{TOKEN}/callback")))
                .and(body_json(body))
                .respond_with(ResponseTemplate::new(204))
                .expect(1)
                .mount(&server)
                .await;
            let tap = GatewayTap::new(store().await, acker(&server));
            assert_eq!(
                tap.on_frame("discord", &frame(&id, t)).await,
                TapVerdict::Pass
            );
        }
    }

    #[tokio::test]
    async fn non_interaction_frames_other_platforms_and_unsupported_types_pass_untouched() {
        let server = MockServer::start().await;
        let tap = GatewayTap::new(store().await, acker(&server));
        let msg = serde_json::to_vec(&json!({"op":0,"t":"MESSAGE_CREATE","d":{}})).unwrap();
        assert_eq!(tap.on_frame("discord", &msg).await, TapVerdict::Pass);
        assert_eq!(
            tap.on_frame("twitch", &frame(&unique(), 2)).await,
            TapVerdict::Pass
        );
        assert_eq!(
            tap.on_frame("discord", &frame(&unique(), 9)).await,
            TapVerdict::Pass
        );
        assert!(server.received_requests().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn a_failed_ack_or_malformed_interaction_is_refused_not_routed() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(404))
            .mount(&server)
            .await;
        let tap = GatewayTap::new(store().await, acker(&server));
        assert_eq!(
            tap.on_frame("discord", &frame(&unique(), 2)).await,
            TapVerdict::Refuse("interaction_ack_failed")
        );
        let bad = serde_json::to_vec(&json!({"op":0,"t":"INTERACTION_CREATE","d":{
            "id":"x","application_id":"42","type":2,"token":TOKEN}}))
        .unwrap();
        let before = intake_count("gateway", "malformed");
        assert_eq!(
            tap.on_frame("discord", &bad).await,
            TapVerdict::Refuse("interaction_malformed")
        );
        assert!(intake_count("gateway", "malformed") > before);
    }

    #[tokio::test]
    async fn the_webhook_door_stores_verified_bodies_and_dedupes() {
        let s = store().await;
        let id = unique();
        let body = serde_json::to_vec(&json!({"id":id,"application_id":"42","type":2,
            "token":TOKEN,"guild_id":"111"}))
        .unwrap();
        assert_eq!(store_verified(&s, &body).await, Ok(true));
        assert_eq!(store_verified(&s, &body).await, Ok(false));
        assert!(s.get(&id).await.unwrap().is_some());
        let ping = serde_json::to_vec(&json!({"id":unique(),"application_id":"42","type":1,
            "token":TOKEN}))
        .unwrap();
        assert_eq!(store_verified(&s, &ping).await, Ok(false));
        let bad = serde_json::to_vec(&json!({"id":"x","type":2})).unwrap();
        assert_eq!(store_verified(&s, &bad).await, Err("interaction_malformed"));
    }
}
