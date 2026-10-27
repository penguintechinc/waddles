//! Opaque VIEW-credential generation + at-rest hashing.
//!
//! `generate_view_token()` is the direct Rust equivalent of the legacy
//! `secrets.token_hex(32)` (`core/browser_source_core_module/services/
//! overlay_service.py`) -- same length (64 hex chars), same source
//! (OS CSPRNG via `rand::rngs::OsRng`, never derived from `community_id`
//! or any other predictable input, security.md "unguessable, not merely
//! unlisted"). The only behavioral change from the legacy scheme is
//! `hash_token()`: the plaintext token is never written to the database --
//! only its SHA-256 hex digest is persisted
//! (`overlay_view_credentials.key_hash`,
//! `config/postgres/migrations/100_overlay_view_credentials.sql`).

use rand::RngCore;
use sha2::{Digest, Sha256};

/// Length in raw bytes of a generated VIEW token before hex-encoding --
/// 32 bytes -> 64 hex chars, matching the legacy key's length exactly so
/// nothing downstream (URL parsing, DB column width) needs to change.
pub const VIEW_TOKEN_BYTES: usize = 32;

/// Generate a new cryptographically-random opaque VIEW token (64 lowercase
/// hex chars). Never derived from `community_id` or any other predictable
/// input -- callers must not "salt" this with anything guessable.
pub fn generate_view_token() -> String {
    let mut bytes = [0u8; VIEW_TOKEN_BYTES];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    hex::encode(bytes)
}

/// SHA-256 hex digest of `token` -- the only form of a VIEW token ever
/// persisted. Deterministic (same input always hashes identically), which
/// is what allows `WHERE key_hash = $1` lookups; this is a public,
/// non-secret-keyed hash (not HMAC) because the thing being protected is
/// "don't hand out live credentials from a DB dump/backup leak", not
/// "resist a hash-table of previously-seen tokens" -- the token itself
/// already has 256 bits of entropy, so a bare SHA-256 digest is
/// unreversible for any attacker who doesn't already hold the token.
pub fn hash_token(token: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    hex::encode(hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_token_is_64_lowercase_hex_chars() {
        let token = generate_view_token();
        assert_eq!(token.len(), 64);
        assert!(token
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn two_generated_tokens_differ() {
        assert_ne!(generate_view_token(), generate_view_token());
    }

    #[test]
    fn hash_is_deterministic() {
        let token = "a".repeat(64);
        assert_eq!(hash_token(&token), hash_token(&token));
    }

    #[test]
    fn hash_differs_for_different_tokens() {
        assert_ne!(hash_token(&"a".repeat(64)), hash_token(&"b".repeat(64)));
    }

    #[test]
    fn hash_is_64_char_hex_sha256_digest() {
        let digest = hash_token("known-input");
        assert_eq!(digest.len(), 64);
        assert!(digest.chars().all(|c| c.is_ascii_hexdigit()));
    }
}
