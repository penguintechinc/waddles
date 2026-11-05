//! Integrity stamp (`pin_sig`) for the outbound relay queue envelope.
//!
//! The Valkey ACL does NOT restrict who may `LPUSH` the outbound list (every
//! app client authenticates as the shared `default` user with `~*` key access,
//! see `k8s/helm/waddlebot/templates/infrastructure/valkey-durable.yaml`), so
//! `origin_channel == channel` alone proves nothing about provenance. The
//! producer (`svc_action`) therefore signs the destination-bearing fields with
//! HMAC-SHA256 under a DEDICATED key `OUTBOUND_PIN_KEYS` (`kid:hexkey[,...]`,
//! active kid `OUTBOUND_PIN_ACTIVE_KID`), mounted only into svc-action (signer)
//! and svc-ingest (verifier), and the drain (`svc_ingest`) verifies in
//! constant time before it sends anything. Every op is signed (chat.send,
//! chat.delete AND dm.send) together with an `exp_ms` freshness bound.
//!
//! **v2 (P6a T6d-1): the numeric `community_id` and `tenant_id` are in the
//! MAC** (`0` = absent), so a queue writer who replays a validly signed entry
//! can neither change nor add nor strip the community whose credential the
//! drain will resolve. An entry signed under the v1 label (no community /
//! tenant) is authentic but UNSCOPED: it verifies as
//! [`PinSigError::LegacyUnscoped`], a distinct refusal, never as success.
//! Rollout: deploy `svc_action` (signer) first, then `svc_ingest` (verifier);
//! in-flight v1 entries are refused (counted) and dropped -- they are not
//! replayed (alpha is disposable; the producer's ack timeout reports them).
//!
//! This file is byte-identical in `core/svc_action/src/pin_sig.rs`
//! (separate crates, no shared lib); a test in `svc_ingest` fails if the
//! two copies drift. Never logs key material, MACs, channel ids or text.

use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// Domain-separation label: a MAC for one purpose can never validate here.
const LABEL: &[u8] = b"waddles.outbound-pin.v2";
/// The v1 label (pre-community binding), accepted ONLY to classify an old
/// entry as [`PinSigError::LegacyUnscoped`] -- never to authorize a send.
const LABEL_V1: &[u8] = b"waddles.outbound-pin.v1";
/// Shortest accepted key (bytes); a weaker key is refused, not used.
const MIN_KEY_BYTES: usize = 16;

/// Why signing / verifying / key loading failed. Variants carry no secret.
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum PinSigError {
    /// No keys were configured.
    #[error("no outbound pin keys configured")]
    NoKeys,
    /// A key entry is not `kid:hexkey` with a long-enough hex key.
    #[error("malformed outbound pin key entry")]
    MalformedKeys,
    /// Signing requested but no active `kid` is set / present in the ring.
    #[error("no active outbound pin signing key")]
    NoActiveKey,
    /// The envelope carries no `pin_sig`.
    #[error("outbound entry has no pin signature")]
    MissingSignature,
    /// `pin_sig` is not `kid:hexmac`.
    #[error("outbound pin signature is malformed")]
    MalformedSignature,
    /// `pin_sig` names a `kid` this ring does not hold.
    #[error("outbound pin signature names an unknown key id")]
    UnknownKid,
    /// The MAC does not match the envelope fields.
    #[error("outbound pin signature mismatch")]
    BadSignature,
    /// The entry carries a valid v1 signature (no community/tenant binding):
    /// authentic but unscoped, so refused. Distinct so a rolling upgrade is
    /// visible in the refusal counter.
    #[error("outbound pin signature predates community binding")]
    LegacyUnscoped,
}

