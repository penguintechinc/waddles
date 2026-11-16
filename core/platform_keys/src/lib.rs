//! Data-plane verifying-key cache over `platform_vault.public_keys` (design T5 of the platform
//! secret store, `docs/PLATFORM_VAULT.md`).
//!
//! Data-plane services verify signatures (bundle artifacts, service JWTs) against PUBLIC keys that
//! hub-api generates and rotates. They read those keys from the read-only replica view
//! `platform_vault.public_keys` (active + verify-only rows, public columns only) instead of a
//! ConfigMap, so a rotation needs no redeploy. This crate is the cache between the two:
//!
//! * [`KeyCache::seed`] at startup (bounded backoff) -- readiness is false until every required key
//!   type has a usable row;
//! * lazy-load on an unknown `kid`, with a 30 s negative cache so a flood of bad kids cannot hammer
//!   the database;
//! * a 60 s poll ([`KeyCache::spawn_poller`]) that replaces the snapshot, so a retired key simply
//!   disappears;
//! * database down: keep the last-known keys (never clear), count the failure;
//! * unknown `kid`: fail closed. Verify-only keys are honoured only until `verify_until`
//!   (`rotated_at` + a configured window) and only while the snapshot is fresher than
//!   [`CacheConfig::max_staleness`] -- a retired or compromised key cannot keep verifying just
//!   because hub-api or the database is unreachable (security review M6);
//! * [`KeySource`] is the `db|configmap` migration switch. It is resolved ONCE at startup and a
//!   `db` source that cannot load FAILS (never degrades to the ConfigMap).
//!
//! The core has no database dependency (so the credential-less bundle executor, whose dependency
//! policy bans sqlx/sea-orm, can embed it behind the [`KeyLoader`] trait); the Postgres loader is
//! the `postgres` feature ([`pg::PgKeyLoader`]). Only public key material, key type codes,
//! versions and kids ever appear here: no sealed value, fingerprint or secret, and logs/labels
//! never carry key bytes.

mod cache;
mod metrics;
#[cfg(feature = "postgres")]
pub mod pg;

use std::sync::Arc;
use std::time::{Duration, SystemTime};

pub use cache::{CacheConfig, KeyCache};

/// Source of wall-clock time, injectable so expiry/staleness are testable without sleeping.
pub trait Clock: Send + Sync {
    /// The current wall-clock time.
    fn now(&self) -> SystemTime;
}

/// [`Clock`] backed by the system clock (production).
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> SystemTime {
        SystemTime::now()
    }
}

/// Lifecycle state of a published key (`platform_vault.secrets.state`, restricted by the view to
/// the two verifiable states).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyState {
    /// Code 2: the current signing key; always verifies.
    Active,
    /// Code 3: demoted by a rotation; verifies only until `verify_until`.
    VerifyOnly,
}

impl KeyState {
    /// Decodes the view's int state code. Any other code is a data error, never a default.
    pub fn from_code(code: i16) -> Result<Self, LoaderError> {
        match code {
            2 => Ok(Self::Active),
            3 => Ok(Self::VerifyOnly),
            other => Err(LoaderError::Malformed(format!(
                "unexpected key state code {other}"
            ))),
        }
    }
}

/// One verifying key as published by the view. `public_material` is public by definition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyRecord {
    /// Registry type code (`secret_type`); a kid is only meaningful within its type.
    pub key_type: i16,
    /// Scope (tenant) the key belongs to; `None` is global.
    pub scope_id: Option<i64>,
    /// Monotonic version within (type, scope).
    pub version: i32,
    /// Key identifier carried in signatures / JWT headers.
    pub kid: String,
    /// Lifecycle state.
    pub state: KeyState,
    /// Raw public key bytes (algorithm-specific encoding, interpreted by the caller).
    pub public_material: Vec<u8>,
    /// When the key became active.
    pub activated_at: Option<SystemTime>,
    /// When the key was demoted to verify-only (start of its verify window).
    pub rotated_at: Option<SystemTime>,
}

