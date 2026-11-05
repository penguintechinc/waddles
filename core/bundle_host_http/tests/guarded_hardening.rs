//! Real-path regression tests for the PR #989 verification round: address
//! policy through the PUBLIC entry point, redirect semantics (RFC 9110),
//! wire-level framing against a raw TCP server, DNS-before-reject, host
//! canonicalization + pinning, rebinding and decompression bounds.

use std::collections::VecDeque;
use std::net::{IpAddr, SocketAddr};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::State;
use axum::http::{HeaderMap, Method, StatusCode};
use axum::routing::any;
use axum::Router;
use bundle_host_http::guarded::{
    forbidden_reason, resolve_pinned, GuardError, GuardedClient, GuardedLimits, HostResolver,
    ResolveFuture, MAX_REDIRECTS_CEILING,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

fn lo() -> Vec<IpAddr> {
    vec!["127.0.0.1".parse().unwrap()]
}

fn limits(max_redirects: usize) -> GuardedLimits {
    GuardedLimits {
        max_redirects,
        timeout: Duration::from_secs(5),
        max_response_bytes: 64 * 1024,
    }
}

async fn serve(app: Router) -> SocketAddr {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let a = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    a
}

/// Resolver that records every lookup and serves scripted answers (the last
/// answer repeats), so rebinding and DNS-oracle behavior is observable.
#[derive(Debug)]
struct Recording {
    calls: Mutex<Vec<String>>,
    answers: Mutex<VecDeque<Vec<IpAddr>>>,
}

impl Recording {
    fn new(answers: Vec<Vec<IpAddr>>) -> Arc<Self> {
        Arc::new(Self {
            calls: Mutex::default(),
            answers: Mutex::new(answers.into()),
        })
    }
    fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }
}

impl HostResolver for Recording {
    fn lookup<'a>(&'a self, host: &'a str, _port: u16) -> ResolveFuture<'a> {
        Box::pin(async move {
            self.calls.lock().unwrap().push(host.to_string());
            let mut q = self.answers.lock().unwrap();
            let a = if q.len() > 1 {
                q.pop_front().unwrap()
            } else {
                q.front().cloned().unwrap_or_default()
            };
            Ok(a)
        })
    }
}

/// Raw TCP HTTP server: records each request's exact wire bytes and answers
/// connection N with `script[min(N, len-1)]`.
async fn raw_server(script: Vec<String>) -> (SocketAddr, Arc<Mutex<Vec<Vec<u8>>>>) {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let seen: Arc<Mutex<Vec<Vec<u8>>>> = Arc::default();
    let s2 = seen.clone();
    tokio::spawn(async move {
        let mut n = 0usize;
        loop {
            let Ok((mut sock, _)) = l.accept().await else {
                return;
            };
            let reply = script[n.min(script.len() - 1)].clone();
            n += 1;
            let seen = s2.clone();
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut tmp = [0u8; 4096];
                loop {
                    match tokio::time::timeout(Duration::from_millis(400), sock.read(&mut tmp))
                        .await
                    {
                        Ok(Ok(k)) if k > 0 => buf.extend_from_slice(&tmp[..k]),
                        _ => break,
                    }
                    if let Some(h) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8_lossy(&buf[..h]).to_ascii_lowercase();
                        let cl = head
                            .lines()
                            .find_map(|l| l.strip_prefix("content-length:"))
                            .and_then(|v| v.trim().parse::<usize>().ok())
                            .unwrap_or(0);
                        if buf.len() >= h + 4 + cl {
                            break;
                        }
                    }
                }
                seen.lock().unwrap().push(buf);
                let _ = sock.write_all(reply.as_bytes()).await;
                let _ = sock.shutdown().await;
            });
        }
    });
    (addr, seen)
}

fn ok_resp() -> String {
    "HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok".to_string()
}

// ---- claim 1: every non-global address refused through the public entry ----

