//! Unit tests for the YouTube profile derivation (no network).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use super::*;

const CHAT: &str = "Cg0KC-abc_123";

fn env() -> RuntimeEnv {
    RuntimeEnv {
        environment: "alpha".to_owned(),
        max_frame_bytes: 262_144,
        egress_allow: Vec::new(),
        heartbeat_enabled: false,
    }
}

fn binding(meta: &[(&str, &str)]) -> BindingSummary {
    BindingSummary {
        tenant_id: 1,
        secret_version: 1,
        community_id: 7,
        connection_id: 12,
        provider: "youtube".to_owned(),
        kind: ConnectionKind::PlatformOauth,
        metadata: meta
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect(),
    }
}

fn good() -> BindingSummary {
    binding(&[("live_chat_id", CHAT), ("tenant_id", "3")])
}

fn poll_of(d: &DesiredConnection) -> &PollConfig {
    match &d.plan {
        TransportPlan::Poll(c) => c,
        _ => panic!("youtube must derive a poll plan"),
    }
}

fn one(b: &BindingSummary) -> DesiredConnection {
    let mut v = YoutubeProfile::new().plan(b, &env()).unwrap();
    assert_eq!(v.len(), 1, "exactly one desired connection");
    v.remove(0)
}

#[test]
fn identity_constants() {
    let p = YoutubeProfile::new();
    assert_eq!(p.platform(), "youtube");
    assert_eq!(p.app_id(), "waddles.core.service.youtube");
    assert_eq!(p.provider(), "youtube");
    assert_eq!(p.capability(), "chat_ingest");
}

#[test]
fn derived_poll_config_golden() {
    let d = one(&good());
    assert_eq!(
        d.key,
        ConnKey {
            platform: "youtube",
            community_id: 7,
            connection_id: 12
        }
    );
    assert_eq!(d.tenant_id, 1, "the feed's tenant, not a placeholder");
    let c = poll_of(&d);
    assert_eq!(c.platform, "youtube");
    assert_eq!(
        c.url,
        "https://www.googleapis.com/youtube/v3/liveChat/messages?part=id%2Csnippet%2CauthorDetails&liveChatId=Cg0KC-abc_123&maxResults=200"
    );
    assert_eq!(c.source_key, "youtube:livechat:Cg0KC-abc_123");
    assert_eq!(c.handles.len(), 1);
    assert_eq!(
        c.handles["youtube.oauth"],
        ConnectionRef {
            community_id: 7,
            connection_id: 12
        }
    );
    let a = c.auth.as_ref().expect("bearer auth");
    assert_eq!(
        (a.header.as_str(), a.prefix.as_str(), a.handle.as_str()),
        ("Authorization", "Bearer ", "youtube.oauth")
    );
    assert_eq!(c.cursor_param.as_deref(), Some("pageToken"));
    assert_eq!(c.cursor_pointer.as_deref(), Some("/nextPageToken"));
    assert_eq!(c.initial_cursor, None);
    assert_eq!(c.interval, Duration::from_secs(5));
    assert_eq!(c.request_timeout, Duration::from_secs(15));
    assert_eq!(c.max_body_bytes, 262_144);
    assert_eq!(c.max_drop_attempts, 3);
    assert!(c.egress_allow.is_empty());
    assert!(!c.allow_loopback_insecure);
    // Independent literal, not the constructor under test.
    let q = |r: &[&str]| r.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
    let expected = PollLifecycle {
        interval_hint: Some(IntervalHint {
            pointer: "/pollingIntervalMillis".to_owned(),
            min: Duration::from_secs(2),
            max: Duration::from_secs(60),
        }),
        error_rules: vec![
            HttpErrorRule {
                status: 403,
                reason_pointer: "/error/errors/0/reason".to_owned(),
                reasons: q(&[
                    "quotaExceeded",
                    "rateLimitExceeded",
                    "dailyLimitExceeded",
                    "userRateLimitExceeded",
                ]),
                action: HttpAction::Backoff {
                    base: Duration::from_secs(30),
                    max: Duration::from_secs(3600),
                    honor_retry_after: true,
                },
            },
            HttpErrorRule {
                status: 403,
                reason_pointer: "/error/errors/0/reason".to_owned(),
                reasons: q(&["liveChatEnded"]),
                action: HttpAction::Rediscover,
            },
            HttpErrorRule {
                status: 403,
                reason_pointer: "/error/errors/0/reason".to_owned(),
                reasons: q(&["forbidden", "insufficientPermissions"]),
                action: HttpAction::Park,
            },
        ],
        query_secret: None,
    };
    assert_eq!(c.lifecycle, expected);
}

