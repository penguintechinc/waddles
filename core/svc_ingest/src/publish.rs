//! One write per event (spec S10.6): wraps a normalized
//! `penguin_spine::PlatformEvent` in a `StageEnvelope` and `XADD`s it once
//! onto its ingest source's Valkey stream via `penguin_spine::SpineClient`.
//!
//! `app_id` is a synthetic per-**source** id (PA-ENVELOPE, see
//! `docs/superpowers/plans/2026-09-14-rust-data-plane-m5-svc-ingest.md`) --
//! ingest resolves no consumer and consults no manifest (D23/D24/S10.6).
//! This module also mints every D30 workstream-identity/trace/binding
//! field (ingest **mints**, it never **verifies** -- S5.11) via
//! `penguin_spine::KeyRing`/`compute_binding_mac`, the real landed
//! `penguin-spine` binding API (`kid` is supplied by the caller and folded
//! into the `Binding` this module constructs -- the crate itself has no
//! "active kid" concept, unlike an earlier plan draft that assumed a
//! richer `BindingKeyring::signing_kid_and_key()` helper that was never
//! landed).
//!
//! D31 usage-delta metering (`UsageBatcher`/`UsageDelta`/`append_usage`) is
//! **not yet implemented**: `penguin-spine` at the pinned rev
//! (`2976bf8a336a720ee5f939ad2d1a3f7b4b820642`) exports no `usage` module
//! (`Cargo.toml`'s dependency-pattern comment documents the exact revision
//! checked). This is a crate gap, not a local re-implementation --
//! `publish_event` records nothing onto `waddles:usage` today; wiring it in
//! is `// TODO(M5)`, blocked on that module landing in `penguin-spine`.

use std::sync::{Arc, OnceLock};

use penguin_spine::{
    compute_binding_mac, compute_binding_mac_v3, trace_id_from_traceparent, Binding, BindingError,
    KeyRing, PlatformEvent, Scope, SpineClient, SpineError, SpineMetrics, StageEnvelope, Trace,
    ENVELOPE_SCHEMA_VERSION, LEGACY_ENVELOPE_SCHEMA_VERSION,
};
use uuid::Uuid;

use crate::scope_resolver::{
    HubScopeSource, ResolvedScope, ScopeResolver, ScopeSource, Unresolved,
};

/// The process-wide dataplane scope resolver. Set once at startup, only when
/// `waddles.ingest-dynamic-connections` is ON and the resolver is fully
/// configured; unset means every event takes the unchanged v2/static path.
static SCOPE_RESOLVER: OnceLock<Arc<ScopeResolver<HubScopeSource>>> = OnceLock::new();

/// Installs the process-wide resolver; `false` if one was already installed.
pub fn install_scope_resolver(resolver: Arc<ScopeResolver<HubScopeSource>>) -> bool {
    SCOPE_RESOLVER.set(resolver).is_ok()
}

/// Fixed namespace for PA-WORKSTREAM's deterministic fixed-platform
/// workstream ids. Generated once and never regenerated -- changing it
/// would change every fixed-platform `workstream_id` on next deploy,
/// breaking usage-metering continuity once D31 lands (S5.12).
pub const WORKSTREAM_NAMESPACE: Uuid = Uuid::from_bytes([
    0x2c, 0x9e, 0x1a, 0x40, 0x6f, 0x8b, 0x4c, 0x1d, 0x9a, 0x3e, 0x7d, 0x2f, 0x51, 0x0a, 0x8b, 0x6c,
]);

/// Derives a stable `workstream_id` for a fixed-platform ingest source from
/// its `source_id` (the same string already used to build the Valkey
/// stream key) -- PA-WORKSTREAM. Same `source_id` in, same `workstream_id`
/// out, always: `Uuid::new_v5` is a pure function of its namespace + name.
#[must_use]
pub fn deterministic_workstream_id(source_id: &str) -> String {
    Uuid::new_v5(&WORKSTREAM_NAMESPACE, source_id.as_bytes()).to_string()
}

/// Sanitizes one `app_id` path segment (PA-ENVELOPE): lowercase, every
/// character outside `[a-z0-9_-]` becomes `-`, and any leading run of
/// characters outside `[a-z0-9]` is stripped, so the segment can never
/// violate `penguin_spine`'s `^waddles\.<seg>\.<seg>\.<seg>$` app_id regex.
#[must_use]
pub fn app_id_slug(raw: &str) -> String {
    let mut out: String = raw
        .to_ascii_lowercase()
        .chars()
        .map(|c| {
            if c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-' {
                c
            } else {
                '-'
            }
        })
        .collect();
    while out.starts_with(|c: char| !c.is_ascii_lowercase() && !c.is_ascii_digit()) {
        out.remove(0);
    }
    if out.is_empty() {
        out.push('x');
    }
    out
}

