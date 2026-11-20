//! `POST .../configs/{config_id}/start`, `POST .../stop`,
//! `GET .../status` -- pipeline lifecycle, delegated to whichever
//! [`crate::api::engine::SharedEngine`] is wired into the router. Ported
//! from the Python alpha's `blueprints/streaming.py`
//! `start_forwarding`/`stop_forwarding`/`get_status`; the real ffmpeg
//! subprocess (`services/ffmpeg_engine.py`) is not reimplemented here --
//! that is `crate::pipeline::supervisor`'s job (S3). TRANSCODE-token
//! admission (`services/token_ledger_client.py`) **is** reimplemented here
//! (slice S2 of the Rust rewrite, `crate::billing::token_ledger`) since
//! [`start`] is this crate's direct analog of Python's
//! `start_forwarding`: the only handler holding both the caller's bearer
//! JWT and a `streaming_configs.transcode_enabled` row at the same time.

use axum::extract::{Path, State};
use axum::Extension;
use sea_orm::DatabaseConnection;
use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};

use crate::billing::token_ledger::{TokenLedgerClient, REASON_IDEMPOTENCY_CONFLICT};
use crate::db::entities::streaming_config;
use crate::error::ApiError;
use crate::http::auth::AuthenticatedClaims;
use crate::http::AppState;
use crate::pipeline::{
    InputSpec, ObjectStoreRef, OutputSpec, PipelineError, PipelineSpec, PipelineState,
};
use crate::spec_builder::{plan_push_outputs, DEFAULT_PROFILE};

use super::common::{enabled_targets, fetch_config, pipeline_id_for_config};
use super::dto::PipelineStatusDto;
use super::engine::SharedEngine;
use super::extract::DbConn;
use super::response::ApiSuccess;
use super::tenancy::assert_tenant_owns_community;