impl KeyRecord {
    /// The instant a verify-only key stops verifying: `rotated_at + window`. `None` for an active
    /// key, and for a verify-only key with no `rotated_at` (callers treat that as already expired:
    /// the window cannot be computed, so fail closed).
    #[must_use]
    pub fn verify_until(&self, window: Duration) -> Option<SystemTime> {
        match self.state {
            KeyState::Active => None,
            KeyState::VerifyOnly => self.rotated_at.and_then(|t| t.checked_add(window)),
        }
    }
}

/// Failure reading keys from the backing store. Messages never carry connection strings or key
/// material.
#[derive(Debug, thiserror::Error)]
pub enum LoaderError {
    /// The store could not be reached or the query failed.
    #[error("key store unavailable: {0}")]
    Unavailable(String),
    /// A returned row violated the schema contract.
    #[error("malformed key row: {0}")]
    Malformed(String),
}

/// Reads keys from the backing store. Implemented by [`pg::PgKeyLoader`]; tests use fakes.
#[async_trait::async_trait]
pub trait KeyLoader: Send + Sync {
    /// Every active and verify-only key (the seed / poll query).
    async fn load_all(&self) -> Result<Vec<KeyRecord>, LoaderError>;
    /// The keys carrying exactly `kid` (the lazy-load query).
    async fn load_kid(&self, kid: &str) -> Result<Vec<KeyRecord>, LoaderError>;
}

/// In-memory [`KeyLoader`] over a replaceable row set: for embedders' tests and for services that
/// stage keys from another trusted channel. Not a fallback for a failing database source.
#[derive(Debug, Default)]
pub struct MemoryKeyLoader {
    rows: std::sync::Mutex<Vec<KeyRecord>>,
}

impl MemoryKeyLoader {
    /// A loader serving `rows`.
    #[must_use]
    pub fn new(rows: Vec<KeyRecord>) -> Self {
        Self {
            rows: std::sync::Mutex::new(rows),
        }
    }

    /// Replaces the served rows.
    pub fn set(&self, rows: Vec<KeyRecord>) {
        *self
            .rows
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = rows;
    }

    fn snapshot(&self) -> Vec<KeyRecord> {
        self.rows
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
}

#[async_trait::async_trait]
impl KeyLoader for MemoryKeyLoader {
    async fn load_all(&self) -> Result<Vec<KeyRecord>, LoaderError> {
        Ok(self.snapshot())
    }

    async fn load_kid(&self, kid: &str) -> Result<Vec<KeyRecord>, LoaderError> {
        Ok(self
            .snapshot()
            .into_iter()
            .filter(|r| r.kid == kid)
            .collect())
    }
}

/// Why a lookup was refused. Every variant is a fail-closed outcome: the caller must reject the
/// signature.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum KeyError {
    /// No key with this kid (and type) exists, or it was recently confirmed absent.
    #[error("unknown key id")]
    UnknownKid,
    /// A verify-only key past its `verify_until`, or with no computable window.
    #[error("verify-only key past its verify window")]
    Expired,
    /// A verify-only key while the snapshot is older than the staleness cap.
    #[error("key snapshot too stale to honour a verify-only key")]
    Stale,
    /// The kid was not cached and the store could not be read.
    #[error("key store unavailable and key not cached")]
    Unavailable,
}

/// Startup failure of [`KeyCache::seed`] / [`CacheConfig`] validation / [`KeySource::resolve`].
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SetupError {
    /// Required key types still missing when the seed deadline passed.
    #[error("platform keys not ready after {attempts} attempts; missing key types {missing:?}")]
    NotReady {
        /// Required type codes with no usable row.
        missing: Vec<i16>,
        /// Number of load attempts made.
        attempts: u32,
    },
    /// Invalid configuration (empty required set, bad source value, ...).
    #[error("invalid platform key configuration: {0}")]
    Config(String),
}

/// Where a service reads verifying keys from during the ConfigMap -> database migration.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeySource {
    /// `platform_vault.public_keys` through [`KeyCache`] (the default).
    Db,
    /// The legacy static ConfigMap/env key set (migration only).
    ConfigMap,
}

