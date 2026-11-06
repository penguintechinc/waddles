//! Host selector (spec 2026-11-05 section 5): which inbound implementation
//! runs per platform -- the legacy receivers or the connector service bundle runtime.
//!
//! ```text
//!  CONNECTOR_<P>_ENABLED (env baseline, default OFF, strict bool)
//!    OFF ──> legacy starters exactly as before
//!    ON  ──> legacy inbound (and, per table, outbound/EventSub) NOT started,
//!            every prerequisite validated up front (fatal, names the item)
//! ```
//!
//! # Licensing bypass (regression note)
//!
//! This module must NEVER read the PostHog / `penguin_licensing` client. The
//! service's hardcoded `*.waddles.app` domain bypass makes that client
//! evaluate EVERY flag ON, so a flag-client-backed selector would flip every
//! environment to the connector path at once. The baseline is env only, the same
//! precedent as `INGEST_DYNAMIC_CONNECTIONS` and `CONNECTOR_TOOLS_ENABLED`.
//! `selector_source_does_not_touch_the_flag_client` pins this.

use crate::config::CliConfig;

/// Flipped to `true` by the identify-budget PR once the Valkey-backed Discord
/// IDENTIFY budget is ported into the runtime. While `false`,
/// `CONNECTOR_DISCORD_ENABLED=true` is a fatal boot error: without the budget a
/// restart storm would trip Discord's session-start limits.
pub const IDENTIFY_BUDGET_READY: bool = false;

/// Platforms the selector knows, in validation order.
pub const PLATFORMS: [&str; 3] = ["twitch", "discord", "youtube"];

/// Platforms whose webhook routes the runtime can own.
pub const WEBHOOK_CAPABLE: [&str; 2] = ["twitch", "discord"];