fn map_pipeline_error(err: PipelineError) -> ApiError {
    match err {
        PipelineError::NotFound(id) => ApiError::NotFound(format!("pipeline {id} not found")),
        PipelineError::InvalidSpec(msg) => ApiError::BadRequest(msg),
        PipelineError::Unimplemented(what) => ApiError::Unimplemented(what.to_string()),
        // Not a true failure (see `PipelineError::NoFfmpegNeeded`'s own doc
        // comment) but this API layer does not orchestrate the RTP-direct
        // forward path it signals -- reported as not-yet-implemented rather
        // than silently claiming success.
        PipelineError::NoFfmpegNeeded => ApiError::Unimplemented(
            "pipeline requires no ffmpeg process (pure RTP forward); not orchestrated at the API layer yet"
                .into(),
        ),
        PipelineError::Unsupported(what) => {
            ApiError::Unimplemented(format!("{what} is not supported yet"))
        }
        PipelineError::Other(err) => ApiError::Internal(err),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn idempotency_key_is_stable_across_retries_and_timestamp_free() {
        let first = admission_idempotency_key(7, 3, 42, "adm-1");
        std::thread::sleep(std::time::Duration::from_millis(5));
        let retry = admission_idempotency_key(7, 3, 42, "adm-1");
        assert_eq!(first, retry);
        assert_eq!(first, "stream:7:3:42:adm-1");
        assert_ne!(first, admission_idempotency_key(7, 3, 42, "adm-2"));
        assert_ne!(first, admission_idempotency_key(7, 4, 42, "adm-1"));
        assert_ne!(first, admission_idempotency_key(8, 3, 42, "adm-1"));
        assert_ne!(first, admission_idempotency_key(7, 3, 43, "adm-1"));
    }

    /// Mirrors `resolve_transcode_admission`'s key derivation for a header-less request.
    fn headerless_key(community_id: i32, config_id: i32, actor: i64) -> String {
        let id = run_session_id(community_id, config_id, actor).id;
        admission_idempotency_key(community_id, config_id, actor, &id)
    }

    #[test]
    fn headerless_retries_of_the_same_admission_produce_the_same_key() {
        // Distinct ids so this test cannot interact with others sharing the process-wide map.
        let (community, config, actor) = (9_001, 9_002, 9_003);
        let first = headerless_key(community, config, actor);
        std::thread::sleep(std::time::Duration::from_millis(5));
        let retry = headerless_key(community, config, actor);
        assert_eq!(first, retry, "a retry must reuse the run's key");
        assert!(first.starts_with("stream:9001:9002:9003:"));

        // Another member starting the same config is a different admission.
        assert_ne!(first, headerless_key(community, config, actor + 1));
    }

    #[test]
    fn a_started_admission_key_is_never_reused_for_a_new_start() {
        let (community, config, actor) = (9_201, 9_202, 9_203);
        let first = headerless_key(community, config, actor);
        // The pipeline started: its admission is spent.
        let adm = run_session_id(community, config, actor);
        assert!(claim_run_start(community, config, actor, &adm));
        let second = headerless_key(community, config, actor);
        assert_ne!(first, second, "a new admission must carry a new key");
        // ...and a retry of that new admission is stable again.
        assert_eq!(second, headerless_key(community, config, actor));
    }

    /// regression: start/stop/start interleaving. Request B captures its admission BEFORE run A's
    /// start claims it; A starts and is stopped; B must NOT start run 2 on that admission (its
    /// debit would replay run 1's key within hub-api's 600s TTL and run 2 would ride free).
    #[test]
    fn interleaved_start_stop_start_cannot_reuse_the_first_runs_admission() {
        let (community, config, actor) = (9_401, 9_402, 9_403);
        let run1 = run_session_id(community, config, actor);
        let b = run_session_id(community, config, actor);
        assert_eq!(run1, b, "concurrent starts share the admission (dedupe)");
        assert!(claim_run_start(community, config, actor, &run1), "A starts");
        end_run_session(community, config, actor); // stop
        assert!(
            !claim_run_start(community, config, actor, &b),
            "B's stale admission must be rejected"
        );
        let run2 = run_session_id(community, config, actor);
        assert_ne!(run1.id, run2.id, "re-admission carries a new debit key");
        assert_eq!(run2.epoch, run1.epoch + 1);
        assert!(claim_run_start(community, config, actor, &run2));
    }

    /// Eviction at capacity must not rewind epochs: an admission taken before the clear stays
    /// stale (an old epoch-0 admission must not pass after the map is wiped).
    #[test]
    fn eviction_at_capacity_never_lets_an_old_admission_claim() {
        let mut state = RunState::new(2);
        let victim = (1, 1, 1);
        let old = state.admission(victim);
        assert_eq!(old.epoch, 0);
        // Fill the epoch map to capacity with other actors' runs.
        for actor in 10..12 {
            let adm = state.admission((1, 1, actor));
            assert!(state.claim((1, 1, actor), &adm));
        }
        // The next new key triggers eviction.
        let other = state.admission((1, 1, 99));
        assert!(state.claim((1, 1, 99), &other));
        assert!(
            !state.claim(victim, &old),
            "a pre-eviction admission must be stale, not match the reset epoch"
        );
        // Re-admitting yields a fresh key under the raised floor, which claims normally.
        let fresh = state.admission(victim);
        assert!(fresh.epoch > old.epoch);
        assert_ne!(fresh.id, old.id);
        assert!(state.claim(victim, &fresh));
    }

    #[test]
    fn concurrent_starts_of_one_run_claim_exactly_once() {
        let (community, config, actor) = (9_501, 9_502, 9_503);
        let admission = run_session_id(community, config, actor);
        let wins: usize = std::thread::scope(|s| {
            (0..8)
                .map(|_| s.spawn(|| claim_run_start(community, config, actor, &admission)))
                .collect::<Vec<_>>()
                .into_iter()
                .map(|h| usize::from(h.join().unwrap()))
                .sum()
        });
        assert_eq!(wins, 1);
    }

    #[test]
    fn a_retry_of_the_same_start_dedupes_and_a_failed_start_can_be_retried_free() {
        let (community, config, actor) = (9_601, 9_602, 9_603);
        let first = run_session_id(community, config, actor);
        assert_eq!(
            first,
            run_session_id(community, config, actor),
            "retry = same key"
        );
        // Claimed, but the pipeline failed to start: the retry reuses the debited key.
        assert!(claim_run_start(community, config, actor, &first));
        release_run_start(community, config, actor, &first);
        let retry = run_session_id(community, config, actor);
        assert_eq!(
            first, retry,
            "retry of the failed start must not be charged again"
        );
        assert!(claim_run_start(community, config, actor, &retry));
    }

    #[test]
    fn a_refused_key_is_retired_without_touching_other_actors() {
        let (community, config) = (9_301, 9_302);
        let mine = headerless_key(community, config, 1);
        let other = headerless_key(community, config, 2);
        end_run_session(community, config, 1);
        assert_ne!(mine, headerless_key(community, config, 1));
        assert_eq!(other, headerless_key(community, config, 2));
    }

    #[test]
    fn stop_ends_the_actors_run_so_the_next_start_is_a_new_admission() {
        let (community, config, actor) = (9_101, 9_102, 9_103);
        let first = headerless_key(community, config, actor);
        assert_eq!(first, headerless_key(community, config, actor));
        end_run_session(community, config, actor);
        assert_ne!(first, headerless_key(community, config, actor));
    }

    #[test]
    fn map_pipeline_error_covers_every_variant() {
        assert!(matches!(
            map_pipeline_error(PipelineError::NotFound(uuid::Uuid::nil())),
            ApiError::NotFound(_)
        ));
        assert!(matches!(
            map_pipeline_error(PipelineError::InvalidSpec("bad spec".into())),
            ApiError::BadRequest(_)
        ));
        assert!(matches!(
            map_pipeline_error(PipelineError::Unimplemented("x")),
            ApiError::Unimplemented(_)
        ));
        assert!(matches!(
            map_pipeline_error(PipelineError::NoFfmpegNeeded),
            ApiError::Unimplemented(_)
        ));
        assert!(matches!(
            map_pipeline_error(PipelineError::Unsupported("multi-input")),
            ApiError::Unimplemented(_)
        ));
        assert!(matches!(
            map_pipeline_error(PipelineError::Other(anyhow::anyhow!("boom"))),
            ApiError::Internal(_)
        ));
    }
}

/// Builds the [`PipelineSpec`] for `config` from its DB row plus its
/// enabled `streaming_targets`. `source_url` is mapped to a single
/// [`InputSpec::Pull`] (migration 079 stores one ingest URL per config,
/// not a per-protocol stream key/token) and every enabled target becomes
/// an [`OutputSpec::RtmpPush`] or [`OutputSpec::SrtPush`] according to its
/// `protocol`, encoded with the codec the config/target select
/// ([`plan_push_outputs`] -- the same rules the write handlers and the
/// ingest orchestrator apply, so a combination ffmpeg cannot mux is a `400`
/// here rather than a destination silently dropped later).
/// `transcode_applied` is resolved by the caller
/// ([`resolve_transcode_admission`]) before this is called -- building the
/// spec itself never touches the token ledger.
async fn build_pipeline_spec(
    db: &DatabaseConnection,
    tenant: &str,
    community_id: i32,
    config: &streaming_config::Model,
    transcode_applied: bool,
) -> Result<PipelineSpec, ApiError> {
    let targets = enabled_targets(db, config.id).await?;
    let plan = plan_push_outputs(config, &targets, transcode_applied)?;

    let mut outputs = plan.outputs;
    if config.record_enabled {
        outputs.push(OutputSpec::Record {
            profile: DEFAULT_PROFILE.into(),
            target: ObjectStoreRef {
                store: "default".into(),
                prefix: format!("{tenant}/{community_id}"),
            },
        });
    }

    Ok(PipelineSpec {
        id: pipeline_id_for_config(config.id),
        tenant: tenant.to_string(),
        community_id: community_id.to_string(),
        inputs: vec![InputSpec::Pull {
            url: config.source_url.clone(),
        }],
        profiles: plan.profiles,
        outputs,
    })
}

/// The acting member's hub-api user id: the validated JWT's `sub` (a `hub_users.id`). The
/// member's token itself is never forwarded to hub-api -- the debit is authenticated with this
/// service's own machine JWT and names the member as the actor. `None` for a non-numeric `sub`
/// (a service/legacy token), which cannot be admitted as a member.
fn actor_user_id(claims: &crate::http::auth::Claims) -> Option<i64> {
    claims.sub.parse::<i64>().ok().filter(|id| *id > 0)
}

/// Upper bound on tracked run sessions/epochs; each map is cleared when reached so it cannot grow
/// without bound (worst case: an in-flight retry after a clear is billed as a new admission).
const MAX_RUN_SESSIONS: usize = 10_000;

/// `(community_id, config_id, actor_user_id)`: one actor's run of one config.
type RunKey = (i32, i32, i64);

/// One admission in progress: the id that goes into the debit key plus the run epoch it was
/// allocated under. The epoch is what lets [`claim_run_start`] reject a stale admission.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Admission {
    /// `{epoch}-{uuid}`: epoch-prefixed so two runs can never share a debit key even if the
    /// session map is cleared.
    id: String,
    epoch: u64,
}

