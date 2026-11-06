//! Stream-key handling shared by every ingest listener (RTMP/SRT/WHIP) and `ingest-auth`.
//!
//! hub-api (alembic 0077) stores only `hex(sha256(key))` in
//! `streaming_configs.stream_key_hash`; the plaintext key is never persisted. Every
//! authorization path therefore (1) rejects an empty or too-short key outright -- the legacy
//! plaintext match on `source_url` let an empty key match a config created with
//! `source_url = ''` -- and (2) hashes the presented key and matches the hash column.
//! `source_url` is no longer consulted for authorization anywhere.

use sea_orm::{ActiveModelTrait, DatabaseConnection, EntityTrait, Set};
use sha2::{Digest, Sha256};

use crate::db::entities::streaming_config;

/// Shortest presented key any listener will look up. hub-api mints 48-character keys; anything
/// below this is rejected before touching the DB.
pub const MIN_KEY_LEN: usize = 16;

/// `true` when `key` is long enough to be a real stream key (never true for the empty string).
pub fn is_acceptable(key: &str) -> bool {
    key.len() >= MIN_KEY_LEN
}

/// Lowercase hex SHA-256 of `key` -- identical to hub-api's `hash_stream_key`.
pub fn hash(key: &str) -> String {
    format!("{:x}", Sha256::digest(key.as_bytes()))
}

/// Test-fixture helper: fills `stream_key_hash` from each row's `source_url` (fixtures seed the
/// plaintext key there because sqlite has no SHA-256). Never called by production code.
#[doc(hidden)]
pub async fn backfill_hashes_from_source_url_for_tests(db: &DatabaseConnection) {
    for row in streaming_config::Entity::find()
        .all(db)
        .await
        .expect("load fixture configs")
    {
        let mut active: streaming_config::ActiveModel = row.clone().into();
        active.stream_key_hash = Set(Some(hash(&row.source_url)));
        active.update(db).await.expect("backfill fixture hash");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hash_matches_known_sha256_vector() {
        assert_eq!(
            hash("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn empty_and_short_keys_are_not_acceptable() {
        assert!(!is_acceptable(""));
        assert!(!is_acceptable("short"));
        assert!(!is_acceptable(&"k".repeat(MIN_KEY_LEN - 1)));
        assert!(is_acceptable(&"k".repeat(MIN_KEY_LEN)));
    }
}