/// Errors [`publish_event`] can return: either the binding-MAC computation
/// failed (a misconfigured keyring/kid -- a startup-time invariant that
/// should never actually trip once [`crate::config`] validates the
/// keyring), or the downstream `XADD` itself failed.
#[derive(Debug, thiserror::Error)]
pub enum PublishError {
    /// `compute_binding_mac` rejected `active_kid`/the keyring.
    #[error("binding mac computation failed: {0}")]
    Binding(#[from] BindingError),
    /// `EventAppender::append` (the `XADD`) failed.
    #[error("spine append failed: {0}")]
    Append(#[from] SpineError),
    /// Dynamic-connections mode could not resolve the event's source to a
    /// real scope: the event is DROPPED (never published, never given a
    /// default tenant/community -- svc-ingest is a cross-tenant ingest point).
    #[error("event dropped, scope unresolved: {0}")]
    Unresolved(Unresolved),
    /// The numeric scope disagrees with the display strings about to be
    /// stamped -- a producer bug; refused loudly rather than published.
    #[error("inconsistent scope refused: {0}")]
    InconsistentScope(String),
}

/// Producer-side guard (finding 4): the authoritative numeric scope and the
/// string scope (stream key / `verify_hop` display scope) must agree --
/// `community.is_none() == (community_id == 0)`, positive tenant id, and the
/// strings are exactly what the numeric scope's resolution produced.
fn assert_scope_consistent(resolved: &ResolvedScope, scope: &Scope) -> Result<(), PublishError> {
    let bad = |why: &str| Err(PublishError::InconsistentScope(why.to_string()));
    if resolved.tenant_id < 1 {
        return bad("tenant_id must be >= 1");
    }
    if resolved.community_id < 0 {
        return bad("community_id must be >= 0");
    }
    if scope.community.is_none() != (resolved.community_id == 0) {
        return bad("community presence must match (community_id == 0)");
    }
    if scope.tenant != resolved.tenant_slug || scope.community != resolved.community_name {
        return bad("display strings diverge from the resolved scope");
    }
    Ok(())
}

/// Wraps `penguin_spine::SpineClient::append` behind a trait so callers
/// (and this module's own tests) never need a real Valkey connection.
/// A generic bound (not `dyn`) at every call site, matching
/// `core/svc_process/src/spine.rs`'s `StreamReader` precedent -- native
/// async-fn-in-traits isn't `dyn`-safe without boxing, and nothing here
/// needs runtime polymorphism across implementations.
///
/// `#[allow(async_fn_in_trait)]`: this trait must be `pub` (it appears in
/// `publish_event`'s public signature), so the crate-level "you can
/// suppress this if the trait is only used in your own code" escape hatch
/// applies literally -- `svc-ingest` is a deployed service binary, not a
/// published library other crates implement this trait against.
pub trait EventAppender: Send + Sync {
    /// Performs the `XADD`, returning the new entry id.
    fn append(
        &self,
        stream: &str,
        env: &StageEnvelope,
    ) -> impl std::future::Future<Output = Result<String, SpineError>> + Send;
}

impl EventAppender for SpineClient {
    async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
        SpineClient::append(self, stream, env).await
    }
}

/// A fresh W3C trace -- one per inbound event, never a continuation of any
/// inbound request's own trace (S5.11).
fn mint_trace() -> Trace {
    let trace_id = Uuid::new_v4().simple().to_string();
    let span_id = Uuid::new_v4().simple().to_string()[..16].to_string();
    Trace {
        traceparent: format!("00-{trace_id}-{span_id}-01"),
        tracestate: None,
    }
}

/// Publishes one normalized event onto its ingest source's stream, exactly
/// once, minting every D30 identity field -- the sole `StageEnvelope`
/// construction site in this crate.
///
/// `source_id` is this service's own stable identifier for the ingest
/// configuration the event came from (e.g. `tw-somechannel`,
/// `dg-123456789`); `workstream_id` is supplied by the caller
/// ([`deterministic_workstream_id`] for a fixed-platform source, PA-
/// WORKSTREAM); `active_kid`/`keyring` mint `binding.mac` under the
/// operator-configured active key version (`crate::config`).
///
/// With the dynamic-connections resolver installed ([`install_scope_resolver`])
/// the envelope is a schema-v3 one: numeric `tenant_id`/`community_id` resolved
/// from the event's platform source (an unresolvable source is dropped, never defaulted),
/// bound by `compute_binding_mac_v3`, and `scope` is ignored in favor of the
/// resolved scope. Without it, behavior is the unchanged v2/static path.
#[allow(clippy::too_many_arguments)]
pub async fn publish_event<A: EventAppender, M: SpineMetrics + ?Sized>(
    appender: &A,
    metrics: &M,
    keyring: &KeyRing,
    active_kid: &str,
    scope: &Scope,
    source_id: &str,
    workstream_id: &str,
    session_id: Option<&str>,
    event: PlatformEvent,
) -> Result<String, PublishError> {
    publish_event_with(
        SCOPE_RESOLVER.get().map(Arc::as_ref),
        appender,
        metrics,
        keyring,
        active_kid,
        scope,
        source_id,
        workstream_id,
        session_id,
        event,
    )
    .await
}

/// [`publish_event`] with an explicit resolver (`None` = v2/static path) --
/// the seam tests drive directly instead of the process-wide static.
#[allow(clippy::too_many_arguments)]
pub async fn publish_event_with<A: EventAppender, M: SpineMetrics + ?Sized, S: ScopeSource>(
    resolver: Option<&ScopeResolver<S>>,
    appender: &A,
    metrics: &M,
    keyring: &KeyRing,
    active_kid: &str,
    scope: &Scope,
    source_id: &str,
    workstream_id: &str,
    session_id: Option<&str>,
    event: PlatformEvent,
) -> Result<String, PublishError> {
    // v3: resolve the authoritative numeric scope from the event's platform
    // source (Discord guild id / Twitch channel login = `source.account_id`).
    // A DM / sourceless event, a miss, or a failed lookup is DROPPED: there is
    // no default scope to stamp, and processing under one would mis-route
    // another tenant's message.
    let resolved = match resolver {
        Some(r) => {
            let outcome = match event.source.as_ref().map(|s| s.account_id.as_str()) {
                Some(src) if !src.is_empty() && src != "dm" => {
                    r.resolve(&event.platform, src).await
                }
                _ => Err(r.no_source(&event.platform)),
            };
            match outcome {
                Ok(scope) => Some(scope),
                Err(why) => {
                    tracing::warn!(platform = %event.platform, reason = why.label(), "dropping event: scope unresolved");
                    return Err(PublishError::Unresolved(why));
                }
            }
        }
        None => None,
    };
    let v3_scope = resolved.as_ref().map(ResolvedScope::spine_scope);
    let scope = match (&resolved, &v3_scope) {
        (Some(res), Some(sc)) => {
            assert_scope_consistent(res, sc)?;
            sc
        }
        _ => scope,
    };
    let stream = scope.source_stream(&event.platform, source_id);
    let platform = event.platform.clone();

    let trace = mint_trace();
    let trace_id = trace_id_from_traceparent(&trace.traceparent)
        .expect("mint_trace always produces a valid W3C traceparent")
        .to_string();
    let event_id = Uuid::new_v4().to_string();

    let mac = match &resolved {
        Some(res) => compute_binding_mac_v3(
            keyring,
            active_kid,
            res.tenant_id,
            res.community_id,
            &scope.tenant,
            scope.community.as_deref(),
            workstream_id,
            &event_id,
            Some(&trace_id),
        )?,
        None => compute_binding_mac(
            keyring,
            active_kid,
            &scope.tenant,
            scope.community.as_deref(),
            workstream_id,
            &event_id,
            Some(&trace_id),
        )?,
    };
    let binding = Binding {
        kid: active_kid.to_string(),
        mac,
    };

    let env = StageEnvelope {
        // Resolver on -> schema v3 with the numeric ids (covered by the v3 MAC;
        // the strings are display-only). Resolver off -> the string-scope v2
        // shape, where the ids MUST stay `None` (a v2 MAC does not cover them).
        schema_version: if resolved.is_some() {
            ENVELOPE_SCHEMA_VERSION
        } else {
            LEGACY_ENVELOPE_SCHEMA_VERSION
        },
        tenant: scope.tenant.clone(),
        community: scope.community.clone(),
        tenant_id: resolved.as_ref().map(|r| r.tenant_id),
        community_id: resolved.as_ref().map(|r| r.community_id),
        app_id: format!(
            "waddles.ingest.{}.{}",
            app_id_slug(&platform),
            app_id_slug(source_id)
        ),
        // "process", NOT "ingest": `StageEnvelope.stage` names the stage
        // that will read/consume this entry off the stream and therefore
        // owns its DLQ routing -- the same forward-looking convention this
        // crate's own sibling, `svc_process::spine`, already follows when
        // *it* produces an envelope for svc-action to consume (it stamps
        // `stage: "action"`, never "process", its own identity). Every
        // process bundle granted this ingest-source stream reads it via
        // `penguin_spine::GroupReader`/`SpineClient::dead_letter`, which
        // parses `d.env.stage` with `Stage::parse` -- that parser only
        // accepts `"process"`/`"action"` (`penguin_spine::Stage` has no
        // `Ingest` variant: ingest only ever writes, never consumes, so it
        // never dead-letters under its own name). Stamping "ingest" here
        // was a bug: any DLQ-worthy entry read off an ingest-source stream
        // (hop-verification failure, executor-unavailable, bundle error,
        // ...) made `dead_letter` fail with `SpineError::Config("spine
        // config error: unsupported stage for spine DLQ routing:
        // \"ingest\" ...")`, which propagated out of `drain_loop` and
        // killed the entire process-stage drain loop on the very first
        // failure -- a live incident on the Discord ingest-source stream.
        stage: "process".to_string(),
        event,
        ts: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
        target_app_id: None,
        workstream_id: workstream_id.to_string(),
        event_id,
        session_id: session_id.map(str::to_string),
        trace: Some(trace),
        binding,
    };

    let entry_id = appender.append(&stream, &env).await?;
    metrics.stream_event_written(&platform, source_id);
    Ok(entry_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use penguin_spine::Source;
    use std::sync::Mutex;

    struct FakeAppender {
        calls: Mutex<Vec<(String, StageEnvelope)>>,
        fail: bool,
    }

    impl EventAppender for FakeAppender {
        async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
            if self.fail {
                return Err(SpineError::Config("connection refused".to_string()));
            }
            self.calls
                .lock()
                .unwrap()
                .push((stream.to_string(), env.clone()));
            Ok("1757851200000-0".to_string())
        }
    }

    #[derive(Default)]
    struct RecordingMetrics {
        calls: Mutex<Vec<(String, String)>>,
    }

    impl SpineMetrics for RecordingMetrics {
        fn stream_event_written(&self, platform: &str, source_id: &str) {
            self.calls
                .lock()
                .unwrap()
                .push((platform.to_string(), source_id.to_string()));
        }
    }

    fn test_event() -> PlatformEvent {
        PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some("alice".to_string()),
            payload: serde_json::json!({"text": "hi"})
                .as_object()
                .unwrap()
                .clone(),
            occurred_at: "2026-09-14T12:00:00.000Z".to_string(),
            source: Some(Source {
                platform: "twitch".to_string(),
                account_id: "bot-primary".to_string(),
                channel_id: Some("chan".to_string()),
            }),
        }
    }