impl KeySource {
    /// Resolves the source once at startup. An explicit `*_KEY_SOURCE` value (`db` or `configmap`,
    /// case-insensitive) wins; any other non-empty value is an ERROR (a typo must not silently
    /// pick a source). With no value, the kill-switch flag `waddles.disable-db-platform-keys`
    /// decides: ON selects the ConfigMap, OFF or unseen selects the database.
    ///
    /// There is deliberately no runtime fallback between the two: once `Db` is chosen, a database
    /// that cannot be read fails readiness and fails lookups (fail loud), it never consults the
    /// ConfigMap.
    pub fn resolve(explicit: Option<&str>, disable_db_flag_on: bool) -> Result<Self, SetupError> {
        match explicit.map(str::trim) {
            None | Some("") => Ok(if disable_db_flag_on {
                Self::ConfigMap
            } else {
                Self::Db
            }),
            Some(v) if v.eq_ignore_ascii_case("db") => Ok(Self::Db),
            Some(v) if v.eq_ignore_ascii_case("configmap") => Ok(Self::ConfigMap),
            Some(_) => Err(SetupError::Config(
                "key source must be `db` or `configmap`".to_string(),
            )),
        }
    }
}

/// Convenience alias for a shared cache handle.
pub type SharedKeyCache = Arc<KeyCache>;

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn key_state_codes() {
        assert_eq!(
            KeyState::from_code(2).unwrap_or(KeyState::VerifyOnly),
            KeyState::Active
        );
        assert_eq!(KeyState::from_code(3).ok(), Some(KeyState::VerifyOnly));
        for bad in [0, 1, 4, 5, -1] {
            assert!(KeyState::from_code(bad).is_err(), "code {bad}");
        }
    }

    #[test]
    fn verify_until_rules() {
        let t0 = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000);
        let mut r = KeyRecord {
            key_type: 1,
            scope_id: None,
            version: 1,
            kid: "k".into(),
            state: KeyState::Active,
            public_material: vec![1],
            activated_at: None,
            rotated_at: Some(t0),
        };
        assert_eq!(r.verify_until(Duration::from_secs(60)), None);
        r.state = KeyState::VerifyOnly;
        assert_eq!(
            r.verify_until(Duration::from_secs(60)),
            Some(t0 + Duration::from_secs(60))
        );
        r.rotated_at = None;
        assert_eq!(r.verify_until(Duration::from_secs(60)), None);
    }

    #[test]
    fn key_source_resolution() {
        let cases: [(Option<&str>, bool, Result<KeySource, ()>); 9] = [
            (None, false, Ok(KeySource::Db)),
            (None, true, Ok(KeySource::ConfigMap)),
            (Some(""), false, Ok(KeySource::Db)),
            (Some("  "), true, Ok(KeySource::ConfigMap)),
            (Some("db"), true, Ok(KeySource::Db)),
            (Some("DB"), false, Ok(KeySource::Db)),
            (Some("configmap"), false, Ok(KeySource::ConfigMap)),
            (Some("ConfigMap"), true, Ok(KeySource::ConfigMap)),
            (Some("dbb"), false, Err(())),
        ];
        for (explicit, flag, want) in cases {
            let got = KeySource::resolve(explicit, flag).map_err(|_| ());
            assert_eq!(got, want, "explicit={explicit:?} flag={flag}");
        }
    }

    #[tokio::test]
    async fn memory_loader_serves_and_replaces_rows() {
        let row = |kid: &str| KeyRecord {
            key_type: 1,
            scope_id: None,
            version: 1,
            kid: kid.into(),
            state: KeyState::Active,
            public_material: vec![1],
            activated_at: None,
            rotated_at: None,
        };
        let l = MemoryKeyLoader::new(vec![row("a"), row("b")]);
        assert_eq!(l.load_all().await.map(|v| v.len()).ok(), Some(2));
        assert_eq!(l.load_kid("a").await.map(|v| v.len()).ok(), Some(1));
        l.set(vec![]);
        assert_eq!(l.load_all().await.map(|v| v.len()).ok(), Some(0));
    }

    #[test]
    fn system_clock_moves_forward() {
        let c = SystemClock;
        assert!(c.now() >= SystemTime::UNIX_EPOCH);
    }
}
