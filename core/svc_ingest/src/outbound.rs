//! Twitch outbound relay drain (spec S4.1/S5.5): `BRPOP`s the plain Valkey
//! list `svc_action::capabilities::outbound_relay_queue_key("twitch")`
//! (`waddles:transport:irc:twitch:outbound`) that `svc_action`'s `relay`
//! host capability `LPUSH`es onto, and sends each queued
//! `{"channel", "text"}` message over a fresh
//! `penguin_connector_twitch::irc::TwitchIrcSender` connection.
//!
//! **Credential resolution.** The outbound credential is this process's
//! own configured Twitch IRC identity (`TWITCH_IRC_NICK`/
//! `TWITCH_IRC_OAUTH_TOKEN`, the same one the receive side uses,
//! `crate::ingest::twitch`) -- never a per-message tenant/community
//! lookup. `svc_action::capabilities::outbound_relay_queue_key`'s own doc
//! comment is explicit about why: "One key per provider (not per tenant/
//! community), matching the inbound side's single-socket model" -- there
//! is exactly one configured Twitch bot account per `svc_ingest` replica
//! today, so "resolve the credential from the process's own configuration"
//! (spec S4.3's platform-credential rule) and "resolve it from the
//! envelope's tenant/community" collapse to the same single answer under
//! the current single-socket-per-provider architecture. A genuine per-
//! tenant multi-account model would need a per-tenant queue key and
//! per-tenant credential store neither `svc_action`'s push side nor this
//! drain implements yet -- flagged as a follow-up, not silently assumed
//! away.
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

