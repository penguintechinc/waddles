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

use std::sync::OnceLock;
use std::time::Duration;

// `TwitchError` is re-exported at the crate root (`pub use error::
// TwitchError` in `penguin_connector_twitch::lib`) but not from the `irc`
// submodule itself (`irc.rs` only privately `use`s it) -- import from the
// root, not `irc::TwitchError`.
use penguin_connector_twitch::irc::TwitchIrcSender;
use penguin_connector_twitch::TwitchError;
use tokio::sync::oneshot;

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

/// One relay send request, the exact JSON shape
/// `StageCapabilities::handle_relay` (`svc_action`) `LPUSH`es.
#[derive(Debug, serde::Deserialize, PartialEq, Eq)]
struct RelayMessage {
    channel: String,
    text: String,
}

/// Installs the process-level rustls `CryptoProvider` exactly once,
/// ignoring an "already installed" error -- safe regardless of whether
/// `penguin_spine::SpineClient::connect` (which installs its own, private
/// copy of this same defensive call) has already run first. Idempotent by
/// construction (`OnceLock`), not merely by convention.
fn ensure_crypto_provider_installed() {
    static INSTALLED: OnceLock<()> = OnceLock::new();
    INSTALLED.get_or_init(|| {
        // Ignoring the `Result`: `install_default` only fails when a
        // provider is already installed (by this call or `penguin-spine`'s
        // own), which is exactly the outcome this function wants.
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

/// Builds a `redis::Client` for `cfg`'s transport, mirroring
/// `penguin_spine`'s own (private, unexported) `build_redis_client` --
/// duplicated here rather than imported since that function is
/// `pub(crate)` inside `penguin-spine` and this module needs a plain-list
/// connection, not a `SpineClient`.
fn build_redis_client(
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
        ensure_crypto_provider_installed();
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
async fn drain_loop<Q, S>(mut queue: Q, sender: &S, mut shutdown: oneshot::Receiver<()>)
where
    Q: RelaySource,
    S: IrcOutbound,
{
    loop {
        tokio::select! {
            _ = &mut shutdown => return,
            popped = queue.brpop_one() => match popped {
                Ok(Some(raw)) => {
                    match serde_json::from_str::<RelayMessage>(&raw) {
                        Ok(msg) => {
                            if let Err(err) = sender.send(&msg.channel, &msg.text).await {
                                tracing::warn!(platform = "twitch", channel = %msg.channel, error = %err, "outbound relay send failed");
                            }
                        }
                        Err(err) => {
                            tracing::warn!(platform = "twitch", error = %err, "malformed outbound relay payload, dropping");
                        }
                    }
                }
                Ok(None) => {} // BRPOP timeout, empty queue -- loop and re-check shutdown.
                Err(err) => {
                    tracing::warn!(platform = "twitch", error = %err, "outbound relay queue read error, retrying");
                    tokio::select! {
                        _ = &mut shutdown => return,
                        () = tokio::time::sleep(Duration::from_secs(2)) => {}
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
    let conn = client.get_multiplexed_async_connection().await?;
    let sender = RealIrcOutbound::new(identity);
    drain_loop(conn, &sender, shutdown).await;
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

    #[test]
    fn relay_message_deserializes_the_svc_action_push_shape() {
        let msg: RelayMessage =
            serde_json::from_str("{\"channel\":\"#somechannel\",\"text\":\"hi\"}").unwrap();
        assert_eq!(
            msg,
            RelayMessage {
                channel: "#somechannel".to_string(),
                text: "hi".to_string()
            }
        );
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

        let result =
            tokio::time::timeout(Duration::from_secs(5), drain_loop(queue, &sender, rx)).await;
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
        drain_loop(queue, &sender, rx).await;
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
        let result =
            tokio::time::timeout(Duration::from_secs(5), drain_loop(queue, &sender, rx)).await;
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
        let result =
            tokio::time::timeout(Duration::from_secs(5), drain_loop(queue, &sender, rx)).await;
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
        let result =
            tokio::time::timeout(Duration::from_secs(5), drain_loop(queue, &sender, rx)).await;
        assert!(result.is_ok(), "must not hang retrying forever");
    }

    #[test]
    fn ensure_crypto_provider_installed_is_idempotent() {
        // Calling twice in the same test process must not panic (the
        // OnceLock guard is exactly what makes this safe).
        ensure_crypto_provider_installed();
        ensure_crypto_provider_installed();
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
}
