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

use crate::ingest::Backoff;
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
async fn run_loop<C, A, M>(
    connector: C,
    appender: &A,
    metrics: &M,
    keyring: &KeyRing,
    active_kid: &str,
    scope: &Scope,
    mut shutdown: oneshot::Receiver<()>,
) where
    C: GatewayConnector,
    A: EventAppender,
    M: SpineMetrics,
{
    let mut backoff = Backoff::new(Duration::from_secs(30));

    'outer: loop {
        let mut channel = tokio::select! {
            _ = &mut shutdown => return,
            result = connector.connect() => match result {
                Ok(channel) => {
                    backoff.reset();
                    tracing::info!(platform = "discord", "connected");
                    channel
                }
                Err(err) => {
                    tracing::warn!(platform = "discord", error = %err, "connect failed, retrying");
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
                recv = channel.next_chat_message() => match recv {
                    Ok(Some(msg)) => {
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
                        continue 'outer;
                    }
                    Err(err) => {
                        tracing::warn!(platform = "discord", error = %err, "recv error, reconnecting");
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
    run_loop(
        receiver, appender, metrics, keyring, active_kid, scope, shutdown,
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
}