/// Why the runtime cannot start. `Display` names the setting/item only --
/// never a value, a path's content or a secret.
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum BootError {
    /// A required setting is unset or empty.
    #[error("connector service bundle is ON but {0} is not set")]
    Missing(&'static str),
    /// A setting is present but unusable.
    #[error("{0} is malformed")]
    Malformed(&'static str),
    /// A prerequisite is not satisfied.
    #[error("connector service bundle is ON but {0} is OFF")]
    Prerequisite(&'static str),
    /// The platform connector is blocked until a named item lands.
    #[error("{0}")]
    Blocked(&'static str),
}

/// Strictly parses an optional boolean env value: unset is OFF; only
/// `true`/`false`/`1`/`0` (case-insensitive, trimmed) are accepted -- anything
/// else, including an empty string, is fatal rather than silently OFF.
fn parse_bool(
    lookup: &dyn Fn(&str) -> Option<String>,
    name: &'static str,
) -> Result<bool, BootError> {
    match lookup(name) {
        None => Ok(false),
        Some(raw) => match raw.trim().to_ascii_lowercase().as_str() {
            "true" | "1" => Ok(true),
            "false" | "0" => Ok(false),
            _ => Err(BootError::Malformed(name)),
        },
    }
}

/// Which platforms run on the connector runtime (`true`) instead of legacy.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub struct ConnectorSelector {
    twitch: bool,
    discord: bool,
    youtube: bool,
}

impl ConnectorSelector {
    /// Reads `CONNECTOR_{TWITCH,DISCORD,YOUTUBE}_ENABLED` via `lookup` (env in
    /// production). Default OFF; malformed is fatal; Discord ON is fatal
    /// while [`IDENTIFY_BUDGET_READY`] is `false`.
    ///
    /// # Errors
    /// [`BootError::Malformed`] for a non-boolean value,
    /// [`BootError::Blocked`] for Discord ON before the identify budget lands.
    pub fn from_env(lookup: &dyn Fn(&str) -> Option<String>) -> Result<Self, BootError> {
        Self::parse(lookup, IDENTIFY_BUDGET_READY)
    }

    fn parse(
        lookup: &dyn Fn(&str) -> Option<String>,
        identify_budget_ready: bool,
    ) -> Result<Self, BootError> {
        let sel = Self {
            twitch: parse_bool(lookup, "CONNECTOR_TWITCH_ENABLED")?,
            discord: parse_bool(lookup, "CONNECTOR_DISCORD_ENABLED")?,
            youtube: parse_bool(lookup, "CONNECTOR_YOUTUBE_ENABLED")?,
        };
        if sel.discord && !identify_budget_ready {
            return Err(BootError::Blocked(
                "discord connector requires the identify budget, not yet ported",
            ));
        }
        Ok(sel)
    }

    /// `true` when the connector path is active for `platform` (unknown: false).
    #[must_use]
    pub fn connector(&self, platform: &str) -> bool {
        match platform {
            "twitch" => self.twitch,
            "discord" => self.discord,
            "youtube" => self.youtube,
            _ => false,
        }
    }

    /// `true` when the legacy path is active for `platform`.
    #[must_use]
    pub fn legacy(&self, platform: &str) -> bool {
        !self.connector(platform)
    }

    /// `true` when any platform runs on the connector runtime.
    #[must_use]
    pub fn any_connector(&self) -> bool {
        self.twitch || self.discord || self.youtube
    }

    /// Platforms with the connector ON, in [`PLATFORMS`] order.
    #[must_use]
    pub fn enabled_platforms(&self) -> Vec<&'static str> {
        PLATFORMS
            .into_iter()
            .filter(|p| self.connector(p))
            .collect()
    }
}

/// Parses `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS` (csv, default empty) into the
/// platforms whose webhook routes the runtime owns.
///
/// # Errors
/// [`BootError::Malformed`] for an unknown or empty-element entry.
pub fn parse_webhook_platforms(raw: &str) -> Result<Vec<&'static str>, BootError> {
    const NAME: &str = "CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS";
    let mut out: Vec<&'static str> = Vec::new();
    if raw.trim().is_empty() {
        return Ok(out);
    }
    for part in raw.split(',') {
        let part = part.trim().to_ascii_lowercase();
        let known = WEBHOOK_CAPABLE
            .into_iter()
            .find(|p| *p == part)
            .ok_or(BootError::Malformed(NAME))?;
        if !out.contains(&known) {
            out.push(known);
        }
    }
    Ok(out)
}

/// Which legacy starters run, derived once from the selector (spec 5.2).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LegacyPlan {
    pub twitch_inbound: bool,
    pub twitch_eventsub: bool,
    pub twitch_outbound: bool,
    pub discord_inbound: bool,
    pub discord_outbound: bool,
}

impl LegacyPlan {
    /// Computes the plan. Twitch EventSub stays legacy unless `twitch` is a
    /// runtime webhook platform; outbound drains are suppressed only when the
    /// generic executor path (`CONNECTOR_TOOLS_ENABLED`) replaces them.
    #[must_use]
    pub fn new(sel: &ConnectorSelector, webhook_platforms: &[&str], tools_enabled: bool) -> Self {
        let runtime_owns_eventsub =
            sel.connector("twitch") && webhook_platforms.contains(&"twitch");
        Self {
            twitch_inbound: sel.legacy("twitch"),
            twitch_eventsub: !runtime_owns_eventsub,
            twitch_outbound: !(sel.connector("twitch") && tools_enabled),
            discord_inbound: sel.legacy("discord"),
            discord_outbound: !(sel.connector("discord") && tools_enabled),
        }
    }
}

/// The legacy starters `run_with_shutdown` drives. A trait so tests can count
/// calls and prove suppression without sockets.
#[allow(async_fn_in_trait)]
pub trait LegacyStarters {
    /// Twitch IRC inbound (static dev receiver or per-community sessions).
    ///
    /// # Errors
    /// A fatal misconfiguration.
    fn twitch_inbound(&mut self) -> anyhow::Result<()>;
    /// Discord Gateway receiver.
    fn discord_inbound(&mut self);
    /// Twitch outbound relay drain.
    ///
    /// # Errors
    /// A fatal misconfiguration.
    fn twitch_outbound(&mut self) -> anyhow::Result<()>;
    /// Discord outbound drain.
    fn discord_outbound(&mut self);
    /// Twitch EventSub webhook state.
    async fn twitch_eventsub(&mut self);
}

