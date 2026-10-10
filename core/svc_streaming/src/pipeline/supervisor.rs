//! `ffmpeg` process supervision -- owned by S3. Implements the
//! [`PipelineEngine`] trait other modules (`http`/`api`, `ingest`,
//! `egress`) build against, wrapping `ffmpeg-sidecar` to spawn/monitor the
//! process [`crate::pipeline::ffmpeg::build_argv`] describes for one
//! [`PipelineSpec`]. See
//! `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md` §6 for the
//! supervision/observability contract this module implements.
//!
//! **Teardown (never shells out, never blocks a runtime worker).** Every
//! attempt ends through one path -- `teardown_attempt`:
//!
//! 1. *Graceful quit = EOF on stdin.* ffmpeg's stdin **is** the media pipe
//!    (`-i pipe:0`), so the old `q\n` "quit" command was just more media
//!    bytes ffmpeg could never interpret. Dropping our write end delivers
//!    EOF instead, which makes ffmpeg drain, write its muxer trailers and
//!    exit on its own.
//! 2. *Escalation* (stop requests only): grace -> `SIGTERM` to the whole
//!    process group -> grace -> `SIGKILL`, delivered by direct `killpg`
//!    syscalls ([`crate::pipeline::process_group`]) because the runtime
//!    image has no `kill` binary. A failed delivery is logged, counted and
//!    (for `SIGKILL`) retried against the direct child -- never ignored.
//! 3. *Sweep + reap.* The group is `SIGKILL`ed once more while the leader's
//!    PID is still reserved (so leaked helpers die), then the leader is
//!    reaped with a bounded non-blocking poll, and the stderr event thread
//!    is joined off the async workers with a bound. A child that cannot be
//!    reaped is handed to a detached blocking reaper and the pipeline is
//!    marked `Failed` rather than respawned.
//!
//! Every wait above is bounded ([`SupervisorConfig::stop_deadline`]), so one
//! pipeline's stuck teardown cannot wedge `stop()`, a runtime worker, or the
//! service's `/health`.
//!
//! **Progress parsing choice:** `ffmpeg-sidecar`'s built-in
//! `FfmpegChild::iter()` event stream (parsed from ffmpeg's default
//! stderr stats line) is used, not a hand-rolled `-progress pipe:2
//! -nostats` argv injection -- it's the crate's primary documented usage
//! pattern, needs no extra argv wiring, and `FfmpegEvent::Progress` already
//! carries every field spec §6 asks for (`frame`, `fps`, `bitrate_kbps`,
//! `speed`).
//!
//! **Known model gap (flagged for the model owner, not worked around by
//! bloating `model::PipelineStatus`):** spec §6 asks for `PipelineStatus`
//! to carry `frame`/`fps`/`bitrate_kbps`/`speed`/`uptime`/`restarts`/
//! `last_error`, but `model::PipelineStatus` is a fixed `{id, state,
//! detail}` shape that `tests/model.rs` (owned by a different chunk)
//! already exercises with an exhaustive struct literal -- adding required
//! fields there would break a file this chunk does not own. The rich
//! fields live in [`PipelineProgress`] instead: [`FfmpegSupervisor::status`]
//! (the `PipelineEngine` trait method) folds them into `PipelineStatus.detail`
//! as a formatted summary; [`FfmpegSupervisor::progress`] returns the full
//! structured value for a later chunk (S2's `/api/v1/*` routes) to expose.

use std::collections::HashMap;
use std::io::Write as _;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Mutex as StdMutex, MutexGuard, PoisonError, TryLockError};
use std::time::{Duration, Instant};

use ffmpeg_sidecar::command::FfmpegCommand;
use ffmpeg_sidecar::event::{FfmpegEvent, LogLevel};
use opentelemetry::metrics::{Counter, Gauge, Histogram};
use opentelemetry::{global, KeyValue};
use tokio::sync::{mpsc, Mutex as TokioMutex, Notify, RwLock as TokioRwLock};
use tokio::task::JoinHandle;

use crate::pipeline::ffmpeg;
use crate::pipeline::model::{
    OutputSpec, PipelineEngine, PipelineError, PipelineHandle, PipelineId, PipelineSpec,
    PipelineState, PipelineStatus,
};
use crate::pipeline::process_group::{self, Delivery, GroupSignal};
use crate::store::{SecretRef, SecretResolver};

/// How often [`wait_for_exit`] re-checks child liveness between event-channel
/// reads. Short enough that a clean exit is noticed within a frame or two.
const EXIT_POLL_INTERVAL: Duration = Duration::from_millis(25);

/// Locks a std mutex, recovering the guard if a panicking holder poisoned it.
/// The guarded data here (progress counters, an optional pipe handle) has no
/// cross-field invariant a mid-update panic could break, and refusing to lock
/// would turn one panic into a wedged pipeline -- the failure mode this module
/// exists to prevent.
fn lock<T>(mutex: &StdMutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Tunable timings for the ffmpeg lifecycle (spec §6). [`Default`] matches
/// the spec's production defaults; tests inject a config with millisecond-
/// scale values so the lifecycle suite runs fast.
#[derive(Debug, Clone)]
pub struct SupervisorConfig {
    /// Initial restart backoff after an unexpected exit.
    pub backoff_initial: Duration,
    /// Restart backoff ceiling (doubles each attempt, capped here).
    pub backoff_max: Duration,
    /// Restart attempts allowed before a pipeline is marked `Failed`.
    pub max_restarts: u32,
    /// No `Progress` event for this long while running -> forced restart.
    pub stall_timeout: Duration,
    /// Grace period after closing ffmpeg's stdin (EOF on the `pipe:0` media
    /// input -- the graceful-quit signal) before escalating to SIGTERM.
    pub stop_grace_timeout: Duration,
    /// Grace period after SIGTERM before escalating to SIGKILL.
    pub term_grace_timeout: Duration,
    /// Upper bound on each post-SIGKILL step -- waiting for the exit, reaping
    /// the leader, and joining the stderr event thread. SIGKILL cannot be
    /// ignored, so this only expires for a process stuck in uninterruptible
    /// kernel sleep; the teardown then fails loudly instead of hanging.
    pub reap_timeout: Duration,
}

impl SupervisorConfig {
    /// Worst-case wall time of one pipeline teardown: both grace periods
    /// expire, then the post-SIGKILL exit wait, the reap and the event-thread
    /// join each consume their full [`Self::reap_timeout`]. [`FfmpegSupervisor::stop`]
    /// bounds its wait on the monitor task by this value.
    pub fn stop_deadline(&self) -> Duration {
        self.stop_grace_timeout + self.term_grace_timeout + self.reap_timeout * 3
    }
}

impl Default for SupervisorConfig {
    fn default() -> Self {
        Self {
            backoff_initial: Duration::from_secs(1),
            backoff_max: Duration::from_secs(30),
            max_restarts: 10,
            stall_timeout: Duration::from_secs(10),
            stop_grace_timeout: Duration::from_secs(3),
            term_grace_timeout: Duration::from_secs(3),
            reap_timeout: Duration::from_secs(5),
        }
    }
}

/// Rich per-pipeline encode telemetry -- see module docs for why this
/// lives alongside [`model::PipelineStatus`](crate::pipeline::model::PipelineStatus)
/// instead of extending it.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct PipelineProgress {
    pub state: PipelineState,
    pub frame: u32,
    pub fps: f32,
    pub bitrate_kbps: f32,
    pub speed: f32,
    pub uptime_secs: u64,
    pub restarts: u32,
    pub last_error: Option<String>,
}

impl PipelineProgress {
    fn starting() -> Self {
        Self {
            state: PipelineState::Starting,
            frame: 0,
            fps: 0.0,
            bitrate_kbps: 0.0,
            speed: 0.0,
            uptime_secs: 0,
            restarts: 0,
            last_error: None,
        }
    }
}

