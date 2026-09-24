//! Hop verification (spec §5.11, D30): `binding.mac` recompute-and-verify
//! plus the tenant/community-vs-stream-key check, run on every entry this
//! stage reads before any other processing (including invoking the
//! executor) -- spec §16 M3 row: "`binding.mac` and tenant/community/grant/
//! approval verification (§5.11) run before every dispatch; outbound
//! credential/target resolved only from the verified envelope".
//!
//! `penguin_spine`'s own `binding` module (`compute_binding_mac`/
//! `verify_binding`, that crate's doc comments reference it as "Task 19")
//! has now landed, at the `rev` this service pins as of the M1a/dead-letter
//! finalization pass. Its module doc states it was "reproduced byte-for-
//! byte from `waddles core/svc_action/src/hop.rs` (verified-correct,
//! reviewed) -- same formula, same keyring shape, same constant-time
//! comparison", and this module's own re-verification below
//! (`tests::this_modules_mac_matches_penguin_spines_binding_module_exactly`)
//! confirms it against that crate's own published canonical test vectors:
//! byte-identical output for the same `(tenant, community, workstream_id,
//! event_id, trace_id)` tuple and keyring. Switching `svc_action::hop` to
//! call `penguin_spine::binding` directly instead of keeping this local
//! copy remains a separate follow-up (a call-site refactor, not a
//! behavior change, since the two are already proven identical) -- not
//! done in this landing.
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

use hmac::{Hmac, Mac};
use sha2::Sha256;
use subtle::ConstantTimeEq;

use penguin_spine::{trace_id_from_traceparent, StageEnvelope, TENANT_WIDE_SEGMENT};

type HmacSha256 = Hmac<Sha256>;

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

/// Symmetric HMAC key material for `binding.mac`, keyed by `kid` (spec
/// §5.11: "Keys are named by `kid` and rotated with an overlap window").
/// Verification accepts a MAC produced under any `kid` the ring holds; new
/// MACs (this stage never mints one in the M3 dispatch path -- only
/// svc-ingest does, spec §5.11 "Minting" -- but `compute_mac` is exposed
/// for tests and any future stage-side re-signing need) are produced under
/// whichever `kid` the caller names.
#[derive(Clone, Debug)]
pub struct KeyRing {
    keys: Vec<(String, Vec<u8>)>,
}

/// Raised building a [`KeyRing`] from its env-var text form.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum KeyRingError {
    #[error("ENVELOPE_BINDING_KEYS entry {0:?} is not of the form kid:hexkey")]
    MalformedEntry(String),
    #[error("ENVELOPE_BINDING_KEYS entry for kid {0:?} is not valid hex: {1}")]
    InvalidHex(String, String),
    #[error("ENVELOPE_BINDING_KEYS is empty -- hop verification cannot start with no keys")]
    Empty,
}

impl KeyRing {
    /// Builds a ring directly from `(kid, key_bytes)` pairs -- the
    /// programmatic constructor tests use.
    pub fn new(keys: Vec<(String, Vec<u8>)>) -> Self {
        Self { keys }
    }

    /// Parses the `ENVELOPE_BINDING_KEYS` env-var shape:
    /// `kid1:hexkey1,kid2:hexkey2`. Refuses to build an empty ring -- a
    /// misconfigured/missing keyring must be a hard startup error, never a
    /// silent "every MAC fails to verify" (which would DLQ 100% of
    /// traffic) or worse, "every MAC passes" (which would defeat the wall
    /// entirely).
    pub fn parse(raw: &str) -> Result<Self, KeyRingError> {
        let mut keys = Vec::new();
        for entry in raw.split(',') {
            let entry = entry.trim();
            if entry.is_empty() {
                continue;
            }
            let (kid, hexkey) = entry
                .split_once(':')
                .ok_or_else(|| KeyRingError::MalformedEntry(entry.to_string()))?;
            if kid.is_empty() || hexkey.is_empty() {
                return Err(KeyRingError::MalformedEntry(entry.to_string()));
            }
            let key_bytes = hex::decode(hexkey)
                .map_err(|e| KeyRingError::InvalidHex(kid.to_string(), e.to_string()))?;
            keys.push((kid.to_string(), key_bytes));
        }
        if keys.is_empty() {
            return Err(KeyRingError::Empty);
        }
        Ok(Self { keys })
    }

    fn key_for(&self, kid: &str) -> Option<&[u8]> {
        self.keys
            .iter()
            .find(|(k, _)| k == kid)
            .map(|(_, v)| v.as_slice())
    }
}

