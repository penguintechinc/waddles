//! Verifying keys for the platform user/session JWT (ES256), read from
//! `platform_vault.public_keys` through [`platform_keys::KeyCache`] (Secrets T10).
//!
//! The verifier never holds a shared secret: hub-api signs with a private ES256 key it alone
//! can unseal, and this service only ever sees the PUBLIC half (a 65-byte uncompressed P-256
//! point, `0x04 || X || Y`). Rotation needs no redeploy -- the cache re-reads the replica view,
//! honours a demoted key until its verify window closes, and refuses everything else.
//!
//! [`JwtKeys`] is deliberately a thin, opaque handle: its `Debug` never prints key material
//! and a lookup failure collapses to a closed `REASON_*` so nothing attacker-chosen reaches a
//! log or metric label.

use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use platform_keys::{CacheConfig, KeyCache, KeyError, KeyRecord, MemoryKeyLoader};

use crate::http::auth::hardening::{REASON_BAD_KID, REASON_NO_KEY};

/// `SecretType.JWT_SIGNING` in the platform key registry (`platform_vault.secrets.secret_type`).
pub const JWT_SIGNING_KEY_TYPE: i16 = 3;

/// How long a rotated (verify-only) signing key keeps verifying: the 2 h session-token ceiling
/// the issuer uses, so tokens minted just before a rotation stay valid until they expire.
pub const JWT_VERIFY_WINDOW: Duration = Duration::from_secs(2 * 3600);

/// Length of an uncompressed P-256 public point (`0x04 || X(32) || Y(32)`).
const P256_POINT_LEN: usize = 65;

/// The affine coordinates of a P-256 public key.
#[derive(Clone, PartialEq, Eq)]
pub struct EcCoordinates {
    /// Big-endian X coordinate.
    pub x: [u8; 32],
    /// Big-endian Y coordinate.
    pub y: [u8; 32],
}

impl fmt::Debug for EcCoordinates {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("EcCoordinates(..)")
    }
}

impl EcCoordinates {
    /// Splits an uncompressed point; anything but exactly 65 bytes starting `0x04` is refused.
    pub fn from_uncompressed_point(point: &[u8]) -> Option<Self> {
        if point.len() != P256_POINT_LEN || point[0] != 0x04 {
            return None;
        }
        let mut x = [0_u8; 32];
        let mut y = [0_u8; 32];
        x.copy_from_slice(&point[1..33]);
        y.copy_from_slice(&point[33..65]);
        Some(Self { x, y })
    }
}

/// Handle to the platform JWT verifying keys carried on [`crate::config::Config`].
#[derive(Clone, Default)]
pub enum JwtKeys {
    /// No key source configured: every authenticated request is refused (`no_key`).
    #[default]
    None,
    /// The live cache over the replica view (production, and tests via [`JwtKeys::in_memory`]).
    Cache(Arc<KeyCache>),
}

impl fmt::Debug for JwtKeys {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::None => f.write_str("JwtKeys::None"),
            Self::Cache(_) => f.write_str("JwtKeys::Cache(..)"),
        }
    }
}

impl JwtKeys {
    /// The cache settings every JWT-key cache uses: type [`JWT_SIGNING_KEY_TYPE`] required,
    /// [`JWT_VERIFY_WINDOW`] for rotated keys.
    #[must_use]
    pub fn cache_config() -> CacheConfig {
        CacheConfig::new(vec![JWT_SIGNING_KEY_TYPE], JWT_VERIFY_WINDOW)
    }

    /// Wraps an already-built cache (production wires a `PgKeyLoader`-backed one in `lib.rs`).
    #[must_use]
    pub fn from_cache(cache: Arc<KeyCache>) -> Self {
        Self::Cache(cache)
    }

    /// A cache served from fixed in-memory rows -- the single code path for tests and dev
    /// tooling (same lookup, expiry and staleness rules as production, no database). Not a
    /// fallback for a failing database source: production never calls it.
    #[must_use]
    pub fn in_memory(rows: Vec<KeyRecord>) -> Self {
        Self::in_memory_with(Arc::new(MemoryKeyLoader::new(rows)), Self::cache_config())
    }

    /// [`Self::in_memory`] over a caller-held loader and config, so a test can swap rows or
    /// shrink the verify window while the cache is live.
    #[must_use]
    pub fn in_memory_with(loader: Arc<MemoryKeyLoader>, cfg: CacheConfig) -> Self {
        match KeyCache::new(loader, cfg) {
            Ok(cache) => Self::Cache(Arc::new(cache)),
            // `cache_config` always names one required type, so this cannot happen for it; a
            // caller-supplied invalid config yields "no keys" -- every request refused.
            Err(_) => Self::None,
        }
    }

    /// Readiness: `None` = no source configured, `Some(ready)` = every required type usable.
    #[must_use]
    pub fn ready(&self) -> Option<bool> {
        match self {
            Self::None => None,
            Self::Cache(cache) => Some(cache.ready()),
        }
    }