/// Handle for writing raw ingest bytes (RTMP/SRT listener output) into a
/// running pipeline's `ffmpeg` stdin. `ffmpeg-sidecar`'s
/// `FfmpegChild::take_stdin()` permanently takes ownership of the stdin
/// channel (unlike `send_stdin_command`/`quit`, which only borrow it via
/// take-then-replace) -- so exactly one owner of the channel must exist.
/// The supervisor's graceful stop closes this same slot (dropping the write
/// end delivers EOF on `pipe:0`) rather than competing for a second handle;
/// writes after that fail fast with `InvalidSpec` instead of blocking.
#[derive(Clone)]
pub struct StdinHandle {
    slot: Arc<Slot>,
}

impl StdinHandle {
    /// Writes `bytes` to the pipeline's ffmpeg stdin. Blocking under the
    /// hood (`std::process::ChildStdin`) -- callers on an async ingest
    /// path should wrap sustained writes in `tokio::task::spawn_blocking`.
    pub fn write(&self, bytes: &[u8]) -> Result<(), PipelineError> {
        let mut guard = lock(&self.slot.stdin);
        match guard.as_mut() {
            Some(stdin) => stdin.write_all(bytes).map_err(|err| {
                PipelineError::Other(anyhow::anyhow!("ffmpeg stdin write failed: {err}"))
            }),
            None => Err(PipelineError::InvalidSpec(
                "pipeline has no ffmpeg stdin available (not yet spawned, or already stopped)"
                    .into(),
            )),
        }
    }
}

struct SlotState {
    progress: PipelineProgress,
}

/// Per-pipeline shared state, referenced by the registry and by the
/// background monitor task spawned in [`FfmpegSupervisor::start`].
struct Slot {
    state: StdMutex<SlotState>,
    stdin: StdMutex<Option<std::process::ChildStdin>>,
    stdout: StdMutex<Option<std::process::ChildStdout>>,
    stdout_taken: AtomicBool,
    stopping: AtomicBool,
    stop_notify: Notify,
    monitor: TokioMutex<Option<JoinHandle<()>>>,
    /// `false` for a pure-copy WHIP->WHEP pipeline (spec §1/§7 case 6) --
    /// no ffmpeg process, no monitor task, stop() just deregisters it.
    has_process: bool,
}

#[derive(Clone)]
struct Metrics {
    pipeline_start_ms: Histogram<u64>,
    encode_speed: Histogram<f64>,
    output_bitrate_kbps: Histogram<f64>,
    active_pipelines: Gauge<i64>,
    restarts_total: Counter<u64>,
    output_failures_total: Counter<u64>,
    teardown_duration_ms: Histogram<u64>,
    teardown_failures_total: Counter<u64>,
}

impl Metrics {
    fn new() -> Self {
        let meter = global::meter("svc_streaming_pipeline");
        Self {
            pipeline_start_ms: meter
                .u64_histogram("pipeline_start_ms")
                .with_description("Time from ffmpeg spawn to first progress event")
                .with_unit("ms")
                .build(),
            encode_speed: meter
                .f64_histogram("encode_speed")
                .with_description("ffmpeg's reported encode speed multiplier (speed=1.02x -> 1.02)")
                .build(),
            output_bitrate_kbps: meter
                .f64_histogram("output_bitrate_kbps")
                .with_description(
                    "ffmpeg's aggregate output bitrate across all mapped outputs (tee doesn't report per-slave bitrate)",
                )
                .with_unit("kbps")
                .build(),
            active_pipelines: meter
                .i64_gauge("active_pipelines")
                .with_description("Currently registered pipelines (running or starting)")
                .build(),
            restarts_total: meter
                .u64_counter("restarts_total")
                .with_description("Pipeline restart attempts, labeled by pipeline_id/reason")
                .build(),
            output_failures_total: meter
                .u64_counter("output_failures_total")
                .with_description("Terminal pipeline/output failures, labeled by pipeline_id/kind/reason")
                .build(),
            teardown_duration_ms: meter
                .u64_histogram("teardown_duration_ms")
                .with_description("Wall time of one ffmpeg attempt teardown (sweep + reap + event-thread join)")
                .with_unit("ms")
                .build(),
            teardown_failures_total: meter
                .u64_counter("teardown_failures_total")
                .with_description(
                    "Teardown steps that failed or timed out, labeled by stage (signal/reap/join/stop_deadline) and signal",
                )
                .build(),
        }
    }
}

/// [`PipelineEngine`] implementation that spawns and supervises real
/// `ffmpeg` processes via `ffmpeg-sidecar`, against the **system** binary
/// at `ffmpeg_path` (never the crate's auto-download -- see `Cargo.toml`'s
/// `deny.toml` ban on linking `ffmpeg-next` and the container image's
/// packaged `ffmpeg`).
pub struct FfmpegSupervisor {
    ffmpeg_path: PathBuf,
    stream_data_dir: PathBuf,
    rtp_base_port: u16,
    resolver: Arc<dyn SecretResolver>,
    config: SupervisorConfig,
    registry: Arc<TokioRwLock<HashMap<PipelineId, Arc<Slot>>>>,
    metrics: Arc<Metrics>,
    active_count: Arc<AtomicI64>,
}

impl FfmpegSupervisor {
    /// Builds a supervisor bound to a specific `ffmpeg` binary
    /// (`FFMPEG_PATH`), local staging root (`STREAM_DATA_DIR`), first RTP
    /// handoff port (`WEBRTC_UDP_RANGE` start), secret resolver, and
    /// lifecycle timings.
    pub fn new(
        ffmpeg_path: PathBuf,
        stream_data_dir: PathBuf,
        rtp_base_port: u16,
        resolver: Arc<dyn SecretResolver>,
        config: SupervisorConfig,
    ) -> Self {
        Self {
            ffmpeg_path,
            stream_data_dir,
            rtp_base_port,
            resolver,
            config,
            registry: Arc::new(TokioRwLock::new(HashMap::new())),
            metrics: Arc::new(Metrics::new()),
            active_count: Arc::new(AtomicI64::new(0)),
        }
    }

    /// Returns a handle listeners (`ingest::rtmp`/`ingest::srt`) use to
    /// write raw demuxed bytes into the pipeline's ffmpeg stdin.
    pub async fn stdin_writer(&self, id: PipelineId) -> Result<StdinHandle, PipelineError> {
        let slot = self.get_slot(id).await?;
        Ok(StdinHandle { slot })
    }

