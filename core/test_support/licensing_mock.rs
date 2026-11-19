// Shared test-only helper, pulled in with `include!` by the service crates whose flag-resolution
// tests need a REAL `penguin_licensing::LicenseClient` holding a REAL PostHog snapshot (an explicit
// `false`, an unseen key, ...). The library offers no snapshot injection, so this serves the
// `/decide` endpoint from a loopback std TCP listener. No tokio feature needed.

/// Serves `body` (a `/decide` JSON document) to every request on a loopback port; returns the port.
#[allow(dead_code)]
fn mock_decide(body: &'static str) -> u16 {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind loopback");
    let port = listener.local_addr().expect("local addr").port();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { break };
            let mut buf = Vec::new();
            let mut chunk = [0u8; 4096];
            loop {
                let n = stream.read(&mut chunk).unwrap_or(0);
                if n == 0 {
                    break;
                }
                buf.extend_from_slice(&chunk[..n]);
                if let Some(h) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                    let head = String::from_utf8_lossy(&buf[..h]).to_ascii_lowercase();
                    let want = head
                        .lines()
                        .find_map(|l| l.strip_prefix("content-length:"))
                        .and_then(|v| v.trim().parse::<usize>().ok())
                        .unwrap_or(0);
                    if buf.len() >= h + 4 + want {
                        break;
                    }
                }
            }
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = stream.write_all(resp.as_bytes());
        }
    });
    port
}

/// A client whose snapshot was really fetched from [`mock_decide`]: `flags_json` is the
/// `featureFlags` object body (e.g. `{"waddles.x":false}`). `bypass` puts it on a license-bypass
/// domain (bypass answers license checks only; it must never change a flag value).
#[allow(dead_code)]
async fn seeded_client(
    flags_json: &str,
    bypass: bool,
) -> std::sync::Arc<penguin_licensing::LicenseClient> {
    let body: &'static str = Box::leak(format!("{{\"featureFlags\":{flags_json}}}").into_boxed_str());
    let port = mock_decide(body);
    let mut cfg = penguin_licensing::LicenseConfig::new("waddles-test-seeded").expect("config");
    cfg.posthog_key = Some("phc_test".to_owned());
    cfg.posthog_host.set_scheme("http").expect("http scheme");
    cfg.posthog_host.set_host(Some("127.0.0.1")).expect("host");
    cfg.posthog_host.set_port(Some(port)).expect("port");
    if bypass {
        cfg = cfg
            .with_bypass_domain("waddles.app")
            .with_deployment_domain("svc.waddles.app");
    }
    let client = penguin_licensing::LicenseClient::new(cfg).expect("client");
    client.refresh().await.expect("refresh from mock decide");
    assert!(client.snapshot().is_some(), "snapshot must be seeded");
    client
}
