//! Process-stage built-ins (spec §4.2's "Built-ins vs bundles" table): a
//! hook the Python `runner.py`/`services/moderation_gate.py` alpha ran for
//! every event, re-homed here as "a Rust built-in of the stage, no third
//! path" -- either wired into the live drain loop (`crate::spine`) or an
//! honest, documented seam, never a silent stub.
//!
//! | Built-in | Status this landing |
//! |---|---|
//! | Cross-app `_target_app_id` routing | **Wired** -- [`resolve_cross_app_route`], called from every successful `transform` in `crate::spine::handle_delivered` |
//! | Moderation-enforcement routing | Implemented and tested ([`maybe_build_enforcement_envelope`]), not yet wired to a live trigger -- see [`run_moderation_gate`]'s doc |
//! | Content-moderation gate | **TODO(M4+), honest seam** -- see [`run_moderation_gate`] |

use std::collections::HashMap;

use penguin_spine::{PlatformEvent, StageEnvelope, PROCESS_TARGET_APP_ID_KEY};

/// `app_catalog.app_id` for the enforcement action bundle, matching
/// `core/svc_process/services/moderation_gate.py::_MODERATION_ENFORCE_
/// APP_ID` (the Python alpha this stage's own `crate::spine` runs
/// alongside during the v3.0 cut-over, migration `0016_moderation_
/// enforce_app`).
pub const MODERATION_ENFORCE_APP_ID: &str = "waddles.community.moderation.default";

/// Identity payload keys copied verbatim onto the synthetic enforcement
/// envelope -- a byte-exact port of `core/svc_process/runner.py::
/// _ENFORCEMENT_IDENTITY_PAYLOAD_KEYS`. Deliberately excludes `text`: the
/// whole point of the synthetic envelope is NOT re-transmitting the
/// original message body to the enforcement action.
const ENFORCEMENT_IDENTITY_PAYLOAD_KEYS: &[&str] = &[
    "author_id",
    "user_id",
    "channel_id",
    "guild_id",
    "channel_name",
    "broadcaster_id",
    "room_id",
    "message_id",
];

/// Outcome of applying spec §5.9's cross-app `_target_app_id` routing rule
/// to a process bundle's `transform` output, immediately before enqueue.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RouteDecision {
    /// No `_target_app_id` was set (or it was empty/non-string) -- deliver
    /// to the source bundle's own action stream, unchanged.
    SameApp,
    /// `_target_app_id` named an approved, same-tenant target -- deliver
    /// there instead of the source bundle's own action stream.
    Redirect { target_app_id: String },
    /// `_target_app_id` named an undeclared/unapproved target, or one
    /// whose approved tenant does not match the source envelope's tenant
    /// -- dropped, the event is not delivered anywhere (spec §5.9: "A
    /// redirect to an undeclared or unapproved target... is dropped").
    Denied { reason: &'static str },
}

impl RouteDecision {
    /// The `reason` label used on `waddles_route_denied_total{app_id,target}`
    /// (spec §5.9) -- `None` for the two non-denied variants, which never
    /// increment that counter.
    pub fn as_metric_reason(&self) -> Option<&'static str> {
        match self {
            RouteDecision::Denied { reason } => Some(reason),
            _ => None,
        }
    }
}

