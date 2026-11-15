//! Contract pin against the REAL hub-api gRPC contract: the `.proto` and the Python servicer
//! source. Method names, scopes, request fields and the resolver status vocabulary this crate
//! speaks must all appear there. Breaks if either side renames one.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bundle_host_media::credentials::{BYOK_RESOLVE_METHOD, BYOK_SCOPE};
use bundle_host_media::tier::{EFFECTIVE_TIER_METHOD, TIER_SCOPE};

const PROTO: &str = include_str!("../../../proto/waddles/hub/internal/v1/media.proto");
const SERVICER: &str = include_str!("../../../hub_api/grpc_internal/dataplane_servicers.py");

#[test]
fn rust_client_matches_the_hub_api_grpc_contract() {
    assert!(PROTO.contains("package waddles.hub.internal.v1;"));
    assert!(PROTO.contains("service MediaService {"));
    for (full, rpc) in [
        (EFFECTIVE_TIER_METHOD, "GetEffectiveTier"),
        (BYOK_RESOLVE_METHOD, "ResolveByok"),
    ] {
        assert_eq!(full, format!("/waddles.hub.internal.v1.MediaService/{rpc}"));
        assert!(PROTO.contains(&format!("rpc {rpc}(")), "{rpc}");
        // The servicer registers the required scope under exactly this method name.
        assert!(
            SERVICER.contains(&format!("_MEDIA + \"{rpc}\"")),
            "servicer scope table has {rpc}"
        );
    }
    assert!(
        SERVICER.contains(&format!(
            "SCOPE_MEDIA_TIER_RESOLVE: Final = \"{TIER_SCOPE}\""
        )),
        "tier scope"
    );
    assert!(
        SERVICER.contains(&format!(
            "SCOPE_MEDIA_BYOK_RESOLVE: Final = \"{BYOK_SCOPE}\""
        )),
        "byok scope"
    );
    // Response vocabulary the Rust mapper switches on.
    for variant in [
        "BYOK_STATUS_OK",
        "BYOK_STATUS_NOT_CONFIGURED",
        "BYOK_STATUS_TIER_REQUIRED",
        "MEDIA_TIER_FREE",
        "MEDIA_TIER_PROFESSIONAL",
        "MEDIA_TIER_ENTERPRISE",
    ] {
        assert!(PROTO.contains(variant), "{variant}");
    }
    for field in ["tenant_id", "community_id", "provider", "api_key", "tier"] {
        assert!(PROTO.contains(field), "{field}");
    }
}
