//! Unit tests for the Spectrum profile derivation (no network).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use super::*;

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
        tenant_id: 4,
        secret_version: 2,
        community_id: 7,
        connection_id: 12,
        provider: "spectrum".to_owned(),
        kind: ConnectionKind::ExternalHttp,
        metadata: meta
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect(),
    }
}

fn one(b: &BindingSummary) -> DesiredConnection {
    let mut v = SpectrumProfile::new().plan(b, &env()).unwrap();
    assert_eq!(v.len(), 1);
    v.remove(0)
}

fn plan_of(d: &DesiredConnection) -> &SpectrumPlan {
    match &d.plan {
        TransportPlan::Spectrum(p) => p,
        _ => panic!("spectrum plan expected"),
    }
}

#[test]
fn identity_constants() {
    let p = SpectrumProfile::new();
    assert_eq!(p.platform(), "spectrum");
    assert_eq!(p.app_id(), "waddles.core.service.spectrum");
    assert_eq!(p.provider(), "spectrum");
    assert_eq!(p.capability(), "chat_ingest");
}

#[test]
fn every_kind_derives_its_host_constant_request() {
    let want = [
        (
            "lobby",
            "/lobby/messages",
            r#"{"limit":50,"lobby_id":"L1"}"#,
            false,
            15,
        ),
        (
            "forum",
            "/forum/channel/threads",
            r#"{"channel_id":"L1","page":1,"sort":"newest"}"#,
            false,
            15,
        ),
        (
            "roster",
            "/community/member/list",
            r#"{"community_id":"L1","page":1,"pagesize":100}"#,
            true,
            300,
        ),
        (
            "events",
            "/community/event/list",
            r#"{"community_id":"L1","page":1,"pagesize":100}"#,
            true,
            300,
        ),
    ];
    for (kind, path, body, paged, secs) in want {
        let d = one(&binding(&[
            ("kind", kind),
            ("source_id", "L1"),
            ("tenant_id", "99"),
        ]));
        assert_eq!(
            d.tenant_id, 4,
            "the feed's tenant; metadata tenant is inert"
        );
        let p = plan_of(&d);
        assert_eq!(p.poll.url, format!("{API_BASE}{path}"));
        let got: serde_json::Value =
            serde_json::from_slice(p.poll.request.json_body.as_ref().unwrap()).unwrap();
        assert_eq!(
            got,
            serde_json::from_str::<serde_json::Value>(body).unwrap(),
            "{kind}"
        );
        assert_eq!(p.poll.request.page_field.is_some(), paged, "{kind}");
        assert_eq!(p.poll.interval, Duration::from_secs(secs));
        assert_eq!(p.poll.source_key, format!("spectrum:{kind}:L1"));
        assert_eq!(p.spec.kind.as_str(), kind);
        assert!(p
            .spec
            .key
            .starts_with(&format!("waddles:ingest:spectrum:4:7:{kind}:")));
        assert!(
            !p.spec.key.contains("L1"),
            "the RSI id is hashed in the key"
        );
        assert!(p.poll.egress_allow.is_empty() && !p.poll.allow_loopback_insecure);
    }
}

#[test]
fn credentials_ride_the_two_host_owned_headers_from_one_handle() {
    let d = one(&binding(&[("kind", "lobby"), ("source_id", "1")]));
    let c = &plan_of(&d).poll;
    assert_eq!(c.handles.len(), 1);
    assert_eq!(
        c.handles[HANDLE_SESSION],
        ConnectionRef {
            community_id: 7,
            connection_id: 12
        }
    );
    let a = c.auth.as_ref().unwrap();
    assert_eq!((a.header.as_str(), a.prefix.as_str()), ("X-Rsi-Token", ""));
    let x = &c.request.extra_auth[0];
    assert_eq!(
        (x.header.as_str(), x.prefix.as_str()),
        ("Cookie", "Rsi-Token=")
    );
}

