//! Cross-prefix object-storage grants (spec
//! `docs/superpowers/specs/2026-11-06-bundle-object-storage.md`): ids
//! `storage.<op>:<prefix_owner>`. Mirrored in `hub_api/services/bundle_storage_access.py`.

/// Operation a storage grant authorises. `Read` covers get + presign-get.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum StorageOp {
    Read,
    List,
    Write,
    Delete,
}

impl StorageOp {
    /// The id-segment spelling.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Read => "read",
            Self::List => "list",
            Self::Write => "write",
            Self::Delete => "delete",
        }
    }
}

/// A prefix owner is a bundle app_id: `[a-z0-9][a-z0-9._-]{0,62}`, no `..`.
pub fn is_valid_owner(owner: &str) -> bool {
    let b = owner.as_bytes();
    !b.is_empty()
        && b.len() <= 63
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter().all(|c| {
            c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, b'.' | b'_' | b'-')
        })
        && !owner.contains("..")
}
