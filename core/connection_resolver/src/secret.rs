//! Zeroizing secret string used for every credential value this crate returns.

use serde::{Deserialize, Deserializer};
use zeroize::Zeroize;

/// A credential value that is zeroed on drop, never printed, never serialized.
///
/// Exists so a resolved token/password cannot leak through `{:?}`, `Display`
/// or serde by accident; the only way out is the explicit [`SecretString::expose`].
pub struct SecretString(String);

impl SecretString {
    /// Wrap a plaintext value (takes ownership so the caller's copy is moved, not cloned).
    pub fn new(value: String) -> Self {
        Self(value)
    }

    /// Borrow the plaintext; call only at the point of use (signing, header build).
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl Drop for SecretString {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}

impl std::fmt::Debug for SecretString {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("[REDACTED]")
    }
}

impl<'de> Deserialize<'de> for SecretString {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        String::deserialize(d).map(SecretString)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_is_redacted_and_expose_returns_plaintext() {
        let s = SecretString::new("hunter2".to_string());
        assert_eq!(format!("{s:?}"), "[REDACTED]");
        assert_eq!(s.expose(), "hunter2");
    }

    #[test]
    fn deserializes_from_json_string() {
        let s: SecretString = serde_json::from_str("\"abc\"").unwrap();
        assert_eq!(s.expose(), "abc");
    }
}
