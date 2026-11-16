//! Real host-call round trips for every `object-storage` import: the call crosses the actual
//! frame codec to a fake stage that answers once, so the encoded args (the executor half of the
//! wire contract) and the decoded result are both pinned -- never a fabricated return value.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use super::*;
use crate::host::imports::tests::{one_shot_bridge_capturing, state_with};
use penguin_bundle_host::wire::HostResultError;
use serde_json::json;

fn info(key: &str, size: u64) -> serde_json::Value {
    json!({"key": key, "size": size, "content_type": "image/png"})
}

#[tokio::test]
async fn put_encodes_the_body_and_decodes_the_object_info() {
    let (bridge, args) = one_shot_bridge_capturing(Ok(info("a/b.png", 3)));
    let mut st = state_with(bridge);
    let out = object_storage::Host::put(
        &mut st,
        None,
        "a/b.png".into(),
        vec![1, 2, 3],
        object_storage::PutOptions {
            content_type: "image/png".into(),
            tags: vec!["k:v".into()],
        },
    )
    .await
    .unwrap();
    assert_eq!((out.key.as_str(), out.size), ("a/b.png", 3));
    assert_eq!(
        args.await.unwrap(),
        json!({"owner": null, "key": "a/b.png", "body_b64": "AQID",
               "content_type": "image/png", "tags": ["k:v"]})
    );
}

#[tokio::test]
async fn put_refuses_an_oversized_key_or_body_before_the_wire() {
    let mut st = ExecState::new(None, "waddles.test.app".to_string(), 1);
    let opts = || object_storage::PutOptions {
        content_type: "x/y".into(),
        tags: vec![],
    };
    let e = object_storage::Host::put(&mut st, None, "k".repeat(1025), vec![], opts())
        .await
        .unwrap_err();
    assert!(matches!(e, object_storage::Error::Invalid(_)));
    let e = object_storage::Host::put(&mut st, None, "k".into(), vec![0; MAX_BODY + 1], opts())
        .await
        .unwrap_err();
    assert!(matches!(e, object_storage::Error::TooLarge(n) if n == (MAX_BODY as u64 + 1)));
}

#[tokio::test]
async fn get_decodes_info_and_body_and_rejects_malformed_results() {
    let mut ok = info("k", 3);
    ok["body_b64"] = json!("AQID");
    let (bridge, args) = one_shot_bridge_capturing(Ok(ok));
    let mut st = state_with(bridge);
    let (meta, body) = object_storage::Host::get(&mut st, Some("owner".into()), "k".into())
        .await
        .unwrap();
    assert_eq!((meta.size, body), (3, vec![1, 2, 3]));
    assert_eq!(args.await.unwrap(), json!({"owner": "owner", "key": "k"}));

    let (bridge, _a) = one_shot_bridge_capturing(Ok(info("k", 3)));
    let mut st = state_with(bridge);
    let e = object_storage::Host::get(&mut st, None, "k".into())
        .await
        .unwrap_err();
    assert!(matches!(e, object_storage::Error::Backend(m) if m.contains("no body")));

    let mut bad = info("k", 3);
    bad["body_b64"] = json!("!!!not-base64!!!");
    let (bridge, _a) = one_shot_bridge_capturing(Ok(bad));
    let mut st = state_with(bridge);
    let e = object_storage::Host::get(&mut st, None, "k".into())
        .await
        .unwrap_err();
    assert!(matches!(e, object_storage::Error::Backend(m) if m.contains("body")));
}

#[tokio::test]
async fn head_and_presign_get_round_trip() {
    let (bridge, args) = one_shot_bridge_capturing(Ok(info("k", 9)));
    let mut st = state_with(bridge);
    let out = object_storage::Host::head(&mut st, None, "k".into())
        .await
        .unwrap();
    assert_eq!(out.size, 9);
    assert_eq!(args.await.unwrap(), json!({"owner": null, "key": "k"}));

    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"url": "https://s3.example/k?sig"})));
    let mut st = state_with(bridge);
    let url = object_storage::Host::presign_get(&mut st, None, "k".into(), 60)
        .await
        .unwrap();
    assert_eq!(url, "https://s3.example/k?sig");
    assert_eq!(
        args.await.unwrap(),
        json!({"owner": null, "key": "k", "ttl_seconds": 60})
    );

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({})));
    let mut st = state_with(bridge);
    let e = object_storage::Host::presign_get(&mut st, None, "k".into(), 60)
        .await
        .unwrap_err();
    assert!(matches!(e, object_storage::Error::Backend(_)));
}

#[tokio::test]
async fn list_decodes_a_page_with_and_without_a_cursor() {
    let page = json!({"objects": [info("p/a", 1), info("p/b", 2)], "next_cursor": "c2"});
    let (bridge, args) = one_shot_bridge_capturing(Ok(page));
    let mut st = state_with(bridge);
    let out = object_storage::Host::list(&mut st, None, "p/".into(), Some("c1".into()), 50)
        .await
        .unwrap();
    assert_eq!(out.objects.len(), 2);
    assert_eq!(out.next_cursor.as_deref(), Some("c2"));
    assert_eq!(
        args.await.unwrap(),
        json!({"owner": null, "key_prefix": "p/", "cursor": "c1", "limit": 50})
    );

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"objects": []})));
    let mut st = state_with(bridge);
    let out = object_storage::Host::list(&mut st, None, "p/".into(), None, 50)
        .await
        .unwrap();
    assert!(out.objects.is_empty() && out.next_cursor.is_none());

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"nope": 1})));
    let mut st = state_with(bridge);
    let e = object_storage::Host::list(&mut st, None, "p/".into(), None, 50)
        .await
        .unwrap_err();
    assert!(matches!(e, object_storage::Error::Backend(_)));
}

#[tokio::test]
async fn delete_and_tag_ops_send_their_args_and_surface_refusals() {
    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
    let mut st = state_with(bridge);
    object_storage::Host::delete(&mut st, None, "k".into())
        .await
        .unwrap();
    assert_eq!(args.await.unwrap(), json!({"owner": null, "key": "k"}));

    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
    let mut st = state_with(bridge);
    object_storage::Host::tag_add(&mut st, None, "k".into(), "app.consumer".into())
        .await
        .unwrap();
    assert_eq!(
        args.await.unwrap(),
        json!({"owner": null, "key": "k", "consumer": "app.consumer"})
    );

    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
    let mut st = state_with(bridge);
    object_storage::Host::tag_remove(&mut st, None, "k".into(), "app.consumer".into())
        .await
        .unwrap();
    assert_eq!(
        args.await.unwrap(),
        json!({"owner": null, "key": "k", "consumer": "app.consumer"})
    );

    let refusal = HostResultError {
        code: "not_granted".to_string(),
        message: "no".to_string(),
    };
    let (bridge, _a) = one_shot_bridge_capturing(Err(refusal));
    let mut st = state_with(bridge);
    let e = object_storage::Host::delete(&mut st, None, "k".into())
        .await
        .unwrap_err();
    assert!(matches!(e, object_storage::Error::NotGranted));

    let mut st = ExecState::new(None, "waddles.test.app".to_string(), 1);
    let e = object_storage::Host::tag_add(&mut st, None, "k".into(), "c".repeat(1025))
        .await
        .unwrap_err();
    assert!(matches!(e, object_storage::Error::Invalid(_)));
}
