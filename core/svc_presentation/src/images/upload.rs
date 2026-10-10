//! P6: `POST /{overlay_code}/image/push` -- authenticated (code-resolving PUSH
//! guard, `crate::overlay::router::with_push_guard`, which applies
//! `overlay_auth::authorize_push`) multipart upload of one overlay image asset.
//! The literal `image` path segment plays the generic PUSH route's `{surface}`
//! role (`overlay_auth::push::push_scope` is keyed on `community_id` alone,
//! not surface, so this reuses the exact same guard/scope machinery every
//! other surface's push route uses). The guard resolves the overlay code to the
//! community and the handler trusts only the verified credential's
//! `community_id`.
//!
//! Gated on [`crate::flags::IMAGE_UPLOAD_FLAG`] (default OFF for a
//! never-seen flag) -- `rules/general.md` Red Flags: every merged feature
//! ships behind a flag.

use axum::extract::{Multipart, State};
use axum::http::StatusCode;
use axum::{Extension, Json};
use overlay_auth::PushCredential;
use serde::Serialize;
use sha2::{Digest, Sha256};
use utoipa::ToSchema;
use uuid::Uuid;

use crate::error::ApiError;
use crate::http::AppState;
use crate::images::asset_store::NewImageAsset;

/// Content types this endpoint accepts, and the file extension each maps
/// to for the stored object key -- `docs/guides/seaweedfs-object-storage.md`
/// "Multi-Format Support" lists the same four plus SVG; SVG is
/// deliberately excluded here (an SVG can embed `<script>`, making it an
/// XSS vector if ever rendered inline rather than via `<img src>` --
/// `rules/security.md` Input Validation) until that risk is explicitly
/// reviewed.
const ALLOWED_CONTENT_TYPES: &[(&str, &str)] = &[
    ("image/png", "png"),
    ("image/jpeg", "jpg"),
    ("image/gif", "gif"),
    ("image/webp", "webp"),
];

/// Returns the allowed image content type whose magic bytes `data` starts
/// with, or `None`. The declared `Content-Type` is client-controlled, so
/// only the sniffed type is trusted (stored-XSS guard: a spoofed header over
/// an HTML payload must never reach the bucket).
fn sniff_content_type(data: &[u8]) -> Option<&'static str> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if data.starts_with(&[0xFF, 0xD8, 0xFF]) {
        Some("image/jpeg")
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if data.len() >= 12 && &data[0..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        Some("image/webp")
    } else {
        None
    }
}

fn extension_for(content_type: &str) -> Option<&'static str> {
    ALLOWED_CONTENT_TYPES
        .iter()
        .find(|(ct, _)| *ct == content_type)
        .map(|(_, ext)| *ext)
}

/// `POST /{overlay_code}/image/push` response body.
#[derive(Debug, Serialize, ToSchema)]
pub struct UploadImageResponse {
    /// Opaque reference a later `Surface::image` push's `OverlayPush.
    /// image_url` field carries -- see `crate::images` module doc.
    pub asset_id: Uuid,
    pub content_type: String,
    pub size_bytes: u64,
    pub sha256: String,
}

/// One multipart field's accumulated numeric display-param value, parsed
/// from its UTF-8 text body. `None`/parse failure is just "not set" --
/// display params are all optional (`rules/general.md` Red Flags: never
/// reject the whole upload over an optional decorative field).
async fn read_optional_i32(field: axum::extract::multipart::Field<'_>) -> Option<i32> {
    field.text().await.ok()?.trim().parse().ok()
}

async fn read_optional_i64(field: axum::extract::multipart::Field<'_>) -> Option<i64> {
    field.text().await.ok()?.trim().parse().ok()
}

#[derive(Default)]
struct ParsedUpload {
    bytes: Option<bytes::Bytes>,
    content_type: Option<String>,
    position_x: Option<i32>,
    position_y: Option<i32>,
    width: Option<i32>,
    height: Option<i32>,
    duration_ms: Option<i64>,
}

async fn parse_multipart(mut multipart: Multipart) -> Result<ParsedUpload, ApiError> {
    let mut parsed = ParsedUpload::default();
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|err| ApiError::BadRequest(format!("invalid multipart body: {err}")))?
    {
        let Some(name) = field.name().map(str::to_owned) else {
            tracing::debug!(
                op = "parse_multipart",
                "multipart field missing a name; rejecting"
            );
            return Err(ApiError::BadRequest(
                "multipart field is missing a name".to_string(),
            ));
        };
        match name.as_str() {
            "file" => {
                parsed.content_type = field.content_type().map(str::to_string);
                parsed.bytes = Some(field.bytes().await.map_err(|err| {
                    ApiError::BadRequest(format!("failed reading file field: {err}"))
                })?);
            }
            "position_x" => parsed.position_x = read_optional_i32(field).await,
            "position_y" => parsed.position_y = read_optional_i32(field).await,
            "width" => parsed.width = read_optional_i32(field).await,
            "height" => parsed.height = read_optional_i32(field).await,
            "duration_ms" => parsed.duration_ms = read_optional_i64(field).await,
            _ => {
                // Unrecognized field -- ignored, not rejected (forward
                // compatible with a future client sending extra metadata
                // this version doesn't understand yet).
            }
        }
    }
    Ok(parsed)
}

