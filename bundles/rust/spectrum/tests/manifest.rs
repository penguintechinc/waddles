//! The manifest must grant the guest no network capability: Spectrum is inbound-only and the
//! host-side poll transport owns the RSI allowlist and credential.

fn manifest() -> String {
    std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/bundle.yaml"))
        .expect("bundle.yaml present")
}

#[test]
fn manifest_grants_no_guest_egress() {
    let m = manifest();
    let lines: Vec<&str> = m
        .lines()
        .filter(|l| !l.trim_start().starts_with('#'))
        .collect();
    assert!(
        lines.contains(&"egress: []"),
        "egress must be the empty list"
    );
    assert!(
        !lines.iter().any(|l| l.contains("host:")),
        "no egress host entries"
    );
    assert!(
        !lines
            .iter()
            .any(|l| l.contains("net.http") || l.contains("chat.send")),
        "no http/outbound permission"
    );
    assert!(lines.contains(&"routes_to: []"));
}

#[test]
fn manifest_permissions_are_only_identity_lookup() {
    let m = manifest();
    let ids: Vec<&str> = m
        .lines()
        .filter_map(|l| l.trim_start().strip_prefix("- id: "))
        .collect();
    assert_eq!(ids, ["connector.pii.read"]);
}