/// Process-wide run state, guarded by ONE lock so session lookup and epoch claim are atomic.
struct RunState {
    /// Maximum tracked sessions/epochs before eviction.
    capacity: usize,
    /// The admission svc-streaming allocated for the start in progress; reused by every retry.
    sessions: HashMap<RunKey, Admission>,
    /// Monotonic run generation per key, incremented on every successful start claim.
    epochs: HashMap<RunKey, u64>,
    /// Epoch of every key absent from `epochs`. Raised past every evicted epoch whenever the map
    /// is cleared at capacity, so an admission older than the eviction can never match again.
    epoch_floor: u64,
}

impl RunState {
    fn new(capacity: usize) -> Self {
        Self {
            capacity,
            sessions: HashMap::new(),
            epochs: HashMap::new(),
            epoch_floor: 0,
        }
    }

    /// At capacity, and `key` not tracked: drop every epoch, raising the floor past all of them.
    /// Evicting must never rewind a key to an epoch an old admission still holds: every
    /// pre-eviction admission becomes stale (re-admitted under a new key -- at worst one extra
    /// charge, never a free run).
    fn evict_if_full(&mut self, key: &RunKey) {
        if self.epochs.len() >= self.capacity && !self.epochs.contains_key(key) {
            let highest = self.epochs.values().copied().max().unwrap_or(0);
            self.epoch_floor = self.epoch_floor.max(highest) + 1;
            self.epochs.clear();
            self.sessions.clear();
        }
    }

