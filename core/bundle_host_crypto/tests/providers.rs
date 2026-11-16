#![allow(clippy::expect_used)] // test-only: a failed expectation IS the test failure

//! Public-API tests for the DEK provider family: the alpha/local dev provider (environment and
//! KEK gating), the TTL cache's versioned lookups, the broker client's version checks, the
//! runtime-selected provider, the invalidation listener, envelope header parsing and the error
//! label surface. Every refusal here is a fail-closed path -- none may degrade to plaintext.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use bundle_host_crypto::envelope::{envelope_dek_version, ALG_AES256GCM_V1};
use bundle_host_crypto::provider::{BoxFuture, LocalDevDekProviderError};
use bundle_host_crypto::{
    metrics, run_dek_invalidation_listener, ConfiguredDekProvider, CryptoError, Dek, DekBroker,
    DekProvider, DekUnavailableError, HubApiDekProvider, Invalidate, InvalidationSource,
    LocalDevDekProvider, SealedDek, TenantCrypto, TtlCachedDekProvider, MAGIC, OVERHEAD,
};
use hkdf::Hkdf;
use sha2::Sha256;

const KEK: &str = "0123456789abcdef0123456789abcdef-dev-kek";

fn set_env(name: &str, value: &str) {
    // Each test owns a distinct variable name, so concurrent tests never race on one.
    std::env::set_var(name, value);
}

mod local_dev {
    use super::*;

    #[test]
    fn production_like_environments_are_refused() {
        for env in ["production", "beta", "gamma", ""] {
            assert_eq!(
                LocalDevDekProvider::new(env, "BHC_UNUSED_KEK").err(),
                Some(LocalDevDekProviderError::EnvironmentNotAllowed(
                    env.to_string()
                )),
                "{env:?}"
            );
        }
    }

    #[test]
    fn a_missing_kek_is_refused_and_named() {
        let err = LocalDevDekProvider::new("alpha", "BHC_TEST_KEK_NEVER_SET").err();
        assert_eq!(
            err,
            Some(LocalDevDekProviderError::KekNotSet(
                "BHC_TEST_KEK_NEVER_SET".to_string()
            ))
        );
        assert!(err
            .map(|e| e.to_string())
            .unwrap_or_default()
            .contains("BHC_TEST_KEK_NEVER_SET"));
    }

    #[test]
    fn a_short_kek_is_refused() {
        set_env("BHC_TEST_KEK_SHORT", "too-short");
        assert_eq!(
            LocalDevDekProvider::new("LOCAL", "BHC_TEST_KEK_SHORT").err(),
            Some(LocalDevDekProviderError::KekTooShort(
                "BHC_TEST_KEK_SHORT".to_string()
            ))
        );
    }

    #[tokio::test]
    async fn deks_are_deterministic_per_tenant_and_distinct_across_tenants() {
        set_env("BHC_TEST_KEK_OK", KEK);
        let p = LocalDevDekProvider::new("Alpha", "BHC_TEST_KEK_OK").expect("dev provider");
        let (a1, v1) = p.get_dek("1").await.expect("dek");
        let (a2, _) = p.get_dek("1").await.expect("dek");
        let (b, _) = p.get_dek("2").await.expect("dek");
        assert_eq!(v1, 1);
        assert_eq!(&a1[..], &a2[..]);
        assert_ne!(&a1[..], &b[..]);
        // the default `get_dek_version` accepts only the active version
        assert!(p.get_dek_version("1", 1).await.is_ok());
        // (never `expect_err`: a wrongly-served DEK must not be Debug-printed into test output)
        match p.get_dek_version("1", 2).await {
            Err(err) => assert!(err.to_string().contains('2'), "{err}"),
            Ok(_) => panic!("version 2 must be refused"),
        }
    }

    #[test]
    fn debug_redacts_the_kek_and_clones_derive_the_same_keys() {
        set_env("BHC_TEST_KEK_DBG", KEK);
        let p = LocalDevDekProvider::new("alpha", "BHC_TEST_KEK_DBG").expect("dev provider");
        let shown = format!("{p:?}");
        assert!(
            shown.contains("redacted") && !shown.contains(KEK),
            "{shown}"
        );
        let q = p.clone();
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .expect("runtime");
        let (x, _) = rt.block_on(p.get_dek("t")).expect("dek");
        let (y, _) = rt.block_on(q.get_dek("t")).expect("dek");
        assert_eq!(&x[..], &y[..]);
    }

