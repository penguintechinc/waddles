//! Idiomatic wrapper over the WIT `http` interface (guarded outbound HTTP,
//! granted only when the bundle manifest's `egress` is non-empty;
//! `wit/waddle-bundle/stage.wit` `interface http`).

use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::error::{HttpError, SdkError};

/// One outbound request header.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Header {
    pub name: String,
    pub value: String,
}

/// A builder for the WIT `http` import's `request` record.
///
/// `secret_refs` never carries a secret *value* -- only a header name to
/// secret-reference-name mapping; the stage resolves the reference and
/// injects the header, and the value never enters the guest (spec
/// `interface http`'s `secret-refs` doc comment).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    pub method: String,
    pub url: String,
    pub headers: Vec<Header>,
    pub body: Option<Vec<u8>>,
    pub secret_refs: Vec<(String, String)>,
}

impl Request {
    pub fn new(method: impl Into<String>, url: impl Into<String>) -> Self {
        Self {
            method: method.into(),
            url: url.into(),
            headers: Vec::new(),
            body: None,
            secret_refs: Vec::new(),
        }
    }

    pub fn get(url: impl Into<String>) -> Self {
        Self::new("GET", url)
    }

    pub fn post(url: impl Into<String>) -> Self {
        Self::new("POST", url)
    }

    pub fn header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push(Header {
            name: name.into(),
            value: value.into(),
        });
        self
    }

    /// Marks `header_name` to be filled in by the stage from the secret
    /// reference `secret_ref_name` -- the value is never present in this
    /// struct or in guest memory.
    pub fn secret_header(
        mut self,
        header_name: impl Into<String>,
        secret_ref_name: impl Into<String>,
    ) -> Self {
        self.secret_refs
            .push((header_name.into(), secret_ref_name.into()));
        self
    }

    pub fn body_bytes(mut self, body: Vec<u8>) -> Self {
        self.body = Some(body);
        self
    }

    /// Serializes `body` as JSON, sets the body bytes, and adds a
    /// `content-type: application/json` header.
    pub fn json_body<T: Serialize>(self, body: &T) -> Result<Self, SdkError> {
        let bytes = serde_json::to_vec(body).map_err(SdkError::from)?;
        Ok(self
            .header("content-type", "application/json")
            .body_bytes(bytes))
    }
}

/// The WIT `http` import's `response` record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    pub headers: Vec<Header>,
    pub body: Vec<u8>,
    /// `true` if the host truncated the body to its configured cap.
    pub truncated: bool,
}

impl Response {
    pub fn is_success(&self) -> bool {
        (200..300).contains(&self.status)
    }

    /// The body decoded as UTF-8 text. Fails with
    /// [`HttpError::Transport`] on invalid UTF-8 rather than panicking or
    /// lossily replacing bytes -- a bundle author decides how to handle a
    /// malformed body.
    pub fn text(&self) -> Result<String, SdkError> {
        String::from_utf8(self.body.clone())
            .map_err(|err| SdkError::Http(HttpError::Transport(err.to_string())))
    }

    /// The body decoded as JSON into `T`.
    pub fn json<T: DeserializeOwned>(&self) -> Result<T, SdkError> {
        serde_json::from_slice(&self.body).map_err(SdkError::from)
    }
}

/// Sends `req` through the stage's guarded outbound HTTP path.
///
/// Only compiles for `wasm32` targets -- see `crate::bindings_glue`'s
/// module doc comment for the resulting host coverage carve-out.
#[cfg(target_arch = "wasm32")]
pub fn send(req: Request) -> Result<Response, SdkError> {
    crate::bindings_glue::http_send(req)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[test]
    fn get_builder_defaults_to_get_method_no_body() {
        let req = Request::get("https://example.test/api");
        assert_eq!(req.method, "GET");
        assert_eq!(req.body, None);
        assert!(req.secret_refs.is_empty());
    }

    #[test]
    fn secret_header_never_carries_the_secret_value() {
        let req = Request::post("https://example.test")
            .secret_header("Authorization", "twitch-bot-token");
        assert_eq!(
            req.secret_refs,
            vec![("Authorization".to_string(), "twitch-bot-token".to_string())]
        );
        assert!(req.headers.is_empty());
    }

    #[derive(Debug, Serialize, Deserialize, PartialEq)]
    struct Body {
        message: String,
    }

    #[test]
    fn json_body_sets_content_type_and_bytes() {
        let req = Request::post("https://example.test")
            .json_body(&Body {
                message: "hi".to_string(),
            })
            .expect("serializes");
        assert!(
            req.headers
                .iter()
                .any(|h| h.name == "content-type" && h.value == "application/json")
        );
        assert!(req.body.is_some());
    }

    #[test]
    fn response_is_success_checks_2xx_range() {
        let ok = Response {
            status: 204,
            headers: vec![],
            body: vec![],
            truncated: false,
        };
        let not_found = Response {
            status: 404,
            ..ok.clone()
        };
        assert!(ok.is_success());
        assert!(!not_found.is_success());
    }

    #[test]
    fn response_text_decodes_utf8_body() {
        let resp = Response {
            status: 200,
            headers: vec![],
            body: b"hello".to_vec(),
            truncated: false,
        };
        assert_eq!(resp.text().expect("valid utf8"), "hello");
    }

    #[test]
    fn response_text_rejects_invalid_utf8() {
        let resp = Response {
            status: 200,
            headers: vec![],
            body: vec![0xff, 0xfe],
            truncated: false,
        };
        assert!(resp.text().is_err());
    }

    #[test]
    fn response_json_decodes_typed_body() {
        let resp = Response {
            status: 200,
            headers: vec![],
            body: br#"{"message":"hi"}"#.to_vec(),
            truncated: false,
        };
        let decoded: Body = resp.json().expect("valid json body");
        assert_eq!(
            decoded,
            Body {
                message: "hi".to_string()
            }
        );
    }
}
