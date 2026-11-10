//! Per-user active-community context for slash interactions, and the built-in `/context`.
//!
//! A Discord guild can be paired to several communities. Slash is guild-wide, so WHICH
//! community processes an invocation is the invoking user's active context (hub-api
//! `active-context`, default = first-registered community). This module is the host side:
//!
//! * [`ContextSink`] wraps the connection's [`EventSink`]. For every `interaction.*` event it
//!   resolves the actor's active community (tokenized user UUID only; hub-api never sees a
//!   Discord id) and stamps `active_community` (a community `public_id` UUID) into the
//!   payload; routing keys on that, never on a name.
//! * Routing is recorded host-side: once the community is resolved, [`ContextSink`] verifies the
//!   stored interaction against the connection's own guild and application id and calls
//!   [`InteractionStore::set_routed`] with the hub-resolved numeric community. Only then can the
//!   sender-side ownership check answer that interaction; none of the three values comes from
//!   the guest or the payload. A failed check answers the user generically and publishes nothing.
//! * `/context` is handled HERE, never published to a bundle: bare `/context` echoes the
//!   current community and lists the switchable ones; `/context <name>` matches the display
//!   name (or a listed UUID) client-side and PUTs the `public_id`. Names never resolve at
//!   hub-api.
//! * Fail closed: a lookup failure, a missing actor or an unreadable payload answers the
//!   user with a generic ephemeral notice (via the host-held token) and publishes nothing.
//!
//! The reply goes out as an interaction followup built by the host from the stored token;
//! no bundle is involved and no token appears in a log.

use std::sync::Arc;

use bundle_host_http::guarded::GuardedClient;
use hub_client::active_context::ActiveContextClient;
use hub_client::pb;
use serde_json::Value;

use crate::commands_reconcile::BoxFut;
use crate::connector::sink::{EventSink, SinkError};
use crate::connector::types::{ConnectionCtx, ConnectorEvent};
use crate::interaction_store::InteractionStore;
use prometheus::{HistogramOpts, HistogramVec, IntCounterVec, Opts, Registry};
use tracing::Instrument;

/// Machine-JWT scope svc-ingest holds for the active-context endpoints.
pub const ACTIVE_CONTEXT_JWT_SCOPE: &str = hub_client::active_context::SCOPE;
/// Discord message flag: only the invoker sees the reply.
const FLAG_EPHEMERAL: u64 = 64;
/// Discord's message content cap.
const MAX_REPLY_CHARS: usize = 2000;

/// One switchable community.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Community {
    /// Community `public_id` UUID.
    pub public_id: String,
    /// Display name (a mutable alias; matched only client-side).
    pub display_name: String,
    /// 0 = first-registered default.
    pub position: u32,
}

/// A user's active context for a source.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContextView {
    /// Active community `public_id`.
    pub community_public_id: String,
    /// Active community NUMERIC id (the key the interaction ownership check compares).
    /// Required: a hub answer without it is `Malformed`, so routing fails closed.
    pub community_id: i64,
    /// Active community display name.
    pub community_name: String,
    /// Whether the user chose it (vs the source default).
    pub explicit: bool,
    /// Switchable communities.
    pub communities: Vec<Community>,
}

impl TryFrom<pb::ActiveContext> for ContextView {
    type Error = ContextError;

    /// Converts the wire answer; a missing/zero numeric id or an empty `public_id` is
    /// `Malformed` (hub numeric ids start at 1; proto3 cannot tell "unset" from 0).
    fn try_from(a: pb::ActiveContext) -> Result<Self, ContextError> {
        if a.community_id <= 0 || a.community_public_id.is_empty() {
            return Err(ContextError::Malformed);
        }
        Ok(Self {
            community_public_id: a.community_public_id,
            community_id: a.community_id,
            community_name: a.community_name,
            explicit: a.explicit,
            communities: a
                .communities
                .into_iter()
                .map(|c| Community {
                    public_id: c.public_id,
                    display_name: c.display_name,
                    position: c.position,
                })
                .collect(),
        })
    }
}

/// Why a hub call failed (never carries ids or names).
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum ContextError {
    /// hub-api unreachable / non-2xx / token failure.
    #[error("active context unavailable")]
    Unavailable,
    /// Response not the documented shape.
    #[error("active context malformed")]
    Malformed,
}

