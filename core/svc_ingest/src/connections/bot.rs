//! One dynamic Twitch bot: a single IRC socket for one bot account, joined to
//! every channel that account is bound to, supervised through reconnects.
//!
//! The supervisor (`super::supervisor`) owns *what* should run (the desired
//! [`BotSpec`], pushed through a `watch` channel); this module owns *how* one
//! bot runs it:
//!
//! 1. **Acquire credentials.** Every (re)connect re-resolves each member's
//!    connection through hub-api -- the token is never cached past a reconnect;
//!    hub-api refreshes a stale one inside the call. The first successful token
//!    wins -- every member's token is first verified to authenticate as THIS
//!    bot's login (a missing/malformed `bot_login` fails closed), so tokens in
//!    one group are interchangeable for IRC identity.
//!    A *terminal* refusal (`ClientError::NotUsable`: 409 `CONNECTION_NOT_USABLE`
//!    / `REFRESH_REJECTED`, or an enveloped 404 `NOT_FOUND`) **drops that member
//!    for good** and is reported to the supervisor. Anything else (5xx, 401/403,
//!    409 `REFRESH_NOT_CONFIGURED`, a bare 404 from a mid-rollout replica, ...)
//!    is transient: a member that cannot resolve right now is **skipped -- its
//!    channels are never joined under another member's token** -- while a
//!    background probe re-tries it with backoff and, the moment it resolves,
//!    triggers one reconnect that joins it. A bot with no usable token at all
//!    backs off and retries the whole pass.
//! 2. **Connect and JOIN all channels on ONE socket**
//!    (`penguin_connector_twitch`'s `IrcConfig::channel` carries a
//!    comma-separated list, rendered as a single `JOIN #a,#b,#c`).
//! 3. **Route** each inbound `PRIVMSG` by its channel to the member bindings
//!    that own it and publish under each binding's spine `Scope`.
//! 4. **Reconnect on membership change.** The pinned connector exposes no
//!    in-place `JOIN`/`PART`, so adding or removing a channel reconnects the
//!    bot's one socket (a brief gap, coalesced per reconcile pass).
//!
//! Reconnect discipline mirrors `crate::ingest::twitch::run_loop` (see
//! `crate::ingest::Backoff`'s incident note): the backoff delay is applied --
//! racing shutdown and membership changes -- on *every* failure path, and
//! reset only once the connection proved itself (delivered a line, or survived
//! `STABILITY_WINDOW`), never on a bare successful connect. Stability is
//! evaluated at failure time rather than by a timer inside the receive
//! `select!`, because dropping `IrcConnection::recv` mid-`read_line` can
//! discard a partially read line.
//!
//! **Token hygiene:** the raw token is held only in [`AccessToken`] and, for
//! the lifetime of one connection, inside the connector's `IrcConfig` (whose
//! derived `Debug` would print it -- so no `IrcConfig` is ever formatted here).
//! Nothing in this module logs a token, a login, or a channel name; log lines
//! carry numeric grant/connection ids, counts and a short hashed bot reference.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use penguin_connector_twitch::irc::{ChatMessage, TwitchIrcReceiver};
use penguin_connector_twitch::TwitchError;
use penguin_spine::{KeyRing, Scope, SpineMetrics};
use sha2::{Digest, Sha256};
use tokio::sync::{mpsc, oneshot, watch};

use super::client::{ClientError, ConnectionsApi};
use super::dto::{AccessToken, ResolvedCredentials, PROVIDER_TWITCH};
use super::metrics::ConnectionsMetrics;
use crate::ingest::{twitch, Backoff, STABILITY_WINDOW};
use crate::publish::{deterministic_workstream_id, publish_event, EventAppender};

/// One (community, connection) channel binding a bot serves.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Member {
    /// hub-api grant id -- the binding's stable identity across polls.
    pub grant_id: i64,
    /// Connection row to resolve for credentials.
    pub connection_id: i64,
    /// Community the connection is granted to (also the resolve path segment).
    pub community_id: i64,
    /// Owning tenant id.
    pub tenant_id: i64,
    /// Normalized channel login (no `#`, lowercase, allow-list validated).
    pub channel: String,
    /// The spine scope events from this binding are minted under.
    pub scope: Scope,
    /// hub-api's last-modified stamp, echoed back when this member is dropped
    /// so the supervisor can tell "same refused binding" from "edited since".
    pub updated_at: Option<String>,
}

