//! The Twitch [`SlotSource`]: turns the per-community credential seam
//! (`crate::outbound_creds`, P6a T6b) into the four Helix slots the connector
//! templates carry (`token`, `client_id`, `broadcaster_id`, `sender_id`).
//!
//! * `token` -- the community's own OAuth access token, resolved by
//!   [`TwitchCredentialSource::credential`] on EVERY call (that call also
//!   proves the community owns the stamp-pinned channel and refuses a
//!   channel another community claims). Never cached, never logged.
//! * `client_id` -- the bot application's public client id (host config).
//! * `broadcaster_id` / `sender_id` -- numeric Twitch user ids of the pinned
//!   channel and of the resolved bot login. Helix addresses users by numeric
//!   id; the connection record carries logins, so ONE guarded
//!   `GET /helix/users?login=<channel>&login=<bot>` per send maps them. Not
//!   cached: a login can be renamed and re-registered to a different user, so
//!   a cached id could misdirect a moderation action. The ids come only from
//!   Twitch's answer for the logins the HOST derived -- never from the guest
//!   or the queue payload.

use std::collections::HashMap;

use bundle_host_http::guarded::{GuardedClient, GuardedLimits};
use connection_resolver::SecretString;
use serde::Deserialize;
use serde_json::{Map, Value};

use super::policy::APP_HANDLE_PREFIX;
use super::subst::SlotValue;
use super::{DutyCx, DutyHook, ExecError, SlotError, SlotRequest, SlotSource};
use crate::outbound_creds::{CredError, TwitchCredentialSource};
use crate::outbound_ops::{OutboundAction, PolicyOp};

/// Helix users endpoint.
const USERS_URL: &str = "https://api.twitch.tv/helix/users";

#[derive(Deserialize)]
struct Users {
    data: Vec<User>,
}

#[derive(Deserialize)]
struct User {
    id: String,
    login: String,
}

/// Source of the APP-scoped Twitch credentials (client-credentials token + the broadcaster a
/// community's connection names) behind the `app.*` slot handles. EventSub webhook
/// subscriptions are created with an APP token, never the community's user token.
#[allow(async_fn_in_trait)] // service-binary-internal, see outbound.rs::IrcOutbound
pub trait TwitchAppSource: Send + Sync {
    /// `false` only for [`NoApp`]: an app-scoped kind is then refused (fail-closed) rather
    /// than falling back to a user token.
    const AVAILABLE: bool;

    /// A current app access token (client-credentials grant). Resolved per call, never logged.
    async fn app_token(&self) -> Result<SecretString, SlotError>;

    /// The numeric Twitch user id of the broadcaster the community's connection names. The
    /// HOST decides it from the connection record -- never the guest or the queue payload.
    async fn broadcaster_id(&self, community_id: i64) -> Result<String, SlotError>;
}

/// The default [`TwitchAppSource`]: app credentials are unavailable, so every app-scoped kind
/// is refused loudly.
#[derive(Clone, Copy, Debug, Default)]
pub struct NoApp;

impl TwitchAppSource for NoApp {
    const AVAILABLE: bool = false;

    async fn app_token(&self) -> Result<SecretString, SlotError> {
        Err(SlotError::Refused("slot_app_credential_unsupported"))
    }

    async fn broadcaster_id(&self, _community_id: i64) -> Result<String, SlotError> {
        Err(SlotError::Refused("slot_app_credential_unsupported"))
    }
}

/// Per-community Twitch slot resolution over a [`TwitchCredentialSource`] (and, for app-scoped
/// kinds, a [`TwitchAppSource`]).
pub struct TwitchSlots<C: TwitchCredentialSource, A: TwitchAppSource = NoApp> {
    creds: C,
    client_id: String,
    client: GuardedClient,
    users_url: String,
    app: A,
}

impl<C: TwitchCredentialSource> TwitchSlots<C, NoApp> {
    /// Production wiring: the real Helix users endpoint through a default
    /// (empty-allowlist) guarded client.
    #[must_use]
    pub fn new(creds: C, client_id: String) -> Self {
        Self::with_endpoint(
            creds,
            client_id,
            GuardedClient::new(GuardedLimits::default(), Vec::new()),
            USERS_URL.to_string(),
        )
    }

    /// Explicit endpoint + client (tests point it at a local fake).
    #[must_use]
    pub fn with_endpoint(
        creds: C,
        client_id: String,
        client: GuardedClient,
        users_url: String,
    ) -> Self {
        Self {
            creds,
            client_id,
            client,
            users_url,
            app: NoApp,
        }
    }

    /// Enables the `app.*` handles (EventSub) over `app`.
    #[must_use]
    pub fn with_app_source<A: TwitchAppSource>(self, app: A) -> TwitchSlots<C, A> {
        TwitchSlots {
            creds: self.creds,
            client_id: self.client_id,
            client: self.client,
            users_url: self.users_url,
            app,
        }
    }
}

impl<C: TwitchCredentialSource, A: TwitchAppSource> TwitchSlots<C, A> {
    /// Resolves the APP-scoped handles for the community: the app token, the app's client id
    /// and the connection's broadcaster. No channel pin is involved (EventSub is per
    /// connection, not per chat event); the community scope is mandatory.
    async fn resolve_app(
        &self,
        req: &SlotRequest<'_>,
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        let community = req
            .community_id
            .ok_or(SlotError::Refused("no_community_scope"))?;
        let mut out = HashMap::new();
        for h in req.handles {
            let v = match *h {
                "app.token" => SlotValue::new(self.app.app_token().await?.expose().to_owned()),
                "app.client_id" => SlotValue::new(self.client_id.clone()),
                "app.broadcaster_id" => SlotValue::new(self.app.broadcaster_id(community).await?),
                _ => return Err(SlotError::Refused("unknown_handle")),
            };
            out.insert((*h).to_string(), v);
        }
        Ok(out)
    }

