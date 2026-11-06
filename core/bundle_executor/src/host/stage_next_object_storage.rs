//! `object-storage`: the `world stage-next` import for host-mediated bundle object
//! storage (spec `docs/superpowers/specs/2026-11-06-bundle-object-storage.md`).
//! Cloned from the recipe in [`super::stage_next_platform_table`].
//!
//! Every call rides the same [`imports::call`](crate::host::imports::call) round
//! trip, carried as `capability = db`, `op = "object_storage.<op>"`. Nothing here
//! decides authority: the executor encodes the call, decodes the answer and maps
//! refusals onto the WIT `object-storage.error` variant. Tenant/community scope and
//! every S3 credential live in the stage -- NEVER an argument or a result.

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use penguin_bundle_host::wire::CapabilityKind;

use crate::engine::stage_next_world::waddle::bundle::object_storage;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

/// Largest object body the executor will forward (the host enforces its own cap too).
const MAX_BODY: usize = 25 * 1024 * 1024;
const MAX_STR: usize = 1024;

/// Maps a stage-side error code onto the WIT `object-storage.error` variant.
fn error_from(err: ExecutorError) -> object_storage::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "not_found" => object_storage::Error::NotFound,
            "invalid" | "invalid_args" => object_storage::Error::Invalid(message.clone()),
            "too_large" => object_storage::Error::TooLarge(message.parse().unwrap_or(u64::MAX)),
            "quota_exceeded" => object_storage::Error::QuotaExceeded,
            "rate_limited" => object_storage::Error::RateLimited,
            "backend" => object_storage::Error::Backend(message.clone()),
            "not_implemented" | "feature_disabled" => object_storage::Error::NotImplemented,
            // every gate/policy refusal (not_granted, revoked, quota, ...) is a denial
            _ => object_storage::Error::NotGranted,
        },
        other => object_storage::Error::Backend(other.to_string()),
    }
}

fn check_str(what: &str, v: &str) -> Result<(), object_storage::Error> {
    if v.len() > MAX_STR {
        return Err(object_storage::Error::Invalid(format!(
            "{what} must be <= {MAX_STR} bytes"
        )));
    }
    Ok(())
}

fn info_from(v: &serde_json::Value) -> Result<object_storage::ObjectInfo, object_storage::Error> {
    let bad = || object_storage::Error::Backend("malformed host-result: object info".to_string());
    Ok(object_storage::ObjectInfo {
        key: v
            .get("key")
            .and_then(|x| x.as_str())
            .ok_or_else(bad)?
            .to_string(),
        size: v.get("size").and_then(|x| x.as_u64()).ok_or_else(bad)?,
        content_type: v
            .get("content_type")
            .and_then(|x| x.as_str())
            .ok_or_else(bad)?
            .to_string(),
    })
}

async fn op(
    st: &mut ExecState,
    name: &'static str,
    args: serde_json::Value,
) -> Result<serde_json::Value, object_storage::Error> {
    call(st, CapabilityKind::Db, name, args)
        .await
        .map_err(error_from)
}

impl object_storage::Host for ExecState {
    async fn put(
        &mut self,
        owner: Option<String>,
        key: String,
        body: Vec<u8>,
        opts: object_storage::PutOptions,
    ) -> Result<object_storage::ObjectInfo, object_storage::Error> {
        check_str("key", &key)?;
        if body.len() > MAX_BODY {
            return Err(object_storage::Error::TooLarge(body.len() as u64));
        }
        let args = serde_json::json!({
            "owner": owner, "key": key, "body_b64": B64.encode(&body),
            "content_type": opts.content_type, "tags": opts.tags,
        });
        info_from(&op(self, "object_storage.put", args).await?)
    }

    async fn get(
        &mut self,
        owner: Option<String>,
        key: String,
    ) -> Result<(object_storage::ObjectInfo, Vec<u8>), object_storage::Error> {
        check_str("key", &key)?;
        let v = op(
            self,
            "object_storage.get",
            serde_json::json!({"owner": owner, "key": key}),
        )
        .await?;
        let body = B64
            .decode(v.get("body_b64").and_then(|x| x.as_str()).ok_or_else(|| {
                object_storage::Error::Backend("malformed host-result: no body".to_string())
            })?)
            .map_err(|_| {
                object_storage::Error::Backend("malformed host-result: body".to_string())
            })?;
        Ok((info_from(&v)?, body))
    }

