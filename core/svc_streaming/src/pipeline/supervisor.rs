//! `ffmpeg` process supervision -- owned by S3. Implements the
//! [`PipelineEngine`] trait other modules (`http`/`api`, `ingest`,
//! `egress`) build against, wrapping `ffmpeg-sidecar` to spawn/monitor the
//! process [`crate::pipeline::ffmpeg::build_argv`] describes for one
//! [`PipelineSpec`]. See
//! `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md` §6 for the
//! supervision/observability contract this module implements.
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
use std::sync::{Arc, Mutex as StdMutex};
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
use crate::redact::scrub_diagnostic;
use crate::store::{SecretRef, SecretResolver};
use crate::telemetry::stream::{Stage, StreamMetrics};

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
    /// Grace period after `q\n` before escalating to SIGTERM.
    pub stop_grace_timeout: Duration,
    /// Grace period after SIGTERM before escalating to SIGKILL.
    pub term_grace_timeout: Duration,
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
/// The supervisor's own graceful-stop sequence (`q\n`) shares this exact
/// handle with listeners rather than competing for a second one.
#[derive(Clone)]
pub struct StdinHandle {
    slot: Arc<Slot>,
}

impl StdinHandle {
    /// Writes `bytes` to the pipeline's ffmpeg stdin. Blocking under the
    /// hood (`std::process::ChildStdin`) -- callers on an async ingest
    /// path should wrap sustained writes in `tokio::task::spawn_blocking`.
    pub fn write(&self, bytes: &[u8]) -> Result<(), PipelineError> {
        let mut guard = self.slot.stdin.lock().unwrap();
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
    /// Shared stream data-plane instruments: the ffmpeg lifecycle stages
    /// (`ffmpeg_spawn`, `ffmpeg_first_progress`, `ffmpeg_stop`) land in the
    /// same `stream_stage_duration_seconds{stage}` histogram as the
    /// orchestrator's setup stages, so one panel shows the whole pipeline
    /// timeline.
    stream: StreamMetrics,
}

impl Metrics {
    fn new() -> Self {
        let meter = global::meter("svc_streaming_pipeline");
        Self {
            stream: StreamMetrics::shared(),
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
            if let Some(stdout) = slot.stdout.lock().unwrap().take() {
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
        let progress = slot.state.lock().unwrap().progress.clone();
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
            slot.state.lock().unwrap().progress.state = PipelineState::Stopped;
            return Ok(());
        }

        slot.stopping.store(true, Ordering::SeqCst);
        slot.stop_notify.notify_one();

        let handle = slot.monitor.lock().await.take();
        if let Some(handle) = handle {
            // The monitor task performs the full q\n -> SIGTERM -> SIGKILL
            // sequence itself (it owns the child) and decrements
            // active_pipelines on the way out; bound the wait so a truly
            // stuck task can't hang stop() forever.
            let overall_timeout = self.config.stop_grace_timeout
                + self.config.term_grace_timeout
                + Duration::from_secs(5);
            if tokio::time::timeout(overall_timeout, handle).await.is_err() {
                tracing::warn!(pipeline_id = %id, "monitor task did not finish within the stop timeout");
            }
        }

        self.registry.write().await.remove(&id);
        Ok(())
    }

    async fn status(&self, id: PipelineId) -> Result<PipelineStatus, PipelineError> {
        let slot = self.get_slot(id).await?;
        let progress = slot.state.lock().unwrap().progress.clone();
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
        // supervisor never observes EOF. See `terminate_process_group`.
        #[cfg(unix)]
        std::os::unix::process::CommandExt::process_group(cmd.as_inner_mut(), 0);
        let spawn_syscall_start = Instant::now();
        let spawn_result = attempt_span.in_scope(|| cmd.spawn());
        metrics
            .stream
            .record_stage(Stage::FfmpegSpawn, spawn_syscall_start.elapsed());
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
                slot.state.lock().unwrap().progress.restarts = restarts;
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
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(config.backoff_max);
                continue 'outer;
            }
        };

        // Take stdout FIRST when a PCM sink is needed, so `.iter()` (which
        // opportunistically takes stdout too, for OutputFrame/OutputChunk
        // parsing) sees it already gone and leaves it alone.
        if needs_stdout {
            if let Some(stdout) = child.take_stdout() {
                *slot.stdout.lock().unwrap() = Some(stdout);
            }
        }
        if let Some(stdin) = child.take_stdin() {
            *slot.stdin.lock().unwrap() = Some(stdin);
        } else {
            tracing::warn!(pipeline_id = %id, "ffmpeg child has no stdin channel");
        }