    /// Takes ownership of the pipeline's ffmpeg stdout, for a
    /// `DiscordVoice` output's raw PCM sink. May be called only once per
    /// pipeline; polls briefly since the monitor task may not have
    /// attached stdout to the slot yet immediately after `start()`
    /// returns.
    pub async fn take_stdout(
        &self,
        id: PipelineId,
    ) -> Result<std::process::ChildStdout, PipelineError> {
        let slot = self.get_slot(id).await?;
        if slot.stdout_taken.swap(true, Ordering::SeqCst) {
            return Err(PipelineError::InvalidSpec(
                "stdout has already been taken for this pipeline".into(),
            ));
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(stdout) = lock(&slot.stdout).take() {
                return Ok(stdout);
            }
            if Instant::now() >= deadline {
                return Err(PipelineError::InvalidSpec(
                    "pipeline has no stdout PCM sink available (no DiscordVoice output, or ffmpeg has not started yet)"
                        .into(),
                ));
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    /// Returns the full structured encode telemetry for `id` -- see
    /// module docs for why this is separate from the trait's `status()`.
    pub async fn progress(&self, id: PipelineId) -> Result<PipelineProgress, PipelineError> {
        let slot = self.get_slot(id).await?;
        let progress = lock(&slot.state).progress.clone();
        Ok(progress)
    }

    async fn get_slot(&self, id: PipelineId) -> Result<Arc<Slot>, PipelineError> {
        self.registry
            .read()
            .await
            .get(&id)
            .cloned()
            .ok_or(PipelineError::NotFound(id))
    }

    fn bump_active(&self, delta: i64) {
        let prev = self.active_count.fetch_add(delta, Ordering::SeqCst);
        self.metrics
            .active_pipelines
            .record((prev + delta).max(0), &[]);
    }

    /// Resolves every `SecretRef`-backed output URL in `spec` up front,
    /// building the [`ffmpeg::Paths`] `build_argv` needs. `WHIP` SDP paths
    /// are left empty -- `ingest::whip` (S6) does not yet own real spawn
    /// wiring in this scaffold.
    async fn build_paths(&self, spec: &PipelineSpec) -> Result<ffmpeg::Paths, PipelineError> {
        let mut resolved_secrets = HashMap::new();
        for output in &spec.outputs {
            let secret_ref: Option<&SecretRef> = match output {
                OutputSpec::RtmpPush { url_secret_ref }
                | OutputSpec::SrtPush { url_secret_ref } => Some(url_secret_ref),
                _ => None,
            };
            if let Some(secret_ref) = secret_ref {
                let key = ffmpeg::secret_ref_key(secret_ref);
                if let std::collections::hash_map::Entry::Vacant(entry) =
                    resolved_secrets.entry(key)
                {
                    let value = self.resolver.resolve(secret_ref).map_err(|err| {
                        PipelineError::InvalidSpec(format!("failed to resolve secret ref: {err}"))
                    })?;
                    entry.insert(value.expose().to_string());
                }
            }
        }
        Ok(ffmpeg::Paths {
            stream_data_dir: self.stream_data_dir.clone(),
            resolved_secrets,
            whip_sdp_paths: HashMap::new(),
            rtp_base_port: self.rtp_base_port,
        })
    }
}

impl FfmpegSupervisor {
    /// Like [`PipelineEngine::start`], but merges `whip_sdp_paths` into the
    /// resolved [`ffmpeg::Paths`] before building argv -- the orchestrator
    /// (`src/orchestrator.rs`) calls this instead of the trait method when
    /// `spec.inputs[0]` is [`crate::pipeline::model::InputSpec::Whip`] and
    /// it already knows the `input.sdp` path from
    /// `crate::ingest::whip::WhipState::sdp_path_for` -- [`Self::build_paths`]
    /// alone has no way to learn that path (it isn't derivable from `spec`
    /// or this supervisor's own config, see that method's doc comment).
    /// Called through the concrete type, not [`PipelineEngine`] (whose
    /// signature is shared with every other engine implementation and
    /// can't carry WHIP-specific extras) -- this is why the orchestrator
    /// holds `Arc<FfmpegSupervisor>` directly rather than only
    /// `SharedEngine`.
    pub async fn start_with_whip_sdp(
        &self,
        spec: PipelineSpec,
        whip_sdp_paths: HashMap<usize, PathBuf>,
    ) -> Result<PipelineHandle, PipelineError> {
        self.start_inner(spec, whip_sdp_paths).await
    }

    async fn start_inner(
        &self,
        spec: PipelineSpec,
        extra_whip_sdp_paths: HashMap<usize, PathBuf>,
    ) -> Result<PipelineHandle, PipelineError> {
        let id = spec.id;
        if self.registry.read().await.contains_key(&id) {
            tracing::debug!(pipeline_id = %id, "start() called for an already-registered pipeline id -- returning the existing handle");
            return Ok(PipelineHandle { id });
        }

        let mut paths = self.build_paths(&spec).await?;
        paths.whip_sdp_paths.extend(extra_whip_sdp_paths);

        let argv = match ffmpeg::build_argv(&spec, &paths) {
            Ok(argv) => argv,
            Err(PipelineError::NoFfmpegNeeded) => {
                // A pure-copy WHIP->WHEP leg needs no ffmpeg process at all
                // -- webrtc-rs forwards RTP directly (SFU-style). Register
                // the pipeline as already-`Running` with no monitor task.
                tracing::info!(pipeline_id = %id, "pure-copy WHIP->WHEP leg, no ffmpeg process needed");
                let slot = Arc::new(Slot {
                    state: StdMutex::new(SlotState {
                        progress: PipelineProgress {
                            state: PipelineState::Running,
                            ..PipelineProgress::starting()
                        },
                    }),
                    stdin: StdMutex::new(None),
                    stdout: StdMutex::new(None),
                    stdout_taken: AtomicBool::new(false),
                    stopping: AtomicBool::new(false),
                    stop_notify: Notify::new(),
                    monitor: TokioMutex::new(None),
                    has_process: false,
                });
                self.registry.write().await.insert(id, slot);
                self.bump_active(1);
                return Ok(PipelineHandle { id });
            }
            Err(other) => return Err(other),
        };

        let needs_stdout = spec
            .outputs
            .iter()
            .any(|o| matches!(o, OutputSpec::DiscordVoice { .. }));
        let slot = Arc::new(Slot {
            state: StdMutex::new(SlotState {
                progress: PipelineProgress::starting(),
            }),
            stdin: StdMutex::new(None),
            stdout: StdMutex::new(None),
            stdout_taken: AtomicBool::new(false),
            stopping: AtomicBool::new(false),
            stop_notify: Notify::new(),
            monitor: TokioMutex::new(None),
            has_process: true,
        });
        self.registry.write().await.insert(id, slot.clone());
        self.bump_active(1);

        let span = tracing::info_span!("pipeline", pipeline_id = %id, tenant = %spec.tenant);
        let handle = tokio::spawn(
            run_pipeline(
                id,
                self.ffmpeg_path.clone(),
                argv,
                needs_stdout,
                slot.clone(),
                self.config.clone(),
                self.metrics.clone(),
                self.active_count.clone(),
            )
            .instrument(span),
        );
        *slot.monitor.lock().await = Some(handle);
        Ok(PipelineHandle { id })
    }
}

impl PipelineEngine for FfmpegSupervisor {
    async fn start(&self, spec: PipelineSpec) -> Result<PipelineHandle, PipelineError> {
        self.start_inner(spec, HashMap::new()).await
    }

    async fn stop(&self, id: PipelineId) -> Result<(), PipelineError> {
        let slot = match self.registry.read().await.get(&id).cloned() {
            Some(slot) => slot,
            None => return Ok(()), // idempotent: unknown id is not an error
        };

        if !slot.has_process {
            self.registry.write().await.remove(&id);
            self.bump_active(-1);
            lock(&slot.state).progress.state = PipelineState::Stopped;
            return Ok(());
        }

        slot.stopping.store(true, Ordering::SeqCst);
        slot.stop_notify.notify_one();

        let handle = slot.monitor.lock().await.take();
        if let Some(mut handle) = handle {
            // The monitor task performs the full EOF -> SIGTERM -> SIGKILL
            // -> sweep -> reap sequence itself (it owns the child) and
            // decrements active_pipelines on the way out. Every step inside
            // it is individually bounded, so this deadline is a backstop that
            // should never fire; if it does, the failure is loud (error log +
            // counter), the registry entry is still dropped, and the task is
            // left to finish detached rather than aborted mid-teardown (an
            // abort could orphan the very child it is trying to reap).
            let deadline = self.config.stop_deadline();
            match tokio::time::timeout(deadline, &mut handle).await {
                Ok(Ok(())) => {}
                Ok(Err(join_err)) => {
                    tracing::error!(pipeline_id = %id, error = %join_err, "pipeline monitor task ended abnormally during stop");
                }
                Err(_) => {
                    tracing::error!(pipeline_id = %id, ?deadline, "pipeline monitor task did not finish within the stop deadline -- deregistering anyway");
                    self.metrics
                        .teardown_failures_total
                        .add(1, &[KeyValue::new("stage", "stop_deadline")]);
                }
            }
        }

        self.registry.write().await.remove(&id);
        Ok(())
    }

    async fn status(&self, id: PipelineId) -> Result<PipelineStatus, PipelineError> {
        let slot = self.get_slot(id).await?;
        let progress = lock(&slot.state).progress.clone();
        let mut detail = format!(
            "frame={} fps={:.1} bitrate_kbps={:.0} speed={:.2}x uptime_s={} restarts={}",
            progress.frame,
            progress.fps,
            progress.bitrate_kbps,
            progress.speed,
            progress.uptime_secs,
            progress.restarts
        );
        if !slot.has_process {
            detail.push_str(" note=\"no ffmpeg process (pure RTP forward)\"");
        }
        if let Some(err) = &progress.last_error {
            detail.push_str(&format!(" last_error={err:?}"));
        }
        Ok(PipelineStatus {
            id,
            state: progress.state,
            detail: Some(detail),
        })
    }
}

/// Placeholder [`PipelineEngine`] that answers every call with
/// `PipelineError::Unimplemented`, predating [`FfmpegSupervisor`] (the
/// real implementation this file now provides). Kept -- not removed --
/// because `api::engine::default_engine()` (a different chunk's file,
/// out of scope here) references it by name; that wiring is S2's/the
/// router owner's follow-up to switch to `FfmpegSupervisor`, not this
/// chunk's to make unilaterally by editing `src/api/*`.
#[derive(Debug, Default, Clone, Copy)]
pub struct StubSupervisor;

impl PipelineEngine for StubSupervisor {
    async fn start(&self, spec: PipelineSpec) -> Result<PipelineHandle, PipelineError> {
        tracing::debug!(pipeline_id = %spec.id, tenant = %spec.tenant, "PipelineEngine::start is not yet implemented (StubSupervisor)");
        Err(PipelineError::Unimplemented("PipelineEngine::start"))
    }

    async fn stop(&self, id: PipelineId) -> Result<(), PipelineError> {
        tracing::debug!(pipeline_id = %id, "PipelineEngine::stop is not yet implemented (StubSupervisor)");
        Err(PipelineError::Unimplemented("PipelineEngine::stop"))
    }

    async fn status(&self, id: PipelineId) -> Result<PipelineStatus, PipelineError> {
        tracing::debug!(pipeline_id = %id, "PipelineEngine::status is not yet implemented (StubSupervisor)");
        Err(PipelineError::Unimplemented("PipelineEngine::status"))
    }
}

/// Events forwarded from the blocking `ffmpeg-sidecar` iterator thread to
/// the async monitor loop.
enum MonitorEvent {
    Progress(ffmpeg_sidecar::event::FfmpegProgress),
    Error(String),
    StreamEnded,
}

use tracing::Instrument as _;

#[allow(clippy::too_many_arguments)]
async fn run_pipeline(
    id: PipelineId,
    ffmpeg_path: PathBuf,
    argv: Vec<String>,
    needs_stdout: bool,
    slot: Arc<Slot>,
    config: SupervisorConfig,
    metrics: Arc<Metrics>,
    active_count: Arc<AtomicI64>,
) {
    let pid_kv = [KeyValue::new("pipeline_id", id.to_string())];
    let mut backoff = config.backoff_initial;
    let mut restarts: u32 = 0;
    let run_start = Instant::now();

    'outer: loop {
        if slot.stopping.load(Ordering::SeqCst) {
            set_state(&slot, PipelineState::Stopped, None);
            break;
        }

        set_state(&slot, PipelineState::Starting, None);
        let spawn_start = Instant::now();

        let attempt_span =
            tracing::info_span!("pipeline_attempt", pipeline_id = %id, attempt = restarts);
        let mut cmd = FfmpegCommand::new_with_path(&ffmpeg_path);
        cmd.args(&argv);
        // Spawn into a fresh process group (PGID == the child's own PID)
        // so a stall-triggered kill or the stop-sequence SIGTERM/SIGKILL
        // reaches every process ffmpeg forks (e.g. filter helper
        // processes), not just the direct child -- otherwise an orphaned
        // grandchild can keep the stdout/stderr pipes open and the
        // supervisor never observes EOF. See `process_group::signal_group`.
        #[cfg(unix)]
        std::os::unix::process::CommandExt::process_group(cmd.as_inner_mut(), 0);
        let spawn_result = attempt_span.in_scope(|| cmd.spawn());
        let mut child = match spawn_result {
            Ok(child) => child,
            Err(err) => {
                tracing::warn!(pipeline_id = %id, error = %err, "ffmpeg spawn failed");
                metrics.restarts_total.add(
                    1,
                    &[
                        KeyValue::new("pipeline_id", id.to_string()),
                        KeyValue::new("reason", "spawn_error"),
                    ],
                );
                restarts += 1;
                lock(&slot.state).progress.restarts = restarts;
                if restarts > config.max_restarts {
                    set_state(
                        &slot,
                        PipelineState::Failed,
                        Some(format!("spawn failed: {err}")),
                    );
                    break 'outer;
                }
                set_state(
                    &slot,
                    PipelineState::Degraded,
                    Some(format!("spawn failed: {err}")),
                );
                backoff_sleep(&slot, backoff).await;
                backoff = (backoff * 2).min(config.backoff_max);
                continue 'outer;
            }
        };

        // Take stdout FIRST when a PCM sink is needed, so `.iter()` (which
        // opportunistically takes stdout too, for OutputFrame/OutputChunk
        // parsing) sees it already gone and leaves it alone.
        if needs_stdout {
            if let Some(stdout) = child.take_stdout() {
                *lock(&slot.stdout) = Some(stdout);
            }
        }
        if let Some(stdin) = child.take_stdin() {
            *lock(&slot.stdin) = Some(stdin);
        } else {
            tracing::warn!(pipeline_id = %id, "ffmpeg child has no stdin channel");
        }

        let (tx, mut rx) = mpsc::channel::<MonitorEvent>(64);
        let iter_thread = match child.iter() {
            Ok(iter) => {
                let tx2 = tx.clone();
                Some(std::thread::spawn(move || {
                    for event in iter {
                        let forwarded = match event {
                            FfmpegEvent::Progress(p) => {
                                tx2.blocking_send(MonitorEvent::Progress(p))
                            }
                            FfmpegEvent::Error(e) => tx2.blocking_send(MonitorEvent::Error(e)),
                            FfmpegEvent::Log(LogLevel::Error, msg)
                            | FfmpegEvent::Log(LogLevel::Fatal, msg) => {
                                tx2.blocking_send(MonitorEvent::Error(msg))
                            }
                            _ => Ok(()),
                        };
                        if forwarded.is_err() {
                            break;
                        }
                    }
                    let _ = tx2.blocking_send(MonitorEvent::StreamEnded);
                }))
            }
            Err(err) => {
                tracing::warn!(pipeline_id = %id, error = %err, "failed to attach to the ffmpeg event stream");
                None
            }
        };
        drop(tx);

        set_state(&slot, PipelineState::Running, None);
        let mut got_first_progress = false;

        'attempt: loop {
            let stall = tokio::time::sleep(config.stall_timeout);
            tokio::select! {
                _ = slot.stop_notify.notified() => {
                    if slot.stopping.load(Ordering::SeqCst) {
                        graceful_stop(id, &mut child, &mut rx, &slot, &config, &metrics).await;
                        break 'attempt;
                    }
                }
                msg = rx.recv() => {
                    match msg {
                        Some(MonitorEvent::Progress(p)) => {
                            if !got_first_progress {
                                metrics
                                    .pipeline_start_ms
                                    .record(spawn_start.elapsed().as_millis() as u64, &pid_kv);
                                got_first_progress = true;
                            }
                            metrics.encode_speed.record(p.speed as f64, &pid_kv);
                            metrics.output_bitrate_kbps.record(p.bitrate_kbps as f64, &pid_kv);
                            let mut s = lock(&slot.state);
                            s.progress.state = PipelineState::Running;
                            s.progress.frame = p.frame;
                            s.progress.fps = p.fps;
                            s.progress.bitrate_kbps = p.bitrate_kbps;
                            s.progress.speed = p.speed;
                            s.progress.uptime_secs = run_start.elapsed().as_secs();
                        }
                        Some(MonitorEvent::Error(e)) => {
                            tracing::warn!(pipeline_id = %id, error = %e, "ffmpeg reported an error");
                            set_state(&slot, PipelineState::Degraded, Some(e));
                        }
                        Some(MonitorEvent::StreamEnded) | None => {
                            break 'attempt;
                        }
                    }
                }
                _ = stall, if got_first_progress => {
                    tracing::warn!(pipeline_id = %id, stall_timeout = ?config.stall_timeout, "no progress advance -- forcing a restart");
                    metrics.restarts_total.add(
                        1,
                        &[
                            KeyValue::new("pipeline_id", id.to_string()),
                            KeyValue::new("reason", "stall"),
                        ],
                    );
                    // `teardown_attempt` below SIGKILLs the whole group.
                    break 'attempt;
                }
            }
        }

        let teardown_clean =
            teardown_attempt(id, child, rx, iter_thread, &slot, &config, &metrics).await;

        if slot.stopping.load(Ordering::SeqCst) {
            set_state(&slot, PipelineState::Stopped, None);
            break 'outer;
        }
        if !teardown_clean {
            // A child that survived SIGKILL (uninterruptible kernel sleep) is
            // a hazard; respawning on top of it would stack a second ffmpeg
            // on the same outputs. Fail loudly and leave the slot for an
            // operator / an explicit stop() + restart.
            metrics.output_failures_total.add(
                1,
                &[
                    KeyValue::new("pipeline_id", id.to_string()),
                    KeyValue::new("kind", "process"),
                    KeyValue::new("reason", "teardown_unclean"),
                ],
            );
            set_state(
                &slot,
                PipelineState::Failed,
                Some("ffmpeg process could not be reaped after SIGKILL".into()),
            );
            break 'outer;
        }

        restarts += 1;
        lock(&slot.state).progress.restarts = restarts;
        metrics.restarts_total.add(
            1,
            &[
                KeyValue::new("pipeline_id", id.to_string()),
                KeyValue::new("reason", "exit"),
            ],
        );
        if restarts > config.max_restarts {
            metrics.output_failures_total.add(
                1,
                &[
                    KeyValue::new("pipeline_id", id.to_string()),
                    KeyValue::new("kind", "process"),
                    KeyValue::new("reason", "max_restarts_exceeded"),
                ],
            );
            set_state(
                &slot,
                PipelineState::Failed,
                Some("max restart attempts exceeded".into()),
            );
            break 'outer;
        }
        set_state(
            &slot,
            PipelineState::Degraded,
            Some(format!("restarting (attempt {restarts})")),
        );
        backoff_sleep(&slot, backoff).await;
        backoff = (backoff * 2).min(config.backoff_max);
    }

    let prev = active_count.fetch_sub(1, Ordering::SeqCst);
    metrics.active_pipelines.record((prev - 1).max(0), &[]);
}

fn set_state(slot: &Slot, state: PipelineState, error: Option<String>) {
    let mut s = lock(&slot.state);
    s.progress.state = state;
    if let Some(err) = error {
        s.progress.last_error = Some(err);
    }
}

/// Sleeps for `delay` (a restart backoff) but wakes immediately when a stop
/// is requested, so `stop()` never waits out a 30s backoff behind a pipeline
/// that is between ffmpeg attempts.
async fn backoff_sleep(slot: &Slot, delay: Duration) {
    tokio::select! {
        _ = tokio::time::sleep(delay) => {}
        _ = slot.stop_notify.notified() => {}
    }
}

/// Graceful stop sequence (spec §6): EOF on stdin -> grace period ->
/// SIGTERM to the process group -> grace period -> SIGKILL to the group.
///
/// stdin is ffmpeg's `pipe:0` *media* input, so a `q` written there is just
/// more (invalid) media bytes -- the only quit signal that cannot collide
/// with the data is closing the pipe, which ffmpeg sees as end of input and
/// answers by draining, writing muxer trailers and exiting. Exit is detected
/// by polling `waitid(WNOWAIT)` (not by stderr EOF, which a leaked helper
/// can delay indefinitely) while still draining `rx` so the stderr event
/// thread never parks on a full channel and stalls ffmpeg's own shutdown.
/// The caller ([`teardown_attempt`]) does the final sweep and reap.
async fn graceful_stop(
    id: PipelineId,
    child: &mut ffmpeg_sidecar::child::FfmpegChild,
    rx: &mut mpsc::Receiver<MonitorEvent>,
    slot: &Slot,
    config: &SupervisorConfig,
    metrics: &Metrics,
) {
    set_state(slot, PipelineState::Stopping, None);
    close_stdin(id, slot);
    if wait_for_exit(id, child, rx, config.stop_grace_timeout).await {
        tracing::debug!(pipeline_id = %id, "ffmpeg exited after stdin EOF");
        return;
    }
    tracing::warn!(pipeline_id = %id, "stdin-EOF grace period elapsed, sending SIGTERM to the process group");
    deliver_signal(id, child, GroupSignal::Term, metrics);
    if wait_for_exit(id, child, rx, config.term_grace_timeout).await {
        return;
    }
    tracing::warn!(pipeline_id = %id, "SIGTERM grace period elapsed, sending SIGKILL to the process group");
    deliver_signal(id, child, GroupSignal::Kill, metrics);
    if !wait_for_exit(id, child, rx, config.reap_timeout).await {
        tracing::error!(pipeline_id = %id, timeout = ?config.reap_timeout, "ffmpeg still alive after SIGKILL");
    }
}

/// Closes the pipeline's ffmpeg stdin by dropping our write end (EOF for
/// `-i pipe:0`). Never blocks: an ingest writer parked inside `write_all`
/// holds this mutex, and waiting for it on an async worker is exactly the
/// kind of wedge teardown must not have -- in that case EOF is skipped and
/// the signal ladder (which unblocks the writer with `EPIPE`) takes over.
fn close_stdin(id: PipelineId, slot: &Slot) {
    match slot.stdin.try_lock() {
        Ok(mut guard) => {
            if guard.take().is_some() {
                tracing::debug!(pipeline_id = %id, "closed ffmpeg stdin (EOF = end of media input)");
            }
        }
        Err(TryLockError::Poisoned(poisoned)) => {
            drop(poisoned.into_inner().take());
        }
        Err(TryLockError::WouldBlock) => {
            tracing::warn!(pipeline_id = %id, "ffmpeg stdin busy with an in-flight ingest write -- skipping EOF, escalating via signals");
        }
    }
}

/// Polls until `child` has exited (without reaping it) or `timeout`
/// elapses, returning whether it exited. Keeps draining `rx` meanwhile.
async fn wait_for_exit(
    id: PipelineId,
    child: &mut ffmpeg_sidecar::child::FfmpegChild,
    rx: &mut mpsc::Receiver<MonitorEvent>,
    timeout: Duration,
) -> bool {
    let deadline = tokio::time::Instant::now() + timeout;
    let mut events_open = true;
    loop {
        match process_group::has_exited(child.as_inner_mut()) {
            Ok(true) => return true,
            Ok(false) => {}
            Err(err) => {
                tracing::warn!(pipeline_id = %id, error = %err, "could not query ffmpeg exit status -- escalating");
                return false;
            }
        }
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return false;
        }
        let tick = remaining.min(EXIT_POLL_INTERVAL);
        if events_open {
            if let Ok(None) = tokio::time::timeout(tick, rx.recv()).await {
                events_open = false;
            }
        } else {
            tokio::time::sleep(tick).await;
        }
    }
}