    fn test_keyring() -> KeyRing {
        KeyRing::new(vec![("test-kid".to_string(), vec![7u8; 32])])
    }

    #[tokio::test]
    async fn writes_onto_the_correct_source_stream_with_d30_fields_populated() {
        let appender = FakeAppender {
            calls: Mutex::new(vec![]),
            fail: false,
        };
        let metrics = RecordingMetrics::default();
        let keyring = test_keyring();
        let scope = Scope::new("acme", Some("main".to_string()));
        let entry_id = publish_event(
            &appender,
            &metrics,
            &keyring,
            "test-kid",
            &scope,
            "tw-channelA",
            "ws-1",
            None,
            test_event(),
        )
        .await
        .unwrap();
        assert_eq!(entry_id, "1757851200000-0");

        let calls = appender.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        let (stream, env) = &calls[0];
        assert_eq!(
            stream,
            "waddles:t:acme:c:main:src:twitch:tw-channelA:events"
        );
        assert_eq!(env.schema_version, LEGACY_ENVELOPE_SCHEMA_VERSION);
        assert_eq!(env.stage, "process");
        assert_eq!(env.app_id, "waddles.ingest.twitch.tw-channela");
        assert_eq!(env.target_app_id, None);
        assert_eq!(env.tenant, "acme");
        assert_eq!(env.community.as_deref(), Some("main"));
        assert_eq!(env.workstream_id, "ws-1");
        assert!(
            Uuid::parse_str(&env.event_id).is_ok(),
            "event_id must be a UUID"
        );
        assert_eq!(env.session_id, None);
        let trace = env
            .trace
            .as_ref()
            .expect("publish_event always mints a trace");
        assert!(trace.traceparent.starts_with("00-"));
        assert_eq!(env.binding.kid, "test-kid");
        assert_eq!(env.binding.mac.len(), 64);
        assert!(env.binding.mac.chars().all(|c| c.is_ascii_hexdigit()));

        assert_eq!(
            *metrics.calls.lock().unwrap(),
            vec![("twitch".to_string(), "tw-channelA".to_string())]
        );
    }

