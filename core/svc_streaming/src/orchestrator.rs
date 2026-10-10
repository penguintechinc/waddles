//! S12 integration: wires ingest (RTMP/SRT/WHIP) to the pipeline engine
//! (`pipeline::FfmpegSupervisor`) to egress (HLS/relay/record), driven by
//! `streaming_configs` rows resolved from each ingest session's routing
//! key. This is the piece every prior chunk (S1-S11) built a contract for
//! but nothing previously called end to end -- see module docs across
//! `pipeline`, `ingest`, `egress` for the individual contracts this module
//! now exercises together.
//!
//! # Flow
//!
//! 1. `ingest::rtmp`/`ingest::srt` listeners and `ingest::whip`'s router
//!    (mounted by `http::router`) all push accepted sessions onto one
//!    shared [`crate::ingest::IngestSession`] channel (constructed in
//!    [`crate::run_with_shutdown`]).
//! 2. [`Orchestrator::run`] drains that channel, spawning
//!    [`Orchestrator::handle_session`] per session so one slow/stalled
//!    publisher never blocks the next.
//! 3. The session's routing key is matched against an enabled
//!    `streaming_configs.source_url` row (the same match
//!    `api::internal::ingest_auth` performs over HTTP -- done here directly
//!    against the DB since the orchestrator is not an HTTP client of
//!    itself) to resolve `community_id`/tenant/targets/record flag.
//! 4. A [`crate::pipeline::PipelineSpec`] is built: HLS is always an output
//!    (so `/live/{community_id}` and the HLS file-serving router have
//!    something to show); each enabled `streaming_targets` row becomes an
//!    `RtmpPush` or `SrtPush` output according to its `protocol`, encoded
//!    with the codec the config/target select
//!    ([`crate::spec_builder::plan_push_outputs`]); a `Record` output is
//!    added (on its own dedicated always-copy profile -- see
//!    [`build_pipeline_spec_for_ingest`]'s doc comment for why) when
//!    `record_enabled`.
//! 5. Egress sinks are registered/started *before* the ffmpeg process spawns
//!    ([`Orchestrator::start_egress_sinks`]) so their output directories
//!    exist and relay/record policy checks run before ffmpeg would start
//!    writing into them.
//! 6. The ffmpeg pipeline is started via
//!    [`crate::pipeline::FfmpegSupervisor::start`] (RTMP/SRT) or
//!    [`crate::pipeline::FfmpegSupervisor::start_with_whip_sdp`] (WHIP,
//!    which needs the `input.sdp` path
//!    [`crate::ingest::whip::WhipState::sdp_path_for`] provides).
//! 7. For RTMP/SRT, the ingest session's byte stream is pumped into
//!    ffmpeg's stdin until EOF (publisher disconnect), which triggers
//!    [`Orchestrator::stop_pipeline`] -- tearing every sink down and
//!    stopping the ffmpeg process. WHIP media reaches ffmpeg via
//!    `WhipTranscodeBridge`'s local UDP forward, not stdin, so there is no
//!    pump and no EOF-driven teardown for it (see the module-level "Not
//!    wired" note in the S12 completion report).

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex as StdMutex, RwLock as StdRwLock};
use std::time::Duration;

use anyhow::Context as _;
use chrono::{DateTime, Utc};
use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter};
use tokio::io::AsyncReadExt;
use tokio::sync::mpsc;

use crate::config::Config;
use crate::db::entities::{community, streaming_config, streaming_target, tenant};
use crate::egress::hls::{HlsSink, RunningPipeline, RunningPipelines};
use crate::egress::record::RecordSink;
use crate::egress::relay::RelaySink;
use crate::egress::OutputSink;
use crate::ingest::rtmp::{AuthDecision, IngestAuth};
use crate::ingest::srt::SrtAuth;
use crate::ingest::whip::WhipState;
use crate::ingest::{IngestKind, IngestSession};
use crate::pipeline::{
    AudioCodec, FfmpegSupervisor, HlsVariant, InputSpec, ObjectStoreRef, OutputSpec,
    PipelineEngine, PipelineId, PipelineSpec, TranscodeProfile, VideoCodec,
};
use crate::rtc::ingest_auth::{IngestAuthError, WhipTokenAuthorizer};
use crate::spec_builder::{plan_push_outputs, DEFAULT_PROFILE};
/// Dedicated profile for `Record` outputs -- always `Copy`/`Copy`
/// regardless of `streaming_configs.transcode_enabled`, deliberately never
/// sharing a name with `DEFAULT_PROFILE`. Two *transcoded* (non-`Copy`)
/// profiles with different names both entering `pipeline::ffmpeg::build_argv`'s
/// scale ladder would require a `resolution` on each (see
/// `build_filter_complex`'s doc comment) that this MVP never sets --
/// recording the raw ingest quality sidesteps that entirely and is also the
/// more defensible product default (archive the source, not the
/// stream-adjusted encode).
const RECORD_PROFILE: &str = "record";

// ---------------------------------------------------------------------
// Running-pipeline registry (backs the HLS listing endpoint)
// ---------------------------------------------------------------------

struct RegistryEntry {
    community_id: String,
    profile: String,
    started_at: DateTime<Utc>,
}

