//! Twitch IRC chat receiver supervisor: connects via
//! `penguin_connector_twitch::irc`, receives `PRIVMSG` chat lines, skips
//! this bot's own echoed messages (`crate::normalize::is_self_message`,
//! ported from `receivers/twitch_irc.py`), normalizes each remaining
//! message into a `PlatformEvent` (`crate::normalize`), and publishes it
//! onto the spine (`crate::publish::publish_event`) -- reconnecting with
//! backoff on any transport failure or unexpected close, never exiting the
//! process. This is the primary e2e path (a real Twitch chat message
//! reaching an `XADD`), see `crate::lib`'s module doc.
//!
//! EventSub (the webhook alternative to IRC) is a separate `// TODO(M5)`
//! seam -- this module is IRC-only, matching the M5 milestone's stated
//! priority order.

use std::time::Duration;

// `TwitchError` is re-exported at the crate root only (`irc.rs` privately
// `use`s it, no `pub use` inside that submodule) -- import from the root.
use penguin_connector_twitch::irc::{
    ChatMessage, IrcConfig, IrcConnection, IrcStream, TwitchIrcReceiver,
};
use penguin_connector_twitch::TwitchError;
use penguin_spine::{KeyRing, Scope, SpineMetrics};
use tokio::sync::oneshot;

use crate::ingest::Backoff;
use crate::publish::{deterministic_workstream_id, publish_event, EventAppender};

/// Abstraction over a live, joined IRC connection's `recv()` -- lets
/// [`run_loop`] be driven by a scripted fake in tests instead of a real
/// socket. Implemented for the real `IrcConnection<IrcStream>` as a pure
/// delegation, matching `core/svc_process/src/spine.rs`'s `StreamReader`
/// precedent (a generic bound, not `dyn`: native async-fn-in-traits isn't
/// `dyn`-safe without boxing, and nothing here needs runtime polymorphism
/// across implementations).
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait IrcChannel: Send {
    /// Reads the next chat message, `Ok(None)` on a clean close.
    async fn recv(&mut self) -> Result<Option<ChatMessage>, TwitchError>;
}

impl IrcChannel for IrcConnection<IrcStream> {
    async fn recv(&mut self) -> Result<Option<ChatMessage>, TwitchError> {
        IrcConnection::recv(self).await
    }
}

/// Abstraction over establishing a new IRC connection -- lets the
/// reconnect loop's *connect* step be faked too, so [`run_loop`]'s
/// reconnect-with-backoff control flow is fully unit-tested without a live
/// network or a real Twitch account.
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait IrcConnector: Send + Sync {
    /// The channel type this connector yields once connected.
    type Channel: IrcChannel;
    /// Establishes one new connection (connect + register + join).
    async fn connect(&self) -> Result<Self::Channel, TwitchError>;
}

impl IrcConnector for TwitchIrcReceiver {
    type Channel = IrcConnection<IrcStream>;
    async fn connect(&self) -> Result<Self::Channel, TwitchError> {
        TwitchIrcReceiver::connect(self).await
    }
}

