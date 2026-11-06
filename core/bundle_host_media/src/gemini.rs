//! Gemini adapter over [`GuardedClient`], pinned to `generativelanguage.googleapis.com`.
//!
//! Request shapes follow the official Gemini API docs (fetched 2026-11-06):
//! * Interactions API: <https://aistudio.google.com/docs/interactions-overview.md>
//! * Image (Nano Banana): <https://aistudio.google.com/docs/image-generation.md>
//!   -- `POST /v1beta/interactions` `{model, input:[{type:text,text}]}`; the
//!   legacy Imagen models were SHUT DOWN (<https://ai.google.dev/gemini-api/docs/imagen.md>).
//! * Music (Lyria 3.5): <https://aistudio.google.com/docs/music-generation.md> --
//!   `{model:"lyria-3.5", input, response_format:{type:"audio"}}`.
//! * Video (Gemini Omni Flash): <https://aistudio.google.com/docs/omni.md> --
//!   `{model:"gemini-omni-1.2-flash", input, response_format:{type:"video"}}`;
//!   Veo 3.1 preview was shut down 2026-10-22 so the spec's "Veo" is Omni here.
//! * Long-running: <https://aistudio.google.com/docs/background-execution.md> --
//!   `"background": true`, then `GET /v1beta/interactions/{id}` until
//!   `status` leaves `in_progress` (`completed` | `failed` | `cancelled`).
//!
//! Auth: `x-goog-api-key`; `Api-Revision: 2026-05-20`. Everything is on the one
//! pinned host (no Vertex host needed). Model ids are operator config, never
//! guest values; no guest string reaches the URL.

use std::net::IpAddr;
use std::time::Duration;

use async_trait::async_trait;
use base64::Engine;
use bundle_host_http::guarded::{GuardedClient, GuardedLimits, GuardedResponse};

use crate::credentials::ApiKey;
use crate::provider::{
    GeneratedMedia, MediaProvider, OperationId, PollOutcome, ProviderError, SubmitOutcome,
};
use crate::types::{MediaKind, PROVIDER_GEMINI};

/// The only host production traffic may reach.
pub const GEMINI_HOST: &str = "generativelanguage.googleapis.com";
/// API revision header value the documented request shapes are written against.
pub const API_REVISION: &str = "2026-05-20";

/// Operator-configured model ids (never guest values).
#[derive(Debug, Clone)]
pub struct GeminiModels {
    /// Image model.
    pub image: String,
    /// Music model.
    pub music: String,
    /// Video model.
    pub video: String,
}

impl Default for GeminiModels {
    fn default() -> Self {
        Self {
            image: "gemini-nano-banana-2.1".into(),
            music: "lyria-3.5".into(),
            video: "gemini-omni-1.2-flash".into(),
        }
    }
}

/// The Gemini [`MediaProvider`].
#[derive(Debug, Clone)]
pub struct GeminiProvider {
    base_url: String,
    allow: Vec<IpAddr>,
    models: GeminiModels,
    request_timeout: Duration,
}

impl GeminiProvider {
    /// Production: pinned to [`GEMINI_HOST`] over https, no IP exemptions.
    pub fn production(models: GeminiModels, request_timeout: Duration) -> Self {
        Self {
            base_url: format!("https://{GEMINI_HOST}"),
            allow: Vec::new(),
            models,
            request_timeout,
        }
    }

    /// TEST ONLY: points at a local fake. `allow` exempts exactly those IPs from
    /// the SSRF denylist (and permits plain http). Production never calls this.
    pub fn for_test_endpoint(
        base_url: String,
        allow: Vec<IpAddr>,
        models: GeminiModels,
        request_timeout: Duration,
    ) -> Self {
        Self {
            base_url,
            allow,
            models,
            request_timeout,
        }
    }

    fn client(&self, max_response_bytes: usize) -> GuardedClient {
        GuardedClient::new(
            GuardedLimits {
                max_redirects: 3,
                timeout: self.request_timeout,
                max_response_bytes,
            },
            self.allow.clone(),
        )
    }

    fn headers(key: &ApiKey) -> Vec<(String, String)> {
        vec![
            ("x-goog-api-key".into(), key.expose().to_string()),
            ("content-type".into(), "application/json".into()),
            ("api-revision".into(), API_REVISION.into()),
        ]
    }

