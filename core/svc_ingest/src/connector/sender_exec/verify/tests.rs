#![allow(clippy::unwrap_used, clippy::expect_used)]
//! Verifier tests. The "good" templates are byte-for-byte what the overlay
//! sources (`bundles/rust/{twitch,discord,youtube}/src`) emit; every "bad"
//! case is one mutation of a good template, so each gate is proven to be the
//! one that fires (mutation-checked: removing the gate makes exactly its case
//! pass).

use super::*;
use crate::connector::sender_exec::policy::PlatformPolicy;

fn refs(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    pairs
        .iter()
        .map(|(a, b)| ((*a).to_string(), (*b).to_string()))
        .collect()
}

fn hdrs(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    refs(pairs)
}

pub(crate) fn twitch_send() -> RequestTpl {
    RequestTpl {
        method: "POST".into(),
        url: "https://api.twitch.tv/helix/chat/messages".into(),
        headers: hdrs(&[
            ("Authorization", "Bearer {{TWITCH_TOKEN}}"),
            ("Client-Id", "{{TWITCH_CLIENT_ID}}"),
            ("Content-Type", "application/json"),
        ]),
        body: Some(
            serde_json::json!({
                "broadcaster_id": "{{TWITCH_BROADCASTER_ID}}",
                "sender_id": "{{TWITCH_SENDER_ID}}",
                "message": "hello",
            })
            .to_string()
            .into_bytes(),
        ),
        secret_refs: refs(&[
            ("{{TWITCH_TOKEN}}", "token"),
            ("{{TWITCH_CLIENT_ID}}", "client_id"),
            ("{{TWITCH_BROADCASTER_ID}}", "broadcaster_id"),
            ("{{TWITCH_SENDER_ID}}", "sender_id"),
        ]),
    }
}

pub(crate) fn twitch_delete() -> RequestTpl {
    RequestTpl {
        method: "DELETE".into(),
        url: "https://api.twitch.tv/helix/moderation/chat?broadcaster_id={{TWITCH_BROADCASTER_ID}}&moderator_id={{TWITCH_SENDER_ID}}&message_id=abc-123".into(),
        headers: hdrs(&[
            ("Authorization", "Bearer {{TWITCH_TOKEN}}"),
            ("Client-Id", "{{TWITCH_CLIENT_ID}}"),
        ]),
        body: None,
        secret_refs: refs(&[
            ("{{TWITCH_TOKEN}}", "token"),
            ("{{TWITCH_CLIENT_ID}}", "client_id"),
            ("{{TWITCH_BROADCASTER_ID}}", "broadcaster_id"),
            ("{{TWITCH_SENDER_ID}}", "sender_id"),
        ]),
    }
}

pub(crate) fn discord_send() -> RequestTpl {
    RequestTpl {
        method: "POST".into(),
        url: "https://discord.com/api/v10/channels/{target.channel_id}/messages".into(),
        headers: hdrs(&[("Content-Type", "application/json")]),
        body: Some(
            serde_json::json!({"content": "hi {x} \n there", "allowed_mentions": {"parse": ["users"]}})
                .to_string()
                .into_bytes(),
        ),
        secret_refs: refs(&[("Authorization", "discord-bot")]),
    }
}

pub(crate) fn discord_delete() -> RequestTpl {
    RequestTpl {
        method: "DELETE".into(),
        url: "https://discord.com/api/v10/channels/{target.channel_id}/messages/123456789".into(),
        headers: vec![],
        body: None,
        secret_refs: refs(&[("Authorization", "discord-bot")]),
    }
}

pub(crate) fn youtube_send() -> RequestTpl {
    RequestTpl {
        method: "POST".into(),
        url: "https://www.googleapis.com/youtube/v3/liveChat/messages?part=snippet".into(),
        headers: hdrs(&[
            ("Authorization", "Bearer {{YT_OAUTH_TOKEN}}"),
            ("Content-Type", "application/json"),
        ]),
        body: Some(
            serde_json::json!({"snippet": {
                "liveChatId": "{{YT_LIVE_CHAT_ID}}",
                "type": "textMessageEvent",
                "textMessageDetails": {"messageText": "yo"},
            }})
            .to_string()
            .into_bytes(),
        ),
        secret_refs: refs(&[
            ("{{YT_OAUTH_TOKEN}}", "youtube.oauth"),
            ("{{YT_LIVE_CHAT_ID}}", "youtube.live_chat_id"),
        ]),
    }
}

