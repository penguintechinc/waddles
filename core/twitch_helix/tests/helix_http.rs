//! HTTP-level tests for `HelixClient` against a wiremock server.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::json;
use twitch_helix::{AccessToken, HelixClient, HelixError};
use wiremock::matchers::{body_json, header, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn client(server: &MockServer) -> HelixClient {
    HelixClient::with_base_url("cid", AccessToken::new("tok").unwrap(), server.uri()).unwrap()
}

fn epoch_in(secs: u64) -> String {
    (SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
        + secs)
        .to_string()
}

#[tokio::test]
async fn delete_happy_path() {
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .and(path("/moderation/chat"))
        .and(query_param("broadcaster_id", "b1"))
        .and(query_param("moderator_id", "m1"))
        .and(query_param("message_id", "msg1"))
        .and(header("Authorization", "Bearer tok"))
        .and(header("Client-Id", "cid"))
        .respond_with(
            ResponseTemplate::new(204)
                .insert_header("Ratelimit-Limit", "800")
                .insert_header("Ratelimit-Remaining", "799")
                .insert_header("Ratelimit-Reset", "1700000000"),
        )
        .expect(1)
        .mount(&s)
        .await;
    let c = client(&s);
    c.delete_chat_message("b1", "m1", "msg1").await.unwrap();
    let rl = c.last_rate_limit().unwrap();
    assert_eq!(rl.limit, Some(800));
    assert_eq!(rl.remaining, Some(799));
    assert_eq!(rl.reset_epoch_secs, Some(1_700_000_000));
}

#[tokio::test]
async fn delete_401_is_unauthorized() {
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .respond_with(ResponseTemplate::new(401))
        .mount(&s)
        .await;
    let err = client(&s)
        .delete_chat_message("b", "m", "x")
        .await
        .unwrap_err();
    assert!(matches!(err, HelixError::Unauthorized));
    assert!(!err.is_retryable());
}

#[tokio::test]
async fn delete_403_is_forbidden_with_message() {
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .respond_with(
            ResponseTemplate::new(403)
                .set_body_json(json!({"error":"Forbidden","status":403,"message":"missing scope"})),
        )
        .mount(&s)
        .await;
    let err = client(&s)
        .delete_chat_message("b", "m", "x")
        .await
        .unwrap_err();
    match err {
        HelixError::Forbidden { message } => assert_eq!(message, "missing scope"),
        other => panic!("unexpected {other:?}"),
    }
}

#[tokio::test]
async fn delete_400_is_client_error_with_plain_body() {
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .respond_with(ResponseTemplate::new(400).set_body_string("not json"))
        .mount(&s)
        .await;
    let err = client(&s)
        .delete_chat_message("b", "m", "x")
        .await
        .unwrap_err();
    assert!(
        matches!(err, HelixError::Client { status: 400, ref message } if message == "not json")
    );
}

#[tokio::test]
async fn delete_429_exposes_retry_after() {
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .respond_with(
            ResponseTemplate::new(429).insert_header("Ratelimit-Reset", epoch_in(5).as_str()),
        )
        .mount(&s)
        .await;
    let err = client(&s)
        .delete_chat_message("b", "m", "x")
        .await
        .unwrap_err();
    match err {
        HelixError::RateLimited { retry_after } => {
            assert!(retry_after >= Duration::from_secs(1) && retry_after <= Duration::from_secs(6));
        }
        other => panic!("unexpected {other:?}"),
    }
}

#[tokio::test]
async fn delete_429_waits_and_retries_once_when_opted_in() {
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .respond_with(ResponseTemplate::new(429))
        .up_to_n_times(1)
        .mount(&s)
        .await;
    Mock::given(method("DELETE"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&s)
        .await;
    let c = client(&s).with_wait_on_rate_limit(true);
    c.delete_chat_message("b", "m", "x").await.unwrap();
}

#[tokio::test]
async fn persistent_429_after_one_wait_still_errors() {
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .respond_with(ResponseTemplate::new(429))
        .expect(2)
        .mount(&s)
        .await;
    let c = client(&s).with_wait_on_rate_limit(true);
    let err = c.delete_chat_message("b", "m", "x").await.unwrap_err();
    assert!(matches!(err, HelixError::RateLimited { .. }));
}

#[tokio::test]
async fn server_error_is_retryable() {
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .respond_with(ResponseTemplate::new(503))
        .mount(&s)
        .await;
    let err = client(&s)
        .delete_chat_message("b", "m", "x")
        .await
        .unwrap_err();
    assert!(matches!(err, HelixError::Server { status: 503 }));
    assert!(err.is_retryable());
}

#[tokio::test]
async fn transport_failure_is_retryable() {
    // Bind then close a port so the connection is actively refused.
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let uri = format!("http://{}", listener.local_addr().unwrap());
    drop(listener);
    let c = HelixClient::with_base_url("cid", AccessToken::new("t").unwrap(), uri).unwrap();
    let err = c.delete_chat_message("b", "m", "x").await.unwrap_err();
    assert!(matches!(err, HelixError::Transport(_)));
    assert!(err.is_retryable());
}

#[tokio::test]
async fn send_chat_happy_path_with_reply() {
    let s = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/chat/messages"))
        .and(body_json(json!({
            "broadcaster_id":"b","sender_id":"s","message":"hi","reply_parent_message_id":"p"
        })))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"data":[{"message_id":"abc","is_sent":true}]})),
        )
        .expect(1)
        .mount(&s)
        .await;
    let sent = client(&s)
        .send_chat_message("b", "s", "hi", Some("p"))
        .await
        .unwrap();
    assert_eq!(sent.message_id, "abc");
}

