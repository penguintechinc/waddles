//! The tool-relevant slice of a bundle's approved manifest (bundle-MCP-tools
//! P1 -> P3b): whether it is a trusted `service` that exports tools, which
//! tools it exports (with their read/write access), and which tools it
//! requests from others.
//!
//! Sourced from the install-time consent summary
//! (`app_install_approvals.summary_json`, written by hub-api's
//! `permission_summary_service.build_permission_summary` at approval time --
//! the same blob `declared_capabilities` comes from): `capabilities` carries
//! `mcp.tools` ONLY for a trusted-source `service` bundle that exports tools
//! (`bundle_approval_service._derive_capabilities`), `exportedTools` lists
//! `{name, access, risk}` and `requestedTools` lists `{target, tool, access}`.
//! A missing / malformed shape parses to the empty (`Default`) value, which
//! the invocation gate treats as "not a service, exports nothing, requests
//! nothing" -- every tool call then fails closed.

/// The derived capability marking a trusted service bundle that exports tools
/// (mirrors `bundle_approval_service.MCP_TOOLS_CAPABILITY`).
pub const MCP_TOOLS_CAPABILITY: &str = "mcp.tools";

/// One tool a service bundle exports.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExportedToolInfo {
    pub name: String,
    /// `true` for `access: write`; `false` for `access: read`.
    pub write: bool,
}

/// One tool a bundle requests from another bundle.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestedToolInfo {
    pub target: String,
    pub tool: String,
    /// `true` for `access: write`; `false` for `access: read`.
    pub write: bool,
}

/// `Some(true)` for `access: write`, `Some(false)` for `access: read`,
/// `None` for anything else (the entry is then dropped, fail closed).
fn access_is_write(entry: &serde_json::Value) -> Option<bool> {
    match entry.get("access").and_then(|v| v.as_str()) {
        Some("write") => Some(true),
        Some("read") => Some(false),
        _ => None,
    }
}

/// Parses the `key` array of a summary into tool infos. Fails closed (fewer
/// facts, never more) but LOUD: a present-but-non-array value, or any entry
/// dropped as malformed, is logged -- never silently swallowed. An absent
/// key is the normal "declares none" case and logs nothing.
fn parse_tool_list<T>(
    summary: &serde_json::Value,
    key: &str,
    parse: impl Fn(&serde_json::Value) -> Option<T>,
) -> Vec<T> {
    let Some(raw) = summary.get(key) else {
        return Vec::new();
    };
    let Some(list) = raw.as_array() else {
        tracing::warn!(
            key,
            "approval summary tool list is not an array; treating as empty (fail closed)"
        );
        return Vec::new();
    };
    let parsed: Vec<T> = list.iter().filter_map(parse).collect();
    if parsed.len() != list.len() {
        tracing::warn!(
            key,
            declared = list.len(),
            kept = parsed.len(),
            "approval summary contained malformed tool entries; dropped (fail closed)"
        );
    }
    parsed
}

/// The parsed tool facts of one approved bundle version -- see the module doc.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ToolManifestInfo {
    /// `true` only for a trusted-source `service` bundle that exports tools.
    pub is_tool_service: bool,
    pub exported: Vec<ExportedToolInfo>,
    pub requested: Vec<RequestedToolInfo>,
}

impl ToolManifestInfo {
    /// Parses the tool facts out of an approval `summary_json`; any missing or
    /// malformed part is dropped (fail closed -- fewer facts, never more).
    pub fn from_summary(summary: &serde_json::Value) -> Self {
        let is_tool_service = summary
            .get("capabilities")
            .and_then(|v| v.as_array())
            .is_some_and(|caps| {
                caps.iter()
                    .any(|c| c.as_str() == Some(MCP_TOOLS_CAPABILITY))
            });
        let exported = if is_tool_service {
            parse_tool_list(summary, "exportedTools", |t| {
                Some(ExportedToolInfo {
                    name: t.get("name")?.as_str()?.to_string(),
                    write: access_is_write(t)?,
                })
            })
        } else {
            Vec::new()
        };
        let requested = parse_tool_list(summary, "requestedTools", |t| {
            Some(RequestedToolInfo {
                target: t.get("target")?.as_str()?.to_string(),
                tool: t.get("tool")?.as_str()?.to_string(),
                write: access_is_write(t)?,
            })
        });
        Self {
            is_tool_service,
            exported,
            requested,
        }
    }

    /// The exported tool named `name`, if this bundle is a tool service that
    /// exports it.
    pub fn exported_tool(&self, name: &str) -> Option<&ExportedToolInfo> {
        if !self.is_tool_service {
            return None;
        }
        self.exported.iter().find(|t| t.name == name)
    }

    /// Whether this bundle's approved manifest requests `tool` from `target`.
    pub fn requests(&self, target: &str, tool: &str) -> bool {
        self.requested
            .iter()
            .any(|r| r.target == target && r.tool == tool)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn service_summary_parses_exports_and_requests() {
        let s = json!({
            "capabilities": ["kv", "mcp.tools"],
            "exportedTools": [{"name": "get_balance", "access": "read", "risk": "normal"},
                              {"name": "adjust", "access": "write", "risk": "dangerous"}],
            "requestedTools": [{"target": "svc.b", "tool": "x", "access": "read"}],
        });
        let info = ToolManifestInfo::from_summary(&s);
        assert!(info.is_tool_service);
        assert_eq!(info.exported_tool("adjust").map(|t| t.write), Some(true));
        assert_eq!(
            info.exported_tool("get_balance").map(|t| t.write),
            Some(false)
        );
        assert!(info.requests("svc.b", "x"));
        assert!(!info.requests("svc.b", "y"));
    }

    #[test]
    fn exports_without_the_service_capability_are_ignored() {
        let s = json!({
            "capabilities": ["kv"],
            "exportedTools": [{"name": "adjust", "access": "write", "risk": "normal"}],
        });
        let info = ToolManifestInfo::from_summary(&s);
        assert!(!info.is_tool_service);
        assert!(info.exported.is_empty());
        assert!(info.exported_tool("adjust").is_none());
    }

    #[test]
    fn malformed_shapes_parse_to_empty() {
        for s in [
            json!(null),
            json!({"capabilities": "mcp.tools"}),
            json!({"capabilities": ["mcp.tools"], "exportedTools": [{"name": 1, "access": "read"}]}),
            json!({"capabilities": ["mcp.tools"], "exportedTools": [{"name": "t", "access": "rw"}]}),
        ] {
            let info = ToolManifestInfo::from_summary(&s);
            assert!(info.exported.is_empty(), "{s}");
            assert!(info.requested.is_empty(), "{s}");
        }
    }
}