    /// Response cap: base64 inflates by 4/3, plus JSON framing.
    fn cap(kind: MediaKind) -> usize {
        crate::validate::max_bytes(kind) / 3 * 4 + 64 * 1024
    }

    fn model(&self, kind: MediaKind) -> &str {
        match kind {
            MediaKind::Image => &self.models.image,
            MediaKind::Music => &self.models.music,
            MediaKind::Video => &self.models.video,
        }
    }

    fn create_body(&self, kind: MediaKind, prompt: &str, background: bool) -> serde_json::Value {
        let mut body = match kind {
            MediaKind::Image => serde_json::json!({
                "model": self.model(kind),
                "input": [{"type": "text", "text": prompt}],
            }),
            MediaKind::Music => serde_json::json!({
                "model": self.model(kind),
                "input": prompt,
                "response_format": {"type": "audio"},
            }),
            MediaKind::Video => serde_json::json!({
                "model": self.model(kind),
                "input": prompt,
                "response_format": {"type": "video"},
            }),
        };
        if background {
            body["background"] = serde_json::Value::Bool(true);
        }
        body
    }

    async fn send(
        &self,
        method: reqwest::Method,
        path: &str,
        key: &ApiKey,
        kind: MediaKind,
        body: Option<&serde_json::Value>,
    ) -> Result<GuardedResponse, ProviderError> {
        let url = format!("{}{}", self.base_url, path);
        let payload = match body {
            Some(b) => Some(
                serde_json::to_vec(b)
                    .map_err(|_| ProviderError::Backend("encode_request".into()))?,
            ),
            None => None,
        };
        let resp = self
            .client(Self::cap(kind))
            .fetch(method, &url, Self::headers(key), payload)
            .await
            .map_err(|e| {
                tracing::warn!(class = e.class(), "gemini egress failed");
                match e.class() {
                    "timeout" => ProviderError::Timeout,
                    _ => ProviderError::Backend(format!("egress_{}", e.class())),
                }
            })?;
        classify_status(&resp)?;
        Ok(resp)
    }
}

/// Maps non-2xx statuses onto the closed provider-error vocabulary.
fn classify_status(resp: &GuardedResponse) -> Result<(), ProviderError> {
    if (200..300).contains(&resp.status) {
        return Ok(());
    }
    let text = String::from_utf8_lossy(&resp.body).to_ascii_lowercase();
    let safety = ["safety", "blocked", "prohibited", "policy"]
        .iter()
        .any(|w| text.contains(w));
    Err(match resp.status {
        429 => ProviderError::Quota,
        408 | 504 => ProviderError::Timeout,
        400 | 403 if safety => ProviderError::Refused("safety".into()),
        401 | 403 => ProviderError::Backend("provider_auth_rejected".into()),
        s if s >= 500 => ProviderError::Backend("provider_5xx".into()),
        _ => ProviderError::Backend("provider_rejected_request".into()),
    })
}

/// What one Interactions response says.
enum Parsed {
    Pending(Option<String>),
    Done(GeneratedMedia),
}

/// Parses an Interaction resource: status, id and the first `model_output`
/// content block of the wanted type (`image`/`audio`/`video`).
fn parse_interaction(body: &[u8], want: &str) -> Result<Parsed, ProviderError> {
    let v: serde_json::Value = serde_json::from_slice(body)
        .map_err(|_| ProviderError::Backend("malformed_provider_response".into()))?;
    let id = v.get("id").and_then(|x| x.as_str()).map(str::to_string);
    match v.get("status").and_then(|s| s.as_str()) {
        Some("in_progress") => return Ok(Parsed::Pending(id)),
        Some("failed") => {
            let text = v.to_string().to_ascii_lowercase();
            return Err(
                if ["safety", "blocked", "prohibited", "policy"]
                    .iter()
                    .any(|w| text.contains(w))
                {
                    ProviderError::Refused("safety".into())
                } else if text.contains("resource_exhausted") || text.contains("rate") {
                    ProviderError::Quota
                } else {
                    ProviderError::Backend("provider_failed".into())
                },
            );
        }
        Some("cancelled" | "requires_action") => {
            return Err(ProviderError::Backend("provider_not_completed".into()))
        }
        Some("completed") | None => {}
        Some(_) => return Err(ProviderError::Backend("unknown_provider_status".into())),
    }
    let steps = v.get("steps").and_then(|s| s.as_array());
    for step in steps.into_iter().flatten() {
        if step.get("type").and_then(|t| t.as_str()) != Some("model_output") {
            continue;
        }
        for block in step
            .get("content")
            .and_then(|c| c.as_array())
            .into_iter()
            .flatten()
        {
            if block.get("type").and_then(|t| t.as_str()) != Some(want) {
                continue;
            }
            let data = block
                .get("data")
                .and_then(|d| d.as_str())
                .ok_or_else(|| ProviderError::Backend("provider_output_not_inline".into()))?;
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(data)
                .map_err(|_| ProviderError::Backend("provider_output_base64".into()))?;
            let claimed_mime = block
                .get("mime_type")
                .and_then(|m| m.as_str())
                .map(str::to_string);
            return Ok(Parsed::Done(GeneratedMedia {
                bytes,
                claimed_mime: claimed_mime
                    .ok_or_else(|| ProviderError::Backend("provider_output_no_mime".into()))?,
            }));
        }
    }
    // Completed with no media block: the usual shape of a silent safety block.
    Err(ProviderError::Refused("no_output".into()))
}

