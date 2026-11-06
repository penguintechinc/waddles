//! Grant evaluation: own prefix auto-granted; cross-prefix needs an approved exact grant.

use std::collections::HashSet;

use crate::error::StorageError;

/// A storage operation. `Read` covers get + presign-get.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum Op {
    Read,
    List,
    Write,
    Delete,
}

impl Op {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Read => "read",
            Self::List => "list",
            Self::Write => "write",
            Self::Delete => "delete",
        }
    }
}

/// The caller's APPROVED cross-prefix grants, parsed from granted permission ids
/// `storage.<op>:<prefix_owner>` (the gate snapshot's granted set).
#[derive(Clone, Debug, Default)]
pub struct GrantSet {
    grants: HashSet<(String, Op)>,
}

impl GrantSet {
    /// Builds from granted permission ids; non-storage ids are ignored, malformed
    /// `storage.*` ids are an error (fail loud, never silently dropped).
    pub fn from_permission_ids<'a>(
        ids: impl IntoIterator<Item = &'a str>,
    ) -> Result<Self, StorageError> {
        let mut grants = HashSet::new();
        for id in ids {
            let Some(rest) = id.strip_prefix("storage.") else {
                continue;
            };
            let (op, owner) = rest
                .split_once(':')
                .ok_or_else(|| StorageError::Invalid(format!("malformed grant {id}")))?;
            let op = match op {
                "read" => Op::Read,
                "list" => Op::List,
                "write" => Op::Write,
                "delete" => Op::Delete,
                _ => return Err(StorageError::Invalid(format!("unknown storage op in {id}"))),
            };
            crate::key::validate_owner(owner)?;
            grants.insert((owner.to_string(), op));
        }
        Ok(Self { grants })
    }

    /// `true` iff the exact `(owner, op)` grant is approved.
    pub fn allows(&self, owner: &str, op: Op) -> bool {
        self.grants.contains(&(owner.to_string(), op))
    }
}

/// `true` when `owner` is the caller's own prefix (auto-granted).
pub fn is_own(caller: &str, owner: &str) -> bool {
    caller == owner
}

/// Authorization decision; `Ok(true)` = cross-prefix (tag filter + audit apply).
pub fn authorize(
    caller: &str,
    owner: &str,
    op: Op,
    grants: &GrantSet,
) -> Result<bool, StorageError> {
    if is_own(caller, owner) {
        return Ok(false);
    }
    if grants.allows(owner, op) {
        Ok(true)
    } else {
        Err(StorageError::NotGranted(format!(
            "storage.{}:{owner}",
            op.as_str()
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn own_prefix_needs_no_grant_other_needs_exact_grant() {
        let g = GrantSet::from_permission_ids(["storage.read:media", "tool.call:x:y"]).unwrap();
        assert_eq!(authorize("me", "me", Op::Delete, &g), Ok(false));
        assert_eq!(authorize("me", "media", Op::Read, &g), Ok(true));
        assert!(authorize("me", "media", Op::Write, &g).is_err());
        assert!(authorize("me", "other", Op::Read, &g).is_err());
    }

    #[test]
    fn malformed_grant_ids_fail_loud() {
        for id in ["storage.read", "storage.rm:x", "storage.read:A/B"] {
            assert!(GrantSet::from_permission_ids([id]).is_err(), "{id}");
        }
    }
}