/// Runs the connect/receive/publish/reconnect loop until `shutdown`
/// resolves. Split out from [`run`] (which builds the real connector,
/// keyring, and `SpineClient`) so this control flow is exercised in tests
/// against a scripted fake connector -- no live socket, no live Valkey.
#[allow(clippy::too_many_arguments)] // every parameter is independently varied across tests; a params struct would just move the same count elsewhere
async fn run_loop<C, A, M>(
    connector: C,
    configured_nick: &str,
    appender: &A,
    metrics: &M,
    keyring: &KeyRing,
    active_kid: &str,
    scope: &Scope,
    source_id: String,
    mut shutdown: oneshot::Receiver<()>,
) where
    C: IrcConnector,
    A: EventAppender,
    M: SpineMetrics,
{
    let workstream_id = deterministic_workstream_id(&source_id);
    let mut backoff = Backoff::new(Duration::from_secs(30));

    'outer: loop {
        let mut channel = tokio::select! {
            _ = &mut shutdown => return,
            result = connector.connect() => match result {
                Ok(channel) => {
                    backoff.reset();
                    tracing::info!(platform = "twitch", source_id = %source_id, "connected");
                    channel
                }
                Err(err) => {
                    tracing::warn!(platform = "twitch", source_id = %source_id, error = %err, "connect failed, retrying");
                    let delay = backoff.delay();
                    tokio::select! {
                        _ = &mut shutdown => return,
                        () = tokio::time::sleep(delay) => {}
                    }
                    continue 'outer;
                }
            }
        };

        loop {
            tokio::select! {
                _ = &mut shutdown => return,
                recv = channel.recv() => match recv {
                    Ok(Some(msg)) => {
                        if crate::normalize::is_self_message(&msg.sender, configured_nick) {
                            continue;
                        }
                        let event = crate::normalize::normalize_twitch_irc(&msg);
                        if let Err(err) = publish_event(
                            appender,
                            metrics,
                            keyring,
                            active_kid,
                            scope,
                            &source_id,
                            &workstream_id,
                            None,
                            event,
                        )
                        .await
                        {
                            tracing::error!(platform = "twitch", source_id = %source_id, error = %err, "failed to publish chat event");
                        }
                    }
                    Ok(None) => {
                        tracing::warn!(platform = "twitch", source_id = %source_id, "connection closed, reconnecting");
                        continue 'outer;
                    }
                    Err(err) => {
                        tracing::warn!(platform = "twitch", source_id = %source_id, error = %err, "recv error, reconnecting");
                        continue 'outer;
                    }
                }
            }
        }
    }
}

/// Derives the stable ingest `source_id` for a Twitch channel (used for
/// both the Valkey stream key and the deterministic workstream id,
/// PA-WORKSTREAM) -- `#channel` and `channel` both normalize to the same
/// id, so a config typo with/without the leading `#` never splits one
/// channel across two workstreams.
#[must_use]
pub fn source_id(channel: &str) -> String {
    format!(
        "tw-{}",
        channel.trim_start_matches('#').to_ascii_lowercase()
    )
}

/// Builds the real `penguin_connector_twitch::irc::IrcConfig` for this
/// service's configured Twitch channel. `oauth_token` is prefixed with
/// `oauth:` if not already present -- Twitch's IRC `PASS` line requires
/// that exact prefix, matching `receivers/twitch_irc.py`'s own convention.
/// Shared by [`run`] (the receive side) and `crate::outbound` (the
/// outbound relay sender), which build a fresh one per outbound message
/// (a different `channel` each time) using the same host/nick/oauth/tls.
#[must_use]
pub fn irc_config(
    host: &str,
    port: u16,
    nick: &str,
    channel: &str,
    oauth_token: &str,
    use_tls: bool,
) -> IrcConfig {
    let password = if oauth_token.starts_with("oauth:") {
        oauth_token.to_string()
    } else {
        format!("oauth:{oauth_token}")
    };
    let mut cfg = IrcConfig::new(host, nick, channel);
    cfg.port = port;
    cfg.password = Some(password);
    cfg.use_tls = use_tls;
    cfg
}

