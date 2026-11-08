//! `HubApiDekProvider` against a fake broker that plays hub-api: seals a fixed
//! DEK to the caller's ephemeral key (HPKE-lite, bound to
//! `service|tenant|purpose|version`). Proves the unseal path, the context
//! binding, and fail-closed behaviour on outage.

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use bundle_host_crypto::provider::BoxFuture;
use bundle_host_crypto::{
    CryptoError, DekBroker, DekProvider, HubApiDekProvider, SealedDek, TenantCrypto,
};
use hkdf::Hkdf;
use sha2::Sha256;

const AAD: [&str; 5] = ["1", "2", "app.a", "kv", "k1"];

struct FakeBroker {
    service_id: &'static str,
    fail: bool,
}

impl DekBroker for FakeBroker {
    fn fetch_sealed<'a>(
        &'a self,
        tenant_id: &'a str,
        purpose: &'a str,
        version: u32,
        client_public_key: [u8; 32],
    ) -> BoxFuture<'a, Result<SealedDek, String>> {
        Box::pin(async move {
            if self.fail {
                return Err("broker down".to_string());
            }
            let served = if version == 0 { 4 } else { version };
            let server_secret = x25519_dalek::EphemeralSecret::random();
            let enc = *x25519_dalek::PublicKey::from(&server_secret).as_bytes();
            let shared =
                server_secret.diffie_hellman(&x25519_dalek::PublicKey::from(client_public_key));
            let ctx = format!("{}|{tenant_id}|{purpose}|{served}", self.service_id);
            let mut kn = [0u8; 44];
            Hkdf::<Sha256>::new(None, shared.as_bytes())
                .expand(ctx.as_bytes(), &mut kn)
                .map_err(|e| e.to_string())?;
            let cipher = Aes256Gcm::new_from_slice(&kn[..32]).map_err(|e| e.to_string())?;
            let ciphertext = cipher
                .encrypt(
                    Nonce::from_slice(&kn[32..]),
                    Payload {
                        msg: &[0x5Au8; 32],
                        aad: ctx.as_bytes(),
                    },
                )
                .map_err(|e| e.to_string())?;
            Ok(SealedDek {
                enc,
                ciphertext,
                dek_version: served,
                ttl_seconds: 60,
            })
        })
    }
}

fn provider(service_id: &'static str, fail: bool) -> HubApiDekProvider<FakeBroker> {
    HubApiDekProvider::new(
        FakeBroker { service_id, fail },
        "svc-process",
        "bundle-data",
    )
}

#[tokio::test]
async fn broker_provider_unseals_and_feeds_tenant_crypto() {
    let p = provider("svc-process", false);
    let (dek, v) = p.get_dek("1").await.unwrap_or_else(|e| panic!("{e}"));
    assert_eq!((&dek[..], v), (&[0x5Au8; 32][..], 4));
    let c = TenantCrypto::new(p);
    let sealed = c
        .seal("kv", "1", &AAD, b"v")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        c.open("kv", "1", &AAD, &sealed).await.unwrap_or_default(),
        b"v"
    );
}

#[tokio::test]
async fn broker_context_mismatch_fails_closed() {
    assert!(provider("svc-other", false).get_dek("1").await.is_err());
}

#[tokio::test]
async fn broker_outage_refuses_seal() {
    let c = TenantCrypto::new(provider("svc-process", true));
    assert!(matches!(
        c.seal("kv", "1", &AAD, b"v").await,
        Err(CryptoError::KeyUnavailable(_))
    ));
}
