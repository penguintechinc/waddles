//! Credential-resolving IRC connector: every (re)connect asks hub-api for the
//! current connection secret, so no token is ever cached or held past one
//! `IrcConfig` build, and a live session is recycled before its token expires.

use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use connection_resolver::{ConnectionRef, ConnectionResolve, ConnectionSecret, ResolveError};
use penguin_connector_twitch::irc::{IrcConnection, IrcStream, TwitchIrcReceiver};
use penguin_connector_twitch::TwitchError;
use tokio::time::Instant;

use crate::ingest::twitch::{irc_config, IrcChannel, IrcConnector};
use crate::twitch_sessions::spec::SessionSpec;

/// How long before token expiry a live session is closed so the loop
/// reconnects and re-resolves a hub-refreshed token.
pub const RECYCLE_MARGIN: Duration = Duration::from_secs(300);
/// Floor on session lifetime so an expired/near-expiry token never busy-loops
/// connect -> instant recycle -> connect.
pub const MIN_SESSION: Duration = Duration::from_secs(30);

/// Seconds since the Unix epoch (0 if the clock is before it).
fn epoch_now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| i64::try_from(d.as_secs()).unwrap_or(i64::MAX))
        .unwrap_or(0)
}

/// Lifetime of a session whose token expires at `expires_at` (epoch secs):
/// `expires_at - now - RECYCLE_MARGIN`, floored at [`MIN_SESSION`]; `None`
/// (non-expiring credential) means never recycle.
fn recycle_delay(expires_at: Option<i64>, now_epoch: i64) -> Option<Duration> {
    let exp = expires_at?;
    let remaining = u64::try_from(exp.saturating_sub(now_epoch)).unwrap_or(0);
    let usable = Duration::from_secs(remaining).saturating_sub(RECYCLE_MARGIN);
    Some(usable.max(MIN_SESSION))
}

/// Maps a resolver failure to a [`TwitchError`] using the fixed class label
/// only -- never the error text -- so hub payloads cannot reach logs.
///
/// Hub trouble (`Transient`/`CircuitOpen`/`Auth`) is retryable: it says
/// nothing about the session's own validity. A refused connection
/// (`NotFound`/`NotUsable`/`Malformed`) is terminal for this session.
pub(crate) fn map_resolve_error(e: &ResolveError) -> TwitchError {
    match e {
        ResolveError::Transient(_) | ResolveError::CircuitOpen | ResolveError::Auth => {
            TwitchError::Connection(format!("resolve failed: {}", e.outcome()))
        }
        ResolveError::NotFound | ResolveError::NotUsable(_) | ResolveError::Malformed(_) => {
            TwitchError::RegistrationRejected(format!("resolve refused: {}", e.outcome()))
        }
    }
}

/// An [`IrcConnector`] that resolves the session's credentials on every
/// connect and holds none of them afterwards.
pub struct ResolvingConnector<R: ConnectionResolve> {
    resolver: Arc<R>,
    spec: SessionSpec,
    host: String,
    port: u16,
    use_tls: bool,
    timeout: Duration,
    clock: fn() -> i64,
}

impl<R: ConnectionResolve> ResolvingConnector<R> {
    /// Builds a connector for one session; `timeout` bounds each resolve call.
    pub fn new(
        resolver: Arc<R>,
        spec: SessionSpec,
        host: impl Into<String>,
        port: u16,
        use_tls: bool,
        timeout: Duration,
    ) -> Self {
        Self::with_clock(resolver, spec, host, port, use_tls, timeout, epoch_now)
    }

    /// As [`Self::new`] with an injectable epoch-seconds clock (tests).
    pub(crate) fn with_clock(
        resolver: Arc<R>,
        spec: SessionSpec,
        host: impl Into<String>,
        port: u16,
        use_tls: bool,
        timeout: Duration,
        clock: fn() -> i64,
    ) -> Self {
        Self {
            resolver,
            spec,
            host: host.into(),
            port,
            use_tls,
            timeout,
            clock,
        }
    }
}

impl<R: ConnectionResolve> fmt::Debug for ResolvingConnector<R> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ResolvingConnector")
            .field("spec", &self.spec)
            .field("host", &self.host)
            .field("port", &self.port)
            .field("use_tls", &self.use_tls)
            .finish_non_exhaustive()
    }
}

impl<R: ConnectionResolve> IrcConnector for ResolvingConnector<R> {
    type Channel = DeadlineChannel<IrcConnection<IrcStream>>;