/// In-memory record of which pipelines the orchestrator currently considers
/// running, keyed by [`PipelineId`]. Implements
/// [`crate::egress::hls::RunningPipelines`] so `GET /live/{community_id}`
/// (`crate::egress::hls::hls_router`) can list them -- see
/// `crate::http::AppState::hls_router_state`, which
/// [`crate::run_with_shutdown`] points at the same registry instance the
/// orchestrator writes to.
#[derive(Default)]
pub struct PipelineRegistry {
    entries: StdMutex<HashMap<PipelineId, RegistryEntry>>,
}

impl PipelineRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    fn register(
        &self,
        id: PipelineId,
        community_id: impl Into<String>,
        profile: impl Into<String>,
    ) {
        let mut entries = lock(&self.entries);
        entries.insert(
            id,
            RegistryEntry {
                community_id: community_id.into(),
                profile: profile.into(),
                started_at: Utc::now(),
            },
        );
    }

    fn unregister(&self, id: PipelineId) {
        lock(&self.entries).remove(&id);
    }
}

fn lock<T>(mutex: &StdMutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

impl RunningPipelines for PipelineRegistry {
    fn list(&self, community_id: &str) -> Vec<RunningPipeline> {
        lock(&self.entries)
            .iter()
            .filter(|(_, entry)| entry.community_id == community_id)
            .map(|(id, entry)| RunningPipeline {
                id: *id,
                profile: entry.profile.clone(),
                started_at: entry.started_at,
            })
            .collect()
    }
}

// ---------------------------------------------------------------------
// DB-backed ingest authorization
// ---------------------------------------------------------------------

/// Resolves an enabled `streaming_configs` row whose `source_url` matches
/// `key` -- the same match `api::internal::ingest_auth` performs over HTTP,
/// used directly against the DB here (RTMP/WHIP auth and the orchestrator's
/// own pipeline-spec lookup both need it, and both already hold a
/// [`DatabaseConnection`] rather than being HTTP clients of this process).
/// A DB error is logged and treated as "not found" -- fail-closed, never a
/// panic on a transient DB hiccup.
async fn find_enabled_config(
    db: &DatabaseConnection,
    key: &str,
) -> Option<streaming_config::Model> {
    match streaming_config::Entity::find()
        .filter(streaming_config::Column::SourceUrl.eq(key))
        .filter(streaming_config::Column::Enabled.eq(true))
        .one(db)
        .await
    {
        Ok(found) => found,
        Err(err) => {
            tracing::warn!(error = %err, "orchestrator: db error while resolving an ingest key, treating as unauthorized");
            None
        }
    }
}

/// [`IngestAuth`] (RTMP) and [`WhipTokenAuthorizer`] (WHIP) implementation
/// backed directly by the DB -- both traits are `async`, so both can query
/// per-attempt without a cache. [`crate::ingest::srt::SrtAuth`] cannot
/// (its trait method is synchronous); see [`RefreshingSrtAuth`] for SRT's
/// workaround.
///
/// Holds [`Config`], not an already-established [`DatabaseConnection`], and
/// resolves one lazily (via [`crate::db::get_or_connect`]) on every
/// `authorize` call -- constructing this must never itself require a live
/// DB connection (`crate::run_with_shutdown` builds one at startup
/// regardless of whether anything has published yet, and the lazy
/// singleton `crate::db::get_or_connect` establishes is shared with every
/// other DB access in the process, so this is not a per-call reconnect in
/// practice).
pub struct DbIngestAuth {
    config: Config,
}

impl DbIngestAuth {
    pub fn new(config: Config) -> Self {
        Self { config }
    }

    async fn db(&self) -> anyhow::Result<DatabaseConnection> {
        crate::db::get_or_connect(&self.config)
            .await
            .context("connecting to the database for ingest authorization")
    }
}

impl IngestAuth for DbIngestAuth {
    fn authorize<'a>(
        &'a self,
        _kind: IngestKind,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = anyhow::Result<AuthDecision>> + Send + 'a>> {
        Box::pin(async move {
            let db = self.db().await?;
            match find_enabled_config(&db, key).await {
                Some(cfg) => Ok(AuthDecision {
                    community_id: cfg.community_id.to_string(),
                    config_id: cfg.id.to_string(),
                }),
                None => Err(anyhow::anyhow!(
                    "stream key does not match any enabled streaming_configs.source_url"
                )),
            }
        })
    }
}

#[async_trait::async_trait]
impl WhipTokenAuthorizer for DbIngestAuth {
    async fn authorize(&self, token: &str) -> Result<bool, IngestAuthError> {
        let db = self
            .db()
            .await
            .map_err(|err| IngestAuthError::Request(err.to_string()))?;
        Ok(find_enabled_config(&db, token).await.is_some())
    }
}

/// [`SrtAuth`] implementation for a trait whose `authorize` method is
/// synchronous (no `.await` available at the call site) -- a background
/// task refreshed every `interval` re-queries every enabled
/// `streaming_configs.source_url` and atomically swaps the allowed-key
/// set; `authorize` itself only ever takes a read lock. A key added to the
/// DB between refreshes is rejected until the next tick -- documented
/// staleness window, not a bug; default `interval` (5s, see
/// [`crate::run_with_shutdown`]) keeps it short.
pub struct RefreshingSrtAuth {
    allowed: Arc<StdRwLock<HashSet<String>>>,
}

impl RefreshingSrtAuth {
    pub fn new() -> Self {
        Self {
            allowed: Arc::new(StdRwLock::new(HashSet::new())),
        }
    }

