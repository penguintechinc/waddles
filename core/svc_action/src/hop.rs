//! Hop verification (spec §5.11, D30): `binding.mac` recompute-and-verify
//! plus the tenant/community-vs-stream-key check, run on every entry this
//! stage reads before any other processing (including invoking the
//! executor) -- spec §16 M3 row: "`binding.mac` and tenant/community/grant/
//! approval verification (§5.11) run before every dispatch; outbound
//! credential/target resolved only from the verified envelope".
//!
//! MAC verification is `penguin_spine::verify_binding` -- the single,
//! schema-version-aware implementation (v2 legacy formula; v3 domain-tagged
//! formula that also binds the numeric `tenant_id`/`community_id`). There is
//! deliberately NO local MAC formula here: a local v2-only copy ignored
//! `schema_version` and the numeric ids, so a relabelled v2->v3 envelope with
//! forged ids verified, and genuine v3 envelopes failed.
//!
//! Implements checks 1-2 of the four-part list in spec §5.11 ("Verification
//! at every hop"): `binding.mac` recomputation under the claimed `kid`, and
//! `tenant`/`community` on the envelope matching the `t:`/`c:` segments of
//! the stream key the entry was read from. Checks 3-4 (grant/install-
//! approval DB scope, outbound credential/target resolution) are the
//! dispatch loop's own responsibility (`crate::dispatch`): 3 reduces to
//! "the entry came from this app_id's own action stream" for the action
//! stage (there is no per-stream grant concept here, unlike process's
//! ingest-source grants, §6.7 -- action's group is always `{app_id}` on
//! `{scope}:app:{app_id}:action`), enforced by construction since
//! `penguin_spine::GroupReader` refuses to read anything outside its own
//! grant list; 4 is satisfied by `crate::dispatch`/`crate::senders` never
//! reading a target/credential field from a bundle's `dispatch` return
//! value.

use penguin_spine::{parse_scope_from_key, verify_binding, BindingError, StageEnvelope};

// Re-exported so callers write `crate::hop::KeyRing` -- the type lives in
// `penguin_spine::binding`, the single version-aware MAC implementation.
pub use penguin_spine::KeyRing;

/// Why a hop failed verification -- each variant is a `reason` label on
/// `waddles_tenant_boundary_violations_total{stage,reason}` (spec §5.11)
/// and maps to `error.kind = "tenant_boundary"` (spec §6.3) in the DLQ
/// record. Field names match the spec's own reason vocabulary exactly.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum BoundaryReason {
    /// `binding.mac` did not recompute to the envelope's value under any
    /// `kid` the keyring holds.
    #[error("binding.mac mismatch")]
    MacMismatch,
    /// `binding.kid` names a key this pod's keyring does not hold.
    #[error("binding.kid {0:?} is not a known key version")]
    UnknownKid(String),
    /// The envelope's `tenant` does not equal the stream key's `t:` segment.
    #[error("envelope tenant {envelope:?} does not match stream key tenant {key:?}")]
    TenantMismatch { envelope: String, key: String },
    /// The envelope's `community` does not equal the stream key's `c:`
    /// segment.
    #[error("envelope community {envelope:?} does not match stream key community {key:?}")]
    CommunityMismatch {
        envelope: Option<String>,
        key: Option<String>,
    },
    /// The stream the entry was read from does not belong to the app_id
    /// this reader is scoped to (spec §5.11 check 3, action-stage form).
    #[error("entry app_id {envelope_app_id:?} does not own the stream it was read from")]
    GrantScopeMismatch { envelope_app_id: String },
    /// A process-stage bundle output tried to set an identity field
    /// (`tenant_id`/`community_id`/`workstream_id`) -- never applicable to
    /// the action stage's own dispatch, kept here so the reason vocabulary
    /// matches the spec's table exactly for any shared reporting code.
    #[error("bundle output attempted to set an identity field")]
    BundleSetIdentity,
}

impl BoundaryReason {
    /// The exact snake_case `reason` label used on
    /// `waddles_tenant_boundary_violations_total{stage,reason}` (spec
    /// §5.11).
    pub fn as_metric_reason(&self) -> &'static str {
        match self {
            BoundaryReason::MacMismatch => "mac_mismatch",
            BoundaryReason::UnknownKid(_) => "unknown_kid",
            BoundaryReason::TenantMismatch { .. } => "tenant_mismatch",
            BoundaryReason::CommunityMismatch { .. } => "community_mismatch",
            BoundaryReason::GrantScopeMismatch { .. } => "grant_scope_mismatch",
            BoundaryReason::BundleSetIdentity => "bundle_set_identity",
        }
    }
}

