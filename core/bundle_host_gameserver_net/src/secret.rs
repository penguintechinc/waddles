//! Zeroizing secret string for the RCON password.

use zeroize::Zeroize;

/// A credential zeroed on drop and never printed; the only way out is [`Secret::expose`].
pub struct Secret(String);

impl Secret {
    /// Wraps a plaintext value (moved, not cloned).
    pub fn new(value: String) -> Self {
        Self(value)
    }

    /// Borrows the plaintext; call only at the point of use (the RCON login packet).
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl Drop for Secret {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("[REDACTED]")
    }
}
