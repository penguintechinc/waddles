//! Error surface; `wire_code` collapses to the WIT `storage.error` variants.

/// Failure of one storage host call. Tag-hidden objects are reported as `NotFound`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum StorageError {
    #[error("storage not granted: {0}")]
    NotGranted(String),
    #[error("not found")]
    NotFound,
    #[error("invalid: {0}")]
    Invalid(String),
    #[error("too large: {0} bytes")]
    TooLarge(u64),
    #[error("quota exceeded")]
    QuotaExceeded,
    #[error("rate limited")]
    RateLimited,
    #[error("backend: {0}")]
    Backend(String),
    #[error("storage capability not enabled")]
    NotImplemented,
}

impl StorageError {
    /// Stable metric/WIT variant name.
    pub fn wire_code(&self) -> &'static str {
        match self {
            Self::NotGranted(_) => "not-granted",
            Self::NotFound => "not-found",
            Self::Invalid(_) => "invalid",
            Self::TooLarge(_) => "too-large",
            Self::QuotaExceeded => "quota-exceeded",
            Self::RateLimited => "rate-limited",
            Self::Backend(_) => "backend",
            Self::NotImplemented => "not-implemented",
        }
    }
}