/// Delivers `signal` to `child`'s whole process group and reports the
/// outcome -- a failed delivery is an error log + counter, never silently
/// dropped (the old shell-out ignored its exit status). If a `SIGKILL`
/// cannot be delivered to the group, falls back to killing the direct child
/// so the failure mode degrades to "orphaned helpers" rather than "ffmpeg
/// survives teardown".
fn deliver_signal(
    id: PipelineId,
    child: &mut ffmpeg_sidecar::child::FfmpegChild,
    signal: GroupSignal,
    metrics: &Metrics,
) {
    match process_group::signal_group(child.as_inner_mut(), signal) {
        Ok(Delivery::Delivered) => {
            tracing::debug!(pipeline_id = %id, signal = signal.name(), "signal delivered to ffmpeg process group");
        }
        Ok(Delivery::AlreadyGone) => {
            tracing::debug!(pipeline_id = %id, signal = signal.name(), "ffmpeg process group already gone");
        }
        Err(err) => {
            tracing::error!(pipeline_id = %id, signal = signal.name(), error = %err, "failed to signal ffmpeg process group");
            metrics.teardown_failures_total.add(
                1,
                &[
                    KeyValue::new("stage", "signal"),
                    KeyValue::new("signal", signal.name()),
                ],
            );
            if signal == GroupSignal::Kill {
                if let Err(kill_err) = child.kill() {
                    tracing::error!(pipeline_id = %id, error = %kill_err, "fallback direct kill of the ffmpeg child also failed");
                }
            }
        }
    }
}