    /// Maps the two logins to numeric ids with one guarded GET.
    async fn user_ids(
        &self,
        token: &str,
        channel_login: &str,
        bot_login: &str,
    ) -> Result<(String, String), SlotError> {
        let mut url = reqwest::Url::parse(&self.users_url)
            .map_err(|_| SlotError::Unavailable("twitch_users_lookup_failed"))?;
        url.query_pairs_mut()
            .append_pair("login", channel_login)
            .append_pair("login", bot_login);
        let resp = self
            .client
            .fetch(
                reqwest::Method::GET,
                url.as_str(),
                vec![
                    ("Authorization".to_string(), format!("Bearer {token}")),
                    ("Client-Id".to_string(), self.client_id.clone()),
                ],
                None,
            )
            .await
            .map_err(|_| SlotError::Unavailable("twitch_users_lookup_failed"))?;
        match resp.status {
            200..=299 => {}
            401 | 403 => return Err(SlotError::Refused("connection_not_usable")),
            _ => return Err(SlotError::Unavailable("twitch_users_lookup_failed")),
        }
        let users: Users = serde_json::from_slice(&resp.body)
            .map_err(|_| SlotError::Unavailable("twitch_users_lookup_failed"))?;
        let find = |login: &str| {
            users
                .data
                .iter()
                .find(|u| u.login.eq_ignore_ascii_case(login))
                .map(|u| u.id.clone())
        };
        match (find(channel_login), find(bot_login)) {
            (Some(b), Some(s)) => Ok((b, s)),
            _ => Err(SlotError::Refused("twitch_user_not_found")),
        }
    }
}

/// `#Login` -> `login` (the stamp already canonicalized the channel).
fn login_of(channel: &str) -> String {
    channel.trim_start_matches('#').to_ascii_lowercase()
}

pub(crate) fn map_cred(e: CredError) -> SlotError {
    match e {
        CredError::Unavailable | CredError::TokenExpired => SlotError::Unavailable(e.reason()),
        other => SlotError::Refused(other.reason()),
    }
}

impl<C: TwitchCredentialSource, A: TwitchAppSource> SlotSource for TwitchSlots<C, A> {
    fn supports_app_credentials(&self, platform: &str) -> bool {
        platform == "twitch" && A::AVAILABLE
    }

    async fn resolve(
        &self,
        req: &SlotRequest<'_>,
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        if req.platform != "twitch" {
            return Err(SlotError::Refused("unsupported_platform"));
        }
        if req.handles.iter().any(|h| h.starts_with(APP_HANDLE_PREFIX)) {
            // The executor refuses a kind mixing app and user handles before getting here; a
            // direct caller doing so is refused too, never half-served.
            if !req.handles.iter().all(|h| h.starts_with(APP_HANDLE_PREFIX)) {
                return Err(SlotError::Refused("slot_credential_scope_mixed"));
            }
            return self.resolve_app(req).await;
        }
        let channel = req
            .pinned_channel
            .ok_or(SlotError::Refused("no_origin_pin"))?;
        // Resolved fresh on every send; also asserts channel ownership.
        let cred = self
            .creds
            .credential(req.community_id, channel)
            .await
            .map_err(map_cred)?;
        let mut out = HashMap::new();
        for h in req.handles {
            match *h {
                "token" | "client_id" | "broadcaster_id" | "sender_id" => {}
                _ => return Err(SlotError::Refused("unknown_handle")),
            }
        }
        if req.handles.contains(&"broadcaster_id") || req.handles.contains(&"sender_id") {
            let (broadcaster, sender) = self
                .user_ids(
                    cred.oauth_token.expose(),
                    &login_of(channel),
                    &cred.nick.to_ascii_lowercase(),
                )
                .await?;
            out.insert("broadcaster_id".to_string(), SlotValue::new(broadcaster));
            out.insert("sender_id".to_string(), SlotValue::new(sender));
        }
        out.insert(
            "token".to_string(),
            SlotValue::new(cred.oauth_token.expose().to_owned()),
        );
        out.insert(
            "client_id".to_string(),
            SlotValue::new(self.client_id.clone()),
        );
        Ok(out)
    }
}

/// Fetches the per-subscription EventSub webhook secret by its opaque `callbackKey` (hub-api
/// `GET /api/v1/internal/eventsub/secret/<callback_key>`, scope `eventsub:secret-read`, held by
/// the svc-ingest identity ONLY, never the bundle's). The value is injected into
/// `transport.secret` host-side and never enters guest memory.
///
/// Both lookups are bound to the invoking COMMUNITY (the producer-supplied `callbackKey` and
/// subscription id are never trusted on their own): an implementation backed by the hub-api
/// subscription ledger MUST refuse a key / subscription not registered under that
/// `(tenant, community)`, so one community can never obtain another's secret or callback or
/// delete another's subscription.
#[allow(async_fn_in_trait)] // service-binary-internal, see outbound.rs::IrcOutbound
pub trait EventSubSecretSource: Send + Sync {
    /// The webhook secret for `callback_key`, refused unless the key is registered to
    /// `community_id`.
    async fn secret(
        &self,
        community_id: i64,
        callback_key: &str,
    ) -> Result<SecretString, SlotError>;

    /// `Ok` only when Twitch subscription `twitch_subscription_id` is registered in the ledger
    /// under `community_id` (`Refused("subscription_not_owned")` otherwise).
    async fn owns_subscription(
        &self,
        community_id: i64,
        twitch_subscription_id: &str,
    ) -> Result<(), SlotError>;
}

/// Kinds [`TwitchDuties`] claims.
const DUTY_KINDS: [&str; 3] = ["eventsub.create", "schedule.update", "schedule.delete"];

/// `[A-Za-z0-9_-]{8,128}`: the opaque, URL-safe `callbackKey` of an EventSub work item.
fn is_callback_key(k: &str) -> bool {
    (8..=128).contains(&k.len())
        && k.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// Schedule segment ids are base64 text (`=` padding, standard or url-safe alphabet).
fn is_segment_id(k: &str) -> bool {
    (1..=256).contains(&k.len())
        && k.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'=' | b'+' | b'/'))
}

/// The queue entry's [`PolicyOp`] (every duty kind is a generic policy op).
fn policy_op<'a>(cx: &DutyCx<'a>) -> Result<&'a PolicyOp, ExecError> {
    match cx.action {
        OutboundAction::PolicyOp(p) => Ok(p),
        _ => Err(ExecError::BadAction),
    }
}

/// The producer's parameter object (`text` is JSON-object text); absent/invalid is refused.
fn params(p: &PolicyOp) -> Result<Map<String, Value>, ExecError> {
    let raw = p.text.as_deref().ok_or(ExecError::BadAction)?;
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(m)) => Ok(m),
        _ => Err(ExecError::BadAction),
    }
}