    async fn head(
        &mut self,
        owner: Option<String>,
        key: String,
    ) -> Result<object_storage::ObjectInfo, object_storage::Error> {
        check_str("key", &key)?;
        info_from(
            &op(
                self,
                "object_storage.head",
                serde_json::json!({"owner": owner, "key": key}),
            )
            .await?,
        )
    }

    async fn list(
        &mut self,
        owner: Option<String>,
        key_prefix: String,
        cursor: Option<String>,
        limit: u32,
    ) -> Result<object_storage::ListPage, object_storage::Error> {
        check_str("key-prefix", &key_prefix)?;
        let args = serde_json::json!({
            "owner": owner, "key_prefix": key_prefix, "cursor": cursor, "limit": limit,
        });
        let v = op(self, "object_storage.list", args).await?;
        let objects = v
            .get("objects")
            .and_then(|x| x.as_array())
            .ok_or_else(|| {
                object_storage::Error::Backend("malformed host-result: objects".to_string())
            })?
            .iter()
            .map(info_from)
            .collect::<Result<Vec<_>, _>>()?;
        Ok(object_storage::ListPage {
            objects,
            next_cursor: v
                .get("next_cursor")
                .and_then(|x| x.as_str())
                .map(str::to_string),
        })
    }

    async fn delete(
        &mut self,
        owner: Option<String>,
        key: String,
    ) -> Result<(), object_storage::Error> {
        check_str("key", &key)?;
        op(
            self,
            "object_storage.delete",
            serde_json::json!({"owner": owner, "key": key}),
        )
        .await
        .map(|_| ())
    }

    async fn presign_get(
        &mut self,
        owner: Option<String>,
        key: String,
        ttl_seconds: u32,
    ) -> Result<String, object_storage::Error> {
        check_str("key", &key)?;
        let v = op(
            self,
            "object_storage.presign_get",
            serde_json::json!({"owner": owner, "key": key, "ttl_seconds": ttl_seconds}),
        )
        .await?;
        v.get("url")
            .and_then(|x| x.as_str())
            .map(str::to_string)
            .ok_or_else(|| object_storage::Error::Backend("malformed host-result: url".to_string()))
    }

    async fn tag_add(
        &mut self,
        owner: Option<String>,
        key: String,
        consumer: String,
    ) -> Result<(), object_storage::Error> {
        check_str("key", &key)?;
        check_str("consumer", &consumer)?;
        op(
            self,
            "object_storage.tag_add",
            serde_json::json!({"owner": owner, "key": key, "consumer": consumer}),
        )
        .await
        .map(|_| ())
    }

    async fn tag_remove(
        &mut self,
        owner: Option<String>,
        key: String,
        consumer: String,
    ) -> Result<(), object_storage::Error> {
        check_str("key", &key)?;
        check_str("consumer", &consumer)?;
        op(
            self,
            "object_storage.tag_remove",
            serde_json::json!({"owner": owner, "key": key, "consumer": consumer}),
        )
        .await
        .map(|_| ())
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    fn denied(code: &str, msg: &str) -> ExecutorError {
        ExecutorError::HostCallDenied {
            capability: "db",
            op: "object_storage.get",
            code: code.to_string(),
            message: msg.to_string(),
        }
    }

    #[test]
    fn error_codes_map_onto_the_wit_variant() {
        use object_storage::Error as E;
        assert!(matches!(error_from(denied("not_found", "")), E::NotFound));
        assert!(matches!(error_from(denied("invalid", "m")), E::Invalid(_)));
        assert!(matches!(
            error_from(denied("too_large", "42")),
            E::TooLarge(42)
        ));
        assert!(matches!(
            error_from(denied("quota_exceeded", "")),
            E::QuotaExceeded
        ));
        assert!(matches!(
            error_from(denied("rate_limited", "")),
            E::RateLimited
        ));
        assert!(matches!(error_from(denied("backend", "m")), E::Backend(_)));
        assert!(matches!(
            error_from(denied("feature_disabled", "")),
            E::NotImplemented
        ));
        assert!(matches!(
            error_from(denied("not_granted", "")),
            E::NotGranted
        ));
        assert!(matches!(error_from(denied("revoked", "")), E::NotGranted));
    }

    #[test]
    fn info_decoding_is_strict() {
        let ok = serde_json::json!({"key":"a","size":3,"content_type":"image/png"});
        assert_eq!(info_from(&ok).unwrap().size, 3);
        assert!(info_from(&serde_json::json!({"key":"a"})).is_err());
        assert!(check_str("k", &"x".repeat(1025)).is_err());
    }
}
