//! Suppression-code parity against `hub_api/services/suppression_keys.py` (+ the envelope
//! platform-KEK HKDF/kek_id it relies on). Vectors from `scripts/gen_ops_cli_vectors.py`.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use serde::Deserialize;
use waddles_ops_cli::pystr::{py_int, py_strip};
use waddles_ops_cli::suppression::*;

#[derive(Deserialize)]
struct CodeV {
    key_ver: String,
    hmac: String,
}
#[derive(Deserialize)]
struct Case {
    tenant_id: i64,
    platform: String,
    platform_user_id: String,
    norm_platform: String,
    norm_platform_user_id: String,
    tenant_key_vers: Vec<String>,
    codes: Vec<CodeV>,
}
#[derive(Deserialize)]
struct Invalid {
    tenant_id: i64,
    platform: String,
    platform_user_id: String,
}
#[derive(Deserialize)]
struct Vectors {
    kek_hex: String,
    kek_previous_hex: String,
    kek_ids: Vec<String>,
    kek_id_current: String,
    codes: Vec<Case>,
    invalid: Vec<Invalid>,
}

fn vectors() -> Vectors {
    serde_json::from_str(include_str!("suppression_vectors.json")).unwrap()
}

#[test]
fn derived_tenant_keys_and_codes_match_python() {
    let v = vectors();
    assert_eq!(v.codes.len(), 9, "zero/short denominator");
    let kek = PlatformKek::from_values(Some(&v.kek_hex), Some(&v.kek_previous_hex)).unwrap();
    assert_eq!(kek.kek_ids(), v.kek_ids);
    assert_eq!(kek.kek_ids()[0], v.kek_id_current);
    for c in &v.codes {
        let (plat, puid) = normalize_identity(&c.platform, &c.platform_user_id).unwrap();
        assert_eq!(plat, c.norm_platform, "platform normalisation");
        assert_eq!(puid, c.norm_platform_user_id, "id normalisation");
        let keys = kek.tenant_keys(c.tenant_id).unwrap();
        // The HKDF tenant key itself is not stored (secret-scanner hygiene); the Python HMAC
        // codes below can only match if the derived key is byte-identical.
        let vers: Vec<&str> = keys.iter().map(|k| k.key_ver.as_str()).collect();
        assert_eq!(vers, c.tenant_key_vers);
        let codes = lookup_codes(&kek, c.tenant_id, &c.platform, &c.platform_user_id).unwrap();
        assert_eq!(codes.len(), c.codes.len());
        for (got, want) in codes.iter().zip(&c.codes) {
            assert_eq!(got.key_ver, want.key_ver);
            assert_eq!(
                got.hmac, want.hmac,
                "suppression code diverges (tenant {})",
                c.tenant_id
            );
        }
    }
}

#[test]
fn codes_are_per_tenant_and_per_platform() {
    let v = vectors();
    let h = |t: usize| &v.codes[t].codes[0].hmac;
    assert_ne!(h(0), h(2), "same id, different tenant must differ");
    assert_eq!(
        h(0),
        h(1),
        "normalisation makes padded/mixed-case input match"
    );
}

#[test]
fn invalid_inputs_are_rejected_exactly_like_python() {
    let v = vectors();
    assert_eq!(v.invalid.len(), 12, "zero/short denominator");
    let tk = TenantKey::from_parts("v", [7u8; 32]);
    for i in &v.invalid {
        let r1 = compute_code(&tk, &i.platform, &i.platform_user_id);
        let r2 = validate_tenant_id(i.tenant_id);
        assert!(r1.is_err() || r2.is_err(), "must reject {:?}", i.platform);
    }
}

#[test]
fn missing_or_malformed_kek_fails_closed() {
    assert!(PlatformKek::from_values(None, None).is_err());
    assert!(PlatformKek::from_values(Some(""), None).is_err());
    assert!(PlatformKek::from_values(Some("   "), None).is_err());
    assert!(PlatformKek::from_values(Some(&"ab".repeat(31)), None).is_err());
    assert!(PlatformKek::from_values(Some(&"zz".repeat(32)), None).is_err());
    let ok = "ab".repeat(32);
    assert!(PlatformKek::from_values(Some(&ok), Some("short")).is_err());
    // Same previous as current collapses to one id (Python dict-key collision).
    assert_eq!(
        PlatformKek::from_values(Some(&ok), Some(&ok))
            .unwrap()
            .kek_ids()
            .len(),
        1
    );
    // Key material never leaks through Debug/errors.
    let k = PlatformKek::from_values(Some(&ok), None)
        .unwrap()
        .tenant_keys(1)
        .unwrap();
    let dbg = format!("{k:?}");
    assert!(
        dbg.contains("<redacted>") && !dbg.contains("key: ["),
        "{dbg}"
    );
    let err = PlatformKek::from_values(Some(&"zz".repeat(32)), None)
        .err()
        .unwrap();
    assert!(!err.to_string().contains("zz"));
}

#[test]
fn python_str_semantics() {
    assert_eq!(py_strip("\u{1c}x\u{1f}"), "x");
    assert_eq!(py_strip("\u{a0}x\u{3000}"), "x");
    assert_eq!(py_int(" +1_0 "), Some(10));
    assert_eq!(py_int("-7"), Some(-7));
    for bad in ["", "_1", "1_", "1__0", "1.0", "0x1", "abc", "- 1"] {
        assert_eq!(py_int(bad), None, "{bad:?}");
    }
}
