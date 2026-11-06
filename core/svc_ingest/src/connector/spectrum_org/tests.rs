//! Unit tests for the Spectrum snapshot parsers and diffs (no I/O).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use super::*;

fn events(v: Value) -> Vec<OrgEvent> {
    parse_events(v.as_array().unwrap()).items
}

fn roster(ids: &[(&str, &[&str])]) -> Vec<Member> {
    ids.iter()
        .map(|(id, roles)| Member {
            member_id: (*id).to_owned(),
            roles: roles.iter().map(|r| ((*r).to_owned(), None)).collect(),
        })
        .collect()
}

#[test]
fn member_parsing_covers_alias_roles_rank_and_drops_idless() {
    let p = parse_members(
        json!([
            {"id": 5, "roles": [{"id": "b", "name": "Boss"}, "a", 7, {"name": "x"}], "rank": {"id": 2, "name": "Cap"}},
            {"member_id": "m9", "role_ids": ["z"]},
            {"member_id": "m10", "roles": "nope", "role_ids": ["q"]},
            {"displayname": "no id"},
            "junk"
        ])
        .as_array()
        .unwrap(),
    );
    assert_eq!(p.dropped, 2);
    assert_eq!(p.items[0].member_id, "5");
    let ids: Vec<_> = p.items[0].roles.iter().map(|r| r.0.as_str()).collect();
    assert_eq!(ids, ["7", "a", "b", "rank:2"]);
    assert_eq!(
        p.items[0].role_names(),
        vec!["Boss".to_owned(), "Cap".to_owned()]
    );
    assert_eq!(p.items[1].role_ids(), ["z"]);
    assert_eq!(p.items[2].role_ids(), ["q"]);
}

#[test]
fn event_parsing_aliases_cancel_and_epochs() {
    let e = events(json!([
        {"id": "e1", "title": "T", "body": " desc ", "time_start": 100, "time_end": "200.5",
         "location": "L", "member": {"id": "o1"}, "rsvp": {"count_attending": "4"}},
        {"event_id": 9, "name": "N", "start_time": 0, "starts_at": 50, "cancelled": 1, "rsvp_count": 3,
         "member_id": "o2"},
        {"id": "e3", "cancelled": "false", "attending_count": 2.0, "status": "cancelled"},
        {"id": "e4", "cancelled": true, "attending_count": -1},
        {"id": "e5", "cancelled": false, "time_start": true, "time_end": "bad"},
        {"title": "no id"}
    ]));
    assert_eq!(e.len(), 5);
    assert_eq!(e[0].description.as_deref(), Some("desc"));
    assert_eq!(e[0].starts_epoch, Some(100.0));
    assert_eq!(e[0].ends_epoch, Some(200.5));
    assert_eq!(e[0].rsvp_count, Some(4));
    assert_eq!(e[0].organizer_id.as_deref(), Some("o1"));
    assert_eq!(e[1].event_id, "9");
    assert_eq!(
        e[1].starts_epoch,
        Some(50.0),
        "zero falls through like `or`"
    );
    assert_eq!(e[1].status, "cancelled");
    assert_eq!(e[1].organizer_id.as_deref(), Some("o2"));
    assert_eq!(e[2].status, "cancelled", "status string cancels");
    assert_eq!(e[2].rsvp_count, Some(2));
    assert_eq!(e[3].rsvp_count, None);
    assert_eq!(e[4].status, "scheduled");
    assert_eq!(e[4].starts_epoch, None);
}

#[test]
fn iso_conversion() {
    assert_eq!(iso_of(0.0).as_deref(), Some("1970-01-01T00:00:00+00:00"));
    assert!(iso_of(1.5).unwrap().contains(".500"));
    assert_eq!(iso_of(f64::NAN), None);
    assert_eq!(iso_of(1e30), None);
}

#[test]
fn roster_first_run_primes_without_changes() {
    let d = diff_roster(None, &roster(&[("1", &["a"])]), 0.5, 10).unwrap();
    assert!(d.primed && d.changes.is_empty() && !d.previous_discarded);
    assert_eq!(d.snapshot["members"]["1"], "a");
}

#[test]
fn roster_unusable_previous_reprimes_and_flags_discard() {
    for bad in [json!({"v": 9, "members": {}}), json!({"v": 1}), json!(7)] {
        let d = diff_roster(Some(&bad), &roster(&[("1", &[])]), 0.5, 10).unwrap();
        assert!(d.primed && d.previous_discarded, "{bad}");
    }
}

#[test]
fn roster_diff_reports_join_leave_and_role_changes() {
    let first = diff_roster(
        None,
        &roster(&[("1", &["a", "b"]), ("2", &[]), ("3", &["x"])]),
        0.5,
        10,
    )
    .unwrap();
    let now = roster(&[
        ("1", &["b", "c"]),
        ("2", &[]),
        ("4", &["y"]),
        ("4", &["y", "z"]),
    ]);
    let d = diff_roster(Some(&first.snapshot), &now, 0.9, 10).unwrap();
    assert!(!d.primed);
    let got: Vec<(&str, &str)> = d
        .changes
        .iter()
        .map(|c| (c.change, c.member_id.as_str()))
        .collect();
    assert_eq!(
        got,
        [("roles_changed", "1"), ("joined", "4"), ("left", "3")]
    );
    assert_eq!(d.changes[0].roles_added, ["c"]);
    assert_eq!(d.changes[0].roles_removed, ["a"]);
    assert_eq!(
        d.changes[1].roles_added,
        ["y", "z"],
        "duplicate id: last value wins"
    );
}

