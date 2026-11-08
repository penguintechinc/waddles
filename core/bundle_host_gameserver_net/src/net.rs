//! The capability itself: `rcon-open`, `tcp-open`, `udp-exchange` and the [`Session`] they return.

use std::sync::Arc;
use std::time::Instant;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpStream, UdpSocket};

use crate::audit::{AuditEvent, AuditSink};
use crate::config::GameNetConfig;
use crate::directory::{Scope, ServerDirectory, ServerTarget};
use crate::error::NetError;
use crate::guard::resolve_pinned;
use crate::limits::{Limits, Permits};
use crate::metrics;
use crate::rcon;

/// RCON dialect (both share framing; the label is for audit/metrics).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scheme {
    /// Valve Source RCON.
    SourceRcon,
    /// Minecraft RCON.
    MinecraftRcon,
}

impl Scheme {
    fn label(self) -> &'static str {
        match self {
            Self::SourceRcon => "rcon_open_source",
            Self::MinecraftRcon => "rcon_open_minecraft",
        }
    }
}

/// The host capability. Cheap to share (`Arc` it); holds the limits and the directory.
pub struct GameNet {
    directory: Arc<dyn ServerDirectory>,
    audit: Arc<dyn AuditSink>,
    cfg: GameNetConfig,
    limits: Limits,
}

impl GameNet {
    /// Builds the capability over a directory and an audit sink.
    pub fn new(
        directory: Arc<dyn ServerDirectory>,
        audit: Arc<dyn AuditSink>,
        cfg: GameNetConfig,
    ) -> Self {
        let limits = Limits::new(&cfg);
        Self {
            directory,
            audit,
            cfg,
            limits,
        }
    }

    /// Rate check -> directory lookup -> concurrency permit -> pinned, guarded address.
    async fn prepare(
        &self,
        scope: &Scope,
        server: &str,
    ) -> Result<(ServerTarget, std::net::SocketAddr), NetError> {
        let target = tokio::time::timeout(
            self.cfg.directory_timeout,
            self.directory.lookup(scope, server),
        )
        .await
        .map_err(|_| NetError::Timeout)??;
        self.limits
            .check_rate(scope.community_id, &target.server_id)?;
        let addr = resolve_pinned(
            &target.host,
            target.port,
            self.cfg.loopback_allowed(),
            self.cfg.connect_timeout,
        )
        .await?;
        Ok((target, addr))
    }

    fn open_audit(&self, scope: &Scope, server: &str, op: &'static str, outcome: String) {
        self.audit.record(&AuditEvent::Open {
            scope: *scope,
            server: server.to_string(),
            op,
            outcome,
        });
    }

    async fn connect(&self, addr: std::net::SocketAddr) -> Result<TcpStream, NetError> {
        match tokio::time::timeout(self.cfg.connect_timeout, TcpStream::connect(addr)).await {
            Err(_) => Err(NetError::Timeout),
            Ok(Err(_)) => Err(NetError::Unreachable),
            Ok(Ok(s)) => {
                let _ = s.set_nodelay(true);
                Ok(s)
            }
        }
    }

    /// Opens an authenticated RCON session: the HOST performs the login with the stored password.
    pub async fn rcon_open(
        &self,
        scope: &Scope,
        server: &str,
        scheme: Scheme,
    ) -> Result<Session, NetError> {
        let started = Instant::now();
        let r = self.rcon_open_inner(scope, server, scheme).await;
        self.finish(scope, server, scheme.label(), &r, started);
        r
    }

    async fn rcon_open_inner(
        &self,
        scope: &Scope,
        server: &str,
        scheme: Scheme,
    ) -> Result<Session, NetError> {
        let (target, addr) = self.prepare(scope, server).await?;
        if target.password.expose().is_empty() {
            return Err(NetError::Denied("no_password"));
        }
        tracing::debug!(scheme = scheme.label(), "gameserver-net rcon open");
        let permits = self.limits.acquire(scope.community_id)?;
        let mut stream = self.connect(addr).await?;
        rcon::login(&mut stream, &target.password, self.cfg.read_timeout).await?;
        Ok(Session {
            stream,
            kind: Kind::Rcon,
            scope: *scope,
            server_id: target.server_id,
            max_in: self.cfg.rcon_max_in,
            max_out: self.cfg.rcon_max_out,
            max_command_len: self.cfg.max_command_len,
            read_timeout: self.cfg.read_timeout,
            bytes_in: 0,
            bytes_out: 0,
            audit: Arc::clone(&self.audit),
            _permits: permits,
        })
    }