    fn current_epoch(&self, key: &RunKey) -> u64 {
        self.epochs.get(key).copied().unwrap_or(self.epoch_floor)
    }

    /// See [`run_session_id`].
    fn admission(&mut self, key: RunKey) -> Admission {
        if self.sessions.len() >= self.capacity {
            self.sessions.clear();
        }
        // Evict BEFORE allocating, so this admission is taken under the raised floor and its
        // own claim does not immediately go stale (which would bill it twice).
        self.evict_if_full(&key);
        let epoch = self.current_epoch(&key);
        // A session allocated under an older epoch (the epoch moved or was evicted since) is
        // stale: replace it so the retry is admitted under the current run generation.
        if let Some(existing) = self.sessions.get(&key) {
            if existing.epoch == epoch {
                return existing.clone();
            }
        }
        let admission = Admission {
            id: format!("{epoch}-{}", uuid::Uuid::new_v4()),
            epoch,
        };
        self.sessions.insert(key, admission.clone());
        admission
    }

    /// See [`claim_run_start`].
    fn claim(&mut self, key: RunKey, admission: &Admission) -> bool {
        self.evict_if_full(&key);
        let current = self.current_epoch(&key);
        if current != admission.epoch {
            return false;
        }
        self.epochs.insert(key, current + 1);
        self.sessions.remove(&key);
        true
    }

