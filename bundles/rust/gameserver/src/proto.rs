//! Wire codecs for the game-server protocols the `gameserver` service speaks:
//! Source RCON / Minecraft RCON (same packet framing), Valve A2S_INFO /
//! A2S_PLAYER (UDP) and the Minecraft Server List Ping (TCP).
//!
//! Everything here is pure byte-in / byte-out over the [`Stream`] seam, so it
//! is exercised on the host against real sockets in tests. The guest NEVER
//! sees a password or an address: authentication (RCON login) is performed by
//! the host before the stream is handed over, and the address is resolved
//! host-side (see README "Needs from S0").

use crate::net::{GameNet, NetError, Scheme, Stream};

/// RCON packet type: server reply body.
const TYPE_RESPONSE: i32 = 0;
/// RCON packet type: execute command (client -> server).
const TYPE_EXEC: i32 = 2;
/// Largest RCON packet body a server may send (Source + Minecraft).
pub const RCON_MAX_BODY: usize = 4096;
/// Largest packet we accept off the wire (header + body + padding).
const RCON_MAX_PACKET: usize = RCON_MAX_BODY + 16;
/// Largest command body the client will send (Minecraft's 1446 minus slack).
pub const RCON_MAX_COMMAND: usize = 1400;
/// Most packets read for one command (bounds work on a hostile server).
const MAX_RCON_PACKETS: usize = 64;
/// Request id for the command packet.
const CMD_ID: i32 = 0x0057_4144;
/// Request id for the Source end-of-response sentinel packet.
const SENTINEL_ID: i32 = CMD_ID + 1;

/// Failure inside a protocol exchange.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtoError {
    /// Transport-level failure (denied/unreachable/timeout/unavailable).
    Net(NetError),
    /// The peer sent bytes that violate the protocol.
    Malformed(&'static str),
    /// The peer answered with an unsupported but valid shape.
    Unsupported(&'static str),
}

impl From<NetError> for ProtoError {
    fn from(e: NetError) -> Self {
        ProtoError::Net(e)
    }
}

/// Buffered reader over a [`Stream`].
struct Reader<'a> {
    stream: &'a mut dyn Stream,
    buf: Vec<u8>,
}

impl<'a> Reader<'a> {
    fn new(stream: &'a mut dyn Stream) -> Self {
        Reader {
            stream,
            buf: Vec::new(),
        }
    }

    /// Ensures at least `n` buffered bytes; EOF before that is malformed.
    fn fill(&mut self, n: usize) -> Result<(), ProtoError> {
        while self.buf.len() < n {
            let chunk = self.stream.read(n - self.buf.len())?;
            if chunk.is_empty() {
                return Err(ProtoError::Malformed("unexpected end of stream"));
            }
            self.buf.extend_from_slice(&chunk);
        }
        Ok(())
    }

    /// Removes and returns exactly `n` bytes.
    fn take(&mut self, n: usize) -> Result<Vec<u8>, ProtoError> {
        self.fill(n)?;
        Ok(self.buf.drain(..n).collect())
    }

    /// Reads one byte.
    fn byte(&mut self) -> Result<u8, ProtoError> {
        Ok(self.take(1)?[0])
    }

    /// Reads a Minecraft VarInt (max 5 bytes).
    fn varint(&mut self) -> Result<i32, ProtoError> {
        let mut value: u32 = 0;
        for shift in 0..5 {
            let b = self.byte()?;
            value |= u32::from(b & 0x7f) << (7 * shift);
            if b & 0x80 == 0 {
                return Ok(value as i32);
            }
        }
        Err(ProtoError::Malformed("varint too long"))
    }
}

/// One decoded RCON packet.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RconPacket {
    pub id: i32,
    pub kind: i32,
    pub body: Vec<u8>,
}