/// What one bot should be running: its IRC nick and every channel binding it
/// serves. `members` is kept sorted by `grant_id` so two specs compare equal
/// iff they describe the same work.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BotSpec {
    /// Normalized bot login -- the IRC `NICK` and the grouping key.
    pub bot_login: String,
    /// Channel bindings served by this bot's socket.
    pub members: Vec<Member>,
}

/// Messages a bot task sends back to its supervisor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BotEvent {
    /// hub-api refused this member's connection (409/404/permanent): the
    /// supervisor must not re-add it until the binding changes or vanishes.
    MemberDropped {
        /// Dropped member's grant id.
        grant_id: i64,
        /// The binding's `updatedAt` at drop time.
        updated_at: Option<String>,
    },
}

/// IRC endpoint settings shared by every bot (`TWITCH_IRC_HOST`/`_PORT`/
/// `_USE_TLS` -- the same env the static path uses).
#[derive(Debug, Clone)]
pub struct IrcSettings {
    /// IRC server host.
    pub host: String,
    /// IRC server port.
    pub port: u16,
    /// Whether to wrap the socket in TLS (always `true` against real Twitch).
    pub use_tls: bool,
}

/// Everything a bot needs to mint and publish events -- the spine publish
/// dependencies the static path threads through `ingest::twitch::run`.
pub struct PublishCtx<A, M> {
    /// The spine `XADD` seam.
    pub appender: A,
    /// Spine write metrics.
    pub metrics: M,
    /// D30 binding keyring.
    pub keyring: KeyRing,
    /// Active binding `kid`.
    pub active_kid: String,
}

/// Cheap, non-reversible log reference for a bot login (first 8 hex chars of
/// its SHA-256): lets an operator correlate lines for one bot without writing
/// the account handle into logs.
#[must_use]
pub fn bot_ref(bot_login: &str) -> String {
    let digest = Sha256::digest(bot_login.as_bytes());
    digest.iter().take(4).map(|b| format!("{b:02x}")).collect()
}

/// RAII counter for the `channels_joined` gauge: adds on creation, subtracts on
/// drop, so a cancelled or panicking bot task can never leave the gauge high.
struct JoinedGuard {
    metrics: Arc<ConnectionsMetrics>,
    count: i64,
}

impl JoinedGuard {
    fn new(metrics: &Arc<ConnectionsMetrics>, count: usize) -> Self {
        let count = i64::try_from(count).unwrap_or(i64::MAX);
        metrics.channels_joined.add(count);
        Self {
            metrics: Arc::clone(metrics),
            count,
        }
    }
}

impl Drop for JoinedGuard {
    fn drop(&mut self) {
        self.metrics.channels_joined.sub(self.count);
    }
}

/// Outcome of one credential-acquisition pass over a bot's members.
enum Acquired {
    /// A token, the members that resolved (their channels get joined), and the
    /// members that failed *transiently* (skipped this session, probed in the
    /// background until they resolve).
    Ready {
        token: AccessToken,
        members: Vec<Member>,
        pending: Vec<Member>,
    },
    /// Every member was terminally refused -- nothing left to serve; the bot
    /// exits.
    AllDropped,
    /// No token yet but at least one member may still resolve -- back off.
    Retry,
}

/// Outcome of resolving ONE member's connection.
enum MemberOutcome {
    /// A token verified to authenticate as this bot's login.
    Usable(AccessToken),
    /// A terminal refusal (reason label): drop the member for good.
    Refused(String),
    /// Not resolvable right now: retry later. The member must NOT be joined
    /// under another member's token in the meantime.
    Transient,
}