/// Applies spec §5.9's cross-app routing rule to `payload` in place: pops
/// the reserved [`PROCESS_TARGET_APP_ID_KEY`] key -- it must never reach
/// an action bundle or a chat reply, whether or not the redirect is
/// approved (spec §6.1.2: "the stage pops it back out before enqueuing")
/// -- and decides where the resulting event should be enqueued.
///
/// `approved_targets` maps an approved `target_app_id` to the tenant slug
/// it was approved for (spec: "hub-api validates that each target exists
/// in `app_catalog`, is installed in the SAME tenant as the declaring
/// bundle... a cross-tenant target is refused outright at approval").
/// **TODO(M4+)**: sourced from a real `app_install_approvals`/`routes_to`
/// (§6.9) database lookup once that table is wired; this landing's caller
/// (`crate::lib::try_start_process_loop`) supplies it from a
/// `PROCESS_ROUTES_TO_APPROVED` env var as an honest interim substitute
/// (see that module's doc). The runtime enforcement itself -- comparing
/// against the declared set, plus an INDEPENDENT tenant check ("The tenant
/// check is independent of the install-time one... the stage never trusts
/// that alone", spec §5.9) -- is real and unconditional regardless of
/// where the approval data came from.
pub fn resolve_cross_app_route(
    payload: &mut serde_json::Map<String, serde_json::Value>,
    source_tenant: &str,
    approved_targets: &HashMap<String, String>,
) -> RouteDecision {
    let Some(raw) = payload.remove(PROCESS_TARGET_APP_ID_KEY) else {
        return RouteDecision::SameApp;
    };
    let Some(target_app_id) = raw.as_str().filter(|s| !s.is_empty()) else {
        return RouteDecision::SameApp;
    };
    match approved_targets.get(target_app_id) {
        None => RouteDecision::Denied {
            reason: "undeclared_or_unapproved",
        },
        Some(target_tenant) if target_tenant != source_tenant => RouteDecision::Denied {
            reason: "cross_tenant_target",
        },
        Some(_) => RouteDecision::Redirect {
            target_app_id: target_app_id.to_string(),
        },
    }
}

/// Parses the interim `PROCESS_ROUTES_TO_APPROVED` env-var shape
/// (`crate::lib::try_start_process_loop`): `target_app_id1:tenant1,
/// target_app_id2:tenant2`, into the `target_app_id -> approved tenant`
/// map [`resolve_cross_app_route`] expects -- see that function's doc for
/// why this is a TODO(M4+) substitute for a real `app_install_approvals`
/// lookup. A malformed entry (no `:`, an empty side) is logged at WARN and
/// skipped -- never a hard startup error, since an empty/partial map only
/// narrows which redirects are allowed (fails closed), never widens it.
pub fn parse_approved_targets(raw: &str) -> HashMap<String, String> {
    let mut approved = HashMap::new();
    for entry in raw.split(',') {
        let entry = entry.trim();
        if entry.is_empty() {
            continue;
        }
        match entry.split_once(':') {
            Some((app_id, tenant)) if !app_id.is_empty() && !tenant.is_empty() => {
                approved.insert(app_id.to_string(), tenant.to_string());
            }
            _ => {
                tracing::warn!(
                    entry,
                    "PROCESS_ROUTES_TO_APPROVED entry is malformed, skipping"
                );
            }
        }
    }
    approved
}

/// Runs the mandatory content-moderation gate (spec §4.2: "which no
/// community may opt out of") against an inbound event, before the
/// bundle's own `transform` is invoked. **TODO(M4+), honest seam**: the
/// real classifier (`LocalOllamaClassifier`, Python `moderation_module`)
/// calls an Ollama endpoint over the network, the PostHog master-flag
/// check (`waddles.community.content_moderation`, default OFF per
/// `rules/critical-rules.md` Feature Flags) needs a Rust PostHog client
/// that has not landed in this crate, and the reputation-service call
/// needs an HTTP client wired to `core/reputation_module` -- none of
/// which is realistic scope for this landing (task instruction: "honest
/// TODO-seam what you can't finish"; the invoke/enqueue path, items 1-2,
/// took priority). This function is the correct, already-wired call site
/// (would run before `transform`, fail-open on any internal error exactly
/// like the Python gate's own contract -- never blocks or alters the
/// message) and always returns `None` (no match) today. [`crate::spine`]
/// does not call it yet; wiring it in is the follow-up once the pieces
/// above land. [`maybe_build_enforcement_envelope`] below is exercised
/// directly by its own tests, not through a live classification.
pub fn run_moderation_gate(_event: &PlatformEvent) -> Option<serde_json::Value> {
    None
}

