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
//!    wins (tokens for one bot login are interchangeable for IRC identity).
//!    A refusal (`ClientError::NotUsable`, HTTP 409/404) **drops that member for
//!    good** and is reported to the supervisor; transient failures retry with
//!    backoff.
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
use super::dto::{AccessToken, PROVIDER_TWITCH};
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
    /// A token plus the members still believed usable.
    Ready {
        token: AccessToken,
        members: Vec<Member>,
    },
    /// Every member was refused -- nothing left to serve; the bot exits.
    AllDropped,
    /// No token yet but at least one member may still be usable -- back off.
    Retry,
}

/// Resolves every member's connection, dropping refused ones.
///
/// Resolves are sequential (a bot has a handful of members; hub-api resolve
/// takes a row lock). A token for a different bot login than the group's, or
/// for a different provider, is treated as a refusal (`identity_mismatch`):
/// authenticating the socket as another account would attribute chat to the
/// wrong identity.
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

    for member in members {
        let drop_member = |reason: &str| {
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
        };
        match api.resolve(member.community_id, member.connection_id).await {
            Ok(creds) => {
                let wrong_provider = creds.provider != PROVIDER_TWITCH;
                let wrong_login = creds.bot_login().is_some_and(|login| login != bot_login);
                if wrong_provider || wrong_login {
                    drop_member("identity_mismatch");
                    continue;
                }
                metrics
                    .tokens_acquired_total
                    .with_label_values(&[cause])
                    .inc();
                if token.is_none() {
                    token = Some(creds.access_token);
                }
                keep.push(member.clone());
            }
            Err(ClientError::NotUsable { reason, .. }) => drop_member(&reason),
            Err(ClientError::Permanent { .. }) => drop_member("other"),
            Err(ClientError::Transient(err)) => {
                tracing::warn!(
                    bot = %bot_ref(bot_login),
                    grant_id = member.grant_id,
                    connection_id = member.connection_id,
                    error = %err,
                    "transient hub-api failure resolving credentials; will retry"
                );
                keep.push(member.clone());
            }
        }
    }

    match token {
        Some(token) => Acquired::Ready {
            token,
            members: keep,
        },
        None if keep.is_empty() => Acquired::AllDropped,
        None => Acquired::Retry,
    }
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
        let (token, members) = match acquired {
            Acquired::Ready { token, members } => (token, members),
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