/// Computes `binding.mac` (spec §5.11's exact formula):
/// `hex(HMAC-SHA256(k_binding[kid], tenant ‖ community ‖ workstream_id ‖
/// event_id ‖ trace_id))`, where `community` renders as the literal
/// `_tenant` when absent and `trace_id` is the 32-hex trace-id segment of
/// `traceparent` (empty string when no trace is present -- the formula has
/// no defined behavior for a missing trace, so this stage's own inputs are
/// concatenated consistently with how `compute_mac`/`verify_mac` are both
/// called: always from the same envelope fields).
pub fn compute_mac(
    ring: &KeyRing,
    kid: &str,
    tenant: &str,
    community: Option<&str>,
    workstream_id: &str,
    event_id: &str,
    trace_id: Option<&str>,
) -> Result<String, BoundaryReason> {
    let key = ring
        .key_for(kid)
        .ok_or_else(|| BoundaryReason::UnknownKid(kid.to_string()))?;
    let mut mac = HmacSha256::new_from_slice(key).map_err(|_| BoundaryReason::MacMismatch)?;
    mac.update(tenant.as_bytes());
    mac.update(community.unwrap_or(TENANT_WIDE_SEGMENT).as_bytes());
    mac.update(workstream_id.as_bytes());
    mac.update(event_id.as_bytes());
    mac.update(trace_id.unwrap_or("").as_bytes());
    Ok(hex::encode(mac.finalize().into_bytes()))
}

/// Recomputes `env.binding.mac` under `env.binding.kid` and compares in
/// constant time (`subtle`) against the value on the wire -- a
/// variable-time `==` on a MAC is a timing side channel a network attacker
/// can exploit to forge a valid one byte at a time.
fn verify_mac(ring: &KeyRing, env: &StageEnvelope) -> Result<(), BoundaryReason> {
    let trace_id = env
        .trace
        .as_ref()
        .and_then(|t| trace_id_from_traceparent(&t.traceparent));
    let expected = compute_mac(
        ring,
        &env.binding.kid,
        &env.tenant,
        env.community.as_deref(),
        &env.workstream_id,
        &env.event_id,
        trace_id,
    )?;
    let actual = env.binding.mac.as_bytes();
    let expected_bytes = expected.as_bytes();
    if expected_bytes.len() == actual.len() && bool::from(expected_bytes.ct_eq(actual)) {
        Ok(())
    } else {
        Err(BoundaryReason::MacMismatch)
    }
}

