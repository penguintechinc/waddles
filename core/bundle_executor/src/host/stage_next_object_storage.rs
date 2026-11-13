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

    use crate::host::imports::tests::{one_shot_bridge_capturing, state_with};
    use object_storage::Host as _;
    use penguin_bundle_host::wire::HostResultError;
    use serde_json::json;

    fn info_json() -> serde_json::Value {
        json!({"key": "a/b.png", "size": 5, "content_type": "image/png"})
    }

    /// `put` base64-encodes the body, forwards the guest's words and decodes
    /// the returned object info.
    #[tokio::test]
    async fn put_encodes_the_body_and_decodes_info() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(info_json()));
        let mut st = state_with(bridge);
        let info = st
            .put(
                None,
                "a/b.png".into(),
                b"hello".to_vec(),
                object_storage::PutOptions {
                    content_type: "image/png".into(),
                    tags: vec!["k:v".into()],
                },
            )
            .await
            .unwrap();
        assert_eq!((info.key.as_str(), info.size), ("a/b.png", 5));
        assert_eq!(info.content_type, "image/png");
        let sent = args.await.unwrap();
        assert_eq!(sent["body_b64"], B64.encode(b"hello"));
        assert_eq!(sent["key"], "a/b.png");
        assert_eq!(sent["tags"], json!(["k:v"]));
        assert_eq!(sent["owner"], serde_json::Value::Null);
    }

    /// Oversized keys and bodies are refused locally, before any host call.
    #[tokio::test]
    async fn put_refuses_oversized_key_and_body_without_a_host_call() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(info_json()));
        let mut st = state_with(bridge);
        let opts = || object_storage::PutOptions {
            content_type: "x/y".into(),
            tags: vec![],
        };
        let r = st.put(None, "k".repeat(MAX_STR + 1), vec![], opts()).await;
        assert!(matches!(r, Err(object_storage::Error::Invalid(_))));
        let r = st
            .put(None, "k".into(), vec![0u8; MAX_BODY + 1], opts())
            .await;
        assert!(matches!(
            r,
            Err(object_storage::Error::TooLarge(n)) if n == (MAX_BODY as u64) + 1
        ));
        // Neither refusal reached the stage: no host-call args ever arrive.
        let seen = tokio::time::timeout(std::time::Duration::from_millis(300), args).await;
        assert!(
            seen.is_err(),
            "a locally refused call must not cross the bridge"
        );
    }

    /// `get` decodes info + body and rejects a missing or non-base64 body.
    #[tokio::test]
    async fn get_decodes_body_and_rejects_malformed_results() {
        let mut ok = info_json();
        ok["body_b64"] = json!(B64.encode(b"hello"));
        let (bridge, _a) = one_shot_bridge_capturing(Ok(ok));
        let (info, body) = state_with(bridge)
            .get(None, "a/b.png".into())
            .await
            .unwrap();
        assert_eq!(info.size, 5);
        assert_eq!(body, b"hello");

        let (bridge, _a) = one_shot_bridge_capturing(Ok(info_json()));
        assert!(matches!(
            state_with(bridge).get(None, "k".into()).await,
            Err(object_storage::Error::Backend(m)) if m.contains("no body")
        ));

        let mut bad = info_json();
        bad["body_b64"] = json!("!!!not base64!!!");
        let (bridge, _a) = one_shot_bridge_capturing(Ok(bad));
        assert!(matches!(
            state_with(bridge).get(None, "k".into()).await,
            Err(object_storage::Error::Backend(m)) if m.contains("body")
        ));

        let (bridge, _a) = one_shot_bridge_capturing(Err(HostResultError {
            code: "not_found".into(),
            message: String::new(),
        }));
        assert!(matches!(
            state_with(bridge).get(None, "k".into()).await,
            Err(object_storage::Error::NotFound)
        ));
    }

    /// `head`, `delete`, `tag_add`, `tag_remove` and `presign_get` send their args.
    #[tokio::test]
    async fn head_delete_tags_and_presign_cross_the_bridge() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(info_json()));
        let info = state_with(bridge)
            .head(Some("other.app".into()), "a/b.png".into())
            .await
            .unwrap();
        assert_eq!(info.content_type, "image/png");
        assert_eq!(
            args.await.unwrap(),
            json!({"owner": "other.app", "key": "a/b.png"})
        );

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
        state_with(bridge).delete(None, "k".into()).await.unwrap();
        assert_eq!(args.await.unwrap(), json!({"owner": null, "key": "k"}));

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
        state_with(bridge)
            .tag_add(None, "k".into(), "app-bundle:x".into())
            .await
            .unwrap();
        assert_eq!(
            args.await.unwrap(),
            json!({"owner": null, "key": "k", "consumer": "app-bundle:x"})
        );

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
        state_with(bridge)
            .tag_remove(None, "k".into(), "app-bundle:x".into())
            .await
            .unwrap();
        assert_eq!(
            args.await.unwrap(),
            json!({"owner": null, "key": "k", "consumer": "app-bundle:x"})
        );

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"url": "https://s/x?sig=1"})));
        let url = state_with(bridge)
            .presign_get(None, "k".into(), 60)
            .await
            .unwrap();
        assert_eq!(url, "https://s/x?sig=1");
        assert_eq!(
            args.await.unwrap(),
            json!({"owner": null, "key": "k", "ttl_seconds": 60})
        );

        let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"nourl": 1})));
        assert!(matches!(
            state_with(bridge).presign_get(None, "k".into(), 60).await,
            Err(object_storage::Error::Backend(m)) if m.contains("url")
        ));
    }

    /// Oversized key / consumer strings are refused for every op, locally.
    #[tokio::test]
    async fn oversized_strings_are_refused_for_every_op() {
        let long = "x".repeat(MAX_STR + 1);
        let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({})));
        let mut st = state_with(bridge);
        assert!(matches!(
            st.get(None, long.clone()).await,
            Err(object_storage::Error::Invalid(_))
        ));
        assert!(matches!(
            st.head(None, long.clone()).await,
            Err(object_storage::Error::Invalid(_))
        ));
        assert!(matches!(
            st.list(None, long.clone(), None, 10).await,
            Err(object_storage::Error::Invalid(_))
        ));
        assert!(matches!(
            st.delete(None, long.clone()).await,
            Err(object_storage::Error::Invalid(_))
        ));
        assert!(matches!(
            st.presign_get(None, long.clone(), 1).await,
            Err(object_storage::Error::Invalid(_))
        ));
        assert!(matches!(
            st.tag_add(None, "k".into(), long.clone()).await,
            Err(object_storage::Error::Invalid(_))
        ));
        assert!(matches!(
            st.tag_remove(None, long.clone(), "c".into()).await,
            Err(object_storage::Error::Invalid(_))
        ));
        assert!(matches!(
            st.tag_remove(None, "k".into(), long).await,
            Err(object_storage::Error::Invalid(_))
        ));
    }

    /// `list` decodes the page (with and without a cursor) and rejects bad shapes.
    #[tokio::test]
    async fn list_decodes_pages_and_rejects_malformed_results() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({
            "objects": [info_json(), info_json()], "next_cursor": "c2"
        })));
        let page = state_with(bridge)
            .list(None, "a/".into(), Some("c1".into()), 50)
            .await
            .unwrap();
        assert_eq!(page.objects.len(), 2);
        assert_eq!(page.next_cursor.as_deref(), Some("c2"));
        assert_eq!(
            args.await.unwrap(),
            json!({"owner": null, "key_prefix": "a/", "cursor": "c1", "limit": 50})
        );

        let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"objects": []})));
        let page = state_with(bridge)
            .list(None, "".into(), None, 1)
            .await
            .unwrap();
        assert!(page.objects.is_empty());
        assert_eq!(page.next_cursor, None);

        let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({})));
        assert!(matches!(
            state_with(bridge).list(None, "".into(), None, 1).await,
            Err(object_storage::Error::Backend(m)) if m.contains("objects")
        ));

        let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"objects": [{"key": "only"}]})));
        assert!(matches!(
            state_with(bridge).list(None, "".into(), None, 1).await,
            Err(object_storage::Error::Backend(m)) if m.contains("object info")
        ));
    }

    /// A non-host-call failure and an unparsable `too_large` bound stay loud.
    #[test]
    fn transport_failures_and_bad_bounds_are_backend_or_max() {
        assert!(matches!(
            error_from(ExecutorError::Config("x".into())),
            object_storage::Error::Backend(_)
        ));
        assert!(matches!(
            error_from(denied("too_large", "not-a-number")),
            object_storage::Error::TooLarge(u64::MAX)
        ));
        assert!(matches!(
            error_from(denied("invalid_args", "m")),
            object_storage::Error::Invalid(m) if m == "m"
        ));
        assert!(matches!(
            error_from(denied("not_implemented", "")),
            object_storage::Error::NotImplemented
        ));
    }
}