#[tokio::test]
async fn every_non_global_address_is_refused_through_fetch() {
    let c = GuardedClient::new(limits(0), vec![]);
    let table = [
        "::ffff:169.254.169.254",
        "::ffff:127.0.0.1",
        "::127.0.0.1",
        "::ffff:0:127.0.0.1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::7f00:1",
        "64:ff9b:1::1",
        "2002:a9fe:a9fe::",
        "2001::1",
        "fd00:ec2::254",
        "fe80::1",
        "ff02::1",
        "ff0e::1",
        "::",
        "::1",
        "100::1",
        "4000::1",
        "6000::1",
        "8000::1",
        "a000::1",
        "c000::1",
        "e000::1",
        "1000::1",
        "100.64.0.1",
        "100.100.100.200",
        "100.127.255.255",
        "224.0.0.1",
        "239.255.255.255",
        "0.0.0.0",
        "0.1.2.3",
        "192.0.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "198.19.255.255",
        "240.0.0.1",
        "255.255.255.255",
        "169.254.169.254",
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
    ];
    for ip in table {
        let url = if ip.contains(':') {
            format!("https://[{ip}]/")
        } else {
            format!("https://{ip}/")
        };
        let r = c.fetch(Method::GET, &url, vec![], None).await;
        assert!(
            matches!(r, Err(GuardError::ForbiddenAddress(_))),
            "{ip}: {r:?}"
        );
    }
    // Control: genuine global unicast (v4, v6, NAT64-embedded public) passes
    // the policy (resolve only -- no network is touched).
    for ip in ["8.8.8.8", "2606:4700::1111", "64:ff9b::808:808"] {
        assert!(forbidden_reason(ip.parse().unwrap()).is_none(), "{ip}");
    }
    let u = reqwest::Url::parse("https://[2606:4700::1111]/").unwrap();
    assert!(resolve_pinned(&u, &[]).await.is_ok());
}

// ---- claim 3: redirect counter can neither overflow nor exceed the ceiling ----

#[tokio::test]
async fn max_redirects_u8_boundary_and_beyond_are_bounded_by_the_ceiling() {
    for mr in [
        MAX_REDIRECTS_CEILING,
        255,
        256,
        usize::from(u8::MAX) + 2,
        usize::MAX,
    ] {
        let hits = Arc::new(Mutex::new(0usize));
        let app = Router::new()
            .route(
                "/loop",
                any(|State(h): State<Arc<Mutex<usize>>>| async move {
                    *h.lock().unwrap() += 1;
                    (StatusCode::FOUND, [("location", "/loop")])
                }),
            )
            .with_state(hits.clone());
        let a = serve(app).await;
        let c = GuardedClient::new(limits(mr), lo());
        let r = c
            .fetch(Method::GET, &format!("http://{a}/loop"), vec![], None)
            .await;
        assert_eq!(r.unwrap_err(), GuardError::TooManyRedirects, "mr={mr}");
        assert_eq!(*hits.lock().unwrap(), MAX_REDIRECTS_CEILING + 1, "mr={mr}");
    }
}

// ---- claim 4: only 301/302/303/307/308 + Location are redirects ----

#[tokio::test]
async fn non_redirect_3xx_and_locationless_3xx_are_returned_not_errors() {
    let hits = Arc::new(Mutex::new(0usize));
    let app = Router::new()
        .route(
            "/t",
            any(|State(h): State<Arc<Mutex<usize>>>| async move {
                *h.lock().unwrap() += 1;
                "target"
            }),
        )
        .route("/304", any(|| async { StatusCode::NOT_MODIFIED }))
        .route(
            "/300",
            any(|| async { (StatusCode::MULTIPLE_CHOICES, [("location", "/t")]) }),
        )
        .route(
            "/305",
            any(|| async { (StatusCode::USE_PROXY, [("location", "/t")]) }),
        )
        .route("/302-no-loc", any(|| async { StatusCode::FOUND }))
        .route(
            "/307-no-loc",
            any(|| async { StatusCode::TEMPORARY_REDIRECT }),
        )
        .with_state(hits.clone());
    let a = serve(app).await;
    let c = GuardedClient::new(limits(3), lo());
    for (path, code) in [
        ("304", 304u16),
        ("300", 300),
        ("305", 305),
        ("302-no-loc", 302),
        ("307-no-loc", 307),
    ] {
        let r = c
            .fetch(Method::GET, &format!("http://{a}/{path}"), vec![], None)
            .await
            .unwrap_or_else(|e| panic!("{path}: {e:?}"));
        assert_eq!(r.status, code, "{path}");
    }
    assert_eq!(*hits.lock().unwrap(), 0, "a non-redirect 3xx was followed");
}

// ---- claim 5: RFC 9110 method rewriting ----

