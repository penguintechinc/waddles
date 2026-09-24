//! Hop verification (spec §5.11, D30) for the process stage: `binding.mac`
//! recompute-and-verify plus the tenant/community-vs-stream-key check, run
//! on every entry this stage reads before any other processing (including
//! invoking the executor) -- spec §16 M4 row and §5.11's four-part
//! "Verification at every hop" list.
//!
//! Unlike `core/svc_action/src/hop.rs` (M3, which had to reimplement the
//! HMAC formula locally because `penguin_spine`'s own `binding` module
//! (Task 19) had not landed at the rev that crate pinned), this module
//! delegates check 1 (`binding.mac` recomputation) directly to
//! `penguin_spine::verify_binding`/`KeyRing` -- the crate's own
//! doc comment on `svc_action::hop` names this exact swap as "a drop-in
//! replacement, not a behavior change" once the module landed, and it has
//! (see `Cargo.toml`'s pinned rev). No local HMAC/constant-time-compare
//! code lives in this crate at all.
//!
//! **Check 3 differs structurally from `svc_action::hop`'s.** Action's
//! grant check reduces to "the entry came from this app_id's own action
//! stream" (there is exactly one stream per bundle, `{scope}:app:{app_id}
//! :action`) and is verified by comparing `env.app_id` against the
//! reader's own scope. Process's ingest-source streams are the opposite
//! shape (spec §5.1/§5.2): **one shared stream per ingest source, read by
//! many bundles' independent consumer groups** -- `StageEnvelope.app_id`
//! on an entry sitting on that shared stream does not identify which
//! bundle is currently reading it (it is stamped once by svc-ingest, the
//! same value for every subscriber), so an `env.app_id`-vs-reader-identity
//! comparison here would be meaningless, not a security check. Process's
//! check 3 ("the bundle's installation, its stream grant, and its install
//! approval belong to that same tenant") is instead satisfied
//! structurally: `penguin_spine::GroupReader` only ever holds the `grants`
//! hub-api resolved for *this* app's own tenant/community (spec §5.2's
//! resolution step), and refuses to read anything outside that list
//! (`SpineError::StreamNotGranted`, already covered by that crate's own
//! test suite) -- so any stream this reader ever produces an entry from
//! already passed the tenant-scoped grant check at resolution time. The
//! distribution poll that would populate `grants` from real, tenant-scoped
//! `app_stream_grants` rows is itself `// TODO(M4+)` (see `crate::lib`);
//! this module's own responsibility is checks 1-2 only, exactly as
//! documented here.
//!
//! Check 4 ("bundle output attempted to set an identity field") is
//! `crate::spine`'s responsibility at the enqueue step (never reading
//! `tenant`/`community`/`workstream_id`/`event_id`/`trace` from a bundle's
//! `transform` return value) -- [`BoundaryReason::BundleSetIdentity`] is
//! kept here anyway so the `reason` label vocabulary on
//! `waddles_tenant_boundary_violations_total{stage,reason}` matches the
//! spec's table exactly for any shared reporting code, matching
//! `svc_action::hop`'s identical choice.

use penguin_spine::{parse_scope_from_key, verify_binding, BindingError, StageEnvelope};

// Re-exported so callers write `crate::hop::KeyRing` -- this module owns
// hop verification end to end even though the type itself now lives in
// `penguin_spine::binding` (see the module doc).
pub use penguin_spine::KeyRing;

/// Why a hop failed verification -- each variant is a `reason` label on
/// `waddles_tenant_boundary_violations_total{stage="process",reason}`
/// (spec §5.11) and maps to `error.kind = "tenant_boundary"` (spec §6.3,
/// `penguin_spine::DlqErrorKind::TenantBoundary`) in the DLQ record. Field
/// names match the spec's own reason vocabulary exactly.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum BoundaryReason {
    /// `binding.mac` did not recompute to the envelope's value under the
    /// `kid` it claims.
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
    /// A process-stage bundle's `transform` output tried to set an
    /// identity field (`tenant_id`/`community_id`/`workstream_id`/
    /// `event_id`) -- never triggered by this module (see the module doc);
    /// kept for `reason` label parity with the spec's vocabulary table.
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
            BoundaryReason::BundleSetIdentity => "bundle_set_identity",
        }
    }
}

/// Maps a `penguin_spine::binding` failure onto this module's own reason
/// vocabulary. `verify_binding` only ever returns
/// [`BindingError::MacMismatch`]/[`BindingError::UnknownKid`] -- the other
/// three variants are `KeyRing::parse`-time-only (rejected at startup by
/// `crate::lib::try_start_process_loop` before any envelope is ever
/// verified, per that module's "a missing/malformed keyring must never
/// fail open" contract) and are mapped conservatively to `MacMismatch`
/// here (never a silent pass) purely so this match stays exhaustive.
fn from_binding_error(e: BindingError) -> BoundaryReason {
    match e {
        BindingError::MacMismatch => BoundaryReason::MacMismatch,
        BindingError::UnknownKid(kid) => BoundaryReason::UnknownKid(kid),
        BindingError::MalformedKeyEntry(_)
        | BindingError::InvalidKeyHex(_, _)
        | BindingError::EmptyKeyring => BoundaryReason::MacMismatch,
    }
}

