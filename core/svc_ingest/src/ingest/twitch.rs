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

use crate::ingest::{Backoff, STABILITY_WINDOW};
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
///
/// Reconnect discipline mirrors `ingest::discord::run_loop` exactly (see
/// `crate::ingest::Backoff`'s doc comment for the incident that drove
/// this):
/// - `backoff.delay()` is applied -- and awaited, racing `shutdown` -- on
///   *every* reconnect path: a failed connect, a closed channel
///   (`Ok(None)`), and a recv error, not just some of them.
/// - `backoff.reset()` fires only once the connection is confirmed
///   healthy: it delivers a line (a `PRIVMSG`, even a self-echoed one --
///   receiving anything at all proves registration+join actually worked)
///   or it simply survives, error-free, for [`STABILITY_WINDOW`] -- never
///   on the raw `connect_and_register` succeeding.
/// - A [`TwitchError`] that `!is_retryable()` (`RegistrationRejected` --
///   bad nick/password, an auth failure) stops this loop entirely instead
///   of backing off forever against a connection that can never succeed.
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
    mut backoff: Backoff,
    mut shutdown: oneshot::Receiver<()>,
) where
    C: IrcConnector,
    A: EventAppender,
    M: SpineMetrics,
{
    let workstream_id = deterministic_workstream_id(&source_id);

    'outer: loop {
        let mut channel = tokio::select! {
            _ = &mut shutdown => return,
            result = connector.connect() => match result {
                Ok(channel) => {
                    // Deliberately NOT `backoff.reset()` -- a successful
                    // connect+register+join is not evidence the session is
                    // usable. See `crate::ingest::Backoff`'s doc comment.
                    tracing::info!(platform = "twitch", source_id = %source_id, "connected, awaiting session stability");
                    channel
                }
                Err(err) => {
                    tracing::warn!(platform = "twitch", source_id = %source_id, error = %err, "connect failed, retrying");
                    if !err.is_retryable() {
                        tracing::error!(platform = "twitch", source_id = %source_id, error = %err, "non-retryable connect failure, stopping Twitch ingest");
                        return;
                    }
                    let delay = backoff.delay();
                    tokio::select! {
                        _ = &mut shutdown => return,
                        () = tokio::time::sleep(delay) => {}
                    }
                    continue 'outer;
                }
            }
        };

        let mut stable = false;
        let stability_timer = tokio::time::sleep(STABILITY_WINDOW);
        tokio::pin!(stability_timer);

        loop {
            tokio::select! {
                _ = &mut shutdown => return,
                () = &mut stability_timer, if !stable => {
                    stable = true;
                    backoff.reset();
                    tracing::info!(platform = "twitch", source_id = %source_id, "session stable (stability window elapsed), backoff reset");
                }
                recv = channel.recv() => match recv {
                    Ok(Some(msg)) => {
                        if !stable {
                            stable = true;
                            backoff.reset();
                            tracing::info!(platform = "twitch", source_id = %source_id, "session stable (line received), backoff reset");
                        }
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
                        let delay = backoff.delay();
                        tokio::select! {
                            _ = &mut shutdown => return,
                            () = tokio::time::sleep(delay) => {}
                        }
                        continue 'outer;
                    }
                    Err(err) => {
                        tracing::warn!(platform = "twitch", source_id = %source_id, error = %err, "recv error, reconnecting");
                        if !err.is_retryable() {
                            tracing::error!(platform = "twitch", source_id = %source_id, error = %err, "non-retryable gateway error, stopping Twitch ingest");
                            return;
                        }
                        let delay = backoff.delay();
                        tokio::select! {
                            _ = &mut shutdown => return,
                            () = tokio::time::sleep(delay) => {}
                        }
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
    let backoff = Backoff::new(Duration::from_secs(30));
    run_loop(
        receiver,
        configured_nick,
        appender,
        metrics,
        keyring,
        active_kid,
        scope,
        sid,
        backoff,
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
                Backoff::new(Duration::from_secs(30)),
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
                Backoff::new(Duration::from_secs(30)),
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
                Backoff::new(Duration::from_secs(30)),
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

    // `Backoff`'s own unit tests live in `crate::ingest::tests` -- the
    // shared type's single canonical home, not re-tested per call site.

    /// The regression test for the reconnect-storm class of bug (the same
    /// shape as the live Discord incident this fix responds to, see
    /// `ingest::discord`'s own regression test): a fake IRC server that
    /// completes registration+join every time but whose connection closes
    /// immediately after -- before any `PRIVMSG`, before `STABILITY_WINDOW`
    /// elapses. Proves the backoff keeps escalating (and never drops back
    /// to the base) for as long as the churn continues.
    #[tokio::test(start_paused = true)]
    async fn backoff_grows_and_never_resets_on_repeated_pre_stability_connect_churn() {
        struct ImmediateCloseConnector {
            connect_times: std::sync::Arc<Mutex<Vec<tokio::time::Instant>>>,
        }

        impl IrcConnector for ImmediateCloseConnector {
            type Channel = FakeChannel;
            async fn connect(&self) -> Result<Self::Channel, TwitchError> {
                self.connect_times
                    .lock()
                    .unwrap()
                    .push(tokio::time::Instant::now());
                Ok(FakeChannel {
                    messages: vec![],
                    idx: 0,
                })
            }
        }

        let connect_times: std::sync::Arc<Mutex<Vec<tokio::time::Instant>>> =
            std::sync::Arc::new(Mutex::new(Vec::new()));
        let connector = ImmediateCloseConnector {
            connect_times: connect_times.clone(),
        };
        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        const SEED: u64 = 0x5EED_C0FF_EE12_3456;
        let backoff = Backoff::seeded(Duration::from_secs(30), SEED);

        let _ = tokio::time::timeout(
            Duration::from_secs(600),
            run_loop(
                connector,
                "waddlebot",
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                "tw-somechannel".to_string(),
                backoff,
                rx,
            ),
        )
        .await;

        let times = connect_times.lock().unwrap();
        assert!(
            times.len() >= 8,
            "expected at least 8 connect attempts within the virtual window, got {}",
            times.len()
        );
        assert!(
            times.len() < 100,
            "reconnect attempt count must stay bounded by the capped backoff, got {}",
            times.len()
        );

        let deltas: Vec<Duration> = times.windows(2).map(|w| w[1] - w[0]).collect();
        let mut expected_backoff = Backoff::seeded(Duration::from_secs(30), SEED);
        let expected: Vec<Duration> = (0..deltas.len())
            .map(|_| expected_backoff.delay())
            .collect();
        assert_eq!(
            deltas, expected,
            "observed reconnect delays must match the never-reset reference sequence"
        );
    }

    #[tokio::test]
    async fn non_retryable_registration_rejected_stops_the_loop_instead_of_backing_off_forever() {
        struct AlwaysRejectedConnector;
        impl IrcConnector for AlwaysRejectedConnector {
            type Channel = FakeChannel;
            async fn connect(&self) -> Result<Self::Channel, TwitchError> {
                Err(TwitchError::RegistrationRejected(
                    "Login authentication failed".to_string(),
                ))
            }
        }

        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        let result = tokio::time::timeout(
            Duration::from_secs(5),
            run_loop(
                AlwaysRejectedConnector,
                "waddlebot",
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                "tw-somechannel".to_string(),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert!(
            result.is_ok(),
            "a non-retryable registration-rejected error must make run_loop return promptly, not keep backing off forever"
        );
        assert!(appender.calls.lock().unwrap().is_empty());
    }

    /// Proves the other half of the stability contract: once the
    /// connection *does* prove itself (here, by delivering a `PRIVMSG`),
    /// the backoff resets, so the *next* disconnect starts escalating from
    /// the base delay again instead of continuing to climb from wherever
    /// pre-stability churn had left it.
    #[tokio::test(start_paused = true)]
    async fn a_delivered_line_resets_backoff_so_the_next_churn_starts_from_the_base_again() {
        struct ScriptedConnector {
            connect_times: std::sync::Arc<Mutex<Vec<tokio::time::Instant>>>,
            stable_message: ChatMessage,
        }

        impl IrcConnector for ScriptedConnector {
            type Channel = FakeChannel;
            async fn connect(&self) -> Result<Self::Channel, TwitchError> {
                let mut times = self.connect_times.lock().unwrap();
                let call_index = times.len();
                times.push(tokio::time::Instant::now());
                drop(times);
                if call_index == 2 {
                    Ok(FakeChannel {
                        messages: vec![self.stable_message.clone()],
                        idx: 0,
                    })
                } else {
                    Ok(FakeChannel {
                        messages: vec![],
                        idx: 0,
                    })
                }
            }
        }

        let connect_times: std::sync::Arc<Mutex<Vec<tokio::time::Instant>>> =
            std::sync::Arc::new(Mutex::new(Vec::new()));
        let connector = ScriptedConnector {
            connect_times: connect_times.clone(),
            stable_message: test_msg("someuser", "proves stability"),
        };
        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        const SEED: u64 = 0xFEED_FACE_1234_5678;
        let backoff = Backoff::seeded(Duration::from_secs(30), SEED);

        let _ = tokio::time::timeout(
            Duration::from_secs(600),
            run_loop(
                connector,
                "waddlebot",
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                "tw-somechannel".to_string(),
                backoff,
                rx,
            ),
        )
        .await;

        assert_eq!(
            appender.calls.lock().unwrap().len(),
            1,
            "the stability-proving line must have been published"
        );

        let times = connect_times.lock().unwrap();
        assert!(
            times.len() >= 4,
            "expected at least 4 connects, got {}",
            times.len()
        );
        let deltas: Vec<Duration> = times.windows(2).map(|w| w[1] - w[0]).collect();

        let mut expected_backoff = Backoff::seeded(Duration::from_secs(30), SEED);
        let expected_churn_1 = expected_backoff.delay();
        let expected_churn_2 = expected_backoff.delay();
        expected_backoff.reset();
        let expected_post_reset = expected_backoff.delay();

        assert_eq!(deltas[0], expected_churn_1);
        assert_eq!(deltas[1], expected_churn_2);
        assert_eq!(
            deltas[2], expected_post_reset,
            "the delay right after the stability-proving line must reflect a reset backoff, not a continued climb"
        );
        assert!(
            deltas[2] <= Duration::from_secs(1),
            "post-reset delay {:?} must be bounded by the base 1s ceiling, not the pre-reset ~4s ceiling",
            deltas[2]
        );
    }
}