    /// Spawns the background refresh loop. Returns the join handle for the
    /// caller to hold (or drop -- the task runs detached either way); a DB
    /// failure on a given tick is logged and the previous allowlist is kept
    /// rather than clearing it (never fail-open to "reject everything"
    /// just because one refresh hiccupped, and never fail-open to "allow
    /// everything" either).
    pub fn spawn_refresh_loop(
        &self,
        config: Config,
        interval: Duration,
    ) -> tokio::task::JoinHandle<()> {
        let allowed = self.allowed.clone();
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(interval);
            loop {
                ticker.tick().await;
                let db = match crate::db::get_or_connect(&config).await {
                    Ok(db) => db,
                    Err(err) => {
                        tracing::warn!(error = %err, "srt auth cache refresh: db connection failed, keeping the previous allowlist");
                        continue;
                    }
                };
                match streaming_config::Entity::find()
                    .filter(streaming_config::Column::Enabled.eq(true))
                    .all(&db)
                    .await
                {
                    Ok(rows) => {
                        let keys: HashSet<String> =
                            rows.into_iter().map(|r| r.source_url).collect();
                        let count = keys.len();
                        *allowed
                            .write()
                            .unwrap_or_else(std::sync::PoisonError::into_inner) = keys;
                        tracing::debug!(count, "srt auth cache refreshed");
                    }
                    Err(err) => {
                        tracing::warn!(error = %err, "srt auth cache refresh: query failed, keeping the previous allowlist");
                    }
                }
            }
        })
    }
}

impl Default for RefreshingSrtAuth {
    fn default() -> Self {
        Self::new()
    }
}

impl SrtAuth for RefreshingSrtAuth {
    fn authorize(&self, key: &str) -> Result<(), &'static str> {
        let allowed = self
            .allowed
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if allowed.contains(key) {
            Ok(())
        } else {
            Err("unknown, disabled, or not-yet-cached stream key")
        }
    }
}

// ---------------------------------------------------------------------
// Pipeline-spec construction
// ---------------------------------------------------------------------

/// Resolves `community_id` to its owning tenant's slug -- the same shared
/// `tenants`/`communities` tables `api::tenancy::assert_tenant_owns_community`
/// reads (see `db::entities::mod` for the cross-service DB-grant
/// assumption).
async fn resolve_tenant_slug(db: &DatabaseConnection, community_id: i32) -> anyhow::Result<String> {
    let community_row = community::Entity::find_by_id(community_id)
        .one(db)
        .await
        .context("querying communities")?
        .ok_or_else(|| anyhow::anyhow!("community {community_id} not found"))?;
    let tenant_row = tenant::Entity::find_by_id(community_row.tenant_id)
        .one(db)
        .await
        .context("querying tenants")?
        .ok_or_else(|| {
            anyhow::anyhow!(
                "tenant {} not found for community {community_id}",
                community_row.tenant_id
            )
        })?;
    Ok(tenant_row.slug)
}

/// Builds the [`PipelineSpec`] for an ingest-triggered pipeline (RTMP/SRT/
/// WHIP publish) -- distinct from `api::lifecycle::build_pipeline_spec`,
/// which builds a control-plane-triggered (`Pull` input, admin `/start`)
/// spec from the same `streaming_configs` row and never adds an HLS output.
/// This one always adds HLS (so the community's live-streams surface and
/// `/live/{community_id}` have something to show the moment a publisher
/// connects) and gives `Record` its own always-`Copy` profile -- see
/// [`RECORD_PROFILE`]'s doc comment for why.
///
/// The default profile (HLS, and every push without an override) carries
/// the config's selected codec; pushes that need a different codec -- an
/// RTMP push on an H.265 config, which must stay H.264 -- get their own
/// profile and are encoded off a shared decode. The codec rules (and the
/// loud rejection of combinations ffmpeg cannot mux) live in
/// [`plan_push_outputs`].
///
/// **No transcode-token admission here, deliberately**: unlike
/// `api::lifecycle::start` (which holds the caller's bearer JWT from the
/// `/start` HTTP request), an ingest-triggered publish has no end-user JWT
/// to pass through to hub-api's token ledger -- a stream key authenticates
/// the publisher directly, not an authenticated community member spending
/// their own tokens. `config.transcode_enabled` is honored here unmetered
/// until a service-identity credential (SPIFFE mTLS / machine JWT) exists
/// for this unattended path -- see `crate::billing::token_ledger`'s module
/// doc comment, which calls this out as explicit follow-up work, not an
/// oversight.
async fn build_pipeline_spec_for_ingest(
    db: &DatabaseConnection,
    config: &streaming_config::Model,
    tenant_slug: &str,
    input: InputSpec,
) -> anyhow::Result<PipelineSpec> {
    let targets = streaming_target::Entity::find()
        .filter(streaming_target::Column::ConfigId.eq(config.id))
        .filter(streaming_target::Column::Enabled.eq(true))
        .all(db)
        .await
        .context("querying streaming_targets")?;

    // Not wrapped in `.context(..)`: the plan error already names the row
    // and the reason, and must reach the log intact.
    let plan = plan_push_outputs(config, &targets, config.transcode_enabled)?;

    let mut outputs = Vec::with_capacity(plan.outputs.len() + 2);
    outputs.push(OutputSpec::Hls {
        variant: HlsVariant::Std,
        profile: DEFAULT_PROFILE.into(),
    });
    outputs.extend(plan.outputs);
    let mut profiles = plan.profiles;

    if config.record_enabled {
        profiles.push(TranscodeProfile {
            name: RECORD_PROFILE.into(),
            video: VideoCodec::Copy,
            audio: AudioCodec::Copy,
            resolution: None,
            fps: None,
        });
        outputs.push(OutputSpec::Record {
            profile: RECORD_PROFILE.into(),
            target: ObjectStoreRef {
                store: "default".into(),
                prefix: format!("{tenant_slug}/{}", config.community_id),
            },
        });
    }

    Ok(PipelineSpec {
        id: crate::api::pipeline_id_for_config(config.id),
        tenant: tenant_slug.to_string(),
        community_id: config.community_id.to_string(),
        inputs: vec![input],
        profiles,
        outputs,
    })
}