    /// Resolves, builds the `IrcConfig`, drops the resolved secret, then
    /// connects; the returned channel closes itself before token expiry.
    async fn connect(&self) -> Result<Self::Channel, TwitchError> {
        let r = ConnectionRef {
            community_id: self.spec.community_id,
            connection_id: self.spec.connection_id,
        };
        let resolved = tokio::time::timeout(self.timeout, self.resolver.resolve(r))
            .await
            .map_err(|_elapsed| TwitchError::Connection("resolve failed: timeout".into()))?
            .map_err(|e| map_resolve_error(&e))?;
        let expires_at = resolved.expires_at;
        let cfg = match &resolved.secret {
            ConnectionSecret::OAuthAccessToken { access_token } => irc_config(
                &self.host,
                self.port,
                &self.spec.bot_login,
                &self.spec.channel,
                access_token.expose(),
                self.use_tls,
            ),
            _ => {
                return Err(TwitchError::RegistrationRejected(
                    "resolved secret is not an oauth access token".into(),
                ))
            }
        };
        drop(resolved);
        let recycle_at = recycle_delay(expires_at, (self.clock)()).map(|d| Instant::now() + d);
        let inner = TwitchIrcReceiver::new(cfg).connect().await?;
        Ok(DeadlineChannel::new(inner, recycle_at))
    }
}

/// Wraps a channel and reports a clean close once `recycle_at` passes, so the
/// run loop reconnects (and therefore re-resolves) instead of holding a
/// session authenticated by an expiring token.
pub struct DeadlineChannel<C: IrcChannel> {
    inner: C,
    recycle_at: Option<Instant>,
}

impl<C: IrcChannel> DeadlineChannel<C> {
    /// Wraps `inner`; `None` means never recycle.
    pub fn new(inner: C, recycle_at: Option<Instant>) -> Self {
        Self { inner, recycle_at }
    }
}

