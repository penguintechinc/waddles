//! Metric parity through the REAL global meter provider.
//!
//! `service_auth::verify` and `egress_proxy::assertion::verify_from_header`
//! both report via `opentelemetry::global`. This test owns its process, so it
//! installs a capturing provider as the global one and then drives the public
//! entry points exactly as a service does. It pins the contract the Python
//! verifiers share (`docs/JWT_VERIFICATION.md`): counter
//! `waddles_jwt_verifications_total{verifier,alg,outcome}`, histogram
//! `waddles_jwt_verification_seconds{verifier,alg}`, closed label values.
//!
//! One test function on purpose: the global provider is process-wide and may
//! only be set once, and `JwtMetrics::global()` binds on first use.

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use egress_proxy::assertion::{self, InMemoryReplayCache};
use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey};
use serde_json::{json, Value};
use service_auth::test_support::Capture;
use service_auth::TrustBundle;

const KEY_A_PRIV_DER: &[u8] = &[
    48, 129, 135, 2, 1, 0, 48, 19, 6, 7, 42, 134, 72, 206, 61, 2, 1, 6, 8, 42, 134, 72, 206, 61, 3,
    1, 7, 4, 109, 48, 107, 2, 1, 1, 4, 32, 209, 234, 92, 37, 28, 130, 216, 139, 48, 1, 225, 220,
    88, 158, 30, 88, 20, 126, 43, 7, 182, 151, 62, 129, 248, 115, 187, 119, 18, 12, 142, 93, 161,
    68, 3, 66, 0, 4, 17, 138, 186, 198, 3, 136, 154, 139, 190, 114, 14, 103, 123, 44, 247, 26, 31,
    171, 203, 86, 236, 40, 175, 97, 236, 72, 32, 22, 179, 142, 109, 144, 173, 115, 191, 94, 95,
    104, 151, 149, 88, 212, 250, 138, 99, 63, 147, 180, 150, 85, 29, 12, 130, 136, 7, 86, 162, 99,
    191, 4, 123, 168, 238, 207,
];
const KEY_A_X: &str = "EYq6xgOImou-cg5neyz3Gh-ry1bsKK9h7EggFrOObZA";
const KEY_A_Y: &str = "rXO_Xl9ol5VY1PqKYz-TtJZVHQyCiAdWomO_BHuo7s8";

struct Bundle(HashMap<String, DecodingKey>);

#[async_trait::async_trait]
impl TrustBundle for Bundle {
    async fn public_key(&self, kid: &str) -> Option<DecodingKey> {
        self.0.get(kid).cloned()
    }
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock after epoch")
        .as_secs()
}

fn b64(value: &Value) -> String {
    URL_SAFE_NO_PAD.encode(value.to_string())
}

fn sign(header: &Value, claims: &Value) -> String {
    let message = format!("{}.{}", b64(header), b64(claims));
    let signature = jsonwebtoken::crypto::sign(
        message.as_bytes(),
        &EncodingKey::from_ec_der(KEY_A_PRIV_DER),
        Algorithm::ES256,
    )
    .expect("sign");
    format!("{message}.{signature}")
}

fn machine_claims() -> Value {
    json!({
        "iss": "hub-api", "aud": "egress-proxy", "sub": "spiffe://penguintech.io/alpha/svc-process",
        "scope": "egress:connect", "iat": now(), "nbf": now(), "exp": now() + 300, "jti": "m-1",
    })
}

fn assertion_claims() -> Value {
    json!({
        "sub": "spiffe://penguintech.io/alpha/svc-process",
        "tenant": "tenant-a", "community": "community-a", "app": "app-a",
        "category": "fqdn", "destination": "discord.com", "port": 443,
        "jti": "a-1", "iat": now(), "exp": now() + 30,
    })
}

#[tokio::test]
async fn both_egress_proxy_verifiers_report_into_the_shared_metric_stream() {
    let capture = Capture::new();
    capture.install_global();

    let bundle = Bundle(HashMap::from([(
        "k1".to_string(),
        DecodingKey::from_ec_components(KEY_A_X, KEY_A_Y).expect("test P-256 key"),
    )]));
    let header = json!({"alg": "ES256", "typ": "JWT", "kid": "k1"});

    // --- service_eddsa (service_auth::verify, the machine-JWT verifier) ----
    let good = sign(&header, &machine_claims());
    service_auth::verify(
        &good,
        &bundle,
        "egress-proxy",
        &["hub-api"],
        "egress:connect",
    )
    .await
    .expect("valid machine JWT");
    let none = format!(
        "{}.{}.",
        b64(&json!({"alg": "none", "kid": "k1"})),
        b64(&machine_claims())
    );
    service_auth::verify(
        &none,
        &bundle,
        "egress-proxy",
        &["hub-api"],
        "egress:connect",
    )
    .await
    .expect_err("alg none");
    let mut wrong_aud = machine_claims();
    wrong_aud["aud"] = json!("someone-else");
    service_auth::verify(
        &sign(&header, &wrong_aud),
        &bundle,
        "egress-proxy",
        &["hub-api"],
        "egress:connect",
    )
    .await
    .expect_err("wrong audience");

    // --- egress_assertion (assertion::verify_from_header) -------------------
    let replay = InMemoryReplayCache::new();
    let mut headers = http::HeaderMap::new();
    headers.insert(
        assertion::ASSERTION_HEADER,
        sign(&header, &assertion_claims())
            .parse()
            .expect("header value"),
    );
    assertion::verify_from_header(&headers, &bundle, 60, &replay)
        .await
        .expect("valid assertion");

    let mut jku = header.clone();
    jku["jku"] = json!("https://attacker.example/keys");
    headers.insert(
        assertion::ASSERTION_HEADER,
        sign(&jku, &assertion_claims())
            .parse()
            .expect("header value"),
    );
    assertion::verify_from_header(&headers, &bundle, 60, &replay)
        .await
        .expect_err("jku header");

    // The bare verifier (audit context) is NOT counted.
    let before = capture.total();
    assertion::verify(&sign(&header, &assertion_claims()), &bundle, 60)
        .await
        .expect("bare verify still decides");
    assert_eq!(capture.total(), before, "bare verify must not report");

    // --- the stream: exact label names + values -----------------------------
    assert_eq!(capture.count("service_eddsa", "es256", "ok"), 1);
    assert_eq!(capture.count("service_eddsa", "none", "alg_none"), 1);
    assert_eq!(capture.count("service_eddsa", "es256", "bad_audience"), 1);
    assert_eq!(capture.count("egress_assertion", "es256", "ok"), 1);
    assert_eq!(
        capture.count("egress_assertion", "es256", "forbidden_header"),
        1
    );
    assert_eq!(capture.total(), 5, "one sample per reporting verification");

    for point in capture.points("waddles_jwt_verifications_total") {
        let labels: Vec<&str> = point.attrs.keys().map(String::as_str).collect();
        assert_eq!(
            labels,
            ["alg", "outcome", "verifier"],
            "identical to Python"
        );
        assert_eq!(point.unit, "{verification}");
    }
    let latency = capture.points("waddles_jwt_verification_seconds");
    assert_eq!(latency.iter().map(|p| p.value).sum::<u64>(), 5);
    for point in latency {
        let labels: Vec<&str> = point.attrs.keys().map(String::as_str).collect();
        assert_eq!(labels, ["alg", "verifier"]);
        assert_eq!(point.unit, "s");
    }
}