/// hub-api active-context access.
pub trait ContextSource: Send + Sync {
    /// `GET active-context`.
    fn get<'a>(
        &'a self,
        source_id: &'a str,
        user: &'a str,
    ) -> BoxFut<'a, Result<ContextView, ContextError>>;
    /// `PUT active-context` with the community `public_id`.
    fn put<'a>(
        &'a self,
        source_id: &'a str,
        user: &'a str,
        community: &'a str,
    ) -> BoxFut<'a, Result<ContextView, ContextError>>;
}

/// Production [`ContextSource`]: hub-api's internal gRPC `ActiveContextService` (TLS, machine JWT
/// with scope [`ACTIVE_CONTEXT_JWT_SCOPE`]) over one long-lived channel.
pub struct HubContextSource {
    client: ActiveContextClient,
}

/// Why [`HubContextSource::connect`] refused to start.
#[derive(Debug, thiserror::Error)]
pub enum HubContextConnectError {
    /// The endpoint is not `https://` -- hub-api's internal gRPC listener is TLS-only.
    #[error("HUB_API_GRPC_ENDPOINT must be an https:// URL (hub-api's internal gRPC is TLS-only)")]
    NotTls,
    /// Dialing/authenticating the channel failed (CA unreadable, unreachable, untrusted).
    #[error("hub-api gRPC channel unavailable: {0}")]
    Client(#[from] hub_client::HubClientError),
}

impl HubContextSource {
    /// Dials hub-api's internal gRPC `endpoint` (`HUB_API_GRPC_ENDPOINT`, `https://` only) once;
    /// `ca_cert_path` pins the CA that signed hub-api's certificate (`HUB_API_GRPC_CA_FILE`).
    ///
    /// # Errors
    /// [`HubContextConnectError`] -- fatal at boot, never a silent fallback.
    pub async fn connect(
        endpoint: &str,
        token_endpoint: &str,
        sa_token_path: &str,
        ca_cert_path: Option<&str>,
    ) -> Result<Self, HubContextConnectError> {
        if !endpoint.trim().to_ascii_lowercase().starts_with("https://") {
            return Err(HubContextConnectError::NotTls);
        }
        let client = ActiveContextClient::connect(
            endpoint.trim(),
            token_endpoint,
            sa_token_path,
            ca_cert_path,
        )
        .await?;
        Ok(Self { client })
    }

    /// Wraps an already-built client (loopback servers in tests).
    #[must_use]
    pub fn from_client(client: ActiveContextClient) -> Self {
        Self { client }
    }
}

/// Maps a hub failure to the host's two-valued error (the caller answers the user generically).
fn classify(err: &hub_client::HubClientError) -> ContextError {
    tracing::warn!(error = %err, "active context call failed");
    ContextError::Unavailable
}

impl ContextSource for HubContextSource {
    fn get<'a>(
        &'a self,
        source_id: &'a str,
        user: &'a str,
    ) -> BoxFut<'a, Result<ContextView, ContextError>> {
        Box::pin(async move {
            let answer = self
                .client
                .get("discord", source_id, user)
                .await
                .map_err(|e| classify(&e))?;
            ContextView::try_from(answer)
        })
    }

    fn put<'a>(
        &'a self,
        source_id: &'a str,
        user: &'a str,
        community: &'a str,
    ) -> BoxFut<'a, Result<ContextView, ContextError>> {
        Box::pin(async move {
            let answer = self
                .client
                .set("discord", source_id, user, community)
                .await
                .map_err(|e| classify(&e))?;
            ContextView::try_from(answer)
        })
    }
}

/// What `/context <args>` should do.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ContextAction {
    /// Reply with this text only.
    Reply(String),
    /// PUT this community, then confirm with `name`.
    Switch {
        /// Community `public_id` to switch to.
        public_id: String,
        /// Its display name (for the confirmation).
        name: String,
    },
}

fn listing(view: &ContextView) -> String {
    let mut s = format!("Your active community: {}.", view.community_name);
    if view.communities.len() > 1 {
        s.push_str(" Switch with /context <name>:");
        let mut cs: Vec<&Community> = view.communities.iter().collect();
        cs.sort_by_key(|c| c.position);
        for c in cs {
            s.push_str("\n- ");
            s.push_str(&c.display_name);
        }
    }
    s
}