fn want_block(kind: MediaKind) -> &'static str {
    match kind {
        MediaKind::Image => "image",
        MediaKind::Music => "audio",
        MediaKind::Video => "video",
    }
}

/// An operation id from a provider response is only ever interpolated into a URL
/// path after this charset check.
fn valid_op_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 200
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-' || b == b'.')
}

#[async_trait]
impl MediaProvider for GeminiProvider {
    fn name(&self) -> &'static str {
        PROVIDER_GEMINI
    }

    async fn generate_image(
        &self,
        key: &ApiKey,
        prompt: &str,
    ) -> Result<GeneratedMedia, ProviderError> {
        let body = self.create_body(MediaKind::Image, prompt, false);
        let resp = self
            .send(
                reqwest::Method::POST,
                "/v1beta/interactions",
                key,
                MediaKind::Image,
                Some(&body),
            )
            .await?;
        match parse_interaction(&resp.body, want_block(MediaKind::Image))? {
            Parsed::Done(m) => Ok(m),
            Parsed::Pending(_) => Err(ProviderError::Backend("image_not_synchronous".into())),
        }
    }

    async fn submit(
        &self,
        kind: MediaKind,
        key: &ApiKey,
        prompt: &str,
    ) -> Result<SubmitOutcome, ProviderError> {
        if kind == MediaKind::Image {
            return Err(ProviderError::Backend("image_is_synchronous".into()));
        }
        let body = self.create_body(kind, prompt, true);
        let resp = self
            .send(
                reqwest::Method::POST,
                "/v1beta/interactions",
                key,
                kind,
                Some(&body),
            )
            .await?;
        match parse_interaction(&resp.body, want_block(kind))? {
            Parsed::Done(m) => Ok(SubmitOutcome::Done(m)),
            Parsed::Pending(Some(id)) if valid_op_id(&id) => {
                Ok(SubmitOutcome::Running(OperationId(id)))
            }
            Parsed::Pending(_) => Err(ProviderError::Backend(
                "missing_or_invalid_operation_id".into(),
            )),
        }
    }

    async fn poll(
        &self,
        kind: MediaKind,
        key: &ApiKey,
        op: &OperationId,
    ) -> Result<PollOutcome, ProviderError> {
        if !valid_op_id(&op.0) {
            return Err(ProviderError::Backend("invalid_operation_id".into()));
        }
        let path = format!("/v1beta/interactions/{}", op.0);
        let resp = self
            .send(reqwest::Method::GET, &path, key, kind, None)
            .await?;
        match parse_interaction(&resp.body, want_block(kind))? {
            Parsed::Done(m) => Ok(PollOutcome::Done(m)),
            Parsed::Pending(_) => Ok(PollOutcome::Pending),
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn op_id_charset_blocks_path_injection() {
        assert!(valid_op_id("v1_abc-DEF.9"));
        for bad in ["", "../x", "a/b", "a?b", "a b", "a%2f", &"x".repeat(201)] {
            assert!(!valid_op_id(bad), "{bad}");
        }
    }

    #[test]
    fn completed_without_media_is_a_refusal() {
        let b = br#"{"status":"completed","steps":[{"type":"model_output","content":[{"type":"text","text":"x"}]}]}"#;
        assert!(
            matches!(parse_interaction(b, "image"), Err(ProviderError::Refused(r)) if r == "no_output")
        );
    }

    #[test]
    fn failed_safety_is_refused_other_failures_are_backend() {
        let safe = br#"{"status":"failed","error":{"message":"blocked by safety"}}"#;
        assert!(matches!(
            parse_interaction(safe, "video"),
            Err(ProviderError::Refused(_))
        ));
        let other = br#"{"status":"failed","error":{"message":"internal"}}"#;
        assert!(matches!(
            parse_interaction(other, "video"),
            Err(ProviderError::Backend(_))
        ));
        let quota = br#"{"status":"failed","error":{"status":"RESOURCE_EXHAUSTED"}}"#;
        assert!(matches!(
            parse_interaction(quota, "video"),
            Err(ProviderError::Quota)
        ));
        let cancelled = br#"{"status":"cancelled"}"#;
        assert!(matches!(
            parse_interaction(cancelled, "video"),
            Err(ProviderError::Backend(_))
        ));
        assert!(matches!(
            parse_interaction(br#"{"status":"zzz"}"#, "video"),
            Err(ProviderError::Backend(_))
        ));
    }

    #[test]
    fn malformed_bodies_and_bad_blocks_are_backend_errors() {
        assert!(matches!(
            parse_interaction(b"not json", "image"),
            Err(ProviderError::Backend(_))
        ));
        let no_data = br#"{"status":"completed","steps":[{"type":"model_output","content":[{"type":"image","mime_type":"image/png","uri":"x"}]}]}"#;
        assert!(matches!(
            parse_interaction(no_data, "image"),
            Err(ProviderError::Backend(_))
        ));
        let bad_b64 = br#"{"steps":[{"type":"model_output","content":[{"type":"image","mime_type":"image/png","data":"!!!"}]}]}"#;
        assert!(matches!(
            parse_interaction(bad_b64, "image"),
            Err(ProviderError::Backend(_))
        ));
        let no_mime =
            br#"{"steps":[{"type":"model_output","content":[{"type":"image","data":"AAAA"}]}]}"#;
        assert!(matches!(
            parse_interaction(no_mime, "image"),
            Err(ProviderError::Backend(_))
        ));
    }

    #[test]
    fn http_status_classification_is_closed_vocabulary() {
        let r = |status: u16, body: &str| GuardedResponse {
            status,
            headers: vec![],
            body: body.as_bytes().to_vec(),
        };
        assert!(classify_status(&r(200, "")).is_ok());
        assert_eq!(classify_status(&r(429, "")), Err(ProviderError::Quota));
        assert_eq!(classify_status(&r(504, "")), Err(ProviderError::Timeout));
        assert_eq!(
            classify_status(&r(400, "Prompt was BLOCKED")),
            Err(ProviderError::Refused("safety".into()))
        );
        assert!(
            matches!(classify_status(&r(403, "forbidden")), Err(ProviderError::Backend(m)) if m == "provider_auth_rejected")
        );
        assert!(matches!(
            classify_status(&r(401, "")),
            Err(ProviderError::Backend(_))
        ));
        assert!(
            matches!(classify_status(&r(503, "")), Err(ProviderError::Backend(m)) if m == "provider_5xx")
        );
        assert!(
            matches!(classify_status(&r(400, "bad")), Err(ProviderError::Backend(m)) if m == "provider_rejected_request")
        );
    }

    #[test]
    fn request_bodies_match_documented_shapes() {
        let p = GeminiProvider::production(GeminiModels::default(), Duration::from_secs(1));
        let img = p.create_body(MediaKind::Image, "cat", false);
        assert_eq!(img["input"][0]["type"], "text");
        assert!(img.get("background").is_none());
        let mus = p.create_body(MediaKind::Music, "tune", true);
        assert_eq!(mus["model"], "lyria-3.5");
        assert_eq!(mus["response_format"]["type"], "audio");
        assert_eq!(mus["background"], true);
        let vid = p.create_body(MediaKind::Video, "clip", true);
        assert_eq!(vid["model"], "gemini-omni-1.2-flash");
        assert_eq!(vid["response_format"]["type"], "video");
    }
}