/// Encodes one RCON packet (`len | id | type | body | 0 | 0`, little endian).
pub fn encode_rcon(id: i32, kind: i32, body: &[u8]) -> Vec<u8> {
    let len = (10 + body.len()) as i32;
    let mut out = Vec::with_capacity(14 + body.len());
    out.extend_from_slice(&len.to_le_bytes());
    out.extend_from_slice(&id.to_le_bytes());
    out.extend_from_slice(&kind.to_le_bytes());
    out.extend_from_slice(body);
    out.extend_from_slice(&[0, 0]);
    out
}

/// Reads and validates one RCON packet.
fn read_rcon(r: &mut Reader<'_>) -> Result<RconPacket, ProtoError> {
    let len_bytes = r.take(4)?;
    let len = i32::from_le_bytes([len_bytes[0], len_bytes[1], len_bytes[2], len_bytes[3]]);
    if !(10..=RCON_MAX_PACKET as i32).contains(&len) {
        return Err(ProtoError::Malformed("rcon packet length out of range"));
    }
    let rest = r.take(len as usize)?;
    let id = i32::from_le_bytes([rest[0], rest[1], rest[2], rest[3]]);
    let kind = i32::from_le_bytes([rest[4], rest[5], rest[6], rest[7]]);
    let body = rest[8..rest.len() - 2].to_vec();
    if rest[rest.len() - 2..] != [0, 0] {
        return Err(ProtoError::Malformed("rcon packet missing terminator"));
    }
    Ok(RconPacket { id, kind, body })
}

/// Result of one RCON command.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RconOutput {
    pub text: String,
    pub truncated: bool,
}

/// Executes one command over an already-authenticated RCON stream and returns
/// its output, capped at `max_output` bytes (`truncated` is set when more was
/// available). Source servers get the empty-packet sentinel so multi-packet
/// replies are read to completion; Minecraft replies are read until a short
/// packet.
pub fn rcon_exec(
    stream: &mut dyn Stream,
    scheme: Scheme,
    command: &str,
    max_output: usize,
) -> Result<RconOutput, ProtoError> {
    if command.len() > RCON_MAX_COMMAND {
        return Err(ProtoError::Malformed("command too long"));
    }
    stream.write_all(&encode_rcon(CMD_ID, TYPE_EXEC, command.as_bytes()))?;
    if scheme == Scheme::SourceRcon {
        stream.write_all(&encode_rcon(SENTINEL_ID, TYPE_RESPONSE, b""))?;
    }
    let mut r = Reader::new(stream);
    let mut out: Vec<u8> = Vec::new();
    let mut truncated = false;
    for _ in 0..MAX_RCON_PACKETS {
        let pkt = read_rcon(&mut r)?;
        if pkt.id == -1 {
            return Err(ProtoError::Net(NetError::AuthFailed));
        }
        if scheme == Scheme::SourceRcon && pkt.id == SENTINEL_ID {
            return Ok(finish(out, truncated));
        }
        if pkt.id != CMD_ID {
            // Stray packet from an earlier request on a shared connection.
            continue;
        }
        let short = pkt.body.len() < RCON_MAX_BODY;
        let room = max_output.saturating_sub(out.len());
        if pkt.body.len() > room {
            out.extend_from_slice(&pkt.body[..room]);
            truncated = true;
            // Output is already capped: stop reading, drop the connection.
            return Ok(finish(out, truncated));
        }
        out.extend_from_slice(&pkt.body);
        if scheme == Scheme::MinecraftRcon && short {
            return Ok(finish(out, truncated));
        }
    }
    // Too many packets: cap reached by count, report truncation honestly.
    Ok(finish(out, true))
}

/// Builds the sanitized output record.
fn finish(raw: Vec<u8>, truncated: bool) -> RconOutput {
    RconOutput {
        text: sanitize_text(&String::from_utf8_lossy(&raw)),
        truncated,
    }
}

