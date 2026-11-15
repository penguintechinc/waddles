//! Shared ES256 fixture for the integration tests that mint platform JWTs.
//!
//! A throwaway P-256 key is generated once per test process (never a real key, nothing in
//! git); [`keys`] publishes its public point as an active `JWT_SIGNING` row through the same
//! `KeyCache` path production uses (a `MemoryKeyLoader`, no database).

#![allow(dead_code)]

use std::sync::OnceLock;

use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use platform_keys::{KeyRecord, KeyState};
use serde::Serialize;
use svc_streaming::jwt_keys::{JwtKeys, JWT_SIGNING_KEY_TYPE};

/// The `kid` every fixture token carries.
pub const KID: &str = "test-es256-v1";

/// One generated signing key.
pub struct Fixture {
    /// PKCS#8 private key.
    pub encoding: EncodingKey,
    /// Uncompressed public point (`0x04 || X || Y`).
    pub point: Vec<u8>,
}

impl Fixture {
    /// Generates a fresh ECDSA P-256 key.
    pub fn generate() -> Self {
        let pair = rcgen::KeyPair::generate().expect("generate p256 key");
        Self {
            encoding: EncodingKey::from_ec_pem(pair.serialize_pem().as_bytes())
                .expect("pem encoding key"),
            point: pair.public_key_raw().to_vec(),
        }
    }

    /// An active published row for this key.
    pub fn row(&self, kid: &str) -> KeyRecord {
        KeyRecord {
            key_type: JWT_SIGNING_KEY_TYPE,
            scope_id: None,
            version: 1,
            kid: kid.to_string(),
            state: KeyState::Active,
            public_material: self.point.clone(),
            activated_at: None,
            rotated_at: None,
        }
    }

    /// Signs `claims` as ES256 under `kid`.
    pub fn sign_with_kid<T: Serialize>(&self, claims: &T, kid: &str) -> String {
        let mut header = Header::new(Algorithm::ES256);
        header.kid = Some(kid.to_string());
        encode(&header, claims, &self.encoding).expect("sign")
    }
}

/// The process-wide fixture key.
pub fn fixture() -> &'static Fixture {
    static FIXTURE: OnceLock<Fixture> = OnceLock::new();
    FIXTURE.get_or_init(Fixture::generate)
}

/// A verifier trusting only the fixture key under [`KID`].
pub fn keys() -> JwtKeys {
    JwtKeys::in_memory(vec![fixture().row(KID)])
}

/// Signs `claims` with the fixture key and [`KID`].
pub fn sign<T: Serialize>(claims: &T) -> String {
    fixture().sign_with_kid(claims, KID)
}
