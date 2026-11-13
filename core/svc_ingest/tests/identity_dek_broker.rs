//! Real-path tests for ingest's identity-field encryption against the hub-api key broker:
//! a real tonic `KeyService` fake implementing the sealed-DEK contract, a real machine-JWT
//! bootstrap (`service_auth::MachineJwtClient` against an in-process `ServiceAuthService`),
//! the real `HubDekBroker` + cache, and the real `publish_event_scoped` construction site.
//! Only Valkey (the appender) is a recording fake.
//!
//! The ciphertext is decrypted with the fake hub's independently derived per-(tenant,
//! version) key and the AAD svc-process rebuilds from the delivered envelope, so these tests
//! pin exactly what the process side relies on.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::{Arc, Mutex};

use hub_client::dek::testing::{test_key, FakeKeyService, FAKE_TOKEN, REQUIRED_SCOPE};
use hub_client::dek::STREAM_DEK_SCOPE;
use penguin_spine::{KeyRing, NoopMetrics, PlatformEvent, SpineError, StageEnvelope};
use service_auth::test_support::{MintReply, MockHub};
use service_auth::MachineJwtClient;
use svc_ingest::identity_crypto::{
    decrypt_identity_value, ConfiguredDekProvider, IdentityCryptoError, JsonEnvelope,
};
use svc_ingest::publish::{publish_event, publish_event_scoped, EventAppender, PublishError};
use svc_ingest::scope_resolver::ResolvedScope;
use zeroize::Zeroizing;

const TENANT_ID: i64 = 7;

struct RecordingAppender {
    calls: Mutex<Vec<(String, StageEnvelope)>>,
}

impl EventAppender for RecordingAppender {
    async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
        self.calls
            .lock()
            .unwrap()
            .push((stream.to_string(), env.clone()));
        Ok("1-0".to_string())
    }
}

fn appender() -> RecordingAppender {
    RecordingAppender {
        calls: Mutex::new(Vec::new()),
    }
}

fn identity_metrics() -> prometheus::IntCounterVec {
    prometheus::IntCounterVec::new(
        prometheus::Opts::new("test_identity_encryption_total", "test-only"),
        &["tenant", "status"],
    )
    .unwrap()
}

fn keyring() -> KeyRing {
    KeyRing::new(vec![("kid-1".to_string(), vec![9u8; 32])])
}

fn scope(tenant_id: i64) -> ResolvedScope {
    ResolvedScope {
        tenant_id,
        community_id: 3,
        tenant_slug: "acme".to_string(),
        community_name: Some("main".to_string()),
    }
}

fn ping() -> PlatformEvent {
    PlatformEvent {
        platform: "discord".to_string(),
        event_type: "message".to_string(),
        actor: Some("alice".to_string()),
        payload: serde_json::from_value(serde_json::json!({
            "text": "!ping",
            "channel_id": "chan-1"
        }))
        .unwrap(),
        occurred_at: "2026-11-11T00:00:00.000Z".to_string(),
        source: None,
    }
}

/// A broker-backed provider whose JWT really comes from the mint hub, over a real channel.
async fn provider(fake: &FakeKeyService, tag: &str) -> (ConfiguredDekProvider, MockHub) {
    let mint = MockHub::builder()
        .mint(vec![MintReply::when(
            |c| c.scope == REQUIRED_SCOPE,
            FAKE_TOKEN,
            3600,
        )])
        .start()
        .await;
    let sa = std::env::temp_dir().join(format!("svc-ingest-dek-sa-{tag}-{}", std::process::id()));
    std::fs::write(&sa, "projected-sa-token").unwrap();
    let tokens = Arc::new(MachineJwtClient::new(
        mint.endpoint.clone(),
        sa.to_string_lossy().to_string(),
        STREAM_DEK_SCOPE,
    ));
    let channel = hub_client::lazy_channel(&fake.serve().await, None).unwrap();
    let client = hub_client::HubClient::with_channel(channel, tokens);
    (
        ConfiguredDekProvider::hub(Arc::new(client), "svc-ingest"),
        mint,
    )
}

async fn publish(
    provider: &ConfiguredDekProvider,
    appender: &RecordingAppender,
    tenant_id: i64,
) -> Result<String, PublishError> {
    publish_event_scoped(
        &scope(tenant_id),
        appender,
        &NoopMetrics,
        &keyring(),
        "kid-1",
        "guild-A",
        "00000000-0000-0000-0000-000000000001",
        None,
        ping(),
        provider,
        &identity_metrics(),
    )
    .await
}

fn field(env: &StageEnvelope, name: &str) -> JsonEnvelope {
    let raw = if name == "actor" {
        env.event.actor.clone().unwrap()
    } else {
        env.event.payload[name].as_str().unwrap().to_string()
    };
    serde_json::from_str(&raw).expect("an identity field is a JSON envelope on the stream")
}