/// A required string parameter satisfying `ok`.
fn str_param<'a>(
    m: &'a Map<String, Value>,
    key: &str,
    ok: fn(&str) -> bool,
) -> Result<&'a str, ExecError> {
    m.get(key)
        .and_then(Value::as_str)
        .filter(|v| ok(v))
        .ok_or(ExecError::BadAction)
}

/// Twitch host duties for the generic outbound executor: the [`DutyHook`] behind
/// `eventsub.create` (callback + secret slots), `schedule.update` and `schedule.delete`
/// (segment-id slot).
///
/// | duty | enforcement |
/// |---|---|
/// | `transport.callback` | built by the HOST as `<configured base>/eventsub/twitch/webhook/<callbackKey>`; a URL written by the bundle (or a hub-api work item) is never used, so deliveries cannot be redirected |
/// | `transport.secret` | fetched by the host from [`EventSubSecretSource`] by `callbackKey` per call; never cached, never in the payload, template, logs or guest memory |
/// | `id` (segment) | taken from the producer's validated parameters (base64 charset, <= 256) and percent-encoded by the substituter |
/// | parameters | `text` is a JSON object; `eventsub.create` accepts EXACTLY `{type, version, callbackKey}` and the connector sees only `{type, version}`; `schedule.delete` accepts EXACTLY `{segment_id}` |
///
/// The broadcaster of an EventSub condition is the `app.broadcaster_id` handle (the host's
/// [`TwitchAppSource`]), so a subscription can only name the community's own channel.
///
/// **`eventsub.delete` is ownership-bound.** The app token can delete ANY subscription of the
/// application, so before the delete the host resolves the subscription (`GET
/// /eventsub/subscriptions?id=`) and refuses unless its condition names THIS community's own
/// broadcaster (`subscription_not_owned` / `subscription_not_found`; any lookup failure also
/// refuses). Without an app source ([`NoApp`], the default) delete is not claimed at all.
/// **`eventsub.list` is claimed by no hook**: the app token lists every community's
/// subscriptions, so it fails closed (`HostDutyMissing`) until a per-broadcaster filter exists.
pub struct TwitchDuties<S: EventSubSecretSource, A: TwitchAppSource = NoApp> {
    secrets: S,
    callback_base: String,
    app: A,
    client_id: String,
    subs_url: String,
}

/// Helix EventSub subscriptions endpoint (ownership lookups).
const SUBS_URL: &str = "https://api.twitch.tv/helix/eventsub/subscriptions";

/// `[A-Za-z0-9_-]{1,64}`: a Twitch EventSub subscription id.
fn is_sub_id(k: &str) -> bool {
    (1..=64).contains(&k.len())
        && k.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

impl<S: EventSubSecretSource> TwitchDuties<S, NoApp> {
    /// Duties over `secrets`, with webhook callbacks under `callback_base` (host config,
    /// `TWITCH_EVENTSUB_CALLBACK_BASE_URL`).
    ///
    /// # Errors
    /// A stable reason when `callback_base` is not a bare `https://host[:port][/path]` URL.
    pub fn new(secrets: S, callback_base: &str) -> Result<Self, &'static str> {
        let base = callback_base.trim_end_matches('/');
        let rest = base
            .strip_prefix("https://")
            .ok_or("callback_base_not_https")?;
        let plain = !rest.is_empty()
            && rest.bytes().all(|b| {
                b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b':' | b'/' | b'_')
            });
        if !plain || rest.starts_with('/') || rest.contains("//") {
            return Err("callback_base_invalid");
        }
        Ok(Self {
            secrets,
            callback_base: base.to_string(),
            app: NoApp,
            client_id: String::new(),
            subs_url: SUBS_URL.to_string(),
        })
    }

    /// Enables the ownership-bound `eventsub.delete` over `app` (the same app source the slot
    /// resolver uses) with the app's public `client_id`.
    #[must_use]
    pub fn with_app_source<A: TwitchAppSource>(
        self,
        app: A,
        client_id: String,
    ) -> TwitchDuties<S, A> {
        TwitchDuties {
            secrets: self.secrets,
            callback_base: self.callback_base,
            app,
            client_id,
            subs_url: self.subs_url,
        }
    }
}

impl<S: EventSubSecretSource, A: TwitchAppSource> TwitchDuties<S, A> {
    /// Test seam: point the ownership lookup at a local fake.
    #[must_use]
    pub fn with_subs_endpoint(mut self, url: String) -> Self {
        self.subs_url = url;
        self
    }

    /// The validated `callbackKey` of an `eventsub.create` entry.
    fn callback_key(cx: &DutyCx<'_>) -> Result<String, ExecError> {
        let m = params(policy_op(cx)?)?;
        str_param(&m, "callbackKey", is_callback_key).map(str::to_string)
    }

    /// The validated subscription id of an `eventsub.delete` entry.
    fn delete_id<'a>(cx: &DutyCx<'a>) -> Result<&'a str, ExecError> {
        policy_op(cx)?
            .message_id
            .as_deref()
            .filter(|i| is_sub_id(i))
            .ok_or(ExecError::BadAction)
    }

    /// Refuses (fail closed) unless subscription `id`'s condition names the invoking
    /// community's OWN broadcaster. Any lookup problem is a refusal, never a pass.
    async fn check_owned(&self, cx: &DutyCx<'_>, id: &str) -> Result<(), ExecError> {
        let community = cx
            .community_id
            .ok_or(ExecError::Slot(SlotError::Refused("no_community_scope")))?;
        // Ledger binding first: the subscription must be registered under THIS community
        // (broadcaster ids are not unique across communities, so they cannot be the authority).
        self.secrets
            .owns_subscription(community, id)
            .await
            .map_err(ExecError::Slot)?;
        let broadcaster = self
            .app
            .broadcaster_id(community)
            .await
            .map_err(ExecError::Slot)?;
        let token = self.app.app_token().await.map_err(ExecError::Slot)?;
        let unavailable = || ExecError::Slot(SlotError::Unavailable("subscription_lookup_failed"));
        let resp = cx
            .client
            .fetch(
                reqwest::Method::GET,
                &format!("{}?id={id}", self.subs_url),
                vec![
                    (
                        "Authorization".to_string(),
                        format!("Bearer {}", token.expose()),
                    ),
                    ("Client-Id".to_string(), self.client_id.clone()),
                ],
                None,
            )
            .await
            .map_err(|_| unavailable())?;
        match resp.status {
            200..=299 => {}
            404 => {
                return Err(ExecError::Slot(SlotError::Refused(
                    "subscription_not_found",
                )))
            }
            _ => return Err(unavailable()),
        }
        let v: Value = serde_json::from_slice(&resp.body).map_err(|_| unavailable())?;
        let sub = v
            .get("data")
            .and_then(Value::as_array)
            .and_then(|d| {
                d.iter()
                    .find(|s| s.get("id").and_then(Value::as_str) == Some(id))
            })
            .ok_or(ExecError::Slot(SlotError::Refused(
                "subscription_not_found",
            )))?;
        let cond = sub.get("condition");
        let names = |k: &str| cond.and_then(|c| c.get(k)).and_then(Value::as_str);
        // Exactly one of the two keys the seven supported types use must be present and equal.
        let owner = names("broadcaster_user_id").or_else(|| names("to_broadcaster_user_id"));
        if owner == Some(broadcaster.as_str()) {
            Ok(())
        } else {
            Err(ExecError::Slot(SlotError::Refused(
                "subscription_not_owned",
            )))
        }
    }
}

