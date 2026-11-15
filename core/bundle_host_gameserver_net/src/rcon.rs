//! Source / Minecraft RCON framing: the host-side login and the wire validator for guest writes.
//!
//! Packet: `[size:i32le][id:i32le][type:i32le][body..][0][0]`, `size = 10 + body.len()`.
//! Types: 3 = auth, 2 = auth-response (login) / exec-command (guest), 0 = response-value.

use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

use crate::error::NetError;
use crate::secret::Secret;

pub(crate) const TYPE_RESPONSE: i32 = 0;
pub(crate) const TYPE_EXEC_OR_AUTH_RESPONSE: i32 = 2;
pub(crate) const TYPE_AUTH: i32 = 3;
/// Largest packet the login reader will accept from a server (login replies are tiny).
const LOGIN_MAX_PACKET: usize = 4096 + 10;
/// Longest password the host will send.
pub const MAX_PASSWORD_LEN: usize = 1024;
/// Login request id (any positive value; the server echoes it on success, `-1` on failure).
const LOGIN_ID: i32 = 0x5741_4444;

/// Encodes one RCON packet.
pub fn encode_packet(id: i32, ty: i32, body: &[u8]) -> Vec<u8> {
    let size = (10 + body.len()) as i32;
    let mut out = Vec::with_capacity(14 + body.len());
    out.extend_from_slice(&size.to_le_bytes());
    out.extend_from_slice(&id.to_le_bytes());
    out.extend_from_slice(&ty.to_le_bytes());
    out.extend_from_slice(body);
    out.extend_from_slice(&[0, 0]);
    out
}

/// Reads one packet (`id`, `type`, `body_len`), bounding the declared size by `max_packet`.
async fn read_packet<R: AsyncRead + Unpin>(
    r: &mut R,
    max_packet: usize,
) -> Result<(i32, i32, usize), NetError> {
    let mut size = [0u8; 4];
    r.read_exact(&mut size)
        .await
        .map_err(|_| NetError::Unreachable)?;
    let size = i32::from_le_bytes(size);
    if !(10..=max_packet as i32).contains(&size) {
        return Err(NetError::Backend);
    }
    let mut rest = vec![0u8; size as usize];
    r.read_exact(&mut rest)
        .await
        .map_err(|_| NetError::Unreachable)?;
    let id = i32::from_le_bytes([rest[0], rest[1], rest[2], rest[3]]);
    let ty = i32::from_le_bytes([rest[4], rest[5], rest[6], rest[7]]);
    Ok((id, ty, size as usize - 10))
}

/// Performs the RCON login on `stream` with `password` (type-3 auth; success = a type-2 reply
/// echoing our id; `id == -1` = rejected). A leading empty type-0 packet (Source) is skipped.
/// The password is written to the socket only; it is not returned, logged or put in an error.
pub async fn login<S>(stream: &mut S, password: &Secret, deadline: Duration) -> Result<(), NetError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    if password.expose().len() > MAX_PASSWORD_LEN {
        return Err(NetError::Denied("invalid_target"));
    }
    let pkt = encode_packet(LOGIN_ID, TYPE_AUTH, password.expose().as_bytes());
    let exchange = async {
        stream
            .write_all(&pkt)
            .await
            .map_err(|_| NetError::Unreachable)?;
        for _ in 0..3 {
            let (id, ty, _len) = read_packet(stream, LOGIN_MAX_PACKET).await?;
            if ty == TYPE_EXEC_OR_AUTH_RESPONSE {
                return if id == LOGIN_ID {
                    Ok(())
                } else {
                    Err(NetError::AuthFailed)
                };
            }
            if id == -1 {
                return Err(NetError::AuthFailed);
            }
        }
        Err(NetError::Backend)
    };
    match tokio::time::timeout(deadline, exchange).await {
        Ok(r) => r,
        Err(_) => Err(NetError::Timeout),
    }
}

/// What a validated guest write contained, for auditing.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct WriteSummary {
    /// Command bodies (type-2 packets) in order.
    pub commands: Vec<String>,
}