fn check(
    policy: &PlatformPolicy,
    kind: &str,
    t: &RequestTpl,
) -> Result<VerifiedTemplate, TemplateError> {
    verify(policy, policy.kind(kind).unwrap(), t)
}

fn twitch(kind: &str, t: &RequestTpl) -> Result<VerifiedTemplate, TemplateError> {
    check(&PlatformPolicy::twitch(), kind, t)
}

#[test]
fn every_overlay_template_verifies() {
    assert!(twitch("chat.send", &twitch_send()).is_ok());
    assert!(twitch("chat.delete", &twitch_delete()).is_ok());
    assert!(check(&PlatformPolicy::discord(), "chat.send", &discord_send()).is_ok());
    assert!(check(&PlatformPolicy::discord(), "chat.delete", &discord_delete()).is_ok());
    assert!(check(&PlatformPolicy::youtube(), "chat.send", &youtube_send()).is_ok());
}

#[test]
fn discord_json_body_with_nested_braces_and_lone_brace_text_is_not_a_stray_marker() {
    // `}}` closes the nested object structurally; `{x}` is chat text.
    let t = discord_send();
    let raw = String::from_utf8(t.body.clone().unwrap()).unwrap();
    assert!(raw.ends_with("}}"));
    assert!(check(&PlatformPolicy::discord(), "chat.send", &t).is_ok());
}

fn mutate(base: RequestTpl, f: impl FnOnce(&mut RequestTpl)) -> RequestTpl {
    let mut t = base;
    f(&mut t);
    t
}

#[test]
fn undeclared_slot_is_refused_everywhere_it_can_hide() {
    let evil = "{{TWITCH_ADMIN}}";
    let in_url = mutate(twitch_delete(), |t| t.url = t.url.replace("abc-123", evil));
    let in_hdr = mutate(twitch_send(), |t| {
        t.headers.push(("Accept".into(), evil.into()))
    });
    let in_body = mutate(twitch_send(), |t| {
        t.body = Some(
            serde_json::json!({"broadcaster_id": "{{TWITCH_BROADCASTER_ID}}", "sender_id": "{{TWITCH_SENDER_ID}}", "message": evil})
                .to_string()
                .into_bytes(),
        );
    });
    for (name, t, kind) in [
        ("url", in_url, "chat.delete"),
        ("header", in_hdr, "chat.send"),
        ("body", in_body, "chat.send"),
    ] {
        assert_eq!(
            twitch(kind, &t).unwrap_err(),
            TemplateError::UndeclaredSlot,
            "{name}"
        );
    }
}

#[test]
fn stray_or_unclosed_markers_outside_a_declared_slot_are_refused() {
    for bad in [
        "{{",
        "}}",
        "x}}y",
        "{{TWITCH_SENDER_ID",
        "{{{TWITCH_SENDER_ID}}}",
    ] {
        let t = mutate(twitch_send(), |t| {
            t.body = Some(
                serde_json::json!({"broadcaster_id": "{{TWITCH_BROADCASTER_ID}}", "sender_id": "{{TWITCH_SENDER_ID}}", "message": format!("a{bad}b")})
                    .to_string()
                    .into_bytes(),
            );
        });
        assert!(
            matches!(
                twitch("chat.send", &t),
                Err(TemplateError::StrayMarker | TemplateError::UndeclaredSlot)
            ),
            "{bad}"
        );
    }
    let in_url = mutate(twitch_delete(), |t| {
        t.url = t.url.replace("abc-123", "a}}b")
    });
    assert_eq!(
        twitch("chat.delete", &in_url).unwrap_err(),
        TemplateError::StrayMarker
    );
}