// ---------------------------------------------------------------------
// Orchestrator
// ---------------------------------------------------------------------

/// Owns every piece [`Orchestrator::handle_session`] needs to take an
/// [`IngestSession`] all the way to a running, egress-wired ffmpeg
/// pipeline. Constructed once in [`crate::run_with_shutdown`] and shared
/// (`Arc<Orchestrator>`) across every spawned session handler.
pub struct Orchestrator {
    config: Config,
    supervisor: Arc<FfmpegSupervisor>,
    hls: Arc<HlsSink>,
    relay: Arc<RelaySink>,
    record: Option<Arc<RecordSink>>,
    registry: Arc<PipelineRegistry>,
    whip_state: Arc<WhipState>,
}

impl Orchestrator {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        config: Config,
        supervisor: Arc<FfmpegSupervisor>,
        hls: Arc<HlsSink>,
        relay: Arc<RelaySink>,
        record: Option<Arc<RecordSink>>,
        registry: Arc<PipelineRegistry>,
        whip_state: Arc<WhipState>,
    ) -> Self {
        Self {
            config,
            supervisor,
            hls,
            relay,
            record,
            registry,
            whip_state,
        }
    }

    /// Drains `rx` until the channel closes (every ingest listener/router
    /// holding a sender has been dropped), spawning
    /// [`Self::handle_session`] per accepted session so a stalled publisher
    /// never blocks the next one from starting.
    pub async fn run(self: Arc<Self>, mut rx: mpsc::Receiver<IngestSession>) {
        tracing::info!("orchestrator: ingest dispatch loop started");
        while let Some(session) = rx.recv().await {
            let this = Arc::clone(&self);
            tokio::spawn(async move {
                this.handle_session(session).await;
            });
        }
        tracing::info!("orchestrator: ingest channel closed, dispatch loop exiting");
    }

    async fn handle_session(self: Arc<Self>, session: IngestSession) {
        let kind = session.kind;
        let db = match crate::db::get_or_connect(&self.config).await {
            Ok(db) => db,
            Err(err) => {
                tracing::error!(error = %err, ?kind, "orchestrator: no database connection, dropping ingest session");
                return;
            }
        };

        let config = match find_enabled_config(&db, &session.key).await {
            Some(config) => config,
            None => {
                // Should be rare: the listener/router already ran the same
                // check via `DbIngestAuth`/`RefreshingSrtAuth` before
                // accepting -- a config disabled in the gap between accept
                // and here is the main legitimate cause.
                tracing::warn!(?kind, "orchestrator: ingest session key no longer matches an enabled streaming_config, dropping");
                return;
            }
        };

        let tenant_slug = match resolve_tenant_slug(&db, config.community_id).await {
            Ok(slug) => slug,
            Err(err) => {
                tracing::error!(error = %err, community_id = config.community_id, "orchestrator: failed to resolve tenant, dropping ingest session");
                return;
            }
        };

        let input = match kind {
            IngestKind::Rtmp => InputSpec::Rtmp {
                stream_key: session.key.clone(),
            },
            IngestKind::Srt => InputSpec::Srt {
                stream_id: session.key.clone(),
            },
            IngestKind::Whip => InputSpec::Whip {
                token: session.key.clone(),
            },
        };

        let spec = match build_pipeline_spec_for_ingest(&db, &config, &tenant_slug, input).await {
            Ok(spec) => spec,
            Err(err) => {
                tracing::error!(error = %err, config_id = config.id, "orchestrator: failed to build pipeline spec, dropping ingest session");
                return;
            }
        };
        let pipeline_id = spec.id;
        let community_id = config.community_id;

        self.start_egress_sinks(pipeline_id, community_id, &spec)
            .await;

        let start_result = match kind {
            IngestKind::Whip => match self.whip_state.sdp_path_for(&session.key).await {
                Some(sdp_path) => {
                    let mut whip_sdp_paths = HashMap::new();
                    whip_sdp_paths.insert(0usize, sdp_path);
                    self.supervisor
                        .start_with_whip_sdp(spec, whip_sdp_paths)
                        .await
                }
                None => {
                    tracing::error!(%pipeline_id, "orchestrator: no WHIP transcode-bridge SDP path registered for this token, cannot start ffmpeg");
                    self.stop_pipeline(pipeline_id).await;
                    return;
                }
            },
            IngestKind::Rtmp | IngestKind::Srt => self.supervisor.start(spec).await,
        };

        if let Err(err) = start_result {
            tracing::error!(%pipeline_id, error = %err, "orchestrator: ffmpeg supervisor failed to start the pipeline");
            self.stop_pipeline(pipeline_id).await;
            return;
        }

        self.registry
            .register(pipeline_id, community_id.to_string(), DEFAULT_PROFILE);
        tracing::info!(%pipeline_id, community_id, ?kind, "orchestrator: pipeline started");

        if matches!(kind, IngestKind::Rtmp | IngestKind::Srt) {
            match self.supervisor.stdin_writer(pipeline_id).await {
                Ok(stdin) => {
                    pump_ingest_to_stdin(session.stream, stdin, pipeline_id).await;
                    tracing::info!(%pipeline_id, "orchestrator: ingest stream ended, tearing pipeline down");
                    self.stop_pipeline(pipeline_id).await;
                }
                Err(err) => {
                    tracing::error!(%pipeline_id, error = %err, "orchestrator: could not obtain the ffmpeg stdin handle -- pipeline is running but will never receive ingest bytes");
                }
            }
        }
        // WHIP media reaches ffmpeg via `WhipTranscodeBridge`'s local UDP
        // forward, not this process's stdin -- no pump, no EOF-driven
        // teardown for it (see the module doc's "Not wired" note).
    }

    /// Registers/starts every egress sink `spec.outputs` needs, before the
    /// ffmpeg process itself spawns -- HLS's target directory must exist
    /// before ffmpeg's `-f hls` muxer can write into it, and relay/record
    /// policy checks should run before committing to a spawn. A sink
    /// failing to start is logged, not fatal to the whole pipeline: ffmpeg
    /// still forwards `RtmpPush`/`SrtPush`/`Hls`/`Record` targets directly
    /// via its own argv (`pipeline::ffmpeg::build_argv`) regardless of
    /// whether the supervisory sink layer (health tracking, upload,
    /// metrics) is also running -- only HLS is load-bearing for ffmpeg
    /// actually having somewhere to write, so a failure there aborts
    /// before it further.
    async fn start_egress_sinks(
        &self,
        pipeline_id: PipelineId,
        community_id: i32,
        spec: &PipelineSpec,
    ) {
        self.hls
            .register_pipeline(pipeline_id, community_id.to_string());
        for output in &spec.outputs {
            match output {
                OutputSpec::Hls { .. } => {
                    if let Err(err) = self.hls.start(pipeline_id, output.clone()).await {
                        tracing::error!(%pipeline_id, error = %err, "orchestrator: HLS sink failed to start");
                    }
                }
                OutputSpec::RtmpPush { .. } | OutputSpec::SrtPush { .. } => {
                    if let Err(err) = self.relay.start(pipeline_id, output.clone()).await {
                        tracing::warn!(%pipeline_id, error = %err, "orchestrator: relay sink failed to start for a target -- ffmpeg still forwards it directly via argv, only health tracking/policy enforcement is degraded for this target");
                    }
                }
                OutputSpec::Record { .. } => match &self.record {
                    Some(record) => {
                        if let Err(err) = record.start(pipeline_id, output.clone()).await {
                            tracing::warn!(%pipeline_id, error = %err, "orchestrator: record sink failed to start -- recording will not be uploaded to object storage");
                        }
                    }
                    None => {
                        tracing::warn!(%pipeline_id, "orchestrator: recording requested but no RecordSink is configured (S3_ENDPOINT/RECORDINGS_BUCKET/S3_ACCESS_KEY/S3_SECRET_KEY)");
                    }
                },
                OutputSpec::Whep { .. } | OutputSpec::DiscordVoice { .. } => {}
            }
        }
    }

    /// Tears a pipeline down: stops the ffmpeg process/engine registration,
    /// every egress sink, and removes it from the running-pipeline
    /// registry. Idempotent (every sink's own `stop` is) -- safe to call
    /// more than once for the same `pipeline_id`, and safe to call for a
    /// pipeline that only partially started.
    pub async fn stop_pipeline(&self, pipeline_id: PipelineId) {
        if let Err(err) = PipelineEngine::stop(self.supervisor.as_ref(), pipeline_id).await {
            tracing::warn!(%pipeline_id, error = %err, "orchestrator: engine stop failed");
        }
        if let Err(err) = self.hls.stop(pipeline_id).await {
            tracing::warn!(%pipeline_id, error = %err, "orchestrator: HLS sink stop failed");
        }
        if let Err(err) = self.relay.stop(pipeline_id).await {
            tracing::warn!(%pipeline_id, error = %err, "orchestrator: relay sink stop failed");
        }
        if let Some(record) = &self.record {
            if let Err(err) = record.stop(pipeline_id).await {
                tracing::warn!(%pipeline_id, error = %err, "orchestrator: record sink stop failed");
            }
        }
        self.registry.unregister(pipeline_id);
        tracing::info!(%pipeline_id, "orchestrator: pipeline stopped and torn down");
    }

    /// The running-pipeline registry backing `/live/{community_id}` --
    /// [`crate::run_with_shutdown`] hands a clone of this same `Arc` to
    /// [`crate::http::AppState::hls_router_state`] so both sides observe
    /// the same set of pipelines.
    pub fn registry(&self) -> Arc<PipelineRegistry> {
        Arc::clone(&self.registry)
    }
}

