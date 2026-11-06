//! Platform-table access allowlist (spec
//! `docs/superpowers/specs/2026-11-06-bundle-table-access-grants.md`).
//!
//! The closed set of `(table, column, op)` triples a bundle may be granted via
//! `platform.table.<op>:<table>.<column>`. Mirrored in
//! `hub_api/services/bundle_table_access.py`; both are parity-tested against
//! `tests/fixtures/platform_table_allowlist.json`. Widening it is a code change.

/// Operation a table-access grant authorises.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum TableOp {
    Read,
    Write,
}

impl TableOp {
    /// The id-segment spelling (`read` / `write`).
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Read => "read",
            Self::Write => "write",
        }
    }
}

/// `(table, column, ops)` rows. Deliberately narrow: never an auth/credential/PII
/// identity table or PII column.
pub const ALLOWLIST: &[(&str, &str, &[TableOp])] = &[
    (
        "community_members",
        "role",
        &[TableOp::Read, TableOp::Write],
    ),
    ("community_members", "is_active", &[TableOp::Read]),
    ("community_members", "joined_at", &[TableOp::Read]),
];

/// `true` iff `(op, table, column)` is on the allowlist.
pub fn is_allowed(op: TableOp, table: &str, column: &str) -> bool {
    ALLOWLIST
        .iter()
        .any(|(t, c, ops)| *t == table && *c == column && ops.contains(&op))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allowlist_matches_parity_fixture() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../tests/fixtures/platform_table_allowlist.json"
        ))
        .unwrap();
        let mut from_fixture: Vec<(String, String, Vec<String>)> = Vec::new();
        for (table, cols) in fixture.as_object().unwrap() {
            for (col, ops) in cols.as_object().unwrap() {
                let mut ops: Vec<String> = ops
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|o| o.as_str().unwrap().to_string())
                    .collect();
                ops.sort();
                from_fixture.push((table.clone(), col.clone(), ops));
            }
        }
        let mut from_code: Vec<(String, String, Vec<String>)> = ALLOWLIST
            .iter()
            .map(|(t, c, ops)| {
                let mut ops: Vec<String> = ops.iter().map(|o| o.as_str().to_string()).collect();
                ops.sort();
                (t.to_string(), c.to_string(), ops)
            })
            .collect();
        from_fixture.sort();
        from_code.sort();
        assert_eq!(from_code, from_fixture);
    }

    #[test]
    fn pii_and_identity_columns_are_never_allowed() {
        for col in [
            "display_name",
            "avatar_url",
            "bio",
            "social_links",
            "platform_user_id",
            "user_id",
        ] {
            assert!(
                !is_allowed(TableOp::Read, "community_members", col),
                "{col}"
            );
            assert!(
                !is_allowed(TableOp::Write, "community_members", col),
                "{col}"
            );
        }
        assert!(!is_allowed(TableOp::Read, "hub_users", "email"));
        assert!(!is_allowed(
            TableOp::Write,
            "community_members",
            "is_active"
        ));
    }
}