/// Builds the synthetic action-stage envelope routed onto
/// [`MODERATION_ENFORCE_APP_ID`]'s own action stream when
/// `enforcement_stamp` is `Some` (spec §4.2's "moderation-enforcement
/// routing" built-in; Python: `runner.py::_maybe_route_moderation_
/// enforcement`). Reuses `source`'s `binding`/`workstream_id`/`event_id`/
/// `trace` **unchanged** -- never mints a new MAC: spec §5.11's formula is
/// `HMAC(tenant, community, workstream_id, event_id, trace_id)`, and none
/// of those inputs differ between the source envelope and this synthetic
/// one (only `app_id`/`stage`/`event`/`ts` change), so the source's own
/// binding verifies byte-identically on read -- the same invariant
/// `crate::spine::handle_delivered` relies on for the ordinary
/// process→action hop.
pub fn maybe_build_enforcement_envelope(
    source: &StageEnvelope,
    enforcement_stamp: Option<&serde_json::Value>,
) -> Option<StageEnvelope> {
    let stamp = enforcement_stamp?;
    let mut payload = serde_json::Map::with_capacity(ENFORCEMENT_IDENTITY_PAYLOAD_KEYS.len() + 1);
    payload.insert("moderation_enforcement".to_string(), stamp.clone());
    for key in ENFORCEMENT_IDENTITY_PAYLOAD_KEYS {
        if let Some(value) = source.event.payload.get(*key) {
            payload.insert((*key).to_string(), value.clone());
        }
    }
    let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
    Some(StageEnvelope {
        schema_version: penguin_spine::ENVELOPE_SCHEMA_VERSION,
        tenant: source.tenant.clone(),
        community: source.community.clone(),
        app_id: MODERATION_ENFORCE_APP_ID.to_string(),
        stage: "action".to_string(),
        event: PlatformEvent {
            platform: source.event.platform.clone(),
            event_type: source.event.event_type.clone(),
            actor: source.event.actor.clone(),
            payload,
            occurred_at: now.clone(),
            source: None,
        },
        ts: now,
        target_app_id: None,
        workstream_id: source.workstream_id.clone(),
        event_id: source.event_id.clone(),
        session_id: source.session_id.clone(),
        trace: source.trace.clone(),
        binding: source.binding.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_envelope() -> StageEnvelope {
        serde_json::from_value(serde_json::json!({
            "schema_version": 2,
            "tenant": "acme",
            "community": "main",
            "app_id": "waddles.bot.commands.default",
            "stage": "process",
            "event": {
                "platform": "twitch",
                "event_type": "chat.message",
                "actor": "some_user",
                "payload": {"author_id": "u123", "channel_id": "c456", "text": "secret message"},
                "occurred_at": "2026-09-22T00:00:00.000Z",
                "source": null
            },
            "ts": "2026-09-22T00:00:00.000Z",
            "target_app_id": null,
            "workstream_id": "00000000-0000-0000-0000-000000000001",
            "event_id": "00000000-0000-4000-8000-000000000002",
            "session_id": null,
            "trace": null,
            "binding": {"kid": "k1", "mac": "a".repeat(64)}
        }))
        .unwrap()
    }

    #[test]
    fn no_target_app_id_key_is_same_app() {
        let mut payload = serde_json::Map::new();
        let decision = resolve_cross_app_route(&mut payload, "acme", &HashMap::new());
        assert_eq!(decision, RouteDecision::SameApp);
    }

    #[test]
    fn empty_target_app_id_is_same_app_and_key_is_popped() {
        let mut payload = serde_json::Map::new();
        payload.insert(PROCESS_TARGET_APP_ID_KEY.to_string(), serde_json::json!(""));
        let decision = resolve_cross_app_route(&mut payload, "acme", &HashMap::new());
        assert_eq!(decision, RouteDecision::SameApp);
        assert!(!payload.contains_key(PROCESS_TARGET_APP_ID_KEY));
    }

    #[test]
    fn approved_same_tenant_target_redirects() {
        let mut payload = serde_json::Map::new();
        payload.insert(
            PROCESS_TARGET_APP_ID_KEY.to_string(),
            serde_json::json!("waddles.community.forums.default"),
        );
        let mut approved = HashMap::new();
        approved.insert(
            "waddles.community.forums.default".to_string(),
            "acme".to_string(),
        );
        let decision = resolve_cross_app_route(&mut payload, "acme", &approved);
        assert_eq!(
            decision,
            RouteDecision::Redirect {
                target_app_id: "waddles.community.forums.default".to_string()
            }
        );
        assert!(!payload.contains_key(PROCESS_TARGET_APP_ID_KEY));
    }

    #[test]
    fn undeclared_target_is_denied_and_key_still_popped() {
        let mut payload = serde_json::Map::new();
        payload.insert(
            PROCESS_TARGET_APP_ID_KEY.to_string(),
            serde_json::json!("waddles.other.app.default"),
        );
        let decision = resolve_cross_app_route(&mut payload, "acme", &HashMap::new());
        assert_eq!(
            decision,
            RouteDecision::Denied {
                reason: "undeclared_or_unapproved"
            }
        );
        assert!(!payload.contains_key(PROCESS_TARGET_APP_ID_KEY));
    }

    #[test]
    fn cross_tenant_approved_target_is_denied_independent_of_install_time_check() {
        let mut payload = serde_json::Map::new();
        payload.insert(
            PROCESS_TARGET_APP_ID_KEY.to_string(),
            serde_json::json!("waddles.community.forums.default"),
        );
        let mut approved = HashMap::new();
        // Approved, but for a DIFFERENT tenant than the source envelope's.
        approved.insert(
            "waddles.community.forums.default".to_string(),
            "other-tenant".to_string(),
        );
        let decision = resolve_cross_app_route(&mut payload, "acme", &approved);
        assert_eq!(
            decision,
            RouteDecision::Denied {
                reason: "cross_tenant_target"
            }
        );
    }

    #[test]
    fn route_decision_metric_reason_only_set_when_denied() {
        assert_eq!(RouteDecision::SameApp.as_metric_reason(), None);
        assert_eq!(
            RouteDecision::Redirect {
                target_app_id: "x".to_string()
            }
            .as_metric_reason(),
            None
        );
        assert_eq!(
            RouteDecision::Denied {
                reason: "undeclared_or_unapproved"
            }
            .as_metric_reason(),
            Some("undeclared_or_unapproved")
        );
    }

    #[test]
    fn parse_approved_targets_reads_app_id_tenant_pairs() {
        let approved = parse_approved_targets(
            "waddles.community.forums.default:acme,waddles.community.moderation.default:acme",
        );
        assert_eq!(
            approved.get("waddles.community.forums.default"),
            Some(&"acme".to_string())
        );
        assert_eq!(
            approved.get("waddles.community.moderation.default"),
            Some(&"acme".to_string())
        );
    }

    #[test]
    fn parse_approved_targets_skips_malformed_entries() {
        let approved = parse_approved_targets("nocoloninhere,:emptyappid,emptytenant:,valid:acme");
        assert_eq!(approved.len(), 1);
        assert_eq!(approved.get("valid"), Some(&"acme".to_string()));
    }

    #[test]
    fn parse_approved_targets_empty_string_is_empty_map() {
        assert!(parse_approved_targets("").is_empty());
    }

    #[test]
    fn run_moderation_gate_is_a_documented_noop_seam() {
        let env = fixture_envelope();
        assert_eq!(run_moderation_gate(&env.event), None);
    }

    #[test]
    fn no_enforcement_stamp_builds_nothing() {
        let env = fixture_envelope();
        assert_eq!(maybe_build_enforcement_envelope(&env, None), None);
    }

    #[test]
    fn enforcement_stamp_builds_a_synthetic_envelope_reusing_the_source_binding() {
        let env = fixture_envelope();
        let stamp = serde_json::json!({"category": "spam", "score": 0.9, "timeout_s": 600, "warn_text": "stop", "action": "timeout+warn"});
        let synthetic = maybe_build_enforcement_envelope(&env, Some(&stamp)).unwrap();

        assert_eq!(synthetic.app_id, MODERATION_ENFORCE_APP_ID);
        assert_eq!(synthetic.stage, "action");
        assert_eq!(synthetic.tenant, env.tenant);
        assert_eq!(synthetic.community, env.community);
        assert_eq!(synthetic.workstream_id, env.workstream_id);
        assert_eq!(synthetic.event_id, env.event_id);
        assert_eq!(synthetic.binding, env.binding);
        assert_eq!(
            synthetic.event.payload.get("moderation_enforcement"),
            Some(&stamp)
        );
        // Identity fields carried, `text` never carried.
        assert_eq!(
            synthetic.event.payload.get("author_id"),
            Some(&serde_json::json!("u123"))
        );
        assert_eq!(
            synthetic.event.payload.get("channel_id"),
            Some(&serde_json::json!("c456"))
        );
        assert!(!synthetic.event.payload.contains_key("text"));
    }
}