    /// Regression: a live incident (svc-process's drain loop crash-looping
    /// on the Discord ingest-source stream) traced back to this crate
    /// stamping `stage: "ingest"` on every envelope it writes onto an
    /// ingest-source stream. `penguin_spine::Stage::parse` -- the function
    /// `SpineClient::dead_letter` calls on `d.env.stage` to route a DLQ
    /// write -- only accepts `"process"`/`"action"`; `"ingest"` is
    /// deliberately rejected there because ingest never consumes and so
    /// never dead-letters under its own name. Any entry needing dead-letter
    /// handling once a process bundle reads it off this stream would
    /// otherwise crash `dead_letter` with `SpineError::Config`, which
    /// propagates out of the process-stage drain loop and kills it. This
    /// test asserts the envelope this crate produces is always
    /// DLQ-routable by its actual (forward-looking) consumer, not just that
    /// `stage` holds a particular literal.
    #[tokio::test]
    async fn published_envelope_stage_is_dlq_routable_by_penguin_spine() {
        let appender = FakeAppender {
            calls: Mutex::new(vec![]),
            fail: false,
        };
        let (metrics, keyring) = (RecordingMetrics::default(), test_keyring());
        let scope = Scope::new("acme", Some("main".to_string()));
        publish_event(
            &appender,
            &metrics,
            &keyring,
            "test-kid",
            &scope,
            "dg-guildX",
            "ws-1",
            None,
            test_event(),
        )
        .await
        .unwrap();

        let calls = appender.calls.lock().unwrap();
        let (_, env) = &calls[0];
        assert_eq!(
            env.stage, "process",
            "ingest-source-stream envelopes must name their consumer's \
             stage (\"process\"), never ingest's own identity"
        );
        assert!(
            penguin_spine::Stage::parse(&env.stage).is_ok(),
            "envelope stage {:?} must be one penguin_spine::SpineClient::dead_letter \
             can route (\"process\"/\"action\") -- \"ingest\" crashes it, see gh incident \
             \"unsupported stage for spine DLQ routing\"",
            env.stage
        );
    }

