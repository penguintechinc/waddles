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
    48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 1, 204, 5, 142, 35, 153, 231, 38,
    150, 122, 1, 218, 34, 237, 70, 125, 233, 62, 126, 103, 151, 16, 11, 238, 95, 122, 209, 74, 183,
    9, 171, 161,
];
const KEY_A_PUB_RAW: &[u8] = &[
    169, 90, 255, 23, 51, 151, 156, 147, 56, 247, 214, 168, 76, 160, 67, 99, 211, 238, 208, 5, 69,
    236, 245, 115, 4, 81, 1, 42, 23, 107, 4, 187,
];

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
        &EncodingKey::from_ed_der(KEY_A_PRIV_DER),
        Algorithm::EdDSA,
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
        DecodingKey::from_ed_der(KEY_A_PUB_RAW),
    )]));
    let header = json!({"alg": "EdDSA", "typ": "JWT", "kid": "k1"});

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
    assert_eq!(capture.count("service_eddsa", "eddsa", "ok"), 1);
    assert_eq!(capture.count("service_eddsa", "none", "alg_none"), 1);
    assert_eq!(capture.count("service_eddsa", "eddsa", "bad_audience"), 1);
    assert_eq!(capture.count("egress_assertion", "eddsa", "ok"), 1);
    assert_eq!(
        capture.count("egress_assertion", "eddsa", "forbidden_header"),
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
