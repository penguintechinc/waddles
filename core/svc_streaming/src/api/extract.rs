//! `axum::Json<T>` wrapper whose rejection is [`ApiError::BadRequest`]
//! instead of axum's built-in `JsonRejection` body shape, so malformed or
//! schema-violating request bodies (including
//! `#[serde(deny_unknown_fields)]` rejections on the DTOs in
//! [`crate::api::dto`]) come back through the same `{error, message}`
//! envelope as every other failure in this service. Also
//! [`DbConn`], the DB-access extractor every handler uses -- see its own
//! doc comment for why.

use axum::extract::{FromRequest, FromRequestParts, Request};
use axum::http::request::Parts;
use axum::Json;
use sea_orm::DatabaseConnection;
use serde::de::DeserializeOwned;

use crate::error::ApiError;
use crate::http::AppState;

#[derive(Debug)]
pub struct ValidatedJson<T>(pub T);

impl<T> FromRequest<AppState> for ValidatedJson<T>
where
    T: DeserializeOwned,
{
    type Rejection = ApiError;

    async fn from_request(req: Request, state: &AppState) -> Result<Self, Self::Rejection> {
        let Json(value) = Json::<T>::from_request(req, state)
            .await
            .map_err(|rejection| ApiError::BadRequest(rejection.to_string()))?;
        Ok(ValidatedJson(value))
    }
}

/// Resolves a `DatabaseConnection` for the request: if
/// [`crate::api::router_for_testing`] layered one in via `Extension`
/// (tests), that value is used as-is; otherwise (production, via
/// [`crate::api::router`]) it lazily connects via
/// [`crate::db::get_or_connect`] using `state.config`. A single extractor
/// covering both cases means every handler uses `DbConn(db): DbConn`
/// regardless of which router constructor built it -- `AppState`
/// (`src/http/mod.rs`, owned by a different chunk) has no `db` field to
/// extract via `State<AppState>` instead.
pub struct DbConn(pub DatabaseConnection);

impl FromRequestParts<AppState> for DbConn {
    type Rejection = ApiError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &AppState,
    ) -> Result<Self, Self::Rejection> {
        if let Some(db) = parts.extensions.get::<DatabaseConnection>() {
            return Ok(DbConn(db.clone()));
        }
        let db = crate::db::get_or_connect(&state.config)
            .await
            .map_err(|err| ApiError::Internal(err.into()))?;
        Ok(DbConn(db))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request as HttpRequest;
    use serde::Deserialize;

    use crate::config::{CliConfig, Config, Secret};
    use clap::Parser;

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Sample {
        #[allow(dead_code)]
        name: String,
    }

    fn test_state() -> AppState {
        let cli = CliConfig::parse_from(["svc-streaming"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
            service_api_key: Secret::new("x"),
            jwt_keys: crate::jwt_keys::JwtKeys::None,
        };
        AppState::new(config, prometheus::Registry::new())
    }

    #[tokio::test]
    async fn valid_body_extracts() {
        let state = test_state();
        let req = HttpRequest::builder()
            .method("POST")
            .header("content-type", "application/json")
            .body(Body::from(r#"{"name":"a"}"#))
            .unwrap();
        let ValidatedJson(sample) = ValidatedJson::<Sample>::from_request(req, &state)
            .await
            .expect("valid body must extract");
        assert_eq!(sample.name, "a");
    }

    #[tokio::test]
    async fn unknown_field_is_bad_request_not_axum_default_body() {
        let state = test_state();
        let req = HttpRequest::builder()
            .method("POST")
            .header("content-type", "application/json")
            .body(Body::from(r#"{"name":"a","extra":"nope"}"#))
            .unwrap();
        let err = ValidatedJson::<Sample>::from_request(req, &state)
            .await
            .unwrap_err();
        assert!(matches!(err, ApiError::BadRequest(_)));
    }
}
