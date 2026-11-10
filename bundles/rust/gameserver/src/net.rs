//! The `gameserver-net` host-capability seam.
//!
//! The guest never opens a socket, never learns a server address and never
//! holds an RCON password. It names a registered server by its UUID and the
//! HOST resolves host:port + the connection secret, runs the SSRF egress guard
//! on the RESOLVED address (loopback/private/link-local/metadata/cluster CIDRs
//! blocked), connects, and (for RCON) performs the login itself before handing
//! the stream over. The exact contract the host must implement is specified in
//! README.md "Needs from S0". Until it exists, [`UnavailableNet`] makes every
//! network tool fail loud with `Unavailable`.

use std::fmt;

/// RCON dialect; selects the host-side login and the guest-side read loop.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scheme {
    SourceRcon,
    MinecraftRcon,
}

/// Transport failure, mapped 1:1 from the host capability's error variant.
// Most variants are produced only by the host `gameserver-net` capability, which the
// guest cannot construct until S0 lands it (README "Needs from S0"); the mapping for
// every one of them is already exercised by tests.
#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NetError {
    /// Host policy refused (unregistered server, blocked address, no grant).
    Denied(String),
    /// Could not connect / no route.
    Unreachable,
    /// Deadline elapsed.
    Timeout,
    /// Host-side RCON login was rejected.
    AuthFailed,
    /// Peer sent more than the host's per-call byte budget.
    TooLarge,
    /// The host capability is not available (fail loud, never a fallback).
    Unavailable(String),
}

impl fmt::Display for NetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            NetError::Denied(c) => write!(f, "denied:{c}"),
            NetError::Unreachable => f.write_str("unreachable"),
            NetError::Timeout => f.write_str("timeout"),
            NetError::AuthFailed => f.write_str("auth_failed"),
            NetError::TooLarge => f.write_str("too_large"),
            NetError::Unavailable(_) => f.write_str("net_unavailable"),
        }
    }
}

/// An open, host-owned TCP stream.
pub trait Stream {
    /// Writes all bytes.
    fn write_all(&mut self, bytes: &[u8]) -> Result<(), NetError>;
    /// Reads 1..=`max` bytes; an empty vec means the peer closed. Blocks up to
    /// the host's per-read deadline, then `Timeout`.
    fn read(&mut self, max: usize) -> Result<Vec<u8>, NetError>;
}

/// The game-server network capability.
pub trait GameNet {
    /// Opens an AUTHENTICATED RCON stream to registered server `server`.
    fn rcon_open(&self, server: &str, scheme: Scheme) -> Result<Box<dyn Stream>, NetError>;
    /// Opens an unauthenticated TCP stream (Minecraft status ping).
    fn tcp_open(&self, server: &str) -> Result<Box<dyn Stream>, NetError>;
    /// One UDP request/response (A2S). `max_response` bounds the reply.
    fn udp_exchange(
        &self,
        server: &str,
        payload: &[u8],
        max_response: usize,
    ) -> Result<Vec<u8>, NetError>;
}

/// The capability as shipped today: absent. Every call fails loud.
pub struct UnavailableNet;

const MSG: &str = "gameserver-net host capability is not available";

impl GameNet for UnavailableNet {
    fn rcon_open(&self, _: &str, _: Scheme) -> Result<Box<dyn Stream>, NetError> {
        Err(NetError::Unavailable(MSG.into()))
    }
    fn tcp_open(&self, _: &str) -> Result<Box<dyn Stream>, NetError> {
        Err(NetError::Unavailable(MSG.into()))
    }
    fn udp_exchange(&self, _: &str, _: &[u8], _: usize) -> Result<Vec<u8>, NetError> {
        Err(NetError::Unavailable(MSG.into()))
    }
}

/// Test doubles: byte-exact scripted peers and a real loopback RCON server.
#[cfg(test)]
pub mod testing {
    use super::*;
    use std::collections::VecDeque;
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::sync::{Arc, Mutex};

    /// A stream that serves `wire` in `chunk`-sized reads and records writes.
    pub struct ScriptedStream {
        wire: VecDeque<u8>,
        chunk: usize,
        pub written: Vec<u8>,
    }

    impl ScriptedStream {
        pub fn new(wire: Vec<u8>, chunk: usize) -> Self {
            ScriptedStream {
                wire: wire.into(),
                chunk,
                written: Vec::new(),
            }
        }
    }