/// True iff `creds` is a Twitch token that authenticates as `bot_login`.
///
/// Fails **closed**: a resolve answer without a valid `bot_login` is rejected,
/// never accepted -- authenticating the socket as an account we could not
/// confirm would attribute chat to the wrong identity.
fn identity_matches(creds: &ResolvedCredentials, bot_login: &str) -> bool {
    creds.provider == PROVIDER_TWITCH && creds.bot_login().is_some_and(|login| login == bot_login)
}

/// Resolves one member and classifies the answer (see [`MemberOutcome`]).
/// Logs carry numeric ids and the redacted bot reference only -- never a token.
async fn resolve_member<C: ConnectionsApi>(
    api: &C,
    bot_login: &str,
    member: &Member,
) -> MemberOutcome {
    match api.resolve(member.community_id, member.connection_id).await {
        Ok(creds) => {
            if identity_matches(&creds, bot_login) {
                MemberOutcome::Usable(creds.access_token)
            } else {
                MemberOutcome::Refused("identity_mismatch".to_string())
            }
        }
        Err(ClientError::NotUsable { reason, .. }) => MemberOutcome::Refused(reason),
        Err(ClientError::Permanent { status, code }) => {
            tracing::warn!(
                bot = %bot_ref(bot_login),
                grant_id = member.grant_id,
                connection_id = member.connection_id,
                community_id = member.community_id,
                status,
                code = %code,
                "hub-api rejected the resolve request; channel skipped, will retry with backoff"
            );
            MemberOutcome::Transient
        }
        Err(ClientError::Transient(err)) => {
            tracing::warn!(
                bot = %bot_ref(bot_login),
                grant_id = member.grant_id,
                connection_id = member.connection_id,
                community_id = member.community_id,
                error = %err,
                "transient hub-api failure resolving credentials; channel skipped, will retry"
            );
            MemberOutcome::Transient
        }
    }
}

/// Counts + logs a terminal refusal and tells the supervisor to stop
/// re-admitting this binding until it changes.
fn refuse_member(
    metrics: &ConnectionsMetrics,
    events: &mpsc::UnboundedSender<BotEvent>,
    bot_login: &str,
    member: &Member,
    reason: &str,
) {
    metrics.record_not_usable(reason);
    tracing::warn!(
        bot = %bot_ref(bot_login),
        grant_id = member.grant_id,
        connection_id = member.connection_id,
        community_id = member.community_id,
        reason = super::metrics::bounded_reason(reason),
        "hub-api refused connection; dropping channel (no retry)"
    );
    let _ = events.send(BotEvent::MemberDropped {
        grant_id: member.grant_id,
        updated_at: member.updated_at.clone(),
    });
}

/// Resolves every member's connection: terminally refused ones are dropped,
/// transiently failing ones are set aside as `pending`.
///
/// Resolves are sequential (a bot has a handful of members; hub-api resolve
/// takes a row lock). A token for a different bot login than the group's (or
/// with no verifiable login), or for a different provider, is a refusal
/// (`identity_mismatch`).
async fn acquire<C: ConnectionsApi>(
    api: &C,
    metrics: &ConnectionsMetrics,
    events: &mpsc::UnboundedSender<BotEvent>,
    bot_login: &str,
    members: &[Member],
    cause: &str,
) -> Acquired {
    let mut token: Option<AccessToken> = None;
    let mut keep: Vec<Member> = Vec::with_capacity(members.len());
    let mut pending: Vec<Member> = Vec::new();

    for member in members {
        match resolve_member(api, bot_login, member).await {
            MemberOutcome::Usable(creds_token) => {
                metrics
                    .tokens_acquired_total
                    .with_label_values(&[cause])
                    .inc();
                if token.is_none() {
                    token = Some(creds_token);
                }
                keep.push(member.clone());
            }
            MemberOutcome::Refused(reason) => {
                refuse_member(metrics, events, bot_login, member, &reason);
            }
            MemberOutcome::Transient => pending.push(member.clone()),
        }
    }

    match token {
        Some(token) => Acquired::Ready {
            token,
            members: keep,
            pending,
        },
        None if pending.is_empty() => Acquired::AllDropped,
        None => Acquired::Retry,
    }
}

/// Aborts the recovery-probe task when a session ends (reconnect, shutdown,
/// exit) so a probe never outlives the connection it was started for.
struct ProbeGuard(tokio::task::JoinHandle<()>);