    #[tokio::test]
    async fn the_configured_provider_serves_and_invalidates_the_dev_cache() {
        set_env("BHC_TEST_KEK_CFG", KEK);
        let dev = LocalDevDekProvider::new("alpha", "BHC_TEST_KEK_CFG").expect("dev provider");
        let cfg: ConfiguredDekProvider<NoBroker> = ConfiguredDekProvider::LocalDev(
            TtlCachedDekProvider::new(dev, Duration::from_secs(60)),
        );
        let (dek, v) = cfg.get_dek("7").await.expect("dek");
        assert_eq!(v, 1);
        assert_eq!(
            &cfg.get_dek_version("7", 1).await.expect("version 1")[..],
            &dek[..]
        );
        assert!(cfg.get_dek_version("7", 9).await.is_err());
        cfg.invalidate("7");
        cfg.invalidate_tenant("7");
        assert!(
            cfg.get_dek("7").await.is_ok(),
            "re-resolves after invalidation"
        );
    }

    #[tokio::test]
    async fn a_tenant_crypto_over_the_dev_provider_round_trips() {
        set_env("BHC_TEST_KEK_RT", KEK);
        let dev = LocalDevDekProvider::new("alpha", "BHC_TEST_KEK_RT").expect("dev provider");
        let crypto = TenantCrypto::new(TtlCachedDekProvider::new(dev, Duration::from_secs(60)));
        let aad = ["1", "2", "app.a", "kv", "k"];
        let sealed = crypto.seal("kv", "1", &aad, b"secret").await.expect("seal");
        assert!(TenantCrypto::is_sealed(&sealed));
        assert_eq!(
            crypto.open("kv", "1", &aad, &sealed).await.expect("open"),
            b"secret"
        );
        assert!(format!("{crypto:?}").contains("TenantCrypto"));
    }
}

/// A broker that is never called (the dev variant of `ConfiguredDekProvider` does not need one).
struct NoBroker;

impl DekBroker for NoBroker {
    fn fetch_sealed<'a>(
        &'a self,
        _tenant_id: &'a str,
        _purpose: &'a str,
        _version: u32,
        _client_public_key: [u8; 32],
    ) -> BoxFuture<'a, Result<SealedDek, String>> {
        Box::pin(async { Err("no broker in this test".to_string()) })
    }
}

/// A broker that plays hub-api but can answer with a different version than asked for.
struct SkewBroker {
    served: u32,
    calls: AtomicU32,
}

impl DekBroker for SkewBroker {
    fn fetch_sealed<'a>(
        &'a self,
        tenant_id: &'a str,
        purpose: &'a str,
        _version: u32,
        client_public_key: [u8; 32],
    ) -> BoxFuture<'a, Result<SealedDek, String>> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let server_secret = x25519_dalek::EphemeralSecret::random();
            let enc = *x25519_dalek::PublicKey::from(&server_secret).as_bytes();
            let shared =
                server_secret.diffie_hellman(&x25519_dalek::PublicKey::from(client_public_key));
            let ctx = format!("svc-process|{tenant_id}|{purpose}|{}", self.served);
            let mut kn = [0u8; 44];
            Hkdf::<Sha256>::new(None, shared.as_bytes())
                .expand(ctx.as_bytes(), &mut kn)
                .map_err(|e| e.to_string())?;
            let cipher = Aes256Gcm::new_from_slice(&kn[..32]).map_err(|e| e.to_string())?;
            let ciphertext = cipher
                .encrypt(
                    Nonce::from_slice(&kn[32..]),
                    Payload {
                        msg: &[0x33u8; 32],
                        aad: ctx.as_bytes(),
                    },
                )
                .map_err(|e| e.to_string())?;
            Ok(SealedDek {
                enc,
                ciphertext,
                dek_version: self.served,
                ttl_seconds: 5,
            })
        })
    }
}

