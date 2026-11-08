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
use std::time::Duration;

use bundle_host_http::guarded::GuardedClient;
use serde::Deserialize;
use serde_json::Value;

use crate::commands_reconcile::BoxFut;
use crate::connector::sink::{EventSink, SinkError};
use crate::connector::types::{ConnectionCtx, ConnectorEvent};
use crate::interaction_store::InteractionStore;

/// Machine-JWT scope svc-ingest holds for the active-context endpoints.
pub const ACTIVE_CONTEXT_JWT_SCOPE: &str = "dataplane:active-context";
const CALL_TIMEOUT: Duration = Duration::from_secs(5);
/// Discord message flag: only the invoker sees the reply.
const FLAG_EPHEMERAL: u64 = 64;
/// Discord's message content cap.
const MAX_REPLY_CHARS: usize = 2000;

/// One switchable community.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize)]
pub struct Community {
    /// Community `public_id` UUID.
    #[serde(rename = "publicId")]
    pub public_id: String,
    /// Display name (a mutable alias; matched only client-side).
    #[serde(rename = "displayName")]
    pub display_name: String,
    /// 0 = first-registered default.
    pub position: u32,
}

/// A user's active context for a source.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize)]
pub struct ContextView {
    /// Active community `public_id`.
    #[serde(rename = "communityPublicId")]
    pub community_public_id: String,
    /// Active community display name.
    #[serde(rename = "communityName")]
    pub community_name: String,
    /// Whether the user chose it (vs the source default).
    pub explicit: bool,
    /// Switchable communities (absent on a PUT answer of an older hub = empty).
    #[serde(default)]
    pub communities: Vec<Community>,
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

/// Production [`ContextSource`]: hub-api over HTTP with a machine JWT.
pub struct HubContextSource {
    http: reqwest::Client,
    base: String,
    jwt: Arc<service_auth::MachineJwtClient>,
}

impl HubContextSource {
    /// `base` is the hub-api root.
    ///
    /// # Errors
    /// The `reqwest` builder error.
    pub fn new(
        base: &str,
        jwt: Arc<service_auth::MachineJwtClient>,
    ) -> Result<Self, reqwest::Error> {
        Ok(Self {
            http: reqwest::Client::builder().timeout(CALL_TIMEOUT).build()?,
            base: base.trim_end_matches('/').to_string(),
            jwt,
        })
    }

    fn url(&self) -> String {
        format!("{}/api/v1/internal/dataplane/active-context", self.base)
    }

    async fn finish(resp: reqwest::Response) -> Result<ContextView, ContextError> {
        if !resp.status().is_success() {
            return Err(ContextError::Unavailable);
        }
        let body = resp.bytes().await.map_err(|_| ContextError::Unavailable)?;
        serde_json::from_slice(&body).map_err(|_| ContextError::Malformed)
    }
}

impl ContextSource for HubContextSource {
    fn get<'a>(
        &'a self,
        source_id: &'a str,
        user: &'a str,
    ) -> BoxFut<'a, Result<ContextView, ContextError>> {
        Box::pin(async move {
            let token = self
                .jwt
                .get_token()
                .await
                .map_err(|_| ContextError::Unavailable)?;
            let resp = self
                .http
                .get(self.url())
                .query(&[
                    ("platform", "discord"),
                    ("source_id", source_id),
                    ("user", user),
                ])
                .bearer_auth(&*token)
                .send()
                .await
                .map_err(|_| ContextError::Unavailable)?;
            Self::finish(resp).await
        })
    }

    fn put<'a>(
        &'a self,
        source_id: &'a str,
        user: &'a str,
        community: &'a str,
    ) -> BoxFut<'a, Result<ContextView, ContextError>> {
        Box::pin(async move {
            let token = self
                .jwt
                .get_token()
                .await
                .map_err(|_| ContextError::Unavailable)?;
            let resp = self
                .http
                .put(self.url())
                .bearer_auth(&*token)
                .json(&serde_json::json!({
                    "platform": "discord", "sourceId": source_id,
                    "user": user, "community": community,
                }))
                .send()
                .await
                .map_err(|_| ContextError::Unavailable)?;
            Self::finish(resp).await
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

const GENERIC_UNAVAILABLE: &str =
    "Sorry, that command is temporarily unavailable. Please try again shortly.";

/// Resolves active context for interaction events and handles `/context`.
pub struct ContextSink<S: EventSink> {
    inner: S,
    source: Arc<dyn ContextSource>,
    replier: Arc<InteractionReplier>,
}

impl<S: EventSink> ContextSink<S> {
    /// Wraps `inner`.
    #[must_use]
    pub fn new(inner: S, source: Arc<dyn ContextSource>, replier: Arc<InteractionReplier>) -> Self {
        Self {
            inner,
            source,
            replier,
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
    use wiremock::matchers::{body_partial_json, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    const U1: &str = "11111111-1111-4111-8111-111111111111";
    const U2: &str = "22222222-2222-4222-8222-222222222222";
    const USER: &str = "123e4567-e89b-12d3-a456-426614174000";

    fn view() -> ContextView {
        ContextView {
            community_public_id: U1.into(),
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
            store,
            client,
            format!("{}/api/v10", server.uri()),
        ));
        let source = Arc::new(Source {
            view,
            puts: Mutex::default(),
            gets: Mutex::default(),
        });
        let rec = Rec::default();
        let sink = ContextSink::new(rec.clone(), source.clone(), replier);
        (server, sink, rec, source, id)
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
}
