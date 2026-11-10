use super::*;
use crate::connector_runtime::testkit::{
    binding, capture, desired_ws, page, ScriptedResolver, TestProfile,
};
use std::time::Duration;

const T: Duration = Duration::from_secs(5);

fn env() -> RuntimeEnv {
    RuntimeEnv {
        environment: "test".into(),
        max_frame_bytes: 1024,
        egress_allow: vec![],
        heartbeat_enabled: false,
    }
}

fn feed(r: ScriptedResolver) -> HubBindingFeed<ScriptedResolver, TestProfile> {
    HubBindingFeed::new(Arc::new(r), TestProfile, env(), T)
}

fn keys(v: &[DesiredConnection]) -> Vec<(i64, i64)> {
    v.iter()
        .map(|d| (d.key.community_id, d.key.connection_id))
        .collect()
}

#[tokio::test]
async fn pages_to_completion_and_returns_a_sorted_set() {
    let r = ScriptedResolver::new(vec![
        page(vec![binding(2, 20, &[("channel", "b")])], Some(20)),
        page(vec![binding(1, 10, &[("channel", "a")])], Some(30)),
        page(vec![binding(3, 30, &[("channel", "c")])], None),
    ]);
    let f = feed(r);
    let got = f.desired("twitch").await.expect("desired");
    assert_eq!(keys(&got), vec![(1, 10), (2, 20), (3, 30)]);
    assert_eq!(*f.resolver.afters.lock().expect("lock"), vec![0, 20, 30]);
}

#[tokio::test]
async fn an_empty_binding_list_is_a_valid_empty_set() {
    let f = feed(ScriptedResolver::new(vec![page(vec![], None)]));
    assert!(f.desired("twitch").await.expect("desired").is_empty());
}

#[tokio::test]
async fn mid_pagination_error_fails_the_whole_discovery() {
    let f = feed(ScriptedResolver::new(vec![
        page(vec![binding(1, 10, &[("channel", "a")])], Some(10)),
        Err(ResolveError::Transient("boom")),
    ]));
    assert!(matches!(
        f.desired("twitch").await,
        Err(FeedError::Resolve(ResolveError::Transient(_)))
    ));
}

#[tokio::test]
async fn page_cap_is_a_failure_not_a_truncation() {
    let mut r = ScriptedResolver::new(vec![]);
    r.endless = true;
    let f = feed(r);
    assert!(matches!(
        f.desired("twitch").await,
        Err(FeedError::PageCapExceeded)
    ));
    assert_eq!(
        f.resolver.afters.lock().expect("lock").len(),
        MAX_PAGES as usize
    );
}

#[tokio::test]
async fn stuck_cursor_is_a_failure() {
    let f = feed(ScriptedResolver::new(vec![
        page(vec![], Some(10)),
        page(vec![], Some(10)),
    ]));
    assert!(matches!(
        f.desired("twitch").await,
        Err(FeedError::StuckCursor)
    ));
}

#[tokio::test(start_paused = true)]
async fn each_call_is_bounded_by_the_timeout() {
    let mut r = ScriptedResolver::new(vec![]);
    r.hang = true;
    let f = feed(r);
    assert!(matches!(f.desired("twitch").await, Err(FeedError::Timeout)));
}

#[tokio::test]
async fn a_platform_the_feed_does_not_serve_is_an_error() {
    let f = feed(ScriptedResolver::new(vec![]));
    assert!(matches!(
        f.desired("discord").await,
        Err(FeedError::UnknownPlatform(p)) if p == "discord"
    ));
}

#[tokio::test]
async fn malformed_and_mismatched_bindings_are_skipped_and_counted() {
    let reg = prometheus::Registry::new();
    let m = RuntimeMetrics::register(&reg).expect("metrics");
    let f = feed(ScriptedResolver::new(vec![page(
        vec![
            binding(1, 1, &[("mode", "bad")]),
            binding(2, 2, &[("mode", "mismatch"), ("channel", "x")]),
            binding(3, 3, &[("channel", "ok")]),
        ],
        None,
    )]))
    .with_metrics(m.clone());
    let got = f.desired("twitch").await.expect("desired");
    assert_eq!(keys(&got), vec![(3, 3)]);
    assert_eq!(
        m.feed_skipped_total
            .with_label_values(&["twitch", "malformed"])
            .get(),
        2
    );
}

#[tokio::test]
async fn one_binding_may_plan_several_connections() {
    let f = feed(ScriptedResolver::new(vec![page(
        vec![binding(1, 5, &[("mode", "two"), ("channel", "x")])],
        None,
    )]));
    let got = f.desired("twitch").await.expect("desired");
    assert_eq!(got.len(), 2);
    let kinds: Vec<_> = got.iter().map(|d| d.plan.kind()).collect();
    assert!(kinds.contains(&"ws") && kinds.contains(&"webhook"));
}