impl Drop for ProbeGuard {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Background re-try of the members skipped as transient. Re-resolves them with
/// backoff -- a *probe only*, it never touches the live IRC socket -- and fires
/// the returned receiver once any of them resolves, so the bot reconnects ONCE
/// to join it. A probe-time terminal refusal is reported like any other drop;
/// the task ends silently (receiver errors) once nothing is left to probe. A
/// persistently failing member therefore costs one resolve per backoff interval
/// and never churns the healthy channels' socket.
fn spawn_recovery_probe<C: ConnectionsApi>(
    api: Arc<C>,
    metrics: Arc<ConnectionsMetrics>,
    events: mpsc::UnboundedSender<BotEvent>,
    bot_login: String,
    pending: Vec<Member>,
    backoff_cap: Duration,
) -> (ProbeGuard, oneshot::Receiver<()>) {
    let (tx, rx) = oneshot::channel();
    let handle = tokio::spawn(async move {
        let mut backoff = Backoff::new(backoff_cap);
        let mut pending = pending;
        loop {
            tokio::time::sleep(backoff.delay()).await;
            let mut still: Vec<Member> = Vec::new();
            let mut recovered = false;
            for member in pending {
                match resolve_member(api.as_ref(), &bot_login, &member).await {
                    MemberOutcome::Usable(token) => {
                        drop(token);
                        recovered = true;
                    }
                    MemberOutcome::Refused(reason) => {
                        refuse_member(&metrics, &events, &bot_login, &member, &reason);
                    }
                    MemberOutcome::Transient => still.push(member),
                }
            }
            if recovered {
                let _ = tx.send(());
                return;
            }
            if still.is_empty() {
                return;
            }
            pending = still;
        }
    });
    (ProbeGuard(handle), rx)
}

/// Renders `members`' distinct channels as the comma-separated `#a,#b` list the
/// connector's single `JOIN` line takes. Order is stable (first occurrence).
fn join_list(members: &[Member]) -> String {
    let mut seen: Vec<&str> = Vec::new();
    for m in members {
        if !seen.contains(&m.channel.as_str()) {
            seen.push(m.channel.as_str());
        }
    }
    seen.iter()
        .map(|c| format!("#{c}"))
        .collect::<Vec<_>>()
        .join(",")
}

/// channel -> member bindings that own it (a channel may feed several
/// communities).
fn route_table(members: &[Member]) -> HashMap<&str, Vec<&Member>> {
    let mut table: HashMap<&str, Vec<&Member>> = HashMap::new();
    for m in members {
        table.entry(m.channel.as_str()).or_default().push(m);
    }
    table
}

/// Distinct scopes among `owners`, first-occurrence order -- one publish per
/// distinct scope, so a channel bound twice under the same scope (e.g. runner
/// scope mode) never double-publishes an event.
fn distinct_scopes<'a>(owners: &[&'a Member]) -> Vec<&'a Scope> {
    let mut scopes: Vec<&Scope> = Vec::new();
    for m in owners {
        if !scopes.contains(&&m.scope) {
            scopes.push(&m.scope);
        }
    }
    scopes
}

/// Sleeps `delay`, returning early with `Some(reason)` if the bot is told to
/// shut down or its desired spec changes (so a backoff never delays acting on
/// either).
enum Wake {
    Elapsed,
    Shutdown,
    SpecChanged,
}

async fn sleep_or_wake(
    delay: Duration,
    shutdown: &mut oneshot::Receiver<()>,
    desired: &mut watch::Receiver<BotSpec>,
) -> Wake {
    tokio::select! {
        _ = &mut *shutdown => Wake::Shutdown,
        changed = desired.changed() => if changed.is_ok() { Wake::SpecChanged } else { Wake::Shutdown },
        () = tokio::time::sleep(delay) => Wake::Elapsed,
    }
}

