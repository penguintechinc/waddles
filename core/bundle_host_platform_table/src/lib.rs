//! The bundle `platform-table` host capability's store (spec
//! `docs/superpowers/specs/2026-11-06-bundle-table-access-grants.md` Sec5).
//!
//! ```text
//! bundle --platform-table.write--> executor --host-call(db, "platform_table.write")-->
//!   svc_process::capabilities::handle_platform_table
//!     1. gate.authorize(PlatformTable{write,table,column}) for EVERY column
//!        (approved grant in THIS (tenant, community, app) scope, quota/rate)
//!     2. flag + wiring check (fail loud)
//!     3. PlatformTableStore::write   <-- THIS CRATE
//!          allowlist re-check (independent of the gate) -> value validation
//!          -> one transaction: scoped row lock -> UPDATE -> audit row
//! ```
//!
//! # Scope derivation
//!
//! The guest supplies only the target user UUID, column names and values.
//! `tenant_id`, `community_id`, `app_id` and `app_version` come from the
//! host-built [`PlatformTableScope`]. Every statement is built from compile-time
//! constants (identifiers are mapped from the guest's string to a `&'static str`
//! out of the allowlist -- never interpolated), values are bound parameters, and
//! every statement filters on BOTH `community_id` and `communities.tenant_id`.
//! A row outside that scope is indistinguishable from an absent one
//! ([`PlatformTableError::NotAMember`]).

mod metrics;
mod postgres;

use std::future::Future;
use std::pin::Pin;

use bundle_capability_gate::platform_table::{TableOp, ALLOWLIST};

pub use metrics::record_denied;
pub use postgres::{connect, ConnectConfig, PostgresPlatformTableStore};

/// Boxed future, so [`PlatformTableStore`] is object-safe without `async_trait`.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Most columns one call may name.
pub const MAX_COLUMNS: usize = 8;

/// Role values a bundle may write. `community-owner` / `community-admin` are
/// never assignable (privilege escalation), mirroring hub-api's
/// `VALID_MEMBER_ROLES` minus the admin tier.
pub const ASSIGNABLE_ROLES: &[&str] = &["member", "vip", "moderator"];

/// Rows currently holding one of these roles are never modified by a bundle
/// (a bundle can neither promote to nor demote an owner/admin).
pub const PROTECTED_ROLES: &[&str] = &["community-owner", "community-admin"];

/// The host-derived scope of one call (never guest-supplied).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlatformTableScope {
    pub tenant_id: i32,
    pub community_id: i32,
    pub app_id: String,
    pub app_version: i64,
}

/// One `(column, value)` pair of a write.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Change {
    pub column: String,
    pub value: String,
}

/// Every way a store call can fail; `wire_code` is the stable host-API code.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum PlatformTableError {
    /// Target absent, not an active member of THIS community/tenant, or holding
    /// a protected role (deliberately indistinguishable).
    #[error("target user is not an active, modifiable member of this community")]
    NotAMember,
    /// Table/column/op is not on the allowlist (defense in depth behind the gate).
    #[error("access to {0:?} is not allowlisted")]
    NotAllowlisted(String),
    /// Malformed argument or a value outside the column's permitted set.
    #[error("invalid argument: {0}")]
    Invalid(String),
    /// Database/connection failure.
    #[error("backend error: {0}")]
    Backend(String),
}

impl PlatformTableError {
    /// Stable machine code carried over the host-API wire.
    pub fn wire_code(&self) -> &'static str {
        match self {
            Self::NotAMember => "not_a_member",
            Self::NotAllowlisted(_) => "not_allowlisted",
            Self::Invalid(_) => "invalid_args",
            Self::Backend(_) => "backend",
        }
    }
}

/// Maps a guest-supplied `(table, column)` to the allowlist's own `&'static str`
/// pair for `op`, or `NotAllowlisted`. This is the ONLY way an identifier reaches
/// SQL: a guest string is never interpolated, only compared.
pub fn resolve_column(
    op: TableOp,
    table: &str,
    column: &str,
) -> Result<(&'static str, &'static str), PlatformTableError> {
    ALLOWLIST
        .iter()
        .find(|(t, c, ops)| *t == table && *c == column && ops.contains(&op))
        .map(|(t, c, _)| (*t, *c))
        .ok_or_else(|| {
            metrics::record_denied("not_allowlisted");
            PlatformTableError::NotAllowlisted(format!("{}.{table}.{column}", op.as_str()))
        })
}

/// Validates a read's column list, returning canonical static column names.
pub fn validate_read(
    table: &str,
    columns: &[String],
) -> Result<Vec<&'static str>, PlatformTableError> {
    validate_columns(TableOp::Read, table, columns.iter().map(String::as_str))
}

