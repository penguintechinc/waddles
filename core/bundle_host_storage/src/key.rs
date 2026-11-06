//! Guest key validation + host-side object-key construction. A guest key is
//! rejected (never "cleaned") unless it is a plain relative `a/b/c` path of
//! `[A-Za-z0-9._-]` segments: no `..`, no `.`, no empty segment, no `%` (so no
//! percent-encoded separators), no backslash, no NUL/control bytes.

use crate::error::StorageError;

pub const MAX_KEY_BYTES: usize = 512;
pub const MAX_SEGMENTS: usize = 16;

fn seg_byte_ok(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-')
}

/// Validates a guest-supplied object key (non-empty).
pub fn validate_key(key: &str) -> Result<(), StorageError> {
    if key.is_empty() {
        return Err(StorageError::Invalid("empty key".into()));
    }
    validate_path(key)
}

/// Validates a guest list prefix (empty allowed; a trailing `/` is stripped by the caller).
pub fn validate_key_prefix(prefix: &str) -> Result<(), StorageError> {
    if prefix.is_empty() {
        return Ok(());
    }
    validate_path(prefix.strip_suffix('/').unwrap_or(prefix))
}

fn validate_path(path: &str) -> Result<(), StorageError> {
    if path.len() > MAX_KEY_BYTES {
        return Err(StorageError::Invalid("key too long".into()));
    }
    let mut count = 0usize;
    for seg in path.split('/') {
        count += 1;
        if seg.is_empty() || seg == "." || seg == ".." {
            return Err(StorageError::Invalid("bad path segment".into()));
        }
        if !seg.bytes().all(seg_byte_ok) {
            return Err(StorageError::Invalid("illegal character in key".into()));
        }
    }
    if count > MAX_SEGMENTS {
        return Err(StorageError::Invalid("too many segments".into()));
    }
    Ok(())
}

/// A bundle app_id usable as a prefix owner (`[a-z0-9][a-z0-9._-]{0,62}`).
pub fn validate_owner(owner: &str) -> Result<(), StorageError> {
    let b = owner.as_bytes();
    let ok = !b.is_empty()
        && b.len() <= 63
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter().all(|c| {
            c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, b'.' | b'_' | b'-')
        })
        && !owner.contains("..");
    if ok {
        Ok(())
    } else {
        Err(StorageError::Invalid("bad prefix owner".into()))
    }
}

/// `<owner>/<tenant>/<community>/` -- the host-built scope prefix (`community` `0` = tenant-wide).
pub fn scope_prefix(owner: &str, tenant: u64, community: u64) -> String {
    format!("{owner}/{tenant}/{community}/")
}

/// Full object key: scope prefix + validated guest key.
pub fn object_key(owner: &str, tenant: u64, community: u64, key: &str) -> String {
    format!("{}{key}", scope_prefix(owner, tenant, community))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_plain_paths() {
        for k in ["a", "a/b.c", "clip_1-2.mp4", "x/y/z/0001.png"] {
            validate_key(k).expect(k);
        }
    }

    #[test]
    fn rejects_traversal_encoding_and_oddities() {
        for k in [
            "", "..", "../x", "a/../b", "a/./b", "/abs", "a//b", "a/", "a\\b", "a%2fb", "%2e%2e/x",
            "a b", "a\0b", "a:b", "a/..%2f", "é", "a?x", "a#x",
        ] {
            assert!(validate_key(k).is_err(), "{k:?}");
        }
        assert!(validate_key(&"a".repeat(513)).is_err());
        assert!(validate_key(&vec!["a"; 17].join("/")).is_err());
        assert!(validate_key(&vec!["a"; 16].join("/")).is_ok());
    }

    #[test]
    fn prefix_validation() {
        assert!(validate_key_prefix("").is_ok());
        assert!(validate_key_prefix("a/b/").is_ok());
        assert!(validate_key_prefix("../").is_err());
        assert!(validate_key_prefix("a/%2e").is_err());
    }

    #[test]
    fn owner_validation() {
        assert!(validate_owner("waddles.harness.media").is_ok());
        for o in ["", "A", "a/b", "a..b", ".a", "a b", &"a".repeat(64)] {
            assert!(validate_owner(o).is_err(), "{o}");
        }
    }

    #[test]
    fn object_key_shape() {
        assert_eq!(object_key("app", 1, 2, "k/v"), "app/1/2/k/v");
    }
}