/// Runs spec §5.11 checks 1-2 (MAC recomputation via
/// `penguin_spine::verify_binding`, then tenant/community-vs-key) -- see
/// the module doc for why check 3 is satisfied structurally by
/// `penguin_spine::GroupReader` rather than re-derived here, and why check
/// 4 belongs to `crate::spine`.
pub fn verify_hop(
    ring: &KeyRing,
    env: &StageEnvelope,
    stream_key: &str,
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
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

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
            "app_id": "waddles.bot.commands.default",
            "stage": "process",
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
            "workstream_id": "8f14e45f-ceea-467e-adde-3fb5c9752730",
            "event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "session_id": null,
            "trace": {
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                "tracestate": null
            },
            "binding": {"kid": kid, "mac": mac}
        }))
        .unwrap()
    }

    fn mac_for(ring: &KeyRing, kid: &str) -> String {
        penguin_spine::compute_binding_mac(
            ring,
            kid,
            "acme",
            Some("main"),
            "8f14e45f-ceea-467e-adde-3fb5c9752730",
            "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            Some("4bf92f3577b34da6a3ce929d0e0e4736"),
        )
        .unwrap()
    }

    #[test]
    fn valid_mac_and_matching_key_passes() {
        let ring = ring();
        let mac = mac_for(&ring, "k1");
        let env = valid_envelope(mac, "k1");
        let key = "waddles:t:acme:c:main:src:twitch:tw-channelA:events";
        assert!(verify_hop(&ring, &env, key).is_ok());
    }

    #[test]
    fn tampered_mac_is_rejected() {
        let ring = ring();
        let mut mac = mac_for(&ring, "k1");
        let flipped = if mac.as_bytes()[0] == b'0' {
            b'1'
        } else {
            b'0'
        };
        // SAFETY: mac is ASCII hex; replacing one byte keeps it valid UTF-8.
        unsafe { mac.as_bytes_mut()[0] = flipped };
        let env = valid_envelope(mac, "k1");
        let key = "waddles:t:acme:c:main:src:twitch:tw-channelA:events";
        let err = verify_hop(&ring, &env, key).unwrap_err();
        assert_eq!(err, BoundaryReason::MacMismatch);
        assert_eq!(err.as_metric_reason(), "mac_mismatch");
    }

    #[test]
    fn unknown_kid_on_envelope_is_rejected() {
        let ring = ring();
        let mac = mac_for(&ring, "k1");
        let env = valid_envelope(mac, "k99");
        let key = "waddles:t:acme:c:main:src:twitch:tw-channelA:events";
        assert!(matches!(
            verify_hop(&ring, &env, key),
            Err(BoundaryReason::UnknownKid(_))
        ));
    }

    // Sec14.11-style test: a valid MAC for its own tenant/workstream read
    // from a DIFFERENT tenant's stream key is rejected.
    #[test]
    fn valid_mac_but_wrong_tenant_stream_key_is_rejected() {
        let ring = ring();
        let mac = mac_for(&ring, "k1");
        let env = valid_envelope(mac, "k1");
        let key = "waddles:t:other-tenant:c:main:src:twitch:tw-channelA:events";
        let err = verify_hop(&ring, &env, key).unwrap_err();
        assert_eq!(err.as_metric_reason(), "tenant_mismatch");
    }

    #[test]
    fn community_mismatch_between_envelope_and_key_is_rejected() {
        let ring = ring();
        let mac = mac_for(&ring, "k1");
        let env = valid_envelope(mac, "k1");
        let key = "waddles:t:acme:c:other-community:src:twitch:tw-channelA:events";
        let err = verify_hop(&ring, &env, key).unwrap_err();
        assert_eq!(err.as_metric_reason(), "community_mismatch");
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
            BoundaryReason::BundleSetIdentity.as_metric_reason(),
            "bundle_set_identity"
        );
    }

    #[test]
    fn from_binding_error_maps_parse_time_variants_conservatively() {
        assert_eq!(
            from_binding_error(BindingError::MalformedKeyEntry("x".into())),
            BoundaryReason::MacMismatch
        );
        assert_eq!(
            from_binding_error(BindingError::InvalidKeyHex("x".into(), "y".into())),
            BoundaryReason::MacMismatch
        );
        assert_eq!(
            from_binding_error(BindingError::EmptyKeyring),
            BoundaryReason::MacMismatch
        );
    }
}