#[tokio::test]
async fn send_chat_omits_reply_when_none() {
    let s = MockServer::start().await;
    Mock::given(method("POST"))
        .and(body_json(
            json!({"broadcaster_id":"b","sender_id":"s","message":"hi"}),
        ))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"data":[{"message_id":"abc","is_sent":true}]})),
        )
        .expect(1)
        .mount(&s)
        .await;
    client(&s)
        .send_chat_message("b", "s", "hi", None)
        .await
        .unwrap();
}

#[tokio::test]
async fn send_chat_dropped_fails_loud() {
    let s = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"data":[{
            "message_id":"","is_sent":false,
            "drop_reason":{"code":"msg_duplicate","message":"dup"}
        }]})))
        .mount(&s)
        .await;
    let err = client(&s)
        .send_chat_message("b", "s", "hi", None)
        .await
        .unwrap_err();
    assert!(matches!(err, HelixError::MessageDropped { ref code, .. } if code == "msg_duplicate"));
}

#[tokio::test]
async fn send_chat_dropped_without_reason_is_unknown() {
    let s = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"data":[{"message_id":"","is_sent":false}]})),
        )
        .mount(&s)
        .await;
    let err = client(&s)
        .send_chat_message("b", "s", "hi", None)
        .await
        .unwrap_err();
    assert!(matches!(err, HelixError::MessageDropped { ref code, .. } if code == "unknown"));
}

#[tokio::test]
async fn send_chat_malformed_and_empty_data() {
    let s = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_string("nope"))
        .up_to_n_times(1)
        .mount(&s)
        .await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"data":[]})))
        .mount(&s)
        .await;
    let c = client(&s);
    assert!(matches!(
        c.send_chat_message("b", "s", "hi", None).await.unwrap_err(),
        HelixError::Malformed(_)
    ));
    assert!(matches!(
        c.send_chat_message("b", "s", "hi", None).await.unwrap_err(),
        HelixError::Malformed(_)
    ));
}

#[tokio::test]
async fn send_chat_401_and_429() {
    let s = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(401))
        .up_to_n_times(1)
        .mount(&s)
        .await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(429))
        .mount(&s)
        .await;
    let c = client(&s);
    assert!(matches!(
        c.send_chat_message("b", "s", "hi", None).await.unwrap_err(),
        HelixError::Unauthorized
    ));
    assert!(matches!(
        c.send_chat_message("b", "s", "hi", None).await.unwrap_err(),
        HelixError::RateLimited { .. }
    ));
}

#[tokio::test]
async fn whisper_happy_path() {
    let s = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/whispers"))
        .and(query_param("from_user_id", "f"))
        .and(query_param("to_user_id", "t"))
        .and(body_json(json!({"message":"psst"})))
        .respond_with(ResponseTemplate::new(204))
        .expect(1)
        .mount(&s)
        .await;
    client(&s).send_whisper("f", "t", "psst").await.unwrap();
}

#[tokio::test]
async fn whisper_401_429_403() {
    let s = MockServer::start().await;
    for (code, n) in [(401u16, 1u64), (429, 1), (403, 1)] {
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(code))
            .up_to_n_times(n)
            .mount(&s)
            .await;
    }
    let c = client(&s);
    assert!(matches!(
        c.send_whisper("f", "t", "m").await.unwrap_err(),
        HelixError::Unauthorized
    ));
    assert!(matches!(
        c.send_whisper("f", "t", "m").await.unwrap_err(),
        HelixError::RateLimited { .. }
    ));
    assert!(matches!(
        c.send_whisper("f", "t", "m").await.unwrap_err(),
        HelixError::Forbidden { .. }
    ));
}

