//! In-process IRC server for real-socket tests of the per-community Twitch
//! sessions: speaks just enough of the wire protocol (`PASS`/`NICK`/`USER`
//! -> `001`, `JOIN` -> echo, `PRIVMSG` injection) for the real
//! `penguin-connector-twitch` client to register, join and receive, and can
//! reject a given token or drop a socket on demand.
#![allow(dead_code)] // each test binary uses a subset of the helpers

use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex, PoisonError};

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpListener;
use tokio::sync::mpsc::{unbounded_channel, UnboundedSender};

/// What the server has observed on one accepted connection.
#[derive(Clone, Debug)]
pub struct ObservedConn {
    /// The `PASS` token with the `oauth:` prefix stripped.
    pub token: Option<String>,
    /// Community the token maps to (via [`IrcTestServer::map_token`]).
    pub community: Option<i64>,
    /// The `NICK` value.
    pub nick: Option<String>,
    /// The channel joined (`#name`), empty until `JOIN`.
    pub joined: String,
    /// Whether the server answered `001` (registration accepted).
    pub registered: bool,
    /// Whether the socket is still open on the server side.
    pub live: bool,
}

/// Commands pushed to one connection task.
enum Cmd {
    Line(String),
    Drop,
}

/// Mutable server state shared with every connection task.
#[derive(Default)]
struct Shared {
    conns: Mutex<Vec<ObservedConn>>,
    senders: Mutex<Vec<UnboundedSender<Cmd>>>,
    tokens: Mutex<HashMap<String, i64>>,
    rejected: Mutex<HashSet<String>>,
}

/// Handle to a running in-process IRC server (accept loop dies with the
/// runtime).
pub struct IrcTestServer {
    /// Address the server listens on (`127.0.0.1:<port>`).
    pub addr: SocketAddr,
    shared: Arc<Shared>,
}

impl IrcTestServer {
    /// Binds an ephemeral localhost port and starts accepting.
    pub async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        let shared = Arc::new(Shared::default());
        let acc = shared.clone();
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let (tx, rx) = unbounded_channel();
                let idx = {
                    let mut conns = acc.conns.lock().unwrap_or_else(PoisonError::into_inner);
                    conns.push(ObservedConn {
                        token: None,
                        community: None,
                        nick: None,
                        joined: String::new(),
                        registered: false,
                        live: true,
                    });
                    acc.senders
                        .lock()
                        .unwrap_or_else(PoisonError::into_inner)
                        .push(tx);
                    conns.len() - 1
                };
                tokio::spawn(serve(stream, idx, acc.clone(), rx));
            }
        });
        Self { addr, shared }
    }

    /// Port to hand the client.
    pub fn port(&self) -> u16 {
        self.addr.port()
    }

    /// Declares which community a token belongs to.
    pub fn map_token(&self, token: &str, community: i64) {
        self.shared
            .tokens
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(token.to_string(), community);
    }

    /// Makes registration with `token` fail (`ERROR`), like a revoked token.
    pub fn reject_token(&self, token: &str) {
        self.shared
            .rejected
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(token.to_string());
    }

    /// Snapshot of every connection ever accepted, in accept order.
    pub fn connections(&self) -> Vec<ObservedConn> {
        self.shared
            .conns
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    /// Number of connections whose socket is still open.
    pub fn live_conn_count(&self) -> usize {
        self.connections().iter().filter(|c| c.live).count()
    }

    /// Sends `:user PRIVMSG <joined channel> :text` on connection `idx`.
    pub fn send_privmsg(&self, idx: usize, user: &str, text: &str) {
        let chan = self.connections()[idx].joined.clone();
        assert!(!chan.is_empty(), "conn {idx} has not joined a channel");
        self.send(
            idx,
            Cmd::Line(format!(
                ":{user}!{user}@{user}.tmi.twitch.tv PRIVMSG {chan} :{text}"
            )),
        );
    }

    /// Closes connection `idx` from the server side.
    pub fn drop_conn(&self, idx: usize) {
        self.send(idx, Cmd::Drop);
    }

    fn send(&self, idx: usize, cmd: Cmd) {
        let senders = self
            .shared
            .senders
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        // A send to an already-closed connection is a test bug worth surfacing.
        senders[idx].send(cmd).expect("connection task alive");
    }
}

/// Mutates connection `idx`'s record under the lock.
fn update(shared: &Shared, idx: usize, f: impl FnOnce(&mut ObservedConn)) {
    let mut conns = shared.conns.lock().unwrap_or_else(PoisonError::into_inner);
    f(&mut conns[idx]);
}

/// Serves one connection until EOF, a drop command, or a rejected login.
async fn serve(
    stream: tokio::net::TcpStream,
    idx: usize,
    shared: Arc<Shared>,
    mut rx: tokio::sync::mpsc::UnboundedReceiver<Cmd>,
) {
    let (rd, mut wr) = stream.into_split();
    let mut lines = BufReader::new(rd).lines();
    let mut nick = String::new();
    loop {
        tokio::select! {
            line = lines.next_line() => {
                let Ok(Some(line)) = line else { break };
                if let Some(pass) = line.strip_prefix("PASS ") {
                    let token = pass.strip_prefix("oauth:").unwrap_or(pass).to_string();
                    let community = shared
                        .tokens
                        .lock()
                        .unwrap_or_else(PoisonError::into_inner)
                        .get(&token)
                        .copied();
                    update(&shared, idx, |c| {
                        c.token = Some(token);
                        c.community = community;
                    });
                } else if let Some(n) = line.strip_prefix("NICK ") {
                    nick = n.to_string();
                    update(&shared, idx, |c| c.nick = Some(n.to_string()));
                } else if line.starts_with("USER ") {
                    let token = shared.conns.lock().unwrap_or_else(PoisonError::into_inner)[idx]
                        .token
                        .clone()
                        .unwrap_or_default();
                    let rejected = shared
                        .rejected
                        .lock()
                        .unwrap_or_else(PoisonError::into_inner)
                        .contains(&token);
                    if rejected {
                        let _ = wr.write_all(b"ERROR :Login authentication failed\r\n").await;
                        break;
                    }
                    update(&shared, idx, |c| c.registered = true);
                    let msg = format!(":tmi.twitch.tv 001 {nick} :Welcome\r\n");
                    if wr.write_all(msg.as_bytes()).await.is_err() {
                        break;
                    }
                } else if let Some(chan) = line.strip_prefix("JOIN ") {
                    update(&shared, idx, |c| c.joined = chan.to_string());
                    let msg = format!(":{nick}!{nick}@{nick}.tmi.twitch.tv JOIN {chan}\r\n");
                    if wr.write_all(msg.as_bytes()).await.is_err() {
                        break;
                    }
                }
            }
            cmd = rx.recv() => match cmd {
                Some(Cmd::Line(l)) => {
                    if wr.write_all(format!("{l}\r\n").as_bytes()).await.is_err() {
                        break;
                    }
                }
                Some(Cmd::Drop) | None => break,
            },
        }
    }
    update(&shared, idx, |c| c.live = false);
}