/// Ends one ffmpeg attempt. Runs after *every* attempt (stop request,
/// stderr EOF, stall) and returns `true` only if the leader was reaped.
///
/// 1. drop `rx` so the stderr event thread's `blocking_send` fails fast
///    instead of parking on a full channel (the old code joined that thread
///    with the receiver still alive and un-drained);
/// 2. close stdin;
/// 3. `SIGKILL` the whole group **before** reaping -- the leader's PID (the
///    group ID) stays reserved until reaped, so leaked helpers die and the
///    signal can never hit a recycled group;
/// 4. reap with a bounded non-blocking poll (never `Child::wait()` on a
///    runtime worker);
/// 5. join the event thread off the workers, bounded.
///
/// A child that is still unreaped after `reap_timeout` is moved to a
/// detached blocking reaper so it cannot linger as a zombie, and the failure
/// is counted -- teardown never hangs and never reports success it did not
/// verify.
async fn teardown_attempt(
    id: PipelineId,
    mut child: ffmpeg_sidecar::child::FfmpegChild,
    rx: mpsc::Receiver<MonitorEvent>,
    event_thread: Option<std::thread::JoinHandle<()>>,
    slot: &Slot,
    config: &SupervisorConfig,
    metrics: &Metrics,
) -> bool {
    let started = Instant::now();
    drop(rx);
    close_stdin(id, slot);
    deliver_signal(id, &mut child, GroupSignal::Kill, metrics);

    let outcome = process_group::reap(child.as_inner_mut(), config.reap_timeout).await;
    let reaped = settle_reap(id, outcome, child, config.reap_timeout, metrics);

    join_event_thread(id, event_thread, config.reap_timeout, metrics).await;
    metrics
        .teardown_duration_ms
        .record(started.elapsed().as_millis() as u64, &[]);
    reaped
}

