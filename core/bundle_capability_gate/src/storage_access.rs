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

/// A prefix owner is a bundle app_id: the one shared grammar
/// ([`crate::resource::is_valid_app_id`]).
pub fn is_valid_owner(owner: &str) -> bool {
    crate::resource::is_valid_app_id(owner)
}
