//! Shared per-tenant at-rest encryption for bundle host capabilities.
//!
//! Layering: [`envelope`] (AES-256-GCM + HKDF subkeys + AAD, pure functions),
//! [`provider`] (tenant DEK resolution: TTL cache, hub-api broker client,
//! dev provider, invalidation listener), [`tenant::TenantCrypto`] (the
//! dyn-safe seal/open/blind-index facade host capabilities hold), and
//! [`metrics`] (OTel, no key material). Design:
//! `docs/superpowers/specs/2026-11-08-tenant-at-rest-encryption.md`.

pub mod envelope;
pub mod error;
#[cfg(feature = "grpc")]
pub mod grpc;
pub mod metrics;
pub mod provider;
pub mod tenant;

pub use envelope::{build_aad, is_envelope, MAGIC, OVERHEAD};
pub use error::{CryptoError, DekUnavailableError};
pub use provider::{
    run_dek_invalidation_listener, ConfiguredDekProvider, Dek, DekBroker, DekProvider,
    HubApiDekProvider, Invalidate, InvalidationSource, LocalDevDekProvider, SealedDek,
    TtlCachedDekProvider, BUNDLE_DATA_PURPOSE, DEFAULT_DEK_CACHE_TTL, DEK_INVALIDATION_STREAM,
};
pub use tenant::TenantCrypto;