impl<S: EventSubSecretSource, A: TwitchAppSource> DutyHook for TwitchDuties<S, A> {
    fn claims(&self, platform: &str, kind: &str) -> bool {
        platform == "twitch"
            && (DUTY_KINDS.contains(&kind) || (kind == "eventsub.delete" && A::AVAILABLE))
    }

    async fn gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError> {
        // Every claimed family acts for one community's own connection.
        if cx.community_id.is_none() {
            return Err(ExecError::Slot(SlotError::Refused("no_community_scope")));
        }
        match policy_op(cx)?.op {
            "eventsub.delete" => {
                let id = Self::delete_id(cx)?;
                self.check_owned(cx, id).await?;
            }
            // The callback key is producer text: prove it is registered to this community
            // before anything is built (the secret lookup refuses a foreign key).
            "eventsub.create" => {
                let key = Self::callback_key(cx)?;
                let community = cx
                    .community_id
                    .ok_or(ExecError::Slot(SlotError::Refused("no_community_scope")))?;
                self.secrets
                    .secret(community, &key)
                    .await
                    .map_err(ExecError::Slot)?;
            }
            _ => {}
        }
        Ok(())
    }

    async fn payload(&self, cx: &DutyCx<'_>) -> Result<String, ExecError> {
        let p = policy_op(cx)?;
        if p.op == "eventsub.delete" {
            return Ok(serde_json::json!({ "message_id": Self::delete_id(cx)? }).to_string());
        }
        let mut m = params(p)?;
        let for_guest = match p.op {
            "eventsub.create" => {
                let mut keep = Map::new();
                for k in ["type", "version"] {
                    let v = m.remove(k).ok_or(ExecError::BadAction)?;
                    keep.insert(k.to_string(), v);
                }
                str_param(&m, "callbackKey", is_callback_key)?;
                m.remove("callbackKey");
                if !m.is_empty() {
                    return Err(ExecError::BadAction);
                }
                Some(keep)
            }
            "schedule.update" => {
                str_param(&m, "segment_id", is_segment_id)?;
                m.remove("segment_id");
                Some(m)
            }
            "schedule.delete" => {
                str_param(&m, "segment_id", is_segment_id)?;
                m.remove("segment_id");
                if !m.is_empty() {
                    return Err(ExecError::BadAction);
                }
                None
            }
            _ => return Err(ExecError::BadAction),
        };
        let payload = match for_guest {
            Some(fields) => serde_json::json!({ "text": Value::Object(fields).to_string() }),
            None => serde_json::json!({}),
        };
        Ok(payload.to_string())
    }

    async fn dynamic_slot(
        &self,
        cx: &DutyCx<'_>,
        slot: &str,
        _client: &GuardedClient,
    ) -> Result<SlotValue, ExecError> {
        match slot {
            "TWITCH_EVENTSUB_CALLBACK" => {
                let key = Self::callback_key(cx)?;
                // Binding re-proven at the point of use: a callback is never built for a key
                // the community does not own.
                let community = cx
                    .community_id
                    .ok_or(ExecError::Slot(SlotError::Refused("no_community_scope")))?;
                self.secrets
                    .secret(community, &key)
                    .await
                    .map_err(ExecError::Slot)?;
                Ok(SlotValue::new(format!(
                    "{}/eventsub/twitch/webhook/{key}",
                    self.callback_base
                )))
            }
            "TWITCH_EVENTSUB_SECRET" => {
                let key = Self::callback_key(cx)?;
                let community = cx
                    .community_id
                    .ok_or(ExecError::Slot(SlotError::Refused("no_community_scope")))?;
                let secret = self
                    .secrets
                    .secret(community, &key)
                    .await
                    .map_err(ExecError::Slot)?;
                Ok(SlotValue::new(secret.expose().to_owned()))
            }
            "TWITCH_SEGMENT_ID" => {
                let m = params(policy_op(cx)?)?;
                str_param(&m, "segment_id", is_segment_id).map(|id| SlotValue::new(id.to_string()))
            }
            _ => Err(ExecError::HostDutyMissing),
        }
    }
}

/// Two duty hooks as one: a kind goes to the first hook that claims it (the second otherwise).
/// Lets the production executor run the Discord and Twitch duties side by side.
pub struct DutyPair<A: DutyHook, B: DutyHook> {
    first: A,
    second: B,
}

impl<A: DutyHook, B: DutyHook> DutyPair<A, B> {
    /// A pair; `first` wins when both claim a kind.
    #[must_use]
    pub fn new(first: A, second: B) -> Self {
        Self { first, second }
    }

    fn first_claims(&self, cx: &DutyCx<'_>) -> bool {
        self.first.claims(cx.platform, cx.action.op())
    }
}

impl<A: DutyHook, B: DutyHook> DutyHook for DutyPair<A, B> {
    fn claims(&self, platform: &str, kind: &str) -> bool {
        self.first.claims(platform, kind) || self.second.claims(platform, kind)
    }

    async fn gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError> {
        if self.first_claims(cx) {
            self.first.gate(cx).await
        } else {
            self.second.gate(cx).await
        }
    }

    async fn bind_template(
        &self,
        cx: &DutyCx<'_>,
        tpl: &mut super::RequestTpl,
    ) -> Result<(), ExecError> {
        if self.first_claims(cx) {
            self.first.bind_template(cx, tpl).await
        } else {
            self.second.bind_template(cx, tpl).await
        }
    }