#[tokio::test]
async fn redirect_method_rewrite_follows_rfc_9110() {
    type Seen = Arc<Mutex<Vec<(String, Vec<u8>)>>>;
    let seen: Seen = Arc::default();
    let rec = |State(s): State<Seen>, m: Method, b: axum::body::Bytes| async move {
        s.lock().unwrap().push((m.to_string(), b.to_vec()));
        "t"
    };
    let app = Router::new()
        .route("/t", any(rec))
        .route(
            "/r301",
            any(|| async { (StatusCode::MOVED_PERMANENTLY, [("location", "/t")]) }),
        )
        .route(
            "/r302",
            any(|| async { (StatusCode::FOUND, [("location", "/t")]) }),
        )
        .route(
            "/r303",
            any(|| async { (StatusCode::SEE_OTHER, [("location", "/t")]) }),
        )
        .route(
            "/r307",
            any(|| async { (StatusCode::TEMPORARY_REDIRECT, [("location", "/t")]) }),
        )
        .with_state(seen.clone());
    let a = serve(app).await;
    let c = GuardedClient::new(limits(3), lo());
    // (method, redirect, has_body) -> (final method, body kept)
    let cases = [
        ("HEAD", "r301", false, "HEAD", false),
        ("HEAD", "r302", false, "HEAD", false),
        ("HEAD", "r303", false, "HEAD", false),
        ("PUT", "r301", true, "PUT", true),
        ("DELETE", "r302", false, "DELETE", false),
        ("POST", "r301", true, "GET", false),
        ("POST", "r302", true, "GET", false),
        ("POST", "r303", true, "GET", false),
        ("PUT", "r303", true, "GET", false),
        ("DELETE", "r303", false, "GET", false),
        ("POST", "r307", true, "POST", true),
        ("GET", "r302", false, "GET", false),
    ];
    for (m, path, has_body, want_m, want_body) in cases {
        seen.lock().unwrap().clear();
        let body = has_body.then(|| b"payload".to_vec());
        let r = c
            .fetch(
                Method::from_bytes(m.as_bytes()).unwrap(),
                &format!("http://{a}/{path}"),
                vec![],
                body,
            )
            .await
            .unwrap_or_else(|e| panic!("{m} {path}: {e:?}"));
        assert_eq!(r.status, 200, "{m} {path}");
        let got = seen.lock().unwrap().clone();
        assert_eq!(got.len(), 1, "{m} {path}");
        assert_eq!(got[0].0, want_m, "{m} {path}");
        assert_eq!(!got[0].1.is_empty(), want_body, "{m} {path} body");
    }
}

// ---- claim 6: dropping the body must drop its framing headers on the wire ----

#[tokio::test]
async fn body_dropped_on_redirect_strips_framing_headers_on_the_wire() {
    let (a, seen) = raw_server(vec![
        "HTTP/1.1 302 Found\r\nlocation: /t\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
            .to_string(),
        ok_resp(),
    ])
    .await;
    let c = GuardedClient::new(limits(3), lo());
    let r = c
        .fetch(
            Method::POST,
            &format!("http://{a}/r"),
            vec![
                ("Content-Length".into(), "5".into()),
                ("Content-Type".into(), "text/plain".into()),
                ("Transfer-Encoding".into(), "chunked".into()),
                ("Accept".into(), "*/*".into()),
            ],
            Some(b"hello".to_vec()),
        )
        .await;
    // Some stacks reject the hand-set framing header on hop one; the property
    // under test is hop two, so only require that hop two happened.
    let wire = seen.lock().unwrap().clone();
    assert!(r.is_ok() && wire.len() == 2, "{r:?} hops={}", wire.len());
    let second = String::from_utf8_lossy(&wire[1]).to_ascii_lowercase();
    assert!(second.starts_with("get /t "), "{second}");
    for bad in ["content-length", "content-type", "transfer-encoding"] {
        assert!(!second.contains(bad), "hop 2 still carries {bad}: {second}");
    }
    assert!(second.contains("accept:"), "{second}");
}

// ---- claim 8: no DNS before a scheme/userinfo/allowlist rejection ----

