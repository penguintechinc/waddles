#![allow(clippy::unwrap_used, clippy::expect_used)]
//! Fail-closed pin and typed-slot tests: every policy kind that requires a pin either pins
//! or errors on every platform (a future platform cannot silently run it unpinned), and an
//! `Int` slot substitutes a JSON integer only for a digits-only u64.

use std::collections::BTreeMap;

use super::policy::{
    KindPolicy, PathSeg, PlatformPolicy, Position, SlotDecl, SlotOrigin, SlotShape, SlotSyntax,
};
use super::subst::{render, SlotValue, SubstError};
use super::verify::verify;
use super::{pin_channel, ExecError, PinError, RequestTpl};
use crate::outbound_ops::{OutboundAction, PolicyOp};

fn action(op: &'static str, chan: Option<&str>, origin: Option<&str>) -> OutboundAction {
    OutboundAction::PolicyOp(PolicyOp {
        op,
        channel: chan.map(str::to_string),
        text: None,
        message_id: None,
        user_id: None,
        origin_channel: origin.map(str::to_string),
        tenant_id: None,
    })
}

fn valid_channel(platform: &str) -> &'static str {
    match platform {
        "twitch" => "#chan",
        "discord" => "123456789",
        _ => "chat1",
    }
}

#[test]
fn every_requires_pin_kind_pins_or_errors_on_every_platform() {
    let mut checked = 0;
    let policies = PlatformPolicy::production_set();
    // Production platforms plus platforms with NO policy / pin implementation.
    let mut platforms: Vec<&str> = policies.iter().map(|p| p.platform).collect();
    platforms.extend(["slack", "mattermost", "bogus", ""]);
    for p in &policies {
        for k in p.kinds.iter().filter(|k| k.requires_pin) {
            for platform in &platforms {
                let ch = valid_channel(platform);
                let ok = action(k.kind, Some(ch), Some(ch));
                let res = pin_channel(platform, &ok, false, true);
                // Never a silent `Ok(None)`: pinned or an error.
                assert!(
                    matches!(res, Ok(Some(_)) | Err(_)),
                    "{platform}/{}: requires_pin kind passed unpinned",
                    k.kind
                );
                if !matches!(*platform, "twitch" | "discord" | "youtube" | "kick") {
                    assert!(
                        matches!(res, Err(ExecError::Pin(PinError::UnsupportedPlatform))),
                        "{platform}/{}: unknown platform must be refused",
                        k.kind
                    );
                }
                // No origin at all, a mismatched channel, and a guild scope never pass.
                for bad in [
                    action(k.kind, Some(ch), None),
                    action(k.kind, None, None),
                    action(k.kind, Some(ch), Some("other9")),
                    action(k.kind, None, Some("guild:123456789")),
                    action(k.kind, None, Some("guild:-")),
                ] {
                    assert!(
                        pin_channel(platform, &bad, false, true).is_err(),
                        "{platform}/{}: {bad:?} passed",
                        k.kind
                    );
                }
                checked += 1;
            }
        }
    }
    assert!(
        checked >= 20,
        "denominator: {checked} (kind x platform) pairs"
    );
}

#[test]
fn kick_pins_to_the_origin_and_unpinned_kinds_on_unknown_platforms_still_run() {
    let a = action("x", Some("room1"), Some("room1"));
    assert_eq!(
        pin_channel("kick", &a, false, true).unwrap().as_deref(),
        Some("room1")
    );
    let mismatch = action("x", Some("room1"), Some("room2"));
    assert_eq!(
        pin_channel("kick", &mismatch, false, true).unwrap_err(),
        ExecError::Pin(PinError::ChannelMismatch)
    );
    // A kind that needs no pin and uses no stamp slot is the only thing allowed through on a
    // platform with no pin implementation.
    let none = action("x", None, None);
    assert_eq!(pin_channel("slack", &none, false, false).unwrap(), None);
    assert!(pin_channel("slack", &none, true, false).is_err());
    assert!(pin_channel("slack", &a, false, true).is_err());
}

fn int_policy() -> PlatformPolicy {
    PlatformPolicy {
        platform: "kick",
        syntax: SlotSyntax::Double,
        hosts: vec!["api.kick.com".to_string()],
        slots: vec![SlotDecl {
            name: "KICK_USER_ID",
            handle: "user_id",
            origin: SlotOrigin::Resolver,
            shape: SlotShape::Int,
            positions: vec![Position::Body("/user_id"), Position::Query("uid")],
        }],
        kinds: vec![KindPolicy {
            slots: vec!["KICK_USER_ID"],
            max_body: Some(256),
            query: vec!["uid"],
            ..KindPolicy::new(
                "mod.ban",
                "POST",
                vec![PathSeg::Lit("public"), PathSeg::Lit("v1")],
            )
        }],
        auth_prefixes: vec![],
        allowed_headers: vec!["content-type"],
        allow_http: false,
    }
}

fn int_tpl() -> RequestTpl {
    RequestTpl {
        method: "POST".into(),
        url: "https://api.kick.com/public/v1?uid={{KICK_USER_ID}}".into(),
        headers: vec![("Content-Type".into(), "application/json".into())],
        body: Some(br#"{"user_id":"{{KICK_USER_ID}}"}"#.to_vec()),
        secret_refs: vec![("{{KICK_USER_ID}}".into(), "user_id".into())],
    }
}

fn render_with(v: &str) -> Result<super::subst::PreparedRequest, SubstError> {
    let p = int_policy();
    let verified = verify(&p, p.kind("mod.ban").unwrap(), &int_tpl()).unwrap();
    let mut values = BTreeMap::new();
    values.insert(0, SlotValue::new(v.to_string()));
    render(&p, &verified, &values, &BTreeMap::new())
}

#[test]
fn an_int_slot_is_a_json_integer_in_the_body_and_digits_in_the_query() {
    let req = render_with("1234567890123").unwrap();
    let body: serde_json::Value = serde_json::from_slice(&req.body.unwrap()).unwrap();
    assert_eq!(body, serde_json::json!({"user_id": 1_234_567_890_123_u64}));
    assert!(body["user_id"].is_u64());
    assert!(req.url.ends_with("?uid=1234567890123"), "{}", req.url);
}

#[test]
fn an_int_slot_refuses_anything_but_a_digits_only_u64() {
    for bad in [
        "",
        "-1",
        "+1",
        "1.5",
        "1e3",
        "0x10",
        " 1",
        "1 ",
        "abc",
        "18446744073709551616",
        "123456789012345678901",
    ] {
        assert_eq!(
            render_with(bad).err(),
            Some(SubstError::ShapeInvalid),
            "{bad:?}"
        );
    }
    assert!(render_with("18446744073709551615").is_ok());
    assert!(render_with("0").is_ok());
}
