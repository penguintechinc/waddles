//! The executor's half of the `http` / `send` host-call JSON wire -- the one
//! encoder/decoder both the `stage` world (`crate::host::imports`) and the
//! `connector` world (`crate::host::connector_imports`) share, so the two
//! can never drift apart from each other or from the stage-side guard.
//!
//! The stage half is `core/bundle_host_http`'s `EgressGuard::send`, which
//! this crate cannot link (its `reqwest` dependency is banned from this
//! binary -- `tests/dependency_policy.rs`). The contract is therefore stated
//! here, mirrored by that crate's own module doc, and pinned end to end by
//! `core/bundle_host_http/tests/executor_wire_e2e.rs` (real executor, real
//! guard) -- change one half without the other and that test fails.
//!
//! **Request** (`args` of the `host-call`):
//!
//! ```json
//! { "method": "POST", "url": "https://...",
//!   "headers": [{"name": "content-type", "value": "application/json"}],
//!   "body_base64": "<standard base64 of the body bytes; key absent = no body>",
//!   "secret_refs": [["Authorization", "slot-ref"], ["?key", "other-ref"]] }
//! ```
//!
//! **Response** (`result` of the `host-result`):
//!
//! ```json
//! { "status": 200,
//!   "headers": [{"name": "content-type", "value": "text/plain"}],
//!   "body_base64": "<standard base64 of the body bytes>",
//!   "truncated": false }
//! ```
//!
//! Bodies are base64 strings, not JSON byte arrays: an HTTP body can be
//! megabytes, and an array of small integers inflates it ~3.5x on the wire
//! and in the JSON parse, while base64 is ~1.33x and byte-exact with the WIT
//! `list<u8>`. Decoding is strict -- a missing or mistyped field, a header
//! lacking `name` or `value`, or an invalid base64 body is a "malformed
//! host-result" error, never a silently empty body or header set
//! (the original defect: this side serialized `body` as a byte array and
//! decoded `headers` as `[(name, value)]`, so every request body was
//! dropped by the guard and every response with headers failed to decode).

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use serde::{Deserialize, Serialize};

/// One header on the wire, borrowed for encoding.
#[derive(Serialize)]
struct HeaderOut<'a> {
    name: &'a str,
    value: &'a str,
}

/// The `http.send` request `args` object, borrowed for encoding.
#[derive(Serialize)]
struct RequestWire<'a> {
    method: &'a str,
    url: &'a str,
    headers: Vec<HeaderOut<'a>>,
    /// Omitted entirely (not `null`) when the guest sent no body, so "no
    /// body" and "empty body" (`""`) stay distinguishable on the wire.
    #[serde(skip_serializing_if = "Option::is_none")]
    body_base64: Option<String>,
    /// Serialized as a list of `[slot, ref]` pairs (a tuple serializes as a
    /// JSON array), the shape the guest's WIT `list<tuple<string, string>>`
    /// maps onto and the guard's decoder accepts.
    secret_refs: &'a [(String, String)],
}

/// One header on the wire, owned for decoding.
#[derive(Debug, Deserialize)]
struct HeaderIn {
    name: String,
    value: String,
}

/// The `http.send` response `result` object. Every field is required: the
/// guard always emits all four, so an absent one means a malformed or
/// version-skewed stage and must fail loudly rather than default.
#[derive(Debug, Deserialize)]
struct ResponseWire {
    status: u16,
    headers: Vec<HeaderIn>,
    body_base64: String,
    truncated: bool,
}

/// A decoded `http.send` response, free of any WIT-generated type so both
/// worlds' own `http::Response` can be built from it.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct DecodedResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
    pub truncated: bool,
}

/// Encodes one guest `http.send` request into the host-call `args` JSON.
///
/// Returns a message (not a panic) if serialization fails, which cannot
/// happen for these string/bytes-only shapes but is surfaced rather than
/// unwrapped per this crate's `unwrap_used`/`expect_used` deny lint.
pub(crate) fn encode_request<'a>(
    method: &str,
    url: &str,
    headers: impl IntoIterator<Item = (&'a str, &'a str)>,
    body: Option<&[u8]>,
    secret_refs: &[(String, String)],
) -> Result<serde_json::Value, String> {
    let wire = RequestWire {
        method,
        url,
        headers: headers
            .into_iter()
            .map(|(name, value)| HeaderOut { name, value })
            .collect(),
        body_base64: body.map(|bytes| BASE64.encode(bytes)),
        secret_refs,
    };
    serde_json::to_value(&wire).map_err(|e| format!("could not encode http.send request: {e}"))
}

