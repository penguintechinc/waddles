//! Discord Gateway chat receiver supervisor: connects via
//! `penguin_connector_discord::gateway`, receives `MESSAGE_CREATE`
//! dispatches, normalizes each into a `PlatformEvent` (`crate::normalize`),
//! and publishes it onto the spine (`crate::publish::publish_event`) --
//! reconnecting with backoff on any transport failure, heartbeat-ack
//! timeout, or session invalidation, never exiting the process. Secondary
//! e2e path per the M5 milestone's stated priority order.
//!
//! Unlike Twitch (one channel per configured receiver), a single Discord
//! bot connection can see messages from every guild it has been invited
//! to -- `source_id`/`workstream_id` are therefore derived **per message**
//! from that message's own `guild_id` (PA-WORKSTREAM: same `source_id` in,
//! same `workstream_id` out), not fixed once at connect time.

use std::time::Duration;

// `DiscordError` is re-exported at the crate root only (`gateway.rs`
// privately `use`s it, no `pub use` inside that submodule) -- import from
// the root.
use penguin_connector_discord::gateway::{
    ChatMessage, DiscordGatewayReceiver, GatewayConfig, GatewaySession,
};
use penguin_connector_discord::DiscordError;
use penguin_spine::{KeyRing, Scope, SpineMetrics};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::oneshot;

use crate::ingest::{Backoff, STABILITY_WINDOW};
use crate::publish::{deterministic_workstream_id, publish_event, EventAppender};

/// Abstraction over a live, identified Gateway session's
/// `next_chat_message()` -- lets [`run_loop`] be driven by a scripted fake
/// in tests instead of a real WebSocket. Implemented for the real
/// `GatewaySession<S>` as a pure delegation.
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait GatewayChannel: Send {
    /// Reads the next chat message, `Ok(None)` on a clean close.
    async fn next_chat_message(&mut self) -> Result<Option<ChatMessage>, DiscordError>;
}

impl<S> GatewayChannel for GatewaySession<S>
where
    S: AsyncRead + AsyncWrite + Unpin + Send,
{
    async fn next_chat_message(&mut self) -> Result<Option<ChatMessage>, DiscordError> {
        GatewaySession::next_chat_message(self).await
    }
}

/// Abstraction over establishing a new Gateway connection -- lets the
/// reconnect loop's *connect* step be faked too, so [`run_loop`]'s
/// reconnect-with-backoff control flow is fully unit-tested without a live
/// network or a real Discord bot token.
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait GatewayConnector: Send + Sync {
    /// The channel type this connector yields once connected+identified.
    type Channel: GatewayChannel;
    /// Establishes one new connection (connect + `HELLO`/`IDENTIFY`).
    async fn connect(&self) -> Result<Self::Channel, DiscordError>;
}

impl GatewayConnector for DiscordGatewayReceiver {
    type Channel = GatewaySession<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;
    async fn connect(&self) -> Result<Self::Channel, DiscordError> {
        DiscordGatewayReceiver::connect(self).await
    }
}

/// Derives the per-message ingest `source_id` from a Discord message's own
/// `guild_id` (`None` for a DM) -- PA-WORKSTREAM.
#[must_use]
pub fn source_id(guild_id: Option<&str>) -> String {
    match guild_id {
        Some(id) => format!("dg-{id}"),
        None => "dg-dm".to_string(),
    }
}