#[test]
fn env_values_flow_into_config() {
    let mut e = env();
    e.max_frame_bytes = 1234;
    e.egress_allow = vec!["10.1.2.3".parse().unwrap()];
    let v = YoutubeProfile::new().plan(&good(), &e).unwrap();
    let c = poll_of(&v[0]);
    assert_eq!(c.max_body_bytes, 1234);
    assert_eq!(c.egress_allow, e.egress_allow);
}

#[test]
fn quota_and_park_rules_classify_real_bodies() {
    let l = lifecycle();
    let body = |r: &str| format!(r#"{{"error":{{"errors":[{{"reason":"{r}"}}]}}}}"#);
    for r in QUOTA_REASONS {
        let rule = l.classify(403, body(r).as_bytes()).expect(r);
        assert!(matches!(rule.action, HttpAction::Backoff { .. }), "{r}");
    }
    for r in PARK_REASONS {
        let rule = l.classify(403, body(r).as_bytes()).expect(r);
        assert_eq!(rule.action, HttpAction::Park, "{r}");
    }
    assert!(l.classify(403, body("somethingElse").as_bytes()).is_none());
    assert!(l.classify(500, body("quotaExceeded").as_bytes()).is_none());
}

#[test]
fn interval_hint_clamps_to_two_and_sixty_seconds() {
    let l = lifecycle();
    let ms = |n: u64| format!(r#"{{"pollingIntervalMillis":{n}}}"#);
    assert_eq!(
        l.next_interval(ms(500).as_bytes()),
        Some((Duration::from_secs(2), true))
    );
    assert_eq!(
        l.next_interval(ms(7000).as_bytes()),
        Some((Duration::from_secs(7), false))
    );
    assert_eq!(
        l.next_interval(ms(600_000).as_bytes()),
        Some((Duration::from_secs(60), true))
    );
}

#[test]
fn missing_live_chat_id_yields_no_plan_and_is_counted() {
    let p = YoutubeProfile::new();
    let b = binding(&[("tenant_id", "3"), ("channel_id", "UCabc")]);
    for n in 1..=3u64 {
        assert!(p.plan(&b, &env()).unwrap().is_empty());
        assert_eq!(p.skipped_incomplete(), n);
    }
}

#[test]
fn hostile_live_chat_ids_are_rejected_and_never_reach_a_url() {
    let long = "a".repeat(129);
    let hostile = [
        "a&b",
        "a=b",
        "a#b",
        "%41",
        "a/b",
        "a b",
        "caf\u{e9}",
        "a\r\nb",
        long.as_str(),
        "",
        "a?b",
        "a+b",
        "{{YT_OAUTH_TOKEN}}",
        "a\u{200b}b",
        "a\0b",
        "../x",
    ];
    let p = YoutubeProfile::new();
    let mut examined = 0;
    for h in hostile {
        let b = binding(&[("live_chat_id", h), ("tenant_id", "3")]);
        let r = p.plan(&b, &env());
        assert_eq!(
            r.err(),
            Some(PlanError::InvalidValue("live_chat_id")),
            "{h:?}"
        );
        examined += 1;
    }
    assert_eq!(examined, hostile.len());
    assert!(examined > 10);
    assert_eq!(p.skipped_incomplete(), 0, "present-but-bad is malformed");
    // 128 chars is the inclusive bound.
    let edge = "a".repeat(128);
    let b = binding(&[("live_chat_id", edge.as_str()), ("tenant_id", "3")]);
    assert!(poll_of(&one(&b))
        .url
        .contains(&format!("liveChatId={edge}&")));
}

#[test]
fn plan_error_never_echoes_the_value() {
    let b = binding(&[("live_chat_id", "SECRET&x=1"), ("tenant_id", "3")]);
    let e = YoutubeProfile::new().plan(&b, &env()).err().unwrap();
    assert!(!e.to_string().contains("SECRET"));
    assert!(!format!("{e:?}").contains("SECRET"));
}

#[test]
fn wrong_provider_and_kind_are_typed_errors() {
    let mut b = good();
    b.provider = "twitch".to_owned();
    assert_eq!(
        YoutubeProfile::new().plan(&b, &env()).err(),
        Some(PlanError::WrongProvider)
    );
    let mut b = good();
    b.kind = ConnectionKind::ExternalHttp;
    assert_eq!(
        YoutubeProfile::new().plan(&b, &env()).err(),
        Some(PlanError::WrongKind)
    );
}

#[test]
fn tenant_is_the_feed_tenant_and_never_editable_metadata() {
    for t in ["3", "999", "0", "-1", "abc", ""] {
        let b = binding(&[("live_chat_id", CHAT), ("tenant_id", t)]);
        let d = one(&b);
        assert_eq!(d.tenant_id, 1, "metadata tenant {t:?} ignored");
        assert_eq!(d.fingerprint, one(&good()).fingerprint);
    }
    let mut moved = good();
    moved.tenant_id = 9;
    assert_eq!(one(&moved).tenant_id, 9);
    assert_ne!(one(&moved).fingerprint, one(&good()).fingerprint);
}

#[test]
fn a_feed_row_without_a_positive_tenant_or_secret_version_is_refused() {
    let p = YoutubeProfile::new();
    for bad in [0_i64, -3] {
        let mut b = good();
        b.tenant_id = bad;
        assert_eq!(
            p.plan(&b, &env()).err(),
            Some(PlanError::Config("binding tenant is not positive"))
        );
        let mut b = good();
        b.secret_version = bad;
        assert_eq!(
            p.plan(&b, &env()).err(),
            Some(PlanError::Config("binding secret version is not positive"))
        );
    }
}

#[test]
fn binding_data_cannot_change_url_host_auth_or_rules() {
    let baseline = one(&good());
    let base = poll_of(&baseline);
    let meta = [
        ("live_chat_id", CHAT),
        ("tenant_id", "3"),
        ("url", "https://evil.example/steal"),
        ("host", "evil.example"),
        ("endpoint", "http://169.254.169.254/"),
        ("auth_header", "X-Evil"),
        ("auth_prefix", "Basic "),
        ("handle", "youtube.apikey"),
        ("query_secret", "key"),
        ("error_rules", "[]"),
        ("interval_ms", "1"),
        ("egress_allow", "10.0.0.1"),
        ("allow_loopback_insecure", "true"),
        ("cursor_param", "evil"),
        ("part", "evil"),
        ("maxResults", "9999"),
    ];
    let d = one(&binding(&meta));
    let c = poll_of(&d);
    assert_eq!(c.url, base.url);
    assert_eq!(c.lifecycle, base.lifecycle);
    assert_eq!(c.auth.as_ref().unwrap().header, "Authorization");
    assert_eq!(c.auth.as_ref().unwrap().prefix, "Bearer ");
    assert_eq!(c.auth.as_ref().unwrap().handle, "youtube.oauth");
    assert_eq!(c.cursor_param, base.cursor_param);
    assert_eq!(c.interval, base.interval);
    assert!(c.egress_allow.is_empty());
    assert!(!c.allow_loopback_insecure);
    assert_eq!(c.handles.keys().collect::<Vec<_>>(), vec!["youtube.oauth"]);
    assert_eq!(d.fingerprint, baseline.fingerprint, "unrelated keys inert");
}

#[test]
fn url_host_is_the_constant_for_every_valid_id() {
    for id in ["a", "Z", "-", "_", "KicKGFVD-x_y", "0123456789"] {
        let b = binding(&[("live_chat_id", id), ("tenant_id", "1")]);
        let d = one(&b);
        let u = reqwest::Url::parse(&poll_of(&d).url).unwrap();
        assert_eq!(u.host_str(), Some("www.googleapis.com"));
        assert_eq!(u.path(), "/youtube/v3/liveChat/messages");
        let pairs: Vec<_> = u
            .query_pairs()
            .map(|(k, v)| (k.into_owned(), v.into_owned()))
            .collect();
        assert_eq!(
            pairs,
            vec![
                ("part".to_owned(), "id,snippet,authorDetails".to_owned()),
                ("liveChatId".to_owned(), id.to_owned()),
                ("maxResults".to_owned(), "200".to_owned()),
            ]
        );
    }
}

#[test]
fn fingerprint_changes_on_chat_or_secret_version_only() {
    let base = one(&good()).fingerprint;
    assert_eq!(one(&good()).fingerprint, base, "deterministic");
    let chat2 = binding(&[("live_chat_id", "OtherChat_1"), ("tenant_id", "3")]);
    assert_ne!(one(&chat2).fingerprint, base, "new live_chat_id restarts");
    let mut v2 = good();
    v2.secret_version = 2;
    let mut v3 = good();
    v3.secret_version = 3;
    assert_ne!(one(&v2).fingerprint, base, "a rotation restarts");
    assert_ne!(
        one(&v2).fingerprint,
        one(&v3).fingerprint,
        "each rotation restarts"
    );
    // The old metadata marker is inert: only the feed counter counts.
    let marker = binding(&[
        ("live_chat_id", CHAT),
        ("tenant_id", "3"),
        ("secret_version", "77"),
    ]);
    assert_eq!(one(&marker).fingerprint, base, "metadata marker is inert");
    let noise = binding(&[
        ("live_chat_id", CHAT),
        ("tenant_id", "3"),
        ("channel_id", "UCzzz"),
        ("scopes", "a b"),
    ]);
    assert_eq!(one(&noise).fingerprint, base, "unrelated metadata is inert");
}

#[test]
fn fingerprint_distinguishes_connections() {
    let mut other = good();
    other.connection_id = 13;
    assert_ne!(one(&other).fingerprint, one(&good()).fingerprint);
}

#[test]
fn lifecycle_constant_validates() {
    let cfg = poll_of(&one(&good())).clone();
    let url = reqwest::Url::parse(&cfg.url).unwrap();
    cfg.lifecycle
        .validate(&cfg.handles, cfg.cursor_param.as_deref(), &url)
        .expect("host constants form a valid lifecycle");
}

// ------------------------------------------------------ rediscovery ----

use crate::connector::{Rediscover, RediscoverError, RediscoverOutcome};

fn key() -> ConnKey {
    ConnKey {
        platform: PLATFORM,
        community_id: 7,
        connection_id: 12,
    }
}

fn hook(
    channel: Option<&str>,
    overrides: &Arc<LiveChatOverrides>,
    current: &str,
) -> YoutubeRediscover {
    YoutubeRediscover::new(
        key(),
        CHAT.to_owned(),
        current.to_owned(),
        channel.map(str::to_owned),
        Arc::clone(overrides),
    )
}

fn broadcasts(items: &[(&str, &str)]) -> Vec<u8> {
    let items: Vec<_> = items
        .iter()
        .map(|(chat, ch)| serde_json::json!({"snippet": {"liveChatId": chat, "channelId": ch}}))
        .collect();
    serde_json::to_vec(&serde_json::json!({ "items": items })).unwrap()
}

#[test]
fn rediscovery_lifecycle_and_urls_are_host_constants() {
    let d = one(&good());
    let cfg = poll_of(&d);
    let rd = cfg.rediscover.as_ref().expect("youtube always rediscovers");
    let url = reqwest::Url::parse(&rd.url).unwrap();
    assert_eq!(url.host_str(), Some("www.googleapis.com"));
    assert_eq!(url.path(), "/youtube/v3/liveBroadcasts");
    let q: HashMap<_, _> = url.query_pairs().into_owned().collect();
    assert_eq!(q["broadcastStatus"], "active");
    assert_eq!(q["part"], "snippet");
    assert_eq!(q["maxResults"], "5");
    assert!(
        !rd.url.contains(CHAT),
        "no binding value reaches the discovery url"
    );
    let ended = cfg
        .lifecycle
        .error_rules
        .iter()
        .find(|r| r.reasons == ["liveChatEnded"])
        .expect("a liveChatEnded rule");
    assert_eq!(ended.action, HttpAction::Rediscover);
    assert!(
        !format!("{rd:?}").contains("googleapis"),
        "Debug hides urls"
    );
}

#[test]
fn the_hook_picks_the_first_live_chat_of_the_bound_channel() {
    let t = Arc::new(LiveChatOverrides::new());
    let h = hook(Some("UCme"), &t, CHAT);
    let body = broadcasts(&[
        ("DecoyOther_1", "UCother"),
        (CHAT, "UCme"),
        ("NewLive_2", "UCme"),
        ("NewLive_3", "UCme"),
    ]);
    assert_eq!(h.apply(&body), Ok(RediscoverOutcome::Rediscovered));
    assert_eq!(
        h.take_outcome(),
        Some(RediscoverOutcome::Rediscovered),
        "other channel skipped, the ended chat skipped"
    );
    assert_eq!(h.take_outcome(), None, "outcome is cleared on read");
    assert_eq!(t.effective(&key(), CHAT), "NewLive_2");
}

#[test]
fn the_hook_waits_when_nothing_acceptable_is_live() {
    let t = Arc::new(LiveChatOverrides::new());
    let h = hook(Some("UCme"), &t, CHAT);
    for body in [
        broadcasts(&[]),
        broadcasts(&[("DecoyOther_1", "UCother")]),
        broadcasts(&[(CHAT, "UCme")]),
        broadcasts(&[("bad chat id!", "UCme")]),
        br#"{"items":[{"snippet":{}},{}]}"#.to_vec(),
        b"{}".to_vec(),
    ] {
        assert_eq!(h.apply(&body), Ok(RediscoverOutcome::WaitingForLive));
        assert_eq!(h.take_outcome(), Some(RediscoverOutcome::WaitingForLive));
    }
    assert!(t.is_empty(), "nothing recorded while waiting");
}

#[test]
fn without_a_pinned_channel_the_credential_scope_decides() {
    let t = Arc::new(LiveChatOverrides::new());
    let h = hook(None, &t, CHAT);
    assert_eq!(
        h.apply(&broadcasts(&[("AnyLive_1", "UCwhoever")])),
        Ok(RediscoverOutcome::Rediscovered)
    );
    assert_eq!(t.effective(&key(), CHAT), "AnyLive_1");
}

#[test]
fn a_malformed_response_is_an_error_not_a_wait() {
    let t = Arc::new(LiveChatOverrides::new());
    let h = hook(None, &t, CHAT);
    for body in [&b"not json"[..], b"", br#"{"items":"x"}"#] {
        assert_eq!(h.apply(body), Err(RediscoverError::Malformed));
    }
    assert_eq!(h.take_outcome(), None);
}

#[test]
fn overrides_apply_only_to_their_binding_and_are_bounded() {
    let t = Arc::new(LiveChatOverrides::new());
    t.record(&key(), CHAT, "NewLive_2").unwrap();
    assert_eq!(t.effective(&key(), CHAT), "NewLive_2");
    // hub-api re-pinned the binding: the stale override is dropped.
    assert_eq!(t.effective(&key(), "Repinned_9"), "Repinned_9");
    assert!(t.is_empty());
    assert_eq!(t.effective(&key(), CHAT), CHAT, "stays dropped");
    // Another connection is untouched.
    let mut other = key();
    other.connection_id = 13;
    t.record(&key(), CHAT, "NewLive_2").unwrap();
    assert_eq!(t.effective(&other, CHAT), CHAT);
    // Bounded: a new key past the cap is refused, an existing one updates.
    for i in 0..(rediscover::MAX_OVERRIDES as i64) {
        let mut k = key();
        k.connection_id = 1_000 + i;
        let _ = t.record(&k, CHAT, "X");
    }
    assert_eq!(t.len(), rediscover::MAX_OVERRIDES);
    let mut fresh = key();
    fresh.connection_id = 999_999;
    assert_eq!(t.record(&fresh, CHAT, "Y"), Err(RediscoverError::Refused));
    t.record(&key(), CHAT, "Updated_1").unwrap();
    assert_eq!(t.effective(&key(), CHAT), "Updated_1");
}

#[test]
fn the_plan_follows_a_recorded_override_and_restarts_on_it() {
    let p = YoutubeProfile::new();
    let first = p.plan(&good(), &env()).unwrap().remove(0);
    p.overrides().record(&key(), CHAT, "NewLive_2").unwrap();
    let second = p.plan(&good(), &env()).unwrap().remove(0);
    let cfg = poll_of(&second);
    assert!(cfg.url.contains("liveChatId=NewLive_2"), "{}", cfg.url);
    assert_eq!(cfg.source_key, "youtube:livechat:NewLive_2");
    assert_ne!(
        first.fingerprint, second.fingerprint,
        "a retarget is a clean restart"
    );
    // A re-pinned binding wins over the stale override.
    let repinned = binding(&[("live_chat_id", "Repinned_9"), ("tenant_id", "3")]);
    let third = p.plan(&repinned, &env()).unwrap().remove(0);
    assert!(poll_of(&third).url.contains("liveChatId=Repinned_9"));
}

#[test]
fn the_binding_channel_is_validated() {
    let ok = binding(&[("live_chat_id", CHAT), ("channel_id", "UCgood_1-x")]);
    assert!(YoutubeProfile::new().plan(&ok, &env()).is_ok());
    for bad in ["", "has space", "a/b", &"x".repeat(65)] {
        let b = binding(&[("live_chat_id", CHAT), ("channel_id", bad)]);
        assert_eq!(
            YoutubeProfile::new().plan(&b, &env()).unwrap_err(),
            PlanError::InvalidValue("channel_id")
        );
    }
    assert!(rediscover::is_channel_id("UC-_09az"));
}