/// Decides the `/context` action. Pure: the name is matched here (case-insensitive, exact,
/// unique); only a listed UUID is ever sent to hub-api.
#[must_use]
pub fn plan_context(args: &str, view: &ContextView) -> ContextAction {
    let want = args.trim();
    if want.is_empty() {
        return ContextAction::Reply(truncate(listing(view)));
    }
    let by_id: Vec<&Community> = view
        .communities
        .iter()
        .filter(|c| c.public_id.eq_ignore_ascii_case(want))
        .collect();
    let by_name: Vec<&Community> = view
        .communities
        .iter()
        .filter(|c| c.display_name.to_lowercase() == want.to_lowercase())
        .collect();
    let hit = if by_id.len() == 1 { by_id } else { by_name };
    match hit.as_slice() {
        [one] => ContextAction::Switch {
            public_id: one.public_id.clone(),
            name: one.display_name.clone(),
        },
        [] => ContextAction::Reply(truncate(format!(
            "No community matches that name.\n{}",
            listing(view)
        ))),
        _ => ContextAction::Reply(truncate(format!(
            "More than one community matches that name; use the exact name.\n{}",
            listing(view)
        ))),
    }
}

fn truncate(s: String) -> String {
    if s.chars().count() <= MAX_REPLY_CHARS {
        s
    } else {
        s.chars().take(MAX_REPLY_CHARS).collect()
    }
}

/// Posts an ephemeral followup built by the host from the stored token.
pub struct InteractionReplier {
    store: InteractionStore,
    client: GuardedClient,
    api_base: String,
}

impl InteractionReplier {
    /// Builds a replier over a store and a guarded client rooted at `api_base`.
    #[must_use]
    pub fn new(store: InteractionStore, client: GuardedClient, api_base: String) -> Self {
        Self {
            store,
            client,
            api_base,
        }
    }

    /// Sends `content` (plain text, `allowed_mentions` none) ephemerally to interaction `id`.
    ///
    /// # Errors
    /// A fixed reason; never the token or URL.
    pub async fn ephemeral(&self, id: &str, content: &str) -> Result<(), &'static str> {
        let rec = self
            .store
            .get(id)
            .await
            .map_err(|_| "store_unavailable")?
            .ok_or("interaction_expired")?;
        let body = serde_json::to_vec(&serde_json::json!({
            "content": content,
            "flags": FLAG_EPHEMERAL,
            "allowed_mentions": { "parse": [] },
        }))
        .map_err(|_| "encode")?;
        let url = format!(
            "{}/webhooks/{}/{}",
            self.api_base, rec.application_id, rec.token
        );
        let resp = self
            .client
            .fetch(
                reqwest::Method::POST,
                &url,
                vec![("Content-Type".to_string(), "application/json".to_string())],
                Some(body),
            )
            .await
            .map_err(|_| "transport")?;
        if (200..300).contains(&resp.status) {
            Ok(())
        } else {
            Err("refused")
        }
    }
}

/// Prometheus instruments for the routing step. Labels are bounded `&'static str` outcomes
/// only; never an id, guild or user (PII-free by construction).
#[derive(Clone)]
pub struct RoutingMetrics {
    /// `svc_ingest_interaction_routing_total{outcome}`: `routed` or the refusal reason.
    pub routing_total: IntCounterVec,
    /// `svc_ingest_interaction_routing_duration_seconds{outcome}`: verify + record latency.
    pub routing_duration_seconds: HistogramVec,
}

/// Registers the routing metrics on `registry` (once per registry).
///
/// # Errors
/// [`prometheus::Error`] when registration fails (e.g. a duplicate).
pub fn register_routing_metrics(registry: &Registry) -> Result<RoutingMetrics, prometheus::Error> {
    let routing_total = IntCounterVec::new(
        Opts::new(
            "svc_ingest_interaction_routing_total",
            "Discord interactions routed to a community (or refused), by outcome",
        ),
        &["outcome"],
    )?;
    let routing_duration_seconds = HistogramVec::new(
        HistogramOpts::new(
            "svc_ingest_interaction_routing_duration_seconds",
            "Interaction routing latency (ownership verify + record) in seconds",
        )
        .buckets(vec![
            0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
        ]),
        &["outcome"],
    )?;
    registry.register(Box::new(routing_total.clone()))?;
    registry.register(Box::new(routing_duration_seconds.clone()))?;
    Ok(RoutingMetrics {
        routing_total,
        routing_duration_seconds,
    })
}

const GENERIC_UNAVAILABLE: &str =
    "Sorry, that command is temporarily unavailable. Please try again shortly.";

/// Resolves active context for interaction events and handles `/context`.
pub struct ContextSink<S: EventSink> {
    inner: S,
    source: Arc<dyn ContextSource>,
    replier: Arc<InteractionReplier>,
    store: InteractionStore,
    application_id: String,
    metrics: RoutingMetrics,
}