/// The destination-bearing fields the MAC covers (all as placed on the wire).
#[derive(Clone, Copy, Debug)]
pub struct PinFields<'a> {
    pub platform: &'a str,
    pub op: &'a str,
    pub channel: &'a str,
    pub origin_channel: &'a str,
    /// `""` when the op has none.
    pub message_id: &'a str,
    /// `""` when the op has none.
    pub text: &'a str,
    /// DM target (`dm.send`); `""` otherwise.
    pub user_id: &'a str,
    /// Producer op id (`""` when none): binds the ack handshake so a signed
    /// entry cannot be re-labelled to settle another producer's op.
    pub op_id: &'a str,
    /// Producer deadline, epoch ms (always present; bounds replay).
    pub exp_ms: u64,
    /// Numeric `communities.id` the drain resolves the credential for
    /// (`0` = none / tenant-wide). Bound by the MAC since v2.
    pub community_id: i64,
    /// Numeric tenant id when the entry carries one (`0` = absent).
    pub tenant_id: i64,
}

/// A rotation-friendly keyring: any held `kid` verifies, `active` signs.
#[derive(Clone)]
pub struct PinKeys {
    keys: Vec<(String, Vec<u8>)>,
    active: Option<String>,
}

impl std::fmt::Debug for PinKeys {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PinKeys")
            .field(
                "kids",
                &self.keys.iter().map(|(k, _)| k).collect::<Vec<_>>(),
            )
            .field("active", &self.active)
            .finish()
    }
}

fn hex_decode(s: &str) -> Option<Vec<u8>> {
    if s.is_empty() || !s.len().is_multiple_of(2) || !s.is_ascii() {
        return None;
    }
    s.as_bytes()
        .chunks(2)
        .map(|p| {
            let hi = char::from(p[0]).to_digit(16)?;
            let lo = char::from(p[1]).to_digit(16)?;
            u8::try_from(hi * 16 + lo).ok()
        })
        .collect()
}