    impl Stream for ScriptedStream {
        fn write_all(&mut self, b: &[u8]) -> Result<(), NetError> {
            self.written.extend_from_slice(b);
            Ok(())
        }
        fn read(&mut self, max: usize) -> Result<Vec<u8>, NetError> {
            let n = self.chunk.min(max).min(self.wire.len());
            Ok(self.wire.drain(..n).collect())
        }
    }

    /// A UDP peer replaying scripted datagrams; records every request.
    pub struct ScriptedNet {
        replies: Mutex<VecDeque<Vec<u8>>>,
        sent: Mutex<Vec<Vec<u8>>>,
        pub tcp: Mutex<Option<Vec<u8>>>,
    }

    impl ScriptedNet {
        pub fn udp(replies: Vec<Vec<u8>>) -> Self {
            ScriptedNet {
                replies: Mutex::new(replies.into()),
                sent: Mutex::new(Vec::new()),
                tcp: Mutex::new(None),
            }
        }
        pub fn sent(&self) -> Vec<Vec<u8>> {
            self.sent.lock().unwrap().clone()
        }
    }

    impl GameNet for ScriptedNet {
        fn rcon_open(&self, _: &str, _: Scheme) -> Result<Box<dyn Stream>, NetError> {
            Err(NetError::Denied("scripted".into()))
        }
        fn tcp_open(&self, _: &str) -> Result<Box<dyn Stream>, NetError> {
            match self.tcp.lock().unwrap().take() {
                Some(w) => Ok(Box::new(ScriptedStream::new(w, 9))),
                None => Err(NetError::Unreachable),
            }
        }
        fn udp_exchange(&self, _: &str, p: &[u8], _: usize) -> Result<Vec<u8>, NetError> {
            self.sent.lock().unwrap().push(p.to_vec());
            self.replies
                .lock()
                .unwrap()
                .pop_front()
                .ok_or(NetError::Timeout)
        }
    }

    /// Real `std::net` stream (loopback tests only; never shipped).
    pub struct TcpAdapter(pub TcpStream);

    impl Stream for TcpAdapter {
        fn write_all(&mut self, b: &[u8]) -> Result<(), NetError> {
            self.0.write_all(b).map_err(|_| NetError::Unreachable)
        }
        fn read(&mut self, max: usize) -> Result<Vec<u8>, NetError> {
            let mut buf = vec![0u8; max.min(8192)];
            let n = self.0.read(&mut buf).map_err(|_| NetError::Timeout)?;
            buf.truncate(n);
            Ok(buf)
        }
    }

    /// Spawns a real loopback Source-RCON server that answers every EXEC with
    /// `reply` split across two packets, then mirrors the sentinel. Returns
    /// the bound address and the list of command bodies it received.
    pub fn spawn_rcon_server(
        reply: &'static str,
    ) -> (std::net::SocketAddr, Arc<Mutex<Vec<String>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let seen2 = seen.clone();
        std::thread::spawn(move || {
            if let Ok((mut sock, _)) = listener.accept() {
                let mut pending = Vec::new();
                let mut buf = [0u8; 1024];
                loop {
                    let Ok(n) = sock.read(&mut buf) else { return };
                    if n == 0 {
                        return;
                    }
                    pending.extend_from_slice(&buf[..n]);
                    while pending.len() >= 4 {
                        let len =
                            i32::from_le_bytes([pending[0], pending[1], pending[2], pending[3]])
                                as usize;
                        if pending.len() < 4 + len {
                            break;
                        }
                        let pkt: Vec<u8> = pending.drain(..4 + len).collect();
                        let id = i32::from_le_bytes([pkt[4], pkt[5], pkt[6], pkt[7]]);
                        let kind = i32::from_le_bytes([pkt[8], pkt[9], pkt[10], pkt[11]]);
                        let body = String::from_utf8_lossy(&pkt[12..pkt.len() - 2]).to_string();
                        if kind == 2 {
                            seen2.lock().unwrap().push(body);
                            let half = reply.len() / 2;
                            for part in [&reply[..half], &reply[half..]] {
                                let _ = sock.write_all(&crate::proto::encode_rcon(
                                    id,
                                    0,
                                    part.as_bytes(),
                                ));
                            }
                        } else {
                            let _ = sock.write_all(&crate::proto::encode_rcon(id, 0, b""));
                        }
                    }
                }
            }
        });
        (addr, seen)
    }
}