use crate::ingest::Backoff;
use crate::outbound_ops::{
    dispatch_to, parse_outbound, parse_outbound_entry, route, DiscordSender, SenderError,
    TwitchSender,
};

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
) -> Result<redis::aio::MultiplexedConnection, redis::RedisError> {
    let config = redis::AsyncConnectionConfig::new().set_response_timeout(Some(response_timeout));
    client
        .get_multiplexed_async_connection_with_config(&config)
        .await
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

/// This process's own Twitch IRC identity, reused for every outbound
/// relay send (see this module's "Credential resolution" doc).
pub struct TwitchOutboundIdentity {
    pub host: String,
    pub port: u16,
    pub nick: String,
    pub oauth_token: String,
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

impl RelaySource for redis::aio::MultiplexedConnection {
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

/// Abstraction over sending one chat message to a Twitch channel -- lets
/// [`drain_loop`] be tested without a live Twitch IRC connection.
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait IrcOutbound: Send + Sync {
    /// Connects fresh, registers, `PRIVMSG`s `channel`, and `QUIT`s
    /// (Twitch chat sends never hold a persistent connection --
    /// `penguin_connector_twitch::irc`'s own module doc).
    async fn send(&self, channel: &str, text: &str) -> Result<(), TwitchError>;
}

/// The production [`IrcOutbound`]: builds a fresh `IrcConfig` (this
/// process's own identity + the message's own `channel`) and sends via
/// `TwitchIrcSender` -- one connection per outbound message, matching
/// Twitch chat's real semantics (`penguin_connector_twitch::irc`'s own
/// "sender: opens a fresh IRC connection per outbound chat message" doc).
pub struct RealIrcOutbound {
    identity: TwitchOutboundIdentity,
}

impl RealIrcOutbound {
    #[must_use]
    pub fn new(identity: TwitchOutboundIdentity) -> Self {
        Self { identity }
    }
}

impl IrcOutbound for RealIrcOutbound {
    async fn send(&self, channel: &str, text: &str) -> Result<(), TwitchError> {
        let cfg = crate::ingest::twitch::irc_config(
            &self.identity.host,
            self.identity.port,
            &self.identity.nick,
            channel,
            &self.identity.oauth_token,
            self.identity.use_tls,
        );
        TwitchIrcSender::new(cfg).send(text).await
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
async fn drain_loop<Q, S>(
    mut queue: Q,
    sender: &S,
    mut backoff: Backoff,
    mut shutdown: oneshot::Receiver<()>,
) where
    Q: RelaySource,
    S: IrcOutbound,
{
    loop {
        tokio::select! {
            _ = &mut shutdown => return,
            popped = queue.brpop_one() => match popped {
                Ok(Some(raw)) => {
                    backoff.reset();
                    match parse_outbound(&raw, "twitch") {
                        Ok(action) => {
                            let twitch = TwitchSender::new(sender);
                            if let Err(err) = route("twitch", &action, &twitch, &DiscordSender::unconfigured()).await {
                                match err {
                                    // A new op with no implementation yet is an
                                    // actionable failure, not degraded service.
                                    SenderError::Unsupported { .. } => tracing::error!(platform = "twitch", op = action.op(), error = %err, "outbound op unsupported, dropping"),
                                    SenderError::Failed { .. } => tracing::warn!(platform = "twitch", op = action.op(), error = %err, "outbound relay send failed"),
                                    SenderError::Rejected { .. } => tracing::warn!(platform = "twitch", op = action.op(), error = %err, "outbound op refused by policy"),
                                }
                            }
                        }
                        Err(err) => {
                            tracing::warn!(platform = "twitch", error = %err, "malformed outbound relay payload, dropping");
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
                    tracing::warn!(platform = "twitch", error = %err, "outbound relay queue read error, retrying");
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
pub async fn run(
    spine_cfg: &penguin_spine::SpineConfig,
    identity: TwitchOutboundIdentity,
    shutdown: oneshot::Receiver<()>,
) -> Result<(), redis::RedisError> {
    let client = build_redis_client(spine_cfg)?;
    let conn = connect_with_response_timeout(&client, BLOCKING_CONN_RESPONSE_TIMEOUT).await?;
    let sender = RealIrcOutbound::new(identity);
    let backoff = Backoff::new(Duration::from_secs(30));
    drain_loop(conn, &sender, backoff, shutdown).await;
    Ok(())
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
    conn: redis::aio::MultiplexedConnection,
}

impl ValkeyDrainControl {
    /// Wraps a dedicated control connection.
    #[must_use]
    pub fn new(conn: redis::aio::MultiplexedConnection) -> Self {
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
    conn: redis::aio::MultiplexedConnection,
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
fn ack_payload(result: &Result<(), SenderError>) -> String {
    match result {
        Ok(()) => serde_json::json!({"ok": true}),
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
async fn handle_discord_entry<C: DrainControl>(raw: &str, sender: &DiscordSender<'_>, ctl: &C) {
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
    let result = dispatch_to(sender, &entry.action).await;
    match &result {
        Ok(()) => {}
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
async fn discord_drain_loop<Q, C>(
    mut queue: Q,
    sender: &DiscordSender<'_>,
    ctl: &C,
    mut backoff: Backoff,
    mut shutdown: oneshot::Receiver<()>,
) where
    Q: RelaySource,
    C: DrainControl,
{
    loop {
        tokio::select! {
            _ = &mut shutdown => return,
            popped = queue.brpop_one() => match popped {
                Ok(Some(raw)) => {
                    backoff.reset();
                    handle_discord_entry(&raw, sender, ctl).await;
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
    shutdown: oneshot::Receiver<()>,
) -> Result<(), redis::RedisError> {
    let client = build_redis_client(spine_cfg)?;
    let list_conn = connect_with_response_timeout(&client, BLOCKING_CONN_RESPONSE_TIMEOUT).await?;
    let ctl = ValkeyDrainControl::new(
        connect_with_response_timeout(&client, CONTROL_CONN_RESPONSE_TIMEOUT).await?,
    );
    let queue = ListQueue {
        conn: list_conn,
        key: DISCORD_OUTBOUND_QUEUE_KEY,
    };
    let sender = DiscordSender::new(rest);
    tracing::info!(
        platform = "discord",
        "discord outbound drain running; advertising readiness"
    );
    tokio::select! {
        () = heartbeat_loop(&ctl) => {}
        () = discord_drain_loop(
            queue,
            &sender,
            &ctl,
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

    impl RelaySource for FakeQueue {
        async fn brpop_one(&mut self) -> Result<Option<String>, String> {
            let i = self.idx.fetch_add(1, Ordering::SeqCst);
            match self.payloads.get(i) {
                Some(result) => result.clone(),
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
        fail: bool,
    }

    impl IrcOutbound for RecordingSender {
        async fn send(&self, channel: &str, text: &str) -> Result<(), TwitchError> {
            if self.fail {
                return Err(TwitchError::Connection("simulated failure".to_string()));
            }
            self.calls
                .lock()
                .unwrap()
                .push((channel.to_string(), text.to_string()));
            Ok(())
        }
    }

    /// Backward compat: a legacy text-only entry (queued by a pre-upgrade
    /// svc_action) and the new versioned envelope both still send chat.
    #[tokio::test]
    async fn legacy_and_versioned_chat_send_payloads_both_send() {
        for raw in [
            "{\"channel\":\"#c\",\"text\":\"hi\"}",
            "{\"v\":1,\"op\":\"chat.send\",\"platform\":\"twitch\",\"channel\":\"#c\",\"text\":\"hi\"}",
        ] {
            let queue = FakeQueue {
                payloads: vec![Ok(Some(raw.to_string()))],
                idx: AtomicUsize::new(0),
            };
            let sender = RecordingSender::default();
            let (_tx, rx) = oneshot::channel();
            let _ = tokio::time::timeout(
                Duration::from_secs(5),
                drain_loop(queue, &sender, Backoff::new(Duration::from_secs(30)), rx),
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
                drain_loop(queue, &sender, Backoff::new(Duration::from_secs(30)), rx),
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
            payloads: vec![Ok(Some("{\"channel\":\"#c\",\"text\":\"hi\"}".to_string()))],
            idx: AtomicUsize::new(0),
        };
        let sender = RecordingSender::default();
        let (_tx, rx) = oneshot::channel();

        let result = tokio::time::timeout(
            Duration::from_secs(5),
            drain_loop(queue, &sender, Backoff::new(Duration::from_secs(30)), rx),
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
        drain_loop(queue, &sender, Backoff::new(Duration::from_secs(30)), rx).await;
        assert!(sender.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn send_failure_is_logged_and_does_not_stop_the_loop() {
        let queue = FakeQueue {
            payloads: vec![Ok(Some("{\"channel\":\"#c\",\"text\":\"hi\"}".to_string()))],
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
            drain_loop(queue, &sender, Backoff::new(Duration::from_secs(30)), rx),
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
            drain_loop(queue, &sender, Backoff::new(Duration::from_secs(30)), rx),
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
            drain_loop(queue, &sender, Backoff::new(Duration::from_secs(30)), rx),
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
            drain_loop(queue, &sender, backoff, rx),
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
                    r#"{"v":1,"op":"chat.send","platform":"twitch","channel":"1","text":"x"}"#
                        .to_string(),
                )),
                Ok(Some(
                    r#"{"v":1,"op":"chat.send","platform":"discord","channel":"1","text":"x","op_id":"send-1"}"#
                        .to_string(),
                )),
                Ok(None),
                Ok(Some(
                    r#"{"v":1,"op":"chat.delete","platform":"discord","channel":"1","message_id":"2","op_id":"del-1"}"#
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
                r#"{"op":"chat.delete","channel":"1","message_id":"2","op_id":"u-1"}"#.to_string(),
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

    /// An op with no `op_id` (legacy / fire-and-forget producer) executes and
    /// posts nothing.
    #[tokio::test]
    async fn discord_drain_posts_no_ack_without_an_op_id() {
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
        drive_discord_drain(
            Some(&rest),
            &ctl,
            vec![Ok(Some(
                r#"{"op":"chat.delete","channel":"1","message_id":"2"}"#.to_string(),
            ))],
        )
        .await;
        assert!(ctl.acks.lock().unwrap().is_empty());
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
                r#"{"op":"chat.delete","channel":"1","message_id":"2","op_id":"old-1","exp_ms":1}"#
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
            r#"{"op":"chat.delete","channel":"1","message_id":"2","op_id":"x-1"}"#.to_string();
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
        assert_eq!(ack_payload(&Ok(())), r#"{"ok":true}"#);
        let rejected = ack_payload(&Err(SenderError::Rejected {
            platform: "discord",
            op: "dm.send",
            reason: "not_in_community",
        }));
        assert_eq!(rejected, r#"{"ok":false,"code":"not_in_community"}"#);
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
}