    #[tokio::test]
    async fn session_id_is_carried_through_when_supplied() {
        let appender = FakeAppender {
            calls: Mutex::new(vec![]),
            fail: false,
        };
        let (metrics, keyring) = (RecordingMetrics::default(), test_keyring());
        let scope = Scope::new("acme", None);
        publish_event(
            &appender,
            &metrics,
            &keyring,
            "test-kid",
            &scope,
            "tw-eventsub-999",
            "ws-2",
            Some("session-abc"),
            test_event(),
        )
        .await
        .unwrap();
        assert_eq!(
            appender.calls.lock().unwrap()[0].1.session_id.as_deref(),
            Some("session-abc")
        );
    }

    #[tokio::test]
    async fn every_call_mints_a_distinct_event_id_and_trace() {
        let appender = FakeAppender {
            calls: Mutex::new(vec![]),
            fail: false,
        };
        let (metrics, keyring) = (RecordingMetrics::default(), test_keyring());
        let scope = Scope::new("acme", None);
        publish_event(
            &appender,
            &metrics,
            &keyring,
            "test-kid",
            &scope,
            "tw-channelA",
            "ws-1",
            None,
            test_event(),
        )
        .await
        .unwrap();
        publish_event(
            &appender,
            &metrics,
            &keyring,
            "test-kid",
            &scope,
            "tw-channelA",
            "ws-1",
            None,
            test_event(),
        )
        .await
        .unwrap();
        let calls = appender.calls.lock().unwrap();
        assert_ne!(calls[0].1.event_id, calls[1].1.event_id);
        assert_ne!(
            calls[0].1.trace.as_ref().unwrap().traceparent,
            calls[1].1.trace.as_ref().unwrap().traceparent
        );
        // Different event_id/trace -> different binding.mac (HMAC input
        // changed), proving the mac is not accidentally constant.
        assert_ne!(calls[0].1.binding.mac, calls[1].1.binding.mac);
    }