#[test]
fn roster_guard_blocks_empty_and_mass_departure() {
    let big: Vec<(String, [&str; 0])> = (0..10).map(|i| (i.to_string(), [])).collect();
    let prev_members: Vec<Member> = big
        .iter()
        .map(|(i, _)| Member {
            member_id: i.clone(),
            roles: vec![],
        })
        .collect();
    let prev = diff_roster(None, &prev_members, 0.5, 10).unwrap().snapshot;
    assert_eq!(
        diff_roster(Some(&prev), &[], 0.5, 10).unwrap_err(),
        DiffError::Anomaly("roster emptied")
    );
    assert!(diff_roster(Some(&prev), &prev_members[..4], 0.5, 10).is_err());
    assert!(diff_roster(Some(&prev), &prev_members[..5], 0.5, 10).is_ok());
    // small orgs skip the ratio guard (but not the empty guard)
    let small = diff_roster(None, &prev_members[..3], 0.5, 10)
        .unwrap()
        .snapshot;
    assert!(diff_roster(Some(&small), &prev_members[..1], 0.5, 10).is_ok());
    assert!(diff_roster(Some(&small), &[], 0.5, 10).is_err());
}

fn ev(id: &str, title: &str, start: f64, rsvp: Option<u64>, cancelled: bool) -> OrgEvent {
    OrgEvent {
        event_id: id.to_owned(),
        title: Some(title.to_owned()),
        description: Some("d".repeat(3000)),
        starts_epoch: Some(start),
        ends_epoch: None,
        location: None,
        organizer_id: Some("o".to_owned()),
        status: if cancelled { "cancelled" } else { "scheduled" }.to_owned(),
        rsvp_count: rsvp,
    }
}

#[test]
fn events_prime_then_diff_every_change_kind() {
    let base = [
        ev("a", "A", 1000.0, Some(1), false),
        ev("b", "B", 1000.0, Some(1), false),
        ev("c", "C", 1000.0, Some(1), false),
        ev("d", "D", 1000.0, None, false),
        ev("gone", "G", 1000.0, None, false),
        ev("ended", "E", 10.0, None, false),
    ];
    let first = diff_events(None, &base, 500.0, 0.9).unwrap();
    assert!(first.primed && first.changes.is_empty());
    let now = [
        ev("a", "A", 1000.0, Some(1), false),
        ev("b", "B2", 1000.0, Some(1), false),
        ev("c", "C", 1000.0, Some(2), true),
        ev("d", "D", 1000.0, Some(5), false),
        ev("new", "N", 2000.0, None, false),
    ];
    let d = diff_events(Some(&first.snapshot), &now, 500.0, 0.9).unwrap();
    let got: Vec<(&str, &str)> = d
        .changes
        .iter()
        .map(|c| (c.change, c.event_id.as_str()))
        .collect();
    assert_eq!(
        got,
        [
            ("updated", "b"),
            ("cancelled", "c"),
            ("rsvp_changed", "c"),
            ("rsvp_changed", "d"),
            ("created", "new"),
            ("removed", "gone"),
        ]
    );
    assert_eq!(d.changes[2].rsvp_previous, Some(1));
    assert_eq!(d.changes[3].rsvp_previous, None);
    assert_eq!(
        d.pruned, 1,
        "the already-started event ended; pruned silently"
    );
}

#[test]
fn events_mass_vanish_of_upcoming_is_an_anomaly() {
    let base: Vec<OrgEvent> = (0..6)
        .map(|i| ev(&i.to_string(), "t", 1000.0, None, false))
        .collect();
    let snap = diff_events(None, &base, 0.0, 0.5).unwrap().snapshot;
    assert!(diff_events(Some(&snap), &base[..2], 0.0, 0.5).is_err());
    assert!(diff_events(Some(&snap), &base[..4], 0.0, 0.5).is_ok());
    // unusable previous -> reprime
    let d = diff_events(Some(&json!({"v": 1})), &base, 0.0, 0.5).unwrap();
    assert!(d.primed && d.previous_discarded);
    // unknown start counts as upcoming
    assert!(is_upcoming(&json!({}), 1.0));
}

#[test]
fn frames_carry_no_display_name_and_bound_the_description() {
    let rc = RosterChange {
        change: "joined",
        member_id: "9".into(),
        roles_added: vec!["a".into()],
        roles_removed: vec![],
        role_names: vec!["A".into()],
    };
    let f: Value = serde_json::from_slice(&roster_frame(&rc, "poll-x", "T")).unwrap();
    assert_eq!(f["kind"], "roster");
    assert_eq!(f["member_id"], "9");
    assert!(f.get("display_name").is_none());
    let ec = EventChange {
        change: "created",
        event_id: "e".into(),
        event: Some(ev("e", "T", 100.0, Some(3), false)),
        rsvp_previous: None,
    };
    let f: Value = serde_json::from_slice(&event_frame(&ec, "poll-x", "T")).unwrap();
    assert_eq!(
        f["description"].as_str().unwrap().chars().count(),
        MAX_DESCRIPTION_CHARS
    );
    assert_eq!(f["organizer_id"], "o");
    assert_eq!(f["starts_at"], "1970-01-01T00:01:40+00:00");
    let removed = EventChange {
        change: "removed",
        event_id: "e".into(),
        event: None,
        rsvp_previous: None,
    };
    let f: Value = serde_json::from_slice(&event_frame(&removed, "s", "T")).unwrap();
    assert!(f["title"].is_null() && f["status"].is_null());
}
