//! Twitch outbound relay drain (spec S4.1/S5.5): `BRPOP`s the plain Valkey
//! list `svc_action::capabilities::outbound_relay_queue_key("twitch")`
//! (`waddles:transport:irc:twitch:outbound`) that `svc_action`'s `relay`
//! host capability `LPUSH`es onto, and sends each queued
//! `{"channel", "text"}` message over a fresh
//! `penguin_connector_twitch::irc::TwitchIrcSender` connection.
//!
//! **Credential resolution.** Per-community, resolved on every send
//! ([`crate::outbound_creds`]): the queue entry carries the numeric
//! `community_id` (stamped by `svc_action` from the binding-verified scope),
//! the drain resolves that community's own Twitch connection through
//! `connection_resolver` for each message and never caches the secret. A
//! missing/unresolvable credential is a typed refusal -- there is NO fallback
//! to any process-wide token. The single process-wide identity
//! (`TWITCH_IRC_NICK`/`TWITCH_IRC_OAUTH_TOKEN`) survives only as the
//! documented legacy [`crate::outbound_creds::StaticCredentialSource`],
//! chosen at startup while `waddles.ingest-dynamic-connections` is OFF.
//!
//! `penguin_spine::SpineClient` is Streams-only (`XADD`/`XREADGROUP`); the
//! relay queue is a plain list (`LPUSH`/`BRPOP`), out of that surface on
//! purpose -- this module uses the `redis` crate directly (matching the M5
//! plan's own "Direct Valkey client ... for the Twitch outbound relay's
//! raw BRPOP list" note).

use std::time::Duration;

// `TwitchError` is re-exported at the crate root (`pub use error::
// TwitchError` in `penguin_connector_twitch::lib`) but not from the `irc`
// submodule itself (`irc.rs` only privately `use`s it) -- import from the
// root, not `irc::TwitchError`.
use penguin_connector_twitch::irc::TwitchIrcSender;
use penguin_connector_twitch::TwitchError;
use tokio::sync::oneshot;

use crate::egress::Egress;
use crate::ingest::Backoff;
use crate::outbound_creds::{CredError, TwitchCredentialSource};
use crate::outbound_ops::{
    dispatch_to, parse_outbound_entry, route, ActionExecutor, DiscordSender, NoExecutor,
    OutboundEntry, SenderError, TwitchSender,
};
use crate::outbound_pin::{PinKeys, VerifiedStamp};
use egress_detokenizer::DetokenizedText;

/// The queue key this module drains -- byte-identical to
/// `core/svc_action/src/capabilities.rs::outbound_relay_queue_key("twitch")`,
/// duplicated as a constant here (not imported) since `svc_action` and
/// `svc_ingest` are separate crates/binaries with no shared-code
/// dependency between them for this one string.
pub const TWITCH_OUTBOUND_QUEUE_KEY: &str = "waddles:transport:irc:twitch:outbound";

/// `BRPOP` timeout, seconds -- bounded so the drain loop wakes up
/// periodically to check `shutdown` even with an empty queue, rather than
/// blocking on the Valkey connection indefinitely.
const BRPOP_TIMEOUT_SECS: f64 = 5.0;

/// Client-side response timeout for a connection that blocks in `BRPOP`.
///
/// **Must exceed [`BRPOP_TIMEOUT_SECS`].** `redis` 1.x gives every
/// multiplexed connection a default 500ms response timeout; a `BRPOP` that
/// blocks for up to 5s therefore always "timed out" client-side while the
/// server kept waiting -- and when an item then arrived the server popped it
/// and replied to a caller that had already given up, so the entry was LOST
/// (found by driving the real drain against a live Valkey: ops were popped but
/// never executed or acked). The margin keeps a genuinely dead connection
/// detectable without cutting a healthy block short.
const BLOCKING_CONN_RESPONSE_TIMEOUT: Duration = Duration::from_secs(15);

/// Response timeout for the drain's non-blocking control connection
/// (heartbeat, ack writes): generous next to a Valkey `SET`, short enough
/// that a hung connection is noticed.
const CONTROL_CONN_RESPONSE_TIMEOUT: Duration = Duration::from_secs(2);

/// Opens a multiplexed connection with an explicit response timeout (see
/// [`BLOCKING_CONN_RESPONSE_TIMEOUT`] for why the default is unusable for a
/// blocking pop).
async fn connect_with_response_timeout(
    client: &redis::Client,
    response_timeout: Duration,
) -> Result<redis::aio::ConnectionManager, redis::RedisError> {
    let config =
        redis::aio::ConnectionManagerConfig::new().set_response_timeout(Some(response_timeout));
    client.get_connection_manager_with_config(config).await
}

/// Builds a `redis::Client` for `cfg`'s transport, mirroring
/// `penguin_spine`'s own (private, unexported) `build_redis_client` --
/// duplicated here rather than imported since that function is
/// `pub(crate)` inside `penguin-spine` and this module needs a plain-list
/// connection, not a `SpineClient`.
pub(crate) fn build_redis_client(
    cfg: &penguin_spine::SpineConfig,
) -> Result<redis::Client, redis::RedisError> {
    let base: redis::ConnectionInfo =
        redis::IntoConnectionInfo::into_connection_info(cfg.valkey_url.as_str())?;
    let mut settings = base.redis_settings().clone();
    if let Some(username) = &cfg.valkey_username {
        settings = settings.set_username(username);
    }
    if let Some(password) = &cfg.valkey_password {
        settings = settings.set_password(password);
    }
    let info = base.set_redis_settings(settings);

    if cfg.security_transport_tls {
        crate::crypto::ensure_installed();
        let root_cert = std::fs::read(&cfg.valkey_ca_file).ok();
        redis::Client::build_with_tls(
            info,
            redis::TlsCertificates {
                client_tls: None,
                root_cert,
            },
        )
    } else {
        redis::Client::open(info)
    }
}

/// Twitch IRC endpoint settings shared by every community (the credential
/// is per-community and comes from the [`TwitchCredentialSource`]).
pub struct TwitchIrcEndpoint {
    pub host: String,
    pub port: u16,
    pub use_tls: bool,
}

/// Abstraction over `BRPOP`ing one raw JSON payload off the outbound relay
/// list -- lets [`drain_loop`] be driven by a fake in tests instead of a
/// real Valkey connection. A generic bound (not `dyn`) at every call site,
/// matching `core/svc_process/src/spine.rs`'s `StreamReader` precedent.
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait RelaySource: Send {
    /// Blocks up to [`BRPOP_TIMEOUT_SECS`] for the next queued payload;
    /// `Ok(None)` on timeout (empty queue), not an error.
    async fn brpop_one(&mut self) -> Result<Option<String>, String>;
}

impl RelaySource for redis::aio::ConnectionManager {
    async fn brpop_one(&mut self) -> Result<Option<String>, String> {
        // `redis::AsyncCommands::brpop` (also named `brpop`, brought into
        // scope by the `use` above) would otherwise be ambiguous with this
        // trait method of the same name -- the explicit `AsyncCommands::`
        // qualification disambiguates rather than renaming the import.
        let result: Option<(String, String)> =
            redis::AsyncCommands::brpop(self, TWITCH_OUTBOUND_QUEUE_KEY, BRPOP_TIMEOUT_SECS)
                .await
                .map_err(|e| e.to_string())?;
        Ok(result.map(|(_key, value)| value))
    }
}

