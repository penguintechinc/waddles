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
    /// The host-verified actor scope a caller must hold (from the manifest's
    /// `required_scope`, summary key `requiredScope`); `None` = unrestricted.
    pub required_scope: Option<String>,
    /// How the HOST executes this tool (summary key `hostOp`): present only on a platform
    /// service bundle's tools whose execution is a pinned producer op rather than a guest
    /// call. `None` = an ordinary guest-served tool.
    pub host_op: Option<HostOpDecl>,
}

/// Where a host-executed tool's pinned destination comes from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HostOpOrigin {
    /// The verified origin channel of the triggering event (must be a `platform` event).
    Channel,
    /// The community's own guild scope (channel when the event has one, else the source
    /// guild / external-platform allowlist) -- Discord role ops.
    GuildScope,
    /// No destination (community-wide op).
    None,
}

/// How a user-valued tool argument reaches the producer op's `user_id`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HostOpUserWire {
    /// Detokenized host-side (hub-api `ResolvePlatformUser`) to the platform numeric id
    /// BEFORE queueing -- the producer envelope carries the raw id the platform API needs.
    PlatformId,
    /// Kept as the `{user:<uuid>}` placeholder; the consumer (svc_ingest) resolves it.
    Placeholder,
}

/// A platform tool's host-side execution declaration (manifest `exported_tools[].host_op`):
/// tool name -> registered producer op + the argument mapping. Everything not named here is
/// refused, so a caller can never smuggle a guild/channel/credential/id argument through.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostOpDecl {
    /// Platform queue the op rides (`twitch`/`discord`/`youtube`/`kick`).
    pub platform: String,
    /// The registered op kind (platform op table), e.g. `mod.ban`.
    pub op: String,
    /// Tool argument holding the tokenized user UUID -> wire `user_id`.
    pub user: Option<String>,
    pub user_wire: HostOpUserWire,
    /// Tool argument -> wire `message_id`.
    pub message_id: Option<String>,
    /// Tool argument (string) -> wire `text`.
    pub text: Option<String>,
    /// `json key -> tool argument`: wire `text` becomes a JSON object of the present args
    /// (mutually exclusive with `text`).
    pub text_object: Vec<(String, String)>,
    /// Tool argument (array of strings) -> wire `text`, sorted, de-duplicated and comma-joined
    /// (1..=16 entries). Mutually exclusive with `text` / `text_object`.
    pub text_list: Option<String>,
    /// Tool arguments that must be present.
    pub required: Vec<String>,
    pub origin: HostOpOrigin,
    /// Keys of the drain's result (`data` object, or the top-level `id`) returned to the
    /// caller as the platform response subset; empty = a bare `{ok}`.
    pub returns: Vec<String>,
}

fn is_arg_name(s: &str) -> bool {
    let mut b = s.bytes();
    matches!(b.next(), Some(c) if c.is_ascii_lowercase())
        && s.len() <= 64
        && b.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_')
}

/// `Ok(None)` when absent/null, `Ok(Some(name))` for a valid argument name, `Err` otherwise.
fn opt_arg(v: &serde_json::Value, key: &str) -> Result<Option<String>, ()> {
    match v.get(key) {
        None | Some(serde_json::Value::Null) => Ok(None),
        Some(serde_json::Value::String(s)) if is_arg_name(s) => Ok(Some(s.clone())),
        Some(_) => Err(()),
    }
}