    /// See [`release_run_start`].
    fn release(&mut self, key: RunKey, admission: &Admission) {
        if self.epochs.get(&key).copied() == Some(admission.epoch + 1) {
            self.epochs.insert(key, admission.epoch);
            self.sessions
                .entry(key)
                .or_insert_with(|| admission.clone());
        }
    }
}

static RUN_SESSIONS: LazyLock<Mutex<RunState>> =
    LazyLock::new(|| Mutex::new(RunState::new(MAX_RUN_SESSIONS)));

/// Stable debit idempotency key for one admission:
/// `stream:{community}:{config}:{actor}:{admission_id}`.
///
/// Deterministic in its inputs and deliberately timestamp-free, so a retry of the same admission
/// produces the same key and hub-api debits once. The admission id is ALWAYS server-derived
/// ([`run_session_id`]) -- never taken from the client, which could otherwise reuse one key on
/// every start for free replays. The actor is part of the key so one member's admission can never
/// satisfy another's (hub-api additionally rejects a key replayed by a different actor, and any
/// replay older than its short TTL).
pub fn admission_idempotency_key(
    community_id: i32,
    config_id: i32,
    actor_user_id: i64,
    admission_id: &str,
) -> String {
    format!("stream:{community_id}:{config_id}:{actor_user_id}:{admission_id}")
}

/// The admission svc-streaming allocates the first time an actor starts a config and then reuses
/// for every retry of that start until a start claims it ([`claim_run_start`]), or it is stopped
/// or refused ([`end_run_session`]).
///
/// This is what makes a header-less retry produce the SAME debit key: the id is allocated once per
/// run, not per request, so it is neither a timestamp nor re-rolled on retry. Its epoch is the
/// key's current run generation; a stop followed by a later start allocates a fresh admission --
/// a genuinely new run is billed again.
fn run_session_id(community_id: i32, config_id: i32, actor_user_id: i64) -> Admission {
    let mut state = RUN_SESSIONS.lock().unwrap_or_else(|e| e.into_inner());
    state.admission((community_id, config_id, actor_user_id))
}

/// Ends one actor's run session for a config -- when a stop arrives or hub-api refused to replay
/// the key (so the next attempt gets a new key instead of being stuck on a dead one). Never
/// touches the run epoch.
fn end_run_session(community_id: i32, config_id: i32, actor_user_id: i64) {
    RUN_SESSIONS
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .sessions
        .remove(&(community_id, config_id, actor_user_id));
}

/// Atomically admits ONE start of the run `admission` was allocated for: under the lock, succeeds
/// only if the key's epoch still equals the admission's, then bumps the epoch and retires the
/// session so the next run gets a new debit key. A concurrent or later start holding the same
/// (already-claimed) admission gets `false`: its debit would replay the first run's key and ride
/// free, so it must re-admit instead of starting.
fn claim_run_start(
    community_id: i32,
    config_id: i32,
    actor_user_id: i64,
    admission: &Admission,
) -> bool {
    RUN_SESSIONS
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .claim((community_id, config_id, actor_user_id), admission)
}

/// Undoes a [`claim_run_start`] whose pipeline then failed to start, so the retry of that same
/// start reuses the already-debited key (dedupes) instead of being charged again. No-op if
/// anything else claimed in between.
fn release_run_start(community_id: i32, config_id: i32, actor_user_id: i64, admission: &Admission) {
    RUN_SESSIONS
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .release((community_id, config_id, actor_user_id), admission);
}