    async fn payload(&self, cx: &DutyCx<'_>) -> Result<String, ExecError> {
        if self.first_claims(cx) {
            self.first.payload(cx).await
        } else {
            self.second.payload(cx).await
        }
    }

    async fn role_bulk(&self, cx: &DutyCx<'_>) -> Result<super::RoleBulkPlan, ExecError> {
        if self.first_claims(cx) {
            self.first.role_bulk(cx).await
        } else {
            self.second.role_bulk(cx).await
        }
    }

    async fn dynamic_slot(
        &self,
        cx: &DutyCx<'_>,
        slot: &str,
        client: &GuardedClient,
    ) -> Result<SlotValue, ExecError> {
        if self.first_claims(cx) {
            self.first.dynamic_slot(cx, slot, client).await
        } else {
            self.second.dynamic_slot(cx, slot, client).await
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::net::{IpAddr, Ipv4Addr};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Duration;

    use connection_resolver::SecretString;
    use wiremock::matchers::{header, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::*;
    use crate::outbound_creds::TwitchCredential;

    struct FakeCreds {
        result: Result<(), CredError>,
        calls: Arc<AtomicUsize>,
    }

    impl TwitchCredentialSource for FakeCreds {
        async fn credential(
            &self,
            community_id: Option<i64>,
            channel: &str,
        ) -> Result<TwitchCredential, CredError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            assert_eq!(community_id, Some(7));
            assert_eq!(channel, "#Chan");
            self.result?;
            Ok(TwitchCredential {
                nick: "BotNick".to_string(),
                oauth_token: SecretString::new("tok-123".to_string()),
            })
        }
    }

    fn slots(
        server: &MockServer,
        result: Result<(), CredError>,
    ) -> (TwitchSlots<FakeCreds>, Arc<AtomicUsize>) {
        let calls = Arc::new(AtomicUsize::new(0));
        let client = GuardedClient::new(
            GuardedLimits {
                max_redirects: 0,
                timeout: Duration::from_secs(5),
                max_response_bytes: 4096,
            },
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        );
        (
            TwitchSlots::with_endpoint(
                FakeCreds {
                    result,
                    calls: calls.clone(),
                },
                "cid".to_string(),
                client,
                format!("{}/helix/users", server.uri()),
            ),
            calls,
        )
    }

    fn req<'a>(handles: &'a [&'static str], channel: Option<&'a str>) -> SlotRequest<'a> {
        SlotRequest {
            guild_scoped: false,
            platform: "twitch",
            community_id: Some(7),
            pinned_channel: channel,
            handles,
        }
    }

    const ALL: [&str; 4] = ["token", "client_id", "broadcaster_id", "sender_id"];

    #[tokio::test]
    async fn resolves_token_client_id_and_numeric_ids_with_one_guarded_lookup() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/helix/users"))
            .and(query_param("login", "chan"))
            .and(header("authorization", "Bearer tok-123"))
            .and(header("client-id", "cid"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "data": [{"id": "999", "login": "botnick"}, {"id": "111", "login": "Chan"}]
            })))
            .expect(2)
            .mount(&server)
            .await;
        let (s, calls) = slots(&server, Ok(()));
        let got = s.resolve(&req(&ALL, Some("#Chan"))).await.unwrap();
        assert_eq!(got["broadcaster_id"].expose(), "111");
        assert_eq!(got["sender_id"].expose(), "999");
        assert_eq!(got["token"].expose(), "tok-123");
        assert_eq!(got["client_id"].expose(), "cid");
        // Credential resolved once per resolve call; a second call resolves again.
        s.resolve(&req(&ALL, Some("#Chan"))).await.ok();
        assert_eq!(calls.load(Ordering::SeqCst), 2, "never cached");
    }

    #[tokio::test]
    async fn no_lookup_when_no_id_handle_is_requested() {
        let server = MockServer::start().await;
        let (s, _) = slots(&server, Ok(()));
        let got = s
            .resolve(&req(&["token", "client_id"], Some("#Chan")))
            .await
            .unwrap();
        assert_eq!(got.len(), 2);
        assert!(server.received_requests().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn credential_refusals_are_typed_and_nothing_is_looked_up() {
        let server = MockServer::start().await;
        for (e, want, retry) in [
            (CredError::NoBinding, "no_connection", false),
            (CredError::ChannelNotOwned, "channel_not_owned", false),
            (CredError::NotUsable, "connection_not_usable", false),
            (CredError::NoCommunityScope, "no_community_scope", false),
            (
                CredError::BindingListOverflow,
                "binding_list_overflow",
                false,
            ),
            (CredError::Unavailable, "credential_unavailable", true),
        ] {
            let (s, _) = slots(&server, Err(e));
            let err = s.resolve(&req(&ALL, Some("#Chan"))).await.unwrap_err();
            let expect = if retry {
                SlotError::Unavailable(want)
            } else {
                SlotError::Refused(want)
            };
            assert_eq!(err, expect);
        }
        assert!(server.received_requests().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn lookup_failures_fail_closed_with_typed_codes() {
        for (status, body, want) in [
            (
                401u16,
                serde_json::json!({}),
                SlotError::Refused("connection_not_usable"),
            ),
            (
                403,
                serde_json::json!({}),
                SlotError::Refused("connection_not_usable"),
            ),
            (
                500,
                serde_json::json!({}),
                SlotError::Unavailable("twitch_users_lookup_failed"),
            ),
            (
                200,
                serde_json::json!({"nope": 1}),
                SlotError::Unavailable("twitch_users_lookup_failed"),
            ),
            // Only one of the two logins resolved: never guess the other id.
            (
                200,
                serde_json::json!({"data": [{"id": "1", "login": "chan"}]}),
                SlotError::Refused("twitch_user_not_found"),
            ),
            (
                200,
                serde_json::json!({"data": []}),
                SlotError::Refused("twitch_user_not_found"),
            ),
            // Right ids for the WRONG logins (renamed / swapped): refused.
            (
                200,
                serde_json::json!({"data": [{"id": "1", "login": "someone"}, {"id": "2", "login": "else"}]}),
                SlotError::Refused("twitch_user_not_found"),
            ),
        ] {
            let server = MockServer::start().await;
            Mock::given(wiremock::matchers::any())
                .respond_with(ResponseTemplate::new(status).set_body_json(body))
                .mount(&server)
                .await;
            let (s, _) = slots(&server, Ok(()));
            assert_eq!(
                s.resolve(&req(&ALL, Some("#Chan"))).await.unwrap_err(),
                want,
                "{status}"
            );
        }
    }

    #[tokio::test]
    async fn the_lookup_goes_through_the_guard_not_around_it() {
        // Production client (empty allowlist) + a loopback endpoint: refused.
        let server = MockServer::start().await;
        let calls = Arc::new(AtomicUsize::new(0));
        let s = TwitchSlots::with_endpoint(
            FakeCreds {
                result: Ok(()),
                calls,
            },
            "cid".into(),
            GuardedClient::new(GuardedLimits::default(), Vec::new()),
            format!("{}/helix/users", server.uri()),
        );
        let e = s.resolve(&req(&ALL, Some("#Chan"))).await.unwrap_err();
        assert_eq!(e, SlotError::Unavailable("twitch_users_lookup_failed"));
        assert!(server.received_requests().await.unwrap().is_empty());
        // Production constructor targets the real Helix host.
        let p = TwitchSlots::new(
            FakeCreds {
                result: Ok(()),
                calls: Arc::new(AtomicUsize::new(0)),
            },
            "cid".into(),
        );
        assert_eq!(p.users_url, "https://api.twitch.tv/helix/users");
    }

    #[tokio::test]
    async fn wrong_platform_missing_pin_and_unknown_handles_are_refused() {
        let server = MockServer::start().await;
        let (s, calls) = slots(&server, Ok(()));
        let mut r = req(&ALL, Some("#Chan"));
        r.platform = "discord";
        assert_eq!(
            s.resolve(&r).await.unwrap_err(),
            SlotError::Refused("unsupported_platform")
        );
        assert_eq!(
            s.resolve(&req(&ALL, None)).await.unwrap_err(),
            SlotError::Refused("no_origin_pin")
        );
        assert_eq!(
            s.resolve(&req(&["token", "discord-bot"], Some("#Chan")))
                .await
                .unwrap_err(),
            SlotError::Refused("unknown_handle")
        );
        // Unknown handle is refused only after the (ownership-proving) credential call.
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn login_of_strips_hash_and_lowercases() {
        assert_eq!(login_of("#MiXed"), "mixed");
        assert_eq!(login_of("plain"), "plain");
    }

    struct App;
    impl TwitchAppSource for App {
        const AVAILABLE: bool = true;
        async fn app_token(&self) -> Result<SecretString, SlotError> {
            Ok(SecretString::new("app-tok".to_string()))
        }
        async fn broadcaster_id(&self, community_id: i64) -> Result<String, SlotError> {
            if community_id == 7 {
                Ok("111".to_string())
            } else {
                Err(SlotError::Refused("no_connection"))
            }
        }
    }

    const APP_ALL: [&str; 3] = ["app.token", "app.client_id", "app.broadcaster_id"];

    #[tokio::test]
    async fn app_handles_resolve_without_a_pin_and_never_touch_the_user_credential() {
        let server = MockServer::start().await;
        let (s, calls) = slots(&server, Ok(()));
        let s = s.with_app_source(App);
        assert!(s.supports_app_credentials("twitch"));
        assert!(!s.supports_app_credentials("discord"));
        let got = s.resolve(&req(&APP_ALL, None)).await.unwrap();
        assert_eq!(got["app.token"].expose(), "app-tok");
        assert_eq!(got["app.client_id"].expose(), "cid");
        assert_eq!(got["app.broadcaster_id"].expose(), "111");
        assert_eq!(
            calls.load(Ordering::SeqCst),
            0,
            "no user credential resolved"
        );
        assert!(server.received_requests().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn app_handles_are_refused_without_a_source_scope_or_with_a_user_mix() {
        let server = MockServer::start().await;
        // Default NoApp: unsupported, and never falls back to the user token.
        let (s, calls) = slots(&server, Ok(()));
        assert!(!s.supports_app_credentials("twitch"));
        assert_eq!(
            s.resolve(&req(&APP_ALL, Some("#Chan"))).await.unwrap_err(),
            SlotError::Refused("slot_app_credential_unsupported")
        );
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        let (s, _) = slots(&server, Ok(()));
        let s = s.with_app_source(App);
        let mut no_scope = req(&APP_ALL, None);
        no_scope.community_id = None;
        assert_eq!(
            s.resolve(&no_scope).await.unwrap_err(),
            SlotError::Refused("no_community_scope")
        );
        let mut other = req(&APP_ALL, None);
        other.community_id = Some(8);
        assert_eq!(
            s.resolve(&other).await.unwrap_err(),
            SlotError::Refused("no_connection")
        );
        assert_eq!(
            s.resolve(&req(&["app.token", "token"], Some("#Chan")))
                .await
                .unwrap_err(),
            SlotError::Refused("slot_credential_scope_mixed")
        );
        assert_eq!(
            s.resolve(&req(&["app.nope"], None)).await.unwrap_err(),
            SlotError::Refused("unknown_handle")
        );
    }

    struct Secrets;
    impl EventSubSecretSource for Secrets {
        async fn owns_subscription(&self, community_id: i64, id: &str) -> Result<(), SlotError> {
            // Ledger: community 7 owns `mine`/`raid`/`nocond`; community 9 owns `theirs`.
            let ok = matches!(
                (community_id, id),
                (7, "mine" | "raid" | "nocond" | "absent") | (9, "theirs" | "shared")
            );
            if ok {
                Ok(())
            } else {
                Err(SlotError::Refused("subscription_not_owned"))
            }
        }

        async fn secret(&self, community_id: i64, key: &str) -> Result<SecretString, SlotError> {
            if community_id == 7 && key == "key-abcdefgh" {
                Ok(SecretString::new("whsec".to_string()))
            } else {
                Err(SlotError::Refused("secret_unavailable"))
            }
        }
    }

    fn policy_action(op: &'static str, text: Option<&str>) -> OutboundAction {
        OutboundAction::PolicyOp(PolicyOp {
            op,
            channel: None,
            text: text.map(str::to_string),
            message_id: None,
            user_id: None,
            origin_channel: None,
            tenant_id: Some(3),
        })
    }

    fn duties() -> TwitchDuties<Secrets> {
        TwitchDuties::new(Secrets, "https://hooks.example.com/base/").unwrap()
    }

    fn client() -> GuardedClient {
        GuardedClient::new(GuardedLimits::default(), Vec::new())
    }

    async fn payload(op: &'static str, text: Option<&str>) -> Result<String, ExecError> {
        let action = policy_action(op, text);
        let c = client();
        let cx = DutyCx {
            platform: "twitch",
            action: &action,
            community_id: Some(7),
            pinned_channel: None,
            client: &c,
        };
        duties().payload(&cx).await
    }

    async fn slot(op: &'static str, text: &str, name: &str) -> Result<String, ExecError> {
        let action = policy_action(op, Some(text));
        let c = client();
        let cx = DutyCx {
            platform: "twitch",
            action: &action,
            community_id: Some(7),
            pinned_channel: None,
            client: &c,
        };
        duties()
            .dynamic_slot(&cx, name, &c)
            .await
            .map(|v| v.expose().to_string())
    }

    #[test]
    fn duties_claim_exactly_the_twitch_duty_kinds() {
        let d = duties();
        for k in ["eventsub.create", "schedule.update", "schedule.delete"] {
            assert!(d.claims("twitch", k), "{k}");
            assert!(!d.claims("discord", k), "{k}");
        }
        for k in ["eventsub.delete", "clip.create", "mod.ban", "dm.send"] {
            assert!(!d.claims("twitch", k), "{k}");
        }
        assert!(TwitchDuties::new(Secrets, "http://x.example").is_err());
        assert!(TwitchDuties::new(Secrets, "https://").is_err());
        assert!(TwitchDuties::new(Secrets, "https://a.example/?q=1").is_err());
        assert!(TwitchDuties::new(Secrets, "https://a.example//x").is_err());
    }

    #[tokio::test]
    async fn eventsub_payload_hides_the_callback_key_from_the_connector() {
        let t = r#"{"type":"channel.follow","version":2,"callbackKey":"key-abcdefgh"}"#;
        let p = payload("eventsub.create", Some(t)).await.unwrap();
        assert!(!p.contains("key-abcdefgh") && !p.contains("callbackKey"));
        let v: Value = serde_json::from_str(&p).unwrap();
        let inner: Value = serde_json::from_str(v["text"].as_str().unwrap()).unwrap();
        assert_eq!(
            inner,
            serde_json::json!({"type":"channel.follow","version":2})
        );
        for bad in [
            r#"{"type":"channel.follow","version":2}"#,
            r#"{"type":"channel.follow","version":2,"callbackKey":"a/b"}"#,
            r#"{"type":"channel.follow","callbackKey":"key-abcdefgh"}"#,
            r#"{"type":"x","version":1,"callbackKey":"key-abcdefgh","secret":"s"}"#,
            "nope",
        ] {
            assert_eq!(
                payload("eventsub.create", Some(bad)).await.unwrap_err(),
                ExecError::BadAction,
                "{bad}"
            );
        }
        assert_eq!(
            payload("eventsub.create", None).await.unwrap_err(),
            ExecError::BadAction
        );
    }

    #[tokio::test]
    async fn schedule_payloads_strip_the_segment_id_and_delete_takes_nothing_else() {
        let upd = r#"{"segment_id":"ZXhh==","title":"t","duration":60}"#;
        let p: Value =
            serde_json::from_str(&payload("schedule.update", Some(upd)).await.unwrap()).unwrap();
        let inner: Value = serde_json::from_str(p["text"].as_str().unwrap()).unwrap();
        assert_eq!(inner, serde_json::json!({"title":"t","duration":60}));
        assert_eq!(
            payload("schedule.delete", Some(r#"{"segment_id":"ZXhh=="}"#))
                .await
                .unwrap(),
            "{}"
        );
        for bad in [
            r#"{"segment_id":"ZXhh==","x":1}"#,
            r#"{"segment_id":"a b"}"#,
            r#"{"segment_id":""}"#,
            r#"{"x":1}"#,
        ] {
            assert_eq!(
                payload("schedule.delete", Some(bad)).await.unwrap_err(),
                ExecError::BadAction,
                "{bad}"
            );
        }
        assert!(payload("schedule.update", Some(r#"{"title":"t"}"#))
            .await
            .is_err());
    }

    #[tokio::test]
    async fn dynamic_slots_are_host_built_and_refuse_bad_input() {
        let t = r#"{"type":"stream.online","version":1,"callbackKey":"key-abcdefgh"}"#;
        assert_eq!(
            slot("eventsub.create", t, "TWITCH_EVENTSUB_CALLBACK")
                .await
                .unwrap(),
            "https://hooks.example.com/base/eventsub/twitch/webhook/key-abcdefgh"
        );
        assert_eq!(
            slot("eventsub.create", t, "TWITCH_EVENTSUB_SECRET")
                .await
                .unwrap(),
            "whsec"
        );
        let unknown = r#"{"type":"stream.online","version":1,"callbackKey":"unknown-key-1"}"#;
        assert_eq!(
            slot("eventsub.create", unknown, "TWITCH_EVENTSUB_SECRET")
                .await
                .unwrap_err(),
            ExecError::Slot(SlotError::Refused("secret_unavailable"))
        );
        assert_eq!(
            slot(
                "schedule.delete",
                r#"{"segment_id":"ZXhh=="}"#,
                "TWITCH_SEGMENT_ID"
            )
            .await
            .unwrap(),
            "ZXhh=="
        );
        assert!(slot(
            "schedule.delete",
            r#"{"segment_id":"a&b"}"#,
            "TWITCH_SEGMENT_ID"
        )
        .await
        .is_err());
        assert_eq!(
            slot("eventsub.create", t, "TWITCH_ANYTHING")
                .await
                .unwrap_err(),
            ExecError::HostDutyMissing
        );
    }

    // regression: callbackKey is producer text. A key registered to another community must not
    // yield that community's callback or secret, nor pass the create gate.
    #[tokio::test]
    async fn a_callback_key_of_another_community_is_refused_everywhere() {
        let t = r#"{"type":"stream.online","version":1,"callbackKey":"key-abcdefgh"}"#;
        let action = policy_action("eventsub.create", Some(t));
        let c = client();
        let cx_for = |community: i64| DutyCx {
            platform: "twitch",
            action: &action,
            community_id: Some(community),
            pinned_channel: None,
            client: &c,
        };
        let foreign = cx_for(9);
        let refused = ExecError::Slot(SlotError::Refused("secret_unavailable"));
        assert_eq!(duties().gate(&foreign).await.unwrap_err(), refused);
        for slot in ["TWITCH_EVENTSUB_CALLBACK", "TWITCH_EVENTSUB_SECRET"] {
            assert_eq!(
                duties().dynamic_slot(&foreign, slot, &c).await.unwrap_err(),
                refused,
                "{slot}"
            );
        }
        assert_eq!(duties().gate(&cx_for(7)).await, Ok(()));
    }

    #[tokio::test]
    async fn gate_needs_a_community_scope() {
        let action = policy_action("eventsub.create", None);
        let c = client();
        let cx = DutyCx {
            platform: "twitch",
            action: &action,
            community_id: None,
            pinned_channel: None,
            client: &c,
        };
        assert_eq!(
            duties().gate(&cx).await.unwrap_err(),
            ExecError::Slot(SlotError::Refused("no_community_scope"))
        );
    }

    fn local_client() -> GuardedClient {
        GuardedClient::new(
            GuardedLimits {
                max_redirects: 0,
                timeout: Duration::from_secs(5),
                max_response_bytes: 4096,
            },
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        )
    }

    fn delete_action(id: &str) -> OutboundAction {
        OutboundAction::PolicyOp(PolicyOp {
            op: "eventsub.delete",
            channel: None,
            text: None,
            message_id: Some(id.to_string()),
            user_id: None,
            origin_channel: Some("guild:-".to_string()),
            tenant_id: Some(3),
        })
    }

    fn app_duties(server: &MockServer) -> TwitchDuties<Secrets, App> {
        TwitchDuties::new(Secrets, "https://hooks.example.com/base")
            .unwrap()
            .with_app_source(App, "cid".to_string())
            .with_subs_endpoint(format!("{}/helix/eventsub/subscriptions", server.uri()))
    }

    async fn mount_sub(server: &MockServer, id: &str, condition: Value) {
        Mock::given(method("GET"))
            .and(path("/helix/eventsub/subscriptions"))
            .and(query_param("id", id))
            .and(header("authorization", "Bearer app-tok"))
            .and(header("client-id", "cid"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(
                    serde_json::json!({"data": [{"id": id, "condition": condition}]}),
                ),
            )
            .mount(server)
            .await;
    }

    async fn gate_delete(
        d: &TwitchDuties<Secrets, App>,
        community: Option<i64>,
        id: &str,
    ) -> Result<(), ExecError> {
        let action = delete_action(id);
        let c = local_client();
        let cx = DutyCx {
            platform: "twitch",
            action: &action,
            community_id: community,
            pinned_channel: None,
            client: &c,
        };
        d.gate(&cx).await
    }

    // regression: cross-tenant BOLA -- the app token can delete any subscription of the app.
    #[tokio::test]
    async fn delete_is_bound_to_the_communitys_own_broadcaster() {
        let server = MockServer::start().await;
        mount_sub(
            &server,
            "mine",
            serde_json::json!({"broadcaster_user_id": "111"}),
        )
        .await;
        mount_sub(
            &server,
            "raid",
            serde_json::json!({"to_broadcaster_user_id": "111"}),
        )
        .await;
        mount_sub(
            &server,
            "theirs",
            serde_json::json!({"broadcaster_user_id": "999"}),
        )
        .await;
        mount_sub(&server, "nocond", serde_json::json!({})).await;
        let d = app_duties(&server);
        assert!(d.claims("twitch", "eventsub.delete"));
        assert_eq!(gate_delete(&d, Some(7), "mine").await, Ok(()));
        assert_eq!(gate_delete(&d, Some(7), "raid").await, Ok(()));
        for (id, code) in [
            ("theirs", "subscription_not_owned"),
            ("nocond", "subscription_not_owned"),
            ("absent", "subscription_not_found"),
        ] {
            assert_eq!(
                gate_delete(&d, Some(7), id).await.unwrap_err(),
                ExecError::Slot(SlotError::Refused(code)),
                "{id}"
            );
        }
        // regression: broadcaster ids are not unique across communities. `shared` names the
        // SAME broadcaster (111) but is registered to community 9: community 7 is refused by the
        // ledger even though the broadcaster matches.
        mount_sub(
            &server,
            "shared",
            serde_json::json!({"broadcaster_user_id": "111"}),
        )
        .await;
        assert_eq!(
            gate_delete(&d, Some(7), "shared").await.unwrap_err(),
            ExecError::Slot(SlotError::Refused("subscription_not_owned"))
        );
        // Community 8 owns nothing in the ledger: refused before any lookup.
        assert_eq!(
            gate_delete(&d, Some(8), "mine").await.unwrap_err(),
            ExecError::Slot(SlotError::Refused("subscription_not_owned"))
        );
        assert_eq!(
            gate_delete(&d, None, "mine").await.unwrap_err(),
            ExecError::Slot(SlotError::Refused("no_community_scope"))
        );
        for bad in ["a&b", "", "x/../y", &"y".repeat(65)] {
            assert_eq!(
                gate_delete(&d, Some(7), bad).await.unwrap_err(),
                ExecError::BadAction,
                "{bad:?}"
            );
        }
    }

    #[tokio::test]
    async fn delete_lookup_failures_refuse_and_the_default_does_not_claim_delete_or_list() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(500))
            .mount(&server)
            .await;
        let d = app_duties(&server);
        assert_eq!(
            gate_delete(&d, Some(7), "mine").await.unwrap_err(),
            ExecError::Slot(SlotError::Unavailable("subscription_lookup_failed"))
        );
        // Without an app source delete is unclaimed; list is never claimed.
        assert!(!duties().claims("twitch", "eventsub.delete"));
        assert!(!d.claims("twitch", "eventsub.list"));
        assert!(!duties().claims("twitch", "eventsub.list"));
    }

    #[tokio::test]
    async fn duty_pair_routes_by_claim() {
        let pair = DutyPair::new(super::super::NoDuties, duties());
        assert!(pair.claims("twitch", "eventsub.create"));
        assert!(!pair.claims("twitch", "mod.ban"));
        let t = r#"{"type":"stream.online","version":1,"callbackKey":"key-abcdefgh"}"#;
        let action = policy_action("eventsub.create", Some(t));
        let c = client();
        let cx = DutyCx {
            platform: "twitch",
            action: &action,
            community_id: Some(7),
            pinned_channel: None,
            client: &c,
        };
        assert!(pair.payload(&cx).await.is_ok());
        assert_eq!(
            pair.dynamic_slot(&cx, "TWITCH_EVENTSUB_SECRET", &c)
                .await
                .unwrap()
                .expose(),
            "whsec"
        );
    }
}
