//! Verifier-level tests for the rooms gRPC service JWT (`rooms::grpc_auth`): the
//! required-claim set is pinned to `flask_core.service_jwt`, tokens minted by hub-api's
//! REAL issuer verify (committed fixture, see `tests/rooms_fixtures/`), only ES256 is
//! accepted, and `exp`/`nbf`/`iat`/`iss`/`aud`/scope/subject are enforced with the small
//! fixed leeway -- the time boundaries are exercised on an injected clock, no sleeping.

use std::collections::BTreeSet;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use serde_json::{json, Value};
use svc_streaming::http::auth::hardening::{
    REASON_ALG_MISMATCH, REASON_ALG_NONE, REASON_BAD_AUDIENCE, REASON_BAD_ISSUER,
    REASON_BAD_SIGNATURE, REASON_EXPIRED, REASON_IMMATURE, REASON_INVALID_CLAIM,
    REASON_MISSING_CLAIM, REASON_SCOPE_DENIED, REASON_UNKNOWN_KID,
};
use svc_streaming::rooms::grpc_auth::{
    ServiceJwtVerifier, CLOCK_SKEW_SECONDS, REQUIRED_CLAIMS, SCOPE_MODERATE, SCOPE_READ,
    SCOPE_WRITE,
};

const KID: &str = "k1";
const SUB: &str = "spiffe://penguintech.io/test/hub-api";
/// All minted test claims are valid around this instant (unix seconds).
const T: i64 = 1_800_000_000;

fn key() -> rcgen::KeyPair {
    rcgen::KeyPair::generate().unwrap()
}

fn verifier(pub_pem: &str, now: i64) -> ServiceJwtVerifier {
    let keys = ServiceJwtVerifier::keys_from_pem([(KID, pub_pem)]).unwrap();
    ServiceJwtVerifier::new(
        keys,
        "waddlebot-internal",
        vec!["hub-api".to_string()],
        vec![SUB.to_string()],
    )
    .with_clock(move || now)
}

fn claims() -> Value {
    json!({
        "iss": "hub-api", "aud": "waddlebot-internal", "sub": SUB, "scope": SCOPE_READ,
        "iat": T, "nbf": T, "exp": T + 60, "jti": "0b7c6a66-9a52-4a0e-9a3c-0d4f2f2c1111",
    })
}

fn mint(k: &rcgen::KeyPair, mutate: impl FnOnce(&mut Value)) -> String {
    let mut c = claims();
    mutate(&mut c);
    let mut header = Header::new(Algorithm::ES256);
    header.kid = Some(KID.into());
    encode(
        &header,
        &c,
        &EncodingKey::from_ec_pem(k.serialize_pem().as_bytes()).unwrap(),
    )
    .unwrap()
}

fn reason(v: &ServiceJwtVerifier, token: &str, scope: &str) -> &'static str {
    v.verify(token, scope)
        .expect_err("token must be rejected")
        .reason
}