/// Reads `stream` (an ingest listener's demuxed byte stream -- FLV for
/// RTMP, MPEG-TS for SRT) until EOF or a read error, forwarding every chunk
/// into `stdin` via `spawn_blocking` (per [`crate::pipeline::StdinHandle`]'s
/// own doc comment: its `write` is a blocking `std::process::ChildStdin`
/// call). A single failed write is logged and skipped rather than aborting
/// the pump -- ffmpeg's stdin can be transiently unavailable while the
/// supervisor's restart-on-crash loop (`pipeline::supervisor`) is between
/// attempts, and the *shared* [`crate::pipeline::StdinHandle`] slot picks
/// up the next attempt's stdin automatically once it reattaches, so a pump
/// that quit on the first transient failure would needlessly orphan an
/// otherwise-recovering pipeline. Returns once the source stream reaches
/// EOF (publisher disconnected) or errors -- the caller tears the pipeline
/// down on return.
async fn pump_ingest_to_stdin(
    mut stream: Box<dyn tokio::io::AsyncRead + Send + Unpin>,
    stdin: crate::pipeline::StdinHandle,
    pipeline_id: PipelineId,
) {
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = match stream.read(&mut buf).await {
            Ok(0) => {
                tracing::debug!(%pipeline_id, "pump_ingest_to_stdin: source stream reached EOF");
                return;
            }
            Ok(n) => n,
            Err(err) => {
                tracing::warn!(%pipeline_id, error = %err, "pump_ingest_to_stdin: source stream read error, stopping pump");
                return;
            }
        };
        let chunk = buf[..n].to_vec();
        let stdin = stdin.clone();
        match tokio::task::spawn_blocking(move || stdin.write(&chunk)).await {
            Ok(Ok(())) => {}
            Ok(Err(err)) => {
                tracing::debug!(%pipeline_id, error = %err, "pump_ingest_to_stdin: ffmpeg stdin write failed (possibly mid-restart), continuing");
            }
            Err(join_err) => {
                tracing::warn!(%pipeline_id, error = %join_err, "pump_ingest_to_stdin: blocking write task panicked, stopping pump");
                return;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::SecretRef;
    use sea_orm::ConnectionTrait;
    use uuid::Uuid;

    #[test]
    fn pipeline_registry_lists_only_the_requested_community() {
        let registry = PipelineRegistry::new();
        let id_a = Uuid::new_v4();
        let id_b = Uuid::new_v4();
        registry.register(id_a, "community-1", DEFAULT_PROFILE);
        registry.register(id_b, "community-2", DEFAULT_PROFILE);

        let listed = registry.list("community-1");
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].id, id_a);

        registry.unregister(id_a);
        assert!(registry.list("community-1").is_empty());
    }

    #[test]
    fn refreshing_srt_auth_starts_empty_and_rejects_everything() {
        let auth = RefreshingSrtAuth::new();
        assert!(auth.authorize("any-key").is_err());
    }

    async fn seed_db() -> DatabaseConnection {
        let db = sea_orm::Database::connect("sqlite::memory:")
            .await
            .expect("connect in-memory sqlite");
        db.execute_unprepared(
            r#"
            CREATE TABLE tenants (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE);
            CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL);
            CREATE TABLE streaming_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                community_id INTEGER NOT NULL UNIQUE,
                source_url TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'rtmp',
                enabled INTEGER NOT NULL DEFAULT 1,
                record_enabled INTEGER NOT NULL DEFAULT 0,
                transcode_enabled INTEGER NOT NULL DEFAULT 0,
                transcode_bitrate_kbps INTEGER NOT NULL DEFAULT 4000,
                video_codec TEXT NOT NULL DEFAULT 'h264',
                audio_codec TEXT NOT NULL DEFAULT 'copy'
            );
            CREATE TABLE streaming_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                forward_url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                protocol TEXT NOT NULL DEFAULT 'rtmp',
                video_codec TEXT,
                audio_codec TEXT
            );
            INSERT INTO tenants (id, slug) VALUES (1, 'tenant-1');
            INSERT INTO communities (id, tenant_id) VALUES (42, 1);
            INSERT INTO communities (id, tenant_id) VALUES (99, 404);
            INSERT INTO streaming_configs (id, community_id, source_url, enabled, record_enabled, transcode_enabled, transcode_bitrate_kbps)
                VALUES (1, 42, 'sk_enabled', 1, 0, 0, 4000);
            INSERT INTO streaming_configs (id, community_id, source_url, enabled)
                VALUES (2, 43, 'sk_disabled', 0);
            "#,
        )
        .await
        .expect("seed schema");
        db
    }

    // --- find_enabled_config ---

    #[tokio::test]
    async fn find_enabled_config_matches_an_enabled_source_url() {
        let db = seed_db().await;
        let cfg = find_enabled_config(&db, "sk_enabled").await.expect("found");
        assert_eq!(cfg.id, 1);
        assert_eq!(cfg.community_id, 42);
    }

    #[tokio::test]
    async fn find_enabled_config_rejects_a_disabled_row() {
        let db = seed_db().await;
        assert!(find_enabled_config(&db, "sk_disabled").await.is_none());
    }

    #[tokio::test]
    async fn find_enabled_config_rejects_an_unknown_key() {
        let db = seed_db().await;
        assert!(find_enabled_config(&db, "sk_does_not_exist")
            .await
            .is_none());
    }

    // --- resolve_tenant_slug ---

    #[tokio::test]
    async fn resolve_tenant_slug_returns_the_owning_tenants_slug() {
        let db = seed_db().await;
        let slug = resolve_tenant_slug(&db, 42).await.expect("resolves");
        assert_eq!(slug, "tenant-1");
    }

    #[tokio::test]
    async fn resolve_tenant_slug_errors_for_an_unknown_community() {
        let db = seed_db().await;
        let err = resolve_tenant_slug(&db, 9999).await.unwrap_err();
        assert!(err.to_string().contains("community"));
    }

    #[tokio::test]
    async fn resolve_tenant_slug_errors_when_the_tenant_row_is_missing() {
        let db = seed_db().await;
        // Community 99 references tenant_id 404, which has no matching row.
        let err = resolve_tenant_slug(&db, 99).await.unwrap_err();
        assert!(err.to_string().contains("tenant"));
    }

    // --- build_pipeline_spec_for_ingest ---

    fn sample_config(
        id: i32,
        community_id: i32,
        record_enabled: bool,
        transcode_enabled: bool,
    ) -> streaming_config::Model {
        streaming_config::Model {
            id,
            community_id,
            source_url: "sk_enabled".into(),
            source_type: "rtmp".into(),
            enabled: true,
            record_enabled,
            transcode_enabled,
            transcode_bitrate_kbps: 4000,
            video_codec: "h264".into(),
            audio_codec: "copy".into(),
        }
    }

    #[tokio::test]
    async fn build_spec_always_includes_hls_with_no_targets_or_record() {
        let db = seed_db().await;
        let config = sample_config(1, 42, false, false);
        let spec = build_pipeline_spec_for_ingest(
            &db,
            &config,
            "tenant-1",
            InputSpec::Rtmp {
                stream_key: "sk_enabled".into(),
            },
        )
        .await
        .expect("builds");

        assert_eq!(spec.id, crate::api::pipeline_id_for_config(1));
        assert_eq!(spec.tenant, "tenant-1");
        assert_eq!(spec.community_id, "42");
        assert_eq!(spec.profiles.len(), 1);
        assert_eq!(spec.profiles[0].name, DEFAULT_PROFILE);
        assert!(matches!(spec.profiles[0].video, VideoCodec::Copy));
        assert_eq!(spec.outputs.len(), 1);
        assert!(matches!(spec.outputs[0], OutputSpec::Hls { .. }));
    }

    #[tokio::test]
    async fn build_spec_transcode_enabled_uses_h264_on_the_default_profile() {
        let db = seed_db().await;
        let config = sample_config(1, 42, false, true);
        let spec = build_pipeline_spec_for_ingest(
            &db,
            &config,
            "tenant-1",
            InputSpec::Rtmp {
                stream_key: "sk_enabled".into(),
            },
        )
        .await
        .expect("builds");
        assert!(matches!(
            spec.profiles[0].video,
            VideoCodec::H264 {
                bitrate_kbps: Some(4000),
                ..
            }
        ));
    }

    #[tokio::test]
    async fn build_spec_adds_a_record_output_on_its_own_always_copy_profile() {
        let db = seed_db().await;
        // transcode_enabled = true, so the default profile is H264 -- the
        // record profile must stay Copy regardless (see RECORD_PROFILE's
        // doc comment: never share a name with a differently-encoded
        // profile, and never itself require a `resolution`).
        let config = sample_config(1, 42, true, true);
        let spec = build_pipeline_spec_for_ingest(
            &db,
            &config,
            "tenant-1",
            InputSpec::Rtmp {
                stream_key: "sk_enabled".into(),
            },
        )
        .await
        .expect("builds");

        assert_eq!(spec.profiles.len(), 2);
        let record_profile = spec
            .profiles
            .iter()
            .find(|p| p.name == RECORD_PROFILE)
            .expect("record profile present");
        assert!(matches!(record_profile.video, VideoCodec::Copy));

        let record_output = spec
            .outputs
            .iter()
            .find(|o| matches!(o, OutputSpec::Record { .. }))
            .expect("record output present");
        if let OutputSpec::Record { profile, target } = record_output {
            assert_eq!(profile, RECORD_PROFILE);
            assert_eq!(target.prefix, "tenant-1/42");
        }

        // `build_argv` must actually accept this spec without erroring
        // (regression guard for the filter_complex/ladder bug a
        // same-name-different-codec record profile would have caused --
        // see RECORD_PROFILE's doc comment).
        let paths = crate::pipeline::ffmpeg::Paths {
            stream_data_dir: std::path::PathBuf::from("/tmp"),
            ..Default::default()
        };
        crate::pipeline::build_argv(&spec, &paths).expect("record + transcoded default must build");
    }

    #[tokio::test]
    async fn build_spec_adds_an_rtmp_push_output_per_enabled_target() {
        let db = seed_db().await;
        db.execute_unprepared(&format!(
            r#"
            INSERT INTO streaming_targets (config_id, platform, forward_url, enabled)
                VALUES (1, 'twitch', '{}', 1);
            INSERT INTO streaming_targets (config_id, platform, forward_url, enabled)
                VALUES (1, 'youtube', '{}', 0);
            "#,
            serde_json::to_string(&SecretRef::Env {
                var: "RELAY_URL".into()
            })
            .unwrap()
            .replace('\'', "''"),
            serde_json::to_string(&SecretRef::Env {
                var: "DISABLED_URL".into()
            })
            .unwrap()
            .replace('\'', "''"),
        ))
        .await
        .expect("seed targets");

        let config = sample_config(1, 42, false, false);
        let spec = build_pipeline_spec_for_ingest(
            &db,
            &config,
            "tenant-1",
            InputSpec::Rtmp {
                stream_key: "sk_enabled".into(),
            },
        )
        .await
        .expect("builds");

        let rtmp_push_count = spec
            .outputs
            .iter()
            .filter(|o| matches!(o, OutputSpec::RtmpPush { .. }))
            .count();
        assert_eq!(rtmp_push_count, 1, "the disabled target must be excluded");
    }

    #[tokio::test]
    async fn build_spec_errors_on_a_non_secret_ref_forward_url() {
        let db = seed_db().await;
        db.execute_unprepared(
            "INSERT INTO streaming_targets (config_id, platform, forward_url, enabled) VALUES (1, 'twitch', 'not-json', 1);",
        )
        .await
        .expect("seed a malformed target");

        let config = sample_config(1, 42, false, false);
        let err = build_pipeline_spec_for_ingest(
            &db,
            &config,
            "tenant-1",
            InputSpec::Rtmp {
                stream_key: "sk_enabled".into(),
            },
        )
        .await
        .unwrap_err();
        assert!(err.to_string().contains("forward_url"));
    }

    // --- Orchestrator sink lifecycle (no DB/ffmpeg process involved) ---

    fn test_orchestrator(stream_data_dir: std::path::PathBuf) -> Orchestrator {
        let cli = clap::Parser::parse_from(["svc-streaming"]);
        let config = crate::config::Config {
            cli,
            db_password: crate::config::Secret::new("x"),
            cache_password: None,
            service_api_key: crate::config::Secret::new("x"),
            jwt_hmac_secret: None,
        };
        let registry = prometheus::Registry::new();
        let supervisor = Arc::new(FfmpegSupervisor::new(
            std::path::PathBuf::from("/nonexistent/ffmpeg-for-orchestrator-unit-tests"),
            stream_data_dir.clone(),
            41000,
            Arc::new(crate::store::DefaultSecretResolver),
            crate::pipeline::SupervisorConfig::default(),
        ));
        let hls = Arc::new(HlsSink::new(stream_data_dir, &registry));
        let relay = Arc::new(RelaySink::new());
        let pipeline_registry = Arc::new(PipelineRegistry::new());
        let rtc_config = crate::rtc::RtcConfig::from_config(&config).unwrap();
        let factory = Arc::new(crate::rtc::PeerConnectionFactory::new(rtc_config).unwrap());
        let rtc_metrics = crate::rtc::RtcMetrics::register(&prometheus::Registry::new()).unwrap();
        let authorizer =
            Arc::new(crate::rtc::ingest_auth::InternalIngestAuthClient::new(&config).unwrap());
        let (tx, _rx) = mpsc::channel(1);
        let whip_state = Arc::new(WhipState::new(
            factory,
            authorizer,
            tx,
            config.cli.stream_data_dir.clone(),
            config.cli.bind_addr,
            rtc_metrics,
        ));
        Orchestrator::new(
            config,
            supervisor,
            hls,
            relay,
            None,
            pipeline_registry,
            whip_state,
        )
    }

    #[tokio::test]
    async fn start_egress_sinks_creates_the_hls_output_directory() {
        let data_dir =
            std::env::temp_dir().join(format!("svc-streaming-orch-unit-{}", Uuid::new_v4()));
        let orchestrator = test_orchestrator(data_dir.clone());
        let pipeline_id = Uuid::new_v4();
        let spec = PipelineSpec {
            id: pipeline_id,
            tenant: "tenant-1".into(),
            community_id: "42".into(),
            inputs: vec![InputSpec::Rtmp {
                stream_key: "sk1".into(),
            }],
            profiles: vec![TranscodeProfile {
                name: DEFAULT_PROFILE.into(),
                video: VideoCodec::Copy,
                audio: AudioCodec::Copy,
                resolution: None,
                fps: None,
            }],
            outputs: vec![OutputSpec::Hls {
                variant: crate::pipeline::HlsVariant::Std,
                profile: DEFAULT_PROFILE.into(),
            }],
        };

        orchestrator
            .start_egress_sinks(pipeline_id, 42, &spec)
            .await;

        let expected_dir = data_dir
            .join("hls")
            .join(pipeline_id.to_string())
            .join(DEFAULT_PROFILE);
        assert!(tokio::fs::metadata(&expected_dir).await.unwrap().is_dir());

        orchestrator.stop_pipeline(pipeline_id).await;
        tokio::fs::remove_dir_all(&data_dir).await.ok();
    }

    #[tokio::test]
    async fn stop_pipeline_on_a_never_started_id_does_not_panic() {
        let data_dir =
            std::env::temp_dir().join(format!("svc-streaming-orch-unit-{}", Uuid::new_v4()));
        let orchestrator = test_orchestrator(data_dir.clone());
        orchestrator.stop_pipeline(Uuid::new_v4()).await;
        tokio::fs::remove_dir_all(&data_dir).await.ok();
    }

    #[tokio::test]
    async fn registry_accessor_returns_the_same_registry_started_pipelines_are_recorded_in() {
        let data_dir =
            std::env::temp_dir().join(format!("svc-streaming-orch-unit-{}", Uuid::new_v4()));
        let orchestrator = test_orchestrator(data_dir.clone());
        assert!(orchestrator.registry().list("42").is_empty());
        tokio::fs::remove_dir_all(&data_dir).await.ok();
    }
}