/// Strips control characters (keeping `\n`/`\t`) and Minecraft `§x` color
/// codes so server text can never carry terminal/markup control sequences.
pub fn sanitize_text(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars();
    while let Some(c) = chars.next() {
        if c == '\u{a7}' {
            chars.next();
            continue;
        }
        if c == '\r' {
            continue;
        }
        if c.is_control() && c != '\n' && c != '\t' {
            continue;
        }
        out.push(c);
    }
    out
}

/// Decoded A2S_INFO reply.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct A2sInfo {
    pub name: String,
    pub map: String,
    pub game: String,
    pub players: u8,
    pub max_players: u8,
    pub bots: u8,
    pub version: String,
}

/// One A2S_PLAYER row (score/duration deliberately not surfaced).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct A2sPlayer {
    pub name: String,
}

const A2S_HEADER: [u8; 4] = [0xff, 0xff, 0xff, 0xff];
const A2S_FRAGMENT: [u8; 4] = [0xfe, 0xff, 0xff, 0xff];
/// UDP response ceiling requested from the host.
pub const A2S_MAX_RESPONSE: usize = 1400;

/// Cursor over a datagram.
struct Cur<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> Cur<'a> {
    fn u8(&mut self) -> Result<u8, ProtoError> {
        let v = *self
            .b
            .get(self.i)
            .ok_or(ProtoError::Malformed("short datagram"))?;
        self.i += 1;
        Ok(v)
    }
    fn skip(&mut self, n: usize) -> Result<(), ProtoError> {
        if self.i + n > self.b.len() {
            return Err(ProtoError::Malformed("short datagram"));
        }
        self.i += n;
        Ok(())
    }
    fn cstr(&mut self) -> Result<String, ProtoError> {
        let rest = &self.b[self.i..];
        let end = rest
            .iter()
            .position(|&c| c == 0)
            .ok_or(ProtoError::Malformed("unterminated string"))?;
        let s = sanitize_text(&String::from_utf8_lossy(&rest[..end]));
        self.i += end + 1;
        Ok(s)
    }
}

/// Checks the datagram header and returns the type byte + the cursor after it.
fn a2s_header(buf: &[u8]) -> Result<(u8, Cur<'_>), ProtoError> {
    if buf.len() >= 4 && buf[..4] == A2S_FRAGMENT {
        return Err(ProtoError::Unsupported("fragmented a2s response"));
    }
    if buf.len() < 5 || buf[..4] != A2S_HEADER {
        return Err(ProtoError::Malformed("bad a2s header"));
    }
    Ok((buf[4], Cur { b: buf, i: 5 }))
}

/// Sends `request` (+ optional challenge) and follows at most one challenge.
fn a2s_roundtrip(
    net: &dyn GameNet,
    server: &str,
    build: &dyn Fn(Option<[u8; 4]>) -> Vec<u8>,
    want: u8,
) -> Result<Vec<u8>, ProtoError> {
    let first = net.udp_exchange(server, &build(None), A2S_MAX_RESPONSE)?;
    let (t, mut cur) = a2s_header(&first)?;
    if t == want {
        return Ok(first);
    }
    if t != 0x41 {
        return Err(ProtoError::Malformed("unexpected a2s reply type"));
    }
    let mut ch = [0u8; 4];
    for byte in &mut ch {
        *byte = cur.u8()?;
    }
    let second = net.udp_exchange(server, &build(Some(ch)), A2S_MAX_RESPONSE)?;
    let (t2, _) = a2s_header(&second)?;
    if t2 != want {
        return Err(ProtoError::Malformed("a2s challenge not accepted"));
    }
    Ok(second)
}