/// Validates a write's changes: allowlisted columns, no duplicates, valid values.
pub fn validate_write(
    table: &str,
    changes: &[Change],
) -> Result<Vec<(&'static str, String)>, PlatformTableError> {
    let cols = validate_columns(
        TableOp::Write,
        table,
        changes.iter().map(|c| c.column.as_str()),
    )?;
    let mut out = Vec::with_capacity(cols.len());
    for (col, change) in cols.into_iter().zip(changes) {
        match col {
            "role" => {
                if !ASSIGNABLE_ROLES.contains(&change.value.as_str()) {
                    metrics::record_denied("invalid_value");
                    return Err(PlatformTableError::Invalid(format!(
                        "role must be one of {ASSIGNABLE_ROLES:?}"
                    )));
                }
            }
            other => {
                // Allowlisted for write but with no value validator: refuse
                // loudly rather than write an unvalidated value.
                metrics::record_denied("no_validator");
                return Err(PlatformTableError::Invalid(format!(
                    "no value validator for {other}"
                )));
            }
        }
        out.push((col, change.value.clone()));
    }
    Ok(out)
}

fn validate_columns<'a>(
    op: TableOp,
    table: &str,
    columns: impl Iterator<Item = &'a str>,
) -> Result<Vec<&'static str>, PlatformTableError> {
    let mut out: Vec<&'static str> = Vec::new();
    for column in columns {
        let (_, col) = resolve_column(op, table, column)?;
        if out.contains(&col) {
            return Err(PlatformTableError::Invalid(format!(
                "duplicate column {col}"
            )));
        }
        out.push(col);
    }
    if out.is_empty() || out.len() > MAX_COLUMNS {
        return Err(PlatformTableError::Invalid(format!(
            "columns must be 1..={MAX_COLUMNS}"
        )));
    }
    Ok(out)
}

/// The durable, scope-bound platform-table store.
pub trait PlatformTableStore: Send + Sync {
    /// Reads the named allowlisted columns of `user`'s row in `scope`'s community.
    /// Returns a JSON object `{column: value}`.
    fn read<'a>(
        &'a self,
        scope: &'a PlatformTableScope,
        table: &'a str,
        user: uuid::Uuid,
        columns: &'a [String],
    ) -> BoxFuture<'a, Result<serde_json::Value, PlatformTableError>>;

    /// Applies `changes` to `user`'s row in `scope`'s community in one
    /// transaction, writing one audit row per changed column.
    fn write<'a>(
        &'a self,
        scope: &'a PlatformTableScope,
        table: &'a str,
        user: uuid::Uuid,
        changes: &'a [Change],
    ) -> BoxFuture<'a, Result<(), PlatformTableError>>;
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn read_accepts_allowlisted_columns_only() {
        assert_eq!(
            validate_read("community_members", &s(&["role", "is_active"])).unwrap(),
            vec!["role", "is_active"]
        );
        for bad in [
            "display_name",
            "bio",
            "platform_user_id",
            "role; DROP TABLE x",
        ] {
            assert!(matches!(
                validate_read("community_members", &s(&[bad])),
                Err(PlatformTableError::NotAllowlisted(_))
            ));
        }
        assert!(matches!(
            validate_read("hub_users", &s(&["email"])),
            Err(PlatformTableError::NotAllowlisted(_))
        ));
    }

    #[test]
    fn read_rejects_empty_duplicate_and_oversized() {
        assert!(validate_read("community_members", &[]).is_err());
        assert!(validate_read("community_members", &s(&["role", "role"])).is_err());
    }

    #[test]
    fn write_column_outside_write_allowlist_is_denied() {
        let c = [Change {
            column: "is_active".into(),
            value: "false".into(),
        }];
        assert!(matches!(
            validate_write("community_members", &c),
            Err(PlatformTableError::NotAllowlisted(_))
        ));
    }

    #[test]
    fn write_role_value_set_is_enforced() {
        let ok = |v: &str| {
            validate_write(
                "community_members",
                &[Change {
                    column: "role".into(),
                    value: v.into(),
                }],
            )
        };
        for good in ASSIGNABLE_ROLES {
            assert!(ok(good).is_ok(), "{good}");
        }
        for bad in ["community-admin", "community-owner", "admin", "", "Member"] {
            assert!(
                matches!(ok(bad), Err(PlatformTableError::Invalid(_))),
                "{bad}"
            );
        }
    }

    #[test]
    fn wire_codes_are_stable() {
        assert_eq!(PlatformTableError::NotAMember.wire_code(), "not_a_member");
        assert_eq!(
            PlatformTableError::NotAllowlisted("x".into()).wire_code(),
            "not_allowlisted"
        );
    }
}