/// The `"require": [...]` list of `ServiceJwtVerifier.verify` in the hub-api issuer module.
fn python_required_claims() -> BTreeSet<String> {
    let src = std::fs::read_to_string(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../libs/flask_core/flask_core/service_jwt.py"),
    )
    .expect("flask_core service_jwt.py is readable from the repo checkout");
    let from = src
        .find("\"require\": [")
        .expect("service_jwt.py still has a \"require\" claim list");
    let body = &src[from + "\"require\": [".len()..];
    let body = &body[..body.find(']').expect("require list terminates")];
    body.split(',')
        .map(|s| s.trim().trim_matches('"').to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

#[test]
fn required_claims_equal_flask_core_service_jwt() {
    let python = python_required_claims();
    let rust: BTreeSet<String> = REQUIRED_CLAIMS.iter().map(|s| s.to_string()).collect();
    assert_eq!(python.len(), 8, "denominator: {python:?}");
    assert_eq!(
        rust, python,
        "rooms gRPC required claims drifted from flask_core"
    );
}

#[test]
fn accepts_tokens_minted_by_hub_apis_real_issuer() {
    let fixture: Value = serde_json::from_str(
        &std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("tests/rooms_fixtures/hub_api_service_jwt.json"),
        )
        .unwrap(),
    )
    .unwrap();
    let kid = fixture["kid"].as_str().unwrap();
    let sub = fixture["sub"].as_str().unwrap().to_string();
    let tokens = fixture["tokens"].as_object().unwrap();
    assert_eq!(tokens.len(), 3, "read, write and moderate tokens");

    for (scope, token) in tokens {
        let token = token.as_str().unwrap();
        let claims = &fixture["claims"][scope];
        let iat = claims["iat"].as_i64().unwrap();
        // Shape pin: exactly the required claims plus the identity's tenant.
        let names: BTreeSet<String> = claims.as_object().unwrap().keys().cloned().collect();
        let mut expected: BTreeSet<String> =
            REQUIRED_CLAIMS.iter().map(|s| s.to_string()).collect();
        expected.insert("tenant".into());
        assert_eq!(names, expected, "hub-api's minted claim set changed");
        // Header as PyJWT writes it: ES256 + kid + typ.
        let header: Value = serde_json::from_slice(
            &URL_SAFE_NO_PAD
                .decode(token.split('.').next().unwrap())
                .unwrap(),
        )
        .unwrap();
        assert_eq!(header["alg"], "ES256");
        assert_eq!(header["kid"], kid);

        let keys =
            ServiceJwtVerifier::keys_from_pem([(kid, fixture["public_key_pem"].as_str().unwrap())])
                .unwrap();
        let v = ServiceJwtVerifier::new(
            keys,
            fixture["audience"].as_str().unwrap(),
            vec![fixture["issuer"].as_str().unwrap().to_string()],
            vec![sub.clone()],
        )
        .with_clock(move || iat + 10);
        let verified = v
            .verify(token, scope)
            .unwrap_or_else(|r| panic!("{scope}: hub-api's own token rejected: {}", r.reason));
        assert_eq!(verified.sub, sub);
        assert_eq!(verified.scope, *scope);
        assert_eq!(verified.tenant.as_deref(), Some("system"));
        assert_eq!(verified.community, None);
        // Same token, any other scope: refused as a scope problem, not a crypto one.
        for other in [SCOPE_READ, SCOPE_WRITE, SCOPE_MODERATE] {
            if other != scope {
                assert_eq!(reason(&v, token, other), REASON_SCOPE_DENIED);
            }
        }
        // The same token an hour and a minute later is expired (30 s leeway elapsed).
        let late = ServiceJwtVerifier::new(
            ServiceJwtVerifier::keys_from_pem([(kid, fixture["public_key_pem"].as_str().unwrap())])
                .unwrap(),
            fixture["audience"].as_str().unwrap(),
            vec![fixture["issuer"].as_str().unwrap().to_string()],
            vec![],
        )
        .with_clock(move || iat + 3600 + CLOCK_SKEW_SECONDS + 1);
        assert_eq!(reason(&late, token, scope), REASON_EXPIRED);
    }
}

#[test]
fn time_claims_are_enforced_with_exactly_the_small_leeway() {
    let k = key();
    let pem = k.public_key_pem();
    let token = mint(&k, |_| {});
    let at = |now: i64| verifier(&pem, now).verify(&token, SCOPE_READ);
    assert_eq!(CLOCK_SKEW_SECONDS, 30, "same skew as flask_core");
    // exp = T+60: accepted up to and including exp+30, refused one second later.
    assert!(at(T + 5).is_ok());
    assert!(at(T + 60).is_ok());
    assert!(at(T + 60 + 30).is_ok(), "within the leeway");
    assert_eq!(at(T + 60 + 31).unwrap_err().reason, REASON_EXPIRED);
    // nbf/iat = T: a verifier clock up to 30 s behind still accepts, 31 s behind refuses.
    assert!(at(T - 30).is_ok());
    assert_eq!(at(T - 31).unwrap_err().reason, REASON_IMMATURE);
    // iat alone in the future is refused even when nbf is fine.
    let future_iat = mint(&k, |c| c["iat"] = json!(T + 3600));
    assert_eq!(
        reason(&verifier(&pem, T + 5), &future_iat, SCOPE_READ),
        REASON_IMMATURE
    );
    // Non-integer time claims are invalid, never silently ignored.
    for name in ["exp", "nbf", "iat"] {
        let bad = mint(&k, |c| c[name] = json!("soon"));
        assert!(
            verifier(&pem, T + 5).verify(&bad, SCOPE_READ).is_err(),
            "{name} as a string"
        );
    }
}

#[test]
fn only_es256_is_accepted() {
    let k = key();
    let v = verifier(&k.public_key_pem(), T + 5);
    assert!(
        v.verify(&mint(&k, |_| {}), SCOPE_READ).is_ok(),
        "positive control"
    );

    // HS256 "signed" with a shared secret (alg confusion).
    let mut hs = Header::new(Algorithm::HS256);
    hs.kid = Some(KID.into());
    let hs256 = encode(
        &hs,
        &claims(),
        &EncodingKey::from_secret(b"0123456789abcdef"),
    )
    .unwrap();
    assert_eq!(reason(&v, &hs256, SCOPE_READ), REASON_ALG_MISMATCH);

    // Hand-built headers: alg none (any case), EdDSA, RS256, ES384 -- with a bogus signature,
    // the algorithm must be refused before any signature work.
    let b64 = |v: Value| URL_SAFE_NO_PAD.encode(serde_json::to_vec(&v).unwrap());
    let payload = b64(claims());
    for (alg, want) in [
        ("none", REASON_ALG_NONE),
        ("NONE", REASON_ALG_NONE),
        ("EdDSA", REASON_ALG_MISMATCH),
        ("RS256", REASON_ALG_MISMATCH),
        ("ES384", REASON_ALG_MISMATCH),
        ("HS256", REASON_ALG_MISMATCH),
    ] {
        let t = format!(
            "{}.{payload}.{}",
            b64(json!({"alg": alg, "kid": KID, "typ": "JWT"})),
            URL_SAFE_NO_PAD.encode([0u8; 64])
        );
        assert_eq!(reason(&v, &t, SCOPE_READ), want, "alg {alg}");
    }
    // `none` with an empty signature segment.
    let t = format!("{}.{payload}.", b64(json!({"alg": "none", "kid": KID})));
    assert_eq!(reason(&v, &t, SCOPE_READ), REASON_ALG_NONE);

    // A token with no `kid` cannot choose its key: unknown/absent kid is refused.
    let no_kid = encode(
        &Header::new(Algorithm::ES256),
        &claims(),
        &EncodingKey::from_ec_pem(k.serialize_pem().as_bytes()).unwrap(),
    )
    .unwrap();
    assert!(v.verify(&no_kid, SCOPE_READ).is_err());
    let mut other_kid = Header::new(Algorithm::ES256);
    other_kid.kid = Some("not-a-trusted-key".into());
    let t = encode(
        &other_kid,
        &claims(),
        &EncodingKey::from_ec_pem(k.serialize_pem().as_bytes()).unwrap(),
    )
    .unwrap();
    assert_eq!(reason(&v, &t, SCOPE_READ), REASON_UNKNOWN_KID);
}

#[test]
fn signature_must_verify() {
    let k = key();
    let v = verifier(&k.public_key_pem(), T + 5);
    // Signed by another key.
    assert_eq!(
        reason(&v, &mint(&key(), |_| {}), SCOPE_READ),
        REASON_BAD_SIGNATURE
    );
    // Payload swapped for a higher-privilege one under the old signature.
    let good = mint(&k, |_| {});
    let parts: Vec<&str> = good.split('.').collect();
    let forged_payload = URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(&{
            let mut c = claims();
            c["scope"] = json!(SCOPE_MODERATE);
            c
        })
        .unwrap(),
    );
    let forged = format!("{}.{forged_payload}.{}", parts[0], parts[2]);
    assert_eq!(reason(&v, &forged, SCOPE_MODERATE), REASON_BAD_SIGNATURE);
}