/// Queries A2S_INFO.
pub fn a2s_info(net: &dyn GameNet, server: &str) -> Result<A2sInfo, ProtoError> {
    let build = |ch: Option<[u8; 4]>| {
        let mut p = A2S_HEADER.to_vec();
        p.push(b'T');
        p.extend_from_slice(b"Source Engine Query\0");
        if let Some(c) = ch {
            p.extend_from_slice(&c);
        }
        p
    };
    let buf = a2s_roundtrip(net, server, &build, b'I')?;
    let (_, mut c) = a2s_header(&buf)?;
    c.skip(1)?; // protocol
    let name = c.cstr()?;
    let map = c.cstr()?;
    let _folder = c.cstr()?;
    let game = c.cstr()?;
    c.skip(2)?; // app id
    let players = c.u8()?;
    let max_players = c.u8()?;
    let bots = c.u8()?;
    c.skip(4)?; // type, environment, visibility, vac
    let version = c.cstr()?;
    Ok(A2sInfo {
        name,
        map,
        game,
        players,
        max_players,
        bots,
        version,
    })
}

/// Queries A2S_PLAYER (names only, at most `limit`).
pub fn a2s_players(
    net: &dyn GameNet,
    server: &str,
    limit: usize,
) -> Result<Vec<A2sPlayer>, ProtoError> {
    let build = |ch: Option<[u8; 4]>| {
        let mut p = A2S_HEADER.to_vec();
        p.push(b'U');
        p.extend_from_slice(&ch.unwrap_or([0xff; 4]));
        p
    };
    let buf = a2s_roundtrip(net, server, &build, b'D')?;
    let (_, mut c) = a2s_header(&buf)?;
    let n = c.u8()? as usize;
    let mut out = Vec::new();
    for _ in 0..n.min(limit) {
        c.skip(1)?; // index
        let name = match c.cstr() {
            Ok(n) => n,
            // A datagram cut at the response ceiling: return what decoded.
            Err(_) => break,
        };
        if c.skip(8).is_err() {
            break;
        }
        if !name.is_empty() {
            out.push(A2sPlayer { name });
        }
    }
    Ok(out)
}

/// Decoded Minecraft status.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McStatus {
    pub motd: String,
    pub version: String,
    pub players: u32,
    pub max_players: u32,
    pub sample: Vec<String>,
}

/// Largest status JSON accepted (favicons make real replies ~10 KiB).
const MC_MAX_JSON: usize = 128 * 1024;

fn put_varint(out: &mut Vec<u8>, v: i32) {
    let mut v = v as u32;
    loop {
        let b = (v & 0x7f) as u8;
        v >>= 7;
        if v == 0 {
            out.push(b);
            return;
        }
        out.push(b | 0x80);
    }
}

/// Encodes the handshake (status intent) + status-request packets. The server
/// address/port fields are empty/0: the guest does not know the target (the
/// host owns it) and vanilla servers ignore them.
pub fn mc_ping_request() -> Vec<u8> {
    let mut hs = Vec::new();
    put_varint(&mut hs, 0); // packet id
    put_varint(&mut hs, -1); // protocol: unspecified
    put_varint(&mut hs, 0); // empty server address
    hs.extend_from_slice(&0u16.to_be_bytes());
    put_varint(&mut hs, 1); // next state: status
    let mut out = Vec::new();
    put_varint(&mut out, hs.len() as i32);
    out.extend_from_slice(&hs);
    out.extend_from_slice(&[0x01, 0x00]); // status request
    out
}

/// Flattens a chat component (string or `{text, extra}`) to plain text.
fn flatten_chat(v: &serde_json::Value, depth: usize, out: &mut String) {
    if depth > 8 {
        return;
    }
    match v {
        serde_json::Value::String(s) => out.push_str(s),
        serde_json::Value::Object(o) => {
            if let Some(t) = o.get("text").and_then(|t| t.as_str()) {
                out.push_str(t);
            }
            if let Some(serde_json::Value::Array(extra)) = o.get("extra") {
                for e in extra {
                    flatten_chat(e, depth + 1, out);
                }
            }
        }
        serde_json::Value::Array(a) => {
            for e in a {
                flatten_chat(e, depth + 1, out);
            }
        }
        _ => {}
    }
}