/// Starts exactly the legacy starters `plan` allows, in the historical order.
///
/// # Errors
/// The first fatal error a starter returns.
pub async fn start_legacy<S: LegacyStarters>(
    plan: &LegacyPlan,
    starters: &mut S,
) -> anyhow::Result<()> {
    if plan.twitch_inbound {
        starters.twitch_inbound()?;
    } else {
        tracing::info!(
            platform = "twitch",
            "connector ON; legacy inbound not started"
        );
    }
    if plan.discord_inbound {
        starters.discord_inbound();
    } else {
        tracing::info!(
            platform = "discord",
            "connector ON; legacy inbound not started"
        );
    }
    if plan.twitch_outbound {
        starters.twitch_outbound()?;
    } else {
        tracing::info!(
            platform = "twitch",
            "connector ON; legacy outbound not started"
        );
    }
    if plan.discord_outbound {
        starters.discord_outbound();
    } else {
        tracing::info!(
            platform = "discord",
            "connector ON; legacy outbound not started"
        );
    }
    if plan.twitch_eventsub {
        starters.twitch_eventsub().await;
    } else {
        tracing::info!(
            platform = "twitch",
            "runtime owns webhooks; legacy EventSub not started"
        );
    }
    Ok(())
}

/// Reads `CONNECTOR_TOOLS_ENABLED` (the outbound executor baseline) strictly.
///
/// # Errors
/// [`BootError::Malformed`] for a non-boolean value.
pub fn tools_enabled(lookup: &dyn Fn(&str) -> Option<String>) -> Result<bool, BootError> {
    parse_bool(lookup, "CONNECTOR_TOOLS_ENABLED")
}

fn required(
    lookup: &dyn Fn(&str) -> Option<String>,
    name: &'static str,
) -> Result<String, BootError> {
    lookup(name)
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .ok_or(BootError::Missing(name))
}

fn digest_name(platform: &str) -> &'static str {
    match platform {
        "twitch" => "SENDER_EXEC_DIGEST_TWITCH",
        "discord" => "SENDER_EXEC_DIGEST_DISCORD",
        _ => "SENDER_EXEC_DIGEST_YOUTUBE",
    }
}

/// Everything the runtime boot validates besides the selector itself.
pub struct BootInputs<'a> {
    pub cli: &'a CliConfig,
    pub selector: &'a ConnectorSelector,
    pub webhook_platforms: &'a [&'static str],
    /// `penguin_spine::SpineConfig::from_env()` succeeded.
    pub spine_configured: bool,
    pub lookup: &'a dyn Fn(&str) -> Option<String>,
}

const WEBHOOK_PREREQ: &str = "the connector of every CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS platform";

