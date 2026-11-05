//! Real-path proof of P6a T6d: svc_ingest's production `WireTemplateBuilder`
//! reaches the REAL bundle executor over the host-API wire, which runs
//! `sender.build-request` on the REAL compiled `connector-tools` fixture.
//! Nothing between the builder and wasmtime is mocked (codec, `ExecutorLink`,
//! `run_connection`, `ConnectorRouter`, `ConnectorHost`, guest).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bundle_executor::manifest::WitWorld;
use connector_e2e::{digest_of, RigConnector, WireRig};
use svc_ingest::connector::sender_exec::{
    BuildError, SenderAction, TemplateBuilder, WireTemplateBuilder,
};

const TOOLS_WASM: &[u8] =
    include_bytes!("../../bundle_executor/tests/fixtures/connector_tools_fixture.wasm");
const PLATFORM: &str = "twitch";

async fn rig() -> (WireRig, WireTemplateBuilder) {
    let rig = WireRig::start(
        vec![RigConnector {
            platform: PLATFORM.to_string(),
            app_id: "waddles.core.test.connector-tools".to_string(),
            wasm: TOOLS_WASM,
            world: WitWorld::ConnectorTools,
        }],
        4096,
        |_| {},
    )
    .await;
    let builder = WireTemplateBuilder::new(Arc::clone(&rig.link), Arc::clone(&rig.registry));
    (rig, builder)
}

fn action(kind: &str) -> SenderAction {
    SenderAction {
        kind: kind.to_string(),
        target_source_id: "src-5".to_string(),
        payload_json: r#"{"text":"hello"}"#.to_string(),
    }
}

#[tokio::test]
async fn the_wire_builder_gets_the_real_guest_template_stamped_with_the_pinned_digest() {
    let (_rig, builder) = rig().await;
    let built = builder
        .build_request(PLATFORM, &action("chat.send"))
        .await
        .unwrap();
    assert_eq!(built.component_digest, digest_of(TOOLS_WASM));
    assert_eq!(built.tpl.method, "POST");
    assert_eq!(built.tpl.url, "https://api.example.test/v1/src-5/messages");
    assert_eq!(
        built.tpl.headers,
        vec![("Content-Type".to_string(), "application/json".to_string())]
    );
    assert_eq!(
        built.tpl.body.as_deref(),
        Some(br#"{"text":"hello"}"#.as_slice())
    );
    assert_eq!(
        built.tpl.secret_refs,
        vec![("token".to_string(), "handle-1".to_string())]
    );
}

#[tokio::test]
async fn the_wire_builder_maps_guest_and_sandbox_outcomes_to_typed_errors() {
    let (_rig, builder) = rig().await;
    for (kind, want) in [
        ("chat.bad", BuildError::Guest("malformed")),
        ("chat.backend", BuildError::Guest("backend")),
        ("dm.send", BuildError::Guest("unsupported")),
        ("chat.spin", BuildError::Sandbox("fuel_exhausted")),
        // An oversized template is refused by the executor (typed), never forwarded.
        ("chat.huge", BuildError::Unavailable),
    ] {
        let err = builder
            .build_request(PLATFORM, &action(kind))
            .await
            .unwrap_err();
        assert_eq!(err, want, "{kind}");
    }
    // The connector keeps serving after every refusal.
    builder
        .build_request(PLATFORM, &action("chat.send"))
        .await
        .unwrap();
}

#[tokio::test]
async fn a_platform_that_was_never_pinned_is_unavailable() {
    let (_rig, builder) = rig().await;
    assert_eq!(
        builder
            .build_request("discord", &action("chat.send"))
            .await
            .unwrap_err(),
        BuildError::Unavailable
    );
}
