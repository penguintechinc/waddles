//! Minimal RTMP publish client (handshake + connect + publish) over
//! `rml_rtmp`, shared by the real-media e2e. Same logic as the copy inside
//! `e2e_rtmp_to_hls_fake.rs` (left untouched so that test's history stays
//! intact).

#![allow(dead_code)]

use rml_rtmp::handshake::{Handshake, HandshakeProcessResult, PeerType};
use rml_rtmp::rml_amf0::Amf0Value;
use rml_rtmp::sessions::{
    ClientSession, ClientSessionConfig, ClientSessionEvent, ClientSessionResult, PublishRequestType,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

// --- Minimal RTMP client (handshake + connect + publish), duplicated from
// tests/ingest_rtmp_publish.rs -- see this file's module doc.

pub async fn client_handshake(stream: &mut TcpStream) -> Vec<u8> {
    let mut handshake = Handshake::new(PeerType::Client);
    let p0_p1 = handshake
        .generate_outbound_p0_and_p1()
        .expect("generate c0/c1");
    stream.write_all(&p0_p1).await.expect("write c0/c1");

    let mut buf = [0u8; 4096];
    loop {
        let n = stream.read(&mut buf).await.expect("read handshake bytes");
        assert!(n > 0, "server closed connection during handshake");
        match handshake
            .process_bytes(&buf[..n])
            .expect("process handshake bytes")
        {
            HandshakeProcessResult::InProgress { response_bytes } => {
                if !response_bytes.is_empty() {
                    stream
                        .write_all(&response_bytes)
                        .await
                        .expect("write handshake response");
                }
            }
            HandshakeProcessResult::Completed {
                response_bytes,
                remaining_bytes,
            } => {
                if !response_bytes.is_empty() {
                    stream
                        .write_all(&response_bytes)
                        .await
                        .expect("write handshake response");
                }
                return remaining_bytes;
            }
        }
    }
}

pub async fn write_client_result(stream: &mut TcpStream, result: ClientSessionResult) {
    if let ClientSessionResult::OutboundResponse(packet) = result {
        stream
            .write_all(&packet.bytes)
            .await
            .expect("write outbound rtmp packet");
    }
}

pub async fn connect_and_publish(
    stream: &mut TcpStream,
    app_name: &str,
    stream_key: &str,
) -> Result<ClientSession, String> {
    let leftover = client_handshake(stream).await;
    let (mut session, _) =
        ClientSession::new(ClientSessionConfig::new()).expect("new client session");

    let mut pending: std::collections::VecDeque<ClientSessionResult> =
        std::collections::VecDeque::new();
    if !leftover.is_empty() {
        pending.extend(
            session
                .handle_input(&leftover)
                .expect("handle leftover bytes"),
        );
    }

    let connect_result = session
        .request_connection(app_name.to_string())
        .expect("request connection");
    write_client_result(stream, connect_result).await;

    let mut requested_publish = false;
    let mut buf = [0u8; 4096];
    loop {
        while let Some(result) = pending.pop_front() {
            match result {
                ClientSessionResult::OutboundResponse(packet) => {
                    stream
                        .write_all(&packet.bytes)
                        .await
                        .expect("write outbound rtmp packet");
                }
                ClientSessionResult::RaisedEvent(ClientSessionEvent::ConnectionRequestAccepted) => {
                    if !requested_publish {
                        requested_publish = true;
                        let publish_result = session
                            .request_publishing(stream_key.to_string(), PublishRequestType::Live)
                            .expect("request publishing");
                        pending.push_back(publish_result);
                    }
                }
                ClientSessionResult::RaisedEvent(
                    ClientSessionEvent::ConnectionRequestRejected { description },
                ) => {
                    return Err(format!("connection rejected: {description}"));
                }
                ClientSessionResult::RaisedEvent(ClientSessionEvent::PublishRequestAccepted) => {
                    return Ok(session);
                }
                ClientSessionResult::RaisedEvent(ClientSessionEvent::UnhandleableAmf0Command {
                    ..
                }) => {}
                ClientSessionResult::RaisedEvent(
                    ClientSessionEvent::UnknownTransactionResultReceived {
                        additional_values, ..
                    },
                ) => {
                    let description = additional_values
                        .iter()
                        .find_map(|value| match value {
                            Amf0Value::Object(props) => props
                                .get("description")
                                .and_then(|v| v.clone().get_string()),
                            _ => None,
                        })
                        .unwrap_or_else(|| "publish rejected".to_string());
                    return Err(description);
                }
                ClientSessionResult::RaisedEvent(other) => {
                    panic!("unexpected client event during connect/publish: {other:?}");
                }
                ClientSessionResult::UnhandleableMessageReceived(_) => {}
            }
        }

        let n = stream.read(&mut buf).await.expect("read server bytes");
        if n == 0 {
            return Err("server closed the connection".to_string());
        }
        pending.extend(
            session
                .handle_input(&buf[..n])
                .expect("handle server bytes"),
        );
    }
}