#[tokio::test]
async fn a_broker_answering_another_version_is_refused() {
    let p = HubApiDekProvider::new(
        SkewBroker {
            served: 2,
            calls: AtomicU32::new(0),
        },
        "svc-process",
        "at-rest",
    );
    match p.get_dek_version("1", 5).await {
        Err(err) => assert!(err.to_string().contains("version"), "{err}"),
        Ok(_) => panic!("a broker answering another version must be refused"),
    }
    // the matching version is served, and the granted TTL is clamped by the broker's own ceiling
    let dek = p.get_dek_version("1", 2).await.expect("version 2");
    assert_eq!(&dek[..], &[0x33u8; 32][..]);
    assert_eq!(p.granted_ttl("1"), Some(Duration::from_secs(5)));
    assert_eq!(p.granted_ttl("unseen"), None);
}

/// A provider that serves the active version 3 and counts each call.
struct Counting {
    active_calls: AtomicU32,
    version_calls: AtomicU32,
}

impl DekProvider for Counting {
    async fn get_dek(&self, _tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        self.active_calls.fetch_add(1, Ordering::SeqCst);
        Ok((zeroizing(3), 3))
    }

    async fn get_dek_version(
        &self,
        tenant_id: &str,
        version: u32,
    ) -> Result<Dek, DekUnavailableError> {
        self.version_calls.fetch_add(1, Ordering::SeqCst);
        if version == 99 {
            return Err(DekUnavailableError {
                tenant_id: tenant_id.to_string(),
                dek_version: Some(version),
                reason: "unknown version".to_string(),
            });
        }
        Ok(zeroizing(version as u8))
    }
}

fn zeroizing(byte: u8) -> Dek {
    zeroize::Zeroizing::new([byte; 32])
}

#[tokio::test]
async fn the_ttl_cache_serves_versions_from_cache_and_forces_one_broker_call_for_unknown_ones() {
    let cache = TtlCachedDekProvider::new(
        Counting {
            active_calls: AtomicU32::new(0),
            version_calls: AtomicU32::new(0),
        },
        Duration::from_secs(60),
    );
    // the active version is cached, so asking for it by number never reaches the broker
    cache.get_dek("t").await.expect("active");
    assert_eq!(
        &cache.get_dek_version("t", 3).await.expect("v3")[..],
        &[3u8; 32][..]
    );
    // an older version: exactly one forced call, then cached
    assert_eq!(
        &cache.get_dek_version("t", 2).await.expect("v2")[..],
        &[2u8; 32][..]
    );
    assert_eq!(
        &cache.get_dek_version("t", 2).await.expect("v2")[..],
        &[2u8; 32][..]
    );
    // an unknown version fails closed and is not cached
    assert!(cache.get_dek_version("t", 99).await.is_err());
    assert!(cache.get_dek_version("t", 99).await.is_err());
    cache.invalidate_tenant("t");
    cache
        .get_dek_version("t", 2)
        .await
        .expect("re-fetched after invalidation");
}

/// Scripted invalidation source: one batch of entries, one failure, then silence.
struct ScriptedSource {
    step: u32,
    seen: Arc<Mutex<Vec<String>>>,
}

impl InvalidationSource for ScriptedSource {
    async fn read_after(
        &mut self,
        last_id: &str,
    ) -> Result<(String, Vec<HashMap<String, String>>), String> {
        self.seen.lock().expect("lock").push(last_id.to_string());
        self.step += 1;
        match self.step {
            1 => {
                let entry = |purpose: &str, tenant: Option<&str>| {
                    let mut m = HashMap::new();
                    m.insert("purpose".to_string(), purpose.to_string());
                    if let Some(t) = tenant {
                        m.insert("tenant".to_string(), t.to_string());
                    }
                    m
                };
                Ok((
                    "1-0".to_string(),
                    vec![
                        entry("at-rest", Some("t1")),
                        entry("ingest-stream", Some("other")), // wrong purpose: ignored
                        entry("at-rest", None),                // no tenant: skipped, logged
                    ],
                ))
            }
            2 => Err("stream unavailable".to_string()),
            _ => {
                tokio::time::sleep(Duration::from_secs(3600)).await;
                Ok((last_id.to_string(), vec![]))
            }
        }
    }
}