#[tokio::test]
async fn refusals_that_need_no_dns_make_no_lookup() {
    for (url, want) in [
        ("http://oracle.test/", GuardError::SchemeNotAllowed),
        ("ws://oracle.test/", GuardError::SchemeNotAllowed),
        ("ftp://oracle.test/", GuardError::SchemeNotAllowed),
        ("https://u:p@oracle.test/", GuardError::UserInfoForbidden),
    ] {
        let rec = Recording::new(vec![vec!["8.8.8.8".parse().unwrap()]]);
        let c = GuardedClient::new(limits(0), vec![]).with_resolver(rec.clone());
        let r = c.fetch(Method::GET, url, vec![], None).await;
        assert_eq!(r.unwrap_err(), want, "{url}");
        assert!(rec.calls().is_empty(), "{url} leaked a lookup");
    }
    // Control: a legal https name does reach the resolver (the check is live),
    // and the forbidden answer is refused.
    let rec = Recording::new(vec![vec!["10.0.0.1".parse().unwrap()]]);
    let c = GuardedClient::new(limits(0), vec![]).with_resolver(rec.clone());
    let r = c
        .fetch(Method::GET, "https://oracle.test/", vec![], None)
        .await;
    assert!(matches!(r, Err(GuardError::ForbiddenAddress(_))));
    assert_eq!(rec.calls(), ["oracle.test"]);
}

// ---- claim 9: trailing-dot / uppercase hosts still hit the pinned address ----

#[tokio::test]
async fn trailing_dot_and_uppercase_hosts_connect_to_the_pinned_address() {
    let hosts: Arc<Mutex<Vec<String>>> = Arc::default();
    let app = Router::new()
        .route(
            "/t",
            any(
                |State(h): State<Arc<Mutex<Vec<String>>>>, hd: HeaderMap| async move {
                    h.lock()
                        .unwrap()
                        .push(hd.get("host").unwrap().to_str().unwrap().to_string());
                    "t"
                },
            ),
        )
        .with_state(hosts.clone());
    let a = serve(app).await;
    let p = a.port();
    for form in ["pin.test.", "PIN.TEST", "Pin.Test."] {
        hosts.lock().unwrap().clear();
        let rec = Recording::new(vec![vec!["127.0.0.1".parse().unwrap()]]);
        let c = GuardedClient::new(limits(3), lo()).with_resolver(rec.clone());
        // `pin.test` is not resolvable by the system: success proves the
        // connection used the pinned address, not a second resolution.
        let r = c
            .fetch(Method::GET, &format!("http://{form}:{p}/t"), vec![], None)
            .await
            .unwrap_or_else(|e| panic!("{form}: {e:?}"));
        assert_eq!(r.status, 200, "{form}");
        assert_eq!(rec.calls(), ["pin.test"], "{form}");
        assert_eq!(hosts.lock().unwrap().as_slice(), [format!("pin.test:{p}")]);
    }
}

#[tokio::test]
async fn redirect_hop_with_trailing_dot_host_is_revalidated_and_pinned() {
    let app = Router::new().route("/t", any(|| async { "t" }));
    // Port must be the server's own: build the redirect target dynamically.
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let p = l.local_addr().unwrap().port();
    let app = app.route(
        "/r2",
        any(move || async move {
            (
                StatusCode::FOUND,
                [("location", format!("http://Hop.Test.:{p}/t"))],
            )
        }),
    );
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    let rec = Recording::new(vec![vec!["127.0.0.1".parse().unwrap()]]);
    let c = GuardedClient::new(limits(3), lo()).with_resolver(rec.clone());
    let r = c
        .fetch(
            Method::GET,
            &format!("http://start.test:{p}/r2"),
            vec![],
            None,
        )
        .await
        .unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(rec.calls(), ["start.test", "hop.test"]);
}

// ---- claim 10 (disputed): no decompression; a gzip-labelled body stays wire bytes ----

#[tokio::test]
async fn content_encoding_gzip_is_never_decoded_and_cap_counts_wire_bytes() {
    let saw_accept_encoding = Arc::new(Mutex::new(false));
    // Valid gzip magic + junk: a decoder (if one were ever enabled) would error
    // or expand it; passthrough returns these exact bytes.
    let payload: Vec<u8> = [0x1f, 0x8b, 0x08, 0x00, 0, 0, 0, 0, 0, 0xff]
        .into_iter()
        .chain(std::iter::repeat_n(0u8, 200))
        .collect();
    let p2 = payload.clone();
    let flag = saw_accept_encoding.clone();
    let app = Router::new().route(
        "/z",
        any(move |hd: HeaderMap| {
            let p2 = p2.clone();
            let flag = flag.clone();
            async move {
                *flag.lock().unwrap() = hd.contains_key("accept-encoding");
                ([("content-encoding", "gzip")], p2)
            }
        }),
    );
    let a = serve(app).await;
    let c = GuardedClient::new(limits(0), lo());
    let r = c
        .fetch(Method::GET, &format!("http://{a}/z"), vec![], None)
        .await
        .unwrap();
    assert_eq!(r.body, payload, "body was transformed");
    assert!(
        !*saw_accept_encoding.lock().unwrap(),
        "advertised compression"
    );
    // The cap applies to the bytes handed to the caller.
    let tiny = GuardedClient::new(
        GuardedLimits {
            max_response_bytes: 100,
            ..limits(0)
        },
        lo(),
    );
    assert_eq!(
        tiny.fetch(Method::GET, &format!("http://{a}/z"), vec![], None)
            .await
            .unwrap_err(),
        GuardError::ResponseTooLarge
    );
}