/// Turns the outcome of the bounded reap into the teardown verdict. `true`
/// only if the leader was actually collected; anything else is counted,
/// logged at error level, and the still-unreaped child is handed to a
/// detached blocking reaper so it cannot linger as a zombie.
fn settle_reap(
    id: PipelineId,
    outcome: std::io::Result<Option<std::process::ExitStatus>>,
    child: ffmpeg_sidecar::child::FfmpegChild,
    timeout: Duration,
    metrics: &Metrics,
) -> bool {
    match outcome {
        Ok(Some(status)) => {
            tracing::debug!(pipeline_id = %id, %status, "ffmpeg reaped");
            true
        }
        Ok(None) => {
            tracing::error!(pipeline_id = %id, ?timeout, "ffmpeg not reaped within the timeout after SIGKILL -- handing it to a detached reaper");
            metrics
                .teardown_failures_total
                .add(1, &[KeyValue::new("stage", "reap")]);
            detach_reaper(id, child);
            false
        }
        Err(err) => {
            tracing::error!(pipeline_id = %id, error = %err, "waiting on the ffmpeg child failed -- handing it to a detached reaper");
            metrics
                .teardown_failures_total
                .add(1, &[KeyValue::new("stage", "reap")]);
            detach_reaper(id, child);
            false
        }
    }
}

/// Moves an un-reapable child to the blocking pool, where a blocking
/// `wait()` is harmless, so it is collected whenever the kernel finally
/// releases it instead of lingering as a zombie.
fn detach_reaper(id: PipelineId, mut child: ffmpeg_sidecar::child::FfmpegChild) {
    drop(tokio::task::spawn_blocking(move || match child.wait() {
        Ok(status) => {
            tracing::warn!(pipeline_id = %id, %status, "detached reaper finally collected the ffmpeg child");
        }
        Err(err) => {
            tracing::error!(pipeline_id = %id, error = %err, "detached reaper failed to collect the ffmpeg child");
        }
    }));
}

