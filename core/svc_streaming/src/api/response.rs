//! `{"status": "success", "data": ..., "meta": {"version": 1, "timestamp":
//! ...}}` response envelope for every successful handler in this module,
//! per `rules/backend.md` API Design. Error responses continue to use
//! [`crate::error::ApiError`]'s existing `{error, message}` shape --
//! `src/error.rs` is owned by a different chunk, so the `"status": "error"`
//! variant of the envelope is intentionally not implemented here.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Serialize;

#[derive(Debug, Serialize)]
struct Meta {
    version: u32,
    timestamp: String,
}

#[derive(Debug, Serialize)]
struct Envelope<T> {
    status: &'static str,
    data: T,
    meta: Meta,
}

/// Successful handler response, wrapping `data` in the standard envelope.
pub struct ApiSuccess<T> {
    status_code: StatusCode,
    data: T,
}

impl<T> ApiSuccess<T> {
    /// `200 OK`.
    pub fn ok(data: T) -> Self {
        Self {
            status_code: StatusCode::OK,
            data,
        }
    }

    /// Any other 2xx status (`201 Created` on the create-config/add-target
    /// routes).
    pub fn with_status(status_code: StatusCode, data: T) -> Self {
        Self { status_code, data }
    }
}

impl<T: Serialize> IntoResponse for ApiSuccess<T> {
    fn into_response(self) -> Response {
        let body = Envelope {
            status: "success",
            data: self.data,
            meta: Meta {
                version: 1,
                timestamp: crate::timestamp::to_rfc3339_offset(time::OffsetDateTime::now_utc()),
            },
        };
        (self.status_code, Json(body)).into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;

    #[tokio::test]
    async fn ok_envelope_has_success_status_and_default_200() {
        let resp = ApiSuccess::ok(serde_json::json!({"x": 1})).into_response();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(parsed["status"], "success");
        assert_eq!(parsed["data"]["x"], 1);
        assert_eq!(parsed["meta"]["version"], 1);
        assert!(parsed["meta"]["timestamp"].is_string());
    }

    #[tokio::test]
    async fn with_status_overrides_status_code() {
        let resp =
            ApiSuccess::with_status(StatusCode::CREATED, serde_json::json!({})).into_response();
        assert_eq!(resp.status(), StatusCode::CREATED);
    }
}