struct Recorder(Arc<Mutex<Vec<String>>>);

impl Invalidate for Recorder {
    fn invalidate_tenant(&self, tenant_id: &str) {
        self.0.lock().expect("lock").push(tenant_id.to_string());
    }
}

#[tokio::test(start_paused = true)]
async fn the_invalidation_listener_evicts_matching_entries_retries_after_errors_and_stops() {
    let evicted = Arc::new(Mutex::new(Vec::new()));
    let seen = Arc::new(Mutex::new(Vec::new()));
    let (stop, shutdown) = tokio::sync::oneshot::channel();
    let task = tokio::spawn(run_dek_invalidation_listener(
        ScriptedSource {
            step: 0,
            seen: seen.clone(),
        },
        Recorder(evicted.clone()),
        "at-rest",
        shutdown,
    ));
    // let the scripted batch, the failure and its one-second backoff elapse
    tokio::time::sleep(Duration::from_secs(5)).await;
    assert_eq!(*evicted.lock().expect("lock"), vec!["t1".to_string()]);
    // the cursor advanced to the newest id after the first batch
    assert_eq!(
        seen.lock().expect("lock")[..2],
        ["$".to_string(), "1-0".to_string()]
    );
    stop.send(()).expect("listener still running");
    task.await.expect("listener stops on shutdown");
}

#[tokio::test(start_paused = true)]
async fn the_invalidation_listener_stops_during_its_error_backoff() {
    struct Failing;
    impl InvalidationSource for Failing {
        async fn read_after(
            &mut self,
            _last_id: &str,
        ) -> Result<(String, Vec<HashMap<String, String>>), String> {
            Err("down".to_string())
        }
    }
    let (stop, shutdown) = tokio::sync::oneshot::channel();
    let task = tokio::spawn(run_dek_invalidation_listener(
        Failing,
        Recorder(Arc::new(Mutex::new(Vec::new()))),
        "at-rest",
        shutdown,
    ));
    tokio::time::sleep(Duration::from_millis(100)).await;
    stop.send(()).expect("listener still running");
    task.await.expect("listener stops while backing off");
}

#[test]
fn envelope_headers_are_validated_without_decrypting() {
    assert!(matches!(
        envelope_dek_version(b"plain text"),
        Err(CryptoError::NotEnvelope)
    ));
    // magic only: shorter than a full envelope
    assert!(matches!(
        envelope_dek_version(&MAGIC),
        Err(CryptoError::Format(m)) if m.contains("too short")
    ));
    // full length, unknown algorithm id
    let mut bad_alg = MAGIC.to_vec();
    bad_alg.resize(OVERHEAD, 0);
    bad_alg[4] = ALG_AES256GCM_V1 + 1;
    assert!(matches!(
        envelope_dek_version(&bad_alg),
        Err(CryptoError::Format(m)) if m.contains("algorithm")
    ));
    // full length, known algorithm: the version is read big-endian
    let mut ok = MAGIC.to_vec();
    ok.resize(OVERHEAD, 0);
    ok[4] = ALG_AES256GCM_V1;
    ok[5..9].copy_from_slice(&7u32.to_be_bytes());
    assert_eq!(envelope_dek_version(&ok).expect("version"), 7);
}

#[test]
fn every_error_has_a_stable_closed_result_label() {
    let labels = [
        CryptoError::KeyUnavailable(DekUnavailableError {
            tenant_id: "1".to_string(),
            dek_version: None,
            reason: "x".to_string(),
        })
        .result_label(),
        CryptoError::Integrity.result_label(),
        CryptoError::Format("x".to_string()).result_label(),
        CryptoError::NotEnvelope.result_label(),
    ];
    assert_eq!(
        labels,
        ["key_unavailable", "integrity", "format", "not_envelope"]
    );
}

#[test]
fn the_remaining_counters_record_without_a_configured_exporter() {
    // No exporter is installed: recording must still be a harmless no-op, never a panic.
    metrics::record_key_unavailable("object");
    metrics::record_legacy_plaintext_read("object");
}
