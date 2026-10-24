//! Regression test: the per-session Twitch IRC loop must never put the
//! channel login (a username, i.e. PII) into a log line or span field --
//! only the numeric `SessionLabel` ids. Lives in its own integration-test
//! binary (own process) because capturing via a thread-local tracing
//! subscriber is racy against concurrent unit tests that hit the same
//! callsites with no subscriber installed.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use penguin_connector_twitch::irc::ChatMessage;
use penguin_connector_twitch::TwitchError;
use penguin_spine::{KeyRing, Scope, SpineError, SpineMetrics, StageEnvelope};
use svc_ingest::ingest::twitch::{run_with_connector, IrcChannel, IrcConnector, SessionLabel};
use svc_ingest::publish::EventAppender;
use tokio::sync::oneshot;

struct NoopAppender;
impl EventAppender for NoopAppender {
    async fn append(&self, _: &str, _: &StageEnvelope) -> Result<String, SpineError> {
        Ok("1-0".to_string())
    }
}

struct NoopMetrics;
impl SpineMetrics for NoopMetrics {}

struct OneMsgChannel(bool, bool);
impl IrcChannel for OneMsgChannel {
    async fn recv(&mut self) -> Result<Option<ChatMessage>, TwitchError> {
        if self.0 && self.1 {
            // After the message: a recv error whose Display embeds the channel.
            self.1 = false;
            return Err(TwitchError::Connection(
                "read failed on #secretchan (tw-secretchan)".to_string(),
            ));
        }
        if self.0 {
            return Ok(None);
        }
        self.0 = true;
        Ok(Some(ChatMessage {
            channel: "#secretchan".to_string(),
            sender: "someuser".to_string(),
            text: "hi".to_string(),
            tags: None,
        }))
    }
}

/// Fails the first connect, succeeds twice (session 2: a message then a recv
/// error whose Display embeds the channel; session 3: a message then a clean
/// close), then
/// fails forever. Every error Display embeds the channel, so any
/// `error = %err` log field would leak it.
struct ScriptedConnector(Mutex<u32>);
impl IrcConnector for ScriptedConnector {
    type Channel = OneMsgChannel;
    async fn connect(&self) -> Result<Self::Channel, TwitchError> {
        let mut n = self.0.lock().expect("lock");
        *n += 1;
        if *n == 2 || *n == 3 {
            // Session 2 ends in a recv error, session 3 in a clean close.
            Ok(OneMsgChannel(false, *n == 2))
        } else {
            Err(TwitchError::Connection(
                "connect to #secretchan failed (tw-secretchan)".to_string(),
            ))
        }
    }
}

struct FieldText(String);
impl tracing::field::Visit for FieldText {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        self.0.push_str(&format!("{}={:?} ", field.name(), value));
    }
}

/// Minimal capturing subscriber: records every event's/span's rendered fields.
struct Capture(Arc<Mutex<Vec<String>>>);
impl tracing::Subscriber for Capture {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, attrs: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        let mut v = FieldText(String::new());
        attrs.record(&mut v);
        self.0.lock().expect("lock").push(v.0);
        tracing::span::Id::from_u64(1)
    }
    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn event(&self, event: &tracing::Event<'_>) {
        let mut v = FieldText(String::new());
        event.record(&mut v);
        self.0.lock().expect("lock").push(v.0);
    }
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
}

#[tokio::test(start_paused = true)]
async fn run_loop_logs_carry_no_channel_login() {
    let lines = Arc::new(Mutex::new(Vec::new()));
    let _guard = tracing::subscriber::set_default(Capture(lines.clone()));

    let (tx, rx) = oneshot::channel();
    let keyring = KeyRing::new(vec![("k1".to_string(), vec![9u8; 32])]);
    let scope = Scope::new("acme", None);
    let fut = run_with_connector(
        ScriptedConnector(Mutex::new(0)),
        "waddlebot",
        "#secretchan",
        SessionLabel {
            community_id: 42,
            connection_id: 5,
        },
        &NoopAppender,
        &NoopMetrics,
        &keyring,
        "k1",
        &scope,
        rx,
    );
    tokio::pin!(fut);
    let _ = tokio::time::timeout(Duration::from_secs(120), &mut fut).await;
    tx.send(()).expect("loop still alive to receive shutdown");
    tokio::time::timeout(Duration::from_secs(5), &mut fut)
        .await
        .expect("loop stops on shutdown");

    let captured = lines.lock().expect("lock").join("\n");
    assert!(
        captured.contains("connect failed")
            && captured.contains("recv error")
            && captured.contains("connection closed"),
        "expected connect-fail and closed paths to log, got: {captured}"
    );
    assert!(
        !captured.contains("secretchan"),
        "channel login leaked: {captured}"
    );
    assert!(
        !captured.contains("tw-secretchan"),
        "source id leaked: {captured}"
    );
    assert!(
        captured.contains("community_id=42"),
        "label missing: {captured}"
    );
    assert!(
        captured.contains("connection_id=5"),
        "label missing: {captured}"
    );
}