#[test]
fn rejects_bad_bindings_without_echoing_values() {
    let p = SpectrumProfile::new();
    let mut b = binding(&[("kind", "lobby"), ("source_id", "1")]);
    b.provider = "youtube".into();
    assert_eq!(p.plan(&b, &env()).unwrap_err(), PlanError::WrongProvider);
    let mut b = binding(&[("kind", "lobby"), ("source_id", "1")]);
    b.kind = ConnectionKind::AwsLambda;
    assert_eq!(p.plan(&b, &env()).unwrap_err(), PlanError::WrongKind);
    assert_eq!(
        p.plan(&binding(&[("source_id", "1")]), &env()).unwrap_err(),
        PlanError::MissingKey("kind")
    );
    assert_eq!(
        p.plan(&binding(&[("kind", "dm"), ("source_id", "1")]), &env())
            .unwrap_err(),
        PlanError::InvalidValue("kind")
    );
    assert_eq!(
        p.plan(&binding(&[("kind", "lobby")]), &env()).unwrap_err(),
        PlanError::MissingKey("community_id")
    );
    assert_eq!(
        p.plan(
            &binding(&[
                ("kind", "roster"),
                ("source_id", "1"),
                ("community_id", "1")
            ]),
            &env()
        )
        .unwrap_err(),
        PlanError::InvalidValue("source_id"),
        "the two forms are exclusive"
    );
    for bad in [
        "",
        "a/b",
        "a b",
        "x?y=1",
        "../x",
        &"9".repeat(65),
        "caf\u{e9}",
        "a\"b",
    ] {
        assert_eq!(
            p.plan(&binding(&[("kind", "lobby"), ("source_id", bad)]), &env())
                .unwrap_err(),
            PlanError::InvalidValue("source_id"),
            "{bad:?}"
        );
    }
    let mut b = binding(&[("kind", "lobby"), ("source_id", "1")]);
    b.tenant_id = 0;
    assert!(matches!(p.plan(&b, &env()), Err(PlanError::Config(_))));
    let mut b = binding(&[("kind", "lobby"), ("source_id", "1")]);
    b.secret_version = 0;
    assert!(matches!(p.plan(&b, &env()), Err(PlanError::Config(_))));
}

#[test]
fn interval_metadata_is_clamped_and_validated() {
    let mk = |k, v| {
        one(&binding(&[
            ("kind", k),
            ("source_id", "1"),
            ("poll_interval_s", v),
        ]))
    };
    assert_eq!(plan_of(&mk("lobby", "1")).poll.interval, MIN_POLL_INTERVAL);
    assert_eq!(
        plan_of(&mk("lobby", "30")).poll.interval,
        Duration::from_secs(30)
    );
    assert_eq!(plan_of(&mk("lobby", "999999")).poll.interval, MAX_INTERVAL);
    assert_eq!(plan_of(&mk("roster", "10")).poll.interval, MIN_ORG_INTERVAL);
    let e = SpectrumProfile::new()
        .plan(
            &binding(&[
                ("kind", "lobby"),
                ("source_id", "1"),
                ("poll_interval_s", "fast"),
            ]),
            &env(),
        )
        .unwrap_err();
    assert_eq!(e, PlanError::InvalidValue("poll_interval_s"));
}

#[test]
fn fingerprint_tracks_source_tenant_and_rotation_not_secrets() {
    let base = one(&binding(&[("kind", "lobby"), ("source_id", "1")]));
    let same = one(&binding(&[("kind", "lobby"), ("source_id", "1")]));
    assert_eq!(base.fingerprint, same.fingerprint);
    assert_ne!(
        base.fingerprint,
        one(&binding(&[("kind", "lobby"), ("source_id", "2")])).fingerprint
    );
    assert_ne!(
        base.fingerprint,
        one(&binding(&[("kind", "forum"), ("source_id", "1")])).fingerprint
    );
    let mut rotated = binding(&[("kind", "lobby"), ("source_id", "1")]);
    rotated.secret_version = 3;
    assert_ne!(base.fingerprint, one(&rotated).fingerprint);
    let mut moved = binding(&[("kind", "lobby"), ("source_id", "1")]);
    moved.tenant_id = 9;
    assert_ne!(base.fingerprint, one(&moved).fingerprint);
}

#[test]
fn hub_community_contract_derives_org_kinds_only() {
    let roster = one(&binding(&[("community_id", "555")]));
    assert_eq!(
        plan_of(&roster).spec.kind,
        SpectrumKind::Roster,
        "default kind"
    );
    assert_eq!(plan_of(&roster).poll.source_key, "spectrum:roster:555");
    let ev = one(&binding(&[("community_id", "555"), ("kind", "events")]));
    assert_eq!(plan_of(&ev).spec.kind, SpectrumKind::Events);
    let p = SpectrumProfile::new();
    for bad in ["lobby", "forum", "nope"] {
        assert_eq!(
            p.plan(&binding(&[("community_id", "555"), ("kind", bad)]), &env())
                .unwrap_err(),
            PlanError::InvalidValue("kind"),
            "{bad}"
        );
    }
    assert_eq!(
        p.plan(&binding(&[("community_id", "a/b")]), &env())
            .unwrap_err(),
        PlanError::InvalidValue("source_id")
    );
}

#[test]
fn source_id_charset() {
    assert!(is_source_id("12345"));
    assert!(is_source_id("a_B-9"));
    assert!(!is_source_id(""));
}