/// Connects the real Twitch IRC receiver and a real `SpineClient`-backed
/// publisher, then runs [`run_loop`] until `shutdown` resolves. This is the
/// function `crate::lib::try_start_twitch_irc` spawns as its own background
/// task.
#[allow(clippy::too_many_arguments)] // mirrors run_loop's own justification
pub async fn run<A: EventAppender, M: SpineMetrics>(
    irc_cfg: IrcConfig,
    channel: &str,
    configured_nick: &str,
    appender: &A,
    metrics: &M,
    keyring: &KeyRing,
    active_kid: &str,
    scope: &Scope,
    shutdown: oneshot::Receiver<()>,
) {
    let receiver = TwitchIrcReceiver::new(irc_cfg);
    let sid = source_id(channel);
    run_loop(
        receiver,
        configured_nick,
        appender,
        metrics,
        keyring,
        active_kid,
        scope,
        sid,
        shutdown,
    )
    .await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use penguin_spine::SpineError;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    fn test_keyring() -> KeyRing {
        KeyRing::new(vec![("k1".to_string(), vec![9u8; 32])])
    }

    #[derive(Default)]
    struct RecordingAppender {
        calls: Mutex<Vec<(String, penguin_spine::StageEnvelope)>>,
    }

    impl EventAppender for RecordingAppender {
        async fn append(
            &self,
            stream: &str,
            env: &penguin_spine::StageEnvelope,
        ) -> Result<String, SpineError> {
            self.calls
                .lock()
                .unwrap()
                .push((stream.to_string(), env.clone()));
            Ok("1-0".to_string())
        }
    }

    #[derive(Default)]
    struct NoopTestMetrics;
    impl SpineMetrics for NoopTestMetrics {}

    /// A fake channel yielding a fixed sequence of `recv()` results, then
    /// closing (`Ok(None)`) forever.
    struct FakeChannel {
        messages: Vec<ChatMessage>,
        idx: usize,
    }

    impl IrcChannel for FakeChannel {
        async fn recv(&mut self) -> Result<Option<ChatMessage>, TwitchError> {
            if self.idx < self.messages.len() {
                let msg = self.messages[self.idx].clone();
                self.idx += 1;
                Ok(Some(msg))
            } else {
                Ok(None)
            }
        }
    }

    /// A fake connector: fails `fail_first_n` times, then succeeds exactly
    /// once (yielding a [`FakeChannel`] over `messages`), then fails every
    /// call after that. The "fails forever after the one success" tail is
    /// deliberate, not an oversight: once `messages` is exhausted the real
    /// `run_loop` reconnects immediately, and if a fake connector kept
    /// succeeding forever it would replay the exact same `messages` from
    /// index 0 every time -- an unbounded hot loop with no real `.await`
    /// suspension point for `tokio::select!` to ever fairly observe
    /// `shutdown` against (this was a real, reproduced bug: the test hung
    /// and grew unbounded memory before this fix). Failing on every
    /// subsequent call forces the real backoff-sleep path
    /// (`Backoff::delay`), which contains a genuine timer -- exactly the
    /// suspension point `shutdown` needs to win the race deterministically.
    struct FakeConnector {
        messages: Vec<ChatMessage>,
        fail_first_n: AtomicUsize,
        succeeded_once: std::sync::atomic::AtomicBool,
    }

    impl FakeConnector {
        fn new(messages: Vec<ChatMessage>, fail_first_n: usize) -> Self {
            Self {
                messages,
                fail_first_n: AtomicUsize::new(fail_first_n),
                succeeded_once: std::sync::atomic::AtomicBool::new(false),
            }
        }
    }

    impl IrcConnector for FakeConnector {
        type Channel = FakeChannel;
        async fn connect(&self) -> Result<Self::Channel, TwitchError> {
            let remaining = self.fail_first_n.load(Ordering::SeqCst);
            if remaining > 0 {
                self.fail_first_n.store(remaining - 1, Ordering::SeqCst);
                return Err(TwitchError::Connection("simulated failure".to_string()));
            }
            if self
                .succeeded_once
                .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
                .is_err()
            {
                return Err(TwitchError::Connection(
                    "simulated permanent failure after first success".to_string(),
                ));
            }
            Ok(FakeChannel {
                messages: self.messages.clone(),
                idx: 0,
            })
        }
    }

    fn test_msg(sender: &str, text: &str) -> ChatMessage {
        ChatMessage {
            channel: "#somechannel".to_string(),
            sender: sender.to_string(),
            text: text.to_string(),
            tags: None,
        }
    }

    #[tokio::test]
    async fn publishes_every_message_before_the_reconnect_that_fails_permanently() {
        // Deliberately never sends `shutdown`: with a *pre-sent* shutdown,
        // `tokio::select!`'s first poll races an already-ready `shutdown`
        // against an already-ready `connector.connect()` and may resolve
        // to `shutdown` before the connector is ever tried at all -- a
        // real, reproduced flake (this exact test occasionally observed
        // zero published messages). Not sending `shutdown` removes that
        // race entirely: the fake connector succeeds deterministically on
        // its first (only) call, publishes both messages, then the
        // channel closes and every reconnect after that fails permanently
        // (see `FakeConnector`'s doc comment) -- `tokio::time::timeout`
        // bounds the resulting infinite backoff loop from the outside.
        let connector = FakeConnector::new(
            vec![test_msg("someuser", "hello"), test_msg("someuser", "world")],
            0,
        );
        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        let result = tokio::time::timeout(
            Duration::from_secs(10),
            run_loop(
                connector,
                "waddlebot",
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                "tw-somechannel".to_string(),
                rx,
            ),
        )
        .await;
        assert!(
            result.is_err(),
            "run_loop only returns via shutdown, which this test never sends"
        );

        let calls = appender.calls.lock().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(
            calls[0].0,
            "waddles:t:acme:c:_tenant:src:twitch:tw-somechannel:events"
        );
        assert_eq!(
            calls[0]
                .1
                .event
                .payload
                .get("text")
                .and_then(|v| v.as_str()),
            Some("hello")
        );
        assert_eq!(calls[0].1.workstream_id, calls[1].1.workstream_id);
    }

    #[tokio::test]
    async fn skips_self_messages_without_publishing() {
        // No pre-sent shutdown -- see the previous test's doc comment for
        // why that would race the very first connect attempt.
        let connector = FakeConnector::new(
            vec![
                test_msg("WaddleBot", "echoed"),
                test_msg("someuser", "real"),
            ],
            0,
        );
        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        let result = tokio::time::timeout(
            Duration::from_secs(10),
            run_loop(
                connector,
                "waddlebot",
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                "tw-somechannel".to_string(),
                rx,
            ),
        )
        .await;
        assert!(result.is_err());

        let calls = appender.calls.lock().unwrap();
        assert_eq!(calls.len(), 1, "the self-echoed message must not publish");
        assert_eq!(
            calls[0]
                .1
                .event
                .payload
                .get("text")
                .and_then(|v| v.as_str()),
            Some("real")
        );
    }

    #[tokio::test]
    async fn reconnects_after_a_connect_failure_and_still_publishes() {
        let connector = FakeConnector::new(vec![test_msg("someuser", "hi after reconnect")], 2);
        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        let result = tokio::time::timeout(
            Duration::from_secs(10),
            run_loop(
                connector,
                "waddlebot",
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                "tw-somechannel".to_string(),
                rx,
            ),
        )
        .await;
        // run_loop never returns Ok on its own (it only returns on
        // shutdown, which never fires here) -- the timeout elapsing is
        // expected; what matters is that the message published before the
        // timeout despite the two simulated connect failures.
        assert!(result.is_err(), "run_loop only returns via shutdown");
        assert_eq!(appender.calls.lock().unwrap().len(), 1);
    }

    #[test]
    fn source_id_normalizes_leading_hash_and_case() {
        assert_eq!(source_id("#SomeChannel"), "tw-somechannel");
        assert_eq!(source_id("somechannel"), "tw-somechannel");
    }

    #[test]
    fn irc_config_prefixes_oauth_when_missing() {
        let cfg = irc_config(
            "irc.chat.twitch.tv",
            6697,
            "waddlebot",
            "somechannel",
            "abc123",
            true,
        );
        assert_eq!(cfg.password.as_deref(), Some("oauth:abc123"));
        assert_eq!(cfg.host, "irc.chat.twitch.tv");
        assert_eq!(cfg.port, 6697);
        assert!(cfg.use_tls);
    }

    #[test]
    fn irc_config_does_not_double_prefix_oauth() {
        let cfg = irc_config(
            "irc.chat.twitch.tv",
            6697,
            "waddlebot",
            "somechannel",
            "oauth:abc123",
            true,
        );
        assert_eq!(cfg.password.as_deref(), Some("oauth:abc123"));
    }
}
