//! `platform-table`: the `world stage-next` import for allowlisted platform-table
//! access (spec `docs/superpowers/specs/2026-11-06-bundle-table-access-grants.md`).
//! Cloned from the recipe in [`super::stage_next_identity`].
//!
//! Every call rides the same [`imports::call`](crate::host::imports::call) round
//! trip, carried as `capability = db`, `op = "platform_table.read"|"platform_table.write"`.
//! Nothing here decides authority: the executor encodes the call, decodes the
//! answer and maps refusals onto the WIT `platform-table.error` variant. The
//! tenant/community scope is NEVER an argument -- the stage derives it host-side.

use penguin_bundle_host::wire::CapabilityKind;

use crate::engine::stage_next_world::waddle::bundle::platform_table;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

/// Longest table / column name the executor will forward (real ones are < 64).
const MAX_NAME_LEN: usize = 64;
/// Most columns one call may carry (the store enforces its own bound too).
const MAX_COLUMNS: usize = 8;

/// Maps a stage-side error code onto the WIT `platform-table.error` variant.
fn error_from(err: ExecutorError) -> platform_table::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "not_a_member" => platform_table::Error::NotAMember,
            "not_allowlisted" => platform_table::Error::NotAllowlisted(message.clone()),
            "invalid_args" => platform_table::Error::Invalid(message.clone()),
            "not_implemented" | "feature_disabled" | "unavailable" => {
                platform_table::Error::Unavailable(message.clone())
            }
            "backend" => platform_table::Error::Backend(message.clone()),
            other => platform_table::Error::Denied(other.to_string()),
        },
        other => platform_table::Error::Backend(other.to_string()),
    }
}

fn validate_name(what: &str, name: &str) -> Result<(), platform_table::Error> {
    if name.is_empty() || name.len() > MAX_NAME_LEN {
        return Err(platform_table::Error::Invalid(format!(
            "{what} must be 1..={MAX_NAME_LEN} bytes"
        )));
    }
    Ok(())
}

/// Renders one JSON cell as the WIT text value (booleans `true`/`false`, null
/// as the empty string is refused -- a null is surfaced as a loud `backend`).
fn cell_text(column: &str, value: &serde_json::Value) -> Result<String, platform_table::Error> {
    match value {
        serde_json::Value::String(s) => Ok(s.clone()),
        serde_json::Value::Bool(b) => Ok(b.to_string()),
        _ => Err(platform_table::Error::Backend(format!(
            "malformed host-result: column {column} is not text/bool"
        ))),
    }
}

impl platform_table::Host for ExecState {
    async fn read(
        &mut self,
        table: String,
        user: String,
        columns: Vec<String>,
    ) -> Result<Vec<platform_table::ColumnValue>, platform_table::Error> {
        validate_name("table", &table)?;
        if columns.is_empty() || columns.len() > MAX_COLUMNS {
            return Err(platform_table::Error::Invalid(format!(
                "columns must be 1..={MAX_COLUMNS}"
            )));
        }
        for c in &columns {
            validate_name("column", c)?;
        }
        let args = serde_json::json!({ "table": table, "user": user, "columns": columns });
        let value = call(self, CapabilityKind::Db, "platform_table.read", args)
            .await
            .map_err(error_from)?;
        let object = value
            .get("columns")
            .and_then(|v| v.as_object())
            .ok_or_else(|| {
                platform_table::Error::Backend(
                    "malformed host-result: no columns object".to_string(),
                )
            })?;
        // Preserve the request order (the host map is unordered).
        columns
            .iter()
            .map(|c| {
                let v = object.get(c).ok_or_else(|| {
                    platform_table::Error::Backend(format!(
                        "malformed host-result: missing column {c}"
                    ))
                })?;
                Ok(platform_table::ColumnValue {
                    column: c.clone(),
                    value: cell_text(c, v)?,
                })
            })
            .collect()
    }

    async fn write(
        &mut self,
        table: String,
        user: String,
        changes: Vec<platform_table::ColumnValue>,
    ) -> Result<(), platform_table::Error> {
        validate_name("table", &table)?;
        if changes.is_empty() || changes.len() > MAX_COLUMNS {
            return Err(platform_table::Error::Invalid(format!(
                "changes must be 1..={MAX_COLUMNS}"
            )));
        }
        for c in &changes {
            validate_name("column", &c.column)?;
        }
        let changes: Vec<serde_json::Value> = changes
            .into_iter()
            .map(|c| serde_json::json!({ "column": c.column, "value": c.value }))
            .collect();
        let args = serde_json::json!({ "table": table, "user": user, "changes": changes });
        call(self, CapabilityKind::Db, "platform_table.write", args)
            .await
            .map(|_| ())
            .map_err(error_from)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    fn denied(code: &str) -> ExecutorError {
        ExecutorError::HostCallDenied {
            capability: "db",
            op: "platform_table.write",
            code: code.to_string(),
            message: "m".to_string(),
        }
    }

    #[test]
    fn error_codes_map_onto_the_wit_variant() {
        assert!(matches!(
            error_from(denied("not_a_member")),
            platform_table::Error::NotAMember
        ));
        assert!(matches!(
            error_from(denied("not_allowlisted")),
            platform_table::Error::NotAllowlisted(_)
        ));
        assert!(matches!(
            error_from(denied("invalid_args")),
            platform_table::Error::Invalid(_)
        ));
        assert!(matches!(
            error_from(denied("feature_disabled")),
            platform_table::Error::Unavailable(_)
        ));
        assert!(matches!(
            error_from(denied("backend")),
            platform_table::Error::Backend(_)
        ));
        assert!(matches!(
            error_from(denied("not_granted")),
            platform_table::Error::Denied(c) if c == "not_granted"
        ));
    }

    #[test]
    fn cell_text_renders_text_and_bool_and_refuses_the_rest() {
        assert_eq!(cell_text("role", &serde_json::json!("vip")).unwrap(), "vip");
        assert_eq!(
            cell_text("is_active", &serde_json::json!(true)).unwrap(),
            "true"
        );
        assert!(cell_text("role", &serde_json::Value::Null).is_err());
        assert!(cell_text("role", &serde_json::json!(5)).is_err());
    }

    #[test]
    fn names_are_bounded() {
        assert!(validate_name("table", "").is_err());
        assert!(validate_name("table", &"x".repeat(65)).is_err());
        assert!(validate_name("table", "community_members").is_ok());
    }
}