#[test]
fn slot_in_free_text_or_a_key_or_a_wrong_pointer_is_misplaced() {
    let embedded = mutate(twitch_send(), |t| {
        t.body = Some(
            serde_json::json!({"broadcaster_id": "{{TWITCH_BROADCASTER_ID}}", "sender_id": "{{TWITCH_SENDER_ID}}", "message": "hi {{TWITCH_BROADCASTER_ID}}"})
                .to_string()
                .into_bytes(),
        );
    });
    assert_eq!(
        twitch("chat.send", &embedded).unwrap_err(),
        TemplateError::SlotMisplaced
    );
    let wrong_ptr = mutate(twitch_send(), |t| {
        t.body = Some(
            serde_json::json!({"broadcaster_id": "{{TWITCH_SENDER_ID}}", "sender_id": "{{TWITCH_SENDER_ID}}", "message": "m"})
                .to_string()
                .into_bytes(),
        );
    });
    assert_eq!(
        twitch("chat.send", &wrong_ptr).unwrap_err(),
        TemplateError::SlotMisplaced
    );
    let in_key = mutate(twitch_send(), |t| {
        t.body = Some(br#"{"{{TWITCH_SENDER_ID}}":"x","message":"m"}"#.to_vec());
    });
    assert_eq!(
        twitch("chat.send", &in_key).unwrap_err(),
        TemplateError::StrayMarker
    );
    // token in a body position it may not occupy
    let token_in_body = mutate(twitch_send(), |t| {
        t.body = Some(
            serde_json::json!({"broadcaster_id": "{{TWITCH_TOKEN}}", "sender_id": "{{TWITCH_SENDER_ID}}", "message": "m"})
                .to_string()
                .into_bytes(),
        );
    });
    assert_eq!(
        twitch("chat.send", &token_in_body).unwrap_err(),
        TemplateError::SlotMisplaced
    );
}

#[test]
fn slot_not_allowed_for_the_kind_is_refused() {
    // YT_VIDEO_ID is declared for the read path but not for chat.send.
    let t = mutate(youtube_send(), |t| {
        t.url =
            "https://www.googleapis.com/youtube/v3/liveChat/messages?part={{YT_VIDEO_ID}}".into();
        t.secret_refs
            .push(("{{YT_VIDEO_ID}}".into(), "youtube.video_id".into()));
    });
    assert_eq!(
        check(&PlatformPolicy::youtube(), "chat.send", &t).unwrap_err(),
        TemplateError::SlotNotAllowedForKind
    );
}

#[test]
fn control_and_zero_width_characters_are_refused_in_url_header_and_body() {
    for c in [
        '\u{0}',
        '\u{1}',
        '\r',
        '\u{200B}',
        '\u{200D}',
        '\u{202E}',
        '\u{FEFF}',
        '\u{2028}',
        '\u{E0041}',
    ] {
        let url = mutate(twitch_delete(), |t| {
            t.url = t.url.replace("abc-123", &format!("a{c}b"))
        });
        assert_eq!(
            twitch("chat.delete", &url).unwrap_err(),
            TemplateError::InvisibleChar,
            "url {c:?}"
        );
        let hdr = mutate(twitch_send(), |t| {
            t.headers.push(("User-Agent".into(), format!("x{c}y")))
        });
        assert_eq!(
            twitch("chat.send", &hdr).unwrap_err(),
            TemplateError::InvisibleChar,
            "hdr {c:?}"
        );
        let body = mutate(twitch_send(), |t| {
            t.body = Some(
                serde_json::json!({"broadcaster_id": "{{TWITCH_BROADCASTER_ID}}", "sender_id": "{{TWITCH_SENDER_ID}}", "message": format!("a{c}b")})
                    .to_string()
                    .into_bytes(),
            );
        });
        // Body text may legally hold \t and \n only; \r and the rest are refused.
        assert_eq!(
            twitch("chat.send", &body).unwrap_err(),
            TemplateError::InvisibleChar,
            "body {c:?}"
        );
    }
    let ok = mutate(twitch_send(), |t| {
        t.body = Some(
            serde_json::json!({"broadcaster_id": "{{TWITCH_BROADCASTER_ID}}", "sender_id": "{{TWITCH_SENDER_ID}}", "message": "a\nb\tc"})
                .to_string()
                .into_bytes(),
        );
    });
    assert!(twitch("chat.send", &ok).is_ok());
}

#[test]
fn userinfo_scheme_host_fragment_and_port_are_refused() {
    let base = twitch_send();
    let cases: [(&str, TemplateError); 9] = [
        (
            "https://user:pw@api.twitch.tv/helix/chat/messages",
            TemplateError::UserInfo,
        ),
        (
            "https://api.twitch.tv@evil.example/helix/chat/messages",
            TemplateError::UserInfo,
        ),
        (
            "http://api.twitch.tv/helix/chat/messages",
            TemplateError::BadScheme,
        ),
        (
            "wss://api.twitch.tv/helix/chat/messages",
            TemplateError::BadScheme,
        ),
        (
            "ftp://api.twitch.tv/helix/chat/messages",
            TemplateError::BadScheme,
        ),
        (
            "https://evil.example/helix/chat/messages",
            TemplateError::HostNotAllowed,
        ),
        (
            "https://api.twitch.tv.evil.example/helix/chat/messages",
            TemplateError::HostNotAllowed,
        ),
        (
            "https://api.twitch.tv:8443/helix/chat/messages",
            TemplateError::HostNotAllowed,
        ),
        (
            "https://api.twitch.tv/helix/chat/messages#frag",
            TemplateError::Fragment,
        ),
    ];
    for (url, want) in cases {
        let t = mutate(base.clone(), |t| t.url = url.to_string());
        assert_eq!(twitch("chat.send", &t).unwrap_err(), want, "{url}");
    }
    let no_scheme = mutate(base.clone(), |t| t.url = "api.twitch.tv/helix".into());
    assert_eq!(
        twitch("chat.send", &no_scheme).unwrap_err(),
        TemplateError::BadUrl
    );
    let long = mutate(base, |t| {
        t.url = format!("https://api.twitch.tv/{}", "a".repeat(3000))
    });
    assert_eq!(
        twitch("chat.send", &long).unwrap_err(),
        TemplateError::BadUrl
    );
}

#[test]
fn host_is_matched_case_insensitively_but_only_exactly() {
    let t = mutate(twitch_send(), |t| {
        t.url = "https://API.Twitch.TV/helix/chat/messages".into()
    });
    assert!(twitch("chat.send", &t).is_ok());
}

#[test]
fn manifest_hosts_only_narrow_never_widen() {
    let p = PlatformPolicy::twitch().with_manifest_hosts(&["api.twitch.tv", "evil.example"]);
    assert_eq!(p.hosts, vec!["api.twitch.tv".to_string()]);
    let none = PlatformPolicy::twitch().with_manifest_hosts(&["other.example"]);
    assert!(none.hosts.is_empty());
    assert_eq!(
        check(&none, "chat.send", &twitch_send()).unwrap_err(),
        TemplateError::HostNotAllowed
    );
}

#[test]
fn path_and_query_must_match_the_kind_exactly() {
    let other_endpoint = mutate(twitch_send(), |t| {
        t.url = "https://api.twitch.tv/helix/users".into()
    });
    assert_eq!(
        twitch("chat.send", &other_endpoint).unwrap_err(),
        TemplateError::PathMismatch
    );
    let traversal = mutate(twitch_send(), |t| {
        t.url = "https://api.twitch.tv/helix/chat/../chat/messages".into()
    });
    assert_eq!(
        twitch("chat.send", &traversal).unwrap_err(),
        TemplateError::PathMismatch
    );
    let extra_q = mutate(twitch_send(), |t| t.url.push_str("?x=1"));
    assert_eq!(
        twitch("chat.send", &extra_q).unwrap_err(),
        TemplateError::QueryMismatch
    );
    let missing_q = mutate(twitch_delete(), |t| {
        t.url = t.url.replace("&message_id=abc-123", "")
    });
    assert_eq!(
        twitch("chat.delete", &missing_q).unwrap_err(),
        TemplateError::QueryMismatch
    );
    let dup_q = mutate(twitch_delete(), |t| t.url.push_str("&message_id=zzz"));
    assert_eq!(
        twitch("chat.delete", &dup_q).unwrap_err(),
        TemplateError::QueryMismatch
    );
    let bad_val = mutate(twitch_delete(), |t| {
        t.url = t.url.replace("abc-123", "a%2Fb")
    });
    assert_eq!(
        twitch("chat.delete", &bad_val).unwrap_err(),
        TemplateError::QueryValueInvalid
    );
    let amp_val = mutate(twitch_delete(), |t| t.url = t.url.replace("abc-123", "a=b"));
    assert!(twitch("chat.delete", &amp_val).is_err());
    let mixed = mutate(twitch_delete(), |t| {
        t.url = t
            .url
            .replace("{{TWITCH_SENDER_ID}}", "x{{TWITCH_SENDER_ID}}")
    });
    assert_eq!(
        twitch("chat.delete", &mixed).unwrap_err(),
        TemplateError::QueryValueInvalid
    );
    let slot_wrong_key = mutate(twitch_delete(), |t| {
        t.url = "https://api.twitch.tv/helix/moderation/chat?broadcaster_id={{TWITCH_SENDER_ID}}&moderator_id={{TWITCH_SENDER_ID}}&message_id=a".into()
    });
    assert_eq!(
        twitch("chat.delete", &slot_wrong_key).unwrap_err(),
        TemplateError::SlotMisplaced
    );
    let d = PlatformPolicy::discord();
    let wrong_chan_slot = mutate(discord_send(), |t| {
        t.url = "https://discord.com/api/v10/channels/12345/messages".into()
    });
    assert_eq!(
        check(&d, "chat.send", &wrong_chan_slot).unwrap_err(),
        TemplateError::PathMismatch
    );
    let snowflake_hostile = mutate(discord_delete(), |t| {
        t.url = t.url.replace("123456789", "1/../2")
    });
    assert_eq!(
        check(&d, "chat.delete", &snowflake_hostile).unwrap_err(),
        TemplateError::PathMismatch
    );
}

#[test]
fn method_must_match_the_kind() {
    let t = mutate(twitch_send(), |t| t.method = "PUT".into());
    assert_eq!(
        twitch("chat.send", &t).unwrap_err(),
        TemplateError::BadMethod
    );
    let t = mutate(twitch_delete(), |t| t.method = "GET".into());
    assert_eq!(
        twitch("chat.delete", &t).unwrap_err(),
        TemplateError::BadMethod
    );
}

#[test]
fn authorization_may_only_come_from_a_credential_slot() {
    let literal = mutate(twitch_send(), |t| {
        t.headers[0] = ("Authorization".into(), "Bearer sk-live-123".into())
    });
    assert_eq!(
        twitch("chat.send", &literal).unwrap_err(),
        TemplateError::AuthorizationFromTemplate
    );
    let wrong_prefix = mutate(twitch_send(), |t| {
        t.headers[0] = ("Authorization".into(), "Basic {{TWITCH_TOKEN}}".into())
    });
    assert_eq!(
        twitch("chat.send", &wrong_prefix).unwrap_err(),
        TemplateError::AuthorizationFromTemplate
    );
    let non_secret_slot = mutate(twitch_send(), |t| {
        t.headers[0] = (
            "Authorization".into(),
            "Bearer {{TWITCH_BROADCASTER_ID}}".into(),
        )
    });
    assert_eq!(
        twitch("chat.send", &non_secret_slot).unwrap_err(),
        TemplateError::AuthorizationFromTemplate
    );
    let suffixed = mutate(twitch_send(), |t| {
        t.headers[0] = ("authorization".into(), "Bearer {{TWITCH_TOKEN}}x".into())
    });
    assert_eq!(
        twitch("chat.send", &suffixed).unwrap_err(),
        TemplateError::AuthorizationFromTemplate
    );
    // Discord: any template-set Authorization (the credential rides secret-refs).
    let d = mutate(discord_send(), |t| {
        t.headers.push(("Authorization".into(), "Bot abc".into()))
    });
    assert_eq!(
        check(&PlatformPolicy::discord(), "chat.send", &d).unwrap_err(),
        TemplateError::AuthorizationFromTemplate
    );
    let tok_in_client_id = mutate(twitch_send(), |t| {
        t.headers[1] = ("Client-Id".into(), "{{TWITCH_TOKEN}}".into())
    });
    assert_eq!(
        twitch("chat.send", &tok_in_client_id).unwrap_err(),
        TemplateError::SlotMisplaced
    );
}

#[test]
fn forbidden_and_unlisted_headers_are_refused() {
    for name in [
        "Host",
        "Cookie",
        "Set-Cookie",
        "Proxy-Authorization",
        "Proxy-Foo",
        "X-Forwarded-For",
        "Forwarded",
        "X-Real-Ip",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Upgrade",
        "TE",
        "Expect",
        "X-HTTP-Method-Override",
        "X-Waddle-Host-Dm-Recipient",
        "X-Custom",
    ] {
        let t = mutate(twitch_send(), |t| t.headers.push((name.into(), "v".into())));
        assert_eq!(
            twitch("chat.send", &t).unwrap_err(),
            TemplateError::HeaderNotAllowed,
            "{name}"
        );
    }
    let dup = mutate(twitch_send(), |t| {
        t.headers
            .push(("content-type".into(), "application/json".into()))
    });
    assert_eq!(
        twitch("chat.send", &dup).unwrap_err(),
        TemplateError::DuplicateHeader
    );
    let many = mutate(twitch_send(), |t| {
        for i in 0..20 {
            t.headers.push((format!("accept-{i}"), "v".into()));
        }
    });
    assert_eq!(
        twitch("chat.send", &many).unwrap_err(),
        TemplateError::TooManyHeaders
    );
    let bad_name = mutate(twitch_send(), |t| {
        t.headers.push(("Bad Name".into(), "v".into()))
    });
    assert_eq!(
        twitch("chat.send", &bad_name).unwrap_err(),
        TemplateError::HeaderNotAllowed
    );
    let ct = mutate(twitch_send(), |t| {
        t.headers[2] = ("Content-Type".into(), "text/plain".into())
    });
    assert_eq!(
        twitch("chat.send", &ct).unwrap_err(),
        TemplateError::HeaderValueInvalid
    );
    let slot_mixed = mutate(twitch_send(), |t| {
        t.headers[1] = ("Client-Id".into(), "x{{TWITCH_CLIENT_ID}}".into())
    });
    assert_eq!(
        twitch("chat.send", &slot_mixed).unwrap_err(),
        TemplateError::HeaderValueInvalid
    );
    let big = mutate(twitch_send(), |t| {
        t.headers.push(("Accept".into(), "a".repeat(600)))
    });
    assert_eq!(
        twitch("chat.send", &big).unwrap_err(),
        TemplateError::HeaderValueInvalid
    );
    let non_ascii = mutate(twitch_send(), |t| {
        t.headers.push(("Accept".into(), "caf\u{e9}".into()))
    });
    assert_eq!(
        twitch("chat.send", &non_ascii).unwrap_err(),
        TemplateError::HeaderValueInvalid
    );
}

#[test]
fn secret_refs_must_match_declarations_exactly() {
    let unknown = mutate(twitch_send(), |t| {
        t.secret_refs.push(("{{EVIL}}".into(), "token".into()))
    });
    assert_eq!(
        twitch("chat.send", &unknown).unwrap_err(),
        TemplateError::SecretRefUnknown
    );
    let bare = mutate(twitch_send(), |t| {
        t.secret_refs.push(("token".into(), "token".into()))
    });
    assert_eq!(
        twitch("chat.send", &bare).unwrap_err(),
        TemplateError::SecretRefUnknown
    );
    let swapped = mutate(twitch_send(), |t| t.secret_refs[0].1 = "sender_id".into());
    assert_eq!(
        twitch("chat.send", &swapped).unwrap_err(),
        TemplateError::SecretRefHandleMismatch
    );
    let dup = mutate(twitch_send(), |t| {
        t.secret_refs.push(t.secret_refs[0].clone())
    });
    assert_eq!(
        twitch("chat.send", &dup).unwrap_err(),
        TemplateError::SecretRefDuplicate
    );
    let missing = mutate(twitch_send(), |t| {
        t.secret_refs.retain(|(s, _)| s != "{{TWITCH_TOKEN}}")
    });
    assert_eq!(
        twitch("chat.send", &missing).unwrap_err(),
        TemplateError::SecretRefMissing
    );
    // Discord: the header credential is mandatory and pinned to its handle.
    let d = PlatformPolicy::discord();
    let none = mutate(discord_send(), |t| t.secret_refs.clear());
    assert_eq!(
        check(&d, "chat.send", &none).unwrap_err(),
        TemplateError::SecretRefMissing
    );
    let wrong = mutate(discord_send(), |t| {
        t.secret_refs = refs(&[("Authorization", "other-bot")])
    });
    assert_eq!(
        check(&d, "chat.send", &wrong).unwrap_err(),
        TemplateError::SecretRefHandleMismatch
    );
    // A secret-ref for a slot the kind may not use.
    let yt = mutate(youtube_send(), |t| {
        t.secret_refs
            .push(("{{YT_VIDEO_ID}}".into(), "youtube.video_id".into()))
    });
    assert_eq!(
        check(&PlatformPolicy::youtube(), "chat.send", &yt).unwrap_err(),
        TemplateError::SecretRefUnknown
    );
}

#[test]
fn body_presence_size_shape_and_content_type_are_enforced() {
    let del_body = mutate(twitch_delete(), |t| t.body = Some(b"{}".to_vec()));
    assert_eq!(
        twitch("chat.delete", &del_body).unwrap_err(),
        TemplateError::BodyPresence
    );
    let send_nobody = mutate(twitch_send(), |t| t.body = None);
    assert_eq!(
        twitch("chat.send", &send_nobody).unwrap_err(),
        TemplateError::BodyPresence
    );
    let huge = mutate(twitch_send(), |t| t.body = Some(vec![b' '; 5000]));
    assert_eq!(
        twitch("chat.send", &huge).unwrap_err(),
        TemplateError::BodyTooLarge
    );
    let not_json = mutate(twitch_send(), |t| t.body = Some(b"nope".to_vec()));
    assert_eq!(
        twitch("chat.send", &not_json).unwrap_err(),
        TemplateError::BodyNotJson
    );
    let array = mutate(twitch_send(), |t| t.body = Some(b"[1]".to_vec()));
    assert_eq!(
        twitch("chat.send", &array).unwrap_err(),
        TemplateError::BodyNotJson
    );
    let deep = mutate(twitch_send(), |t| {
        t.body = Some(format!("{}1{}", r#"{"a":"#.repeat(12), "}".repeat(12)).into_bytes())
    });
    assert_eq!(
        twitch("chat.send", &deep).unwrap_err(),
        TemplateError::BodyNotJson
    );
    let wide = mutate(twitch_send(), |t| {
        let arr: Vec<u8> = vec![0; 600];
        t.body = Some(serde_json::json!({ "a": arr }).to_string().into_bytes());
    });
    assert_eq!(
        twitch("chat.send", &wide).unwrap_err(),
        TemplateError::BodyNotJson
    );
    let no_ct = mutate(twitch_send(), |t| {
        t.headers.retain(|(n, _)| n != "Content-Type")
    });
    assert_eq!(
        twitch("chat.send", &no_ct).unwrap_err(),
        TemplateError::BodyContentType
    );
    let lower_ct = mutate(twitch_send(), |t| t.headers[2].0 = "content-type".into());
    assert!(twitch("chat.send", &lower_ct).is_ok());
}

#[test]
fn array_elements_and_pointer_escaping_are_walked() {
    let t = mutate(twitch_send(), |t| {
        t.body = Some(
            serde_json::json!({"broadcaster_id": "{{TWITCH_BROADCASTER_ID}}", "sender_id": "{{TWITCH_SENDER_ID}}", "message": "m", "extra": ["{{TWITCH_TOKEN}}"]})
                .to_string()
                .into_bytes(),
        );
    });
    assert_eq!(
        twitch("chat.send", &t).unwrap_err(),
        TemplateError::SlotMisplaced
    );
    assert_eq!(escape_pointer("a/b~c"), "a~1b~0c");
}

#[test]
fn used_slots_are_recorded_for_resolution() {
    let v = twitch("chat.send", &twitch_send()).unwrap();
    assert_eq!(v.used.len(), 4);
    assert_eq!(v.body_slots.len(), 2);
    let v = check(&PlatformPolicy::discord(), "chat.send", &discord_send()).unwrap();
    assert_eq!(v.used.len(), 1);
    assert_eq!(v.header_creds.len(), 1);
}

#[test]
fn every_error_has_a_distinct_nonempty_code_and_message() {
    use std::collections::BTreeSet;
    let all = [
        TemplateError::BadMethod,
        TemplateError::BadUrl,
        TemplateError::BadScheme,
        TemplateError::UserInfo,
        TemplateError::Fragment,
        TemplateError::HostNotAllowed,
        TemplateError::PathMismatch,
        TemplateError::QueryMismatch,
        TemplateError::QueryValueInvalid,
        TemplateError::StrayMarker,
        TemplateError::UndeclaredSlot,
        TemplateError::SlotNotAllowedForKind,
        TemplateError::SlotMisplaced,
        TemplateError::InvisibleChar,
        TemplateError::HeaderNotAllowed,
        TemplateError::HeaderValueInvalid,
        TemplateError::DuplicateHeader,
        TemplateError::TooManyHeaders,
        TemplateError::AuthorizationFromTemplate,
        TemplateError::SecretRefUnknown,
        TemplateError::SecretRefHandleMismatch,
        TemplateError::SecretRefDuplicate,
        TemplateError::SecretRefMissing,
        TemplateError::BodyPresence,
        TemplateError::BodyTooLarge,
        TemplateError::BodyNotJson,
        TemplateError::BodyContentType,
    ];
    let codes: BTreeSet<_> = all.iter().map(|e| e.code()).collect();
    assert_eq!(codes.len(), all.len());
    assert!(all.iter().all(|e| !e.to_string().is_empty()));
}