    #[tokio::test]
    async fn append_failure_propagates_as_publish_error_and_records_no_metric() {
        let appender = FakeAppender {
            calls: Mutex::new(vec![]),
            fail: true,
        };
        let (metrics, keyring) = (RecordingMetrics::default(), test_keyring());
        let scope = Scope::new("acme", None);
        let err = publish_event(
            &appender,
            &metrics,
            &keyring,
            "test-kid",
            &scope,
            "tw-channelA",
            "ws-1",
            None,
            test_event(),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, PublishError::Append(_)));
        assert!(err.to_string().contains("connection refused"));
        assert!(metrics.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn unknown_active_kid_fails_closed_before_any_append() {
        let appender = FakeAppender {
            calls: Mutex::new(vec![]),
            fail: false,
        };
        let (metrics, keyring) = (RecordingMetrics::default(), test_keyring());
        let scope = Scope::new("acme", None);
        let err = publish_event(
            &appender,
            &metrics,
            &keyring,
            "no-such-kid",
            &scope,
            "tw-channelA",
            "ws-1",
            None,
            test_event(),
        )
        .await
        .unwrap_err();
        assert!(matches!(
            err,
            PublishError::Binding(BindingError::UnknownKid(_))
        ));
        assert!(appender.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn tenant_wide_community_renders_as_the_literal_tenant_segment() {
        let appender = FakeAppender {
            calls: Mutex::new(vec![]),
            fail: false,
        };
        let (metrics, keyring) = (RecordingMetrics::default(), test_keyring());
        let scope = Scope::new("acme", None);
        publish_event(
            &appender,
            &metrics,
            &keyring,
            "test-kid",
            &scope,
            "tw-channelA",
            "ws-1",
            None,
            test_event(),
        )
        .await
        .unwrap();
        assert_eq!(
            appender.calls.lock().unwrap()[0].0,
            "waddles:t:acme:c:_tenant:src:twitch:tw-channelA:events"
        );
    }

    #[test]
    fn deterministic_workstream_id_is_stable_per_source_id() {
        let a = deterministic_workstream_id("tw-waddlebot");
        let b = deterministic_workstream_id("tw-waddlebot");
        let c = deterministic_workstream_id("dg-guild123");
        assert_eq!(a, b);
        assert_ne!(a, c);
        assert!(Uuid::parse_str(&a).is_ok());
    }

    #[test]
    fn app_id_slug_sanitizes_a_custom_platform_string() {
        assert_eq!(app_id_slug("custom:github"), "custom-github");
        assert_eq!(app_id_slug("Twitch"), "twitch");
        assert_eq!(app_id_slug("---leading-punct"), "leading-punct");
        assert_eq!(app_id_slug(""), "x");
    }

    #[tokio::test]
    async fn a_custom_platform_produces_a_regex_valid_app_id() {
        let appender = FakeAppender {
            calls: Mutex::new(vec![]),
            fail: false,
        };
        let (metrics, keyring) = (RecordingMetrics::default(), test_keyring());
        let scope = Scope::new("acme", None);
        let mut event = test_event();
        event.platform = "custom:github".to_string();
        publish_event(
            &appender,
            &metrics,
            &keyring,
            "test-kid",
            &scope,
            "acme-github",
            "ws-1",
            None,
            event,
        )
        .await
        .unwrap();
        let app_id = &appender.calls.lock().unwrap()[0].1.app_id;
        assert_eq!(app_id, "waddles.ingest.custom-github.acme-github");
        assert!(app_id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '-'));
    }

    #[test]
    fn multi_key_ring_mints_under_the_requested_kid_not_just_the_first() {
        let ring = KeyRing::new(vec![
            ("k1".to_string(), vec![1u8; 32]),
            ("k2".to_string(), vec![2u8; 32]),
        ]);
        let mac_k1 =
            compute_binding_mac(&ring, "k1", "acme", None, "ws-1", "evt-1", Some("trace-1"))
                .unwrap();
        let mac_k2 =
            compute_binding_mac(&ring, "k2", "acme", None, "ws-1", "evt-1", Some("trace-1"))
                .unwrap();
        assert_ne!(mac_k1, mac_k2);
    }

    #[test]
    fn workstream_namespace_constant_is_a_valid_uuid_and_stable() {
        // Regression guard: WORKSTREAM_NAMESPACE must never be silently
        // regenerated (S5.12 usage-metering continuity).
        assert_eq!(
            WORKSTREAM_NAMESPACE.to_string(),
            "2c9e1a40-6f8b-4c1d-9a3e-7d2f510a8b6c"
        );
    }

    // ---- schema-v3 producer (dataplane scope resolver wired) ----

    use crate::scope_resolver::{
        FetchError, RawScope, ResolverConfig, ResolverMetrics, ScopeResolver, ScopeSource,
    };
    use penguin_spine::verify_binding;

    struct FixedSource(Result<RawScope, FetchError>);
    impl ScopeSource for FixedSource {
        async fn fetch(&self, _p: &str, _s: &str) -> Result<RawScope, FetchError> {
            self.0.clone()
        }
    }

    struct NoMetrics;
    impl ResolverMetrics for NoMetrics {
        fn scope_resolution(&self, _platform: &str, _outcome: &'static str) {}
    }

    fn resolver_for(reply: Result<RawScope, FetchError>) -> ScopeResolver<FixedSource> {
        ScopeResolver::new(
            FixedSource(reply),
            ResolverConfig::default(),
            Arc::new(NoMetrics),
        )
    }

    fn resolved_ok() -> Result<RawScope, FetchError> {
        Ok(RawScope {
            tenant_id: 2,
            community_id: 9,
            tenant_slug: "acme".to_string(),
            community_name: "main".to_string(),
        })
    }

    async fn publish_v3(
        resolver: Option<&ScopeResolver<FixedSource>>,
    ) -> (String, StageEnvelope, KeyRing) {
        let appender = FakeAppender {
            calls: Mutex::new(vec![]),
            fail: false,
        };
        let keyring = test_keyring();
        publish_event_with(
            resolver,
            &appender,
            &RecordingMetrics::default(),
            &keyring,
            "test-kid",
            &Scope::new("static-tenant", None),
            "tw-channelA",
            "ws-1",
            None,
            test_event(),
        )
        .await
        .unwrap();
        let (stream, env) = appender.calls.lock().unwrap()[0].clone();
        (stream, env, keyring)
    }

    #[tokio::test]
    async fn resolver_on_stamps_v3_ids_and_a_verifiable_v3_mac() {
        let r = resolver_for(resolved_ok());
        let (stream, env, ring) = publish_v3(Some(&r)).await;
        assert_eq!(env.schema_version, ENVELOPE_SCHEMA_VERSION);
        assert_eq!((env.tenant_id, env.community_id), (Some(2), Some(9)));
        // Display strings are present, and the stream key is built from them
        // (what verify_hop string-compares) -- never from the static scope.
        assert_eq!(
            (env.tenant.as_str(), env.community.as_deref()),
            ("acme", Some("main"))
        );
        assert_eq!(
            stream,
            "waddles:t:acme:c:main:src:twitch:tw-channelA:events"
        );
        verify_binding(&ring, &env).expect("v3 binding.mac verifies");
    }

    #[tokio::test]
    async fn mutating_any_id_or_display_string_invalidates_the_v3_mac() {
        let r = resolver_for(resolved_ok());
        let (_s, env, ring) = publish_v3(Some(&r)).await;
        let mut t = env.clone();
        t.tenant_id = Some(3);
        assert!(verify_binding(&ring, &t).is_err(), "tenant_id swap");
        let mut c = env.clone();
        c.community_id = Some(10);
        assert!(verify_binding(&ring, &c).is_err(), "community_id swap");
        let mut d = env.clone();
        d.tenant = "evil".to_string();
        assert!(
            verify_binding(&ring, &d).is_err(),
            "display string is MAC-bound too"
        );
        let mut dg = env.clone();
        dg.schema_version = LEGACY_ENVELOPE_SCHEMA_VERSION;
        dg.tenant_id = None;
        dg.community_id = None;
        assert!(
            verify_binding(&ring, &dg).is_err(),
            "v3 mac never verifies as v2"
        );
    }

    #[tokio::test]
    async fn unresolvable_sources_are_dropped_never_stamped_with_a_default_scope() {
        for err in [
            FetchError::NotFound,
            FetchError::Ambiguous,
            FetchError::Invalid,
            FetchError::Transient("down"),
        ] {
            let r = resolver_for(Err(err));
            let appender = FakeAppender {
                calls: Mutex::new(vec![]),
                fail: false,
            };
            let res = publish_event_with(
                Some(&r),
                &appender,
                &RecordingMetrics::default(),
                &test_keyring(),
                "test-kid",
                &Scope::new("static-tenant", None),
                "tw-channelA",
                "ws-1",
                None,
                test_event(),
            )
            .await;
            assert!(matches!(res, Err(PublishError::Unresolved(_))), "{res:?}");
            assert!(
                appender.calls.lock().unwrap().is_empty(),
                "nothing may be published (no default tenant/community)"
            );
        }
    }

    #[tokio::test]
    async fn sourceless_and_dm_events_are_dropped_without_resolving() {
        let r = resolver_for(resolved_ok());
        for source in [None, Some("dm")] {
            let mut ev = test_event();
            ev.source = source.map(|a| Source {
                platform: "discord".to_string(),
                account_id: a.to_string(),
                channel_id: None,
            });
            let appender = FakeAppender {
                calls: Mutex::new(vec![]),
                fail: false,
            };
            let res = publish_event_with(
                Some(&r),
                &appender,
                &RecordingMetrics::default(),
                &test_keyring(),
                "test-kid",
                &Scope::new("static-tenant", None),
                "dg-1",
                "ws-1",
                None,
                ev,
            )
            .await;
            assert!(matches!(
                res,
                Err(PublishError::Unresolved(Unresolved::NoSource))
            ));
            assert!(appender.calls.lock().unwrap().is_empty());
        }
    }

    #[tokio::test]
    async fn flag_off_resolver_none_keeps_the_v2_static_output_unchanged() {
        let appender = FakeAppender {
            calls: Mutex::new(vec![]),
            fail: false,
        };
        let keyring = test_keyring();
        publish_event_with::<_, _, FixedSource>(
            None,
            &appender,
            &RecordingMetrics::default(),
            &keyring,
            "test-kid",
            &Scope::new("acme", Some("main".to_string())),
            "tw-channelA",
            "ws-1",
            None,
            test_event(),
        )
        .await
        .unwrap();
        let (stream, env) = appender.calls.lock().unwrap()[0].clone();
        assert_eq!(
            stream,
            "waddles:t:acme:c:main:src:twitch:tw-channelA:events"
        );
        assert_eq!(env.schema_version, LEGACY_ENVELOPE_SCHEMA_VERSION);
        assert_eq!((env.tenant_id, env.community_id), (None, None));
        verify_binding(&keyring, &env).expect("v2 mac verifies");
        let trace_id = env
            .trace
            .as_ref()
            .and_then(|t| trace_id_from_traceparent(&t.traceparent))
            .map(str::to_string);
        let v2 = compute_binding_mac(
            &keyring,
            "test-kid",
            "acme",
            Some("main"),
            &env.workstream_id,
            &env.event_id,
            trace_id.as_deref(),
        )
        .unwrap();
        assert_eq!(env.binding.mac, v2, "exactly today's v2 MAC");
    }

    #[test]
    fn producer_assertion_rejects_numeric_string_divergence() {
        let ok = ResolvedScope::validated(2, 9, "acme", Some("main")).unwrap();
        assert!(assert_scope_consistent(&ok, &ok.spine_scope()).is_ok());
        // community string present but id says tenant-wide
        let mut bad = ok.clone();
        bad.community_id = 0;
        assert!(matches!(
            assert_scope_consistent(&bad, &ok.spine_scope()),
            Err(PublishError::InconsistentScope(_))
        ));
        // tenant-wide id but a community string
        let wide = ResolvedScope::validated(2, 0, "acme", None).unwrap();
        assert!(assert_scope_consistent(&wide, &Scope::new("acme", Some("main".into()))).is_err());
        // display strings diverge from the resolved scope
        assert!(assert_scope_consistent(&ok, &Scope::new("acme", Some("other".into()))).is_err());
        let mut neg = ok.clone();
        neg.tenant_id = 0;
        assert!(assert_scope_consistent(&neg, &ok.spine_scope()).is_err());
    }
}
