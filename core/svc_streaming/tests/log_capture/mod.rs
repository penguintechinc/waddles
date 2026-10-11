//! Shared `tracing` capture for the secret-hygiene regression tests
//! (`tests/log_secret_redaction.rs`, `tests/ingest_rtmp_publish.rs`).
//!
//! [`LogCapture::install`] installs ONE process-global TRACE-level
//! subscriber (once per test binary -- each `tests/*.rs` file is its own
//! process) that renders every event, with its fields and its enclosing
//! spans' fields, into an in-memory buffer the tests search.
//!
//! It is deliberately *global*, not a per-test `set_default` guard: tests in
//! one binary run in parallel threads, and `tracing`'s per-callsite interest
//! cache races with thread-local subscribers -- a callsite first hit on a
//! thread without a subscriber can stay disabled for the thread that has
//! one, silently dropping events (observed: INFO/DEBUG lines vanished under
//! parallel execution). A global subscriber also sees every spawned task on
//! any thread. The buffer is therefore shared between the tests of a
//! binary: every test uses secrets unique to it, so "this secret appears
//! nowhere" stays meaningful, and positive-control fragments are made
//! specific to the test (they embed its own `key_hash`).

#![allow(dead_code)]

use std::io;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use tracing_subscriber::fmt::MakeWriter;

/// Captures everything the global subscriber renders, for substring checks.
#[derive(Clone, Default)]
pub struct LogCapture(Arc<Mutex<Vec<u8>>>);

static CAPTURE: OnceLock<LogCapture> = OnceLock::new();

impl LogCapture {
    /// Installs (once per process) the global TRACE-level, ANSI-free
    /// subscriber and returns a handle on its buffer. Call it first thing
    /// in every test, before any code under test logs.
    pub fn install() -> LogCapture {
        CAPTURE
            .get_or_init(|| {
                let capture = LogCapture::default();
                let subscriber = tracing_subscriber::fmt()
                    .with_max_level(tracing::Level::TRACE)
                    .with_ansi(false)
                    .with_writer(capture.clone())
                    .finish();
                tracing::subscriber::set_global_default(subscriber)
                    .expect("no other global tracing subscriber in a log-capture test binary");
                capture
            })
            .clone()
    }

    /// Everything rendered so far.
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.0.lock().unwrap()).into_owned()
    }

    /// Polls until the capture contains `needle`, panicking (with the whole
    /// capture) after `timeout` -- handlers log from spawned tasks, so the
    /// line may land a moment after the client sees the outcome.
    pub async fn wait_for(&self, needle: &str, timeout: Duration) {
        let deadline = tokio::time::Instant::now() + timeout;
        while !self.text().contains(needle) {
            assert!(
                tokio::time::Instant::now() < deadline,
                "expected a log line containing {needle:?} within {timeout:?}; captured:\n{}",
                self.text()
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    /// Asserts none of `secrets` appears in the capture, and -- the positive
    /// control -- that the capture is non-empty and contains every
    /// `expected` fragment (the messages under test and their `key_hash`
    /// correlation ids). Prints the number of lines examined.
    pub fn assert_no_secret_leak(&self, secrets: &[&str], expected: &[&str]) {
        let text = self.text();
        let lines = text.lines().count();
        println!("log-redaction: examined {lines} captured log line(s)");
        assert!(
            lines > 0,
            "nothing was captured -- the check proved nothing"
        );
        for secret in secrets {
            assert!(!secret.is_empty());
            assert!(
                !text.contains(secret),
                "raw secret {secret:?} leaked into the logs:\n{text}"
            );
        }
        for fragment in expected {
            assert!(
                text.contains(fragment),
                "expected log content {fragment:?} not captured:\n{text}"
            );
        }
    }
}

impl io::Write for LogCapture {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl<'a> MakeWriter<'a> for LogCapture {
    type Writer = LogCapture;

    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}