/// Parses `(tenant, community)` from a spine key of the shape
/// `waddles:t:{tenant}:c:{community}:...`. Local re-implementation of
/// `penguin_spine::parse_scope_from_key`'s exact behavior (that function is
/// `pub` at the pinned rev, so this could call it directly -- kept as a
/// thin local wrapper only so this module's public surface doesn't leak an
/// otherwise-unused re-export; see the call site in `verify_hop`).
fn scope_from_key(key: &str) -> Option<(String, Option<String>)> {
    penguin_spine::parse_scope_from_key(key)
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
    verify_mac(ring, env)?;

    let (key_tenant, key_community) = scope_from_key(stream_key).unwrap_or((String::new(), None));
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
        compute_mac(
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
    fn compute_mac_is_deterministic_and_64_hex_chars() {
        let mac1 = mac_for(&ring(), "k1");
        let mac2 = mac_for(&ring(), "k1");
        assert_eq!(mac1, mac2);
        assert_eq!(mac1.len(), 64);
        assert!(mac1.chars().all(|c| c.is_ascii_hexdigit()));
    }

    /// **Re-verification against the newly-landed `penguin_spine::binding`
    /// module** (spine rev bump, finalization pass): this crate's fixture
    /// (`ring()`, and `mac_for`'s tenant/community/workstream_id/event_id/
    /// trace_id tuple) is byte-identical to `penguin_spine::binding`'s own
    /// canonical test vectors -- so this hex literal is `penguin_spine::
    /// binding::tests::K1_MAIN_MAC`, copied verbatim from that crate's
    /// source at the pinned rev, not hand-computed here. A match proves
    /// this module's independent implementation and spine's newly-landed
    /// one agree exactly; do NOT assume it, the module doc explicitly
    /// requires re-running this check on any future spine binding-module
    /// pin bump.
    #[test]
    fn this_modules_mac_matches_penguin_spines_binding_module_exactly() {
        const K1_MAIN_MAC_FROM_PENGUIN_SPINE_BINDING: &str =
            "d94c3849257550fe817c399410113a45e22b1c9203fbd02908f6bcc19df95f3f";
        assert_eq!(
            mac_for(&ring(), "k1"),
            K1_MAIN_MAC_FROM_PENGUIN_SPINE_BINDING
        );
    }

    #[test]
    fn different_kid_produces_a_different_mac() {
        assert_ne!(mac_for(&ring(), "k1"), mac_for(&ring(), "k2"));
    }

    #[test]
    fn compute_mac_rejects_unknown_kid() {
        let err = compute_mac(&ring(), "nope", "acme", None, "w", "e", None).unwrap_err();
        assert_eq!(err, BoundaryReason::UnknownKid("nope".to_string()));
    }

    #[test]
    fn keyring_parse_reads_kid_hexkey_pairs() {
        let ring = KeyRing::parse("k1:0102030405060708090a0b0c0d0e0f10,k2:ff").unwrap();
        assert!(ring.key_for("k1").is_some());
        assert_eq!(ring.key_for("k2"), Some(&[0xffu8][..]));
    }

    #[test]
    fn keyring_parse_rejects_empty_string() {
        assert_eq!(KeyRing::parse("").unwrap_err(), KeyRingError::Empty);
    }

    #[test]
    fn keyring_parse_rejects_malformed_entry() {
        assert!(matches!(
            KeyRing::parse("k1-nocoloin"),
            Err(KeyRingError::MalformedEntry(_))
        ));
    }

    #[test]
    fn keyring_parse_rejects_invalid_hex() {
        assert!(matches!(
            KeyRing::parse("k1:zzzz"),
            Err(KeyRingError::InvalidHex(_, _))
        ));
    }

    // -- Sec14.11 test 4: a tampered binding.mac (one byte flipped) is
    // rejected regardless of which kid is claimed. --
    #[test]
    fn tampered_mac_is_rejected() {
        let ring = ring();
        let mut mac = mac_for(&ring, "k1");
        // Flip one hex character -- still 64 valid hex chars, just wrong.
        let flipped = if mac.as_bytes()[0] == b'0' {
            b'1'
        } else {
            b'0'
        };
        // SAFETY: mac is ASCII hex; replacing one byte keeps it valid UTF-8.
        unsafe { mac.as_bytes_mut()[0] = flipped };
        let env = valid_envelope(mac, "k1");
        let key = "waddles:t:acme:c:main:app:waddles.bot.commands.default:action";
        let err = verify_hop(&ring, &env, key, "waddles.bot.commands.default").unwrap_err();
        assert_eq!(err, BoundaryReason::MacMismatch);
        assert_eq!(err.as_metric_reason(), "mac_mismatch");
    }

    #[test]
    fn unknown_kid_on_envelope_is_rejected() {
        let ring = ring();
        let mac = mac_for(&ring, "k1");
        let env = valid_envelope(mac, "k99");
        let key = "waddles:t:acme:c:main:app:waddles.bot.commands.default:action";
        assert!(matches!(
            verify_hop(&ring, &env, key, "waddles.bot.commands.default"),
            Err(BoundaryReason::UnknownKid(_))
        ));
    }

    #[test]
    fn valid_mac_and_matching_key_passes() {
        let ring = ring();
        let mac = mac_for(&ring, "k1");
        let env = valid_envelope(mac, "k1");
        let key = "waddles:t:acme:c:main:app:waddles.bot.commands.default:action";
        assert!(verify_hop(&ring, &env, key, "waddles.bot.commands.default").is_ok());
    }

    // -- Sec14.11 test 1: a valid MAC for its own tenant/workstream read
    // from a DIFFERENT tenant's stream key is rejected. --
    #[test]
    fn valid_mac_but_wrong_tenant_stream_key_is_rejected() {
        let ring = ring();
        let mac = mac_for(&ring, "k1");
        let env = valid_envelope(mac, "k1");
        let key = "waddles:t:other-tenant:c:main:app:waddles.bot.commands.default:action";
        let err = verify_hop(&ring, &env, key, "waddles.bot.commands.default").unwrap_err();
        assert_eq!(err.as_metric_reason(), "tenant_mismatch");
    }

    #[test]
    fn community_mismatch_between_envelope_and_key_is_rejected() {
        let ring = ring();
        let mac = mac_for(&ring, "k1");
        let env = valid_envelope(mac, "k1");
        let key = "waddles:t:acme:c:other-community:app:waddles.bot.commands.default:action";
        let err = verify_hop(&ring, &env, key, "waddles.bot.commands.default").unwrap_err();
        assert_eq!(err.as_metric_reason(), "community_mismatch");
    }

    #[test]
    fn app_id_not_matching_the_scoped_reader_is_rejected() {
        let ring = ring();
        let mac = mac_for(&ring, "k1");
        let env = valid_envelope(mac, "k1");
        let key = "waddles:t:acme:c:main:app:waddles.bot.commands.default:action";
        let err = verify_hop(&ring, &env, key, "waddles.other.app.default").unwrap_err();
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