/// The AAD tenant is the numeric id (immutable), never the slug; the DEK is the brokered
/// per-(numeric tenant, version) key; and the process side, rebuilding the AAD from the
/// delivered entry, decrypts it.
#[tokio::test]
async fn ingest_encrypts_under_the_brokered_numeric_tenant_dek() {
    let fake = FakeKeyService::new();
    let (provider, _mint) = provider(&fake, "numeric").await;
    let appender = appender();
    publish(&provider, &appender, TENANT_ID).await.unwrap();

    let calls = appender.calls.lock().unwrap();
    let (stream, env) = &calls[0];
    let dek = Zeroizing::new(test_key(TENANT_ID, 1));
    for (name, expected) in [("actor", "alice"), ("text", "!ping")] {
        let envelope = field(env, name);
        assert_eq!(envelope.dek_version, 1);
        let plain = decrypt_identity_value(
            &envelope,
            &dek,
            &TENANT_ID.to_string(),
            stream,
            name,
            &env.event_id,
        )
        .unwrap();
        assert_eq!(plain, expected);
        // The slug is not what the AAD binds.
        assert!(matches!(
            decrypt_identity_value(&envelope, &dek, "acme", stream, name, &env.event_id),
            Err(IdentityCryptoError::Aead(_))
        ));
    }
    // Opaque platform ids stay plaintext; the rest is ciphertext.
    assert_eq!(env.event.payload["channel_id"], "chan-1");
    assert_eq!(env.tenant_id, Some(TENANT_ID));

    let seen = fake.state.lock().unwrap().calls.clone();
    assert_eq!(
        seen.len(),
        1,
        "one broker call per tenant: actor + text share a DEK"
    );
    assert_eq!(
        (seen[0].0, seen[0].1),
        (TENANT_ID, 0),
        "numeric tenant, current version"
    );
}

/// Tenant isolation: tenant A's DEK can never open tenant B's ciphertext.
#[tokio::test]
async fn one_tenants_dek_cannot_decrypt_another_tenants_events() {
    let fake = FakeKeyService::new();
    let (provider, _mint) = provider(&fake, "isolation").await;
    let appender = appender();
    publish(&provider, &appender, 7).await.unwrap();
    publish(&provider, &appender, 8).await.unwrap();

    let calls = appender.calls.lock().unwrap();
    let (stream_a, env_a) = &calls[0];
    let (_, env_b) = &calls[1];
    let key_a = Zeroizing::new(test_key(7, 1));
    let key_b = Zeroizing::new(test_key(8, 1));
    let b_text = field(env_b, "text");
    assert!(
        decrypt_identity_value(&b_text, &key_a, "8", stream_a, "text", &env_b.event_id).is_err(),
        "tenant 7's key must not open tenant 8's event"
    );
    assert_eq!(
        decrypt_identity_value(&b_text, &key_b, "8", &calls[1].0, "text", &env_b.event_id).unwrap(),
        "!ping"
    );
    assert_eq!(
        decrypt_identity_value(
            &field(env_a, "text"),
            &key_a,
            "7",
            stream_a,
            "text",
            &env_a.event_id
        )
        .unwrap(),
        "!ping"
    );
}

/// Rotation: once the broker's current version is 2, new events carry version 2 in their
/// header and decrypt under the version-2 key; version-1 events written earlier keep
/// decrypting under the version-1 key the header names.
#[tokio::test]
async fn a_rotation_moves_new_events_to_the_new_version_and_old_ones_stay_readable() {
    let fake = FakeKeyService::new();
    let (provider, _mint) = provider(&fake, "rotation").await;
    let appender = appender();
    publish(&provider, &appender, TENANT_ID).await.unwrap();

    fake.state.lock().unwrap().current.insert(TENANT_ID, 2);
    use hub_client::dek::DekInvalidate;
    provider.invalidate(TENANT_ID); // the Valkey invalidation signal
    publish(&provider, &appender, TENANT_ID).await.unwrap();

    let calls = appender.calls.lock().unwrap();
    for (i, version) in [(0usize, 1u32), (1, 2)] {
        let (stream, env) = &calls[i];
        let envelope = field(env, "text");
        assert_eq!(envelope.dek_version, version);
        let key = Zeroizing::new(test_key(TENANT_ID, version));
        assert_eq!(
            decrypt_identity_value(&envelope, &key, "7", stream, "text", &env.event_id).unwrap(),
            "!ping"
        );
    }
}

/// A key-broker outage holds the event (fail closed): nothing is appended, nothing is written
/// plaintext, and the failure is counted.
#[tokio::test]
async fn a_broker_outage_holds_the_event_and_never_writes_plaintext() {
    let fake = FakeKeyService::new();
    fake.state.lock().unwrap().down = true;
    let (provider, _mint) = provider(&fake, "outage").await;
    let appender = appender();
    let err = publish(&provider, &appender, TENANT_ID).await.unwrap_err();
    assert!(matches!(err, PublishError::DekUnavailable(_)), "{err:?}");
    assert!(appender.calls.lock().unwrap().is_empty());
}

/// The legacy string-scope path has no numeric tenant id; the broker is never asked for a
/// slug-keyed DEK, the event is held.
#[tokio::test]
async fn the_legacy_string_scope_path_cannot_obtain_a_broker_dek() {
    let fake = FakeKeyService::new();
    let (provider, _mint) = provider(&fake, "legacy").await;
    let appender = appender();
    let err = publish_event(
        &appender,
        &NoopMetrics,
        &keyring(),
        "kid-1",
        &penguin_spine::Scope::new("acme", Some("main".to_string())),
        "guild-A",
        "00000000-0000-0000-0000-000000000001",
        None,
        ping(),
        &provider,
        &identity_metrics(),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, PublishError::DekUnavailable(_)), "{err:?}");
    assert!(appender.calls.lock().unwrap().is_empty());
    assert_eq!(fake.call_count(), 0, "no request for a slug-keyed DEK");
}