impl<S: EventSink> ContextSink<S> {
    /// Wraps `inner`. `application_id` is the Discord application (client) id of THIS
    /// connection's bot, from the host's own credential binding; `store` is the host-held
    /// interaction store the sender-side ownership check reads.
    #[must_use]
    pub fn new(
        inner: S,
        source: Arc<dyn ContextSource>,
        replier: Arc<InteractionReplier>,
        store: InteractionStore,
        application_id: String,
        metrics: RoutingMetrics,
    ) -> Self {
        Self {
            inner,
            source,
            replier,
            store,
            application_id,
            metrics,
        }
    }

    /// Verifies the stored interaction belongs to this connection's guild and application,
    /// then records `community_id` as its routed community. Returns the refusal reason.
    async fn route(
        &self,
        ctx: &ConnectionCtx,
        id: &str,
        community_id: i64,
    ) -> Result<(), &'static str> {
        if community_id <= 0 {
            return Err("community_id_invalid");
        }
        let rec = self
            .store
            .get(id)
            .await
            .map_err(|_| "store_unavailable")?
            .ok_or("interaction_expired")?;
        if rec.guild_id.as_deref() != Some(ctx.source_id.as_str()) {
            return Err("guild_mismatch");
        }
        if rec.application_id != self.application_id {
            return Err("application_mismatch");
        }
        if self
            .store
            .set_routed(id, community_id)
            .await
            .map_err(|_| "store_unavailable")?
        {
            Ok(())
        } else {
            Err("interaction_expired")
        }
    }

    async fn refuse(&self, id: &str, reason: &'static str) -> Result<(), SinkError> {
        tracing::warn!(
            platform = "discord",
            reason,
            "interaction not routed; user notified"
        );
        if self
            .replier
            .ephemeral(id, GENERIC_UNAVAILABLE)
            .await
            .is_err()
        {
            tracing::warn!(
                platform = "discord",
                "interaction refusal notice could not be sent"
            );
        }
        Ok(())
    }
}