#[tokio::test]
async fn same_community_duplicate_resource_keeps_the_lowest_connection() {
    let reg = prometheus::Registry::new();
    let m = RuntimeMetrics::register(&reg).expect("metrics");
    let f = feed(ScriptedResolver::new(vec![page(
        vec![
            binding(3, 90, &[("channel", "same")]),
            binding(3, 30, &[("channel", "same")]),
        ],
        None,
    )]))
    .with_metrics(m.clone());
    let got = f.desired("twitch").await.expect("desired");
    assert_eq!(keys(&got), vec![(3, 30)]);
    assert_eq!(
        m.feed_skipped_total
            .with_label_values(&["twitch", "duplicate"])
            .get(),
        1
    );
}

#[tokio::test]
async fn cross_community_claim_drops_every_claimant_and_logs_no_handle() {
    let cap = capture();
    let reg = prometheus::Registry::new();
    let m = RuntimeMetrics::register(&reg).expect("metrics");
    let f = feed(ScriptedResolver::new(vec![
        page(
            vec![binding(9, 90, &[("channel", "Contested-Handle")])],
            Some(90),
        ),
        page(
            vec![
                binding(3, 30, &[("channel", "Contested-Handle")]),
                binding(4, 40, &[("channel", "fine")]),
            ],
            None,
        ),
    ]))
    .with_metrics(m.clone());
    let got = f.desired("twitch").await.expect("desired");
    assert_eq!(keys(&got), vec![(4, 40)], "contested channel fails closed");
    assert_eq!(
        m.feed_skipped_total
            .with_label_values(&["twitch", "conflict"])
            .get(),
        1
    );
    let logged = cap.text();
    assert!(logged.contains("claim_hash="), "{logged}");
    assert!(logged.contains("[3, 9]"), "{logged}");
    assert!(!logged.to_lowercase().contains("contested"), "{logged}");
}

#[tokio::test]
async fn a_route_key_claimed_by_two_communities_is_dropped_for_both() {
    let f = feed(ScriptedResolver::new(vec![page(
        vec![
            binding(1, 1, &[("mode", "webhook"), ("route", "shared-route-key")]),
            binding(2, 2, &[("mode", "webhook"), ("route", "shared-route-key")]),
            binding(3, 3, &[("mode", "webhook"), ("route", "unique-route-key")]),
        ],
        None,
    )]));
    let got = f.desired("twitch").await.expect("desired");
    assert_eq!(keys(&got), vec![(3, 3)]);
}

#[test]
fn debug_never_prints_the_resource_handle() {
    let d = desired_ws("twitch", 1, 2, 9, "secret-handle", 5);
    let s = format!("{d:?}");
    assert!(!s.contains("secret-handle"), "{s}");
    assert!(s.contains("community_id") && s.contains("ws"));
}

#[test]
fn conn_key_orders_by_platform_then_ids() {
    let a = ConnKey {
        platform: "twitch",
        community_id: 1,
        connection_id: 9,
        lane: Default::default(),
    };
    let b = ConnKey {
        platform: "twitch",
        community_id: 2,
        connection_id: 1,
        lane: Default::default(),
    };
    assert!(a < b);
}

struct Fixed(&'static str);
impl BindingFeed for Fixed {
    async fn desired(&self, p: &'static str) -> Result<Vec<DesiredConnection>, FeedError> {
        Ok(vec![desired_ws(p, 1, 1, 9, self.0, 1)])
    }
}

#[tokio::test]
async fn feed_set_routes_by_platform_and_rejects_unknown() {
    let set = FeedSet::new()
        .with("twitch", Arc::new(Fixed("a")))
        .with("discord", Arc::new(Fixed("b")));
    assert_eq!(
        set.desired("discord").await.expect("ok")[0].key.platform,
        "discord"
    );
    assert!(matches!(
        set.desired("youtube").await,
        Err(FeedError::UnknownPlatform(_))
    ));
}

#[tokio::test]
async fn bindings_refused_for_the_identify_budget_are_skipped_loudly_not_run() {
    let cap = capture();
    let f = feed(ScriptedResolver::new(vec![page(
        vec![
            binding(1, 1, &[("mode", "budget")]),
            binding(2, 2, &[("channel", "ok")]),
        ],
        None,
    )]));
    let got = f.desired("twitch").await.expect("desired");
    assert_eq!(keys(&got), vec![(2, 2)]);
    assert!(cap.text().contains("identify budget is not ready"));
}
