//! Error types shared by every seal/open/DEK-resolution path. None of them
//! ever carries key material, plaintext, or ciphertext bytes.

/// A tenant's DEK could not be resolved -- the caller MUST fail closed and
/// never fall back to plaintext.
#[derive(Debug, thiserror::Error, Clone)]
#[error("tenant DEK unavailable for tenant={tenant_id:?} dek_version={dek_version:?}: {reason}")]
pub struct DekUnavailableError {
    /// Tenant the lookup was for (numeric id rendered as a string).
    pub tenant_id: String,
    /// Specific version requested, when the caller asked for one.
    pub dek_version: Option<u32>,
    /// Human-readable cause; never contains key bytes.
    pub reason: String,
}

/// Why a seal/open/blind-index call failed.
#[derive(Debug, thiserror::Error, Clone)]
pub enum CryptoError {
    /// No DEK for the tenant (broker down, revoked, unknown tenant/version).
    #[error(transparent)]
    KeyUnavailable(#[from] DekUnavailableError),
    /// AEAD authentication failed: wrong tenant/community/app/key, wrong DEK,
    /// or a corrupted/tampered ciphertext. Deliberately not more specific.
    #[error("aead authentication failed")]
    Integrity,
    /// The bytes are not a well-formed envelope (truncated, bad alg) or a
    /// primitive refused its input.
    #[error("malformed envelope: {0}")]
    Format(String),
    /// The bytes do not start with the envelope magic (legacy plaintext).
    #[error("value is not a sealed envelope")]
    NotEnvelope,
}

impl CryptoError {
    /// Stable low-cardinality label for the `result` metric attribute.
    #[must_use]
    pub fn result_label(&self) -> &'static str {
        match self {
            Self::KeyUnavailable(_) => "key_unavailable",
            Self::Integrity => "integrity",
            Self::Format(_) => "format",
            Self::NotEnvelope => "not_envelope",
        }
    }
}