impl<S: EventSink> EventSink for ContextSink<S> {
    async fn publish(
        &self,
        ctx: &ConnectionCtx,
        mut event: ConnectorEvent,
    ) -> Result<(), SinkError> {
        if event.platform != "discord" || !event.event_type.starts_with("interaction.") {
            return self.inner.publish(ctx, event).await;
        }
        let Ok(mut payload) = serde_json::from_str::<Value>(&event.payload_json) else {
            return self.inner.publish(ctx, event).await;
        };
        let Some(id) = payload
            .get("interaction_id")
            .and_then(Value::as_str)
            .map(str::to_string)
        else {
            return self.inner.publish(ctx, event).await;
        };
        let Some(user) = event.actor.clone() else {
            return self.refuse(&id, "no_actor").await;
        };
        let view = match self.source.get(&ctx.source_id, &user).await {
            Ok(v) => v,
            Err(_) => return self.refuse(&id, "context_unavailable").await,
        };
        let is_context = event.event_type == "interaction.command"
            && payload.get("command").and_then(Value::as_str) == Some("context");
        if is_context {
            let args = payload
                .get("args")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let text = match plan_context(args, &view) {
                ContextAction::Reply(t) => t,
                ContextAction::Switch { public_id, name } => {
                    match self.source.put(&ctx.source_id, &user, &public_id).await {
                        Ok(_) => truncate(format!("Switched to {name}.")),
                        Err(_) => return self.refuse(&id, "context_switch_failed").await,
                    }
                }
            };
            if self.replier.ephemeral(&id, &text).await.is_err() {
                tracing::warn!(platform = "discord", "/context reply could not be sent");
            }
            return Ok(());
        }
        let started = std::time::Instant::now();
        let span = tracing::info_span!(
            "interaction.route",
            platform = "discord",
            community_id = view.community_id
        );
        let routed = self
            .route(ctx, &id, view.community_id)
            .instrument(span.clone())
            .await;
        let outcome = routed.err().unwrap_or("routed");
        self.metrics
            .routing_total
            .with_label_values(&[outcome])
            .inc();
        self.metrics
            .routing_duration_seconds
            .with_label_values(&[outcome])
            .observe(started.elapsed().as_secs_f64());
        if let Err(reason) = routed {
            return self.refuse(&id, reason).await;
        }
        tracing::info!(
            parent: &span,
            platform = "discord",
            community_id = view.community_id,
            "interaction routed to community"
        );
        if let Some(obj) = payload.as_object_mut() {
            obj.insert(
                "active_community".to_string(),
                Value::String(view.community_public_id),
            );
        }
        event.payload_json = payload.to_string();
        self.inner.publish(ctx, event).await
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use bundle_host_http::guarded::GuardedLimits;
    use serde_json::json;
    use std::net::{IpAddr, Ipv4Addr};
    use std::sync::Mutex;
    use std::time::Duration;
    use wiremock::matchers::{body_partial_json, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    const U1: &str = "11111111-1111-4111-8111-111111111111";
    const U2: &str = "22222222-2222-4222-8222-222222222222";
    const USER: &str = "123e4567-e89b-12d3-a456-426614174000";

    fn view() -> ContextView {
        ContextView {
            community_public_id: U1.into(),
            community_id: 1,
            community_name: "Alpha".into(),
            explicit: false,
            communities: vec![
                Community {
                    public_id: U1.into(),
                    display_name: "Alpha".into(),
                    position: 0,
                },
                Community {
                    public_id: U2.into(),
                    display_name: "Beta Team".into(),
                    position: 1,
                },
            ],
        }
    }

    #[test]
    fn bare_context_echoes_and_lists_in_position_order() {
        let ContextAction::Reply(t) = plan_context("  ", &view()) else {
            panic!()
        };
        assert!(t.starts_with("Your active community: Alpha."));
        assert!(t.find("- Alpha").unwrap() < t.find("- Beta Team").unwrap());
        let mut single = view();
        single.communities.truncate(1);
        let ContextAction::Reply(t) = plan_context("", &single) else {
            panic!()
        };
        assert_eq!(t, "Your active community: Alpha.");
    }

    #[test]
    fn names_match_exactly_case_insensitively_and_only_uuids_are_sent() {
        for q in ["beta team", "BETA TEAM", U2, &U2.to_uppercase()] {
            assert_eq!(
                plan_context(q, &view()),
                ContextAction::Switch {
                    public_id: U2.into(),
                    name: "Beta Team".into()
                },
                "{q}"
            );
        }
        for q in ["beta", "gamma", "alph"] {
            let ContextAction::Reply(t) = plan_context(q, &view()) else {
                panic!("{q}")
            };
            assert!(t.starts_with("No community matches"), "{q}");
        }
        let mut dup = view();
        dup.communities[1].display_name = "alpha".into();
        let ContextAction::Reply(t) = plan_context("ALPHA", &dup) else {
            panic!()
        };
        assert!(t.starts_with("More than one community"));
    }

    struct Source {
        view: Result<ContextView, ContextError>,
        puts: Mutex<Vec<String>>,
        gets: Mutex<Vec<(String, String)>>,
    }
    impl ContextSource for Source {
        fn get<'a>(
            &'a self,
            s: &'a str,
            u: &'a str,
        ) -> BoxFut<'a, Result<ContextView, ContextError>> {
            self.gets.lock().unwrap().push((s.into(), u.into()));
            Box::pin(async move { self.view.clone() })
        }
        fn put<'a>(
            &'a self,
            _s: &'a str,
            _u: &'a str,
            c: &'a str,
        ) -> BoxFut<'a, Result<ContextView, ContextError>> {
            self.puts.lock().unwrap().push(c.into());
            Box::pin(async move { self.view.clone() })
        }
    }

    #[derive(Clone, Default)]
    struct Rec(Arc<Mutex<Vec<ConnectorEvent>>>);
    impl EventSink for Rec {
        async fn publish(&self, _c: &ConnectionCtx, e: ConnectorEvent) -> Result<(), SinkError> {
            self.0.lock().unwrap().push(e);
            Ok(())
        }
    }

    fn ctx() -> ConnectionCtx {
        ConnectionCtx {
            platform: "discord".into(),
            source_id: "111".into(),
            connection_id: "c".into(),
            session_token: None,
        }
    }

    fn event(
        kind: &str,
        command: &str,
        args: &str,
        id: &str,
        actor: Option<&str>,
    ) -> ConnectorEvent {
        ConnectorEvent {
            platform: "discord".into(),
            event_type: kind.into(),
            actor: actor.map(str::to_string),
            payload_json: json!({"interaction_id": id, "command": command, "args": args})
                .to_string(),
            occurred_at: "t".into(),
        }
    }

    async fn rig(
        view: Result<ContextView, ContextError>,
    ) -> (MockServer, ContextSink<Rec>, Rec, Arc<Source>, String) {
        let (a, b, c, d, e, _) = rig_with(view, "42").await;
        (a, b, c, d, e)
    }

    async fn rig_with(
        view: Result<ContextView, ContextError>,
        sink_app: &str,
    ) -> (
        MockServer,
        ContextSink<Rec>,
        Rec,
        Arc<Source>,
        String,
        InteractionStore,
    ) {
        let server = MockServer::start().await;
        let url = std::env::var("VALKEY_URL").expect("VALKEY_URL (a local valkey) is required");
        let store = InteractionStore::new(
            redis::Client::open(url)
                .unwrap()
                .get_multiplexed_async_connection()
                .await
                .unwrap(),
        );
        let n = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let id = format!("3{n}");
        store
            .put_new(
                &id,
                &crate::interaction_store::InteractionRecord {
                    application_id: "42".into(),
                    token: "ctxTOKEN-secret".into(),
                    guild_id: Some("111".into()),
                    routed_community_id: None,
                },
            )
            .await
            .unwrap();
        let client = GuardedClient::new(
            GuardedLimits {
                max_redirects: 0,
                timeout: Duration::from_secs(5),
                max_response_bytes: 16 * 1024,
            },
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        );
        let replier = Arc::new(InteractionReplier::new(
            store.clone(),
            client,
            format!("{}/api/v10", server.uri()),
        ));
        let source = Arc::new(Source {
            view,
            puts: Mutex::default(),
            gets: Mutex::default(),
        });
        let rec = Rec::default();
        let metrics = register_routing_metrics(&Registry::new()).unwrap();
        let sink = ContextSink::new(
            rec.clone(),
            source.clone(),
            replier,
            store.clone(),
            sink_app.to_string(),
            metrics,
        );
        (server, sink, rec, source, id, store)
    }

    async fn mock_followup(s: &MockServer, partial: Value) {
        Mock::given(method("POST"))
            .and(path("/api/v10/webhooks/42/ctxTOKEN-secret"))
            .and(body_partial_json(partial))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id":"1"})))
            .expect(1)
            .mount(s)
            .await;
    }

    #[tokio::test]
    async fn command_events_are_stamped_with_the_active_community_and_published() {
        let (server, sink, rec, source, id) = rig(Ok(view())).await;
        sink.publish(
            &ctx(),
            event("interaction.command", "ping", "", &id, Some(USER)),
        )
        .await
        .unwrap();
        let sent = rec.0.lock().unwrap().clone();
        assert_eq!(sent.len(), 1);
        let p: Value = serde_json::from_str(&sent[0].payload_json).unwrap();
        assert_eq!(p["active_community"], U1);
        assert_eq!(
            source.gets.lock().unwrap()[0],
            ("111".to_string(), USER.to_string())
        );
        assert!(server.received_requests().await.unwrap().is_empty());
        // Non-interaction events pass through untouched with no lookup.
        let mut m = event("message", "", "", &id, Some(USER));
        m.payload_json = "{}".into();
        sink.publish(&ctx(), m).await.unwrap();
        assert_eq!(source.gets.lock().unwrap().len(), 1);
        assert_eq!(rec.0.lock().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn context_is_handled_by_the_host_and_never_published() {
        let (server, sink, rec, source, id) = rig(Ok(view())).await;
        mock_followup(
            &server,
            json!({"flags":64,"content":"Switched to Beta Team."}),
        )
        .await;
        sink.publish(
            &ctx(),
            event(
                "interaction.command",
                "context",
                "beta team",
                &id,
                Some(USER),
            ),
        )
        .await
        .unwrap();
        assert!(
            rec.0.lock().unwrap().is_empty(),
            "/context never reaches a bundle"
        );
        assert_eq!(source.puts.lock().unwrap().clone(), vec![U2.to_string()]);
        let reqs = server.received_requests().await.unwrap();
        assert!(reqs[0].headers.get("authorization").is_none());
    }

    #[tokio::test]
    async fn bare_context_replies_with_the_listing_and_changes_nothing() {
        let (server, sink, rec, source, id) = rig(Ok(view())).await;
        mock_followup(&server, json!({"flags":64})).await;
        sink.publish(
            &ctx(),
            event("interaction.command", "context", "", &id, Some(USER)),
        )
        .await
        .unwrap();
        assert!(rec.0.lock().unwrap().is_empty());
        assert!(source.puts.lock().unwrap().is_empty());
        let body: Value =
            serde_json::from_slice(&server.received_requests().await.unwrap()[0].body).unwrap();
        assert!(body["content"].as_str().unwrap().contains("Beta Team"));
    }

    #[tokio::test]
    async fn lookup_failure_or_missing_actor_fails_closed_with_a_generic_notice() {
        for (view, actor) in [
            (Err(ContextError::Unavailable), Some(USER)),
            (Err(ContextError::Malformed), Some(USER)),
            (Ok(view()), None),
        ] {
            let (server, sink, rec, _s, id) = rig(view).await;
            mock_followup(&server, json!({"flags":64,"content":GENERIC_UNAVAILABLE})).await;
            sink.publish(&ctx(), event("interaction.command", "ping", "", &id, actor))
                .await
                .unwrap();
            assert!(
                rec.0.lock().unwrap().is_empty(),
                "nothing is routed on failure"
            );
        }
    }
    #[tokio::test]
    async fn a_routed_interaction_records_the_hub_community_and_is_published() {
        let (_server, sink, rec, _s, id, store) = rig_with(Ok(view()), "42").await;
        assert_eq!(
            store.get(&id).await.unwrap().unwrap().routed_community_id,
            None
        );
        sink.publish(
            &ctx(),
            event("interaction.command", "ping", "", &id, Some(USER)),
        )
        .await
        .unwrap();
        assert_eq!(rec.0.lock().unwrap().len(), 1);
        assert_eq!(
            store.get(&id).await.unwrap().unwrap().routed_community_id,
            Some(1)
        );
        assert_eq!(
            sink.metrics
                .routing_total
                .with_label_values(&["routed"])
                .get(),
            1
        );
    }

    #[tokio::test]
    async fn wrong_application_or_guild_or_bad_community_is_refused_and_left_unrouted() {
        // Wrong application on the sink.
        let (server, sink, rec, _s, id, store) = rig_with(Ok(view()), "999").await;
        mock_followup(&server, json!({"flags":64,"content":GENERIC_UNAVAILABLE})).await;
        sink.publish(
            &ctx(),
            event("interaction.command", "ping", "", &id, Some(USER)),
        )
        .await
        .unwrap();
        assert!(rec.0.lock().unwrap().is_empty());
        assert_eq!(
            store.get(&id).await.unwrap().unwrap().routed_community_id,
            None
        );
        assert_eq!(
            sink.metrics
                .routing_total
                .with_label_values(&["application_mismatch"])
                .get(),
            1
        );
        // Wrong guild: the connection is guild 222, the record is guild 111.
        let (server, sink, rec, _s, id, store) = rig_with(Ok(view()), "42").await;
        mock_followup(&server, json!({"flags":64,"content":GENERIC_UNAVAILABLE})).await;
        let mut other = ctx();
        other.source_id = "222".into();
        sink.publish(
            &other,
            event("interaction.command", "ping", "", &id, Some(USER)),
        )
        .await
        .unwrap();
        assert!(rec.0.lock().unwrap().is_empty());
        assert_eq!(
            store.get(&id).await.unwrap().unwrap().routed_community_id,
            None
        );
        assert_eq!(
            sink.metrics
                .routing_total
                .with_label_values(&["guild_mismatch"])
                .get(),
            1
        );
        // A non-positive numeric community is refused, never recorded.
        let mut bad = view();
        bad.community_id = 0;
        let (server, sink, rec, _s, id, store) = rig_with(Ok(bad), "42").await;
        mock_followup(&server, json!({"flags":64,"content":GENERIC_UNAVAILABLE})).await;
        sink.publish(
            &ctx(),
            event("interaction.command", "ping", "", &id, Some(USER)),
        )
        .await
        .unwrap();
        assert!(rec.0.lock().unwrap().is_empty());
        assert_eq!(
            store.get(&id).await.unwrap().unwrap().routed_community_id,
            None
        );
    }

    fn wire(community_id: i64, public_id: &str) -> pb::ActiveContext {
        pb::ActiveContext {
            community_public_id: public_id.into(),
            community_id,
            community_name: "Alpha".into(),
            explicit: true,
            communities: vec![pb::SwitchableCommunity {
                public_id: public_id.into(),
                display_name: "Alpha".into(),
                position: 0,
            }],
        }
    }

    #[test]
    fn a_hub_answer_without_a_numeric_community_is_malformed() {
        assert_eq!(
            ContextView::try_from(wire(0, U1)).unwrap_err(),
            ContextError::Malformed
        );
        assert_eq!(
            ContextView::try_from(wire(-3, U1)).unwrap_err(),
            ContextError::Malformed
        );
        assert_eq!(
            ContextView::try_from(wire(12, "")).unwrap_err(),
            ContextError::Malformed
        );
    }

    #[test]
    fn a_valid_hub_answer_converts_field_for_field() {
        let view = ContextView::try_from(wire(12, U1)).unwrap();
        assert_eq!(view.community_id, 12);
        assert_eq!(view.community_public_id, U1);
        assert!(view.explicit);
        assert_eq!(view.communities.len(), 1);
        assert_eq!(view.communities[0].display_name, "Alpha");
    }

    #[tokio::test]
    async fn plaintext_or_non_url_endpoint_is_refused_before_dialing() {
        for bad in ["http://hub:50204", "hub:50204", ""] {
            let err = HubContextSource::connect(bad, "t", "s", None).await;
            assert!(matches!(err, Err(HubContextConnectError::NotTls)), "{bad}");
        }
    }

    /// Real tonic `ActiveContextService` on loopback answering with `reply`; returns the
    /// production [`HubContextSource`] wired (real machine-JWT bootstrap against an in-process gRPC
    /// mint) to it.
    async fn grpc_source(
        reply: Result<Option<pb::ActiveContext>, tonic::Status>,
    ) -> (HubContextSource, String) {
        use hub_client::pb::active_context_service_server::{
            ActiveContextService, ActiveContextServiceServer,
        };
        struct Fake(Result<Option<pb::ActiveContext>, tonic::Status>);
        #[tonic::async_trait]
        impl ActiveContextService for Fake {
            async fn get_active_context(
                &self,
                request: tonic::Request<pb::GetActiveContextRequest>,
            ) -> Result<tonic::Response<pb::GetActiveContextResponse>, tonic::Status> {
                assert_eq!(
                    request.metadata().get("authorization").unwrap(),
                    "Bearer machine.jwt.value"
                );
                let r = request.into_inner();
                assert_eq!(
                    (r.platform.as_str(), r.source_id.as_str()),
                    ("discord", "g1")
                );
                self.0
                    .clone()
                    .map(|context| tonic::Response::new(pb::GetActiveContextResponse { context }))
            }
            async fn set_active_context(
                &self,
                request: tonic::Request<pb::SetActiveContextRequest>,
            ) -> Result<tonic::Response<pb::SetActiveContextResponse>, tonic::Status> {
                assert_eq!(request.into_inner().community, U1);
                self.0
                    .clone()
                    .map(|context| tonic::Response::new(pb::SetActiveContextResponse { context }))
            }
        }
        // The machine-JWT mint is gRPC: a real in-process `ServiceAuthService`.
        let tokens = service_auth::test_support::MockHub::builder()
            .mint(vec![service_auth::test_support::MintReply::token(
                "machine.jwt.value",
                3600,
            )])
            .start_detached()
            .await;
        let sa = std::env::temp_dir().join(format!("svc-ingest-ac-sa-{}", uuid::Uuid::new_v4()));
        std::fs::write(&sa, "projected-sa-token").unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(ActiveContextServiceServer::new(Fake(reply)))
                .serve_with_incoming(tonic::transport::server::TcpIncoming::from(listener))
                .await
                .unwrap();
        });
        let channel = tonic::transport::Endpoint::from_shared(format!("http://{addr}"))
            .unwrap()
            .connect()
            .await
            .unwrap();
        let client = ActiveContextClient::with_channel(
            channel,
            Arc::new(service_auth::MachineJwtClient::new(
                tokens.clone(),
                sa.to_string_lossy().to_string(),
                ACTIVE_CONTEXT_JWT_SCOPE,
            )),
        );
        (HubContextSource::from_client(client), tokens)
    }

    #[tokio::test]
    async fn grpc_source_get_and_put_return_the_numeric_view() {
        let (source, _tokens) = grpc_source(Ok(Some(wire(12, U1)))).await;
        let got = source.get("g1", USER).await.unwrap();
        assert_eq!(
            (got.community_id, got.community_public_id.as_str()),
            (12, U1)
        );
        let put = source.put("g1", USER, U1).await.unwrap();
        assert_eq!(put.community_id, 12);
    }

    #[tokio::test]
    async fn grpc_source_fails_closed_on_refusal_or_a_malformed_answer() {
        let (refused, _t1) = grpc_source(Err(tonic::Status::not_found("no"))).await;
        assert_eq!(
            refused.get("g1", USER).await.unwrap_err(),
            ContextError::Unavailable
        );
        let (zero, _t2) = grpc_source(Ok(Some(wire(0, U1)))).await;
        assert_eq!(
            zero.get("g1", USER).await.unwrap_err(),
            ContextError::Malformed
        );
        let (empty, _t3) = grpc_source(Ok(None)).await;
        assert_eq!(
            empty.get("g1", USER).await.unwrap_err(),
            ContextError::Unavailable
        );
    }
}