/// Decodes the status JSON text.
pub fn parse_mc_status(json: &str) -> Result<McStatus, ProtoError> {
    let v: serde_json::Value =
        serde_json::from_str(json).map_err(|_| ProtoError::Malformed("status json"))?;
    let players = v
        .get("players")
        .ok_or(ProtoError::Malformed("no players"))?;
    let num = |k: &str| players.get(k).and_then(|n| n.as_u64()).unwrap_or(0) as u32;
    let mut motd = String::new();
    if let Some(d) = v.get("description") {
        flatten_chat(d, 0, &mut motd);
    }
    let sample = players
        .get("sample")
        .and_then(|s| s.as_array())
        .map_or_else(Vec::new, |a| {
            a.iter()
                .filter_map(|p| p.get("name").and_then(|n| n.as_str()))
                .map(sanitize_text)
                .take(64)
                .collect()
        });
    Ok(McStatus {
        motd: sanitize_text(&motd),
        version: sanitize_text(
            v.get("version")
                .and_then(|x| x.get("name"))
                .and_then(|n| n.as_str())
                .unwrap_or(""),
        ),
        players: num("online"),
        max_players: num("max"),
        sample,
    })
}

/// Runs the Server List Ping over an unauthenticated TCP stream.
pub fn mc_ping(stream: &mut dyn Stream) -> Result<McStatus, ProtoError> {
    stream.write_all(&mc_ping_request())?;
    let mut r = Reader::new(stream);
    let len = r.varint()?;
    if len <= 0 || len as usize > MC_MAX_JSON + 16 {
        return Err(ProtoError::Malformed("status length out of range"));
    }
    let body = r.take(len as usize)?;
    let mut inner = Reader {
        stream: &mut NoStream,
        buf: body,
    };
    if inner.varint()? != 0 {
        return Err(ProtoError::Malformed("unexpected status packet id"));
    }
    let jlen = inner.varint()?;
    if jlen < 0 || jlen as usize > inner.buf.len() {
        return Err(ProtoError::Malformed("status json length"));
    }
    let json = inner.take(jlen as usize)?;
    parse_mc_status(&String::from_utf8_lossy(&json))
}

/// A stream with no further bytes (used to parse an in-memory frame).
struct NoStream;

