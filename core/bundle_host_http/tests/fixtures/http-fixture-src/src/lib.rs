//! A real WASI 0.2 guest that calls the WIT `http.send` import and echoes
//! exactly what it got back, so `tests/executor_wire_e2e.rs` can prove the
//! executor <-> egress-guard `http.send` JSON wire end to end (request body
//! sent, response headers + body decoded) against genuine compiled WASM.
//!
//! `process-stage.transform` handles one event type, `http-send`, whose
//! `payload-json` is four space-separated fields:
//!
//! ```text
//! METHOD URL BODYHEX SECRET
//! ```
//!
//! `BODYHEX` is the request body as lowercase hex (`-` = no body, `0x`-less);
//! `SECRET` is `SLOT=REF` (a `secret-refs` entry: `Authorization=bot-token`,
//! `?key=weather-key`) or `-` for none. The guest always adds a
//! `content-type: application/octet-stream` and an `x-fixture: 1` header.
//!
//! The result is echoed into `payload-json` as
//!
//! ```text
//! ok;status=<n>;truncated=<bool>;headers=<name>:<value>|...;body_hex=<hex>
//! err;<variant>;<text>
//! ```

#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
use bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
use bindings::waddle::bundle::http;
use bindings::waddle::bundle::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};

struct Component;

/// Lowercase hex of `bytes`.
fn to_hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

/// Inverse of [`to_hex`]; `None` on any malformed input.
fn from_hex(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).ok())
        .collect()
}

/// Issues the `http.send` described by `spec` and renders the outcome.
fn http_send(spec: &str) -> String {
    let fields: Vec<&str> = spec.split(' ').collect();
    let [method, url, body_hex, secret] = fields[..] else {
        return format!("err;bad-spec;{spec}");
    };
    let body = if body_hex == "-" {
        None
    } else {
        match from_hex(body_hex) {
            Some(b) => Some(b),
            None => return "err;bad-spec;body hex".to_string(),
        }
    };
    let secret_refs = if secret == "-" {
        Vec::new()
    } else {
        match secret.split_once('=') {
            Some((slot, name)) => vec![(slot.to_string(), name.to_string())],
            None => return "err;bad-spec;secret".to_string(),
        }
    };
    let req = http::Request {
        method: method.to_string(),
        url: url.to_string(),
        headers: vec![
            http::Header {
                name: "content-type".to_string(),
                value: "application/octet-stream".to_string(),
            },
            http::Header {
                name: "x-fixture".to_string(),
                value: "1".to_string(),
            },
        ],
        body,
        secret_refs,
    };
    match http::send(&req) {
        Ok(resp) => {
            let headers: Vec<String> = resp
                .headers
                .iter()
                .map(|h| format!("{}:{}", h.name, h.value))
                .collect();
            format!(
                "ok;status={};truncated={};headers={};body_hex={}",
                resp.status,
                resp.truncated,
                headers.join("|"),
                to_hex(&resp.body)
            )
        }
        Err(http::Error::Denied(m)) => format!("err;denied;{m}"),
        Err(http::Error::Timeout) => "err;timeout;".to_string(),
        Err(http::Error::TooLarge(n)) => format!("err;too-large;{n}"),
        Err(http::Error::RateLimited(n)) => format!("err;rate-limited;{n}"),
        Err(http::Error::Transport(m)) => format!("err;transport;{m}"),
    }
}

impl ProcessGuest for Component {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let out = match event.event_type.as_str() {
            "http-send" => http_send(&event.payload_json),
            _ => {
                return Err(UnsupportedStage {
                    stage: "process".to_string(),
                });
            }
        };
        Ok(Some(PlatformEvent {
            payload_json: out,
            ..event
        }))
    }
}

impl ActionGuest for Component {
    fn dispatch(
        _envelope: StageEnvelope,
        _config: String,
    ) -> Result<TransportResult, TransportError> {
        Err(TransportError {
            retryable: false,
            code: "unsupported".to_string(),
            message: "http fixture implements no action stage".to_string(),
            retry_after_ms: None,
        })
    }
}

bindings::export!(Component with_types_in bindings);