    /// Opens an unauthenticated TCP session (Minecraft status ping).
    pub async fn tcp_open(&self, scope: &Scope, server: &str) -> Result<Session, NetError> {
        let started = Instant::now();
        let r = self.tcp_open_inner(scope, server).await;
        self.finish(scope, server, "tcp_open", &r, started);
        r
    }

    async fn tcp_open_inner(&self, scope: &Scope, server: &str) -> Result<Session, NetError> {
        let (target, addr) = self.prepare(scope, server).await?;
        let permits = self.limits.acquire(scope.community_id)?;
        let stream = self.connect(addr).await?;
        Ok(Session {
            stream,
            kind: Kind::Tcp,
            scope: *scope,
            server_id: target.server_id,
            max_in: self.cfg.tcp_max_in,
            max_out: self.cfg.tcp_max_out,
            max_command_len: self.cfg.max_command_len,
            read_timeout: self.cfg.read_timeout,
            bytes_in: 0,
            bytes_out: 0,
            audit: Arc::clone(&self.audit),
            _permits: permits,
        })
    }

    /// One UDP request/response (A2S) against the registered target's pinned address.
    pub async fn udp_exchange(
        &self,
        scope: &Scope,
        server: &str,
        payload: &[u8],
        max_response: u32,
    ) -> Result<Vec<u8>, NetError> {
        let started = Instant::now();
        let r = self.udp_inner(scope, server, payload, max_response).await;
        let outcome = match &r {
            Ok(_) => "ok",
            Err(e) => e.label(),
        };
        metrics::record_call("udp_exchange", outcome, started.elapsed().as_secs_f64());
        if let Ok(v) = &r {
            metrics::record_bytes("in", v.len());
        }
        self.open_audit(scope, server, "udp_exchange", audit_outcome(&r));
        r
    }

    async fn udp_inner(
        &self,
        scope: &Scope,
        server: &str,
        payload: &[u8],
        max_response: u32,
    ) -> Result<Vec<u8>, NetError> {
        let cap = max_response as usize;
        if payload.is_empty() || payload.len() > self.cfg.udp_max_request {
            return Err(NetError::TooLarge);
        }
        if cap == 0 || cap > self.cfg.udp_max_response {
            return Err(NetError::TooLarge);
        }
        let (_target, addr) = self.prepare(scope, server).await?;
        let _permits = self.limits.acquire(scope.community_id)?;
        let bind: std::net::SocketAddr = if addr.is_ipv4() {
            "0.0.0.0:0".parse().map_err(|_| NetError::Backend)?
        } else {
            "[::]:0".parse().map_err(|_| NetError::Backend)?
        };
        let sock = UdpSocket::bind(bind).await.map_err(|_| NetError::Backend)?;
        // connect() pins the peer: the kernel drops datagrams from any other source.
        sock.connect(addr)
            .await
            .map_err(|_| NetError::Unreachable)?;
        sock.send(payload)
            .await
            .map_err(|_| NetError::Unreachable)?;
        let mut buf = vec![0u8; cap + 1];
        let n = match tokio::time::timeout(self.cfg.read_timeout, sock.recv(&mut buf)).await {
            Err(_) => return Err(NetError::Timeout),
            Ok(Err(_)) => return Err(NetError::Unreachable),
            Ok(Ok(n)) => n,
        };
        if n > cap {
            return Err(NetError::TooLarge);
        }
        buf.truncate(n);
        Ok(buf)
    }