impl Stream for NoStream {
    fn write_all(&mut self, _: &[u8]) -> Result<(), NetError> {
        Err(NetError::Unavailable("in-memory frame".into()))
    }
    fn read(&mut self, _: usize) -> Result<Vec<u8>, NetError> {
        Ok(Vec::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::net::testing::{ScriptedNet, ScriptedStream};

    fn resp(id: i32, body: &[u8]) -> Vec<u8> {
        encode_rcon(id, TYPE_RESPONSE, body)
    }

    #[test]
    fn rcon_packet_layout_is_len_id_type_body_nuls() {
        let p = encode_rcon(7, 2, b"status");
        assert_eq!(&p[..4], &16i32.to_le_bytes());
        assert_eq!(&p[4..8], &7i32.to_le_bytes());
        assert_eq!(&p[8..12], &2i32.to_le_bytes());
        assert_eq!(&p[12..18], b"status");
        assert_eq!(&p[18..], &[0, 0]);
    }

    #[test]
    fn source_exec_reads_multi_packet_until_sentinel() {
        let mut wire = resp(CMD_ID, b"hello ");
        wire.extend(resp(CMD_ID, b"world"));
        wire.extend(resp(SENTINEL_ID, b""));
        let mut s = ScriptedStream::new(wire, 3);
        let out = rcon_exec(&mut s, Scheme::SourceRcon, "status", 1024).unwrap();
        assert_eq!(out.text, "hello world");
        assert!(!out.truncated);
        // Wrote the exec packet then the sentinel.
        let mut want = encode_rcon(CMD_ID, TYPE_EXEC, b"status");
        want.extend(encode_rcon(SENTINEL_ID, TYPE_RESPONSE, b""));
        assert_eq!(s.written, want);
    }

    #[test]
    fn exec_caps_output_and_flags_truncation() {
        let mut wire = resp(CMD_ID, &[b'a'; 100]);
        wire.extend(resp(SENTINEL_ID, b""));
        let mut s = ScriptedStream::new(wire, 64);
        let out = rcon_exec(&mut s, Scheme::SourceRcon, "x", 10).unwrap();
        assert_eq!(out.text.len(), 10);
        assert!(out.truncated);
    }

    #[test]
    fn minecraft_exec_stops_at_short_packet_and_strips_color_codes() {
        let wire = resp(CMD_ID, "\u{a7}6There are 1/20 players".as_bytes());
        let mut s = ScriptedStream::new(wire, 7);
        let out = rcon_exec(&mut s, Scheme::MinecraftRcon, "list", 1024).unwrap();
        assert_eq!(out.text, "There are 1/20 players");
        // No sentinel is sent to Minecraft servers.
        assert_eq!(s.written, encode_rcon(CMD_ID, TYPE_EXEC, b"list"));
    }

    #[test]
    fn minecraft_exec_joins_full_size_fragments() {
        let mut wire = resp(CMD_ID, &vec![b'a'; RCON_MAX_BODY]);
        wire.extend(resp(CMD_ID, b"tail"));
        let mut s = ScriptedStream::new(wire, 1000);
        let out = rcon_exec(&mut s, Scheme::MinecraftRcon, "x", 100_000).unwrap();
        assert_eq!(out.text.len(), RCON_MAX_BODY + 4);
    }

    #[test]
    fn exec_maps_id_minus_one_to_auth_failure() {
        let mut s = ScriptedStream::new(resp(-1, b""), 64);
        assert_eq!(
            rcon_exec(&mut s, Scheme::SourceRcon, "x", 10).unwrap_err(),
            ProtoError::Net(NetError::AuthFailed)
        );
    }

    #[test]
    fn exec_rejects_bad_lengths_and_terminators() {
        let mut s = ScriptedStream::new(vec![0xff, 0xff, 0xff, 0x7f, 0, 0, 0, 0], 64);
        assert!(matches!(
            rcon_exec(&mut s, Scheme::SourceRcon, "x", 10),
            Err(ProtoError::Malformed(_))
        ));
        let mut bad = resp(CMD_ID, b"ab");
        let n = bad.len();
        bad[n - 1] = 1;
        let mut s = ScriptedStream::new(bad, 64);
        assert!(matches!(
            rcon_exec(&mut s, Scheme::SourceRcon, "x", 10),
            Err(ProtoError::Malformed(_))
        ));
    }

    #[test]
    fn exec_eof_is_malformed_not_a_hang() {
        let mut s = ScriptedStream::new(vec![1, 0], 64);
        assert!(matches!(
            rcon_exec(&mut s, Scheme::SourceRcon, "x", 10),
            Err(ProtoError::Malformed(_))
        ));
    }

    #[test]
    fn sanitize_drops_controls_keeps_newlines() {
        assert_eq!(sanitize_text("a\u{1b}[31mb\r\nc\td\u{0}"), "a[31mb\nc\td");
    }

    fn info_reply() -> Vec<u8> {
        let mut r = A2S_HEADER.to_vec();
        r.push(b'I');
        r.push(17);
        for s in ["My Server", "de_dust2", "csgo", "Counter-Strike"] {
            r.extend_from_slice(s.as_bytes());
            r.push(0);
        }
        r.extend_from_slice(&730i16.to_le_bytes());
        r.extend_from_slice(&[12, 24, 2, b'd', b'l', 0, 1]);
        r.extend_from_slice(b"1.38.0\0");
        r
    }

    #[test]
    fn a2s_info_follows_the_challenge() {
        let mut ch = A2S_HEADER.to_vec();
        ch.push(b'A');
        ch.extend_from_slice(&[1, 2, 3, 4]);
        let net = ScriptedNet::udp(vec![ch, info_reply()]);
        let info = a2s_info(&net, "srv").unwrap();
        assert_eq!(info.name, "My Server");
        assert_eq!(info.map, "de_dust2");
        assert_eq!((info.players, info.max_players, info.bots), (12, 24, 2));
        assert_eq!(info.version, "1.38.0");
        let sent = net.sent();
        assert_eq!(sent.len(), 2);
        assert_eq!(&sent[1][sent[1].len() - 4..], &[1, 2, 3, 4]);
    }

    #[test]
    fn a2s_rejects_garbage_and_fragments() {
        let net = ScriptedNet::udp(vec![vec![1, 2, 3, 4, 5, 6]]);
        assert!(matches!(a2s_info(&net, "s"), Err(ProtoError::Malformed(_))));
        let net = ScriptedNet::udp(vec![vec![0xfe, 0xff, 0xff, 0xff, 0, 0]]);
        assert!(matches!(
            a2s_info(&net, "s"),
            Err(ProtoError::Unsupported(_))
        ));
        let net = ScriptedNet::udp(vec![A2S_HEADER.to_vec()]);
        assert!(matches!(a2s_info(&net, "s"), Err(ProtoError::Malformed(_))));
    }

    #[test]
    fn a2s_players_decodes_names_up_to_limit() {
        let mut r = A2S_HEADER.to_vec();
        r.push(b'D');
        r.push(2);
        for (i, n) in ["alpha", "bravo"].iter().enumerate() {
            r.push(i as u8);
            r.extend_from_slice(n.as_bytes());
            r.push(0);
            r.extend_from_slice(&[0; 8]);
        }
        let net = ScriptedNet::udp(vec![r.clone()]);
        let p = a2s_players(&net, "s", 10).unwrap();
        assert_eq!(p.len(), 2);
        let net = ScriptedNet::udp(vec![r]);
        assert_eq!(a2s_players(&net, "s", 1).unwrap().len(), 1);
    }

    fn mc_frame(json: &str) -> Vec<u8> {
        let mut inner = vec![0x00];
        put_varint(&mut inner, json.len() as i32);
        inner.extend_from_slice(json.as_bytes());
        let mut out = Vec::new();
        put_varint(&mut out, inner.len() as i32);
        out.extend(inner);
        out
    }

    #[test]
    fn mc_ping_decodes_status_and_flattens_motd() {
        let json = r#"{"version":{"name":"1.21","protocol":767},"players":{"max":20,"online":2,"sample":[{"name":"Steve","id":"x"}]},"description":{"text":"Hi ","extra":[{"text":"there"}]}}"#;
        let mut s = ScriptedStream::new(mc_frame(json), 5);
        let st = mc_ping(&mut s).unwrap();
        assert_eq!(st.motd, "Hi there");
        assert_eq!(st.version, "1.21");
        assert_eq!((st.players, st.max_players), (2, 20));
        assert_eq!(st.sample, vec!["Steve".to_string()]);
        assert_eq!(s.written, mc_ping_request());
    }

    #[test]
    fn mc_ping_rejects_oversized_and_bad_frames() {
        let mut huge = Vec::new();
        put_varint(&mut huge, 10_000_000);
        let mut s = ScriptedStream::new(huge, 5);
        assert!(matches!(mc_ping(&mut s), Err(ProtoError::Malformed(_))));
        let mut s = ScriptedStream::new(mc_frame("not json"), 5);
        assert!(matches!(mc_ping(&mut s), Err(ProtoError::Malformed(_))));
        let mut s = ScriptedStream::new(vec![0xff; 6], 5);
        assert!(matches!(mc_ping(&mut s), Err(ProtoError::Malformed(_))));
    }

    #[test]
    fn varint_roundtrip() {
        for v in [0, 1, 127, 128, 25565, -1] {
            let mut b = Vec::new();
            put_varint(&mut b, v);
            let mut s = ScriptedStream::new(b, 1);
            assert_eq!(Reader::new(&mut s).varint().unwrap(), v);
        }
    }
}