fn hex_encode(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

impl PinKeys {
    /// Parses `kid1:hexkey1,kid2:hexkey2` (the `OUTBOUND_PIN_KEYS` shape).
    /// `active_kid` selects the signing key (verify-only deployments pass
    /// `None`).
    ///
    /// # Errors
    /// [`PinSigError::NoKeys`] for an empty ring, [`PinSigError::MalformedKeys`]
    /// for a bad entry or a key under 16 bytes, [`PinSigError::NoActiveKey`]
    /// when `active_kid` is not in the ring.
    pub fn parse(raw: &str, active_kid: Option<&str>) -> Result<Self, PinSigError> {
        let mut keys = Vec::new();
        for entry in raw.split(',').map(str::trim).filter(|e| !e.is_empty()) {
            let (kid, hexkey) = entry.split_once(':').ok_or(PinSigError::MalformedKeys)?;
            let bytes = hex_decode(hexkey).ok_or(PinSigError::MalformedKeys)?;
            if kid.is_empty() || bytes.len() < MIN_KEY_BYTES {
                return Err(PinSigError::MalformedKeys);
            }
            keys.push((kid.to_string(), bytes));
        }
        if keys.is_empty() {
            return Err(PinSigError::NoKeys);
        }
        let active = match active_kid.filter(|k| !k.is_empty()) {
            Some(k) if keys.iter().any(|(kid, _)| kid == k) => Some(k.to_string()),
            Some(_) => return Err(PinSigError::NoActiveKey),
            None => None,
        };
        Ok(Self { keys, active })
    }

    fn key_for(&self, kid: &str) -> Option<&[u8]> {
        self.keys
            .iter()
            .find(|(k, _)| k == kid)
            .map(|(_, v)| v.as_slice())
    }

    fn mac(key: &[u8], f: &PinFields<'_>, legacy: bool) -> Result<HmacSha256, PinSigError> {
        let mut mac = HmacSha256::new_from_slice(key).map_err(|_| PinSigError::MalformedKeys)?;
        mac.update(if legacy { LABEL_V1 } else { LABEL });
        let exp = f.exp_ms.to_string();
        let (community, tenant) = (f.community_id.to_string(), f.tenant_id.to_string());
        let mut parts = vec![
            f.platform,
            f.op,
            f.channel,
            f.origin_channel,
            f.message_id,
            f.text,
            f.user_id,
            f.op_id,
            exp.as_str(),
        ];
        if !legacy {
            parts.push(community.as_str());
            parts.push(tenant.as_str());
        }
        for part in parts {
            // Length-prefixed so field boundaries can never be shifted.
            let len = u32::try_from(part.len()).map_err(|_| PinSigError::MalformedSignature)?;
            mac.update(&len.to_be_bytes());
            mac.update(part.as_bytes());
        }
        Ok(mac)
    }

    /// Signs `fields` under the active key; returns `kid:hexmac`.
    ///
    /// # Errors
    /// [`PinSigError::NoActiveKey`] when no signing key is configured.
    pub fn sign(&self, fields: &PinFields<'_>) -> Result<String, PinSigError> {
        let kid = self.active.as_deref().ok_or(PinSigError::NoActiveKey)?;
        let key = self.key_for(kid).ok_or(PinSigError::NoActiveKey)?;
        let tag = Self::mac(key, fields, false)?.finalize().into_bytes();
        Ok(format!("{kid}:{}", hex_encode(&tag)))
    }

    /// Verifies `sig` (`kid:hexmac`) over `fields` in constant time.
    ///
    /// # Errors
    /// [`PinSigError::MalformedSignature`], [`PinSigError::UnknownKid`] or
    /// [`PinSigError::BadSignature`].
    pub fn verify(&self, fields: &PinFields<'_>, sig: &str) -> Result<(), PinSigError> {
        let (kid, tag_hex) = sig.split_once(':').ok_or(PinSigError::MalformedSignature)?;
        let tag = hex_decode(tag_hex).ok_or(PinSigError::MalformedSignature)?;
        let key = self.key_for(kid).ok_or(PinSigError::UnknownKid)?;
        // `verify_slice` compares in constant time (`subtle`).
        if Self::mac(key, fields, false)?.verify_slice(&tag).is_ok() {
            return Ok(());
        }
        // A v1 MAC over the same fields: authentic, but it never bound a
        // community. Classified, never accepted.
        if Self::mac(key, fields, true)?.verify_slice(&tag).is_ok() {
            return Err(PinSigError::LegacyUnscoped);
        }
        Err(PinSigError::BadSignature)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    const KEYS: &str = "k1:000102030405060708090a0b0c0d0e0f,k2:ffeeddccbbaa99887766554433221100";

    fn fields() -> PinFields<'static> {
        PinFields {
            platform: "discord",
            op: "chat.send",
            channel: "123",
            origin_channel: "123",
            message_id: "",
            text: "hi",
            user_id: "",
            op_id: "",
            exp_ms: 1_700_000_000_000,
            community_id: 7,
            tenant_id: 0,
        }
    }

    #[test]
    fn golden_vector_is_stable_across_services() {
        // Same vector is asserted in both crates' copies of this file.
        let k = PinKeys::parse(KEYS, Some("k1")).unwrap();
        assert_eq!(
            k.sign(&fields()).unwrap(),
            "k1:3eb99db7ab2c791e9120e405b16377e605652692127c13441f858013089f57e1"
        );
    }

    /// The pre-v2 golden (signed WITHOUT community/tenant under the v1
    /// label): still recognised, but only as `LegacyUnscoped`.
    #[test]
    fn a_v1_signature_is_classified_legacy_never_accepted() {
        let k = PinKeys::parse(KEYS, Some("k1")).unwrap();
        let v1 = "k1:1ff2c00b8d4eea50b0371acd512329475b9b4397c49148092c8a34436f62100f";
        for f in [
            fields(),
            PinFields {
                community_id: 0,
                ..fields()
            },
        ] {
            assert_eq!(k.verify(&f, v1), Err(PinSigError::LegacyUnscoped));
        }
        // v1 over a tampered field is just a bad signature.
        let tampered = PinFields {
            text: "ho",
            ..fields()
        };
        assert_eq!(k.verify(&tampered, v1), Err(PinSigError::BadSignature));
    }

    /// regression: p6a-t6d1 -- community_id / tenant_id are MAC-bound:
    /// changing, swapping, adding or stripping either breaks the signature.
    #[test]
    fn community_and_tenant_are_bound_by_the_mac() {
        let k = PinKeys::parse(KEYS, Some("k1")).unwrap();
        let sig = k.sign(&fields()).unwrap();
        for f in [
            PinFields {
                community_id: 8,
                ..fields()
            },
            PinFields {
                community_id: 0,
                ..fields()
            },
            PinFields {
                community_id: -7,
                ..fields()
            },
            PinFields {
                tenant_id: 1,
                ..fields()
            },
        ] {
            assert_eq!(k.verify(&f, &sig), Err(PinSigError::BadSignature), "{f:?}");
        }
        // Signed with no community: adding one later is refused too.
        let none = PinFields {
            community_id: 0,
            ..fields()
        };
        let sig0 = k.sign(&none).unwrap();
        assert_eq!(k.verify(&none, &sig0), Ok(()));
        assert_eq!(k.verify(&fields(), &sig0), Err(PinSigError::BadSignature));
        // community 1 + tenant 23 != community 12 + tenant 3 (length-prefixed).
        let a = PinFields {
            community_id: 1,
            tenant_id: 23,
            ..fields()
        };
        let b = PinFields {
            community_id: 12,
            tenant_id: 3,
            ..fields()
        };
        assert_ne!(k.sign(&a).unwrap(), k.sign(&b).unwrap());
    }

    #[test]
    fn roundtrip_and_rotation() {
        let signer = PinKeys::parse(KEYS, Some("k1")).unwrap();
        let sig = signer.sign(&fields()).unwrap();
        // Verifier holds both keys with a new active k2: still accepts k1.
        let rotated = PinKeys::parse(KEYS, Some("k2")).unwrap();
        assert_eq!(rotated.verify(&fields(), &sig), Ok(()));
        let only_k2 = PinKeys::parse("k2:ffeeddccbbaa99887766554433221100", None).unwrap();
        assert_eq!(
            only_k2.verify(&fields(), &sig),
            Err(PinSigError::UnknownKid)
        );
    }

    #[test]
    fn any_field_change_breaks_the_mac() {
        let k = PinKeys::parse(KEYS, Some("k1")).unwrap();
        let sig = k.sign(&fields()).unwrap();
        for f in [
            PinFields {
                channel: "124",
                ..fields()
            },
            PinFields {
                text: "ho",
                ..fields()
            },
            PinFields {
                op: "chat.delete",
                ..fields()
            },
            PinFields {
                platform: "twitch",
                ..fields()
            },
            PinFields {
                message_id: "9",
                ..fields()
            },
            PinFields {
                user_id: "7",
                ..fields()
            },
            PinFields {
                op_id: "op-1",
                ..fields()
            },
            PinFields {
                exp_ms: 1_700_000_000_001,
                ..fields()
            },
        ] {
            assert_eq!(k.verify(&f, &sig), Err(PinSigError::BadSignature));
        }
        // Boundary shifting between adjacent fields is not a collision.
        let a = PinFields {
            channel: "12",
            origin_channel: "3",
            ..fields()
        };
        let b = PinFields {
            channel: "1",
            origin_channel: "23",
            ..fields()
        };
        assert_ne!(k.sign(&a).unwrap(), k.sign(&b).unwrap());
    }

    #[test]
    fn malformed_inputs_fail_loud() {
        assert_eq!(PinKeys::parse("", None).unwrap_err(), PinSigError::NoKeys);
        for bad in [
            "k1",
            "k1:zz",
            "k1:0011",
            ":000102030405060708090a0b0c0d0e0f",
        ] {
            assert_eq!(
                PinKeys::parse(bad, None).unwrap_err(),
                PinSigError::MalformedKeys,
                "{bad}"
            );
        }
        assert_eq!(
            PinKeys::parse(KEYS, Some("nope")).unwrap_err(),
            PinSigError::NoActiveKey
        );
        let verify_only = PinKeys::parse(KEYS, None).unwrap();
        assert_eq!(verify_only.sign(&fields()), Err(PinSigError::NoActiveKey));
        assert_eq!(
            verify_only.verify(&fields(), "nocolon"),
            Err(PinSigError::MalformedSignature)
        );
        assert_eq!(
            verify_only.verify(&fields(), "k1:zz"),
            Err(PinSigError::MalformedSignature)
        );
        assert_eq!(
            verify_only.verify(&fields(), "k1:00"),
            Err(PinSigError::BadSignature)
        );
        assert!(!format!("{verify_only:?}").contains("0102"));
    }
}
