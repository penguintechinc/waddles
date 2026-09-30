//! Server-derived scope and identifier safety for the bundle `db`
//! capability.
//!
//! [`DbScope`] is built exclusively from the invocation's already-validated
//! `(tenant, community, app_id)` -- the same triple `StageCapabilities`
//! already carries in both `core/svc_process` and `core/svc_action`, itself
//! sourced from the JWT/manifest-driven invoke scope, never from a bundle
//! host-call's own `args`. This module never accepts a tenant/community/
//! app_id from guest-controlled input.
//!
//! [`validate_identifier`] is the second, independent layer of defense the
//! design doc requires (SS3.4/SS6.2): every schema/table/column name this
//! crate ever puts into SQL text comes from [`crate::schema::TableSchema`],
//! which is itself only ever populated from host-resolved manifest
//! metadata (never a live host-call argument) -- but every identifier is
//! *also* re-validated here, immediately before use, so a bug that let an
//! unvalidated string reach [`crate::backend`] fails safe rather than
//! silently building unsafe SQL.

/// Postgres's own `NAMEDATALEN` limit (63 bytes, one less for the null
/// terminator) -- an identifier at or above this length is truncated
/// server-side anyway; this crate never accepts one that would be.
pub const MAX_IDENTIFIER_LEN: usize = 63;

/// One rejected identifier -- schema, table, or column name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IdentifierError {
    Empty,
    TooLong(usize),
    InvalidChars,
}

/// `^[a-z][a-z0-9_]{0,62}$` -- the same allowlist the design doc's DDL
/// generator (`hub_api/services/bundle_data_schema.py`, PR #430) enforces
/// at manifest-approval time. Re-checked here, host-side, immediately
/// before any identifier is interpolated into a SQL template: defense in
/// depth against a bug elsewhere ever letting an unsanitized string reach
/// this crate, never the primary control.
pub fn validate_identifier(name: &str) -> Result<(), IdentifierError> {
    if name.is_empty() {
        return Err(IdentifierError::Empty);
    }
    if name.len() > MAX_IDENTIFIER_LEN {
        return Err(IdentifierError::TooLong(name.len()));
    }
    let mut chars = name.chars();
    let first = chars.next().expect("checked non-empty above");
    if !first.is_ascii_lowercase() {
        return Err(IdentifierError::InvalidChars);
    }
    if !chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_') {
        return Err(IdentifierError::InvalidChars);
    }
    Ok(())
}

/// Quotes an already-[`validate_identifier`]-checked identifier for use in
/// SQL text (`"schema"."table"` style). Doubling any embedded `"` is
/// defense in depth only -- [`validate_identifier`]'s charset already
/// excludes `"` entirely, so this can never actually fire for an
/// identifier that passed validation, but the escape is applied
/// unconditionally rather than assumed safe.
pub fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

/// The two schemas a bundle table may ever live in (design doc SS2) --
/// chosen server-side at catalog approval from the bundle's provider
/// namespace, never from the manifest's own claim.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AppSchema {
    Core,
    Community,
}

impl AppSchema {
    pub fn as_str(self) -> &'static str {
        match self {
            AppSchema::Core => "app_core",
            AppSchema::Community => "app_community",
        }
    }
}

/// The authenticated, server-derived scope one `db` host-call is answered
/// under. Every field here is trusted input by the time it reaches this
/// struct -- `tenant`/`community`/`app_id` come from the invocation's own
/// validated scope, exactly like every other capability in both stages.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct DbScope {
    pub tenant: String,
    pub community: Option<String>,
    pub app_id: String,
}

impl DbScope {
    pub fn new(
        tenant: impl Into<String>,
        community: Option<String>,
        app_id: impl Into<String>,
    ) -> Self {
        Self {
            tenant: tenant.into(),
            community,
            app_id: app_id.into(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_identifier_accepts_the_allowed_shape() {
        assert!(validate_identifier("fishing_core").is_ok());
        assert!(validate_identifier("a").is_ok());
        assert!(validate_identifier("waddles_core_quotes_1").is_ok());
    }

    #[test]
    fn validate_identifier_rejects_empty() {
        assert_eq!(validate_identifier(""), Err(IdentifierError::Empty));
    }

    #[test]
    fn validate_identifier_rejects_over_namedatalen() {
        let long = "a".repeat(MAX_IDENTIFIER_LEN + 1);
        assert_eq!(
            validate_identifier(&long),
            Err(IdentifierError::TooLong(MAX_IDENTIFIER_LEN + 1))
        );
    }

    #[test]
    fn validate_identifier_rejects_leading_digit_or_underscore() {
        assert_eq!(
            validate_identifier("1table"),
            Err(IdentifierError::InvalidChars)
        );
        assert_eq!(
            validate_identifier("_table"),
            Err(IdentifierError::InvalidChars)
        );
    }

    #[test]
    fn validate_identifier_rejects_uppercase() {
        assert_eq!(
            validate_identifier("Fishing_Core"),
            Err(IdentifierError::InvalidChars)
        );
    }

    #[test]
    fn validate_identifier_rejects_sql_injection_shaped_input() {
        for bad in [
            "table\"; DROP TABLE users; --",
            "table;drop",
            "table' OR '1'='1",
            "table.other",
            "table-name",
            "table name",
        ] {
            assert_eq!(
                validate_identifier(bad),
                Err(IdentifierError::InvalidChars),
                "expected {bad:?} to be rejected"
            );
        }
    }

    #[test]
    fn quote_ident_wraps_and_escapes() {
        assert_eq!(quote_ident("fishing_core"), "\"fishing_core\"");
        // Defense-in-depth path only -- never reachable for a string that
        // actually passed validate_identifier, exercised directly here.
        assert_eq!(quote_ident("weird\"name"), "\"weird\"\"name\"");
    }

    #[test]
    fn app_schema_renders_the_two_known_schema_names() {
        assert_eq!(AppSchema::Core.as_str(), "app_core");
        assert_eq!(AppSchema::Community.as_str(), "app_community");
    }
}