/// Why a chat send did not happen: the credential could not be produced
/// (typed, nothing was sent) or the IRC send itself failed.
#[derive(Debug, thiserror::Error)]
pub enum IrcSendError {
    /// No credential for this community/channel; see [`CredError`].
    #[error("credential: {0}")]
    Credential(#[from] CredError),
    /// The IRC connection/send failed.
    #[error("{0}")]
    Send(#[from] TwitchError),
}

/// Abstraction over sending one chat message to a Twitch channel -- lets
/// [`drain_loop`] be tested without a live Twitch IRC connection.
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait IrcOutbound: Send + Sync {
    /// Resolves the credential for `community_id` + `channel` (fresh, per
    /// call), connects, registers, `PRIVMSG`s `channel`, and `QUIT`s (Twitch
    /// chat sends never hold a persistent connection).
    async fn send(
        &self,
        community_id: Option<i64>,
        channel: &str,
        text: &DetokenizedText,
    ) -> Result<(), IrcSendError>;
}

/// The production [`IrcOutbound`]: per message, asks its
/// [`TwitchCredentialSource`] for the community's identity, builds a fresh
/// `IrcConfig` and sends via `TwitchIrcSender` -- one connection per
/// message, matching Twitch chat's real semantics.
pub struct RealIrcOutbound<C: TwitchCredentialSource> {
    endpoint: TwitchIrcEndpoint,
    creds: C,
}

impl<C: TwitchCredentialSource> RealIrcOutbound<C> {
    #[must_use]
    pub fn new(endpoint: TwitchIrcEndpoint, creds: C) -> Self {
        Self { endpoint, creds }
    }
}

impl<C: TwitchCredentialSource> IrcOutbound for RealIrcOutbound<C> {
    async fn send(
        &self,
        community_id: Option<i64>,
        channel: &str,
        text: &DetokenizedText,
    ) -> Result<(), IrcSendError> {
        let cred = self.creds.credential(community_id, channel).await?;
        let cfg = crate::ingest::twitch::irc_config(
            &self.endpoint.host,
            self.endpoint.port,
            &cred.nick,
            channel,
            cred.oauth_token.expose(),
            self.endpoint.use_tls,
        );
        Ok(TwitchIrcSender::new(cfg).send(text.as_str()).await?)
    }
}

/// Integrity gate run on every raw entry before it is acted on: refuses
/// (typed [`SenderError::Rejected`], counted + WARN via
/// [`crate::outbound_pin::record_refusal`]) an entry whose `pin_sig` is
/// missing/invalid, so a queue writer other than `svc_action` cannot inject a
/// destination. A refused entry has already been popped, so it is never
/// retried -- it is dropped (Discord: negatively acked) and counted.
fn check_stamp(
    raw: &str,
    platform: &'static str,
    op: &'static str,
    pin: Option<&PinKeys>,
) -> Result<VerifiedStamp, SenderError> {
    crate::outbound_pin::verify_stamp(raw, platform, pin).map_err(|e| {
        crate::outbound_pin::record_refusal(platform, op, e);
        SenderError::Rejected {
            platform,
            op,
            reason: e.reason(),
        }
    })
}

/// Single-use enforcement for signed outbound entries.
///
/// The Valkey ACL lets any app client write the queue and a stamp is valid for
/// its whole TTL, so a captured entry could be replayed. After the stamp
/// verifies, the drain atomically claims the stamp in Valkey before executing.
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait ReplayGuard: Send + Sync {
    /// Atomically records `key` for `ttl_ms`: `Ok(true)` the first time it is
    /// seen (execute), `Ok(false)` if it was already claimed (replay -- drop).
    /// `Err` means the check could not be made; callers must NOT execute.
    async fn claim(&self, key: &str, ttl_ms: u64) -> Result<bool, String>;
}

/// The production [`ReplayGuard`]: `SET <key> 1 NX PX <ttl>` on the existing
/// Valkey (a connection that is not the one blocked in `BRPOP`).
pub struct ValkeyReplayGuard {
    conn: redis::aio::ConnectionManager,
}

impl ValkeyReplayGuard {
    /// Wraps a dedicated (non-blocking) connection.
    #[must_use]
    pub fn new(conn: redis::aio::ConnectionManager) -> Self {
        Self { conn }
    }
}

impl ReplayGuard for ValkeyReplayGuard {
    async fn claim(&self, key: &str, ttl_ms: u64) -> Result<bool, String> {
        let mut conn = self.conn.clone();
        // `SET .. NX` replies `OK` when it set the key and a nil when it
        // already existed -- the single atomic test-and-set.
        let reply: Option<String> = redis::cmd("SET")
            .arg(key)
            .arg(1)
            .arg("NX")
            .arg("PX")
            .arg(ttl_ms)
            .query_async(&mut conn)
            .await
            .map_err(|e| e.to_string())?;
        Ok(reply.is_some())
    }
}

/// Claims `stamp` once. First sight -> `Ok`. A replay is counted
/// (`pin_replayed`) and refused with [`SenderError::Rejected`]. If Valkey
/// cannot answer the entry is NOT executed (fail closed): a typed
/// [`SenderError::Failed`] is returned and counted -- a Valkey outage never
/// lets an unchecked replay through. Like every other post-pop failure the
/// entry is dropped, not re-queued (best-effort relay); a waiting Discord
/// producer sees `failed` and may resend under a fresh `op_id`.
async fn claim_once<G: ReplayGuard>(
    guard: &G,
    platform: &'static str,
    op: &'static str,
    stamp: &VerifiedStamp,
) -> Result<(), SenderError> {
    let key = crate::outbound_pin::replay_key(platform, stamp);
    match guard.claim(&key, stamp.ttl_ms).await {
        Ok(true) => Ok(()),
        Ok(false) => {
            crate::outbound_pin::record_replay(platform, op);
            Err(SenderError::Rejected {
                platform,
                op,
                reason: "pin_replayed",
            })
        }
        Err(_) => {
            // The transport error text is dropped on purpose: it adds nothing
            // an operator needs and must never carry request material.
            crate::outbound_pin::record_replay_check_failed(platform, op);
            Err(SenderError::Failed {
                platform,
                op,
                message: "replay check unavailable".to_string(),
            })
        }
    }
}

/// Runs the `BRPOP`/parse/send loop until `shutdown` resolves. Split out
/// from [`run`] (which builds the real Valkey connection and IRC sender)
/// so this control flow is exercised in tests against fakes -- no live
/// Valkey, no live Twitch account. A malformed queue entry is logged and
/// dropped (never panics, never blocks the loop on one bad message); a
/// send failure is logged and dropped too -- this is a best-effort relay,
/// not an at-least-once delivery guarantee (S5.5 makes no stronger
/// promise: a bundle-initiated chat reply is not spine-durable data).
///
/// Queue-read-error retry discipline uses the same shared [`Backoff`] as
/// the platform reconnect loops (`ingest::discord`/`ingest::twitch`, see
/// `Backoff`'s own doc comment): the delay grows (exponential, jittered,
/// capped at 30s) on every consecutive `Err`, and `reset()`s only once a
/// poll actually succeeds (`Ok(Some(_))` or `Ok(None)` -- either proves
/// the Valkey round-trip is healthy) -- never a fixed 2s retry that never
/// escalates against a genuinely down connection.
#[cfg(test)]
async fn drain_loop<Q, S>(
    queue: Q,
    sender: &S,
    pin: Option<&PinKeys>,
    backoff: Backoff,
    shutdown: oneshot::Receiver<()>,
) where
    Q: RelaySource,
    S: IrcOutbound,
{
    drain_loop_with(
        queue,
        sender,
        None::<&NoExecutor>,
        &tests::MemoryReplayGuard::default(),
        &Egress::disabled(),
        pin,
        backoff,
        shutdown,
    )
    .await;
}

/// [`drain_loop`] with an optional generic-executor backend (P6a T6d-1).
///
/// `exec = Some(_)`: every entry that passes the stamp gate (HMAC + expiry,
/// FIRST) is executed through [`ActionExecutor`] -- the template-verified,
/// host-slot-substituted, `GuardedClient`-only path -- and `sender` is never
/// consulted. `exec = None`: the legacy per-platform sender, unchanged.
/// Retry/ack semantics are identical either way: a refused or failed entry
/// is already popped, logged PII-free and dropped, never re-queued.
#[allow(clippy::too_many_arguments)]
async fn drain_loop_with<Q, S, X, G>(
    queue: Q,
    sender: &S,
    exec: Option<&X>,
    guard: &G,
    egress: &Egress,
    pin: Option<&PinKeys>,
    backoff: Backoff,
    shutdown: oneshot::Receiver<()>,
) where
    Q: RelaySource,
    S: IrcOutbound,
    X: ActionExecutor,
    G: ReplayGuard,
{
    drain_platform_loop(
        "twitch", queue, sender, exec, guard, egress, pin, backoff, shutdown,
    )
    .await;
}

/// The list-drain loop for one IRC-style platform queue. `platform` is the
/// stamp/replay/metric label; a platform other than `twitch` has no legacy
/// sender, so it is only ever started with `exec = Some(_)` (the generic
/// executor -- see [`run_youtube_exec`]).
#[allow(clippy::too_many_arguments)]
async fn drain_platform_loop<Q, S, X, G>(
    platform: &'static str,
    queue: Q,
    sender: &S,
    exec: Option<&X>,
    guard: &G,
    egress: &Egress,
    pin: Option<&PinKeys>,
    backoff: Backoff,
    shutdown: oneshot::Receiver<()>,
) where
    Q: RelaySource,
    S: IrcOutbound,
    X: ActionExecutor,
    G: ReplayGuard,
{
    drain_platform_loop_acked(
        platform,
        queue,
        sender,
        exec,
        guard,
        egress,
        None::<&NoAck>,
        pin,
        backoff,
        shutdown,
    )
    .await;
}

/// A [`DrainControl`] that answers nobody: the fire-and-forget drains (legacy Twitch IRC)
/// have no waiting producer to settle.
struct NoAck;

impl DrainControl for NoAck {
    async fn mark_ready(&self) -> Result<(), String> {
        Ok(())
    }

    async fn clear_ready(&self) -> Result<(), String> {
        Ok(())
    }

    async fn post_ack(&self, _op_id: &str, _payload: &str) -> Result<(), String> {
        Ok(())
    }
}

/// [`drain_platform_loop`] that also settles a waiting producer: an entry carrying an `op_id`
/// gets its outcome posted through `ctl` (the same ack channel the Discord drain uses), so the
/// producer's confirmed-op path works for Twitch / YouTube too. An entry whose stamp does not
/// verify (including one past its expiry), or that is a replay, is dropped with NO ack (acking
/// would let an unauthenticated writer settle another producer's `op_id`).
#[allow(clippy::too_many_arguments)]
async fn drain_platform_loop_acked<Q, S, X, G, C>(
    platform: &'static str,
    mut queue: Q,
    sender: &S,
    exec: Option<&X>,
    guard: &G,
    egress: &Egress,
    ctl: Option<&C>,
    pin: Option<&PinKeys>,
    mut backoff: Backoff,
    mut shutdown: oneshot::Receiver<()>,
) where
    Q: RelaySource,
    S: IrcOutbound,
    X: ActionExecutor,
    G: ReplayGuard,
    C: DrainControl,
{
    loop {
        tokio::select! {
            _ = &mut shutdown => return,
            popped = queue.brpop_one() => match popped {
                Ok(Some(raw)) => {
                    backoff.reset();
                    match parse_outbound_entry(&raw, platform) {
                        Ok(OutboundEntry { action, community_id, op_id, .. }) => {
                            // `exp_ms` is the stamp expiry here: `check_stamp` refuses (and
                            // counts) a stamp past it, so no separate deadline check runs.
                            let Ok(stamp) = check_stamp(&raw, platform, action.op(), pin) else {
                                continue;
                            };
                            // Single-use: claim the stamp BEFORE any send so a
                            // replayed (or unverifiable-by-outage) entry never runs.
                            let claim = claim_once(guard, platform, action.op(), &stamp).await;
                            if matches!(claim, Err(SenderError::Rejected { .. })) {
                                continue;
                            }
                            let result = match claim {
                                Err(e) => Err(e),
                                // PII boundary: detokenize immediately before the send.
                                Ok(()) => {
                                    let prepared = egress.prepare(platform, action.clone()).await;
                                    match exec {
                                        Some(x) => x.execute_action(platform, community_id, &prepared).await,
                                        None if platform != "twitch" => Err(SenderError::Unsupported {
                                            platform: platform.to_string(),
                                            op: action.op(),
                                        }),
                                        None => {
                                            let twitch = TwitchSender::new(sender, community_id);
                                            route("twitch", &prepared, &twitch, &DiscordSender::unconfigured()).await.map(|()| None)
                                        }
                                    }
                                }
                            };
                            if let Err(err) = &result {
                                match err {
                                    // A new op with no implementation yet is an
                                    // actionable failure, not degraded service.
                                    SenderError::Unsupported { .. } => tracing::error!(platform, op = action.op(), error = %err, "outbound op unsupported, dropping"),
                                    SenderError::Failed { .. } => tracing::warn!(platform, op = action.op(), error = %err, "outbound relay send failed"),
                                    SenderError::Rejected { .. } => tracing::warn!(platform, op = action.op(), error = %err, "outbound op refused by policy"),
                                }
                            }
                            if let (Some(ctl), Some(op_id)) = (ctl, &op_id) {
                                if let Err(err) = ctl.post_ack(op_id, &ack_payload(&result)).await {
                                    tracing::error!(platform, op = action.op(), error = %err, "failed to post outbound op result; the producer will time out");
                                }
                            }
                        }
                        Err(err) => {
                            tracing::warn!(platform, error = %err, "malformed outbound relay payload, dropping");
                        }
                    }
                }
                Ok(None) => {
                    // BRPOP timeout, empty queue -- a successful round-trip
                    // with nothing to do, not a failure; loop and re-check
                    // shutdown. Still resets the backoff (see doc comment).
                    backoff.reset();
                }
                Err(err) => {
                    tracing::warn!(platform, error = %err, "outbound relay queue read error, retrying");
                    let delay = backoff.delay();
                    tokio::select! {
                        _ = &mut shutdown => return,
                        () = tokio::time::sleep(delay) => {}
                    }
                }
            }
        }
    }
}

/// Connects the real Valkey list connection and builds the real
/// [`RealIrcOutbound`] sender, then runs [`drain_loop`] until `shutdown`
/// resolves. This is the function `crate::lib::try_start_twitch_outbound`
/// spawns as its own background task.
pub async fn run<C: TwitchCredentialSource>(
    spine_cfg: &penguin_spine::SpineConfig,
    endpoint: TwitchIrcEndpoint,
    creds: C,
    pin: Option<PinKeys>,
    shutdown: oneshot::Receiver<()>,
) -> Result<(), redis::RedisError> {
    let client = build_redis_client(spine_cfg)?;
    let conn = connect_with_response_timeout(&client, BLOCKING_CONN_RESPONSE_TIMEOUT).await?;
    let guard = ValkeyReplayGuard::new(
        connect_with_response_timeout(&client, CONTROL_CONN_RESPONSE_TIMEOUT).await?,
    );
    let sender = RealIrcOutbound::new(endpoint, creds);
    let backoff = Backoff::new(Duration::from_secs(30));
    drain_loop_with(
        conn,
        &sender,
        None::<&NoExecutor>,
        &guard,
        &crate::egress::installed(),
        pin.as_ref(),
        backoff,
        shutdown,
    )
    .await;
    Ok(())
}

/// The twitch drain backed by the generic executor (P6a T6d-1): no IRC
/// identity, no per-process credential -- every send resolves its own
/// community's slots through `exec`. Started instead of [`run`] once a
/// pinned-component [`crate::connector::sender_exec::TemplateBuilder`] is
/// available to build `exec` (see the crate README "T6d-1 status").
///
/// # Errors
/// [`redis::RedisError`] if the Valkey connect fails.
pub async fn run_exec<X: ActionExecutor>(
    spine_cfg: &penguin_spine::SpineConfig,
    exec: &X,
    pin: Option<PinKeys>,
    shutdown: oneshot::Receiver<()>,
) -> Result<(), redis::RedisError> {
    let client = build_redis_client(spine_cfg)?;
    let conn = connect_with_response_timeout(&client, BLOCKING_CONN_RESPONSE_TIMEOUT).await?;
    let guard = ValkeyReplayGuard::new(
        connect_with_response_timeout(&client, CONTROL_CONN_RESPONSE_TIMEOUT).await?,
    );
    let ctl = ValkeyDrainControl::new(
        connect_with_response_timeout(&client, CONTROL_CONN_RESPONSE_TIMEOUT).await?,
    );
    let backoff = Backoff::new(Duration::from_secs(30));
    drain_platform_loop_acked(
        "twitch",
        conn,
        &NoIrc,
        Some(exec),
        &guard,
        &crate::egress::installed(),
        Some(&ctl),
        pin.as_ref(),
        backoff,
        shutdown,
    )
    .await;
    Ok(())
}

/// The queue key the YouTube outbound drain reads: the same
/// `waddles:transport:irc:{provider}:outbound` shape
/// `svc_action::capabilities::outbound_relay_queue_key("youtube")` produces
/// (duplicated, not imported: separate crates). YouTube has NO legacy sender;
/// this drain exists only in executor mode.
pub const YOUTUBE_OUTBOUND_QUEUE_KEY: &str = "waddles:transport:irc:youtube:outbound";

/// The YouTube drain: same stamp gate, single-use claim and generic executor
/// as [`run_exec`], reading [`YOUTUBE_OUTBOUND_QUEUE_KEY`]. There is no legacy
/// YouTube path to fall back to, so it is only started with the executor.
///
/// # Errors
/// [`redis::RedisError`] if the Valkey connect fails.
pub async fn run_youtube_exec<X: ActionExecutor>(
    spine_cfg: &penguin_spine::SpineConfig,
    exec: &X,
    pin: Option<PinKeys>,
    shutdown: oneshot::Receiver<()>,
) -> Result<(), redis::RedisError> {
    let client = build_redis_client(spine_cfg)?;
    let queue = ListQueue {
        conn: connect_with_response_timeout(&client, BLOCKING_CONN_RESPONSE_TIMEOUT).await?,
        key: YOUTUBE_OUTBOUND_QUEUE_KEY,
    };
    let guard = ValkeyReplayGuard::new(
        connect_with_response_timeout(&client, CONTROL_CONN_RESPONSE_TIMEOUT).await?,
    );
    let ctl = ValkeyDrainControl::new(
        connect_with_response_timeout(&client, CONTROL_CONN_RESPONSE_TIMEOUT).await?,
    );
    drain_platform_loop_acked(
        "youtube",
        queue,
        &NoIrc,
        Some(exec),
        &guard,
        &crate::egress::installed(),
        Some(&ctl),
        pin.as_ref(),
        Backoff::new(Duration::from_secs(30)),
        shutdown,
    )
    .await;
    Ok(())
}

/// The queue key the Kick outbound drain reads: the same
/// `waddles:transport:irc:{provider}:outbound` shape
/// `svc_action::capabilities::outbound_relay_queue_key("kick")` produces (duplicated, not
/// imported: separate crates). Kick has NO legacy sender; this drain exists only in executor mode.
pub const KICK_OUTBOUND_QUEUE_KEY: &str = "waddles:transport:irc:kick:outbound";

/// The Kick drain: same stamp gate, single-use claim and generic executor as [`run_exec`],
/// reading [`KICK_OUTBOUND_QUEUE_KEY`]. There is no legacy Kick path to fall back to, so it is
/// only started with the executor.
///
/// # Errors
/// [`redis::RedisError`] if the Valkey connect fails.
pub async fn run_kick_exec<X: ActionExecutor>(
    spine_cfg: &penguin_spine::SpineConfig,
    exec: &X,
    pin: Option<PinKeys>,
    shutdown: oneshot::Receiver<()>,
) -> Result<(), redis::RedisError> {
    let client = build_redis_client(spine_cfg)?;
    let queue = ListQueue {
        conn: connect_with_response_timeout(&client, BLOCKING_CONN_RESPONSE_TIMEOUT).await?,
        key: KICK_OUTBOUND_QUEUE_KEY,
    };
    let guard = ValkeyReplayGuard::new(
        connect_with_response_timeout(&client, CONTROL_CONN_RESPONSE_TIMEOUT).await?,
    );
    let ctl = ValkeyDrainControl::new(
        connect_with_response_timeout(&client, CONTROL_CONN_RESPONSE_TIMEOUT).await?,
    );
    drain_platform_loop_acked(
        "kick",
        queue,
        &NoIrc,
        Some(exec),
        &guard,
        &crate::egress::installed(),
        Some(&ctl),
        pin.as_ref(),
        Backoff::new(Duration::from_secs(30)),
        shutdown,
    )
    .await;
    Ok(())
}

/// The [`IrcOutbound`] of an executor-backed drain: never consulted (the
/// executor replaces the legacy sender); if it ever were, it fails loud.
struct NoIrc;

impl IrcOutbound for NoIrc {
    async fn send(
        &self,
        _community_id: Option<i64>,
        _channel: &str,
        _text: &DetokenizedText,
    ) -> Result<(), IrcSendError> {
        Err(IrcSendError::Credential(CredError::NoBinding))
    }
}

/// The queue key the Discord outbound drain reads -- byte-identical to
/// `core/svc_action/src/capabilities.rs::outbound_relay_queue_key("discord")`
/// (duplicated, not imported: separate crates, same rationale as
/// [`TWITCH_OUTBOUND_QUEUE_KEY`]).
pub const DISCORD_OUTBOUND_QUEUE_KEY: &str = "waddles:transport:irc:discord:outbound";

/// Valkey key this drain keeps alive (short TTL, refreshed by a heartbeat)
/// for as long as it is actually running with a working bot token.
/// `svc_action` refuses to accept a Discord `chat.delete`/`dm.send` while the
/// key is absent, so a misconfigured deployment (flag off, token missing,
/// spine config missing, drain crashed) fails loudly at the producer instead
/// of queueing ops nothing will ever drain. Byte-identical to
/// `svc_action::capabilities::DISCORD_DRAIN_READY_KEY` (duplicated, not
/// imported -- separate crates).
pub const DISCORD_DRAIN_READY_KEY: &str = "waddles:transport:discord:drain-ready";

/// Prefix of the per-op result key (`<prefix><op_id>`) the drain posts an
/// op's outcome under, for a producer waiting on it. Byte-identical to
/// `svc_action::capabilities::DISCORD_OP_ACK_KEY_PREFIX`.
pub const DISCORD_OP_ACK_KEY_PREFIX: &str = "waddles:transport:discord:ack:";

/// TTL of the drain-ready key. A multiple of [`DRAIN_HEARTBEAT_INTERVAL`] so
/// a couple of missed beats do not flap readiness, short enough that a dead
/// drain is noticed by producers within seconds.
const DRAIN_READY_TTL_SECS: u64 = 20;

/// How often the drain re-asserts the drain-ready key.
const DRAIN_HEARTBEAT_INTERVAL: Duration = Duration::from_secs(5);

/// TTL of a posted op result: long enough for a polling producer to read
/// it, short enough that an abandoned result never lingers.
const ACK_TTL_SECS: u64 = 30;

/// The drain's write-side channel back to producers: the readiness heartbeat
/// and the per-op result handshake. A trait so the drain loop is testable
/// without a live Valkey.
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait DrainControl: Send + Sync {
    /// Asserts "a Discord drain is running" ([`DISCORD_DRAIN_READY_KEY`]).
    async fn mark_ready(&self) -> Result<(), String>;
    /// Retracts readiness on a clean shutdown so producers stop waiting on a
    /// drain that is going away.
    async fn clear_ready(&self) -> Result<(), String>;
    /// Publishes `payload` (the JSON ack, see [`ack_payload`]) for the
    /// producer waiting on `op_id`.
    async fn post_ack(&self, op_id: &str, payload: &str) -> Result<(), String>;
}

/// The production [`DrainControl`] over a Valkey connection that is **not**
/// the one blocked in `BRPOP` (a multiplexed connection stalls every other
/// command behind a blocking pop, so the drain opens a second connection for
/// these writes).
pub struct ValkeyDrainControl {
    conn: redis::aio::ConnectionManager,
}

impl ValkeyDrainControl {
    /// Wraps a dedicated control connection.
    #[must_use]
    pub fn new(conn: redis::aio::ConnectionManager) -> Self {
        Self { conn }
    }
}

impl DrainControl for ValkeyDrainControl {
    async fn mark_ready(&self) -> Result<(), String> {
        let mut conn = self.conn.clone();
        redis::AsyncCommands::set_ex::<_, _, ()>(
            &mut conn,
            DISCORD_DRAIN_READY_KEY,
            "1",
            DRAIN_READY_TTL_SECS,
        )
        .await
        .map_err(|e| e.to_string())
    }

    async fn clear_ready(&self) -> Result<(), String> {
        let mut conn = self.conn.clone();
        redis::AsyncCommands::del::<_, ()>(&mut conn, DISCORD_DRAIN_READY_KEY)
            .await
            .map_err(|e| e.to_string())
    }

    async fn post_ack(&self, op_id: &str, payload: &str) -> Result<(), String> {
        let mut conn = self.conn.clone();
        let key = format!("{DISCORD_OP_ACK_KEY_PREFIX}{op_id}");
        redis::AsyncCommands::set_ex::<_, _, ()>(&mut conn, key, payload, ACK_TTL_SECS)
            .await
            .map_err(|e| e.to_string())
    }
}

/// A Valkey list queue with a caller-chosen key (the Twitch
/// [`RelaySource`] impl on the bare connection hardcodes its key).
pub struct ListQueue {
    conn: redis::aio::ConnectionManager,
    key: &'static str,
}

impl RelaySource for ListQueue {
    async fn brpop_one(&mut self) -> Result<Option<String>, String> {
        let result: Option<(String, String)> =
            redis::AsyncCommands::brpop(&mut self.conn, self.key, BRPOP_TIMEOUT_SECS)
                .await
                .map_err(|e| e.to_string())?;
        Ok(result.map(|(_key, value)| value))
    }
}

/// The JSON a waiting producer reads back: `{"ok":true}` on success, else
/// `{"ok":false,"code":"<stable code>"}` ([`SenderError::ack_code`]). Never
/// carries message content, user ids or error text.
fn ack_payload(result: &Result<Option<String>, SenderError>) -> String {
    match result {
        Ok(None) => serde_json::json!({"ok": true}),
        // A create-style op's platform id (a snowflake the executor parsed out
        // of Discord's reply): the producer needs it to address later updates.
        // A bulk read's JSON object result (`role.list`): returned to the producer as `data`.
        Ok(Some(json)) if json.starts_with('{') || json.starts_with('[') => {
            match serde_json::from_str::<serde_json::Value>(json) {
                Ok(data) => serde_json::json!({"ok": true, "data": data}),
                Err(_) => serde_json::json!({"ok": false, "code": "bad_result"}),
            }
        }
        Ok(Some(id)) => serde_json::json!({"ok": true, "id": id}),
        Err(err) => serde_json::json!({"ok": false, "code": err.ack_code()}),
    }
    .to_string()
}

/// Wall-clock now, epoch milliseconds. A clock before the epoch reads as `0`,
/// which can only make an entry look *not yet expired* (it is then executed
/// rather than dropped) -- the fail-open direction for a best-effort deadline.
fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

/// Executes one raw Discord queue entry and, when the producer is waiting
/// (`op_id` present), posts the outcome.
///
/// A malformed entry is logged and dropped. An entry already past its
/// `exp_ms` deadline is dropped *without executing*: the producer stopped
/// waiting and reported failure, so performing the op now would contradict
/// that report (a late DM after "please resend" would be a duplicate). Logs
/// carry platform/op/code only -- never content, ids or error bodies.
async fn handle_discord_entry<C: DrainControl, X: ActionExecutor, G: ReplayGuard>(
    raw: &str,
    sender: &DiscordSender<'_>,
    exec: Option<&X>,
    ctl: &C,
    guard: &G,
    egress: &Egress,
    pin: Option<&PinKeys>,
) {
    let entry = match parse_outbound_entry(raw, "discord") {
        Ok(entry) => entry,
        Err(err) => {
            tracing::warn!(platform = "discord", error = %err, "malformed outbound relay payload, dropping");
            return;
        }
    };
    let op = entry.action.op();
    if entry.exp_ms.is_some_and(|exp| now_ms() > exp) {
        tracing::warn!(
            platform = "discord",
            op,
            "outbound op already past its deadline (producer stopped waiting); dropping unexecuted"
        );
        return;
    }
    // An entry whose stamp does not verify is NOT authentic: it is dropped
    // (counted + WARN by `check_stamp`, already popped so never retried) and
    // gets NO ack -- acking would let an unauthenticated queue writer settle
    // another producer's `op_id` by reusing it.
    let Ok(stamp) = check_stamp(raw, "discord", op, pin) else {
        return;
    };
    // Single-use: a replayed stamp is dropped with NO ack (the original
    // execution already answered the producer; a second ack would overwrite
    // its result). An unavailable check does not execute and acks `failed`.
    let claim = claim_once(guard, "discord", op, &stamp).await;
    if matches!(claim, Err(SenderError::Rejected { .. })) {
        return;
    }
    // The stamp gate above ran first; the executor (when configured) then
    // re-checks the pin, verifies the connector's template and sends through
    // the guarded client only. A refused/failed op is acked and dropped
    // exactly as on the legacy path -- never re-queued.
    let result = match claim {
        Err(e) => Err(e),
        // The env-token sender is the DEFAULT tenant's application only: a
        // non-default tenant's op is refused before anything is sent.
        Ok(()) if exec.is_none() && !crate::tenant_discord::is_default_tenant(entry.tenant_id) => {
            crate::tenant_discord::record_app_missing(
                crate::tenant_discord::SURFACE_OUTBOUND_ENV,
                op,
                entry.tenant_id,
            );
            Err(SenderError::Rejected {
                platform: "discord",
                op,
                reason: crate::tenant_discord::TENANT_DISCORD_APP_MISSING,
            })
        }
        // PII boundary: detokenize immediately before the send.
        Ok(()) => {
            let prepared = egress.prepare("discord", entry.action.clone()).await;
            match exec {
                Some(x) => {
                    match x
                        .execute_action("discord", entry.community_id, &prepared)
                        .await
                    {
                        // No binding for a non-default tenant: it has no app of its own.
                        Err(SenderError::Rejected {
                            reason: "no_connection",
                            ..
                        }) if !crate::tenant_discord::is_default_tenant(entry.tenant_id) => {
                            crate::tenant_discord::record_app_missing(
                                crate::tenant_discord::SURFACE_OUTBOUND_BINDING,
                                op,
                                entry.tenant_id,
                            );
                            Err(SenderError::Rejected {
                                platform: "discord",
                                op,
                                reason: crate::tenant_discord::TENANT_DISCORD_APP_MISSING,
                            })
                        }
                        other => other,
                    }
                }
                None => dispatch_to(sender, &prepared).await.map(|()| None),
            }
        }
    };
    match &result {
        Ok(_) => {}
        Err(err @ SenderError::Unsupported { .. }) => {
            tracing::error!(platform = "discord", op, error = %err, "outbound op unsupported, dropping");
        }
        Err(err @ SenderError::Failed { .. }) => {
            tracing::warn!(platform = "discord", op, error = %err, "outbound relay send failed");
        }
        Err(err @ SenderError::Rejected { .. }) => {
            tracing::warn!(platform = "discord", op, error = %err, "outbound op refused by policy");
        }
    }
    if let Some(op_id) = &entry.op_id {
        if let Err(err) = ctl.post_ack(op_id, &ack_payload(&result)).await {
            tracing::error!(platform = "discord", op, error = %err, "failed to post outbound op result; the producer will time out");
        }
    }
}

/// Discord twin of [`drain_loop`]: `BRPOP`s the Discord outbound queue,
/// parses each entry and executes it via [`DiscordSender`] (REST), then
/// answers a waiting producer through [`DrainControl`]. Same discipline: a
/// malformed or failed entry is logged (PII-free: platform/op/error only)
/// and dropped, never panics, never stalls the loop; queue-read errors back
/// off exponentially.
#[cfg(test)]
async fn discord_drain_loop<Q, C>(
    queue: Q,
    sender: &DiscordSender<'_>,
    ctl: &C,
    pin: Option<&PinKeys>,
    backoff: Backoff,
    shutdown: oneshot::Receiver<()>,
) where
    Q: RelaySource,
    C: DrainControl,
{
    discord_drain_loop_with(
        queue,
        sender,
        None::<&NoExecutor>,
        ctl,
        &tests::MemoryReplayGuard::default(),
        &Egress::disabled(),
        pin,
        backoff,
        shutdown,
    )
    .await;
}

/// [`discord_drain_loop`] with an optional generic-executor backend; see
/// [`drain_loop_with`].
#[allow(clippy::too_many_arguments)] // queue, sender, executor, ack channel, replay guard, pin keys, backoff, shutdown: each is a distinct injected seam
async fn discord_drain_loop_with<Q, C, X, G>(
    mut queue: Q,
    sender: &DiscordSender<'_>,
    exec: Option<&X>,
    ctl: &C,
    guard: &G,
    egress: &Egress,
    pin: Option<&PinKeys>,
    mut backoff: Backoff,
    mut shutdown: oneshot::Receiver<()>,
) where
    Q: RelaySource,
    C: DrainControl,
    X: ActionExecutor,
    G: ReplayGuard,
{
    loop {
        tokio::select! {
            _ = &mut shutdown => return,
            popped = queue.brpop_one() => match popped {
                Ok(Some(raw)) => {
                    backoff.reset();
                    handle_discord_entry(&raw, sender, exec, ctl, guard, egress, pin).await;
                }
                Ok(None) => backoff.reset(),
                Err(err) => {
                    tracing::warn!(platform = "discord", error = %err, "outbound relay queue read error, retrying");
                    let delay = backoff.delay();
                    tokio::select! {
                        _ = &mut shutdown => return,
                        () = tokio::time::sleep(delay) => {}
                    }
                }
            }
        }
    }
}

/// Re-asserts the drain-ready key every [`DRAIN_HEARTBEAT_INTERVAL`] for as
/// long as it is polled. A failed beat is logged loudly -- producers will
/// refuse Discord ops until a later beat succeeds -- and never stops the
/// heartbeat or the drain.
async fn heartbeat_loop<C: DrainControl>(ctl: &C) {
    let mut interval = tokio::time::interval(DRAIN_HEARTBEAT_INTERVAL);
    loop {
        interval.tick().await;
        if let Err(err) = ctl.mark_ready().await {
            tracing::warn!(platform = "discord", error = %err, "failed to refresh the discord drain-ready key; producers will refuse discord ops until it recovers");
        }
    }
}

/// Connects the Valkey list connection (plus a second, control connection
/// for the heartbeat/acks) and runs [`discord_drain_loop`] with the
/// bot-token REST client until `shutdown` resolves, advertising readiness
/// the whole time. Spawned by `crate::lib::try_start_discord_outbound`.
///
/// # Errors
/// [`redis::RedisError`] if a Valkey connect fails.
pub async fn run_discord(
    spine_cfg: &penguin_spine::SpineConfig,
    rest: &crate::discord_rest::DiscordRestClient,
    pin: Option<PinKeys>,
    shutdown: oneshot::Receiver<()>,
) -> Result<(), redis::RedisError> {
    let sender = DiscordSender::new(rest);
    run_discord_inner(spine_cfg, &sender, None::<&NoExecutor>, pin, shutdown).await
}

/// The Discord drain backed by the generic executor (P6a T6d-1): no bot
/// token in this process -- the executor resolves the community's own
/// credential per send. Started instead of [`run_discord`] once a
/// pinned-component template builder is available (crate README "T6d-1
/// status"). Readiness is advertised exactly as on the legacy path.
///
/// # Errors
/// [`redis::RedisError`] if a Valkey connect fails.
pub async fn run_discord_exec<X: ActionExecutor>(
    spine_cfg: &penguin_spine::SpineConfig,
    exec: &X,
    pin: Option<PinKeys>,
    shutdown: oneshot::Receiver<()>,
) -> Result<(), redis::RedisError> {
    let sender = DiscordSender::unconfigured();
    run_discord_inner(spine_cfg, &sender, Some(exec), pin, shutdown).await
}

async fn run_discord_inner<X: ActionExecutor>(
    spine_cfg: &penguin_spine::SpineConfig,
    sender: &DiscordSender<'_>,
    exec: Option<&X>,
    pin: Option<PinKeys>,
    shutdown: oneshot::Receiver<()>,
) -> Result<(), redis::RedisError> {
    let client = build_redis_client(spine_cfg)?;
    let list_conn = connect_with_response_timeout(&client, BLOCKING_CONN_RESPONSE_TIMEOUT).await?;
    let ctl = ValkeyDrainControl::new(
        connect_with_response_timeout(&client, CONTROL_CONN_RESPONSE_TIMEOUT).await?,
    );
    let guard = ValkeyReplayGuard::new(
        connect_with_response_timeout(&client, CONTROL_CONN_RESPONSE_TIMEOUT).await?,
    );
    let egress = crate::egress::installed();
    let queue = ListQueue {
        conn: list_conn,
        key: DISCORD_OUTBOUND_QUEUE_KEY,
    };
    tracing::info!(
        platform = "discord",
        "discord outbound drain running; advertising readiness"
    );
    tokio::select! {
        () = heartbeat_loop(&ctl) => {}
        () = discord_drain_loop_with(
            queue,
            sender,
            exec,
            &ctl,
            &guard,
            &egress,
            pin.as_ref(),
            Backoff::new(Duration::from_secs(30)),
            shutdown,
        ) => {}
    }
    if let Err(err) = ctl.clear_ready().await {
        tracing::warn!(platform = "discord", error = %err, "failed to retract the discord drain-ready key on shutdown");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    /// A fake queue that yields each scripted `payloads` entry once, then
    /// returns `Err(...)` forever after -- **not** `Ok(None)`/replay-last.
    /// A queue that kept synchronously returning `Ok(...)` (even `Ok(None)`)
    /// forever would starve `drain_loop`'s `tokio::select!`: every branch
    /// (`shutdown`, `queue.brpop_one()`) would resolve instantly on every
    /// poll with no real tokio resource (timer/socket) ever involved, so
    /// the surrounding `async fn`'s generated state machine never actually
    /// returns `Poll::Pending` to the runtime -- nothing preempts it, so
    /// even a wrapping `tokio::time::timeout` never gets a chance to fire
    /// (its own timer never gets polled). This was a real, reproduced
    /// hang/unbounded-memory bug (see `crate::ingest::twitch::tests::
    /// FakeConnector`'s doc comment for the twin bug in the receive-side
    /// fakes). Returning `Err` here forces `drain_loop`'s error branch,
    /// which contains a genuine `tokio::time::sleep` -- a real tokio timer
    /// resource that guarantees a scheduler yield point, letting `shutdown`
    /// win deterministically.
    struct FakeQueue {
        payloads: Vec<Result<Option<String>, String>>,
        idx: AtomicUsize,
    }

    /// Shared test keyring (signing `k1`) -- the drain verifies with it.
    pub(super) const TEST_KEYS: &str = "k1:000102030405060708090a0b0c0d0e0f";

    /// In-memory [`ReplayGuard`] with the same first-sight/replay semantics as
    /// the `SET NX` one; `down` simulates a Valkey outage. Records every
    /// (key, ttl) claim for assertions.
    #[derive(Default)]
    pub(super) struct MemoryReplayGuard {
        seen: Mutex<std::collections::HashSet<String>>,
        claims: Mutex<Vec<(String, u64)>>,
        down: bool,
    }

    impl ReplayGuard for MemoryReplayGuard {
        async fn claim(&self, key: &str, ttl_ms: u64) -> Result<bool, String> {
            if self.down {
                return Err("simulated valkey outage".to_string());
            }
            self.claims.lock().unwrap().push((key.to_string(), ttl_ms));
            Ok(self.seen.lock().unwrap().insert(key.to_string()))
        }
    }

    static OP_SEQ: AtomicUsize = AtomicUsize::new(0);

    fn test_pin_keys() -> PinKeys {
        PinKeys::parse(TEST_KEYS, Some("k1")).unwrap()
    }

    /// Stamps a chat entry the way `svc_action` does so pre-existing tests keep
    /// exercising the drain. A `RAW:` prefix opts out (forged/un-stamped).
    fn stamp_for_test(raw: &str) -> String {
        if let Some(rest) = raw.strip_prefix("RAW:") {
            return rest.to_string();
        }
        let Ok(mut v) = serde_json::from_str::<serde_json::Value>(raw) else {
            return raw.to_string();
        };
        // The producer always stamps a unique op_id; tests that do not care
        // get one (a test of "no op_id" opts out with `RAW:`).
        if v.get("op_id").is_none() && v.get("origin_channel").is_some() {
            v["op_id"] = serde_json::Value::from(format!(
                "test-op-{}",
                OP_SEQ.fetch_add(1, Ordering::SeqCst)
            ));
        }
        let snapshot = v.clone();
        let get = |k: &str| {
            snapshot
                .get(k)
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string()
        };
        let op = if snapshot.get("op").is_some() {
            get("op")
        } else {
            "chat.send".to_string()
        };
        let (channel, origin) = (get("channel"), get("origin_channel"));
        if origin.is_empty() {
            return raw.to_string();
        }
        // Producer always stamps a fresh exp_ms; keep an explicit one (tests
        // of the deadline path).
        if v.get("exp_ms").is_none() {
            v["exp_ms"] = serde_json::Value::from(super::now_ms() + 60_000);
        }
        let exp_ms = v["exp_ms"].as_u64().unwrap_or(0);
        // Platform-less test entries: legacy (no `op`) = twitch, else discord.
        let platform = if snapshot.get("platform").is_some() {
            get("platform")
        } else if snapshot.get("op").is_some() {
            "discord".to_string()
        } else {
            "twitch".to_string()
        };
        let sig = test_pin_keys()
            .sign(&crate::pin_sig::PinFields {
                platform: &platform,
                op: &op,
                channel: &channel,
                origin_channel: &origin,
                message_id: &get("message_id"),
                text: &get("text"),
                user_id: &get("user_id"),
                op_id: &get("op_id"),
                exp_ms,
                community_id: snapshot
                    .get("community_id")
                    .and_then(serde_json::Value::as_i64)
                    .unwrap_or(0),
                tenant_id: snapshot
                    .get("tenant_id")
                    .and_then(serde_json::Value::as_i64)
                    .unwrap_or(0),
            })
            .unwrap();
        v["pin_sig"] = serde_json::Value::String(sig);
        v.to_string()
    }

    impl RelaySource for FakeQueue {
        async fn brpop_one(&mut self) -> Result<Option<String>, String> {
            let i = self.idx.fetch_add(1, Ordering::SeqCst);
            match self.payloads.get(i) {
                Some(result) => result.clone().map(|o| o.map(|r| stamp_for_test(&r))),
                None => Err("simulated queue exhausted".to_string()),
            }
        }
    }

    /// Records every readiness beat and posted ack; `fail_ack` simulates a
    /// Valkey outage on the result write.
    #[derive(Default)]
    struct FakeCtl {
        ready_marks: AtomicUsize,
        cleared: AtomicUsize,
        acks: Mutex<Vec<(String, String)>>,
        fail_ack: bool,
    }

    impl DrainControl for FakeCtl {
        async fn mark_ready(&self) -> Result<(), String> {
            self.ready_marks.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
        async fn clear_ready(&self) -> Result<(), String> {
            self.cleared.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
        async fn post_ack(&self, op_id: &str, payload: &str) -> Result<(), String> {
            if self.fail_ack {
                return Err("simulated valkey outage".to_string());
            }
            self.acks
                .lock()
                .unwrap()
                .push((op_id.to_string(), payload.to_string()));
            Ok(())
        }
    }

    #[derive(Default)]
    struct RecordingSender {
        calls: Mutex<Vec<(String, String)>>,
        communities: Mutex<Vec<Option<i64>>>,
        fail: bool,
        cred_err: Option<CredError>,
    }

    impl IrcOutbound for RecordingSender {
        async fn send(
            &self,
            community_id: Option<i64>,
            channel: &str,
            text: &DetokenizedText,
        ) -> Result<(), IrcSendError> {
            self.communities.lock().unwrap().push(community_id);
            if let Some(e) = self.cred_err {
                return Err(e.into());
            }
            if self.fail {
                return Err(TwitchError::Connection("simulated failure".to_string()).into());
            }
            self.calls
                .lock()
                .unwrap()
                .push((channel.to_string(), text.as_str().to_string()));
            Ok(())
        }
    }

    /// regression: P0 host-pin -- a legacy / un-stamped entry (no
    /// `origin_channel`) is refused by the drain, never sent.
    #[tokio::test]
    async fn unpinned_legacy_payload_is_refused_not_sent() {
        let queue = FakeQueue {
            payloads: vec![Ok(Some("{\"channel\":\"#c\",\"text\":\"hi\"}".to_string()))],
            idx: AtomicUsize::new(0),
        };
        let sender = RecordingSender::default();
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            drain_loop(
                queue,
                &sender,
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert!(sender.calls.lock().unwrap().is_empty());
    }

    /// A pinned versioned envelope (stamped `origin_channel`) sends chat.
    #[tokio::test]
    async fn legacy_and_versioned_chat_send_payloads_both_send() {
        for raw in [
            "{\"v\":1,\"op\":\"chat.send\",\"platform\":\"twitch\",\"channel\":\"#c\",\"text\":\"hi\",\"origin_channel\":\"#c\"}",
        ] {
            let queue = FakeQueue {
                payloads: vec![Ok(Some(raw.to_string()))],
                idx: AtomicUsize::new(0),
            };
            let sender = RecordingSender::default();
            let (_tx, rx) = oneshot::channel();
            let _ = tokio::time::timeout(
                Duration::from_secs(5),
                drain_loop(queue, &sender, Some(&test_pin_keys()), Backoff::new(Duration::from_secs(30)), rx),
            )
            .await;
            assert_eq!(
                sender.calls.lock().unwrap().as_slice(),
                &[("#c".to_string(), "hi".to_string())],
                "{raw}"
            );
        }
    }

    /// New ops are routed to the adapter (Unsupported until implemented) and
    /// never reach the IRC chat sender; an unknown op is dropped loudly.
    #[tokio::test]
    async fn new_and_unknown_ops_never_send_chat() {
        for raw in [
            "{\"v\":1,\"op\":\"chat.delete\",\"channel\":\"#c\",\"message_id\":\"m\"}",
            "{\"v\":1,\"op\":\"dm.send\",\"user_id\":\"u\",\"text\":\"hi\"}",
            "{\"v\":1,\"op\":\"bogus\",\"channel\":\"#c\",\"text\":\"hi\"}",
        ] {
            let queue = FakeQueue {
                payloads: vec![Ok(Some(raw.to_string()))],
                idx: AtomicUsize::new(0),
            };
            let sender = RecordingSender::default();
            let (_tx, rx) = oneshot::channel();
            let _ = tokio::time::timeout(
                Duration::from_secs(5),
                drain_loop(
                    queue,
                    &sender,
                    Some(&test_pin_keys()),
                    Backoff::new(Duration::from_secs(30)),
                    rx,
                ),
            )
            .await;
            assert!(sender.calls.lock().unwrap().is_empty(), "{raw}");
        }
    }

    #[test]
    fn queue_key_matches_svc_action_format() {
        assert_eq!(
            TWITCH_OUTBOUND_QUEUE_KEY,
            "waddles:transport:irc:twitch:outbound"
        );
    }

    #[tokio::test]
    async fn sends_a_popped_message_then_stops_on_shutdown() {
        // No pre-sent shutdown: a *pre-sent* value would race
        // `tokio::select!`'s very first poll against the already-ready
        // `queue.brpop_one()`, and could resolve to `shutdown` before the
        // queue is ever popped at all -- a real, reproduced flake (this
        // exact test observed zero sends). `FakeQueue` returns `Err`
        // (exhausted) once its one scripted payload is consumed, which
        // forces `drain_loop`'s error branch and its genuine
        // `tokio::time::sleep` -- `tokio::time::timeout` bounds the
        // resulting backoff loop from the outside.
        let queue = FakeQueue {
            payloads: vec![Ok(Some(
                "{\"channel\":\"#c\",\"text\":\"hi\",\"origin_channel\":\"#c\"}".to_string(),
            ))],
            idx: AtomicUsize::new(0),
        };
        let sender = RecordingSender::default();
        let (_tx, rx) = oneshot::channel();

        let result = tokio::time::timeout(
            Duration::from_secs(5),
            drain_loop(
                queue,
                &sender,
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert!(
            result.is_err(),
            "drain_loop only returns via shutdown, which this test never sends"
        );
        assert_eq!(
            sender.calls.lock().unwrap().as_slice(),
            &[("#c".to_string(), "hi".to_string())]
        );
    }

    #[tokio::test]
    async fn malformed_payload_is_dropped_without_panicking() {
        let queue = FakeQueue {
            payloads: vec![Ok(Some("not json".to_string())), Ok(None)],
            idx: AtomicUsize::new(0),
        };
        let sender = RecordingSender::default();
        let (tx, rx) = oneshot::channel();
        tx.send(()).unwrap();
        drain_loop(
            queue,
            &sender,
            Some(&test_pin_keys()),
            Backoff::new(Duration::from_secs(30)),
            rx,
        )
        .await;
        assert!(sender.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn send_failure_is_logged_and_does_not_stop_the_loop() {
        let queue = FakeQueue {
            payloads: vec![Ok(Some(
                "{\"channel\":\"#c\",\"text\":\"hi\",\"origin_channel\":\"#c\"}".to_string(),
            ))],
            idx: AtomicUsize::new(0),
        };
        let sender = RecordingSender {
            fail: true,
            ..Default::default()
        };
        let (tx, rx) = oneshot::channel();
        tx.send(()).unwrap();
        // Must return promptly on shutdown despite the send failure, not
        // panic and not hang retrying the same message forever.
        let result = tokio::time::timeout(
            Duration::from_secs(5),
            drain_loop(
                queue,
                &sender,
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert!(result.is_ok());
    }

    #[tokio::test]
    async fn empty_queue_timeout_loops_until_shutdown() {
        let queue = FakeQueue {
            payloads: vec![Ok(None), Ok(None), Ok(None)],
            idx: AtomicUsize::new(0),
        };
        let sender = RecordingSender::default();
        let (tx, rx) = oneshot::channel();
        tx.send(()).unwrap();
        let result = tokio::time::timeout(
            Duration::from_secs(5),
            drain_loop(
                queue,
                &sender,
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert!(result.is_ok());
        assert!(sender.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn queue_read_error_backs_off_then_stops_on_shutdown() {
        let queue = FakeQueue {
            payloads: vec![Err("connection reset".to_string())],
            idx: AtomicUsize::new(0),
        };
        let sender = RecordingSender::default();
        let (tx, rx) = oneshot::channel();
        tx.send(()).unwrap();
        let result = tokio::time::timeout(
            Duration::from_secs(5),
            drain_loop(
                queue,
                &sender,
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert!(result.is_ok(), "must not hang retrying forever");
    }

    /// Proves the queue-read retry loop grows its delay across repeated
    /// `Err`s and resets only once a poll actually succeeds (`Ok(None)`
    /// counts -- an empty queue is still a healthy round-trip), mirroring
    /// `ingest::discord`/`ingest::twitch`'s own regression tests for the
    /// same [`Backoff`] discipline.
    #[tokio::test(start_paused = true)]
    async fn queue_read_backoff_grows_and_resets_after_a_successful_poll() {
        struct TimedQueue {
            results: Vec<Result<Option<String>, String>>,
            idx: AtomicUsize,
            call_times: std::sync::Arc<Mutex<Vec<tokio::time::Instant>>>,
        }

        impl RelaySource for TimedQueue {
            async fn brpop_one(&mut self) -> Result<Option<String>, String> {
                self.call_times
                    .lock()
                    .unwrap()
                    .push(tokio::time::Instant::now());
                let i = self.idx.fetch_add(1, Ordering::SeqCst);
                match self.results.get(i) {
                    Some(r) => r.clone(),
                    None => Err("simulated queue exhausted".to_string()),
                }
            }
        }

        let call_times: std::sync::Arc<Mutex<Vec<tokio::time::Instant>>> =
            std::sync::Arc::new(Mutex::new(Vec::new()));
        let queue = TimedQueue {
            results: vec![
                Err("e1".to_string()),
                Err("e2".to_string()),
                Ok(None),
                Err("e3".to_string()),
            ],
            idx: AtomicUsize::new(0),
            call_times: call_times.clone(),
        };
        let sender = RecordingSender::default();
        let (_tx, rx) = oneshot::channel();

        const SEED: u64 = 0xABCD_EF01_2345_6789;
        let backoff = Backoff::seeded(Duration::from_secs(30), SEED);

        let _ = tokio::time::timeout(
            Duration::from_secs(600),
            drain_loop(queue, &sender, Some(&test_pin_keys()), backoff, rx),
        )
        .await;

        let times = call_times.lock().unwrap();
        assert!(
            times.len() >= 5,
            "expected at least 5 brpop calls, got {}",
            times.len()
        );
        let deltas: Vec<Duration> = times.windows(2).map(|w| w[1] - w[0]).collect();

        // Independently replicate the exact call sequence a correct loop
        // makes: two churn delays (Err, Err), then the successful `Ok(None)`
        // resets the backoff with no delay before the very next call, then
        // one more delay (the post-reset Err) -- same seed, so this
        // reproduces the real sequence bit-for-bit if (and only if) the
        // reset actually fired after the successful poll.
        let mut expected_backoff = Backoff::seeded(Duration::from_secs(30), SEED);
        let expected_churn_1 = expected_backoff.delay();
        let expected_churn_2 = expected_backoff.delay();
        expected_backoff.reset();
        let expected_post_reset = expected_backoff.delay();

        assert_eq!(deltas[0], expected_churn_1);
        assert_eq!(deltas[1], expected_churn_2);
        assert_eq!(
            deltas[2],
            Duration::ZERO,
            "a successful poll (Ok(None)) must not sleep before the next call"
        );
        assert_eq!(
            deltas[3], expected_post_reset,
            "the delay right after the successful poll must reflect a reset backoff, not a continued climb"
        );
        assert!(
            deltas[3] <= Duration::from_secs(1),
            "post-reset delay {:?} must be bounded by the base 1s ceiling, not the pre-reset ~4s ceiling",
            deltas[3]
        );
    }

    #[test]
    fn build_redis_client_rejects_an_invalid_url() {
        let cfg = penguin_spine::SpineConfig {
            valkey_url: "not a valid url".to_string(),
            valkey_username: None,
            valkey_password: None,
            valkey_ca_file: std::path::PathBuf::from("/nonexistent-ca.crt"),
            security_transport_tls: false,
            security_transport_auth: false,
            consumer_id: "test".to_string(),
            stream_maxlen: 100,
            read_count: 1,
            block_ms: 1_000,
            claim_idle_ms: 30_000,
            claim_interval_ms: 15_000,
            stats_interval_ms: 10_000,
            pel_alert: 5_000,
            dlq_maxlen: 100,
            max_deliveries: 5,
            drain_socket_timeout_s: 65,
            relay_block_timeout_s: 30,
        };
        assert!(build_redis_client(&cfg).is_err());
    }

    /// The three cross-crate Valkey names are duplicated, not imported
    /// (`svc_action` and `svc_ingest` share no code): pin the literals here
    /// and in `svc_action::capabilities`'s own test so neither side drifts.
    #[test]
    fn discord_queue_key_matches_svc_action_format() {
        assert_eq!(
            DISCORD_OUTBOUND_QUEUE_KEY,
            "waddles:transport:irc:discord:outbound"
        );
        assert_eq!(
            DISCORD_DRAIN_READY_KEY,
            "waddles:transport:discord:drain-ready"
        );
        assert_eq!(DISCORD_OP_ACK_KEY_PREFIX, "waddles:transport:discord:ack:");
    }

    fn rest_for(server: &wiremock::MockServer) -> crate::discord_rest::DiscordRestClient {
        crate::discord_rest::DiscordRestClient::new(crate::config::Secret::new("tok"), server.uri())
            .unwrap()
    }

    /// Drives `discord_drain_loop` over `payloads` until the scripted queue is
    /// exhausted (it then errors forever -- see [`FakeQueue`]) or 5s pass.
    async fn drive_discord_drain(
        rest: Option<&crate::discord_rest::DiscordRestClient>,
        ctl: &FakeCtl,
        payloads: Vec<Result<Option<String>, String>>,
    ) {
        let sender = rest.map_or_else(DiscordSender::unconfigured, DiscordSender::new);
        let queue = FakeQueue {
            payloads,
            idx: AtomicUsize::new(0),
        };
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            discord_drain_loop(
                queue,
                &sender,
                ctl,
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
    }

    /// The Discord drain executes each op against the REST client; a
    /// malformed entry, a platform mismatch and a REST failure are dropped
    /// without stopping the loop -- and a waiting producer gets the real
    /// outcome of its own op (`op_id`), success or a stable failure code.
    #[tokio::test]
    async fn discord_drain_executes_ops_acks_the_outcome_and_survives_bad_entries() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .and(path("/channels/1/messages/2"))
            .respond_with(ResponseTemplate::new(204))
            .expect(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/channels/1/messages"))
            .respond_with(ResponseTemplate::new(500))
            .expect(1)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let ctl = FakeCtl::default();
        drive_discord_drain(
            Some(&rest),
            &ctl,
            vec![
                Ok(Some("not json".to_string())),
                Ok(Some(
                    r#"{"v":1,"op":"chat.send","platform":"twitch","channel":"1","origin_channel":"1","text":"x"}"#
                        .to_string(),
                )),
                Ok(Some(
                    r#"{"v":1,"op":"chat.send","platform":"discord","channel":"1","origin_channel":"1","text":"x","op_id":"send-1"}"#
                        .to_string(),
                )),
                Ok(None),
                Ok(Some(
                    r#"{"v":1,"op":"chat.delete","platform":"discord","channel":"1","origin_channel":"1","message_id":"2","op_id":"del-1"}"#
                        .to_string(),
                )),
            ],
        )
        .await;
        // wiremock `.expect(1)` verifies the REST calls on drop; the acks
        // are the producer-visible outcomes.
        let acks = ctl.acks.lock().unwrap().clone();
        assert_eq!(
            acks,
            vec![
                (
                    "send-1".to_string(),
                    r#"{"ok":false,"code":"failed"}"#.to_string()
                ),
                ("del-1".to_string(), r#"{"ok":true}"#.to_string()),
            ]
        );
    }

    /// A drain with no bot token cannot execute anything: it drops loudly,
    /// but a waiting producer still gets an explicit `unsupported` result
    /// instead of silently timing out.
    #[tokio::test]
    async fn discord_drain_unconfigured_sender_acks_unsupported() {
        let ctl = FakeCtl::default();
        drive_discord_drain(
            None,
            &ctl,
            vec![Ok(Some(
                r#"{"op":"chat.delete","channel":"1","origin_channel":"1","message_id":"2","op_id":"u-1"}"#.to_string(),
            ))],
        )
        .await;
        assert_eq!(
            ctl.acks.lock().unwrap().as_slice(),
            &[(
                "u-1".to_string(),
                r#"{"ok":false,"code":"unsupported"}"#.to_string()
            )]
        );
    }

    /// p6a producer-completeness: an authentic entry with no `op_id` nonce is
    /// refused with the distinct counted reason `no_op_id` (rollout signal),
    /// never executed and never acked.
    #[tokio::test]
    async fn discord_drain_refuses_an_authentic_entry_without_an_op_id() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .and(path("/channels/1/messages/2"))
            .respond_with(ResponseTemplate::new(204))
            .expect(0)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let ctl = FakeCtl::default();
        let before = crate::outbound_pin::refusal_count("discord", "chat.delete", "no_op_id");
        let exp_ms = super::now_ms() + 60_000;
        let sig = test_pin_keys()
            .sign(&crate::pin_sig::PinFields {
                platform: "discord",
                op: "chat.delete",
                channel: "1",
                origin_channel: "1",
                message_id: "2",
                text: "",
                user_id: "",
                op_id: "",
                exp_ms,
                community_id: 0,
                tenant_id: 0,
            })
            .unwrap();
        let raw = format!(
            r#"RAW:{{"op":"chat.delete","channel":"1","origin_channel":"1","message_id":"2","exp_ms":{exp_ms},"pin_sig":"{sig}"}}"#
        );
        drive_discord_drain(Some(&rest), &ctl, vec![Ok(Some(raw))]).await;
        assert!(ctl.acks.lock().unwrap().is_empty());
        assert_eq!(
            crate::outbound_pin::refusal_count("discord", "chat.delete", "no_op_id"),
            before + 1
        );
    }

    /// A pre-stamped-op_id discord entry, signed `exp` ms ahead, reusable
    /// byte-for-byte (same op_id + exp => same pin_sig) to model a replay.
    fn fixed_discord_entry(op_id: &str, exp_ms: u64) -> String {
        stamp_for_test(&format!(
            r#"{{"v":1,"op":"chat.delete","platform":"discord","channel":"1","origin_channel":"1","message_id":"2","op_id":"{op_id}","exp_ms":{exp_ms}}}"#
        ))
    }

    async fn drive_discord_guarded(
        rest: &crate::discord_rest::DiscordRestClient,
        ctl: &FakeCtl,
        guard: &MemoryReplayGuard,
        payloads: Vec<&str>,
    ) {
        let queue = FakeQueue {
            payloads: payloads
                .into_iter()
                .map(|p| Ok(Some(format!("RAW:{p}"))))
                .collect(),
            idx: AtomicUsize::new(0),
        };
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            discord_drain_loop_with(
                queue,
                &DiscordSender::new(rest),
                None::<&NoExecutor>,
                ctl,
                guard,
                &Egress::disabled(),
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
    }

    /// p6a replay (security review MEDIUM): a validly signed entry re-pushed
    /// inside its TTL executes ONCE; the duplicate is counted `pin_replayed`,
    /// is not executed and posts no second ack (it would overwrite the first
    /// outcome). The dedupe key is hash-only.
    #[tokio::test]
    async fn discord_replayed_signed_entry_executes_once_and_is_counted() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .and(path("/channels/1/messages/2"))
            .respond_with(ResponseTemplate::new(204))
            .expect(1)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let (ctl, guard) = (FakeCtl::default(), MemoryReplayGuard::default());
        let entry = fixed_discord_entry("replay-1", super::now_ms() + 60_000);
        let before = crate::outbound_pin::replay_count("discord", "chat.delete");
        drive_discord_guarded(&rest, &ctl, &guard, vec![&entry, &entry, &entry]).await;
        assert_eq!(ctl.acks.lock().unwrap().len(), 1, "one ack, not three");
        assert_eq!(
            crate::outbound_pin::replay_count("discord", "chat.delete"),
            before + 2
        );
        let claims = guard.claims.lock().unwrap();
        assert_eq!(claims.len(), 3);
        let (key, ttl) = &claims[0];
        assert!(key.starts_with("outbound:seen:discord:"));
        assert!(
            !key.contains("replay-1"),
            "key must be a digest, not the op_id"
        );
        assert!(*ttl > 0 && *ttl <= 60_000);
    }

    /// Two distinct signed ops (different op_id) are two executions -- the
    /// dedupe is per-op, not per-text/destination.
    #[tokio::test]
    async fn discord_distinct_op_ids_both_execute() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .and(path("/channels/1/messages/2"))
            .respond_with(ResponseTemplate::new(204))
            .expect(2)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let (ctl, guard) = (FakeCtl::default(), MemoryReplayGuard::default());
        let exp = super::now_ms() + 60_000;
        let (a, b) = (
            fixed_discord_entry("op-a", exp),
            fixed_discord_entry("op-b", exp),
        );
        drive_discord_guarded(&rest, &ctl, &guard, vec![&a, &b]).await;
        assert_eq!(ctl.acks.lock().unwrap().len(), 2);
    }

    /// Fail-CLOSED: when the single-use check cannot reach Valkey the entry is
    /// NOT executed (never an unchecked replay window); the producer gets the
    /// stable `failed` ack and the outage is counted.
    #[tokio::test]
    async fn discord_replay_check_outage_fails_closed_without_executing() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .and(path("/channels/1/messages/2"))
            .respond_with(ResponseTemplate::new(204))
            .expect(0)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let ctl = FakeCtl::default();
        let guard = MemoryReplayGuard {
            down: true,
            ..MemoryReplayGuard::default()
        };
        let entry = fixed_discord_entry("outage-1", super::now_ms() + 60_000);
        let before = crate::outbound_pin::replay_check_failed_count("discord", "chat.delete");
        drive_discord_guarded(&rest, &ctl, &guard, vec![&entry]).await;
        let acks = ctl.acks.lock().unwrap();
        assert_eq!(acks.len(), 1);
        assert_eq!(acks[0].0, "outage-1");
        assert!(acks[0].1.contains(r#""code":"failed""#), "{}", acks[0].1);
        assert_eq!(
            crate::outbound_pin::replay_check_failed_count("discord", "chat.delete"),
            before + 1
        );
    }

    /// Twitch twin: replay executes once; an outage executes nothing.
    #[tokio::test]
    async fn twitch_replay_executes_once_and_outage_fails_closed() {
        let exp = super::now_ms() + 60_000;
        let entry = stamp_for_test(&format!(
            r##"{{"v":1,"op":"chat.send","platform":"twitch","channel":"#c","text":"hi","origin_channel":"#c","op_id":"tw-1","exp_ms":{exp}}}"##
        ));
        let run = |guard: MemoryReplayGuard| {
            let entry = entry.clone();
            async move {
                let queue = FakeQueue {
                    payloads: vec![
                        Ok(Some(format!("RAW:{entry}"))),
                        Ok(Some(format!("RAW:{entry}"))),
                    ],
                    idx: AtomicUsize::new(0),
                };
                let sender = RecordingSender::default();
                let (_tx, rx) = oneshot::channel();
                let _ = tokio::time::timeout(
                    Duration::from_secs(5),
                    drain_loop_with(
                        queue,
                        &sender,
                        None::<&NoExecutor>,
                        &guard,
                        &Egress::disabled(),
                        Some(&test_pin_keys()),
                        Backoff::new(Duration::from_secs(30)),
                        rx,
                    ),
                )
                .await;
                sender
            }
        };
        let before = crate::outbound_pin::replay_count("twitch", "chat.send");
        let sender = run(MemoryReplayGuard::default()).await;
        assert_eq!(
            sender.calls.lock().unwrap().len(),
            1,
            "replay must not resend"
        );
        assert_eq!(
            crate::outbound_pin::replay_count("twitch", "chat.send"),
            before + 1
        );
        let down = MemoryReplayGuard {
            down: true,
            ..MemoryReplayGuard::default()
        };
        let before = crate::outbound_pin::replay_check_failed_count("twitch", "chat.send");
        let sender = run(down).await;
        assert!(
            sender.calls.lock().unwrap().is_empty(),
            "outage must not execute"
        );
        assert_eq!(
            crate::outbound_pin::replay_check_failed_count("twitch", "chat.send"),
            before + 2
        );
    }

    /// A connection to the real Valkey (`VALKEY_URL`, provided by the
    /// svc-ingest CI job like the other real-Valkey suites). Unset FAILS,
    /// never skips.
    async fn real_valkey() -> (redis::Client, String) {
        let url = std::env::var("VALKEY_URL")
            .expect("VALKEY_URL must be set: this test needs a REAL Valkey (no silent skip)");
        let prefix = format!(
            "outbound:seen:test-{}-{}",
            std::process::id(),
            OP_SEQ.fetch_add(1, Ordering::SeqCst)
        );
        (redis::Client::open(url).unwrap(), prefix)
    }

    /// Real-Valkey proof of the atomic `SET NX PX` claim: first claim wins, the
    /// duplicate loses, the key carries the PX ttl, expiry frees it, and 32
    /// concurrent claimers of one key produce EXACTLY ONE winner. Runs in CI
    /// (svc-ingest job sets `VALKEY_URL`); the racer/winner counts are
    /// asserted so a vacuous run cannot pass.
    #[tokio::test]
    async fn real_valkey_replay_guard_is_atomic_single_use_with_ttl() {
        const RACERS: usize = 32;
        let (client, prefix) = real_valkey().await;
        let guard = ValkeyReplayGuard::new(client.get_connection_manager().await.unwrap());
        let key = format!("{prefix}:a");
        assert!(guard.claim(&key, 400).await.unwrap(), "first claim wins");
        assert!(!guard.claim(&key, 400).await.unwrap(), "duplicate loses");
        let mut conn = client.get_connection_manager().await.unwrap();
        let pttl: i64 = redis::cmd("PTTL")
            .arg(&key)
            .query_async(&mut conn)
            .await
            .unwrap();
        assert!((1..=400).contains(&pttl), "PX ttl applied, got {pttl}");
        tokio::time::sleep(Duration::from_millis(500)).await;
        assert!(
            guard.claim(&key, 400).await.unwrap(),
            "expired key is claimable"
        );
        let race = format!("{prefix}:race");
        let g = std::sync::Arc::new(guard);
        let tasks: Vec<_> = (0..RACERS)
            .map(|_| {
                let (g, k) = (g.clone(), race.clone());
                tokio::spawn(async move { g.claim(&k, 5_000).await.unwrap() })
            })
            .collect();
        assert_eq!(tasks.len(), RACERS, "denominator: all racers started");
        let mut winners = 0;
        let mut finished = 0;
        for t in tasks {
            finished += 1;
            if t.await.unwrap() {
                winners += 1;
            }
        }
        assert_eq!(
            (finished, winners),
            (RACERS, 1),
            "exactly one winner of {RACERS}"
        );
        let _: () = redis::cmd("DEL")
            .arg(&race)
            .arg(&key)
            .query_async(&mut conn)
            .await
            .unwrap();
    }

    /// Real-Valkey fail-closed: when the connection to Valkey dies (a TCP proxy
    /// in front of it is killed after connect) `claim` returns Err and
    /// `claim_once` yields a typed `Failed` -- never a pass-through. Uses a
    /// private proxy so the shared CI Valkey is untouched.
    #[tokio::test]
    async fn real_valkey_outage_fails_closed() {
        let (client, prefix) = real_valkey().await;
        let info = client.get_connection_info().clone();
        let addr = info.addr().to_string();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let proxy_addr = listener.local_addr().unwrap();
        let upstream = addr.trim_start_matches("redis://").to_string();
        let proxy = tokio::spawn(async move {
            // Owned by this task: aborting the proxy drops the set and so
            // aborts every proxied connection too.
            let mut conns = tokio::task::JoinSet::new();
            loop {
                let Ok((mut inbound, _)) = listener.accept().await else {
                    return;
                };
                let upstream = upstream.clone();
                conns.spawn(async move {
                    if let Ok(mut out) = tokio::net::TcpStream::connect(&upstream).await {
                        let _ = tokio::io::copy_bidirectional(&mut inbound, &mut out).await;
                    }
                });
            }
        });
        let via = redis::Client::open(format!("redis://{proxy_addr}/")).unwrap();
        let guard = ValkeyReplayGuard::new(
            connect_with_response_timeout(&via, Duration::from_millis(500))
                .await
                .unwrap(),
        );
        let stamp = VerifiedStamp {
            replay_digest: format!("{prefix}-outage"),
            ttl_ms: 5_000,
        };
        // Healthy path through the proxy: first sight executes.
        assert!(claim_once(&guard, "discord", "chat.send", &stamp)
            .await
            .is_ok());
        // Kill the proxy and every proxied connection.
        proxy.abort();
        let _ = proxy.await;
        let before = crate::outbound_pin::replay_check_failed_count("discord", "chat.send");
        let stamp2 = VerifiedStamp {
            replay_digest: format!("{prefix}-outage2"),
            ttl_ms: 5_000,
        };
        let res = claim_once(&guard, "discord", "chat.send", &stamp2).await;
        assert!(
            matches!(res, Err(SenderError::Failed { .. })),
            "outage must fail closed, got {res:?}"
        );
        assert!(crate::outbound_pin::replay_check_failed_count("discord", "chat.send") > before);
    }

    /// `claim_once` maps the guard's three outcomes onto typed results.
    #[tokio::test]
    async fn claim_once_maps_outcomes() {
        let stamp = VerifiedStamp {
            replay_digest: "ab".repeat(32),
            ttl_ms: 5_000,
        };
        let g = MemoryReplayGuard::default();
        assert!(claim_once(&g, "discord", "chat.send", &stamp).await.is_ok());
        let dup = claim_once(&g, "discord", "chat.send", &stamp).await;
        assert!(matches!(
            dup,
            Err(SenderError::Rejected {
                reason: "pin_replayed",
                ..
            })
        ));
        let down = MemoryReplayGuard {
            down: true,
            ..MemoryReplayGuard::default()
        };
        let err = claim_once(&down, "discord", "chat.send", &stamp).await;
        assert!(matches!(err, Err(SenderError::Failed { .. })));
        assert_eq!(err.unwrap_err().ack_code(), "failed");
    }

    /// An entry already past its `exp_ms` deadline is dropped WITHOUT being
    /// executed (the producer reported failure; a late DM/delete would
    /// contradict it) and without an ack.
    #[tokio::test]
    async fn discord_drain_drops_an_expired_entry_unexecuted() {
        use wiremock::matchers::any;
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(any())
            .respond_with(ResponseTemplate::new(204))
            .expect(0)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let ctl = FakeCtl::default();
        drive_discord_drain(
            Some(&rest),
            &ctl,
            vec![Ok(Some(
                r#"{"op":"chat.delete","channel":"1","origin_channel":"1","message_id":"2","op_id":"old-1","exp_ms":1}"#
                    .to_string(),
            ))],
        )
        .await;
        assert!(ctl.acks.lock().unwrap().is_empty());
    }

    /// A DM to a user outside the triggering community is acked
    /// `not_in_community`, never delivered.
    #[tokio::test]
    async fn discord_drain_acks_not_in_community_for_a_foreign_dm_target() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/channels/1"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"id":"1","guild_id":"20"})),
            )
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/guilds/20/members/3"))
            .respond_with(
                ResponseTemplate::new(404).set_body_json(serde_json::json!({"code":10007})),
            )
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/users/@me/channels"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"5"})))
            .expect(0)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let ctl = FakeCtl::default();
        drive_discord_drain(
            Some(&rest),
            &ctl,
            vec![Ok(Some(
                r#"{"op":"dm.send","user_id":"3","text":"x","origin_channel":"1","op_id":"dm-1"}"#
                    .to_string(),
            ))],
        )
        .await;
        assert_eq!(
            ctl.acks.lock().unwrap().as_slice(),
            &[(
                "dm-1".to_string(),
                r#"{"ok":false,"code":"not_in_community"}"#.to_string()
            )]
        );
    }

    /// A failed ack write is loud (logged) but never stops the loop: the next
    /// entry is still executed.
    #[tokio::test]
    async fn discord_drain_survives_an_ack_write_failure() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .and(path("/channels/1/messages/2"))
            .respond_with(ResponseTemplate::new(204))
            .expect(2)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let ctl = FakeCtl {
            fail_ack: true,
            ..Default::default()
        };
        let entry =
            r#"{"op":"chat.delete","channel":"1","origin_channel":"1","message_id":"2","op_id":"x-1"}"#.to_string();
        drive_discord_drain(
            Some(&rest),
            &ctl,
            vec![Ok(Some(entry.clone())), Ok(Some(entry))],
        )
        .await;
        assert!(ctl.acks.lock().unwrap().is_empty());
    }

    #[test]
    fn ack_payload_is_content_free_and_stable() {
        assert_eq!(ack_payload(&Ok(None)), r#"{"ok":true}"#);
        assert_eq!(
            ack_payload(&Ok(Some("123".to_string()))),
            r#"{"ok":true,"id":"123"}"#
        );
        assert_eq!(
            ack_payload(&Ok(Some(r#"{"roles":[]}"#.to_string()))),
            r#"{"ok":true,"data":{"roles":[]}}"#
        );
        assert_eq!(
            ack_payload(&Ok(Some("{bad".to_string()))),
            r#"{"ok":false,"code":"bad_result"}"#
        );
        let rejected = ack_payload(&Err(SenderError::Rejected {
            platform: "discord",
            op: "dm.send",
            reason: "not_in_community",
        }));
        assert_eq!(rejected, r#"{"ok":false,"code":"not_in_community"}"#);
    }

    /// Runs the Twitch drain over raw payloads with the given keys.
    async fn drive_twitch(payloads: Vec<&str>, pin: Option<&PinKeys>) -> RecordingSender {
        let queue = FakeQueue {
            payloads: payloads
                .into_iter()
                .map(|p| Ok(Some(p.to_string())))
                .collect(),
            idx: AtomicUsize::new(0),
        };
        let sender = RecordingSender::default();
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            drain_loop(
                queue,
                &sender,
                pin,
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        sender
    }

    const GOOD_TWITCH: &str = r##"{"v":1,"op":"chat.send","platform":"twitch","channel":"#c","text":"hi","origin_channel":"#c"}"##;

    /// regression: p987 provenance -- an entry with a matching
    /// channel/origin_channel but NO `pin_sig` (any queue writer other than
    /// svc_action) is refused, counted, and never sent; the next stamped entry
    /// is still delivered (popped once -- no poison loop).
    #[tokio::test]
    async fn twitch_unsigned_entry_refused_counted_and_loop_survives() {
        let before = crate::outbound_pin::refusal_count("twitch", "chat.send", "no_pin_sig");
        let forged = format!("RAW:{GOOD_TWITCH}");
        let sender = drive_twitch(vec![&forged, GOOD_TWITCH], Some(&test_pin_keys())).await;
        assert_eq!(
            sender.calls.lock().unwrap().as_slice(),
            &[("#c".to_string(), "hi".to_string())]
        );
        assert!(crate::outbound_pin::refusal_count("twitch", "chat.send", "no_pin_sig") > before);
    }

    /// regression: p987 provenance -- a stamp that does not cover the fields
    /// on the wire (tampered channel/text, unknown kid, other key) is refused.
    #[tokio::test]
    async fn twitch_tampered_or_foreign_signature_refused() {
        let signed = stamp_for_test(GOOD_TWITCH);
        let tampered_text = signed.replace("\"text\":\"hi\"", "\"text\":\"evil\"");
        assert_ne!(signed, tampered_text);
        let sig = serde_json::from_str::<serde_json::Value>(&signed).unwrap()["pin_sig"]
            .as_str()
            .unwrap()
            .to_string();
        let unknown_kid = signed.replace(&sig, &sig.replacen("k1:", "k9:", 1));
        let foreign = PinKeys::parse("k1:ffffffffffffffffffffffffffffffff", Some("k1")).unwrap();
        let foreign_signed = {
            let exp_ms = super::now_ms() + 60_000;
            let mut v: serde_json::Value = serde_json::from_str(GOOD_TWITCH).unwrap();
            v["exp_ms"] = exp_ms.into();
            let sig = foreign
                .sign(&crate::pin_sig::PinFields {
                    platform: "twitch",
                    op: "chat.send",
                    channel: "#c",
                    origin_channel: "#c",
                    message_id: "",
                    text: "hi",
                    user_id: "",
                    op_id: "",
                    exp_ms,
                    community_id: 0,
                    tenant_id: 0,
                })
                .unwrap();
            v["pin_sig"] = serde_json::Value::String(sig);
            v.to_string()
        };
        for bad in [tampered_text, unknown_kid, foreign_signed] {
            let raw = format!("RAW:{bad}");
            let sender = drive_twitch(vec![&raw], Some(&test_pin_keys())).await;
            assert!(sender.calls.lock().unwrap().is_empty(), "{bad}");
        }
    }

    /// regression: p987 -- with no verification key configured the drain
    /// refuses every chat op (fail closed), it never sends unverified.
    #[tokio::test]
    async fn twitch_drain_without_keys_refuses_everything() {
        let sender = drive_twitch(vec![GOOD_TWITCH], None).await;
        assert!(sender.calls.lock().unwrap().is_empty());
        assert!(
            crate::outbound_pin::refusal_count("twitch", "chat.send", "pin_key_unconfigured") >= 1
        );
    }

    /// Rolling upgrade: an un-stamped in-flight entry (no `origin_channel`,
    /// as written by a pre-P6a svc_action) is dropped and counted under
    /// `no_origin_pin`.
    #[tokio::test]
    async fn rolling_upgrade_unstamped_entry_counted_as_no_origin_pin() {
        let before = crate::outbound_pin::refusal_count("twitch", "chat.send", "no_origin_pin");
        let sender = drive_twitch(
            vec![r##"{"channel":"#c","text":"hi"}"##],
            Some(&test_pin_keys()),
        )
        .await;
        assert!(sender.calls.lock().unwrap().is_empty());
        assert!(
            crate::outbound_pin::refusal_count("twitch", "chat.send", "no_origin_pin") > before
        );
    }

    /// regression: p987 4b -- a Discord entry whose stamp fails is dropped
    /// and counted, gets NO ack (an unauthenticated writer must not settle
    /// another producer's op_id), is popped exactly once (never retried),
    /// and the next valid entry still runs.
    #[tokio::test]
    async fn discord_rejected_entry_is_acked_not_retried_and_loop_continues() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .and(path("/channels/1/messages/2"))
            .respond_with(ResponseTemplate::new(204))
            .expect(1)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let ctl = FakeCtl::default();
        let before = crate::outbound_pin::refusal_count("discord", "chat.delete", "no_pin_sig");
        drive_discord_drain(
            Some(&rest),
            &ctl,
            vec![
                Ok(Some(
                    r#"RAW:{"v":1,"op":"chat.delete","platform":"discord","channel":"9","origin_channel":"9","message_id":"2","op_id":"forged-1"}"#.to_string(),
                )),
                Ok(Some(
                    r#"{"v":1,"op":"chat.delete","platform":"discord","channel":"1","origin_channel":"1","message_id":"2","op_id":"del-1"}"#.to_string(),
                )),
            ],
        )
        .await;
        assert_eq!(
            ctl.acks.lock().unwrap().as_slice(),
            &[("del-1".to_string(), r#"{"ok":true}"#.to_string())],
            "a forged entry's op_id must NOT be acked (would settle another producer's op)"
        );
        assert!(
            crate::outbound_pin::refusal_count("discord", "chat.delete", "no_pin_sig") > before
        );
    }

    /// regression: p987 4a -- Discord `chat.delete` is channel-scoped: the REST
    /// path is built from the pinned channel + message id (never from the
    /// message id alone), and a legitimately-signed entry whose `channel`
    /// differs from `origin_channel` makes no REST call at all.
    #[tokio::test]
    async fn discord_delete_is_channel_scoped_and_mismatch_never_hits_rest() {
        use wiremock::matchers::any;
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(any())
            .respond_with(ResponseTemplate::new(204))
            .expect(0)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let ctl = FakeCtl::default();
        // Signed by a real key holder but channel != origin: pin refuses.
        let exp_ms = super::now_ms() + 60_000;
        let signed = test_pin_keys()
            .sign(&crate::pin_sig::PinFields {
                platform: "discord",
                op: "chat.delete",
                channel: "3",
                origin_channel: "1",
                message_id: "2",
                text: "",
                user_id: "",
                op_id: "m-1",
                exp_ms,
                community_id: 0,
                tenant_id: 0,
            })
            .unwrap();
        let raw = format!(
            r#"RAW:{{"v":1,"op":"chat.delete","platform":"discord","channel":"3","origin_channel":"1","message_id":"2","op_id":"m-1","exp_ms":{exp_ms},"pin_sig":"{signed}"}}"#
        );
        drive_discord_drain(Some(&rest), &ctl, vec![Ok(Some(raw))]).await;
        assert_eq!(
            ctl.acks.lock().unwrap().as_slice(),
            &[(
                "m-1".to_string(),
                r#"{"ok":false,"code":"channel_not_pinned"}"#.to_string()
            )]
        );
    }

    /// regression: p987 review -- a signed-but-expired Twitch entry (captured
    /// and replayed after its `exp_ms`) is refused and counted `pin_expired`.
    #[tokio::test]
    async fn twitch_expired_signed_entry_is_refused_as_replay() {
        let before = crate::outbound_pin::refusal_count("twitch", "chat.send", "pin_expired");
        let stale = r##"{"v":1,"op":"chat.send","platform":"twitch","channel":"#c","text":"hi","origin_channel":"#c","exp_ms":1}"##;
        let sender = drive_twitch(vec![stale], Some(&test_pin_keys())).await;
        assert!(sender.calls.lock().unwrap().is_empty());
        assert!(crate::outbound_pin::refusal_count("twitch", "chat.send", "pin_expired") > before);
    }

    /// regression: p987 review (HIGH) -- an unsigned `dm.send` is refused
    /// (`no_pin_sig`, NOT acked) and makes NO REST call; a signed
    /// one still goes through.
    #[tokio::test]
    async fn discord_unsigned_dm_send_is_refused_and_never_hits_rest() {
        use wiremock::matchers::any;
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(any())
            .respond_with(ResponseTemplate::new(204))
            .expect(0)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let ctl = FakeCtl::default();
        let before = crate::outbound_pin::refusal_count("discord", "dm.send", "no_pin_sig");
        drive_discord_drain(
            Some(&rest),
            &ctl,
            vec![Ok(Some(
                r#"RAW:{"v":1,"op":"dm.send","platform":"discord","user_id":"42","text":"x","origin_channel":"1","op_id":"dm-9","exp_ms":99999999999999}"#.to_string(),
            ))],
        )
        .await;
        assert!(
            ctl.acks.lock().unwrap().is_empty(),
            "an unsigned entry carrying an op_id must produce no ack"
        );
        assert!(crate::outbound_pin::refusal_count("discord", "dm.send", "no_pin_sig") > before);
    }

    /// The heartbeat asserts readiness immediately and then every interval:
    /// 0s, 5s, 10s => three beats inside 11 virtual seconds.
    #[tokio::test(start_paused = true)]
    async fn heartbeat_marks_ready_immediately_and_every_interval() {
        let ctl = FakeCtl::default();
        let _ = tokio::time::timeout(Duration::from_secs(11), heartbeat_loop(&ctl)).await;
        assert_eq!(ctl.ready_marks.load(Ordering::SeqCst), 3);
    }

    /// Regression: the blocking-pop connection's response timeout must outlast
    /// the `BRPOP` block, or the client abandons every wait at the library's
    /// 500ms default while the server later pops (and loses) an entry.
    #[test]
    fn blocking_connection_timeout_outlasts_the_brpop_block() {
        assert!(BLOCKING_CONN_RESPONSE_TIMEOUT.as_secs_f64() > BRPOP_TIMEOUT_SECS);
        assert!(CONTROL_CONN_RESPONSE_TIMEOUT > Duration::from_millis(500));
    }

    #[test]
    fn readiness_ttl_spans_several_heartbeats() {
        assert!(DRAIN_READY_TTL_SECS >= 3 * DRAIN_HEARTBEAT_INTERVAL.as_secs());
    }

    async fn drive(raw: &str, sender: &RecordingSender) {
        let queue = FakeQueue {
            payloads: vec![Ok(Some(raw.to_string()))],
            idx: AtomicUsize::new(0),
        };
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(1),
            drain_loop(
                queue,
                sender,
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
    }

    fn chat_entry(extra: &str) -> String {
        format!(
            r##"{{"v":1,"op":"chat.send","platform":"twitch","channel":"#c","text":"hi","origin_channel":"#c"{extra}}}"##
        )
    }

    /// regression: p6a-t6b -- the entry's numeric community_id reaches the
    /// credential-resolving sender (so community A's message is sent with A's
    /// credential, B's with B's); non-positive/absent ids are `None`.
    #[tokio::test]
    async fn community_id_is_threaded_per_entry() {
        for (extra, want) in [
            (r#","community_id":1"#, Some(1)),
            (r#","community_id":2"#, Some(2)),
            (r#","community_id":0"#, None),
            ("", None),
        ] {
            let sender = RecordingSender::default();
            drive(&chat_entry(extra), &sender).await;
            assert_eq!(
                sender.communities.lock().unwrap().as_slice(),
                &[want],
                "{extra}"
            );
        }
    }

    /// regression: p6a-t6b -- a credential failure sends nothing (no fallback
    /// to a process-wide token) and every kind is refused.
    #[tokio::test]
    async fn credential_failure_sends_nothing_for_every_kind() {
        for e in [
            CredError::NoCommunityScope,
            CredError::NoBinding,
            CredError::ChannelNotOwned,
            CredError::NotUsable,
            CredError::Unavailable,
        ] {
            let sender = RecordingSender {
                cred_err: Some(e),
                ..RecordingSender::default()
            };
            drive(&chat_entry(r#","community_id":1"#), &sender).await;
            assert!(sender.calls.lock().unwrap().is_empty(), "{e:?}");
            assert_eq!(sender.communities.lock().unwrap().len(), 1, "{e:?}");
        }
    }

    /// The pin still refuses a non-owned channel BEFORE any credential is
    /// resolved (the sender is never reached).
    #[tokio::test]
    async fn non_pinned_channel_is_refused_before_credential_resolution() {
        let raw = r##"{"v":1,"op":"chat.send","platform":"twitch","channel":"#victim","text":"hi","origin_channel":"#mine","community_id":1}"##;
        let sender = RecordingSender::default();
        drive(raw, &sender).await;
        assert!(sender.communities.lock().unwrap().is_empty());
        assert!(sender.calls.lock().unwrap().is_empty());
    }

    /// regression: p6a-t6b -- through the real `RealIrcOutbound`: one
    /// credential resolve per message, and a missing credential fails loudly
    /// before any IRC connect.
    #[tokio::test]
    async fn real_outbound_resolves_per_message_and_fails_loud() {
        use crate::outbound_creds::{
            StaticCredentialSource, TwitchCredential, TwitchCredentialSource,
        };
        struct Counting(AtomicUsize);
        impl TwitchCredentialSource for Counting {
            async fn credential(
                &self,
                community_id: Option<i64>,
                _channel: &str,
            ) -> Result<TwitchCredential, CredError> {
                self.0.fetch_add(1, Ordering::SeqCst);
                match community_id {
                    Some(1) => Err(CredError::NoBinding),
                    _ => Err(CredError::NoCommunityScope),
                }
            }
        }
        let ep = || TwitchIrcEndpoint {
            host: "127.0.0.1".into(),
            port: 1,
            use_tls: false,
        };
        let real = RealIrcOutbound::new(ep(), Counting(AtomicUsize::new(0)));
        for (c, want) in [
            (Some(1), CredError::NoBinding),
            (Some(1), CredError::NoBinding),
            (None, CredError::NoCommunityScope),
        ] {
            let err = real
                .send(c, "#c", &egress_detokenizer::tokens_unchanged("hi"))
                .await
                .unwrap_err();
            assert!(
                matches!(err, IrcSendError::Credential(e) if e == want),
                "{err}"
            );
        }
        assert_eq!(
            real.creds.0.load(Ordering::SeqCst),
            3,
            "one resolve per use"
        );
        // The legacy source still yields a credential (the connect then fails on port 1).
        let legacy =
            RealIrcOutbound::new(ep(), StaticCredentialSource::new("n".into(), "t".into()));
        let err = legacy
            .send(None, "#c", &egress_detokenizer::tokens_unchanged("hi"))
            .await
            .unwrap_err();
        assert!(matches!(err, IrcSendError::Send(_)), "{err}");
    }

    // ---- P6a T6d-1: executor-backed drains ------------------------------

    use crate::connector::sender_exec::tests as exec_rig;
    use crate::outbound_ops::OutboundAction;

    /// Records what the drain handed the executor and answers from `script`
    /// (a function so the non-`Clone` [`SenderError`] can be minted per call).
    struct ScriptedExec {
        calls: Mutex<Vec<(&'static str, Option<i64>, &'static str)>>,
        script: fn(&'static str, &'static str) -> Result<(), SenderError>,
    }

    impl ScriptedExec {
        fn new(script: fn(&'static str, &'static str) -> Result<(), SenderError>) -> Self {
            Self {
                calls: Mutex::new(Vec::new()),
                script,
            }
        }
    }

    impl ActionExecutor for ScriptedExec {
        async fn execute_action(
            &self,
            platform: &'static str,
            community_id: Option<i64>,
            action: &crate::egress::DetokenizedAction,
        ) -> Result<Option<String>, SenderError> {
            self.calls
                .lock()
                .unwrap()
                .push((platform, community_id, action.op()));
            (self.script)(platform, action.op()).map(|()| None)
        }
    }

    async fn drive_twitch_exec<X: ActionExecutor>(
        exec: &X,
        sender: &RecordingSender,
        payloads: Vec<Result<Option<String>, String>>,
    ) {
        let queue = FakeQueue {
            payloads,
            idx: AtomicUsize::new(0),
        };
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            drain_loop_with(
                queue,
                sender,
                Some(exec),
                &MemoryReplayGuard::default(),
                &Egress::disabled(),
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
    }

    async fn drive_discord_exec<X: ActionExecutor>(
        exec: &X,
        ctl: &FakeCtl,
        payloads: Vec<Result<Option<String>, String>>,
    ) {
        let queue = FakeQueue {
            payloads,
            idx: AtomicUsize::new(0),
        };
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            discord_drain_loop_with(
                queue,
                &DiscordSender::unconfigured(),
                Some(exec),
                ctl,
                &MemoryReplayGuard::default(),
                &Egress::disabled(),
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
    }

    /// The Twitch / YouTube drains settle a waiting producer exactly like Discord's: an
    /// authentic entry with an `op_id` gets `{"ok":true}` or the stable refusal code; a forged
    /// (un-stamped) entry gets NO ack and never reaches the executor.
    #[tokio::test]
    async fn twitch_and_youtube_drains_ack_authentic_entries_only() {
        const BAN: &str = r##"{"v":1,"op":"mod.ban","platform":"twitch","channel":"#c","origin_channel":"#c","user_id":"123","op_id":"u-9","community_id":7}"##;
        const YT_ADD: &str = r##"{"v":1,"op":"mod.add","platform":"youtube","channel":"lc1","origin_channel":"lc1","user_id":"UCx","op_id":"u-10","community_id":7}"##;
        for (platform, ok_entry, forged_id) in [("twitch", BAN, "u-9"), ("youtube", YT_ADD, "u-10")]
        {
            let exec = ScriptedExec::new(|_, _| Ok(()));
            let ctl = FakeCtl::default();
            let forged = ok_entry.replace(forged_id, "forged-1");
            let queue = FakeQueue {
                payloads: vec![
                    Ok(Some(format!("RAW:{forged}"))),
                    Ok(Some(ok_entry.to_string())),
                ],
                idx: AtomicUsize::new(0),
            };
            let (_tx, rx) = oneshot::channel();
            let _ = tokio::time::timeout(
                Duration::from_secs(5),
                drain_platform_loop_acked(
                    platform,
                    queue,
                    &RecordingSender::default(),
                    Some(&exec),
                    &MemoryReplayGuard::default(),
                    &Egress::disabled(),
                    Some(&ctl),
                    Some(&test_pin_keys()),
                    Backoff::new(Duration::from_secs(30)),
                    rx,
                ),
            )
            .await;
            let op = if platform == "twitch" {
                "mod.ban"
            } else {
                "mod.add"
            };
            assert_eq!(
                exec.calls.lock().unwrap().as_slice(),
                &[(platform, Some(7), op)],
                "{platform}: only the stamped entry executes"
            );
            assert_eq!(
                ctl.acks.lock().unwrap().as_slice(),
                &[(forged_id.to_string(), r#"{"ok":true}"#.to_string())],
                "{platform}: exactly one ack, for the authentic entry"
            );
        }
    }

    /// A refused op is acked with its stable code so the producer reports failure, not success.
    #[tokio::test]
    async fn a_refused_twitch_op_is_acked_with_its_code() {
        const BAN: &str = r##"{"v":1,"op":"mod.ban","platform":"twitch","channel":"#c","origin_channel":"#c","user_id":"123","op_id":"u-11","community_id":7}"##;
        let exec = ScriptedExec::new(|p, op| {
            Err(SenderError::Rejected {
                platform: if p == "twitch" { "twitch" } else { "other" },
                op: if op == "mod.ban" { "mod.ban" } else { "other" },
                reason: "slot_refused",
            })
        });
        let ctl = FakeCtl::default();
        let queue = FakeQueue {
            payloads: vec![Ok(Some(BAN.to_string()))],
            idx: AtomicUsize::new(0),
        };
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            drain_platform_loop_acked(
                "twitch",
                queue,
                &RecordingSender::default(),
                Some(&exec),
                &MemoryReplayGuard::default(),
                &Egress::disabled(),
                Some(&ctl),
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert_eq!(
            ctl.acks.lock().unwrap().as_slice(),
            &[(
                "u-11".to_string(),
                r#"{"ok":false,"code":"slot_refused"}"#.to_string()
            )]
        );
    }

    #[test]
    fn ack_payload_returns_objects_and_arrays_as_data_and_ids_as_id() {
        let ok = |s: &str| -> serde_json::Value {
            serde_json::from_str(&ack_payload(&Ok(Some(s.to_string())))).unwrap()
        };
        assert_eq!(
            ok(r#"{"data":[1]}"#),
            serde_json::json!({"ok":true,"data":{"data":[1]}})
        );
        assert_eq!(
            ok(r#"[{"id":"1"}]"#),
            serde_json::json!({"ok":true,"data":[{"id":"1"}]})
        );
        assert_eq!(
            ok("900000000000000001"),
            serde_json::json!({"ok":true,"id":"900000000000000001"})
        );
        assert_eq!(
            ok("{not json"),
            serde_json::json!({"ok":false,"code":"bad_result"})
        );
        assert_eq!(ack_payload(&Ok(None)), r#"{"ok":true}"#);
    }

    const TW_SEND: &str = r##"{"v":1,"op":"chat.send","platform":"twitch","channel":"#c","text":"hi","origin_channel":"#c","community_id":7}"##;

    /// The stamp gate stays the FIRST gate with an executor configured: a
    /// forged / un-stamped / unknown-op / malformed entry never reaches it,
    /// the legacy sender is never consulted, and the loop keeps draining.
    #[tokio::test]
    async fn executor_backed_twitch_drain_keeps_the_stamp_gate_first_and_never_uses_the_legacy_sender(
    ) {
        let exec = ScriptedExec::new(|_, _| Ok(()));
        let sender = RecordingSender::default();
        drive_twitch_exec(
            &exec,
            &sender,
            vec![
                Ok(Some(format!("RAW:{TW_SEND}"))),
                Ok(Some("not json".to_string())),
                Ok(Some(
                    r##"{"v":1,"op":"bogus","platform":"twitch","channel":"#c","text":"hi","origin_channel":"#c"}"##.to_string(),
                )),
                Ok(Some(TW_SEND.to_string())),
            ],
        )
        .await;
        assert_eq!(
            exec.calls.lock().unwrap().as_slice(),
            &[("twitch", Some(7), "chat.send")],
            "only the signed, well-formed entry reaches the executor, with its numeric community"
        );
        assert!(sender.calls.lock().unwrap().is_empty());
        assert!(sender.communities.lock().unwrap().is_empty());
    }

    /// The YouTube drain runs the same stamp-first gate under the `youtube`
    /// label: a signed youtube entry reaches the executor with its numeric
    /// community; a twitch-platform entry on that queue never does.
    #[tokio::test]
    async fn youtube_drain_stamps_and_executes_only_youtube_entries() {
        const YT_SEND: &str = r##"{"v":1,"op":"chat.send","platform":"youtube","channel":"lc1","text":"hi","origin_channel":"lc1","community_id":7}"##;
        let exec = ScriptedExec::new(|_, _| Ok(()));
        let queue = FakeQueue {
            payloads: vec![
                Ok(Some(format!("RAW:{YT_SEND}"))),
                Ok(Some(TW_SEND.to_string())),
                Ok(Some(YT_SEND.to_string())),
            ],
            idx: AtomicUsize::new(0),
        };
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            drain_platform_loop(
                "youtube",
                queue,
                &NoIrc,
                Some(&exec),
                &MemoryReplayGuard::default(),
                &Egress::disabled(),
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert_eq!(
            exec.calls.lock().unwrap().as_slice(),
            &[("youtube", Some(7), "chat.send")]
        );
    }

    /// A non-twitch platform has no legacy sender: without an executor the
    /// entry fails loud (Unsupported) instead of reaching the twitch sender.
    #[tokio::test]
    async fn a_non_twitch_drain_without_an_executor_never_uses_the_twitch_sender() {
        const YT_SEND: &str = r##"{"v":1,"op":"chat.send","platform":"youtube","channel":"lc1","text":"hi","origin_channel":"lc1","community_id":7}"##;
        let sender = RecordingSender::default();
        let queue = FakeQueue {
            payloads: vec![Ok(Some(YT_SEND.to_string()))],
            idx: AtomicUsize::new(0),
        };
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            drain_platform_loop(
                "youtube",
                queue,
                &sender,
                None::<&NoExecutor>,
                &MemoryReplayGuard::default(),
                &Egress::disabled(),
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert!(sender.calls.lock().unwrap().is_empty());
    }

    /// Refused / failed / unsupported outcomes are consumed (already popped),
    /// never re-queued, and never stall the entries behind them.
    #[tokio::test]
    async fn executor_outcomes_are_dropped_not_retried_and_the_loop_continues() {
        type Script = fn(&'static str, &'static str) -> Result<(), SenderError>;
        let scripts: [Script; 3] = [
            |_, op| {
                Err(SenderError::Rejected {
                    platform: "twitch",
                    op,
                    reason: "channel_not_owned",
                })
            },
            |_, op| {
                Err(SenderError::Failed {
                    platform: "twitch",
                    op,
                    message: "upstream_error".into(),
                })
            },
            |p, op| {
                Err(SenderError::Unsupported {
                    platform: p.to_string(),
                    op,
                })
            },
        ];
        for script in scripts {
            let exec = ScriptedExec::new(script);
            drive_twitch_exec(
                &exec,
                &RecordingSender::default(),
                vec![Ok(Some(TW_SEND.to_string())), Ok(Some(TW_SEND.to_string()))],
            )
            .await;
            assert_eq!(
                exec.calls.lock().unwrap().len(),
                2,
                "each entry executed exactly once"
            );
        }
    }

    /// Discord: the producer's ack carries the executor's stable code; an
    /// unsigned entry is neither executed nor acked.
    #[tokio::test]
    async fn executor_backed_discord_drain_acks_stable_codes_and_gates_on_the_stamp() {
        fn script(_: &'static str, op: &'static str) -> Result<(), SenderError> {
            match op {
                "chat.send" => Ok(()),
                "chat.delete" => Err(SenderError::Rejected {
                    platform: "discord",
                    op,
                    reason: "flag_off",
                }),
                _ => Err(SenderError::Unsupported {
                    platform: "discord".into(),
                    op,
                }),
            }
        }
        let exec = ScriptedExec::new(script);
        let ctl = FakeCtl::default();
        drive_discord_exec(
            &exec,
            &ctl,
            vec![
                Ok(Some(r#"RAW:{"v":1,"op":"chat.send","platform":"discord","channel":"1","origin_channel":"1","text":"x","op_id":"forged"}"#.to_string())),
                Ok(Some(r#"{"v":1,"op":"chat.send","platform":"discord","channel":"1","origin_channel":"1","text":"x","op_id":"a","community_id":3}"#.to_string())),
                Ok(Some(r#"{"v":1,"op":"chat.delete","platform":"discord","channel":"1","origin_channel":"1","message_id":"2","op_id":"b"}"#.to_string())),
                Ok(Some(r#"{"v":1,"op":"dm.send","platform":"discord","user_id":"u","text":"x","origin_channel":"1","op_id":"c"}"#.to_string())),
            ],
        )
        .await;
        assert_eq!(
            ctl.acks.lock().unwrap().as_slice(),
            &[
                ("a".to_string(), r#"{"ok":true}"#.to_string()),
                (
                    "b".to_string(),
                    r#"{"ok":false,"code":"flag_off"}"#.to_string()
                ),
                (
                    "c".to_string(),
                    r#"{"ok":false,"code":"unsupported"}"#.to_string()
                ),
            ]
        );
        let calls = exec.calls.lock().unwrap();
        assert_eq!(calls.len(), 3);
        assert_eq!(calls[0], ("discord", Some(3), "chat.send"));
    }

    /// The real executor (verifier + substituter + guarded client) behind the
    /// real twitch drain against a local fake Helix: the signed entry becomes
    /// ONE Helix request, authenticated with the host-resolved credential.
    #[tokio::test]
    async fn real_executor_behind_the_twitch_drain_sends_one_helix_request() {
        let rig = exec_rig::rig(
            crate::connector::sender_exec::PlatformPolicy::twitch(),
            crate::connector::sender_exec::verify::tests::twitch_send(),
            exec_rig::MapSlots::twitch(),
            true,
        )
        .await;
        let sender = RecordingSender::default();
        drive_twitch_exec(&rig.exec, &sender, vec![Ok(Some(TW_SEND.to_string()))]).await;
        let got = exec_rig::sent(&rig).await;
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].url.path(), "/helix/chat/messages");
        assert!(
            sender.calls.lock().unwrap().is_empty(),
            "legacy IRC path unused"
        );
        assert_eq!(rig.slots.seen.lock().unwrap()[0].0, Some(7));
    }

    /// regression: p987 -- with the executor configured, a validly-signed
    /// entry whose channel differs from the stamped origin still makes NO
    /// request (the executor pin runs on top of the stamp gate), and the
    /// refusal is counted.
    #[tokio::test]
    async fn real_executor_refuses_a_wrong_channel_even_when_the_signature_is_valid() {
        let rig = exec_rig::rig(
            crate::connector::sender_exec::PlatformPolicy::twitch(),
            crate::connector::sender_exec::verify::tests::twitch_send(),
            exec_rig::MapSlots::twitch(),
            true,
        )
        .await;
        let before =
            crate::outbound_pin::refusal_count("twitch", "chat.send", "channel_not_pinned");
        drive_twitch_exec(
            &rig.exec,
            &RecordingSender::default(),
            vec![Ok(Some(
                r##"{"v":1,"op":"chat.send","platform":"twitch","channel":"#victim","text":"hi","origin_channel":"#c","community_id":7}"##.to_string(),
            ))],
        )
        .await;
        assert!(exec_rig::sent(&rig).await.is_empty());
        assert_eq!(rig.builder.calls.lock().unwrap().len(), 0);
        assert!(
            crate::outbound_pin::refusal_count("twitch", "chat.send", "channel_not_pinned")
                > before
        );
    }

    /// Flag OFF: the executor refuses; the producer is acked with the stable
    /// code; nothing is built or sent. A DM (host duty not registered) is
    /// acked `unsupported`, never executed half-way.
    #[tokio::test]
    async fn real_executor_flag_off_and_missing_duty_are_acked_loudly() {
        let rig = exec_rig::rig(
            crate::connector::sender_exec::PlatformPolicy::discord(),
            crate::connector::sender_exec::verify::tests::discord_send(),
            exec_rig::MapSlots::with(&[("discord-bot", "tok")]),
            false,
        )
        .await;
        let ctl = FakeCtl::default();
        drive_discord_exec(
            &rig.exec,
            &ctl,
            vec![Ok(Some(
                r#"{"v":1,"op":"chat.send","platform":"discord","channel":"1","origin_channel":"1","text":"x","op_id":"f"}"#.to_string(),
            ))],
        )
        .await;
        assert_eq!(
            ctl.acks.lock().unwrap().as_slice(),
            &[(
                "f".to_string(),
                r#"{"ok":false,"code":"flag_off"}"#.to_string()
            )]
        );
        assert!(exec_rig::sent(&rig).await.is_empty());
        assert_eq!(rig.builder.calls.lock().unwrap().len(), 0);

        let on = exec_rig::rig(
            crate::connector::sender_exec::PlatformPolicy::discord(),
            crate::connector::sender_exec::verify::tests::discord_send(),
            exec_rig::MapSlots::with(&[("discord-bot", "tok")]),
            true,
        )
        .await;
        let ctl = FakeCtl::default();
        drive_discord_exec(
            &on.exec,
            &ctl,
            vec![Ok(Some(
                r#"{"v":1,"op":"dm.send","platform":"discord","user_id":"9","text":"x","origin_channel":"1","op_id":"d"}"#.to_string(),
            ))],
        )
        .await;
        assert_eq!(
            ctl.acks.lock().unwrap().as_slice(),
            &[(
                "d".to_string(),
                r#"{"ok":false,"code":"unsupported"}"#.to_string()
            )]
        );
        assert!(exec_rig::sent(&on).await.is_empty());
    }

    #[test]
    fn exec_error_mapping_is_content_free_and_classified() {
        use crate::connector::sender_exec::{ExecError, SlotError};
        use crate::outbound_ops::exec_to_sender_error as map;
        let r = map("twitch", "chat.send", &ExecError::FlagOff);
        assert!(matches!(
            r,
            SenderError::Rejected {
                reason: "flag_off",
                ..
            }
        ));
        let r = map(
            "twitch",
            "chat.send",
            &ExecError::Slot(SlotError::Refused("no_connection")),
        );
        assert!(matches!(
            r,
            SenderError::Rejected {
                reason: "no_connection",
                ..
            }
        ));
        let r = map(
            "twitch",
            "chat.send",
            &ExecError::Slot(SlotError::Unavailable("credential_unavailable")),
        );
        assert!(
            matches!(&r, SenderError::Failed { message, .. } if message == "credential_unavailable")
        );
        let r = map("twitch", "chat.send", &ExecError::Upstream(500));
        assert!(matches!(&r, SenderError::Failed { message, .. } if message == "upstream_error"));
        let r = map("discord", "dm.send", &ExecError::HostDutyMissing);
        assert!(matches!(r, SenderError::Unsupported { .. }));
        let r = map("slack", "chat.send", &ExecError::UnsupportedPlatform);
        assert!(matches!(r, SenderError::Unsupported { .. }));
        for e in [ExecError::FlagOff, ExecError::Upstream(401)] {
            let shown = map("twitch", "chat.send", &e).to_string();
            assert!(!shown.contains("401") && !shown.contains("http"), "{shown}");
        }
    }

    #[tokio::test]
    async fn no_executor_fails_loud_and_the_legacy_wrapper_matches_none() {
        let a = OutboundAction::ChatSend {
            channel: "#c".into(),
            text: "t".into(),
            origin_channel: Some("#c".into()),
            tenant_id: None,
        };
        let e = NoExecutor
            .execute_action(
                "twitch",
                Some(1),
                &crate::egress::DetokenizedAction::tokens_unchanged(a.clone()),
            )
            .await
            .unwrap_err();
        assert!(matches!(e, SenderError::Unsupported { .. }));
        assert_eq!(e.ack_code(), "unsupported");
    }

    /// Resolver fixture: every token maps to one fixed display name.
    struct FixedName(&'static str);

    impl egress_detokenizer::DisplayNameResolver for FixedName {
        fn resolve_many<'a>(
            &'a self,
            _tenant_id: &'a str,
            tokens: Vec<String>,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<
                        Output = Result<
                            std::collections::HashMap<String, String>,
                            egress_detokenizer::DetokenizeError,
                        >,
                    > + Send
                    + 'a,
            >,
        > {
            let name = self.0;
            Box::pin(async move { Ok(tokens.into_iter().map(|t| (t, name.to_string())).collect()) })
        }
    }

    /// PII boundary: the REAL Discord REST request body carries the resolved, escaped display
    /// name -- the detokenization happens in the drain, immediately before the platform send.
    #[tokio::test]
    async fn discord_drain_sends_the_escaped_display_name_in_the_platform_request() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        // Tenant 7 is the default tenant here, so the env-token sender serves it.
        crate::tenant_discord::set_default_tenant_id(7);
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/channels/1/messages"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
            .expect(1)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let sender = DiscordSender::new(&rest);
        let egress = Egress::new(
            std::sync::Arc::new(FixedName("a_b*c")),
            crate::egress::KillSwitch::Fixed(true),
        );
        let ctl = FakeCtl::default();
        let queue = FakeQueue {
            payloads: vec![Ok(Some(
                r#"{"v":1,"op":"chat.send","platform":"discord","channel":"1","origin_channel":"1","text":"hi {user:0b9f2c1e-7d3a-4c58-9a1e-5f6d7e8a9b0c}","op_id":"send-1","tenant_id":7}"#
                    .to_string(),
            ))],
            idx: AtomicUsize::new(0),
        };
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            discord_drain_loop_with(
                queue,
                &sender,
                None::<&NoExecutor>,
                &ctl,
                &MemoryReplayGuard::default(),
                &egress,
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        let reqs = server.received_requests().await.expect("recorded");
        assert_eq!(reqs.len(), 1, "exactly one platform request");
        let body: serde_json::Value = serde_json::from_slice(&reqs[0].body).expect("json body");
        assert_eq!(body["content"], "hi a\\_b\\*c");
        assert_eq!(
            ctl.acks.lock().unwrap().as_slice(),
            &[("send-1".to_string(), r#"{"ok":true}"#.to_string())]
        );
    }

    // ---- tenant-owned Discord application (tenant_discord) ----

    use crate::tenant_discord::{
        app_missing_count, SURFACE_OUTBOUND_BINDING, SURFACE_OUTBOUND_ENV,
        TENANT_DISCORD_APP_MISSING,
    };

    /// Executor stand-in: no binding for the community (`reject`) or success;
    /// records the communities it was asked to act for.
    #[derive(Default)]
    struct BindingExec {
        reject: bool,
        communities: Mutex<Vec<Option<i64>>>,
    }

    impl ActionExecutor for BindingExec {
        async fn execute_action(
            &self,
            _platform: &'static str,
            community_id: Option<i64>,
            _action: &crate::egress::DetokenizedAction,
        ) -> Result<Option<String>, SenderError> {
            self.communities.lock().unwrap().push(community_id);
            if self.reject {
                return Err(SenderError::Rejected {
                    platform: "discord",
                    op: "chat.send",
                    reason: "no_connection",
                });
            }
            Ok(None)
        }
    }

    fn send_entry(op_id: &str, tenant: Option<i64>, community: i64) -> String {
        let t = tenant.map_or(String::new(), |t| format!(r#","tenant_id":{t}"#));
        format!(
            r#"{{"v":1,"op":"chat.send","platform":"discord","channel":"1","origin_channel":"1","text":"hi","op_id":"{op_id}","community_id":{community}{t}}}"#
        )
    }

    /// Drives the Discord drain over `payloads` with the REAL Valkey replay guard.
    async fn drive_real_guard<X: ActionExecutor>(
        sender: &DiscordSender<'_>,
        exec: Option<&X>,
        ctl: &FakeCtl,
        payloads: Vec<String>,
    ) {
        let url = std::env::var("VALKEY_URL")
            .expect("VALKEY_URL must be set: this test needs a REAL Valkey (no silent skip)");
        let client = redis::Client::open(url).unwrap();
        let guard =
            ValkeyReplayGuard::new(client.get_multiplexed_async_connection().await.unwrap());
        let queue = FakeQueue {
            payloads: payloads.into_iter().map(|p| Ok(Some(p))).collect(),
            idx: AtomicUsize::new(0),
        };
        let (_tx, rx) = oneshot::channel();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            discord_drain_loop_with(
                queue,
                sender,
                exec,
                ctl,
                &guard,
                &Egress::disabled(),
                Some(&test_pin_keys()),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
    }

    /// The env-token drain never sends for a non-default tenant (even though the
    /// env token is configured) and serves the default tenant with it.
    #[tokio::test]
    async fn env_token_drain_serves_only_the_default_tenant_real_valkey() {
        use wiremock::matchers::{header, method};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        // Only the default tenant's send may reach Discord, with the env token.
        Mock::given(method("POST"))
            .and(header("Authorization", "Bot tok"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"9"})))
            .expect(1)
            .mount(&server)
            .await;
        let rest = rest_for(&server);
        let ctl = FakeCtl::default();
        let seq = OP_SEQ.fetch_add(1, Ordering::SeqCst);
        let (foreign, own) = (format!("tdn-f-{seq}"), format!("tdn-d-{seq}"));
        let before = app_missing_count(SURFACE_OUTBOUND_ENV);
        drive_real_guard(
            &DiscordSender::new(&rest),
            None::<&NoExecutor>,
            &ctl,
            vec![
                stamp_for_test(&send_entry(&foreign, Some(99), 3)),
                stamp_for_test(&send_entry(&own, None, 4)),
            ],
        )
        .await;
        let acks = ctl.acks.lock().unwrap();
        assert_eq!(acks.len(), 2, "both ops acked (denominator)");
        let by = |id: &str| acks.iter().find(|(i, _)| i == id).unwrap().1.clone();
        assert!(
            by(&foreign).contains(TENANT_DISCORD_APP_MISSING),
            "{}",
            by(&foreign)
        );
        assert_eq!(by(&own), r#"{"ok":true}"#);
        assert_eq!(app_missing_count(SURFACE_OUTBOUND_ENV), before + 1);
    }

    /// Executor path: a non-default tenant with no binding fails loud with the
    /// stable code (no env fallback exists there); the default tenant keeps the
    /// plain `no_connection`; tenant A's community never serves tenant B's.
    #[tokio::test]
    async fn executor_path_refuses_a_non_default_tenant_without_a_binding_real_valkey() {
        let seq = OP_SEQ.fetch_add(1, Ordering::SeqCst);
        let ctl = FakeCtl::default();
        let exec = BindingExec {
            reject: true,
            ..BindingExec::default()
        };
        let (a, b) = (format!("tdn-a-{seq}"), format!("tdn-b-{seq}"));
        let before = app_missing_count(SURFACE_OUTBOUND_BINDING);
        drive_real_guard(
            &DiscordSender::unconfigured(),
            Some(&exec),
            &ctl,
            vec![
                stamp_for_test(&send_entry(&a, Some(99), 3)),
                stamp_for_test(&send_entry(&b, None, 4)),
            ],
        )
        .await;
        let acks = ctl.acks.lock().unwrap();
        assert_eq!(acks.len(), 2);
        let by = |id: &str| acks.iter().find(|(i, _)| i == id).unwrap().1.clone();
        assert!(by(&a).contains(TENANT_DISCORD_APP_MISSING), "{}", by(&a));
        assert!(by(&b).contains("no_connection") && !by(&b).contains(TENANT_DISCORD_APP_MISSING));
        assert_eq!(app_missing_count(SURFACE_OUTBOUND_BINDING), before + 1);
        assert_eq!(
            exec.communities.lock().unwrap().as_slice(),
            &[Some(3), Some(4)],
            "each op resolved for its own community only"
        );
    }

    /// Per-community binding isolation: community A's token is A's, B's is B's,
    /// and a community with no binding never receives either (nor an env token).
    #[tokio::test]
    async fn slots_use_each_communitys_own_bot_token_and_never_another() {
        use crate::connector::sender_exec::discord_slots::{DiscordSlots, BOT_HANDLE};
        use crate::connector::sender_exec::{SlotError, SlotRequest, SlotSource};
        use crate::outbound_creds_discord::{DiscordBinding, DiscordCredentialSource};
        use std::collections::BTreeMap;

        struct Creds;
        impl DiscordCredentialSource for Creds {
            async fn binding(&self, c: Option<i64>) -> Result<DiscordBinding, CredError> {
                let tok = match c {
                    Some(3) => "TOKEN-TENANT-A",
                    Some(4) => "TOKEN-TENANT-B",
                    _ => return Err(CredError::NoBinding),
                };
                Ok(DiscordBinding {
                    bot_token: connection_resolver::SecretString::new(tok.to_string()),
                    guild_id: "123456789012345678".into(),
                    application_id: None,
                    managed_roles: BTreeMap::new(),
                })
            }
        }
        let slots = DiscordSlots::new(Creds);
        let req = |c: Option<i64>| SlotRequest {
            platform: "discord",
            community_id: c,
            pinned_channel: None,
            handles: &[BOT_HANDLE],
            guild_scoped: true,
        };
        let a = slots.resolve(&req(Some(3))).await.unwrap();
        let b = slots.resolve(&req(Some(4))).await.unwrap();
        assert_eq!(a[BOT_HANDLE].expose(), "TOKEN-TENANT-A");
        assert_eq!(b[BOT_HANDLE].expose(), "TOKEN-TENANT-B");
        assert_ne!(a[BOT_HANDLE].expose(), b[BOT_HANDLE].expose());
        assert!(matches!(
            slots.resolve(&req(Some(5))).await,
            Err(SlotError::Refused("no_connection"))
        ));
    }

    /// Real Postgres + real Valkey proof of the direct-DB egress detokenizer.
    mod real_db_detokenize {
        use super::*;
        use crate::display_names::PgDisplayNameResolver;
        use sea_orm::{ConnectionTrait, Database, DatabaseConnection};

        const TENANT_A: i64 = 7;
        const TENANT_B: i64 = 8;
        const ALICE: &str = "0b9f2c1e-7d3a-4c58-9a1e-5f6d7e8a9b0c";
        const BOB_OTHER_TENANT: &str = "1c8e3d2f-6e4b-4d69-8b2f-6a7e8f9a0b1d";
        const ERASED: &str = "2d7f4e3a-5f5c-4e7a-9c3a-7b8f9a0b1c2e";
        const PSEUDO: &str = "3e6a5f4b-4a6d-4f8b-8d4b-8c9a0b1c2d3f";

        /// `TEST_DATABASE_URL` (owner credential; CI starts the container). Unset FAILS.
        fn admin_url() -> String {
            std::env::var("TEST_DATABASE_URL").expect(
                "TEST_DATABASE_URL must be set: this test needs a REAL Postgres (no silent skip)",
            )
        }

        /// Creates an isolated schema with the identity tables, seeds it, and a LOGIN role
        /// holding EXACTLY alembic 0100's column grants. Returns the role's URL (search_path
        /// pinned to the schema) and the admin connection.
        async fn fixture() -> (String, DatabaseConnection, String) {
            let n = OP_SEQ.fetch_add(1, Ordering::SeqCst);
            let schema = format!("detok_{}_{n}", std::process::id());
            let role = format!("detok_ro_{}_{n}", std::process::id());
            let admin = Database::connect(admin_url()).await.expect("admin connect");
            let sql = format!(
                r#"
DROP SCHEMA IF EXISTS {schema} CASCADE;
CREATE SCHEMA {schema};
SET search_path = {schema};
CREATE TABLE tenants (id BIGINT PRIMARY KEY);
CREATE TABLE communities (id BIGINT PRIMARY KEY, tenant_id BIGINT NOT NULL, name TEXT);
CREATE TABLE hub_users (id BIGINT PRIMARY KEY, uuid UUID UNIQUE NOT NULL,
    display_name TEXT, email TEXT, password_hash TEXT);
CREATE TABLE community_members (id BIGSERIAL PRIMARY KEY, community_id BIGINT NOT NULL,
    user_uuid UUID, user_id TEXT, display_name TEXT, bio TEXT);
CREATE TABLE ephemeral_pseudonyms (tenant_id BIGINT NOT NULL, pseudonym UUID NOT NULL,
    handle TEXT, platform_user_id TEXT);
INSERT INTO tenants VALUES ({TENANT_A}), ({TENANT_B});
INSERT INTO communities VALUES (70, {TENANT_A}, 'a'), (80, {TENANT_B}, 'b');
INSERT INTO hub_users VALUES (1, '{ALICE}', 'Alice', 'a@example.invalid', 'x'),
    (2, '{BOB_OTHER_TENANT}', 'Bob', 'b@example.invalid', 'x'),
    (3, '{ERASED}', NULL, 'deleted_3@example.invalid', NULL);
INSERT INTO community_members (community_id, user_uuid, display_name)
    VALUES (70, '{ALICE}', 'alice-nick'), (80, '{BOB_OTHER_TENANT}', 'bob-nick');
INSERT INTO ephemeral_pseudonyms VALUES ({TENANT_A}, '{PSEUDO}', 'Pseudo', 'p-1');
CREATE ROLE {role} LOGIN PASSWORD 'ci-only-pass';
GRANT USAGE ON SCHEMA {schema} TO {role};
GRANT SELECT (id, uuid, display_name) ON hub_users TO {role};
GRANT SELECT (id, community_id, user_uuid, user_id, display_name) ON community_members TO {role};
GRANT SELECT (id, tenant_id) ON communities TO {role};
GRANT SELECT (tenant_id, pseudonym, handle) ON ephemeral_pseudonyms TO {role};
"#
            );
            admin.execute_unprepared(&sql).await.expect("seed schema");
            let mut url = reqwest::Url::parse(&admin_url()).expect("url");
            url.set_username(&role).expect("user");
            url.set_password(Some("ci-only-pass")).expect("pass");
            url.set_query(Some(&format!("options=-c%20search_path%3D{schema}")));
            (url.to_string(), admin, role)
        }

        async fn teardown(admin: &DatabaseConnection, url: &str, role: &str) {
            let schema = url
                .rsplit("search_path%3D")
                .next()
                .unwrap_or_default()
                .to_string();
            let _ = admin
                .execute_unprepared(&format!(
                    "DROP SCHEMA IF EXISTS {schema} CASCADE; DROP OWNED BY {role}; DROP ROLE IF EXISTS {role};"
                ))
                .await;
        }

        fn egress_over(url: String) -> Egress {
            Egress::new(
                std::sync::Arc::new(PgDisplayNameResolver::new(url, Duration::from_secs(3))),
                crate::egress::KillSwitch::Fixed(true),
            )
        }

        /// Runs ONE entry through the real Discord drain: real Valkey list queue + replay
        /// guard, the resolver under test, a local Discord REST double. Returns the body text.
        async fn drain_one(egress: &Egress, text: &str, tenant: i64) -> String {
            use wiremock::matchers::{method, path};
            use wiremock::{Mock, MockServer, ResponseTemplate};
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/channels/1/messages"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
                .mount(&server)
                .await;
            let rest = rest_for(&server);
            let sender = DiscordSender::new(&rest);
            let url = std::env::var("VALKEY_URL")
                .expect("VALKEY_URL must be set: this test needs a REAL Valkey (no silent skip)");
            let client = redis::Client::open(url).expect("valkey url");
            let key: &'static str = Box::leak(
                format!(
                    "outbound:test:detok-{}-{}",
                    std::process::id(),
                    OP_SEQ.fetch_add(1, Ordering::SeqCst)
                )
                .into_boxed_str(),
            );
            let mut push = client
                .get_multiplexed_async_connection()
                .await
                .expect("valkey");
            let op_id = format!("detok-{}", OP_SEQ.fetch_add(1, Ordering::SeqCst));
            let entry = serde_json::json!({
                "v": 1, "op": "chat.send", "platform": "discord", "channel": "1",
                "origin_channel": "1", "text": text, "op_id": op_id, "tenant_id": tenant,
            });
            let _: () =
                redis::AsyncCommands::lpush(&mut push, key, stamp_for_test(&entry.to_string()))
                    .await
                    .expect("lpush");
            let queue = ListQueue {
                conn: connect_with_response_timeout(&client, BLOCKING_CONN_RESPONSE_TIMEOUT)
                    .await
                    .expect("valkey"),
                key,
            };
            let guard = ValkeyReplayGuard::new(
                client
                    .get_multiplexed_async_connection()
                    .await
                    .expect("valkey"),
            );
            let ctl = FakeCtl::default();
            let (tx, rx) = oneshot::channel();
            let pins = test_pin_keys();
            let run = discord_drain_loop_with(
                queue,
                &sender,
                None::<&NoExecutor>,
                &ctl,
                &guard,
                egress,
                Some(&pins),
                Backoff::new(Duration::from_secs(30)),
                rx,
            );
            let wait = async {
                for _ in 0..100 {
                    if !server
                        .received_requests()
                        .await
                        .unwrap_or_default()
                        .is_empty()
                    {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(50)).await;
                }
                let _ = tx.send(());
            };
            let _ =
                tokio::time::timeout(Duration::from_secs(10), async { tokio::join!(run, wait) })
                    .await;
            let reqs = server.received_requests().await.expect("recorded");
            assert_eq!(
                reqs.len(),
                1,
                "exactly one platform request; acks: {:?}",
                ctl.acks.lock().unwrap()
            );
            let body: serde_json::Value = serde_json::from_slice(&reqs[0].body).expect("json");
            body["content"].as_str().expect("content").to_string()
        }

        #[tokio::test]
        async fn drain_resolves_names_from_the_database() {
            let (url, admin, role) = fixture().await;
            let egress = egress_over(url.clone());
            let out = drain_one(
                &egress,
                &format!("hi {{user:{ALICE}}} and {{user:{PSEUDO}}}"),
                TENANT_A,
            )
            .await;
            teardown(&admin, &url, &role).await;
            assert_eq!(out, "hi Alice and Pseudo");
        }

        #[tokio::test]
        async fn cross_tenant_uuid_does_not_resolve() {
            let (url, admin, role) = fixture().await;
            let egress = egress_over(url.clone());
            let out = drain_one(
                &egress,
                &format!("hi {{user:{BOB_OTHER_TENANT}}}"),
                TENANT_A,
            )
            .await;
            // Same UUID under its own tenant DOES resolve (proves the row exists).
            let own = drain_one(
                &egress,
                &format!("hi {{user:{BOB_OTHER_TENANT}}}"),
                TENANT_B,
            )
            .await;
            // Pseudonym of tenant A never resolves under tenant B.
            let pseudo = drain_one(&egress, &format!("hi {{user:{PSEUDO}}}"), TENANT_B).await;
            teardown(&admin, &url, &role).await;
            assert_eq!(out, "hi Unknown User");
            assert_eq!(own, "hi Bob");
            assert_eq!(pseudo, "hi Unknown User");
        }

        #[tokio::test]
        async fn erased_user_resolves_to_unknown_user() {
            let (url, admin, role) = fixture().await;
            let egress = egress_over(url.clone());
            let before = drain_one(&egress, &format!("hi {{user:{ALICE}}}"), TENANT_A).await;
            // Erasure (hub_api data_privacy_service) deletes membership rows and scrubs the user.
            let schema = url
                .rsplit("search_path%3D")
                .next()
                .unwrap_or_default()
                .to_string();
            admin
                .execute_unprepared(&format!(
                    "DELETE FROM {schema}.community_members WHERE user_uuid = '{ALICE}'; \
                     UPDATE {schema}.hub_users SET display_name = NULL WHERE uuid = '{ALICE}';"
                ))
                .await
                .expect("erase");
            let after = drain_one(&egress, &format!("hi {{user:{ALICE}}}"), TENANT_A).await;
            let never = drain_one(&egress, &format!("hi {{user:{ERASED}}}"), TENANT_A).await;
            teardown(&admin, &url, &role).await;
            assert_eq!(before, "hi Alice");
            assert_eq!(after, "hi Unknown User");
            assert_eq!(never, "hi Unknown User");
        }

        #[tokio::test]
        async fn database_down_sends_tokens_unchanged_with_a_metric() {
            let before = crate::display_names::db_error_count();
            // Valid role URL shape, but nothing listens on the port.
            let egress = egress_over("postgresql://nobody:x@127.0.0.1:1/none".into());
            let text = format!("hi {{user:{ALICE}}}");
            let out = drain_one(&egress, &text, TENANT_A).await;
            assert_eq!(out, text);
            assert!(crate::display_names::db_error_count() > before);
        }

        #[tokio::test]
        async fn the_role_cannot_read_any_other_column() {
            let (url, admin, role) = fixture().await;
            let ro = Database::connect(url.clone()).await.expect("role connect");
            let denied = ro.execute_unprepared("SELECT email FROM hub_users").await;
            let allowed = ro
                .execute_unprepared("SELECT uuid, display_name FROM hub_users")
                .await;
            teardown(&admin, &url, &role).await;
            assert!(denied.is_err(), "email must be unreadable");
            assert!(allowed.is_ok());
        }
    }
}