/// Validates the runtime prerequisites in the spec's fixed order. Names the
/// item, never a value. With every connector OFF, only contradictory runtime
/// settings (master switch or webhook list set) are errors.
///
/// # Errors
/// The first [`BootError`] found.
pub fn validate(inp: &BootInputs<'_>) -> Result<(), BootError> {
    if !inp.selector.any_connector() {
        if inp.cli.connector_runtime_enabled {
            return Err(BootError::Prerequisite("any CONNECTOR_<PLATFORM>_ENABLED"));
        }
        if !inp.webhook_platforms.is_empty() {
            return Err(BootError::Prerequisite(WEBHOOK_PREREQ));
        }
        return Ok(());
    }
    if inp.webhook_platforms.iter().any(|p| inp.selector.legacy(p)) {
        return Err(BootError::Prerequisite(WEBHOOK_PREREQ));
    }
    if !inp.cli.dynamic_connections {
        return Err(BootError::Prerequisite("INGEST_DYNAMIC_CONNECTIONS"));
    }
    required(inp.lookup, "HUB_API_GRPC_ENDPOINT")?;
    if inp.cli.service_jwt_token_endpoint.trim().is_empty() {
        return Err(BootError::Missing("SERVICE_JWT_TOKEN_ENDPOINT"));
    }
    for p in inp.selector.enabled_platforms() {
        required(inp.lookup, digest_name(p))?;
    }
    for name in [
        "HOST_API_SERVER_CERT_FILE",
        "HOST_API_SERVER_KEY_FILE",
        "HOST_API_CLIENT_CA_FILE",
        "HOST_API_CLIENT_IDENTITY",
    ] {
        required(inp.lookup, name)?;
    }
    if !inp.spine_configured {
        return Err(BootError::Missing("spine config (VALKEY_URL)"));
    }
    if !inp.webhook_platforms.is_empty() {
        required(inp.lookup, "CONNECTOR_ROUTE_KEY_SECRET")?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;
    use std::collections::HashMap;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect();
        move |k| m.get(k).cloned()
    }

    #[test]
    fn default_is_all_legacy() {
        let s = ConnectorSelector::from_env(&env(&[])).unwrap();
        for p in PLATFORMS {
            assert!(s.legacy(p) && !s.connector(p));
        }
        assert!(!s.any_connector());
        assert!(!s.connector("nonsense"));
    }

    #[test]
    fn twitch_and_youtube_on_are_independent() {
        let s = ConnectorSelector::from_env(&env(&[
            ("CONNECTOR_TWITCH_ENABLED", "true"),
            ("CONNECTOR_YOUTUBE_ENABLED", "1"),
        ]))
        .unwrap();
        assert!(s.connector("twitch") && s.connector("youtube") && s.legacy("discord"));
        assert_eq!(s.enabled_platforms(), vec!["twitch", "youtube"]);
    }

    #[test]
    fn explicit_false_is_off() {
        let s = ConnectorSelector::from_env(&env(&[
            ("CONNECTOR_TWITCH_ENABLED", " FALSE "),
            ("CONNECTOR_YOUTUBE_ENABLED", "0"),
        ]))
        .unwrap();
        assert!(!s.any_connector());
    }

    #[test]
    fn malformed_bool_is_fatal_for_each_platform() {
        for (name, bad) in [
            ("CONNECTOR_TWITCH_ENABLED", "yes"),
            ("CONNECTOR_DISCORD_ENABLED", ""),
            ("CONNECTOR_YOUTUBE_ENABLED", "tru"),
        ] {
            let err = ConnectorSelector::from_env(&env(&[(name, bad)])).unwrap_err();
            assert_eq!(err, BootError::Malformed(name));
            assert!(!err.to_string().contains(bad) || bad.is_empty());
        }
    }

    #[test]
    fn discord_on_is_fatal_while_identify_budget_is_not_ported() {
        let e = env(&[("CONNECTOR_DISCORD_ENABLED", "true")]);
        let err = ConnectorSelector::parse(&e, false).unwrap_err();
        assert_eq!(
            err.to_string(),
            "discord connector requires the identify budget, not yet ported"
        );
        assert!(ConnectorSelector::parse(&e, true)
            .unwrap()
            .connector("discord"));
    }

    #[test]
    fn from_env_follows_the_identify_budget_const() {
        // Fails while the const is false; the identify-budget PR flips it and
        // this assertion flips with it.
        let r = ConnectorSelector::from_env(&env(&[("CONNECTOR_DISCORD_ENABLED", "true")]));
        assert_eq!(r.is_ok(), IDENTIFY_BUDGET_READY);
    }

    #[test]
    fn discord_off_never_trips_the_budget_gate() {
        assert!(ConnectorSelector::parse(&env(&[]), false).is_ok());
    }

    #[test]
    fn webhook_platforms_parse() {
        assert!(parse_webhook_platforms("").unwrap().is_empty());
        assert_eq!(
            parse_webhook_platforms("Twitch, discord,twitch").unwrap(),
            vec!["twitch", "discord"]
        );
        for bad in ["youtube", "twitch,,discord", "x"] {
            assert_eq!(
                parse_webhook_platforms(bad).unwrap_err(),
                BootError::Malformed("CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS")
            );
        }
    }

    /// Shared with the Python selector (`tests/test_connector_selector.py`): one
    /// fixture, both implementations must reach the same legacy decisions.
    #[test]
    fn shared_decision_table_parity() {
        let table: serde_json::Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/connector_selector_table.json"
        ))
        .unwrap();
        let cases = table["cases"].as_array().unwrap();
        assert!(cases.len() >= 100, "fixture must not be empty");
        for case in cases {
            let m: HashMap<String, String> = case["env"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string()))
                .collect();
            let lookup = move |k: &str| m.get(k).cloned();
            let ready = case["identify_budget_ready"].as_bool().unwrap();
            let sel = ConnectorSelector::parse(&lookup, ready);
            if case["expect"] == "error" {
                assert!(sel.is_err(), "{case}");
                continue;
            }
            let sel = sel.unwrap();
            let wh = match case["env"].get("CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS") {
                Some(raw) => parse_webhook_platforms(raw.as_str().unwrap()).unwrap(),
                None => Vec::new(),
            };
            let tools = lookup("CONNECTOR_TOOLS_ENABLED").as_deref() == Some("true");
            let plan = LegacyPlan::new(&sel, &wh, tools);
            let e = &case["expect"];
            assert_eq!(plan.twitch_inbound, e["twitch_inbound"], "{case}");
            assert_eq!(plan.twitch_eventsub, e["twitch_eventsub"], "{case}");
            assert_eq!(plan.twitch_outbound, e["twitch_outbound"], "{case}");
            assert_eq!(plan.discord_inbound, e["discord_inbound"], "{case}");
            assert_eq!(plan.discord_outbound, e["discord_outbound"], "{case}");
            assert_eq!(
                sel.legacy("youtube"),
                e["youtube_inbound"].as_bool().unwrap(),
                "{case}"
            );
        }
    }

    #[test]
    fn legacy_plan_off_is_everything_legacy() {
        let p = LegacyPlan::new(&ConnectorSelector::default(), &[], true);
        assert!(
            p.twitch_inbound
                && p.twitch_eventsub
                && p.twitch_outbound
                && p.discord_inbound
                && p.discord_outbound
        );
    }

    fn twitch_on() -> ConnectorSelector {
        ConnectorSelector::parse(&env(&[("CONNECTOR_TWITCH_ENABLED", "true")]), true).unwrap()
    }

    #[test]
    fn twitch_on_keeps_eventsub_legacy_unless_listed() {
        let p = LegacyPlan::new(&twitch_on(), &[], false);
        assert!(!p.twitch_inbound && p.twitch_eventsub && p.twitch_outbound);
        let p = LegacyPlan::new(&twitch_on(), &["twitch"], false);
        assert!(!p.twitch_eventsub);
    }

    #[test]
    fn outbound_suppressed_only_with_tools() {
        assert!(!LegacyPlan::new(&twitch_on(), &[], true).twitch_outbound);
        assert!(LegacyPlan::new(&twitch_on(), &[], false).twitch_outbound);
    }

    #[test]
    fn discord_plan_follows_discord_connector() {
        let s =
            ConnectorSelector::parse(&env(&[("CONNECTOR_DISCORD_ENABLED", "true")]), true).unwrap();
        let p = LegacyPlan::new(&s, &[], true);
        assert!(!p.discord_inbound && !p.discord_outbound && p.twitch_inbound);
    }

    #[derive(Default)]
    struct Counting {
        tw_in: u32,
        dc_in: u32,
        tw_out: u32,
        dc_out: u32,
        es: u32,
        fail_twitch: bool,
    }
    impl LegacyStarters for Counting {
        fn twitch_inbound(&mut self) -> anyhow::Result<()> {
            self.tw_in += 1;
            if self.fail_twitch {
                anyhow::bail!("boom");
            }
            Ok(())
        }
        fn discord_inbound(&mut self) {
            self.dc_in += 1;
        }
        fn twitch_outbound(&mut self) -> anyhow::Result<()> {
            self.tw_out += 1;
            Ok(())
        }
        fn discord_outbound(&mut self) {
            self.dc_out += 1;
        }
        async fn twitch_eventsub(&mut self) {
            self.es += 1;
        }
    }

    #[tokio::test]
    async fn all_off_runs_every_legacy_starter_once() {
        let mut c = Counting::default();
        start_legacy(
            &LegacyPlan::new(&ConnectorSelector::default(), &[], true),
            &mut c,
        )
        .await
        .unwrap();
        assert_eq!(
            (c.tw_in, c.dc_in, c.tw_out, c.dc_out, c.es),
            (1, 1, 1, 1, 1)
        );
    }

    #[tokio::test]
    async fn twitch_connector_on_skips_twitch_legacy_but_not_eventsub() {
        let mut c = Counting::default();
        start_legacy(&LegacyPlan::new(&twitch_on(), &[], true), &mut c)
            .await
            .unwrap();
        assert_eq!((c.tw_in, c.tw_out, c.es), (0, 0, 1));
        assert_eq!((c.dc_in, c.dc_out), (1, 1));
    }

    #[tokio::test]
    async fn twitch_connector_on_with_webhook_skips_eventsub() {
        let mut c = Counting::default();
        start_legacy(&LegacyPlan::new(&twitch_on(), &["twitch"], false), &mut c)
            .await
            .unwrap();
        assert_eq!((c.tw_in, c.tw_out, c.es), (0, 1, 0));
    }

    #[tokio::test]
    async fn discord_connector_on_skips_discord_legacy() {
        let s =
            ConnectorSelector::parse(&env(&[("CONNECTOR_DISCORD_ENABLED", "true")]), true).unwrap();
        let mut c = Counting::default();
        start_legacy(&LegacyPlan::new(&s, &[], true), &mut c)
            .await
            .unwrap();
        assert_eq!((c.dc_in, c.dc_out, c.tw_in), (0, 0, 1));
    }

    #[tokio::test]
    async fn starter_error_propagates() {
        let mut c = Counting {
            fail_twitch: true,
            ..Counting::default()
        };
        let r = start_legacy(
            &LegacyPlan::new(&ConnectorSelector::default(), &[], false),
            &mut c,
        )
        .await;
        assert!(r.is_err());
        assert_eq!(c.dc_in, 0);
    }

    fn cli(dynamic: bool, jwt: &str, master: bool) -> CliConfig {
        let mut c = CliConfig::parse_from(["svc-ingest"]);
        c.dynamic_connections = dynamic;
        c.service_jwt_token_endpoint = jwt.to_string();
        c.connector_runtime_enabled = master;
        c
    }

    const FULL: [(&str, &str); 7] = [
        ("HUB_API_GRPC_ENDPOINT", "x"),
        ("SENDER_EXEC_DIGEST_TWITCH", "x"),
        ("HOST_API_SERVER_CERT_FILE", "x"),
        ("HOST_API_SERVER_KEY_FILE", "x"),
        ("HOST_API_CLIENT_CA_FILE", "x"),
        ("HOST_API_CLIENT_IDENTITY", "x"),
        ("CONNECTOR_ROUTE_KEY_SECRET", "x"),
    ];

    fn run(
        cli: &CliConfig,
        sel: &ConnectorSelector,
        wh: &[&'static str],
        spine: bool,
        pairs: &[(&str, &str)],
    ) -> Result<(), BootError> {
        let l = env(pairs);
        validate(&BootInputs {
            cli,
            selector: sel,
            webhook_platforms: wh,
            spine_configured: spine,
            lookup: &l,
        })
    }

    #[test]
    fn validate_all_off_is_ok_and_builds_nothing() {
        let c = cli(false, "", false);
        assert!(run(&c, &ConnectorSelector::default(), &[], false, &[]).is_ok());
    }

    #[test]
    fn master_on_without_connector_is_fatal() {
        let c = cli(true, "j", true);
        assert_eq!(
            run(&c, &ConnectorSelector::default(), &[], true, &FULL).unwrap_err(),
            BootError::Prerequisite("any CONNECTOR_<PLATFORM>_ENABLED")
        );
    }

    #[test]
    fn webhook_list_without_connector_is_fatal() {
        let c = cli(true, "j", false);
        assert!(run(&c, &ConnectorSelector::default(), &["twitch"], true, &FULL).is_err());
        let sel =
            ConnectorSelector::parse(&env(&[("CONNECTOR_YOUTUBE_ENABLED", "true")]), true).unwrap();
        assert!(run(&c, &sel, &["twitch"], true, &FULL).is_err());
    }

    #[test]
    fn validate_full_config_passes() {
        let c = cli(true, "j", false);
        assert!(run(&c, &twitch_on(), &["twitch"], true, &FULL).is_ok());
    }

    #[test]
    fn validate_fixed_order_names_each_missing_item() {
        let sel = twitch_on();
        let mut c = cli(false, "", false);
        let expect = |c: &CliConfig, pairs: &[(&str, &str)], spine: bool, want: BootError| {
            assert_eq!(run(c, &sel, &["twitch"], spine, pairs).unwrap_err(), want);
        };
        expect(
            &c,
            &[],
            false,
            BootError::Prerequisite("INGEST_DYNAMIC_CONNECTIONS"),
        );
        c.dynamic_connections = true;
        expect(&c, &[], false, BootError::Missing("HUB_API_GRPC_ENDPOINT"));
        expect(
            &c,
            &FULL[..1],
            false,
            BootError::Missing("SERVICE_JWT_TOKEN_ENDPOINT"),
        );
        c.service_jwt_token_endpoint = "j".into();
        expect(
            &c,
            &FULL[..1],
            false,
            BootError::Missing("SENDER_EXEC_DIGEST_TWITCH"),
        );
        for (n, name) in [
            (3, "HOST_API_SERVER_CERT_FILE"),
            (4, "HOST_API_SERVER_KEY_FILE"),
            (5, "HOST_API_CLIENT_CA_FILE"),
            (6, "HOST_API_CLIENT_IDENTITY"),
        ] {
            expect(&c, &FULL[..n - 1], false, BootError::Missing(name));
        }
        expect(
            &c,
            &FULL[..6],
            false,
            BootError::Missing("spine config (VALKEY_URL)"),
        );
        expect(
            &c,
            &FULL[..6],
            true,
            BootError::Missing("CONNECTOR_ROUTE_KEY_SECRET"),
        );
    }

    #[test]
    fn route_key_secret_not_required_without_webhooks() {
        let c = cli(true, "j", false);
        assert!(run(&c, &twitch_on(), &[], true, &FULL[..6]).is_ok());
    }

    #[test]
    fn blank_values_count_as_missing_and_errors_leak_no_value() {
        let c = cli(true, "j", false);
        let pairs = [("HUB_API_GRPC_ENDPOINT", "   ")];
        let err = run(&c, &twitch_on(), &[], true, &pairs).unwrap_err();
        assert_eq!(err, BootError::Missing("HUB_API_GRPC_ENDPOINT"));
    }

    #[test]
    fn digest_name_per_platform() {
        assert_eq!(digest_name("discord"), "SENDER_EXEC_DIGEST_DISCORD");
        assert_eq!(digest_name("youtube"), "SENDER_EXEC_DIGEST_YOUTUBE");
    }

    #[test]
    fn tools_enabled_is_strict() {
        assert!(!tools_enabled(&env(&[])).unwrap());
        assert!(tools_enabled(&env(&[("CONNECTOR_TOOLS_ENABLED", "true")])).unwrap());
        assert!(tools_enabled(&env(&[("CONNECTOR_TOOLS_ENABLED", "x")])).is_err());
    }

    #[test]
    fn selector_source_does_not_touch_the_flag_client() {
        // regression: the *.waddles.app bypass reads every PostHog flag ON.
        let src = include_str!("selector.rs");
        let prod = src.split("#[cfg(test)]").next().unwrap_or("");
        for banned in [
            "penguin_licensing",
            "crate::license",
            "flag_enabled",
            "posthog",
        ] {
            let hits = prod
                .lines()
                .filter(|l| !l.trim_start().starts_with("//"))
                .filter(|l| l.to_ascii_lowercase().contains(banned))
                .count();
            assert_eq!(hits, 0, "{banned}");
        }
    }
}
