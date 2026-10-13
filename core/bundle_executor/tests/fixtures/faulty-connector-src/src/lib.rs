// Fault-injection connector (connector spec
// `docs/superpowers/specs/2026-09-28-connector-bundles.md` S2.6/S3.5; issue
// #840 B0). Exists ONLY to prove the host contains a misbehaving connector:
// `on-frame` branches on the raw frame bytes (UTF-8) and misbehaves on demand.
//
//   "ok"            -> one well-formed event
//   "malformed"     -> frame-error::malformed (a clean guest-reported error)
//   "trap"          -> an unreachable trap (guest fault)
//   "spin"          -> an unbounded CPU loop (fuel / epoch deadline must stop it)
//   "grow"          -> touches linear memory in 1 MiB steps up to 128 MiB
//                      (the per-instance memory cap must stop it)
//   "flood:<N>"     -> returns N events (host-side events-per-frame cap)
//
// Never a model for a real connector; kept separate from the Echo reference
// so the reference stays clean.
#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::connector::receiver::Guest as ReceiverGuest;
use bindings::exports::waddle::connector::sender::{
    Action, Guest as SenderGuest, HttpRequestTpl,
};
use bindings::waddle::connector::types::{
    ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
};

struct Component;

fn event(n: usize, at: &str) -> NormalizedEvent {
    NormalizedEvent {
        platform: "faulty".to_string(),
        event_type: "message".to_string(),
        actor: Some(format!("actor-{n}")),
        payload_json: format!("{{\"n\":{n}}}"),
        occurred_at: at.to_string(),
    }
}

impl ReceiverGuest for Component {
    fn on_connect(_ctx: ConnectionCtx) -> Result<HandshakePayload, FrameError> {
        Ok(HandshakePayload {
            bytes: b"faulty-hello".to_vec(),
            secret_refs: Vec::new(),
        })
    }

    fn on_frame(frame: RawFrame) -> Result<Vec<NormalizedEvent>, FrameError> {
        let cmd = String::from_utf8_lossy(&frame.bytes).into_owned();
        match cmd.as_str() {
            "ok" => Ok(vec![event(0, &frame.received_at)]),
            "malformed" => Err(FrameError::Malformed("injected malformed frame".into())),
            "trap" => unreachable!("injected guest trap"),
            "spin" => {
                let mut x: u64 = 1;
                loop {
                    x = std::hint::black_box(x.wrapping_mul(6364136223846793005).wrapping_add(1));
                }
            }
            "grow" => {
                let mut hog: Vec<Vec<u8>> = Vec::new();
                for _ in 0..128 {
                    let mut block = vec![0u8; 1024 * 1024];
                    for i in (0..block.len()).step_by(4096) {
                        block[i] = 1;
                    }
                    hog.push(block);
                }
                Ok(vec![event(hog.len(), &frame.received_at)])
            }
            other => match other.strip_prefix("flood:").and_then(|n| n.parse::<usize>().ok()) {
                Some(n) => Ok((0..n).map(|i| event(i, &frame.received_at)).collect()),
                None => Err(FrameError::Unsupported(format!(
                    "unknown fault command ({} bytes)",
                    frame.bytes.len()
                ))),
            },
        }
    }

    fn on_heartbeat_due(_ctx: ConnectionCtx) -> Result<Option<Vec<u8>>, FrameError> {
        Ok(Some(b"faulty-ping".to_vec()))
    }

    fn on_disconnect(_ctx: ConnectionCtx, _reason: String) {}
}

impl SenderGuest for Component {
    fn build_request(_action: Action) -> Result<HttpRequestTpl, FrameError> {
        Err(FrameError::Unsupported("faulty connector has no sender".to_string()))
    }
}

bindings::export!(Component with_types_in bindings);