/// Validates guest bytes written to an AUTHENTICATED RCON session: whole well-formed packets only,
/// types 0 (sentinel) and 2 (exec) only -- never type 3 (no re-auth) -- exec bodies at most
/// `max_command_len` bytes, printable ASCII/UTF-8 on a SINGLE line (no CR/LF/NUL/controls, so one
/// packet can never smuggle a second command). Anything else is `denied("command_rejected")`.
pub fn validate_guest_write(
    bytes: &[u8],
    max_command_len: usize,
) -> Result<WriteSummary, NetError> {
    let reject = || NetError::Denied("command_rejected");
    let mut summary = WriteSummary::default();
    let mut rest = bytes;
    if rest.is_empty() {
        return Err(reject());
    }
    while !rest.is_empty() {
        if rest.len() < 14 {
            return Err(reject());
        }
        let size = i32::from_le_bytes([rest[0], rest[1], rest[2], rest[3]]);
        if size < 10 {
            return Err(reject());
        }
        let size = size as usize;
        if rest.len() < 4 + size {
            return Err(reject());
        }
        let ty = i32::from_le_bytes([rest[8], rest[9], rest[10], rest[11]]);
        let body = &rest[12..4 + size - 2];
        if rest[4 + size - 2] != 0 || rest[4 + size - 1] != 0 {
            return Err(reject());
        }
        match ty {
            TYPE_EXEC_OR_AUTH_RESPONSE => {
                if body.len() > max_command_len {
                    return Err(reject());
                }
                let text = std::str::from_utf8(body).map_err(|_| reject())?;
                if text.chars().any(char::is_control) {
                    return Err(reject());
                }
                summary.commands.push(text.to_string());
            }
            TYPE_RESPONSE => {
                if !body.is_empty() {
                    return Err(reject());
                }
            }
            _ => return Err(reject()),
        }
        rest = &rest[4 + size..];
    }
    Ok(summary)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::duplex;

    #[test]
    fn encode_round_trips_layout() {
        let p = encode_packet(7, 2, b"status");
        assert_eq!(&p[0..4], &16i32.to_le_bytes());
        assert_eq!(&p[4..8], &7i32.to_le_bytes());
        assert_eq!(&p[8..12], &2i32.to_le_bytes());
        assert_eq!(&p[12..18], b"status");
        assert_eq!(&p[18..], &[0, 0]);
    }

    #[test]
    fn write_validator_accepts_exec_and_sentinel() {
        let mut b = encode_packet(1, 2, b"status");
        b.extend(encode_packet(2, 0, b""));
        let s = validate_guest_write(&b, 256).unwrap();
        assert_eq!(s.commands, vec!["status".to_string()]);
    }

    #[test]
    fn write_validator_rejects_auth_oversize_newline_and_garbage() {
        let rejected = |b: Vec<u8>| {
            assert_eq!(
                validate_guest_write(&b, 16).unwrap_err(),
                NetError::Denied("command_rejected")
            )
        };
        rejected(encode_packet(1, TYPE_AUTH, b"pw"));
        rejected(encode_packet(1, 2, &[b'a'; 17]));
        rejected(encode_packet(1, 2, b"say a\nstop"));
        rejected(encode_packet(1, 2, b"say a\rb"));
        rejected(encode_packet(1, 2, b"a\0b"));
        rejected(encode_packet(1, 9, b"x"));
        rejected(encode_packet(1, 0, b"non-empty"));
        rejected(vec![]);
        rejected(vec![1, 2, 3]);
        let mut trailing = encode_packet(1, 2, b"ok");
        trailing.push(0);
        rejected(trailing);
        let mut bad_term = encode_packet(1, 2, b"ok");
        let n = bad_term.len();
        bad_term[n - 1] = 1;
        rejected(bad_term);
        let mut huge = encode_packet(1, 2, b"ok");
        huge[0..4].copy_from_slice(&i32::MAX.to_le_bytes());
        rejected(huge);
    }

    async fn server_reply(mut s: tokio::io::DuplexStream, replies: Vec<Vec<u8>>) -> Vec<u8> {
        let mut head = [0u8; 4];
        s.read_exact(&mut head).await.unwrap();
        let size = i32::from_le_bytes(head) as usize;
        let mut rest = vec![0u8; size];
        s.read_exact(&mut rest).await.unwrap();
        for r in replies {
            s.write_all(&r).await.unwrap();
        }
        rest
    }

    #[tokio::test]
    async fn login_success_skips_leading_empty_response_and_sends_password_only_to_socket() {
        let (mut c, s) = duplex(4096);
        let server = tokio::spawn(server_reply(
            s,
            vec![
                encode_packet(LOGIN_ID, TYPE_RESPONSE, b""),
                encode_packet(LOGIN_ID, TYPE_EXEC_OR_AUTH_RESPONSE, b""),
            ],
        ));
        login(
            &mut c,
            &Secret::new("hunter2".into()),
            Duration::from_secs(2),
        )
        .await
        .unwrap();
        let seen = server.await.unwrap();
        assert!(seen.windows(7).any(|w| w == b"hunter2"));
    }

    #[tokio::test]
    async fn login_rejected_maps_to_auth_failed() {
        let (mut c, s) = duplex(4096);
        tokio::spawn(server_reply(
            s,
            vec![encode_packet(-1, TYPE_EXEC_OR_AUTH_RESPONSE, b"")],
        ));
        let err = login(&mut c, &Secret::new("bad".into()), Duration::from_secs(2))
            .await
            .unwrap_err();
        assert_eq!(err, NetError::AuthFailed);
        assert!(!format!("{err:?}{err}").contains("bad"));
    }

    #[tokio::test]
    async fn login_times_out_and_rejects_oversized_declared_packets() {
        let (mut c, _s) = duplex(4096);
        let err = login(&mut c, &Secret::new("p".into()), Duration::from_millis(50))
            .await
            .unwrap_err();
        assert_eq!(err, NetError::Timeout);

        let (mut c2, s2) = duplex(4096);
        let mut huge = vec![];
        huge.extend_from_slice(&(1_000_000i32).to_le_bytes());
        tokio::spawn(server_reply(s2, vec![huge]));
        let err = login(&mut c2, &Secret::new("p".into()), Duration::from_secs(2))
            .await
            .unwrap_err();
        assert_eq!(err, NetError::Backend);
    }

    #[tokio::test]
    async fn overlong_password_is_refused_before_any_io() {
        let (mut c, _s) = duplex(64);
        let err = login(
            &mut c,
            &Secret::new("x".repeat(MAX_PASSWORD_LEN + 1)),
            Duration::from_secs(1),
        )
        .await
        .unwrap_err();
        assert_eq!(err, NetError::Denied("invalid_target"));
    }
}