/// Decodes the stage's `http.send` `result` JSON into a [`DecodedResponse`].
///
/// The error string is surfaced to the guest as
/// `http::Error::Transport("malformed host-result: <err>")`; it names the
/// offending field/shape but never echoes the response body.
pub(crate) fn decode_response(value: serde_json::Value) -> Result<DecodedResponse, String> {
    let wire: ResponseWire = serde_json::from_value(value).map_err(|e| e.to_string())?;
    let body = BASE64
        .decode(wire.body_base64.as_bytes())
        .map_err(|e| format!("body_base64 is not valid base64: {e}"))?;
    Ok(DecodedResponse {
        status: wire.status,
        headers: wire
            .headers
            .into_iter()
            .map(|h| (h.name, h.value))
            .collect(),
        body,
        truncated: wire.truncated,
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use serde_json::json;

    fn refs(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs
            .iter()
            .map(|(a, b)| ((*a).to_string(), (*b).to_string()))
            .collect()
    }

    #[test]
    fn request_body_is_base64_under_body_base64_and_byte_exact() {
        // Non-UTF-8 bytes on purpose: a lossy-text or array encoding would
        // corrupt or inflate these.
        let body = [0x00, 0xff, 0x10, b'h', b'i'];
        let args = encode_request("POST", "https://api.test/x", [], Some(&body), &[]).unwrap();
        assert_eq!(args["body_base64"], json!("AP8QaGk="));
        assert!(
            args.get("body").is_none(),
            "the legacy byte-array `body` key must never be sent: {args}"
        );
        assert_eq!(
            BASE64
                .decode(args["body_base64"].as_str().unwrap())
                .unwrap(),
            body
        );
    }

    #[test]
    fn absent_and_empty_bodies_stay_distinct() {
        let none = encode_request("GET", "https://api.test/", [], None, &[]).unwrap();
        assert!(none.get("body_base64").is_none(), "no body => key absent");
        let empty = encode_request("POST", "https://api.test/", [], Some(&[]), &[]).unwrap();
        assert_eq!(empty["body_base64"], json!(""));
    }

    #[test]
    fn headers_are_name_value_objects_and_secret_refs_are_pairs() {
        let args = encode_request(
            "GET",
            "https://api.test/",
            [("x-a", "1"), ("x-a", "2")],
            None,
            &refs(&[("Authorization", "tok"), ("?key", "k")]),
        )
        .unwrap();
        assert_eq!(
            args["headers"],
            json!([{"name": "x-a", "value": "1"}, {"name": "x-a", "value": "2"}])
        );
        assert_eq!(
            args["secret_refs"],
            json!([["Authorization", "tok"], ["?key", "k"]])
        );
        assert_eq!(args["method"], json!("GET"));
        assert_eq!(args["url"], json!("https://api.test/"));
    }

    #[test]
    fn decodes_exactly_the_shape_the_guard_emits() {
        // Verbatim shape of `EgressGuard::send`'s success value.
        let decoded = decode_response(json!({
            "status": 201,
            "headers": [
                {"name": "content-type", "value": "application/json"},
                {"name": "set-cookie", "value": "a=1"},
                {"name": "set-cookie", "value": "b=2"},
            ],
            "body_base64": "AP8QaGk=",
            "truncated": true,
        }))
        .unwrap();
        assert_eq!(decoded.status, 201);
        assert_eq!(
            decoded.headers,
            vec![
                ("content-type".to_string(), "application/json".to_string()),
                ("set-cookie".to_string(), "a=1".to_string()),
                ("set-cookie".to_string(), "b=2".to_string()),
            ]
        );
        assert_eq!(decoded.body, vec![0x00, 0xff, 0x10, b'h', b'i']);
        assert!(decoded.truncated);
    }

    #[test]
    fn empty_headers_and_body_decode() {
        let decoded = decode_response(json!({
            "status": 204, "headers": [], "body_base64": "", "truncated": false,
        }))
        .unwrap();
        assert!(decoded.headers.is_empty());
        assert!(decoded.body.is_empty());
    }

    #[test]
    fn malformed_responses_fail_loudly_not_silently_empty() {
        let good = json!({
            "status": 200, "headers": [], "body_base64": "aGk=", "truncated": false,
        });
        assert!(decode_response(good.clone()).is_ok());

        // Each mutation is a distinct way the wire has actually been (or
        // could be) wrong; every one must be an Err, never a default.
        let mutations: Vec<(&str, serde_json::Value)> = vec![
            ("not an object", json!("nope")),
            (
                "header without a value",
                json!({"status": 200, "headers": [{"name": "x"}], "body_base64": "", "truncated": false}),
            ),
            (
                "headers as a name->value map",
                json!({"status": 200, "headers": {"x": "y"}, "body_base64": "", "truncated": false}),
            ),
            (
                "legacy byte-array body",
                json!({"status": 200, "headers": [], "body": [1, 2, 3], "truncated": false}),
            ),
            (
                "missing body_base64",
                json!({"status": 200, "headers": [], "truncated": false}),
            ),
            (
                "missing headers",
                json!({"status": 200, "body_base64": "", "truncated": false}),
            ),
            (
                "missing truncated",
                json!({"status": 200, "headers": [], "body_base64": ""}),
            ),
            (
                "missing status",
                json!({"headers": [], "body_base64": "", "truncated": false}),
            ),
            (
                "body_base64 not a string",
                json!({"status": 200, "headers": [], "body_base64": [1], "truncated": false}),
            ),
            (
                "invalid base64",
                json!({"status": 200, "headers": [], "body_base64": "***", "truncated": false}),
            ),
            (
                "status out of u16 range",
                json!({"status": 70000, "headers": [], "body_base64": "", "truncated": false}),
            ),
        ];
        for (label, value) in mutations {
            assert!(
                decode_response(value).is_err(),
                "{label}: must be rejected as malformed"
            );
        }
    }

    #[test]
    fn invalid_base64_error_names_the_field_without_echoing_the_input() {
        let err = decode_response(json!({
            "status": 200, "headers": [], "body_base64": "sekrit-token!!", "truncated": false,
        }))
        .unwrap_err();
        assert!(err.contains("body_base64"), "{err}");
        assert!(!err.contains("sekrit-token"), "{err}");
    }
}