#[tokio::test]
async fn invalid_arguments_never_hit_network() {
    let s = MockServer::start().await;
    let c = client(&s);
    let long_chat = "x".repeat(501);
    let long_whisper = "x".repeat(10_001);
    let results = [
        c.delete_chat_message("", "m", "x").await.unwrap_err(),
        c.delete_chat_message("b", " ", "x").await.unwrap_err(),
        c.delete_chat_message("b", "m", "").await.unwrap_err(),
        c.send_chat_message("", "s", "hi", None).await.unwrap_err(),
        c.send_chat_message("b", "", "hi", None).await.unwrap_err(),
        c.send_chat_message("b", "s", "", None).await.unwrap_err(),
        c.send_chat_message("b", "s", &long_chat, None)
            .await
            .unwrap_err(),
        c.send_whisper("", "t", "m").await.unwrap_err(),
        c.send_whisper("f", "", "m").await.unwrap_err(),
        c.send_whisper("f", "t", "").await.unwrap_err(),
        c.send_whisper("f", "t", &long_whisper).await.unwrap_err(),
    ];
    assert_eq!(results.len(), 11);
    assert!(results
        .iter()
        .all(|e| matches!(e, HelixError::InvalidArgument(_))));
    assert!(s.received_requests().await.unwrap().is_empty());
}

#[test]
fn client_rejects_empty_client_id_and_trims_base_url() {
    let t = || AccessToken::new("t").unwrap();
    assert!(matches!(
        HelixClient::new("", t()),
        Err(HelixError::InvalidArgument(_))
    ));
    assert!(HelixClient::with_base_url("cid", t(), "http://localhost:1/helix/").is_ok());
    assert!(HelixClient::new("cid", t()).is_ok());
}

#[tokio::test]
async fn token_from_env_reads_value() {
    // SAFETY-free: unique var name, set before read in this single test.
    std::env::set_var("TWITCH_HELIX_TEST_TOKEN_PRESENT", "abc");
    assert!(AccessToken::from_env("TWITCH_HELIX_TEST_TOKEN_PRESENT").is_ok());
}

/// Display + Debug + every `source()` link of an error, joined for leak scanning.
fn full_error_text(err: &HelixError) -> String {
    let mut out = format!("{err} | {err:?}");
    let mut cur: Option<&dyn std::error::Error> = std::error::Error::source(err);
    while let Some(e) = cur {
        out.push_str(&format!(" | {e} | {e:?}"));
        cur = e.source();
    }
    out
}

// regression: PR #722 review -- Transport errors leaked the full request URL
// (incl. query-string user/channel ids) into Display/Debug.
#[tokio::test]
async fn transport_error_never_leaks_url_or_ids() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let uri = format!("http://{}", listener.local_addr().unwrap());
    drop(listener);
    let c = HelixClient::with_base_url("cid", AccessToken::new("tok").unwrap(), uri).unwrap();

    let errs = [
        c.send_whisper("from-user-9911", "to-user-4422", "hi")
            .await
            .unwrap_err(),
        c.delete_chat_message("chan-7733", "mod-5544", "msg-6655")
            .await
            .unwrap_err(),
        c.send_chat_message("chan-7733", "send-8866", "hi", None)
            .await
            .unwrap_err(),
    ];
    assert_eq!(errs.len(), 3);
    for err in &errs {
        assert!(matches!(err, HelixError::Transport(_)), "{err:?}");
        assert!(err.is_retryable());
        let text = full_error_text(err);
        for needle in [
            "from_user_id",
            "to_user_id",
            "broadcaster_id",
            "moderator_id",
            "message_id",
            // Full ids, not bare digits: the ephemeral port could contain digits.
            "from-user-9911",
            "to-user-4422",
            "chan-7733",
            "mod-5544",
            "msg-6655",
            "send-8866",
            "/whispers",
            "/moderation/chat",
            "/chat/messages",
            "http://",
            "https://",
        ] {
            assert!(!text.contains(needle), "{needle:?} leaked in {text:?}");
        }
    }
}

#[tokio::test]
async fn malformed_error_never_leaks_url() {
    let s = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/chat/messages"))
        .respond_with(ResponseTemplate::new(200).set_body_string("not json"))
        .mount(&s)
        .await;
    let err = client(&s)
        .send_chat_message("chan-7733", "send-8866", "hi", None)
        .await
        .unwrap_err();
    assert!(matches!(err, HelixError::Malformed(_)), "{err:?}");
    let text = full_error_text(&err);
    for needle in ["chan-7733", "send-8866", "/chat/messages", "http://"] {
        assert!(!text.contains(needle), "{needle:?} leaked in {text:?}");
    }
}