    fn finish(
        &self,
        scope: &Scope,
        server: &str,
        op: &'static str,
        r: &Result<Session, NetError>,
        started: Instant,
    ) {
        let outcome = match r {
            Ok(_) => "ok",
            Err(e) => e.label(),
        };
        metrics::record_call(op, outcome, started.elapsed().as_secs_f64());
        let id = match r {
            Ok(s) => s.server_id.clone(),
            Err(_) => server.to_string(),
        };
        self.open_audit(scope, &id, op, audit_outcome(r));
    }
}

fn audit_outcome<T>(r: &Result<T, NetError>) -> String {
    match r {
        Ok(_) => "ok".to_string(),
        Err(NetError::Denied(code)) => format!("denied:{code}"),
        Err(e) => e.label().to_string(),
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Rcon,
    Tcp,
}

/// An open, budgeted socket. Dropping it closes the connection and frees the concurrency slots.
pub struct Session {
    stream: TcpStream,
    kind: Kind,
    scope: Scope,
    server_id: String,
    max_in: usize,
    max_out: usize,
    max_command_len: usize,
    read_timeout: std::time::Duration,
    bytes_in: usize,
    bytes_out: usize,
    audit: Arc<dyn AuditSink>,
    _permits: Permits,
}

impl Session {
    /// Writes `bytes`, within the session's out-budget. RCON sessions additionally require
    /// well-formed single-line exec packets within the command cap (each is audited).
    pub async fn write_all(&mut self, bytes: &[u8]) -> Result<(), NetError> {
        if self.bytes_out + bytes.len() > self.max_out {
            return Err(NetError::TooLarge);
        }
        if self.kind == Kind::Rcon {
            let summary = match rcon::validate_guest_write(bytes, self.max_command_len) {
                Ok(s) => s,
                Err(e) => {
                    self.audit.record(&AuditEvent::Command {
                        scope: self.scope,
                        server: self.server_id.clone(),
                        command: String::new(),
                        outcome: "denied:command_rejected".to_string(),
                    });
                    return Err(e);
                }
            };
            let result =
                tokio::time::timeout(self.read_timeout, self.stream.write_all(bytes)).await;
            let outcome = match &result {
                Ok(Ok(())) => "ok",
                Ok(Err(_)) => "unreachable",
                Err(_) => "timeout",
            };
            for command in summary.commands {
                self.audit.record(&AuditEvent::Command {
                    scope: self.scope,
                    server: self.server_id.clone(),
                    command,
                    outcome: outcome.to_string(),
                });
            }
            return self.after_write(result, bytes.len());
        }
        let result = tokio::time::timeout(self.read_timeout, self.stream.write_all(bytes)).await;
        self.after_write(result, bytes.len())
    }

    fn after_write(
        &mut self,
        result: Result<std::io::Result<()>, tokio::time::error::Elapsed>,
        n: usize,
    ) -> Result<(), NetError> {
        match result {
            Err(_) => Err(NetError::Timeout),
            Ok(Err(_)) => Err(NetError::Unreachable),
            Ok(Ok(())) => {
                self.bytes_out += n;
                metrics::record_bytes("out", n);
                Ok(())
            }
        }
    }

    /// Reads up to `max` bytes (one read; empty = peer closed), within the in-budget.
    pub async fn read(&mut self, max: u32) -> Result<Vec<u8>, NetError> {
        let remaining = self.max_in.saturating_sub(self.bytes_in);
        if remaining == 0 {
            return Err(NetError::TooLarge);
        }
        let want = (max as usize).min(remaining).min(64 * 1024);
        if want == 0 {
            return Err(NetError::TooLarge);
        }
        let mut buf = vec![0u8; want];
        let n = match tokio::time::timeout(self.read_timeout, self.stream.read(&mut buf)).await {
            Err(_) => return Err(NetError::Timeout),
            Ok(Err(_)) => return Err(NetError::Unreachable),
            Ok(Ok(n)) => n,
        };
        buf.truncate(n);
        self.bytes_in += n;
        metrics::record_bytes("in", n);
        Ok(buf)
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.audit.record(&AuditEvent::Close {
            scope: self.scope,
            server: self.server_id.clone(),
            bytes_in: self.bytes_in,
            bytes_out: self.bytes_out,
        });
    }
}
