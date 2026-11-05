//! `is_mod` / `is_broadcaster` derivation, ported from `normalize.rs`
//! (`compute_discord_permissions`) and the REST parsing in
//! `RestPermissionsResolver::fetch` of the hardcoded path.
//!
//! The lookup itself is a host-mediated `http` call (guarded egress, bot token
//! as a secret-ref); this module owns the pure parts. Any failure resolves to
//! the fail-closed default (`false`/`false`), as the hardcoded resolver does.

use serde_json::Value;

use crate::error::{OverlayError, Result};

/// Discord `ADMINISTRATOR` permission bit.
pub const PERM_ADMINISTRATOR: u64 = 1 << 3;
/// Discord `MANAGE_MESSAGES` permission bit (the "moderator" signal).
pub const PERM_MANAGE_MESSAGES: u64 = 1 << 13;

/// The cross-platform permission signal bundles read from the payload.
/// `Default` (`false`/`false`) is the fail-closed value.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct DiscordPermissions {
    /// `MANAGE_MESSAGES`, or anything implying `is_broadcaster`.
    pub is_mod: bool,
    /// Guild owner or `ADMINISTRATOR`.
    pub is_broadcaster: bool,
}

/// Derives permissions from the guild owner, the member's role ids (including
/// the implicit `@everyone` role == guild id) and the guild role->bitfield map.
#[must_use]
pub fn compute_permissions(
    author_id: &str,
    guild_owner_id: &str,
    member_role_ids: &[String],
    guild_roles: &[(String, u64)],
) -> DiscordPermissions {
    let effective = guild_roles
        .iter()
        .filter(|(id, _)| member_role_ids.iter().any(|r| r == id))
        .fold(0u64, |acc, (_, p)| acc | p);
    let is_owner = !author_id.is_empty() && author_id == guild_owner_id;
    from_bits(effective, is_owner)
}

/// Permissions from an already-effective bitfield (an interaction's
/// `member.permissions` is the resolved total, owners included).
#[must_use]
pub fn from_bits(effective: u64, is_owner: bool) -> DiscordPermissions {
    let is_broadcaster = is_owner || effective & PERM_ADMINISTRATOR != 0;
    let is_mod = is_broadcaster || effective & PERM_MANAGE_MESSAGES != 0;
    DiscordPermissions {
        is_mod,
        is_broadcaster,
    }
}

/// `GET /guilds/{id}` result: owner and every role's permission bitfield.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GuildInfo {
    /// Guild owner's user id.
    pub owner_id: String,
    /// `(role_id, permission bitfield)`; a malformed role fails the whole parse.
    pub roles: Vec<(String, u64)>,
}

/// Parses a `GET /guilds/{id}` body.
///
/// # Errors
/// [`OverlayError::Malformed`] when `owner_id` or `roles` is missing.
pub fn parse_guild(body: &[u8]) -> Result<GuildInfo> {
    let v: Value = serde_json::from_slice(body)
        .map_err(|_| OverlayError::Malformed("guild response not JSON".into()))?;
    let owner_id = v
        .get("owner_id")
        .and_then(Value::as_str)
        .ok_or_else(|| OverlayError::Malformed("guild response missing owner_id".into()))?
        .to_string();
    let mut roles = Vec::new();
    for r in v
        .get("roles")
        .and_then(Value::as_array)
        .ok_or_else(|| OverlayError::Malformed("guild response missing roles".into()))?
    {
        let id = r
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| OverlayError::Malformed("guild role missing id".into()))?;
        // Discord stringifies the bitfield; a role we cannot parse is a typed error
        // (the lookup then fails closed) rather than a silently dropped role.
        let perms: u64 = r
            .get("permissions")
            .and_then(Value::as_str)
            .ok_or_else(|| OverlayError::Malformed("guild role missing permissions".into()))?
            .parse()
            .map_err(|_| OverlayError::Malformed("guild role permissions not a bitfield".into()))?;
        roles.push((id.to_string(), perms));
    }
    Ok(GuildInfo { owner_id, roles })
}

