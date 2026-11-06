//! Test-only Wix webhook JWT factory. Keys are generated per process with the
//! `openssl` CLI (no private key is ever committed); a missing `openssl` is a
//! loud panic, never a skip.
#![allow(dead_code, clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use base64::Engine as _;
use std::io::Write as _;
use std::process::{Command, Stdio};

const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::URL_SAFE_NO_PAD;

/// One RSA test key pair (PEM).
pub struct WixKey {
    pub private_pem: String,
    pub public_pem: String,
}

fn run(args: &[&str], stdin: &[u8]) -> Vec<u8> {
    let mut child = Command::new("openssl")
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("openssl must be installed to run the Wix signing tests");
    child.stdin.take().unwrap().write_all(stdin).unwrap();
    let out = child.wait_with_output().unwrap();
    assert!(out.status.success(), "openssl {args:?} failed");
    out.stdout
}

/// Generates a fresh 2048-bit RSA key pair.
pub fn generate() -> WixKey {
    let private = run(
        &[
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
        ],
        b"",
    );
    let public = run(&["pkey", "-pubout"], &private);
    WixKey {
        private_pem: String::from_utf8(private).unwrap(),
        public_pem: String::from_utf8(public).unwrap(),
    }
}

/// RS256-signs `input` with `key` (temp key file removed immediately).
pub fn sign(key: &WixKey, input: &[u8]) -> Vec<u8> {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let path = std::env::temp_dir().join(format!(
        "wix-test-{}-{}.pem",
        std::process::id(),
        N.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::write(&path, &key.private_pem).unwrap();
    let sig = run(&["dgst", "-sha256", "-sign", path.to_str().unwrap()], input);
    std::fs::remove_file(&path).unwrap();
    sig
}

/// The inner domain event JSON string for a Wix `eventType`.
pub fn domain_event(id: &str, body_key: &str, body: &serde_json::Value) -> String {
    serde_json::json!({
        "id": id,
        "entityFqdn": "wix.test.v1.entity",
        "slug": "test",
        "entityId": "ent-1",
        "eventTime": "2026-11-05T12:00:00.000Z",
        body_key: body,
    })
    .to_string()
}

/// Builds the three-layer Wix envelope and signs it.
pub fn jwt(
    key: &WixKey,
    header: &serde_json::Value,
    instance_id: &str,
    event_type: &str,
    inner_event: &str,
    iat: Option<i64>,
    exp: Option<i64>,
) -> String {
    let envelope = serde_json::json!({
        "instanceId": instance_id,
        "eventType": event_type,
        "identity": "{\"identityType\":\"APP\"}",
        "data": inner_event,
    })
    .to_string();
    let mut payload = serde_json::json!({ "data": envelope });
    if let Some(i) = iat {
        payload["iat"] = i.into();
    }
    if let Some(e) = exp {
        payload["exp"] = e.into();
    }
    let h = B64.encode(header.to_string());
    let p = B64.encode(payload.to_string());
    let signing_input = format!("{h}.{p}");
    let sig = sign(key, signing_input.as_bytes());
    format!("{signing_input}.{}", B64.encode(sig))
}

/// Standard RS256 header.
pub fn rs256() -> serde_json::Value {
    serde_json::json!({"alg": "RS256", "typ": "JWT"})
}
