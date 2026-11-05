//! Test-support seam for the connector golden-payload parity harness
//! (`tests/connector_parity.rs`, `docs/connector-parity-harness.md`).
//!
//! [`crate::wire::Connection`] is crate-private, so an integration test cannot
//! build a real [`HostBridge`] on its own. [`scripted_bridge`] does it over an
//! in-memory duplex against a scripted stage, so an overlay's `identity.lookup`
//! import exercises the REAL host-call path (tenant establishment, tenant
//! echo check, wire mapping) instead of a mock of it. Nothing here is reachable
//! from the production binary's request path.

use std::sync::{Arc, Mutex};

use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, Frame, HostCallBody, HostResultBody, HostResultError,
    Message,
};
use sha2::{Digest, Sha256};
use tokio::sync::mpsc;

use crate::host::HostBridge;
use crate::wire::{Connection, WRITER_QUEUE_CAPACITY};

/// Tenant the scripted stage reports; the identity reply echoes it back.
pub const PARITY_TENANT: &str = "parity";

/// Every host-call op the scripted stage saw, in order (`op` only -- never
/// args, which may carry platform user ids).
pub type CallLog = Arc<Mutex<Vec<String>>>;

/// Deterministic, UUID-shaped pseudonym for `(platform, platform_user_id)`:
/// what the harness's identity stub returns and what the expected files carry
/// as the tokenized `actor`. Not a real UUIDv5 -- a stable SHA-256 prefix with
/// the version/variant bits set, so it is shape-valid and collision-free
/// enough for a fixture corpus.
#[must_use]
pub fn pseudonym_uuid(platform: &str, platform_user_id: &str) -> String {
    let mut h = Sha256::new();
    h.update(b"waddles-parity-v1\0");
    h.update(platform.as_bytes());
    h.update(b"\0");
    h.update(platform_user_id.as_bytes());
    let d = h.finalize();
    let mut b = [0u8; 16];
    b.copy_from_slice(&d[..16]);
    b[6] = (b[6] & 0x0f) | 0x50;
    b[8] = (b[8] & 0x3f) | 0x80;
    let x: Vec<String> = b.iter().map(|v| format!("{v:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        x[0..4].concat(),
        x[4..6].concat(),
        x[6..8].concat(),
        x[8..10].concat(),
        x[10..16].concat()
    )
}

/// The scripted stage's answer to one host call: tenant context for
/// `get-context`, a pseudonymous unlinked record for a platform-identity
/// lookup (never a handle -- the fixture identities are not real users), and a
/// typed `not_found` for a uuid lookup or any other op.
fn answer(body: &HostCallBody) -> Result<serde_json::Value, HostResultError> {
    match body.capability {
        CapabilityKind::Context => Ok(serde_json::json!({
            "tenant": PARITY_TENANT, "community": null, "app_id": "parity"
        })),
        CapabilityKind::Log | CapabilityKind::Clock => Ok(serde_json::json!({})),
        CapabilityKind::Db if body.op == "identity.lookup" => {
            let a = &body.args;
            if a["kind"] == "platform-identity" {
                let platform = a["platform"].as_str().unwrap_or_default();
                let id = a["platform_user_id"].as_str().unwrap_or_default();
                Ok(serde_json::json!({
                    "uuid": pseudonym_uuid(platform, id),
                    "linked": false,
                    "handle": null,
                    "display_name": null,
                    "tenant": PARITY_TENANT,
                }))
            } else {
                Err(HostResultError {
                    code: "not_found".to_string(),
                    message: "parity stub resolves platform-identity only".to_string(),
                })
            }
        }
        _ => Err(HostResultError {
            code: "unknown_op".to_string(),
            message: format!("parity stage does not serve {}", body.op),
        }),
    }
}

/// Builds a real [`HostBridge`] wired to the scripted parity stage; returns it
/// with the log of ops the overlay actually invoked (so a report can show
/// `identity.lookup` was exercised, not bypassed).
#[must_use]
pub fn scripted_bridge() -> (Arc<HostBridge>, CallLog) {
    let (exec_io, stage_io) = tokio::io::duplex(64 * 1024);
    let (mut exec_reader, mut exec_writer) = tokio::io::split(exec_io);
    let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_QUEUE_CAPACITY);
    let connection = Connection::new(writer_tx);

    tokio::spawn(async move {
        while let Some(frame) = writer_rx.recv().await {
            if write_frame(&mut exec_writer, &frame).await.is_err() {
                break;
            }
        }
    });
    let reader_conn = Arc::clone(&connection);
    tokio::spawn(async move {
        while let Ok(frame) = read_frame(&mut exec_reader).await {
            let _ = reader_conn.deliver(frame);
        }
    });

    let log: CallLog = Arc::new(Mutex::new(Vec::new()));
    let stage_log = Arc::clone(&log);
    tokio::spawn(async move {
        let mut io = stage_io;
        while let Ok(frame) = read_frame(&mut io).await {
            let Message::HostCall(body) = frame.message else {
                continue;
            };
            if let Ok(mut l) = stage_log.lock() {
                l.push(body.op.clone());
            }
            let reply = match answer(&body) {
                Ok(v) => HostResultBody {
                    result: Some(v),
                    error: None,
                },
                Err(e) => HostResultBody {
                    result: None,
                    error: Some(e),
                },
            };
            if write_frame(&mut io, &Frame::new(frame.id, Message::HostResult(reply)))
                .await
                .is_err()
            {
                break;
            }
        }
    });

    (HostBridge::new(connection), log)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn pseudonym_is_stable_uuid_shaped_and_input_sensitive() {
        let a = pseudonym_uuid("twitch", "999");
        assert_eq!(a, pseudonym_uuid("twitch", "999"));
        assert_ne!(a, pseudonym_uuid("discord", "999"));
        assert_ne!(a, pseudonym_uuid("twitch", "998"));
        let parts: Vec<usize> = a.split('-').map(str::len).collect();
        assert_eq!(parts, vec![8, 4, 4, 4, 12]);
        assert_eq!(&a[14..15], "5");
    }

    #[test]
    fn answer_serves_identity_and_refuses_unknown_ops() {
        let call = |cap, op: &str, args| HostCallBody {
            app_id: "parity".to_string(),
            call_id: 1,
            capability: cap,
            op: op.to_string(),
            args,
        };
        let ok = answer(&call(
            CapabilityKind::Db,
            "identity.lookup",
            serde_json::json!({"kind":"platform-identity","platform":"twitch","platform_user_id":"1"}),
        ))
        .unwrap();
        assert_eq!(ok["uuid"], pseudonym_uuid("twitch", "1"));
        assert_eq!(ok["tenant"], PARITY_TENANT);
        let nf = answer(&call(
            CapabilityKind::Db,
            "identity.lookup",
            serde_json::json!({"kind":"uuid"}),
        ))
        .unwrap_err();
        assert_eq!(nf.code, "not_found");
        assert!(answer(&call(CapabilityKind::Kv, "kv.get", serde_json::json!({}))).is_err());
    }
}