/// Runs the connect/receive/publish/reconnect loop until `shutdown`
/// resolves. Split out from [`run`] (which builds the real connector and
/// `SpineClient`) so this control flow is exercised in tests against a
/// scripted fake connector -- no live socket, no live Discord bot.
///
/// Reconnect discipline (the fix for the reconnect-storm incident, see
/// [`Backoff`]'s doc comment for the full story):
/// - `backoff.delay()` is applied -- and awaited, racing `shutdown` -- on
///   *every* reconnect path: a failed connect, a closed channel (`Ok(None)`),
///   and a recv error, not just some of them.
/// - `backoff.reset()` fires only once the session is confirmed stable:
///   either it delivers a chat message (Discord only ever dispatches
///   `MESSAGE_CREATE` after `READY`) or it simply survives, error-free,
///   for [`STABILITY_WINDOW`] -- never on the raw connect succeeding.
/// - A [`DiscordError`] that `!is_retryable()` (a protocol-level surprise
///   the gateway is never going to un-surprise us on, e.g. `UnexpectedOpcode`/
///   `Decode` during the handshake) stops this loop entirely instead of
///   backing off forever against a connection that can never succeed. The
///   crate's own `ResumeRequested` (opcode 7, session resumable) vs
///   `SessionInvalidated` (opcode 9, must re-`IDENTIFY`) distinction is
///   surfaced via distinct log lines below; both still fall through to a
///   fresh `connector.connect()` because this crate does not yet implement
///   `OP_RESUME` (see [`penguin_connector_discord::gateway::DiscordError::ResumeRequested`]'s
///   own doc comment) -- there is no different *action* to take yet, only
///   a different thing to tell the operator.
#[allow(clippy::too_many_arguments)] // every parameter is independently varied across tests; a params struct would just move the same count elsewhere -- matches twitch.rs's own precedent
async fn run_loop<C, A, M>(
    connector: C,
    appender: &A,
    metrics: &M,
    keyring: &KeyRing,
    active_kid: &str,
    scope: &Scope,
    mut backoff: Backoff,
    mut shutdown: oneshot::Receiver<()>,
) where
    C: GatewayConnector,
    A: EventAppender,
    M: SpineMetrics,
{
    'outer: loop {
        let mut channel = tokio::select! {
            _ = &mut shutdown => return,
            result = connector.connect() => match result {
                Ok(channel) => {
                    // Deliberately NOT `backoff.reset()` -- a successful
                    // HELLO/IDENTIFY handshake is not evidence the session
                    // is usable. See `Backoff`'s doc comment.
                    tracing::info!(platform = "discord", "connected, awaiting session stability");
                    channel
                }
                Err(err) => {
                    tracing::warn!(platform = "discord", error = %err, "connect failed, retrying");
                    if !err.is_retryable() {
                        tracing::error!(platform = "discord", error = %err, "non-retryable connect failure, stopping Discord ingest");
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
                    tracing::info!(platform = "discord", "session stable (stability window elapsed), backoff reset");
                }
                recv = channel.next_chat_message() => match recv {
                    Ok(Some(msg)) => {
                        if !stable {
                            stable = true;
                            backoff.reset();
                            tracing::info!(platform = "discord", "session stable (message received), backoff reset");
                        }
                        let sid = source_id(msg.guild_id.as_deref());
                        let workstream_id = deterministic_workstream_id(&sid);
                        let event = crate::normalize::normalize_discord(&msg);
                        if let Err(err) = publish_event(
                            appender,
                            metrics,
                            keyring,
                            active_kid,
                            scope,
                            &sid,
                            &workstream_id,
                            None,
                            event,
                        )
                        .await
                        {
                            tracing::error!(platform = "discord", source_id = %sid, error = %err, "failed to publish chat event");
                        }
                    }
                    Ok(None) => {
                        tracing::warn!(platform = "discord", "connection closed, reconnecting");
                        let delay = backoff.delay();
                        tokio::select! {
                            _ = &mut shutdown => return,
                            () = tokio::time::sleep(delay) => {}
                        }
                        continue 'outer;
                    }
                    Err(err) => {
                        match &err {
                            DiscordError::ResumeRequested => {
                                tracing::warn!(platform = "discord", "gateway requested reconnect (resumable session), reconnecting");
                            }
                            DiscordError::SessionInvalidated => {
                                tracing::warn!(platform = "discord", "gateway invalidated the session, re-identifying");
                            }
                            _ => {
                                tracing::warn!(platform = "discord", error = %err, "recv error, reconnecting");
                            }
                        }
                        if !err.is_retryable() {
                            tracing::error!(platform = "discord", error = %err, "non-retryable gateway error, stopping Discord ingest");
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

/// Connects the real Discord Gateway receiver and a real
/// `SpineClient`-backed publisher, then runs [`run_loop`] until `shutdown`
/// resolves. This is the function `crate::lib::try_start_discord` spawns as
/// its own background task.
pub async fn run<A: EventAppender, M: SpineMetrics>(
    gateway_cfg: GatewayConfig,
    appender: &A,
    metrics: &M,
    keyring: &KeyRing,
    active_kid: &str,
    scope: &Scope,
    shutdown: oneshot::Receiver<()>,
) {
    let receiver = DiscordGatewayReceiver::new(gateway_cfg);
    let backoff = Backoff::new(Duration::from_secs(30));
    run_loop(
        receiver, appender, metrics, keyring, active_kid, scope, backoff, shutdown,
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

    struct FakeChannel {
        messages: Vec<ChatMessage>,
        idx: usize,
    }

    impl GatewayChannel for FakeChannel {
        async fn next_chat_message(&mut self) -> Result<Option<ChatMessage>, DiscordError> {
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
    /// once, then fails every call after that -- see
    /// `crate::ingest::twitch::tests::FakeConnector`'s doc comment for why
    /// "succeed forever" is a reproduced hang/unbounded-memory bug, not a
    /// simplification: it would replay the same `messages` from index 0
    /// every reconnect, an unbounded hot loop with no real `.await`
    /// suspension point for `tokio::select!` to ever fairly observe
    /// `shutdown` against.
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

    impl GatewayConnector for FakeConnector {
        type Channel = FakeChannel;
        async fn connect(&self) -> Result<Self::Channel, DiscordError> {
            let remaining = self.fail_first_n.load(Ordering::SeqCst);
            if remaining > 0 {
                self.fail_first_n.store(remaining - 1, Ordering::SeqCst);
                return Err(DiscordError::Transport("simulated failure".to_string()));
            }
            if self
                .succeeded_once
                .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
                .is_err()
            {
                return Err(DiscordError::Transport(
                    "simulated permanent failure after first success".to_string(),
                ));
            }
            Ok(FakeChannel {
                messages: self.messages.clone(),
                idx: 0,
            })
        }
    }

    fn test_msg(guild_id: Option<&str>, content: &str) -> ChatMessage {
        ChatMessage {
            guild_id: guild_id.map(str::to_string),
            channel_id: "222".to_string(),
            message_id: "333".to_string(),
            author_id: "444".to_string(),
            author_username: "someuser".to_string(),
            content: content.to_string(),
        }
    }

    #[tokio::test]
    async fn publishes_every_message_before_the_reconnect_that_fails_permanently() {
        // No pre-sent shutdown -- see `crate::ingest::twitch::tests`'s
        // twin test for why a *pre-sent* shutdown races the very first
        // connect attempt (a real, reproduced flake: zero messages
        // published). The fake connector succeeds deterministically once,
        // publishes both messages, then every reconnect after the channel
        // closes fails permanently -- `tokio::time::timeout` bounds the
        // resulting infinite backoff loop from the outside.
        let connector = FakeConnector::new(
            vec![
                test_msg(Some("111"), "hello"),
                test_msg(Some("111"), "world"),
            ],
            0,
        );
        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        let result = tokio::time::timeout(
            Duration::from_secs(10),
            run_loop(
                connector,
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
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
            "waddles:t:acme:c:_tenant:src:discord:dg-111:events"
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
    }

    #[tokio::test]
    async fn dm_messages_use_the_dm_source_id() {
        // No pre-sent shutdown -- see the earlier test's doc comment.
        let connector = FakeConnector::new(vec![test_msg(None, "hi from dm")], 0);
        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        let _ = tokio::time::timeout(
            Duration::from_secs(10),
            run_loop(
                connector,
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;

        let calls = appender.calls.lock().unwrap();
        assert_eq!(
            calls[0].0,
            "waddles:t:acme:c:_tenant:src:discord:dg-dm:events"
        );
    }

    #[tokio::test]
    async fn reconnects_after_a_connect_failure_and_still_publishes() {
        let connector = FakeConnector::new(vec![test_msg(Some("111"), "hi after reconnect")], 2);
        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        let result = tokio::time::timeout(
            Duration::from_secs(10),
            run_loop(
                connector,
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert!(result.is_err(), "run_loop only returns via shutdown");
        assert_eq!(appender.calls.lock().unwrap().len(), 1);
    }

    #[test]
    fn source_id_uses_dg_dm_for_no_guild() {
        assert_eq!(source_id(None), "dg-dm");
    }

    #[test]
    fn source_id_uses_the_guild_id_when_present() {
        assert_eq!(source_id(Some("12345")), "dg-12345");
    }

    // `Backoff`'s own unit tests (ceiling doubling, reset, jitter bounds)
    // now live in `crate::ingest::tests` -- this is the shared type's
    // single canonical home (also used by `ingest::twitch` and
    // `crate::outbound`), not re-tested per call site.

    /// The regression test for the reconnect-storm incident: a fake
    /// gateway that connects successfully every time (the HELLO/IDENTIFY
    /// handshake always succeeds) but whose channel closes immediately --
    /// before `READY`, before any message, before `STABILITY_WINDOW`
    /// elapses. This is exactly the shape of the real incident (a
    /// connection Discord accepts and then immediately drops). The old
    /// code reset `backoff` to ~1s on every one of these "successful"
    /// connects; this test proves the fixed loop instead keeps escalating
    /// the delay -- and never drops back to the base -- for as long as the
    /// churn continues.
    #[tokio::test(start_paused = true)]
    async fn backoff_grows_and_never_resets_on_repeated_pre_ready_connect_churn() {
        struct ImmediateCloseConnector {
            connect_times: std::sync::Arc<Mutex<Vec<tokio::time::Instant>>>,
        }

        impl GatewayConnector for ImmediateCloseConnector {
            type Channel = FakeChannel;
            async fn connect(&self) -> Result<Self::Channel, DiscordError> {
                self.connect_times
                    .lock()
                    .unwrap()
                    .push(tokio::time::Instant::now());
                // Closes immediately: `next_chat_message` sees an empty
                // `messages` list and returns `Ok(None)` on the very first
                // call, with no message and no stability window elapsed.
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

        // Virtual 10 minutes -- with time paused (`start_paused = true`),
        // this elapses instantly in wall-clock terms: the runtime
        // auto-advances straight to each pending timer's deadline whenever
        // it would otherwise idle, so this drives many reconnect cycles
        // without a real multi-minute test.
        let _ = tokio::time::timeout(
            Duration::from_secs(600),
            run_loop(
                connector,
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
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
        // Bounded attempt rate: the real incident produced hundreds of
        // reconnects; the fixed, capped-at-30s backoff must keep this
        // nowhere close, even given 600 virtual seconds to work with.
        assert!(
            times.len() < 100,
            "reconnect attempt count must stay bounded by the capped backoff, got {}",
            times.len()
        );

        let deltas: Vec<Duration> = times.windows(2).map(|w| w[1] - w[0]).collect();

        // Independently recompute the ceiling sequence a *correct*
        // (never-reset) loop would have used, from the same seed --
        // deterministic, no randomness involved. Ceilings must never
        // shrink, and must reach the 30s cap given this much churn: this
        // is the "backoff grows" assertion, made on the ceiling rather
        // than the jittered draw itself (a single draw can be small by
        // chance even at a high ceiling -- see `Backoff::delay`'s doc).
        let mut ceiling_probe = Backoff::seeded(Duration::from_secs(30), SEED);
        let ceilings: Vec<Duration> = (0..deltas.len())
            .map(|_| {
                let c = ceiling_probe.ceiling();
                ceiling_probe.delay();
                c
            })
            .collect();
        for pair in ceilings.windows(2) {
            assert!(
                pair[1] >= pair[0],
                "ceiling must never shrink mid-churn (would indicate a hidden reset): {ceilings:?}"
            );
        }
        assert_eq!(
            *ceilings.last().unwrap(),
            Duration::from_secs(30),
            "ceiling must reach the 30s cap given this much churn: {ceilings:?}"
        );
        for (i, delay) in deltas.iter().enumerate() {
            assert!(
                *delay <= ceilings[i],
                "delay {delay:?} at step {i} must not exceed its ceiling {:?}",
                ceilings[i]
            );
        }

        // Exact-sequence proof that `backoff.reset()` was never invoked:
        // an independently-seeded `Backoff`, driven the same number of
        // times with no reset in between, must reproduce the observed
        // deltas bit-for-bit (same seed, same deterministic PRNG, same
        // call count). If the loop had reset the backoff on any of these
        // connects, the real deltas would diverge from this reference
        // sequence starting at the reset point.
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
    async fn non_retryable_connect_error_stops_the_loop_instead_of_backing_off_forever() {
        /// Always fails with a non-retryable error (mirrors a fatal
        /// protocol surprise during the handshake, e.g. `UnexpectedOpcode`)
        /// -- never succeeds, never returns a retryable error.
        struct AlwaysNonRetryableConnector;
        impl GatewayConnector for AlwaysNonRetryableConnector {
            type Channel = FakeChannel;
            async fn connect(&self) -> Result<Self::Channel, DiscordError> {
                Err(DiscordError::UnexpectedOpcode(99))
            }
        }

        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        // A short real-time timeout proves `run_loop` returns *on its
        // own*, promptly: the pre-fix behavior of always retrying would
        // still be looping (backing off forever against a connection that
        // can never succeed) and this timeout would elapse instead.
        let result = tokio::time::timeout(
            Duration::from_secs(5),
            run_loop(
                AlwaysNonRetryableConnector,
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert!(
            result.is_ok(),
            "a non-retryable connect error must make run_loop return promptly, not keep backing off forever"
        );
        assert!(appender.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn non_retryable_recv_error_stops_the_loop_instead_of_backing_off_forever() {
        struct OneShotConnector;
        struct NonRetryableChannel;

        impl GatewayChannel for NonRetryableChannel {
            async fn next_chat_message(&mut self) -> Result<Option<ChatMessage>, DiscordError> {
                Err(DiscordError::UnexpectedOpcode(7))
            }
        }

        impl GatewayConnector for OneShotConnector {
            type Channel = NonRetryableChannel;
            async fn connect(&self) -> Result<Self::Channel, DiscordError> {
                Ok(NonRetryableChannel)
            }
        }

        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        let result = tokio::time::timeout(
            Duration::from_secs(5),
            run_loop(
                OneShotConnector,
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                Backoff::new(Duration::from_secs(30)),
                rx,
            ),
        )
        .await;
        assert!(
            result.is_ok(),
            "a non-retryable recv error must make run_loop return promptly, not keep reconnecting forever"
        );
    }

    /// Proves the other half of the stability contract: once a session
    /// *does* prove itself (here, by delivering a chat message -- Discord
    /// only ever dispatches `MESSAGE_CREATE` after `READY`), the backoff
    /// resets, so the *next* disconnect starts escalating from the base
    /// delay again instead of continuing to climb from wherever pre-
    /// stability churn had left it.
    #[tokio::test(start_paused = true)]
    async fn a_delivered_message_resets_backoff_so_the_next_churn_starts_from_the_base_again() {
        struct ScriptedConnector {
            connect_times: std::sync::Arc<Mutex<Vec<tokio::time::Instant>>>,
            stable_message: ChatMessage,
        }

        impl GatewayConnector for ScriptedConnector {
            type Channel = FakeChannel;
            async fn connect(&self) -> Result<Self::Channel, DiscordError> {
                let mut times = self.connect_times.lock().unwrap();
                let call_index = times.len();
                times.push(tokio::time::Instant::now());
                drop(times);
                // The 3rd connect (0-indexed 2) delivers one message
                // before its channel closes -- the connection that proves
                // stability. Every other connect closes immediately with
                // no message (churn).
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
            stable_message: test_msg(Some("111"), "proves stability"),
        };
        let appender = RecordingAppender::default();
        let (_tx, rx) = oneshot::channel();

        const SEED: u64 = 0xFEED_FACE_1234_5678;
        let backoff = Backoff::seeded(Duration::from_secs(30), SEED);

        let _ = tokio::time::timeout(
            Duration::from_secs(600),
            run_loop(
                connector,
                &appender,
                &NoopTestMetrics,
                &test_keyring(),
                "k1",
                &Scope::new("acme", None),
                backoff,
                rx,
            ),
        )
        .await;

        assert_eq!(
            appender.calls.lock().unwrap().len(),
            1,
            "the stability-proving message must have been published"
        );

        let times = connect_times.lock().unwrap();
        assert!(
            times.len() >= 4,
            "expected at least 4 connects, got {}",
            times.len()
        );
        let deltas: Vec<Duration> = times.windows(2).map(|w| w[1] - w[0]).collect();

        // Independently replicate the exact call sequence a correct loop
        // makes: two churn delays (pre-stability), then the message
        // arrives and resets the backoff, then one more delay (the
        // post-reset close) -- same seed, so this reproduces the real
        // sequence bit-for-bit if (and only if) the reset actually fired
        // between the 2nd and 3rd delay.
        let mut expected_backoff = Backoff::seeded(Duration::from_secs(30), SEED);
        let expected_churn_1 = expected_backoff.delay();
        let expected_churn_2 = expected_backoff.delay();
        expected_backoff.reset();
        let expected_post_reset = expected_backoff.delay();

        assert_eq!(deltas[0], expected_churn_1);
        assert_eq!(deltas[1], expected_churn_2);
        assert_eq!(
            deltas[2], expected_post_reset,
            "the delay right after the stability-proving message must reflect a reset backoff, not a continued climb"
        );
        assert!(
            deltas[2] <= Duration::from_secs(1),
            "post-reset delay {:?} must be bounded by the base 1s ceiling, not the pre-reset ~4s ceiling",
            deltas[2]
        );
    }
}
