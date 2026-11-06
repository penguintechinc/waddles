//! Contract pin against the REAL hub-api blueprint source (merged from
//! `feature/harness-media-hubapi`, #1070): paths, scopes, request fields and the
//! resolver status vocabulary this crate speaks must all appear there. Breaks if
//! either side renames one.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bundle_host_media::credentials::{BYOK_RESOLVE_PATH, BYOK_SCOPE};
use bundle_host_media::tier::{EFFECTIVE_TIER_PATH, TIER_SCOPE};

const HUB: &str = include_str!("../../../hub_api/blueprints/v1/media_byok.py");

#[test]
fn rust_client_matches_the_hub_api_blueprint() {
    assert!(HUB.contains(r#"url_prefix="/api/v1/internal/media""#));
    let prefix = "/api/v1/internal/media";
    for (full, route) in [
        (EFFECTIVE_TIER_PATH, "/effective-tier"),
        (BYOK_RESOLVE_PATH, "/byok/resolve"),
    ] {
        assert_eq!(full, format!("{prefix}{route}"));
        assert!(
            HUB.contains(&format!(r#"route("{route}", methods=["POST"])"#)),
            "{route}"
        );
    }
    assert!(HUB.contains(&format!(r#""{TIER_SCOPE}""#)), "tier scope");
    assert!(HUB.contains(&format!(r#""{BYOK_SCOPE}""#)), "byok scope");
    // Response vocabulary the Rust mapper switches on.
    assert!(HUB.contains("`status` is `ok` (api_key set) | `not_configured` | `tier_required`."));
    for field in ["tenant_id", "community_id", "provider", "api_key", "tier"] {
        assert!(HUB.contains(field), "{field}");
    }
}