/// TRANSCODE admission (BLOCK-WITH-FALLBACK, ported from the Python
/// alpha's `services/streaming_service.py::start_forwarding`): if
/// `config.transcode_enabled` is false, no ledger call is made and this
/// always resolves to `false` (matching Python's "the debit is only
/// attempted `if config_row.transcode_enabled`"). Otherwise attempts a real
/// token debit; an affordable debit resolves to `true` (the pipeline then
/// re-encodes with the config's selected codec), while an unaffordable one
/// (`insufficient_balance`) OR an unreachable ledger (`ledger_unavailable`)
/// resolves to `false` -- `start()` still succeeds and the stream still
/// starts, just as a passthrough. Never returns an `Err`: a
/// billing/entitlement check must never take down a live-stream start.
#[allow(
    clippy::too_many_arguments,
    reason = "flat inputs of one admission decision, all resolved by the single caller"
)]
async fn resolve_transcode_admission(
    token_ledger: &TokenLedgerClient,
    hub_api_url: &str,
    actor_user_id: Option<i64>,
    transcode_token_cost: i64,
    transcode_product_key: &str,
    community_id: i32,
    config: &streaming_config::Model,
) -> (bool, Option<Admission>) {
    if !config.transcode_enabled {
        return (false, None);
    }

    let Some(actor_user_id) = actor_user_id else {
        // The caller's `sub` is not a member user id, so there is no member to bill: admission
        // cannot be attempted. Loud, then the documented passthrough degrade.
        tracing::error!(
            community_id,
            config_id = config.id,
            "lifecycle: transcode admission requested but the caller's sub is not a numeric member id, falling back to passthrough"
        );
        return (false, None);
    };

    let admission = run_session_id(community_id, config.id, actor_user_id);
    let idempotency_key =
        admission_idempotency_key(community_id, config.id, actor_user_id, &admission.id);
    let reference = format!("stream:{}", config.id);
    let result = token_ledger
        .debit_transcoding_tokens(
            hub_api_url,
            actor_user_id,
            community_id,
            transcode_token_cost,
            transcode_product_key,
            &reference,
            &idempotency_key,
        )
        .await;

    if result.blocked_reason.as_deref() == Some(REASON_IDEMPOTENCY_CONFLICT) {
        end_run_session(community_id, config.id, actor_user_id);
    }

    if result.ok {
        tracing::info!(
            community_id,
            config_id = config.id,
            balance_after = result.balance_after,
            video_codec = %config.video_codec,
            audio_codec = %config.audio_codec,
            "lifecycle: transcode admission granted"
        );
        (true, Some(admission))
    } else {
        tracing::warn!(
            community_id,
            config_id = config.id,
            blocked_reason = result.blocked_reason.as_deref(),
            "lifecycle: transcode admission denied or unreachable, falling back to passthrough"
        );
        (false, Some(admission))
    }
}