/// Runs one bot until `shutdown` fires, its desired spec closes, or every
/// member has been refused. Never returns for a transient failure -- it backs
/// off and retries.
#[allow(clippy::too_many_arguments)] // every collaborator is independently varied across tests
pub async fn run_bot<C, A, M>(
    api: Arc<C>,
    publish: Arc<PublishCtx<A, M>>,
    metrics: Arc<ConnectionsMetrics>,
    irc: IrcSettings,
    events: mpsc::UnboundedSender<BotEvent>,
    mut desired: watch::Receiver<BotSpec>,
    mut shutdown: oneshot::Receiver<()>,
    backoff_cap: Duration,
) where
    C: ConnectionsApi,
    A: EventAppender + 'static,
    M: SpineMetrics + 'static,
{
    let mut backoff = Backoff::new(backoff_cap);
    let mut cause = "initial";

    'outer: loop {
        let spec = desired.borrow_and_update().clone();
        let bot = bot_ref(&spec.bot_login);
        if spec.members.is_empty() {
            tracing::info!(bot = %bot, "dynamic bot has no members; exiting");
            return;
        }

        let acquired = tokio::select! {
            _ = &mut shutdown => return,
            acquired = acquire(api.as_ref(), &metrics, &events, &spec.bot_login, &spec.members, cause) => acquired,
        };
        let (token, members, pending) = match acquired {
            Acquired::Ready {
                token,
                members,
                pending,
            } => (token, members, pending),
            Acquired::AllDropped => {
                tracing::warn!(bot = %bot, "every channel for this bot was refused by hub-api; exiting");
                return;
            }
            Acquired::Retry => {
                let delay = backoff.delay();
                tracing::debug!(bot = %bot, ?delay, "no credentials yet; backing off");
                match sleep_or_wake(delay, &mut shutdown, &mut desired).await {
                    Wake::Shutdown => return,
                    Wake::SpecChanged | Wake::Elapsed => continue 'outer,
                }
            }
        };

        metrics
            .bot_reconnects_total
            .with_label_values(&[cause])
            .inc();
        let channels = join_list(&members);
        let irc_cfg = twitch::irc_config(
            &irc.host,
            irc.port,
            &spec.bot_login,
            &channels,
            token.expose(),
            irc.use_tls,
        );
        drop(token);
        let receiver = TwitchIrcReceiver::new(irc_cfg);

        let connect_started = Instant::now();
        let connected = tokio::select! {
            _ = &mut shutdown => return,
            changed = desired.changed() => {
                if changed.is_err() { return; }
                cause = "membership_changed";
                continue 'outer;
            }
            result = receiver.connect() => result,
        };
        let mut conn = match connected {
            Ok(conn) => conn,
            Err(err) => {
                let auth_rejected = matches!(err, TwitchError::RegistrationRejected(_));
                cause = if auth_rejected {
                    "auth_rejected"
                } else {
                    "error"
                };
                if auth_rejected {
                    // Re-resolve (hub-api refreshes) on the next pass -- never
                    // stop forever on an auth failure a refresh may cure.
                    tracing::error!(bot = %bot, error = %err, "twitch rejected registration; re-resolving credentials");
                } else {
                    tracing::warn!(bot = %bot, error = %err, "twitch irc connect failed; retrying");
                }
                let delay = backoff.delay();
                match sleep_or_wake(delay, &mut shutdown, &mut desired).await {
                    Wake::Shutdown => return,
                    Wake::SpecChanged => cause = "membership_changed",
                    Wake::Elapsed => {}
                }
                continue 'outer;
            }
        };

        let joined = JoinedGuard::new(&metrics, members.len());
        tracing::info!(
            bot = %bot,
            channels = members.len(),
            "dynamic twitch bot connected and joined, awaiting session stability"
        );
        let routes = route_table(&members);
        let mut proven = false;

        // Members that could not resolve this pass are NOT joined (never under
        // another member's token); a probe re-tries them and signals one
        // reconnect when any resolves.
        let mut probe_live = !pending.is_empty();
        let (_probe_guard, mut recovery_rx) = if pending.is_empty() {
            (None, oneshot::channel().1)
        } else {
            tracing::warn!(
                bot = %bot,
                skipped = pending.len(),
                "some channels could not resolve credentials; serving the rest, probing the skipped ones"
            );
            let (guard, rx) = spawn_recovery_probe(
                Arc::clone(&api),
                Arc::clone(&metrics),
                events.clone(),
                spec.bot_login.clone(),
                pending,
                backoff_cap,
            );
            (Some(guard), rx)
        };

        loop {
            let recv = tokio::select! {
                _ = &mut shutdown => return,
                changed = desired.changed() => {
                    if changed.is_err() { return; }
                    tracing::info!(bot = %bot, "membership changed; reconnecting");
                    cause = "membership_changed";
                    drop(joined);
                    continue 'outer;
                }
                recovered = &mut recovery_rx, if probe_live => {
                    probe_live = false;
                    if recovered.is_ok() {
                        tracing::info!(bot = %bot, "a skipped channel's credentials resolved; reconnecting to join it");
                        cause = "member_recovered";
                        drop(joined);
                        continue 'outer;
                    }
                    // Probe ended with nothing left to retry (all refused).
                    continue;
                }
                recv = conn.recv() => recv,
            };
            match recv {
                Ok(Some(msg)) => {
                    proven = true;
                    route_message(&publish, &metrics, &spec.bot_login, &routes, &msg).await;
                }
                Ok(None) | Err(_) => {
                    let (reason, retryable) = match &recv {
                        Ok(_) => ("closed", true),
                        Err(err) => ("error", err.is_retryable()),
                    };
                    if let Err(err) = &recv {
                        tracing::warn!(bot = %bot, error = %err, "twitch irc recv error; reconnecting");
                    } else {
                        tracing::warn!(bot = %bot, "twitch irc connection closed; reconnecting");
                    }
                    if !retryable {
                        tracing::error!(bot = %bot, "non-retryable twitch irc error; will re-resolve and retry with backoff");
                    }
                    // Stability is judged now: a delivered line, or surviving
                    // the window, proves the session -- reset before delaying.
                    if proven || connect_started.elapsed() >= STABILITY_WINDOW {
                        backoff.reset();
                    }
                    cause = reason;
                    drop(joined);
                    let delay = backoff.delay();
                    match sleep_or_wake(delay, &mut shutdown, &mut desired).await {
                        Wake::Shutdown => return,
                        Wake::SpecChanged => cause = "membership_changed",
                        Wake::Elapsed => {}
                    }
                    continue 'outer;
                }
            }
        }
    }
}