impl HostOpDecl {
    /// Parses a summary `hostOp` object. Any unknown key or malformed value is `None` (the
    /// whole tool entry is then dropped by the caller -- fail closed, never a partial decl).
    #[must_use]
    pub fn from_summary(v: &serde_json::Value) -> Option<Self> {
        const KEYS: [&str; 11] = [
            "platform",
            "op",
            "user",
            "userWire",
            "messageId",
            "text",
            "textObject",
            "textList",
            "required",
            "origin",
            "returns",
        ];
        let obj = v.as_object()?;
        if obj.keys().any(|k| !KEYS.contains(&k.as_str())) {
            return None;
        }
        let platform = v.get("platform")?.as_str()?.to_string();
        let op = v.get("op")?.as_str()?.to_string();
        if platform.is_empty() || op.is_empty() {
            return None;
        }
        let user_wire = match v.get("userWire").and_then(|x| x.as_str()) {
            None | Some("platform_id") => HostOpUserWire::PlatformId,
            Some("placeholder") => HostOpUserWire::Placeholder,
            Some(_) => return None,
        };
        let origin = match v.get("origin").and_then(|x| x.as_str()) {
            None | Some("channel") => HostOpOrigin::Channel,
            Some("guild_scope") => HostOpOrigin::GuildScope,
            Some("none") => HostOpOrigin::None,
            Some(_) => return None,
        };
        let names = |key: &str| -> Option<Vec<String>> {
            match v.get(key) {
                None => Some(Vec::new()),
                Some(serde_json::Value::Array(a)) => a
                    .iter()
                    .map(|x| x.as_str().filter(|s| is_arg_name(s)).map(str::to_string))
                    .collect(),
                Some(_) => None,
            }
        };
        let text_object = match v.get("textObject") {
            None => Vec::new(),
            Some(serde_json::Value::Object(m)) => {
                let mut out = Vec::new();
                for (k, a) in m {
                    if !is_arg_name(k) {
                        return None;
                    }
                    out.push((
                        k.clone(),
                        a.as_str().filter(|s| is_arg_name(s))?.to_string(),
                    ));
                }
                out.sort();
                out
            }
            Some(_) => return None,
        };
        let text = opt_arg(v, "text").ok()?;
        let text_list = opt_arg(v, "textList").ok()?;
        let text_forms = usize::from(text.is_some())
            + usize::from(!text_object.is_empty())
            + usize::from(text_list.is_some());
        if text_forms > 1 {
            return None;
        }
        Some(Self {
            platform,
            op,
            user: opt_arg(v, "user").ok()?,
            user_wire,
            message_id: opt_arg(v, "messageId").ok()?,
            text,
            text_object,
            text_list,
            required: names("required")?,
            origin,
            returns: names("returns")?,
        })
    }
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
                // A present-but-malformed `requiredScope` drops the whole entry
                // (fail closed): never silently downgrade to "unrestricted".
                let required_scope = match t.get("requiredScope") {
                    None | Some(serde_json::Value::Null) => None,
                    Some(serde_json::Value::String(s)) if !s.is_empty() => Some(s.clone()),
                    Some(_) => return None,
                };
                // Same rule for `hostOp`: present-but-malformed drops the entry.
                let host_op = match t.get("hostOp") {
                    None | Some(serde_json::Value::Null) => None,
                    Some(v) => Some(HostOpDecl::from_summary(v)?),
                };
                Some(ExportedToolInfo {
                    name: t.get("name")?.as_str()?.to_string(),
                    write: access_is_write(t)?,
                    required_scope,
                    host_op,
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
    fn host_op_declarations_parse_and_malformed_ones_drop_the_tool() {
        let s = json!({
            "capabilities": ["mcp.tools"],
            "exportedTools": [{
                "name": "twitch.moderation.ban", "access": "write", "risk": "dangerous",
                "hostOp": {"platform": "twitch", "op": "mod.ban", "user": "user",
                           "textObject": {"reason": "reason"}, "required": ["user"],
                           "returns": ["id"]}
            }],
        });
        let info = ToolManifestInfo::from_summary(&s);
        let d = info
            .exported_tool("twitch.moderation.ban")
            .and_then(|t| t.host_op.clone())
            .unwrap_or_else(|| panic!("decl parsed"));
        assert_eq!((d.platform.as_str(), d.op.as_str()), ("twitch", "mod.ban"));
        assert_eq!(d.user_wire, HostOpUserWire::PlatformId);
        assert_eq!(d.origin, HostOpOrigin::Channel);
        assert_eq!(
            d.text_object,
            vec![("reason".to_string(), "reason".to_string())]
        );
        for bad in [
            json!({"platform": "twitch"}),
            json!({"platform": "twitch", "op": "mod.ban", "extra": 1}),
            json!({"platform": "twitch", "op": "mod.ban", "userWire": "raw"}),
            json!({"platform": "twitch", "op": "mod.ban", "origin": "anywhere"}),
            json!({"platform": "twitch", "op": "mod.ban", "user": "User"}),
            json!({"platform": "twitch", "op": "mod.ban", "text": "a", "textObject": {"k": "a"}}),
            json!({"platform": "twitch", "op": "mod.ban", "required": "user"}),
            json!("mod.ban"),
        ] {
            let s = json!({
                "capabilities": ["mcp.tools"],
                "exportedTools": [{"name": "t", "access": "write", "risk": "dangerous", "hostOp": bad}],
            });
            assert!(
                ToolManifestInfo::from_summary(&s).exported.is_empty(),
                "{bad} must drop the entry (fail closed)"
            );
        }
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