/// `POST .../configs/{config_id}/start`.
#[utoipa::path(
    post,
    path = "/communities/{community_id}/streaming/configs/{config_id}/start",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    responses(
        (status = 200, description = "Pipeline status after start", body = PipelineStatusDto),
        (status = 404, description = "Config not found"),
        (status = 501, description = "Pipeline engine not yet implemented (S3)"),
    ),
    tag = "streaming"
)]
pub async fn start(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    State(state): State<AppState>,
    DbConn(db): DbConn,
    Extension(engine): Extension<SharedEngine>,
) -> Result<ApiSuccess<PipelineStatusDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let config = fetch_config(&db, community_id, config_id).await?;
    let actor = actor_user_id(&claims);
    // At most one re-admission: a stale admission means another start of this run claimed it
    // first, so the second pass sees that run (still up -> report it) or, if it was stopped in
    // between, admits and bills the genuinely new run under a fresh key.
    for _ in 0..2 {
        // A retry of a start that already succeeded (the client lost the response) must neither
        // be admitted nor billed again: report the pipeline that is already up.
        if let Ok(status) = engine.status(pipeline_id_for_config(config.id)).await {
            if matches!(
                status.state,
                PipelineState::Starting | PipelineState::Running | PipelineState::Degraded
            ) {
                return Ok(ApiSuccess::ok(status.into()));
            }
        }
        let (transcode_applied, admission) = resolve_transcode_admission(
            &state.token_ledger,
            &state.config.cli.hub_api_url,
            actor,
            state.config.cli.transcode_token_cost,
            &state.config.cli.transcode_product_key,
            community_id,
            &config,
        )
        .await;
        let spec = build_pipeline_spec(
            &db,
            &claims.tenant,
            community_id,
            &config,
            transcode_applied,
        )
        .await?;
        // Bind this admission to the run lifecycle: exactly one start may claim it, and the claim
        // retires its key, so a later run (after a stop) is a NEW admission that debits again.
        let claimed = match (actor, &admission) {
            (Some(actor), Some(admission)) => {
                if !claim_run_start(community_id, config.id, actor, admission) {
                    tracing::warn!(
                        community_id,
                        config_id = config.id,
                        "lifecycle: stale transcode admission (run already claimed), re-admitting"
                    );
                    continue;
                }
                Some((actor, admission))
            }
            _ => None,
        };
        let handle = match engine.start(spec).await {
            Ok(handle) => handle,
            Err(err) => {
                if let Some((actor, admission)) = claimed {
                    release_run_start(community_id, config.id, actor, admission);
                }
                return Err(map_pipeline_error(err));
            }
        };
        let status = engine.status(handle.id).await.map_err(map_pipeline_error)?;
        return Ok(ApiSuccess::ok(status.into()));
    }
    Err(ApiError::Internal(anyhow::anyhow!(
        "stream start lost its billing admission twice; retry the start"
    )))
}

/// `POST .../configs/{config_id}/stop` -- idempotent: stopping an
/// already-stopped or unknown pipeline is not an error (mirrors
/// [`crate::pipeline::PipelineEngine::stop`]'s own contract).
#[utoipa::path(
    post,
    path = "/communities/{community_id}/streaming/configs/{config_id}/stop",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    responses(
        (status = 200, description = "Pipeline status after stop", body = PipelineStatusDto),
        (status = 404, description = "Config not found"),
    ),
    tag = "streaming"
)]
pub async fn stop(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
    Extension(engine): Extension<SharedEngine>,
) -> Result<ApiSuccess<PipelineStatusDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let config = fetch_config(&db, community_id, config_id).await?;
    let id = pipeline_id_for_config(config.id);
    engine.stop(id).await.map_err(map_pipeline_error)?;
    // Retire only the stopping actor's session: other members' in-flight admissions are theirs.
    if let Some(actor) = actor_user_id(&claims) {
        end_run_session(community_id, config.id, actor);
    }
    Ok(ApiSuccess::ok(PipelineStatusDto {
        id,
        state: "stopped".to_string(),
        detail: None,
    }))
}

/// `GET .../configs/{config_id}/status`.
#[utoipa::path(
    get,
    path = "/communities/{community_id}/streaming/configs/{config_id}/status",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    responses(
        (status = 200, description = "Current pipeline status", body = PipelineStatusDto),
        (status = 404, description = "Config or pipeline not found"),
    ),
    tag = "streaming"
)]
pub async fn status(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
    Extension(engine): Extension<SharedEngine>,
) -> Result<ApiSuccess<PipelineStatusDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let config = fetch_config(&db, community_id, config_id).await?;
    let id = pipeline_id_for_config(config.id);
    let status = engine.status(id).await.map_err(map_pipeline_error)?;
    Ok(ApiSuccess::ok(status.into()))
}