#[test]
fn every_required_claim_is_enforced() {
    let k = key();
    let v = verifier(&k.public_key_pem(), T + 5);
    let mut examined = 0;
    for name in REQUIRED_CLAIMS {
        let token = mint(&k, |c| {
            c.as_object_mut().unwrap().remove(name);
        });
        assert_eq!(
            reason(&v, &token, SCOPE_READ),
            REASON_MISSING_CLAIM,
            "token without `{name}` must be refused as missing_claim"
        );
        examined += 1;
    }
    assert_eq!(examined, REQUIRED_CLAIMS.len());
    assert_eq!(examined, 8, "denominator");
}

#[test]
fn issuer_audience_scope_subject_and_jti_are_enforced() {
    let k = key();
    let v = verifier(&k.public_key_pem(), T + 5);
    assert_eq!(
        reason(&v, &mint(&k, |c| c["iss"] = json!("evil")), SCOPE_READ),
        REASON_BAD_ISSUER
    );
    assert_eq!(
        reason(
            &v,
            &mint(&k, |c| c["aud"] = json!("someone-else")),
            SCOPE_READ
        ),
        REASON_BAD_AUDIENCE
    );
    assert_eq!(
        reason(&v, &mint(&k, |_| {}), SCOPE_WRITE),
        REASON_SCOPE_DENIED,
        "read token used for a write RPC"
    );
    assert_eq!(
        reason(
            &v,
            &mint(&k, |c| c["sub"] =
                json!("spiffe://penguintech.io/test/other")),
            SCOPE_READ
        ),
        REASON_SCOPE_DENIED,
        "subject outside the allow-list"
    );
    for (name, value) in [("jti", json!("")), ("scope", json!("")), ("sub", json!(""))] {
        let t = mint(&k, |c| c[name] = value);
        assert!(v.verify(&t, SCOPE_READ).is_err(), "empty {name}");
    }
    assert_eq!(
        reason(&v, &mint(&k, |c| c["jti"] = json!(7)), SCOPE_READ),
        REASON_INVALID_CLAIM
    );
    // An empty allow-list means "any subject with a valid token" (the verifier still pins
    // issuer, audience, key and scope).
    let open = ServiceJwtVerifier::new(
        ServiceJwtVerifier::keys_from_pem([(KID, k.public_key_pem().as_str())]).unwrap(),
        "waddlebot-internal",
        vec!["hub-api".to_string()],
        vec![],
    )
    .with_clock(|| T + 5);
    assert!(open
        .verify(
            &mint(&k, |c| c["sub"] =
                json!("spiffe://penguintech.io/test/other")),
            SCOPE_READ
        )
        .is_ok());
}

#[test]
fn optional_tenant_and_community_claims_are_typed() {
    let k = key();
    let v = verifier(&k.public_key_pem(), T + 5);
    let ok = v
        .verify(
            &mint(&k, |c| {
                c["community"] = json!(7);
                c["tenant"] = json!("system");
            }),
            SCOPE_READ,
        )
        .unwrap();
    assert_eq!(
        (ok.community, ok.tenant.as_deref()),
        (Some(7), Some("system"))
    );
    let numeric_tenant = v
        .verify(&mint(&k, |c| c["tenant"] = json!(12)), SCOPE_READ)
        .unwrap();
    assert_eq!(numeric_tenant.tenant.as_deref(), Some("12"));
    for (name, bad) in [
        ("community", json!("7")),
        ("community", json!(-1)),
        ("community", json!(1.5)),
        ("community", json!(null)),
        ("tenant", json!([1])),
        ("tenant", json!("")),
        ("tenant", json!(true)),
    ] {
        let t = mint(&k, |c| c[name] = bad.clone());
        assert_eq!(
            reason(&v, &t, SCOPE_READ),
            REASON_INVALID_CLAIM,
            "{name} = {bad}"
        );
    }
}