// ---- claim 11: rebinding -- one lookup per hop, connection goes to that answer ----

#[tokio::test]
async fn second_lookup_answer_is_never_used_one_resolution_per_hop() {
    let hit = Arc::new(Mutex::new(0usize));
    let h2 = hit.clone();
    let a = serve(Router::new().route(
        "/t",
        any(move || {
            let h2 = h2.clone();
            async move {
                *h2.lock().unwrap() += 1;
                "t"
            }
        }),
    ))
    .await;
    let rec = Recording::new(vec![
        vec!["127.0.0.1".parse().unwrap()],
        vec!["169.254.169.254".parse().unwrap()],
    ]);
    let c = GuardedClient::new(limits(0), lo()).with_resolver(rec.clone());
    let r = c
        .fetch(
            Method::GET,
            &format!("http://rebind.test:{}/t", a.port()),
            vec![],
            None,
        )
        .await
        .unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(rec.calls().len(), 1, "client re-resolved");
    assert_eq!(*hit.lock().unwrap(), 1);
}

#[tokio::test]
async fn any_forbidden_address_among_several_refuses_the_host() {
    let rec = Recording::new(vec![vec![
        "127.0.0.1".parse().unwrap(),
        "169.254.169.254".parse().unwrap(),
    ]]);
    let c = GuardedClient::new(limits(0), lo()).with_resolver(rec);
    let r = c
        .fetch(Method::GET, "http://multi.test:9/", vec![], None)
        .await;
    assert!(matches!(r, Err(GuardError::ForbiddenAddress(_))), "{r:?}");
}

// ---- claim 12: bracket handling ----

#[tokio::test]
async fn double_bracketed_host_is_not_treated_as_an_ip_literal() {
    let c = GuardedClient::new(limits(0), vec![]);
    let r = c.fetch(Method::GET, "https://[[::1]]/", vec![], None).await;
    assert_eq!(r.unwrap_err(), GuardError::MalformedUrl);
}

// ---- metrics labels + no secrets in errors ----

#[test]
fn outcome_labels_are_a_fixed_bounded_set_and_errors_carry_no_secret() {
    let all = [
        GuardError::MalformedUrl,
        GuardError::SchemeNotAllowed,
        GuardError::UserInfoForbidden,
        GuardError::DnsTimeout,
        GuardError::DnsFailed,
        GuardError::NoAddress,
        GuardError::ForbiddenAddress("cloud_metadata"),
        GuardError::BadRedirect,
        GuardError::BodyCrossOrigin,
        GuardError::TooManyRedirects,
        GuardError::Timeout,
        GuardError::ResponseTooLarge,
        GuardError::Transport("https://user:SECRET@host/?k=SECRET"),
    ];
    let mut labels: Vec<&str> = all.iter().map(GuardError::class).collect();
    labels.sort_unstable();
    labels.dedup();
    assert_eq!(labels.len(), all.len(), "labels must be unique and fixed");
    assert!(labels
        .iter()
        .all(|l| l.chars().all(|c| c.is_ascii_lowercase() || c == '_')));
    // Transport's payload is a static kind and never reaches the label.
    assert_eq!(GuardError::Transport("anything").class(), "transport");
}

#[tokio::test]
async fn errors_for_urls_with_credentials_never_echo_them() {
    let c = GuardedClient::new(limits(0), vec![]);
    for url in [
        "https://user:SECRETPW@10.0.0.5/?token=SECRETQ",
        "https://10.0.0.5/?token=SECRETQ",
        "http://10.0.0.5/?token=SECRETQ",
    ] {
        let e = c.fetch(Method::GET, url, vec![], None).await.unwrap_err();
        let text = format!("{e} {e:?}");
        assert!(
            !text.contains("SECRET") && !text.contains("10.0.0.5"),
            "{text}"
        );
    }
}