    /// Resolves `kid` to its P-256 public coordinates, or a closed `REASON_*`.
    ///
    /// * no source configured, or the store unreadable with the kid uncached -> `no_key`;
    /// * unknown / expired verify-only / stale-snapshot kid, or key material that is not a
    ///   valid uncompressed P-256 point -> `bad_kid`.
    pub async fn resolve(&self, kid: &str) -> Result<EcCoordinates, &'static str> {
        let Self::Cache(cache) = self else {
            return Err(REASON_NO_KEY);
        };
        let record = cache
            .get(JWT_SIGNING_KEY_TYPE, kid)
            .await
            .map_err(|e| match e {
                KeyError::Unavailable => REASON_NO_KEY,
                KeyError::UnknownKid | KeyError::Expired | KeyError::Stale => REASON_BAD_KID,
            })?;
        EcCoordinates::from_uncompressed_point(&record.public_material).ok_or(REASON_BAD_KID)
    }
}

/// Test fixtures: throwaway P-256 keys generated per run (never a real key, nothing in git).
#[cfg(test)]
pub(crate) mod test_support {
    use super::*;
    use jsonwebtoken::EncodingKey;
    use platform_keys::KeyState;

    /// A generated signing key plus its published public point.
    pub struct TestKey {
        /// PKCS#8 private key (test only).
        pub encoding: EncodingKey,
        /// Uncompressed public point.
        pub point: Vec<u8>,
    }

    impl TestKey {
        /// Generates a fresh ECDSA P-256 key.
        pub fn generate() -> Self {
            let pair = rcgen::KeyPair::generate().expect("generate p256 key");
            Self {
                encoding: EncodingKey::from_ec_pem(pair.serialize_pem().as_bytes())
                    .expect("pem encoding key"),
                point: pair.public_key_raw().to_vec(),
            }
        }

        /// An active row for this key under `kid`.
        pub fn active_row(&self, kid: &str) -> KeyRecord {
            self.row(kid, KeyState::Active, None)
        }

        /// A row in `state`, rotated at `rotated_at`.
        pub fn row(
            &self,
            kid: &str,
            state: KeyState,
            rotated_at: Option<std::time::SystemTime>,
        ) -> KeyRecord {
            KeyRecord {
                key_type: JWT_SIGNING_KEY_TYPE,
                scope_id: None,
                version: 1,
                kid: kid.into(),
                state,
                public_material: self.point.clone(),
                activated_at: None,
                rotated_at,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::TestKey;
    use super::*;
    use platform_keys::KeyState;
    use std::time::SystemTime;

    #[test]
    fn debug_never_prints_key_material() {
        let key = TestKey::generate();
        let keys = JwtKeys::in_memory(vec![key.active_row("k1")]);
        assert_eq!(format!("{keys:?}"), "JwtKeys::Cache(..)");
        assert_eq!(format!("{:?}", JwtKeys::None), "JwtKeys::None");
        let coords = EcCoordinates::from_uncompressed_point(&key.point).expect("valid point");
        assert_eq!(format!("{coords:?}"), "EcCoordinates(..)");
    }

    #[test]
    fn point_validation_is_exact() {
        let mut point = vec![0x04];
        point.extend_from_slice(&[7_u8; 64]);
        assert!(EcCoordinates::from_uncompressed_point(&point).is_some());
        for bad in [
            Vec::new(),
            point[..64].to_vec(),
            [point.clone(), vec![0]].concat(),
            [vec![0x02], point[1..].to_vec()].concat(),
            [vec![0x03], point[1..].to_vec()].concat(),
        ] {
            assert!(EcCoordinates::from_uncompressed_point(&bad).is_none());
        }
    }

    #[tokio::test]
    async fn none_is_no_key_and_a_known_kid_resolves() {
        assert_eq!(
            JwtKeys::None.resolve("k1").await.unwrap_err(),
            REASON_NO_KEY
        );
        assert_eq!(JwtKeys::None.ready(), None);
        let key = TestKey::generate();
        let keys = JwtKeys::in_memory(vec![key.active_row("k1")]);
        let got = keys.resolve("k1").await.expect("known kid");
        assert_eq!(got.x[..], key.point[1..33]);
        assert_eq!(got.y[..], key.point[33..]);
        assert_eq!(keys.resolve("nope").await.unwrap_err(), REASON_BAD_KID);
    }

    #[tokio::test]
    async fn malformed_key_material_is_bad_kid() {
        let key = TestKey::generate();
        let mut row = key.active_row("k1");
        row.public_material = vec![4, 1, 2, 3];
        let keys = JwtKeys::in_memory(vec![row]);
        assert_eq!(keys.resolve("k1").await.unwrap_err(), REASON_BAD_KID);
    }

    #[tokio::test]
    async fn verify_only_key_is_refused_past_its_window() {
        let key = TestKey::generate();
        let inside = SystemTime::now() - Duration::from_secs(3600);
        let outside = SystemTime::now() - JWT_VERIFY_WINDOW - Duration::from_secs(60);
        let keys = JwtKeys::in_memory(vec![
            key.row("rot-in", KeyState::VerifyOnly, Some(inside)),
            key.row("rot-out", KeyState::VerifyOnly, Some(outside)),
        ]);
        assert!(keys.resolve("rot-in").await.is_ok());
        assert_eq!(keys.resolve("rot-out").await.unwrap_err(), REASON_BAD_KID);
    }
}