// regression: PR #722 review -- a trailing-newline token became a retryable
// Transport (builder) error and was retried forever.
#[tokio::test]
async fn token_with_trailing_newline_is_trimmed_and_sent_clean() {
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .and(path("/moderation/chat"))
        .and(header("Authorization", "Bearer tok"))
        .respond_with(ResponseTemplate::new(204))
        .expect(1)
        .mount(&s)
        .await;
    let c = HelixClient::with_base_url("cid", AccessToken::new("tok\n").unwrap(), s.uri()).unwrap();
    c.delete_chat_message("b", "m", "x").await.unwrap();
}

#[test]
fn bad_tokens_fail_loud_and_non_retryable() {
    let cases = [
        "",
        " ",
        "\n",
        "\r\n",
        "sekrit\ntok",
        "sekrit tok",
        "s\u{e9}krit",
    ];
    for bad in cases {
        let err = AccessToken::new(bad).unwrap_err();
        assert!(matches!(err, HelixError::InvalidArgument(_)), "{bad:?}");
        assert!(!err.is_retryable(), "{bad:?}");
        assert!(!err.to_string().contains("sekrit"), "token echoed: {err}");
    }
}

#[tokio::test]
async fn token_from_env_is_trimmed_and_blank_fails_loud() {
    std::env::set_var("TWITCH_HELIX_TEST_TOKEN_NEWLINE", "envtok\n");
    std::env::set_var("TWITCH_HELIX_TEST_TOKEN_BLANK", "\n");
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .and(header("Authorization", "Bearer envtok"))
        .respond_with(ResponseTemplate::new(204))
        .expect(1)
        .mount(&s)
        .await;
    let token = AccessToken::from_env("TWITCH_HELIX_TEST_TOKEN_NEWLINE").unwrap();
    HelixClient::with_base_url("cid", token, s.uri())
        .unwrap()
        .delete_chat_message("b", "m", "x")
        .await
        .unwrap();

    let err = AccessToken::from_env("TWITCH_HELIX_TEST_TOKEN_BLANK").unwrap_err();
    assert!(matches!(err, HelixError::InvalidArgument(_)));
    assert!(!err.is_retryable());
}

#[tokio::test]
async fn client_id_is_trimmed_and_unsendable_ids_rejected() {
    let s = MockServer::start().await;
    Mock::given(method("DELETE"))
        .and(header("Client-Id", "cid"))
        .respond_with(ResponseTemplate::new(204))
        .expect(1)
        .mount(&s)
        .await;
    let t = || AccessToken::new("tok").unwrap();
    HelixClient::with_base_url("cid\n", t(), s.uri())
        .unwrap()
        .delete_chat_message("b", "m", "x")
        .await
        .unwrap();
    for bad in ["c\nid", "c id", "\n"] {
        let err = HelixClient::with_base_url(bad, t(), s.uri()).unwrap_err();
        assert!(matches!(err, HelixError::InvalidArgument(_)), "{bad:?}");
        assert!(!err.is_retryable());
    }
}

// A 4xx whose body is cut off mid-stream still classifies by status and says
// plainly that the explanation was unreadable (no silent empty default).
#[tokio::test]
async fn unreadable_error_body_is_reported_not_hidden() {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let uri = format!("http://{}", listener.local_addr().unwrap());
    let server = std::thread::spawn(move || {
        let (mut sock, _) = listener.accept().unwrap();
        let mut buf = [0u8; 2048];
        let _ = sock.read(&mut buf).unwrap();
        // Promise 100 body bytes, send 5, then hang up.
        sock.write_all(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 100\r\n\r\nshort")
            .unwrap();
    });
    let c = HelixClient::with_base_url("cid", AccessToken::new("tok").unwrap(), uri).unwrap();
    let err = c.delete_chat_message("b", "m", "x").await.unwrap_err();
    server.join().unwrap();
    match &err {
        HelixError::Client { status, message } => {
            assert_eq!(*status, 400);
            assert_eq!(message, "<response body unreadable>");
        }
        other => panic!("expected Client error, got {other:?}"),
    }
    assert!(!err.is_retryable());
}

#[tokio::test]
async fn unparsable_base_url_is_non_retryable() {
    let c =
        HelixClient::with_base_url("cid", AccessToken::new("tok").unwrap(), "not a url").unwrap();
    let err = c.delete_chat_message("b", "m", "x").await.unwrap_err();
    assert!(matches!(err, HelixError::InvalidArgument(_)), "{err:?}");
    assert!(!err.is_retryable());
}