/// Parses a `GET /guilds/{id}/members/{user}` body into role ids, appending the
/// implicit `@everyone` role (id == `guild_id`).
///
/// # Errors
/// [`OverlayError::Malformed`] when `roles` is missing.
pub fn parse_member_roles(body: &[u8], guild_id: &str) -> Result<Vec<String>> {
    let v: Value = serde_json::from_slice(body)
        .map_err(|_| OverlayError::Malformed("member response not JSON".into()))?;
    let mut roles: Vec<String> = v
        .get("roles")
        .and_then(Value::as_array)
        .ok_or_else(|| OverlayError::Malformed("member response missing roles".into()))?
        .iter()
        .filter_map(|r| r.as_str().map(str::to_string))
        .collect();
    roles.push(guild_id.to_string());
    Ok(roles)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| (*x).to_string()).collect()
    }

    #[test]
    fn owner_admin_manage_and_plain() {
        let roles = vec![
            ("adm".to_string(), PERM_ADMINISTRATOR),
            ("mod".to_string(), PERM_MANAGE_MESSAGES),
            ("111".to_string(), 0),
        ];
        let owner = compute_permissions("900", "900", &s(&["111"]), &roles);
        assert_eq!((owner.is_mod, owner.is_broadcaster), (true, true));
        let adm = compute_permissions("1", "900", &s(&["adm", "111"]), &roles);
        assert_eq!((adm.is_mod, adm.is_broadcaster), (true, true));
        let m = compute_permissions("2", "900", &s(&["mod", "111"]), &roles);
        assert_eq!((m.is_mod, m.is_broadcaster), (true, false));
        let plain = compute_permissions("3", "900", &s(&["111"]), &roles);
        assert_eq!(plain, DiscordPermissions::default());
        // Empty author id never matches an (also empty) owner id.
        assert!(!compute_permissions("", "", &[], &[]).is_broadcaster);
    }

    #[test]
    fn from_bits_matches() {
        assert_eq!(from_bits(0, false), DiscordPermissions::default());
        assert!(from_bits(PERM_MANAGE_MESSAGES, false).is_mod);
        assert!(!from_bits(PERM_MANAGE_MESSAGES, false).is_broadcaster);
        assert!(from_bits(PERM_ADMINISTRATOR, false).is_broadcaster);
        assert!(from_bits(0, true).is_mod);
    }

    #[test]
    fn parses_guild_and_member() {
        let g = parse_guild(
            br#"{"owner_id":"900","roles":[{"id":"a","permissions":"8"},{"id":"b","permissions":"0"}]}"#,
        )
        .unwrap();
        assert_eq!(g.owner_id, "900");
        assert_eq!(g.roles, vec![("a".to_string(), 8), ("b".to_string(), 0)]);
        assert_eq!(
            parse_member_roles(br#"{"roles":["r1","r2"]}"#, "111").unwrap(),
            s(&["r1", "r2", "111"])
        );
    }

    #[test]
    fn malformed_bodies_fail_loud() {
        assert!(parse_guild(b"x").is_err());
        assert!(parse_guild(br#"{"roles":[]}"#).is_err());
        assert!(parse_guild(br#"{"owner_id":"1"}"#).is_err());
        // A malformed role fails the parse (no silently dropped role).
        for roles in [
            r#"[{"id":"b","permissions":"x"}]"#,
            r#"[{"id":"c"}]"#,
            r#"[{"permissions":"8"}]"#,
            r#"[{"id":"d","permissions":8}]"#,
        ] {
            let body = format!(r#"{{"owner_id":"1","roles":{roles}}}"#);
            assert!(parse_guild(body.as_bytes()).is_err(), "{roles}");
        }
        assert!(parse_member_roles(b"x", "1").is_err());
        assert!(parse_member_roles(br#"{}"#, "1").is_err());
    }
}