        let (tx, mut rx) = mpsc::channel::<MonitorEvent>(64);
        let iter_thread = match child.iter() {
            Ok(iter) => {
                let tx2 = tx.clone();
                Some(std::thread::spawn(move || {
                    for event in iter {
                        // ffmpeg's diagnostics echo the resolved secret URLs
                        // it was handed (`Error opening output
                        // rtmp://host/app/<stream key>`), so every error line
                        // is scrubbed before it can reach a log line or the
                        // pipeline's `last_error` (exposed via the status
                        // API).
                        let forwarded = match event {
                            FfmpegEvent::Progress(p) => {
                                tx2.blocking_send(MonitorEvent::Progress(p))
                            }
                            FfmpegEvent::Error(e) => {
                                tx2.blocking_send(MonitorEvent::Error(scrub_diagnostic(&e)))
                            }
                            FfmpegEvent::Log(LogLevel::Error, msg)
                            | FfmpegEvent::Log(LogLevel::Fatal, msg) => {
                                tx2.blocking_send(MonitorEvent::Error(scrub_diagnostic(&msg)))
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
                        let stop_started = Instant::now();
                        graceful_stop(id, &mut child, &mut rx, &slot, &config).await;
                        metrics
                            .stream
                            .record_stage(Stage::FfmpegStop, stop_started.elapsed());
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
                                metrics
                                    .stream
                                    .record_stage(Stage::FfmpegFirstProgress, spawn_start.elapsed());
                                got_first_progress = true;
                            }
                            metrics.encode_speed.record(p.speed as f64, &pid_kv);
                            metrics.output_bitrate_kbps.record(p.bitrate_kbps as f64, &pid_kv);
                            let mut s = slot.state.lock().unwrap();
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
                    terminate_process_group(&mut child, "KILL");
                    break 'attempt;
                }
            }
        }

        if let Some(t) = iter_thread {
            let _ = t.join();
        }
        let _ = child.wait();

        if slot.stopping.load(Ordering::SeqCst) {
            set_state(&slot, PipelineState::Stopped, None);
            break 'outer;
        }

        restarts += 1;
        slot.state.lock().unwrap().progress.restarts = restarts;
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
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(config.backoff_max);
    }

    let prev = active_count.fetch_sub(1, Ordering::SeqCst);
    metrics.active_pipelines.record((prev - 1).max(0), &[]);
}

fn set_state(slot: &Slot, state: PipelineState, error: Option<String>) {
    let mut s = slot.state.lock().unwrap();
    s.progress.state = state;
    if let Some(err) = error {
        s.progress.last_error = Some(err);
    }
}

/// Graceful stop sequence (spec §6): `q\n` on stdin -> grace period ->
/// SIGTERM -> grace period -> SIGKILL. Reuses the SAME `rx` channel the
/// caller's monitor loop already reads from (rather than a second signal)
/// to learn when the process has actually exited.
async fn graceful_stop(
    id: PipelineId,
    child: &mut ffmpeg_sidecar::child::FfmpegChild,
    rx: &mut mpsc::Receiver<MonitorEvent>,
    slot: &Slot,
    config: &SupervisorConfig,
) {
    set_state(slot, PipelineState::Stopping, None);
    {
        let mut guard = slot.stdin.lock().unwrap();
        if let Some(stdin) = guard.as_mut() {
            let _ = stdin.write_all(b"q\n");
            let _ = stdin.flush();
        }
    }
    if wait_for_stream_end(rx, config.stop_grace_timeout).await {
        return;
    }
    tracing::warn!(pipeline_id = %id, "graceful stop grace period elapsed, sending SIGTERM");
    terminate_process_group(child, "TERM");
    if wait_for_stream_end(rx, config.term_grace_timeout).await {
        return;
    }
    tracing::warn!(pipeline_id = %id, "SIGTERM grace period elapsed, sending SIGKILL");
    terminate_process_group(child, "KILL");
    let _ = wait_for_stream_end(rx, Duration::from_secs(5)).await;
}

async fn wait_for_stream_end(rx: &mut mpsc::Receiver<MonitorEvent>, timeout: Duration) -> bool {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return false;
        }
        match tokio::time::timeout(remaining, rx.recv()).await {
            Ok(Some(MonitorEvent::StreamEnded)) | Ok(None) => return true,
            Ok(Some(_)) => continue,
            Err(_) => return false,
        }
    }
}

/// Signals `child`'s entire process **group**, not just the directly
/// tracked PID. `run_pipeline` spawns every ffmpeg child into its own
/// fresh process group (`process_group(0)`, PGID == the child's PID)
/// specifically so this can target `-PGID` -- if ffmpeg (or, in testing,
/// a multi-process fake binary) forks helper processes, a plain
/// `child.kill()` only reaches the direct child, leaving descendants to
/// hold the stdout/stderr pipes open as orphans and the supervisor never
/// observing EOF.
///
/// No `nix`/`libc` dependency declared for this crate -- shelling out to
/// the `kill` utility (present in the `debian:bookworm-slim` runtime
/// image) avoids adding one just for signal delivery. Production targets
/// are Linux containers only (see `client.md` Platform Targets -- this is
/// a backend service, not a cross-platform client).
#[cfg(unix)]
fn terminate_process_group(child: &mut ffmpeg_sidecar::child::FfmpegChild, signal: &str) {
    let pid = child.as_inner().id();
    // `--` is mandatory here: without it, `kill` parses the negative-PID
    // (process-group) argument `-<pid>` as an unrecognized option instead
    // of a target, silently signalling nothing while still exiting 0.
    let _ = std::process::Command::new("kill")
        .args([format!("-{signal}"), "--".to_string(), format!("-{pid}")])
        .status();
}

#[cfg(not(unix))]
fn terminate_process_group(child: &mut ffmpeg_sidecar::child::FfmpegChild, _signal: &str) {
    // No portable process-group signalling outside unix -- fall back to
    // killing just the tracked child.
    let _ = child.kill();
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
}