/// The P6 handler.
#[utoipa::path(
    post,
    path = "/{overlay_code}/image/push",
    responses(
        (status = 201, description = "Image asset stored", body = UploadImageResponse),
        (status = 400, description = "Invalid content type, size, or multipart body"),
        (status = 401, description = "Missing/invalid PUSH credential"),
        (status = 403, description = "PUSH credential scoped to a different community, or the feature flag is OFF"),
    )
)]
pub async fn upload_image(
    State(state): State<AppState>,
    Extension(credential): Extension<PushCredential>,
    multipart: Multipart,
) -> Result<(StatusCode, Json<UploadImageResponse>), ApiError> {
    if !state.image_upload_flag.enabled().await {
        return Err(ApiError::Forbidden(
            "image upload is not enabled for this deployment".to_string(),
        ));
    }

    let Some(image_store) = state.image_store.as_ref() else {
        return Err(ApiError::Internal(anyhow::anyhow!(
            "image bucket is not configured (IMAGE_BUCKET_ACCESS_KEY_ID/IMAGE_BUCKET_SECRET_ACCESS_KEY unset)"
        )));
    };

    let parsed = parse_multipart(multipart).await?;
    let bytes = parsed
        .bytes
        .ok_or_else(|| ApiError::BadRequest("missing required \"file\" field".to_string()))?;
    let content_type = parsed
        .content_type
        .ok_or_else(|| ApiError::BadRequest("\"file\" field had no content-type".to_string()))?;
    let Some(ext) = extension_for(&content_type) else {
        state
            .image_metrics
            .uploads_total
            .with_label_values(&["rejected_content_type"])
            .inc();
        return Err(ApiError::BadRequest(format!(
            "unsupported content type {content_type:?} -- allowed: png, jpeg, gif, webp"
        )));
    };

    let max_bytes = state.config.cli.image_max_bytes;
    if bytes.len() as u64 > max_bytes {
        state
            .image_metrics
            .uploads_total
            .with_label_values(&["rejected_size"])
            .inc();
        return Err(ApiError::BadRequest(format!(
            "image is {} bytes, exceeds the configured {max_bytes}-byte maximum",
            bytes.len()
        )));
    }

    if sniff_content_type(&bytes) != Some(content_type.as_str()) {
        state
            .image_metrics
            .uploads_total
            .with_label_values(&["rejected_magic_bytes"])
            .inc();
        return Err(ApiError::BadRequest(
            "file content does not match the declared image content type".to_string(),
        ));
    }

    let sha256 = {
        let mut hasher = Sha256::new();
        hasher.update(&bytes);
        format!("{:x}", hasher.finalize())
    };

    let asset_id = Uuid::new_v4();
    // Typed `i64` (verified credential) + `Uuid` + allowlisted extension:
    // no client-controlled string reaches the key.
    let community_id: i64 = credential.community_id;
    let object_key = format!(
        "{}/{community_id}/{asset_id}.{ext}",
        state.config.cli.image_bucket_prefix
    );
    let size_bytes = bytes.len() as i64;

    image_store
        .put(&object_key, bytes, &content_type)
        .await
        .map_err(|err| {
            state
                .image_metrics
                .uploads_total
                .with_label_values(&["error"])
                .inc();
            ApiError::Internal(err.into())
        })?;

    state
        .image_asset_store
        .insert(NewImageAsset {
            community_id,
            asset_id,
            object_key: object_key.clone(),
            content_type: content_type.clone(),
            size_bytes,
            sha256: sha256.clone(),
            position_x: parsed.position_x,
            position_y: parsed.position_y,
            width: parsed.width,
            height: parsed.height,
            duration_ms: parsed.duration_ms,
        })
        .await
        .map_err(|err| {
            state
                .image_metrics
                .uploads_total
                .with_label_values(&["error"])
                .inc();
            ApiError::Internal(err.into())
        })?;

    state
        .image_metrics
        .uploads_total
        .with_label_values(&["success"])
        .inc();
    state.image_metrics.upload_bytes.observe(size_bytes as f64);

    tracing::info!(
        community_id,
        asset_id = %asset_id,
        size_bytes,
        content_type = %content_type,
        "overlay image uploaded"
    );

    Ok((
        StatusCode::CREATED,
        Json(UploadImageResponse {
            asset_id,
            content_type,
            size_bytes: size_bytes as u64,
            sha256,
        }),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extension_for_recognizes_every_allowed_content_type() {
        assert_eq!(extension_for("image/png"), Some("png"));
        assert_eq!(extension_for("image/jpeg"), Some("jpg"));
        assert_eq!(extension_for("image/gif"), Some("gif"));
        assert_eq!(extension_for("image/webp"), Some("webp"));
    }

    #[test]
    fn sniff_recognizes_each_allowed_format_and_rejects_html() {
        assert_eq!(
            sniff_content_type(b"\x89PNG\r\n\x1a\nxx"),
            Some("image/png")
        );
        assert_eq!(
            sniff_content_type(&[0xFF, 0xD8, 0xFF, 0xE0]),
            Some("image/jpeg")
        );
        assert_eq!(sniff_content_type(b"GIF89a.."), Some("image/gif"));
        assert_eq!(sniff_content_type(b"GIF87a.."), Some("image/gif"));
        assert_eq!(
            sniff_content_type(b"RIFF\0\0\0\0WEBPVP8 "),
            Some("image/webp")
        );
        assert_eq!(sniff_content_type(b"RIFF\0\0\0\0WAVEfmt "), None);
        assert_eq!(sniff_content_type(b"<html><script>x</script>"), None);
        assert_eq!(sniff_content_type(b""), None);
    }

    #[test]
    fn extension_for_rejects_svg_and_other_unlisted_types() {
        assert_eq!(extension_for("image/svg+xml"), None);
        assert_eq!(extension_for("application/octet-stream"), None);
        assert_eq!(extension_for("text/html"), None);
    }
}