/// Maps a `penguin_spine::binding` failure onto this module's reason
/// vocabulary. `verify_binding` only returns `MacMismatch`/`UnknownKid`; the
/// keyring-parse variants are startup-only and map conservatively to
/// `MacMismatch` (never a silent pass) so the match stays exhaustive.
fn from_binding_error(e: BindingError) -> BoundaryReason {
    match e {
        BindingError::MacMismatch => BoundaryReason::MacMismatch,
        BindingError::UnknownKid(kid) => BoundaryReason::UnknownKid(kid),
        BindingError::MalformedKeyEntry(_)
        | BindingError::InvalidKeyHex(_, _)
        | BindingError::EmptyKeyring => BoundaryReason::MacMismatch,
    }
}

/// Runs spec §5.11 checks 1-2 (MAC recomputation, tenant/community-vs-key)
/// plus this stage's construction-enforced form of check 3 (the entry's
/// `app_id` matches the app_id `expected_app_id` this reader is scoped to
/// -- see the module doc for why action's grant check reduces to this).
/// Check 4 (credential/target resolution) is enforced by `crate::dispatch`/
/// `crate::senders` never reading those fields from bundle output, not by
/// this function.
pub fn verify_hop(
    ring: &KeyRing,
    env: &StageEnvelope,
    stream_key: &str,
    expected_app_id: &str,
) -> Result<(), BoundaryReason> {
    verify_binding(ring, env).map_err(from_binding_error)?;

    let (key_tenant, key_community) =
        parse_scope_from_key(stream_key).unwrap_or((String::new(), None));
    if env.tenant != key_tenant {
        return Err(BoundaryReason::TenantMismatch {
            envelope: env.tenant.clone(),
            key: key_tenant,
        });
    }
    if env.community != key_community {
        return Err(BoundaryReason::CommunityMismatch {
            envelope: env.community.clone(),
            key: key_community,
        });
    }
    if env.app_id != expected_app_id {
        return Err(BoundaryReason::GrantScopeMismatch {
            envelope_app_id: env.app_id.clone(),
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use penguin_spine::{compute_binding_mac, compute_binding_mac_v3, ENVELOPE_SCHEMA_VERSION};

    const WS: &str = "8f14e45f-ceea-467e-adde-3fb5c9752730";
    const EV: &str = "3fa85f64-5717-4562-b3fc-2c963f66afa6";
    const TRACE: &str = "4bf92f3577b34da6a3ce929d0e0e4736";
    const KEY: &str = "waddles:t:acme:c:main:app:waddles.bot.commands.default:action";
    const APP: &str = "waddles.bot.commands.default";

    fn ring() -> KeyRing {
        KeyRing::new(vec![
            ("k1".to_string(), vec![1u8; 32]),
            ("k2".to_string(), vec![2u8; 32]),
        ])
    }

    fn valid_envelope(mac: String, kid: &str) -> StageEnvelope {
        serde_json::from_value(serde_json::json!({
            "schema_version": 2,
            "tenant": "acme",
            "community": "main",
            "app_id": APP,
            "stage": "action",
            "event": {
                "platform": "twitch",
                "event_type": "chat.message",
                "actor": "some_user",
                "payload": {},
                "occurred_at": "2026-09-14T12:00:00.000Z",
                "source": null
            },
            "ts": "2026-09-14T12:00:00.123Z",
            "target_app_id": null,
            "workstream_id": WS,
            "event_id": EV,
            "session_id": null,
            "trace": {
                "traceparent": format!("00-{TRACE}-00f067aa0ba902b7-01"),
                "tracestate": null
            },
            "binding": {"kid": kid, "mac": mac}
        }))
        .unwrap()
    }

    /// A genuine v2 MAC for the fixture envelope under `kid`.
    fn mac_for(ring: &KeyRing, kid: &str) -> String {
        compute_binding_mac(ring, kid, "acme", Some("main"), WS, EV, Some(TRACE)).unwrap()
    }

    /// A genuine v3 envelope (producer-minted `compute_binding_mac_v3`).
    fn v3_envelope(ring: &KeyRing, tenant_id: i64, community_id: i32) -> StageEnvelope {
        let mac = compute_binding_mac_v3(
            ring,
            "k1",
            tenant_id,
            community_id,
            "acme",
            Some("main"),
            WS,
            EV,
            Some(TRACE),
        )
        .unwrap();
        let mut env = valid_envelope(mac, "k1");
        env.schema_version = ENVELOPE_SCHEMA_VERSION;
        env.tenant_id = Some(tenant_id);
        env.community_id = Some(community_id);
        env
    }

    #[test]
    fn tampered_mac_is_rejected() {
        let ring = ring();
        let mut mac = mac_for(&ring, "k1");
        let flipped = if mac.as_bytes()[0] == b'0' { "1" } else { "0" };
        mac.replace_range(0..1, flipped);
        let env = valid_envelope(mac, "k1");
        let err = verify_hop(&ring, &env, KEY, APP).unwrap_err();
        assert_eq!(err, BoundaryReason::MacMismatch);
        assert_eq!(err.as_metric_reason(), "mac_mismatch");
    }

    #[test]
    fn unknown_kid_on_envelope_is_rejected() {
        let ring = ring();
        let env = valid_envelope(mac_for(&ring, "k1"), "k99");
        assert!(matches!(
            verify_hop(&ring, &env, KEY, APP),
            Err(BoundaryReason::UnknownKid(_))
        ));
    }

    /// v2 envelopes keep verifying (as v2) through the spine's dispatch.
    #[test]
    fn valid_v2_mac_and_matching_key_passes() {
        let ring = ring();
        let env = valid_envelope(mac_for(&ring, "k1"), "k1");
        assert_eq!(env.schema_version, 2);
        assert!(verify_hop(&ring, &env, KEY, APP).is_ok());
    }

    /// regression: confused-deputy -- a legit v2 envelope relabelled to
    /// schema_version=3 with forged numeric ids must FAIL the MAC (the v3
    /// formula is domain-tagged + binds the ids, so the v2 MAC cannot match).
    #[test]
    fn v2_mac_relabelled_as_v3_with_forged_ids_is_rejected() {
        let ring = ring();
        let mut env = valid_envelope(mac_for(&ring, "k1"), "k1");
        env.schema_version = ENVELOPE_SCHEMA_VERSION;
        env.tenant_id = Some(999);
        env.community_id = Some(888);
        let err = verify_hop(&ring, &env, KEY, APP).unwrap_err();
        assert_eq!(err, BoundaryReason::MacMismatch);
    }

    /// A genuine v3 envelope passes (the old local v2-only verify failed it).
    #[test]
    fn genuine_v3_mac_passes() {
        let ring = ring();
        let env = v3_envelope(&ring, 10, 7);
        assert!(verify_hop(&ring, &env, KEY, APP).is_ok());
    }

    /// Swapping either numeric id on a genuine v3 envelope breaks the MAC.
    #[test]
    fn v3_envelope_with_swapped_ids_is_rejected() {
        let ring = ring();
        let mut env = v3_envelope(&ring, 10, 7);
        env.tenant_id = Some(11);
        assert_eq!(
            verify_hop(&ring, &env, KEY, APP).unwrap_err(),
            BoundaryReason::MacMismatch
        );
        let mut env = v3_envelope(&ring, 10, 7);
        env.community_id = Some(8);
        assert_eq!(
            verify_hop(&ring, &env, KEY, APP).unwrap_err(),
            BoundaryReason::MacMismatch
        );
    }

    #[test]
    fn valid_mac_but_wrong_tenant_stream_key_is_rejected() {
        let ring = ring();
        let env = valid_envelope(mac_for(&ring, "k1"), "k1");
        let key = "waddles:t:other-tenant:c:main:app:waddles.bot.commands.default:action";
        let err = verify_hop(&ring, &env, key, APP).unwrap_err();
        assert_eq!(err.as_metric_reason(), "tenant_mismatch");
    }

    #[test]
    fn community_mismatch_between_envelope_and_key_is_rejected() {
        let ring = ring();
        let env = valid_envelope(mac_for(&ring, "k1"), "k1");
        let key = "waddles:t:acme:c:other-community:app:waddles.bot.commands.default:action";
        let err = verify_hop(&ring, &env, key, APP).unwrap_err();
        assert_eq!(err.as_metric_reason(), "community_mismatch");
    }

    #[test]
    fn app_id_not_matching_the_scoped_reader_is_rejected() {
        let ring = ring();
        let env = valid_envelope(mac_for(&ring, "k1"), "k1");
        let err = verify_hop(&ring, &env, KEY, "waddles.other.app.default").unwrap_err();
        assert_eq!(err.as_metric_reason(), "grant_scope_mismatch");
    }

    #[test]
    fn boundary_reason_metric_labels_match_spec_vocabulary() {
        assert_eq!(
            BoundaryReason::MacMismatch.as_metric_reason(),
            "mac_mismatch"
        );
        assert_eq!(
            BoundaryReason::UnknownKid("x".into()).as_metric_reason(),
            "unknown_kid"
        );
        assert_eq!(
            BoundaryReason::TenantMismatch {
                envelope: "a".into(),
                key: "b".into()
            }
            .as_metric_reason(),
            "tenant_mismatch"
        );
        assert_eq!(
            BoundaryReason::CommunityMismatch {
                envelope: None,
                key: None
            }
            .as_metric_reason(),
            "community_mismatch"
        );
        assert_eq!(
            BoundaryReason::GrantScopeMismatch {
                envelope_app_id: "x".into()
            }
            .as_metric_reason(),
            "grant_scope_mismatch"
        );
        assert_eq!(
            BoundaryReason::BundleSetIdentity.as_metric_reason(),
            "bundle_set_identity"
        );
    }
}
