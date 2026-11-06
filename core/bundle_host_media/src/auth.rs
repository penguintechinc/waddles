//! Machine-JWT seam: the crate never mints tokens, the stage injects a source.

use std::sync::Arc;

use async_trait::async_trait;

/// Supplies a currently-valid hub-api machine JWT (never a guest value).
#[async_trait]
pub trait BearerSource: Send + Sync {
    /// The bearer token, or a short failure reason (never token material).
    async fn bearer(&self) -> Result<Arc<str>, String>;
}

/// Fixed token source for tests.
pub struct StaticBearer(pub Arc<str>);

#[async_trait]
impl BearerSource for StaticBearer {
    async fn bearer(&self) -> Result<Arc<str>, String> {
        Ok(self.0.clone())
    }
}