/// Routes one inbound `PRIVMSG`: skips the bot's own echo, then publishes once
/// per distinct scope among the bindings that own the message's channel.
async fn route_message<A: EventAppender, M: SpineMetrics>(
    publish: &PublishCtx<A, M>,
    metrics: &ConnectionsMetrics,
    bot_login: &str,
    routes: &HashMap<&str, Vec<&Member>>,
    msg: &ChatMessage,
) {
    if crate::normalize::is_self_message(&msg.sender, bot_login) {
        metrics
            .events_routed_total
            .with_label_values(&["self_echo"])
            .inc();
        return;
    }
    let channel = msg.channel.trim_start_matches('#').to_ascii_lowercase();
    let Some(owners) = routes.get(channel.as_str()) else {
        metrics
            .events_routed_total
            .with_label_values(&["unrouted"])
            .inc();
        tracing::debug!("message for a channel this bot does not serve; dropped");
        return;
    };
    let source_id = twitch::source_id(&channel);
    let workstream_id = deterministic_workstream_id(&source_id);
    for scope in distinct_scopes(owners) {
        let event = crate::normalize::normalize_twitch_irc(msg);
        match publish_event(
            &publish.appender,
            &publish.metrics,
            &publish.keyring,
            &publish.active_kid,
            scope,
            &source_id,
            &workstream_id,
            None,
            event,
        )
        .await
        {
            Ok(_) => metrics
                .events_routed_total
                .with_label_values(&["published"])
                .inc(),
            Err(err) => {
                metrics
                    .events_routed_total
                    .with_label_values(&["publish_failed"])
                    .inc();
                tracing::error!(error = %err, "failed to publish dynamic twitch chat event");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn member(grant_id: i64, channel: &str, tenant: &str, community: Option<&str>) -> Member {
        Member {
            grant_id,
            connection_id: grant_id * 10,
            community_id: grant_id,
            tenant_id: 1,
            channel: channel.to_string(),
            scope: Scope::new(tenant, community.map(str::to_string)),
            updated_at: None,
        }
    }

    #[test]
    fn join_list_is_one_comma_separated_line_with_distinct_channels() {
        let members = vec![
            member(1, "alpha", "t", None),
            member(2, "beta", "t", None),
            member(3, "alpha", "t", Some("c")),
        ];
        assert_eq!(join_list(&members), "#alpha,#beta");
        assert_eq!(join_list(&members[..1]), "#alpha");
    }

    #[test]
    fn route_table_groups_bindings_by_channel() {
        let members = vec![
            member(1, "alpha", "t", None),
            member(2, "beta", "t", None),
            member(3, "alpha", "u", None),
        ];
        let table = route_table(&members);
        assert_eq!(table["alpha"].len(), 2);
        assert_eq!(table["beta"].len(), 1);
        assert!(!table.contains_key("gamma"));
    }

    #[test]
    fn distinct_scopes_dedupes_identical_scopes_but_keeps_different_ones() {
        let members = [
            member(1, "alpha", "t", None),
            member(2, "alpha", "t", None),
            member(3, "alpha", "u", Some("c")),
        ];
        let refs: Vec<&Member> = members.iter().collect();
        let scopes = distinct_scopes(&refs);
        assert_eq!(scopes.len(), 2);
        assert_eq!(scopes[0].tenant, "t");
        assert_eq!(scopes[1].tenant, "u");
    }

    #[test]
    fn bot_ref_is_stable_short_and_does_not_contain_the_login() {
        let a = bot_ref("waddlebots");
        assert_eq!(a, bot_ref("waddlebots"));
        assert_eq!(a.len(), 8);
        assert!(!a.contains("waddle"));
        assert_ne!(a, bot_ref("otherbot"));
    }

    fn creds(provider: &str, metadata: serde_json::Value) -> ResolvedCredentials {
        serde_json::from_value(serde_json::json!({
            "success": true, "connectionId": 1, "communityId": 1, "provider": provider,
            "accessToken": "tok", "metadata": metadata
        }))
        .unwrap()
    }

    // regression: F5 -- a resolve answer whose bot_login is missing or malformed
    // must FAIL CLOSED (rejected), not be accepted as "nothing to contradict".
    #[test]
    fn identity_matches_fails_closed_without_a_verifiable_bot_login() {
        let good = creds("twitch", serde_json::json!({"bot_login": "waddlebots"}));
        assert!(identity_matches(&good, "waddlebots"));
        // Normalization applies (case, leading '#') but never widens the match.
        let cased = creds("twitch", serde_json::json!({"bot_login": "WaddleBots"}));
        assert!(identity_matches(&cased, "waddlebots"));

        assert!(!identity_matches(&good, "someoneelse"), "wrong login");
        for metadata in [
            serde_json::json!({}),
            serde_json::json!({"bot_login": ""}),
            serde_json::json!({"bot_login": "has space"}),
            serde_json::json!({"bot_login": 42}),
            serde_json::json!({"bot_login": null}),
        ] {
            assert!(
                !identity_matches(&creds("twitch", metadata.clone()), "waddlebots"),
                "missing/invalid bot_login {metadata} must be rejected"
            );
        }
        let wrong_provider = creds("discord", serde_json::json!({"bot_login": "waddlebots"}));
        assert!(!identity_matches(&wrong_provider, "waddlebots"));
    }

    #[test]
    fn joined_guard_adds_then_subtracts_even_on_drop() {
        let metrics = Arc::new(ConnectionsMetrics::register(&prometheus::Registry::new()).unwrap());
        {
            let _g = JoinedGuard::new(&metrics, 3);
            assert_eq!(metrics.channels_joined.get(), 3);
            let _g2 = JoinedGuard::new(&metrics, 2);
            assert_eq!(metrics.channels_joined.get(), 5);
        }
        assert_eq!(metrics.channels_joined.get(), 0);
    }
}