impl<C: IrcChannel> IrcChannel for DeadlineChannel<C> {
    /// Delegates to the inner channel; `Ok(None)` (clean close) at the deadline.
    async fn recv(
        &mut self,
    ) -> Result<Option<penguin_connector_twitch::irc::ChatMessage>, TwitchError> {
        match self.recycle_at {
            None => self.inner.recv().await,
            Some(at) => match tokio::time::timeout_at(at, self.inner.recv()).await {
                Ok(r) => r,
                Err(_elapsed) => Ok(None),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use connection_resolver::{
        BindingPage, ConnectionKind, Endpoint, NotUsableReason, ResolvedConnection, SecretString,
    };
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::TcpListener;

    const TOKEN: &str = "tok_SECRET_abc123";
    const LOGIN: &str = "somestreamer";
    const BOT: &str = "somebotnick";

    #[derive(Clone, Copy)]
    enum Mode {
        Oauth,
        Bearer,
        Fail,
    }

    struct FakeResolver {
        calls: AtomicUsize,
        mode: Mode,
        expires_at: Option<i64>,
    }

    impl FakeResolver {
        fn new(mode: Mode, expires_at: Option<i64>) -> Arc<Self> {
            Arc::new(Self {
                calls: AtomicUsize::new(0),
                mode,
                expires_at,
            })
        }
    }

    impl ConnectionResolve for FakeResolver {
        async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let secret = match self.mode {
                Mode::Fail => return Err(ResolveError::Transient("boom")),
                Mode::Oauth => ConnectionSecret::OAuthAccessToken {
                    access_token: SecretString::new(TOKEN.to_string()),
                },
                Mode::Bearer => ConnectionSecret::Bearer {
                    token: SecretString::new(TOKEN.to_string()),
                },
            };
            Ok(ResolvedConnection {
                connection_id: r.connection_id,
                community_id: r.community_id,
                provider: "twitch".into(),
                kind: ConnectionKind::PlatformOauth,
                endpoint: Endpoint::default(),
                secret,
                expires_at: self.expires_at,
            })
        }

        async fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            _a: i64,
            _l: u32,
        ) -> Result<BindingPage, ResolveError> {
            Err(ResolveError::Malformed("unused"))
        }
    }

    fn spec() -> SessionSpec {
        SessionSpec {
            community_id: 7,
            connection_id: 9,
            channel: LOGIN.into(),
            bot_login: BOT.into(),
        }
    }

    /// Fake IRC server: per connection, forwards the first line (PASS) and
    /// answers `001` so registration completes, then holds the socket open.
    async fn irc_server() -> (u16, tokio::sync::mpsc::UnboundedReceiver<String>) {
        let l = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let port = l.local_addr().expect("addr").port();
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(async move {
            loop {
                let Ok((mut s, _)) = l.accept().await else {
                    return;
                };
                let tx = tx.clone();
                tokio::spawn(async move {
                    let (rd, mut wr) = s.split();
                    let mut lines = BufReader::new(rd).lines();
                    if let Ok(Some(first)) = lines.next_line().await {
                        let _ = tx.send(first);
                    }
                    let _ = wr.write_all(b":tmi.twitch.tv 001 bot :Welcome\r\n").await;
                    while let Ok(Some(_)) = lines.next_line().await {}
                });
            }
        });
        (port, rx)
    }

    fn connector(r: Arc<FakeResolver>, port: u16) -> ResolvingConnector<FakeResolver> {
        ResolvingConnector::new(r, spec(), "127.0.0.1", port, false, Duration::from_secs(5))
    }

    #[tokio::test]
    async fn every_connect_calls_resolve_again() {
        let (port, _rx) = irc_server().await;
        let r = FakeResolver::new(Mode::Oauth, None);
        let c = connector(r.clone(), port);
        let mut chans = Vec::new();
        for _ in 0..3 {
            chans.push(c.connect().await.expect("connect"));
        }
        assert_eq!(r.calls.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn resolved_token_reaches_irc_pass_with_oauth_prefix() {
        let (port, mut rx) = irc_server().await;
        let r = FakeResolver::new(Mode::Oauth, None);
        let _chan = connector(r, port).connect().await.expect("connect");
        let first = rx.recv().await.expect("pass line");
        assert_eq!(first, format!("PASS oauth:{TOKEN}"));
    }

    #[test]
    fn map_resolve_error_table() {
        for e in [
            ResolveError::Transient("x"),
            ResolveError::CircuitOpen,
            ResolveError::Auth,
        ] {
            let m = map_resolve_error(&e);
            assert!(matches!(m, TwitchError::Connection(_)), "{e:?}");
            assert!(m.is_retryable(), "{e:?}");
        }
        for e in [
            ResolveError::NotFound,
            ResolveError::NotUsable(NotUsableReason::Disabled),
            ResolveError::Malformed("x"),
        ] {
            let m = map_resolve_error(&e);
            assert!(matches!(m, TwitchError::RegistrationRejected(_)), "{e:?}");
            assert!(!m.is_retryable(), "{e:?}");
        }
    }

    #[tokio::test]
    async fn non_oauth_secret_is_rejected_loudly() {
        let r = FakeResolver::new(Mode::Bearer, None);
        let err = connector(r, 1).connect().await.err().expect("must fail");
        assert!(matches!(err, TwitchError::RegistrationRejected(_)));
        assert!(!err.is_retryable());
    }

    #[tokio::test]
    async fn resolver_failure_is_retryable_connection_error() {
        let r = FakeResolver::new(Mode::Fail, None);
        let err = connector(r, 1).connect().await.err().expect("must fail");
        assert!(err.is_retryable());
    }

    struct Pending;
    impl IrcChannel for Pending {
        async fn recv(
            &mut self,
        ) -> Result<Option<penguin_connector_twitch::irc::ChatMessage>, TwitchError> {
            std::future::pending().await
        }
    }

    #[tokio::test(start_paused = true)]
    async fn deadline_channel_closes_cleanly_at_recycle_point() {
        let start = Instant::now();
        let mut ch = DeadlineChannel::new(Pending, Some(start + Duration::from_secs(1)));
        let got = ch.recv().await.expect("clean close, not an error");
        assert!(got.is_none());
        let waited = start.elapsed();
        assert!(waited >= Duration::from_secs(1) && waited < Duration::from_secs(2));
    }

    #[test]
    fn expires_at_none_or_past_margin_floor_never_busy_loops() {
        let now = 1_000_000;
        assert_eq!(recycle_delay(None, now), None);
        // Already expired, expiring inside the margin: floored, never zero.
        assert_eq!(recycle_delay(Some(now - 10), now), Some(MIN_SESSION));
        assert_eq!(recycle_delay(Some(now + 301), now), Some(MIN_SESSION));
        // Plenty of life left: expiry minus margin.
        assert_eq!(
            recycle_delay(Some(now + 3600), now),
            Some(Duration::from_secs(3600) - RECYCLE_MARGIN)
        );
    }

    #[test]
    fn resolving_connector_error_and_debug_carry_no_token() {
        let c = connector(FakeResolver::new(Mode::Oauth, None), 1);
        let dbg = format!("{c:?}");
        for e in [
            ResolveError::NotFound,
            ResolveError::NotUsable(NotUsableReason::TokenExpired),
            ResolveError::Auth,
            ResolveError::Transient("x"),
            ResolveError::CircuitOpen,
            ResolveError::Malformed("x"),
        ] {
            let out = format!(
                "{dbg} {:?} {}",
                map_resolve_error(&e),
                map_resolve_error(&e)
            );
            for secret in [TOKEN, LOGIN, BOT] {
                assert!(!out.contains(secret), "leaked {secret}: {out}");
            }
        }
    }
}