/// Joins the stderr event thread on the blocking pool with a bound, so a
/// pipe held open by an escaped helper can park one pool thread but never an
/// async worker or the teardown itself.
async fn join_event_thread(
    id: PipelineId,
    thread: Option<std::thread::JoinHandle<()>>,
    timeout: Duration,
    metrics: &Metrics,
) {
    let Some(thread) = thread else {
        return;
    };
    let joiner = tokio::task::spawn_blocking(move || thread.join());
    match tokio::time::timeout(timeout, joiner).await {
        Ok(Ok(Ok(()))) => {}
        Ok(Ok(Err(_panic))) => {
            tracing::error!(pipeline_id = %id, "ffmpeg event thread panicked");
        }
        Ok(Err(join_err)) => {
            tracing::error!(pipeline_id = %id, error = %join_err, "joining the ffmpeg event thread failed");
        }
        Err(_) => {
            tracing::error!(pipeline_id = %id, ?timeout, "ffmpeg event thread did not exit in time (pipe held open?) -- leaving it detached");
            metrics
                .teardown_failures_total
                .add(1, &[KeyValue::new("stage", "join")]);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pipeline::model::{
        AudioCodec, InputSpec, OutputSpec as ModelOutputSpec, PipelineSpec, TranscodeProfile,
        VideoCodec,
    };
    use crate::store::DefaultSecretResolver;
    use uuid::Uuid;

    fn copy_spec(id: PipelineId) -> PipelineSpec {
        PipelineSpec {
            id,
            tenant: "tenant-1".into(),
            community_id: "community-1".into(),
            inputs: vec![InputSpec::Rtmp {
                stream_key: "sk1".into(),
            }],
            profiles: vec![TranscodeProfile {
                name: "copy".into(),
                video: VideoCodec::Copy,
                audio: AudioCodec::Copy,
                resolution: None,
                fps: None,
            }],
            outputs: vec![ModelOutputSpec::Record {
                profile: "copy".into(),
                target: crate::pipeline::model::ObjectStoreRef {
                    store: "local".into(),
                    prefix: "t".into(),
                },
            }],
        }
    }

    #[tokio::test]
    async fn status_of_unknown_id_is_not_found() {
        let sup = FfmpegSupervisor::new(
            PathBuf::from("/usr/bin/ffmpeg"),
            PathBuf::from("/tmp"),
            40000,
            Arc::new(DefaultSecretResolver),
            SupervisorConfig::default(),
        );
        let err = sup.status(Uuid::nil()).await.unwrap_err();
        assert!(matches!(err, PipelineError::NotFound(_)));
    }

    #[tokio::test]
    async fn stop_of_unknown_id_is_idempotent_ok() {
        let sup = FfmpegSupervisor::new(
            PathBuf::from("/usr/bin/ffmpeg"),
            PathBuf::from("/tmp"),
            40000,
            Arc::new(DefaultSecretResolver),
            SupervisorConfig::default(),
        );
        sup.stop(Uuid::nil())
            .await
            .expect("unknown id is not an error");
    }

    #[tokio::test]
    async fn start_missing_secret_ref_is_invalid_spec() {
        let sup = FfmpegSupervisor::new(
            PathBuf::from("/usr/bin/ffmpeg"),
            PathBuf::from("/tmp"),
            40000,
            Arc::new(DefaultSecretResolver),
            SupervisorConfig::default(),
        );
        let mut spec = copy_spec(Uuid::new_v4());
        spec.outputs.push(ModelOutputSpec::RtmpPush {
            url_secret_ref: crate::store::SecretRef::Env {
                var: "SVC_STREAMING_SUPERVISOR_TEST_MISSING".into(),
            },
        });
        let err = sup.start(spec).await.unwrap_err();
        assert!(matches!(err, PipelineError::InvalidSpec(_)));
    }

    #[tokio::test]
    async fn whip_to_whep_copy_registers_with_no_process() {
        let sup = FfmpegSupervisor::new(
            PathBuf::from("/usr/bin/ffmpeg"),
            PathBuf::from("/tmp"),
            40000,
            Arc::new(DefaultSecretResolver),
            SupervisorConfig::default(),
        );
        let id = Uuid::new_v4();
        let spec = PipelineSpec {
            id,
            tenant: "tenant-1".into(),
            community_id: "community-1".into(),
            inputs: vec![InputSpec::Whip {
                token: "tok".into(),
            }],
            profiles: vec![TranscodeProfile {
                name: "copy".into(),
                video: VideoCodec::Copy,
                audio: AudioCodec::Copy,
                resolution: None,
                fps: None,
            }],
            outputs: vec![ModelOutputSpec::Whep {
                profile: "copy".into(),
            }],
        };
        let handle = sup
            .start(spec)
            .await
            .expect("no-ffmpeg-needed still registers");
        assert_eq!(handle.id, id);
        let status = sup.status(id).await.expect("registered");
        assert_eq!(status.state, PipelineState::Running);
        // stdin never gets attached for a no-process pipeline -- write()
        // must report InvalidSpec, not panic.
        let stdin = sup.stdin_writer(id).await.expect("slot is registered");
        assert!(matches!(
            stdin.write(b"q\n"),
            Err(PipelineError::InvalidSpec(_))
        ));
        sup.stop(id).await.expect("stop succeeds");
        assert!(matches!(
            sup.status(id).await,
            Err(PipelineError::NotFound(_))
        ));
    }

    #[tokio::test]
    async fn start_is_idempotent_for_an_already_registered_id() {
        let sup = FfmpegSupervisor::new(
            PathBuf::from("/usr/bin/ffmpeg"),
            PathBuf::from("/tmp"),
            40000,
            Arc::new(DefaultSecretResolver),
            SupervisorConfig::default(),
        );
        let id = Uuid::new_v4();
        let spec = PipelineSpec {
            id,
            tenant: "tenant-1".into(),
            community_id: "community-1".into(),
            inputs: vec![InputSpec::Whip {
                token: "tok".into(),
            }],
            profiles: vec![TranscodeProfile {
                name: "copy".into(),
                video: VideoCodec::Copy,
                audio: AudioCodec::Copy,
                resolution: None,
                fps: None,
            }],
            outputs: vec![ModelOutputSpec::Whep {
                profile: "copy".into(),
            }],
        };
        let first = sup.start(spec.clone()).await.expect("first start succeeds");
        let second = sup.start(spec).await.expect("second start is idempotent");
        assert_eq!(first.id, second.id);
        sup.stop(id).await.expect("stop succeeds");
    }

    #[tokio::test]
    async fn start_propagates_unsupported_for_multi_input_spec() {
        let sup = FfmpegSupervisor::new(
            PathBuf::from("/usr/bin/ffmpeg"),
            PathBuf::from("/tmp"),
            40000,
            Arc::new(DefaultSecretResolver),
            SupervisorConfig::default(),
        );
        let mut spec = copy_spec(Uuid::new_v4());
        spec.inputs.push(InputSpec::Rtmp {
            stream_key: "sk2".into(),
        });
        let err = sup.start(spec).await.unwrap_err();
        assert!(matches!(err, PipelineError::Unsupported(_)));
    }

    #[tokio::test]
    async fn start_dedupes_a_secret_ref_shared_by_two_outputs() {
        // SAFETY: unique env var name, not touched by other tests.
        unsafe {
            std::env::set_var(
                "SVC_STREAMING_SUPERVISOR_TEST_SHARED_SECRET",
                "rtmp://push/shared",
            );
        }
        let sup = FfmpegSupervisor::new(
            PathBuf::from("/nonexistent/ffmpeg-binary-for-dedupe-test"),
            PathBuf::from("/tmp"),
            40000,
            Arc::new(DefaultSecretResolver),
            SupervisorConfig::default(),
        );
        let mut spec = copy_spec(Uuid::new_v4());
        let shared_ref = crate::store::SecretRef::Env {
            var: "SVC_STREAMING_SUPERVISOR_TEST_SHARED_SECRET".into(),
        };
        spec.outputs.push(ModelOutputSpec::RtmpPush {
            url_secret_ref: shared_ref.clone(),
        });
        spec.outputs.push(ModelOutputSpec::RtmpPush {
            url_secret_ref: shared_ref,
        });
        let id = spec.id;
        // start() only resolves secrets + builds argv synchronously; the
        // (bogus) ffmpeg binary is spawned asynchronously in the
        // background, so this succeeds even though that spawn will fail.
        sup.start(spec)
            .await
            .expect("secret dedup resolves once, no error");
        sup.stop(id).await.expect("stop succeeds");
        unsafe { std::env::remove_var("SVC_STREAMING_SUPERVISOR_TEST_SHARED_SECRET") };
    }

    #[tokio::test]
    async fn stub_supervisor_reports_unimplemented_for_every_method() {
        let stub = StubSupervisor;
        assert!(matches!(
            stub.start(copy_spec(Uuid::new_v4())).await,
            Err(PipelineError::Unimplemented("PipelineEngine::start"))
        ));
        assert!(matches!(
            stub.stop(Uuid::nil()).await,
            Err(PipelineError::Unimplemented("PipelineEngine::stop"))
        ));
        assert!(matches!(
            stub.status(Uuid::nil()).await,
            Err(PipelineError::Unimplemented("PipelineEngine::status"))
        ));
    }

    // ---- teardown helpers (real child processes, no mocks) ----

    /// A fresh `Slot` with no process, for exercising `close_stdin`.
    fn test_slot() -> Slot {
        Slot {
            state: StdMutex::new(SlotState {
                progress: PipelineProgress::starting(),
            }),
            stdin: StdMutex::new(None),
            stdout: StdMutex::new(None),
            stdout_taken: AtomicBool::new(false),
            stopping: AtomicBool::new(false),
            stop_notify: Notify::new(),
            monitor: TokioMutex::new(None),
            has_process: true,
        }
    }

    /// Writes an executable script that `exec`s a long `sleep` and returns
    /// its path. Arguments (ffmpeg-sidecar prepends some) are ignored.
    #[cfg(unix)]
    fn sleeper_script() -> PathBuf {
        use std::os::unix::fs::PermissionsExt as _;
        let path = std::env::temp_dir().join(format!(
            "svc-streaming-unit-sleeper-{}-{}.sh",
            std::process::id(),
            Uuid::new_v4()
        ));
        std::fs::write(&path, "#!/bin/sh\nexec sleep 300\n").expect("write sleeper");
        let mut perms = std::fs::metadata(&path).expect("stat").permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&path, perms).expect("chmod");
        path
    }

    /// Spawns the sleeper through `FfmpegCommand`, optionally as its own
    /// process-group leader (the way `run_pipeline` always does).
    #[cfg(unix)]
    fn spawn_sleeper(group_leader: bool) -> ffmpeg_sidecar::child::FfmpegChild {
        let mut cmd = FfmpegCommand::new_with_path(sleeper_script());
        if group_leader {
            std::os::unix::process::CommandExt::process_group(cmd.as_inner_mut(), 0);
        }
        cmd.spawn().expect("spawn sleeper")
    }

    #[cfg(unix)]
    async fn wait_until_reaped_by_kernel(pid: u32) {
        for _ in 0..300 {
            if !std::path::Path::new(&format!("/proc/{pid}")).exists() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("pid {pid} was never reaped");
    }

    #[cfg(unix)]
    fn kill_pid(pid: u32) {
        let raw = i32::try_from(pid).expect("pid fits i32");
        nix::sys::signal::kill(
            nix::unistd::Pid::from_raw(raw),
            nix::sys::signal::Signal::SIGKILL,
        )
        .expect("SIGKILL delivered");
    }

    /// A failed group-signal delivery is surfaced (not ignored like the old
    /// shell-out) and a failed SIGKILL falls back to killing the direct
    /// child. The child here is NOT a group leader, so `signal_group`
    /// refuses with `NotGroupLeader` rather than risk this test process's
    /// own group.
    #[cfg(unix)]
    #[tokio::test]
    async fn failed_group_signal_is_surfaced_and_sigkill_falls_back_to_the_direct_child() {
        let metrics = Metrics::new();
        let id = Uuid::new_v4();
        let mut child = spawn_sleeper(false);

        deliver_signal(id, &mut child, GroupSignal::Term, &metrics);
        assert!(
            child.as_inner_mut().try_wait().expect("try_wait").is_none(),
            "a refused SIGTERM has no fallback and must leave the child alone"
        );

        deliver_signal(id, &mut child, GroupSignal::Kill, &metrics);
        let status = process_group::reap(child.as_inner_mut(), Duration::from_secs(3))
            .await
            .expect("try_wait")
            .expect("fallback direct kill must terminate the child");
        assert!(!status.success());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn deliver_signal_to_a_group_leader_kills_it() {
        let metrics = Metrics::new();
        let mut child = spawn_sleeper(true);
        deliver_signal(Uuid::new_v4(), &mut child, GroupSignal::Kill, &metrics);
        process_group::reap(child.as_inner_mut(), Duration::from_secs(3))
            .await
            .expect("try_wait")
            .expect("group SIGKILL must terminate the leader");
    }

    #[cfg(unix)]
    #[test]
    fn close_stdin_drops_the_pipe_skips_when_busy_and_recovers_from_poison() {
        let id = Uuid::new_v4();
        let mut cat = std::process::Command::new("cat")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .spawn()
            .expect("spawn cat");

        // Normal case: the write end is dropped -> cat sees EOF and exits.
        let slot = test_slot();
        *lock(&slot.stdin) = cat.stdin.take();
        close_stdin(id, &slot);
        assert!(lock(&slot.stdin).is_none(), "stdin must be taken");
        assert!(cat.wait().expect("cat exits on EOF").success());

        // Busy case: an in-flight ingest write holds the lock -> skipped,
        // never blocked on, and the handle stays in place.
        let mut cat2 = std::process::Command::new("cat")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .spawn()
            .expect("spawn cat");
        let busy = test_slot();
        *lock(&busy.stdin) = cat2.stdin.take();
        {
            let _writer_holds_lock = lock(&busy.stdin);
            close_stdin(id, &busy);
        }
        assert!(lock(&busy.stdin).is_some(), "busy stdin must be left alone");
        close_stdin(id, &busy); // lock free now -> closes
        assert!(cat2.wait().expect("cat exits on EOF").success());

        // Poisoned case: a panicking holder must not wedge teardown.
        let mut cat3 = std::process::Command::new("cat")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .spawn()
            .expect("spawn cat");
        let poisoned = Arc::new(test_slot());
        *lock(&poisoned.stdin) = cat3.stdin.take();
        let poisoner = Arc::clone(&poisoned);
        let panicked = std::thread::spawn(move || {
            let _guard = poisoner.stdin.lock().expect("lock");
            panic!("poison the stdin mutex");
        })
        .join();
        assert!(panicked.is_err());
        assert!(poisoned.stdin.is_poisoned());
        close_stdin(id, &poisoned);
        assert!(lock(&poisoned.stdin).is_none());
        assert!(cat3.wait().expect("cat exits on EOF").success());
    }

    /// An unreaped child is never reported as a clean teardown: it is
    /// counted and handed to a detached blocking reaper that collects it as
    /// soon as the kernel lets go -- here, once the test finally kills it.
    #[cfg(target_os = "linux")]
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn unreaped_child_is_not_a_clean_teardown_and_gets_a_detached_reaper() {
        let metrics = Metrics::new();
        let id = Uuid::new_v4();

        let mut child = spawn_sleeper(true);
        let pid = child.as_inner_mut().id();
        let clean = settle_reap(id, Ok(None), child, Duration::from_millis(1), &metrics);
        assert!(!clean, "a still-running child must not be reported clean");
        assert!(
            std::path::Path::new(&format!("/proc/{pid}")).exists(),
            "child still alive while the kernel has not released it"
        );
        kill_pid(pid);
        wait_until_reaped_by_kernel(pid).await;

        let mut child = spawn_sleeper(true);
        let pid = child.as_inner_mut().id();
        let clean = settle_reap(
            id,
            Err(std::io::Error::other("waitpid exploded")),
            child,
            Duration::from_millis(1),
            &metrics,
        );
        assert!(!clean, "a wait error must not be reported clean");
        kill_pid(pid);
        wait_until_reaped_by_kernel(pid).await;
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn settle_reap_reports_clean_only_for_a_collected_exit_status() {
        let metrics = Metrics::new();
        let mut child = spawn_sleeper(true);
        child.kill().expect("kill");
        let status = child.wait().expect("wait");
        // Fresh (already-waited) child object stands in; settle_reap only
        // inspects the outcome it is handed on the `Some(status)` path.
        let clean = settle_reap(
            Uuid::new_v4(),
            Ok(Some(status)),
            child,
            Duration::from_secs(1),
            &metrics,
        );
        assert!(clean);
    }

    #[test]
    fn stop_deadline_covers_both_graces_and_three_bounded_post_kill_steps() {
        let config = SupervisorConfig {
            stop_grace_timeout: Duration::from_secs(1),
            term_grace_timeout: Duration::from_secs(2),
            reap_timeout: Duration::from_secs(3),
            ..SupervisorConfig::default()
        };
        assert_eq!(config.stop_deadline(), Duration::from_secs(1 + 2 + 9));
        assert_eq!(
            SupervisorConfig::default().stop_deadline(),
            Duration::from_secs(3 + 3 + 15)
        );
    }
}
