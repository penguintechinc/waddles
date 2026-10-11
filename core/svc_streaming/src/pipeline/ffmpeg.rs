//! Pure `ffmpeg` argv construction from a [`PipelineSpec`] -- owned by S3.
//! See `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md` for the
//! recipe tables this module implements (§2 inputs, §3 codecs, §4 outputs,
//! §7 golden cases).
//!
//! [`build_argv`] is deliberately a pure function of `(spec, paths)`: it
//! performs no I/O (no env reads, no filesystem access) so it stays cheap
//! to golden-test. Callers (`pipeline::supervisor`) are responsible for
//! resolving `SecretRef`s via [`crate::store::SecretResolver`] and
//! populating [`Paths::resolved_secrets`] before calling in.
//!
//! # Codecs, encoders and fan-out
//!
//! - **Profile binding.** [`OutputSpec::RtmpPush`]/[`OutputSpec::SrtPush`]
//!   carry an optional `profile`; `None` keeps the legacy binding to
//!   `spec.profiles[0]` (see [`output_profile_name`]). That is what lets an
//!   H.265 HLS output and an H.264 RTMP push share one pipeline.
//! - **Container compatibility is validated here, loudly.** `ffmpeg -f tee`
//!   skips a slave whose header write fails (even with the default
//!   `onfail=abort`; verified on the runtime image's ffmpeg 5.1) and still
//!   exits 0, so an HEVC/AV1 stream aimed at FLV would be dropped without
//!   a trace. [`build_argv`] therefore refuses such a spec up front via
//!   [`crate::pipeline::codec`].
//! - **GPU-preferred, CPU-fallback.** The encoder for each codec family comes
//!   from [`Paths::encoders`] (probed once at startup by
//!   [`crate::pipeline::encoder::detect_encoders`]); the default is all
//!   software, so a pure `build_argv` call is a CPU command that works
//!   everywhere.
//! - **Fan-out cap.** [`Paths::relay_policy`] bounds the number of
//!   `RtmpPush`/`SrtPush` destinations in the argv itself.

use std::collections::HashMap;
use std::path::PathBuf;

use crate::egress::hls::HlsOutputTarget;
use crate::egress::relay::RelayPolicy;
use crate::pipeline::codec::{self, AudioChoice, OutputFormat, VideoFamily};
use crate::pipeline::encoder::{
    hardware_video_args, vaapi_global_args, EncoderBackend, EncoderSelection, VAAPI_UPLOAD_FILTER,
};
use crate::pipeline::model::{
    AudioCodec, InputSpec, ObjectStoreRef, OutputSpec, PipelineError, PipelineId, PipelineSpec,
    TranscodeProfile, VideoCodec,
};
use crate::store::SecretRef;

/// Runtime path/secret inputs [`build_argv`] needs to turn a
/// [`PipelineSpec`] into a concrete `ffmpeg` argv. Resolving `SecretRef`s
/// (env/file reads) is deliberately kept outside `build_argv` so the argv
/// builder stays a pure, easily golden-tested function -- callers (the
/// supervisor) resolve secrets via [`crate::store::SecretResolver`] once at
/// spawn time and populate this struct.
#[derive(Debug, Clone, Default)]
pub struct Paths {
    /// Local filesystem root for recordings/segments/HLS output
    /// (`STREAM_DATA_DIR`).
    pub stream_data_dir: PathBuf,
    /// Resolved values for every `SecretRef`-backed output URL in the
    /// spec, keyed by [`secret_ref_key`]. A missing entry for a
    /// `SecretRef` actually referenced by the spec is
    /// `PipelineError::InvalidSpec`, not a panic.
    pub resolved_secrets: HashMap<String, String>,
    /// SDP file path for each `Whip` input, keyed by the input's index in
    /// `spec.inputs`. Written by `ingest::whip` (S6) before the pipeline is
    /// spawned; MVP only ever has a single input (index 0).
    pub whip_sdp_paths: HashMap<usize, PathBuf>,
    /// First local UDP port used for `ffmpeg` <-> `webrtc-rs` RTP handoff
    /// legs (WHIP input / WHEP output). Each leg claims one port,
    /// allocated in `spec.inputs` then `spec.outputs` order -- see
    /// [`rtp_legs`].
    pub rtp_base_port: u16,
    /// Encoder backend per codec family, chosen once at startup (GPU when a
    /// trial encode passed, else CPU). [`Default`] is all-software.
    pub encoders: EncoderSelection,
    /// Destination cap enforced on the `RtmpPush`/`SrtPush` outputs of the
    /// spec. `None` means unbounded -- only for pure unit use; the
    /// supervisor always supplies one.
    pub relay_policy: Option<RelayPolicy>,
}

/// Deterministic map key for a [`SecretRef`], used to look up its resolved
/// value in [`Paths::resolved_secrets`]. Exposed so callers populate the
/// map with matching keys without needing `SecretRef` to implement `Hash`
/// (it doesn't -- `store::secrets` is owned by a different chunk).
pub fn secret_ref_key(secret_ref: &SecretRef) -> String {
    match secret_ref {
        SecretRef::Env { var } => format!("env:{var}"),
        SecretRef::File { path } => format!("file:{path}"),
    }
}

/// Which side of an RTP handoff a [`RtpLeg`] describes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RtpLegDirection {
    /// `webrtc-rs` depacketizes an inbound WHIP `PeerConnection` to this
    /// local UDP port; `ffmpeg` reads it (or, for a pure-copy leg, no
    /// `ffmpeg` process exists at all and `webrtc-rs` forwards RTP
    /// directly to the WHEP leg).
    FromWhip,
    /// `ffmpeg` writes `-f rtp` to this local UDP port; `webrtc-rs`
    /// forwards it SFU-style to N WHEP `PeerConnection`s.
    ToWhep,
}

/// Describes one `ffmpeg`<->`webrtc-rs` RTP handoff point. WHIP/WHEP legs
/// never appear inside `-f tee`'s slave list (tee only muxes byte-stream
/// URLs) -- they are surfaced here instead, for S6 (`ingest::whip` /
/// `egress::whep`) to bind/forward.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RtpLeg {
    pub direction: RtpLegDirection,
    pub local_port: u16,
    /// The transcode profile this leg's `ffmpeg` output block uses, if
    /// any. `None` for a pure-copy `FromWhip` leg with no `ffmpeg`
    /// process (see [`PipelineError::NoFfmpegNeeded`]).
    pub profile: Option<String>,
}

/// Enumerates every WHIP/WHEP RTP handoff leg `spec` needs, in the same
/// `spec.inputs` then `spec.outputs` order [`build_argv`] allocates ports
/// in -- callers must use this (not their own port arithmetic) to stay in
/// sync with the ports embedded in the argv `build_argv` produces for the
/// same `(spec, paths)`.
pub fn rtp_legs(spec: &PipelineSpec, paths: &Paths) -> Vec<RtpLeg> {
    let mut legs = Vec::new();
    let mut port = paths.rtp_base_port;
    for input in &spec.inputs {
        if let InputSpec::Whip { .. } = input {
            legs.push(RtpLeg {
                direction: RtpLegDirection::FromWhip,
                local_port: port,
                profile: None,
            });
            port = port.saturating_add(1);
        }
    }
    for output in &spec.outputs {
        if let OutputSpec::Whep { profile } = output {
            legs.push(RtpLeg {
                direction: RtpLegDirection::ToWhep,
                local_port: port,
                profile: Some(profile.clone()),
            });
            port = port.saturating_add(1);
        }
    }
    legs
}

/// Builds the full `ffmpeg` argv for `spec`, or a [`PipelineError`]
/// signalling why one can't be built:
/// - [`PipelineError::NoFfmpegNeeded`] -- a pure-copy WHIP->WHEP leg (spec
///   §1, §7 case 6); the caller should skip spawning entirely.
/// - [`PipelineError::Unsupported`] -- a well-formed but P2 transform
///   (multi-input compositing, spec §7 case 10).
/// - [`PipelineError::InvalidSpec`] -- malformed input (no inputs/outputs,
///   an output referencing an unknown profile, an unresolved secret ref).
pub fn build_argv(spec: &PipelineSpec, paths: &Paths) -> Result<Vec<String>, PipelineError> {
    if spec.inputs.is_empty() {
        return Err(PipelineError::InvalidSpec("pipeline has no inputs".into()));
    }
    if spec.inputs.len() > 1 {
        // Spec §2: "MVP = separate pipelines, one per source" -- picture-
        // in-picture / audio-mix compositing (golden case #10) is P2.
        return Err(PipelineError::Unsupported(
            "multi-input compositing (picture-in-picture / audio mix) is P2 -- MVP treats each input as its own pipeline",
        ));
    }
    if spec.outputs.is_empty() {
        return Err(PipelineError::InvalidSpec("pipeline has no outputs".into()));
    }
    enforce_push_cap(spec, paths)?;

    let input = &spec.inputs[0];

    if is_pure_whip_to_whep_copy(spec, input)? {
        return Err(PipelineError::NoFfmpegNeeded);
    }

    let discord_outputs: Vec<&OutputSpec> = spec
        .outputs
        .iter()
        .filter(|o| matches!(o, OutputSpec::DiscordVoice { .. }))
        .collect();
    if discord_outputs.len() > 1 {
        // ffmpeg has exactly one stdout pipe; two DiscordVoice sinks can't
        // both write `pipe:1`.
        return Err(PipelineError::InvalidSpec(
            "at most one DiscordVoice output is supported per pipeline (single stdout pipe)".into(),
        ));
    }

    let tee_eligible: Vec<&OutputSpec> = spec
        .outputs
        .iter()
        .filter(|o| {
            matches!(
                o,
                OutputSpec::RtmpPush { .. }
                    | OutputSpec::SrtPush { .. }
                    | OutputSpec::Hls { .. }
                    | OutputSpec::Record { .. }
            )
        })
        .collect();
    let whep_outputs: Vec<&OutputSpec> = spec
        .outputs
        .iter()
        .filter(|o| matches!(o, OutputSpec::Whep { .. }))
        .collect();

    let resolved_tee = resolve_profiles(spec, &tee_eligible)?;
    let resolved_whep = resolve_profiles(spec, &whep_outputs)?;

    if resolved_tee.is_empty() && resolved_whep.is_empty() && discord_outputs.is_empty() {
        return Err(PipelineError::InvalidSpec(
            "pipeline has no usable outputs".into(),
        ));
    }

    // Distinct non-copy profiles referenced anywhere (first-appearance
    // order) decide whether a shared-decode `-filter_complex split` ladder
    // is needed -- spec §1: "only branches that need decode+re-encode go
    // through split". A single non-copy profile never needs to share a
    // decode with anything, so it skips the filter graph (spec §3 ladder
    // note, golden cases #2/#3/#5 vs #7).
    let mut ladder: Vec<&TranscodeProfile> = Vec::new();
    for r in resolved_tee.iter().chain(resolved_whep.iter()) {
        if !is_full_copy(r.profile) && !ladder.iter().any(|p| p.name == r.profile.name) {
            ladder.push(r.profile);
        }
    }
    let use_filter_complex = ladder.len() >= 2;
    let labels = ladder_labels(&ladder);

    // `-nostdin` is a *global* option placed first on purpose. ffmpeg-sidecar's
    // `spawn()` appends a trailing `-n` ("never overwrite", after the output
    // path -- where ffmpeg only warns "Trailing option(s) found" and ignores
    // it) unless one of `-y`/`-n`/`-nostdin` is already present. It is also
    // the honest statement of how this process is driven: stdin is either
    // the `pipe:0` media input or unused, never an interactive console, and
    // the supervisor ends ffmpeg by EOF/signal, not by keypress.
    let mut argv: Vec<String> = vec!["-nostdin".into()];
    // A VA-API encode needs the render node registered once, globally,
    // before the inputs.
    if ladder
        .iter()
        .any(|p| encoder_backend(p, paths) == EncoderBackend::Vaapi)
    {
        let device = paths.encoders.vaapi_device().ok_or_else(|| {
            PipelineError::InvalidSpec(
                "a VA-API encoder is selected but no render node is configured".into(),
            )
        })?;
        argv.extend(vaapi_global_args(device));
    }
    argv.extend(input_args(input, paths)?);

    if use_filter_complex {
        argv.push("-filter_complex".into());
        argv.push(build_filter_complex(&ladder, &labels, paths)?);
    }

    // Group tee-eligible outputs by profile name (stable, first-appearance
    // order) -- outputs sharing a profile fan out from one encode via
    // `-f tee`; a lone output in its group maps/muxes directly.
    let mut groups: Vec<(String, Vec<&Resolved>)> = Vec::new();
    for r in &resolved_tee {
        match groups.iter_mut().find(|(name, _)| *name == r.profile.name) {
            Some((_, members)) => members.push(r),
            None => groups.push((r.profile.name.clone(), vec![r])),
        }
    }

    // A group made only of external pushes must not be able to end the
    // ffmpeg process while the pipeline still serves local outputs (HLS,
    // recording, WHEP, Discord): with `onfail=ignore` a dead destination is
    // dropped from its tee, but a tee whose slaves have *all* failed aborts
    // the whole process ("All tee outputs failed", verified on ffmpeg 5.1).
    // Such a group is therefore always muxed through `tee`, plus a `null`
    // guard slave that can never fail. A pipeline that is pushes only gets
    // no guard: when every destination is dead there is nothing left to
    // serve, and the process exiting (supervisor restart/Failed) is the
    // honest signal rather than a silently idle pipeline.
    let has_local_outputs = spec
        .outputs
        .iter()
        .any(|o| !matches!(o, OutputSpec::RtmpPush { .. } | OutputSpec::SrtPush { .. }));

    for (_, members) in &groups {
        let profile = members[0].profile;
        let grouped = members.len() > 1;
        for m in members {
            validate_output_codecs(m, grouped)?;
        }
        push_map_and_codec(&mut argv, profile, use_filter_complex, &labels, paths);
        let isolate_pushes = has_local_outputs
            && members.iter().all(|m| {
                matches!(
                    m.output,
                    OutputSpec::RtmpPush { .. } | OutputSpec::SrtPush { .. }
                )
            });
        if !grouped && !isolate_pushes {
            argv.extend(solo_muxer_args(members[0].output, spec.id, paths)?);
        } else {
            let mut slaves = Vec::with_capacity(members.len() + 1);
            for m in members {
                slaves.push(tee_slave(m.output, spec.id, paths)?);
            }
            if isolate_pushes {
                slaves.push(TEE_GUARD_SLAVE.to_string());
            }
            argv.push("-f".into());
            argv.push("tee".into());
            argv.push(slaves.join("|"));
        }
    }

    // WHIP/WHEP legs never appear inside `-f tee` (spec §1) -- each gets
    // its own `-f rtp` output block, at the same local UDP port
    // `rtp_legs()` reports to S6.
    let whep_ports: Vec<u16> = rtp_legs(spec, paths)
        .into_iter()
        .filter(|leg| leg.direction == RtpLegDirection::ToWhep)
        .map(|leg| leg.local_port)
        .collect();
    for (r, port) in resolved_whep.iter().zip(whep_ports.iter()) {
        push_map_and_codec(&mut argv, r.profile, use_filter_complex, &labels, paths);
        argv.push("-f".into());
        argv.push("rtp".into());
        argv.push(format!("udp://127.0.0.1:{port}"));
    }

    if !discord_outputs.is_empty() {
        argv.push("-map".into());
        argv.push("0:a".into());
        argv.push("-vn".into());
        argv.push("-c:a".into());
        argv.push("pcm_s16le".into());
        argv.push("-ar".into());
        argv.push("48000".into());
        argv.push("-ac".into());
        argv.push("2".into());
        argv.push("-f".into());
        argv.push("s16le".into());
        argv.push("pipe:1".into());
    }

    Ok(argv)
}

/// Rejects a spec with more `RtmpPush`/`SrtPush` destinations than
/// [`Paths::relay_policy`] allows. Enforced here, in the argv path, so the
/// cap holds for every caller -- the `RelaySink` policy alone is advisory
/// (the orchestrator used to log its refusal and let ffmpeg forward to the
/// extra destinations anyway).
fn enforce_push_cap(spec: &PipelineSpec, paths: &Paths) -> Result<(), PipelineError> {
    let Some(policy) = paths.relay_policy else {
        return Ok(());
    };
    let pushes = spec
        .outputs
        .iter()
        .filter(|o| matches!(o, OutputSpec::RtmpPush { .. } | OutputSpec::SrtPush { .. }))
        .count();
    policy.check_destination_count(pushes).map_err(|err| {
        tracing::warn!(
            pipeline_id = %spec.id,
            destinations = pushes,
            max = policy.max_destinations,
            "pipeline refused: too many relay destinations"
        );
        PipelineError::InvalidSpec(err.to_string())
    })
}

/// The container `output` muxes into. A `Record` output is MPEG-TS
/// segments on its own but a single MP4 inside a `tee` group (see
/// [`solo_muxer_args`] vs [`tee_slave`]).
fn output_format(output: &OutputSpec, in_tee: bool) -> Option<OutputFormat> {
    match output {
        OutputSpec::RtmpPush { .. } => Some(OutputFormat::Flv),
        OutputSpec::SrtPush { .. } => Some(OutputFormat::MpegTs),
        OutputSpec::Hls { .. } => Some(OutputFormat::HlsFmp4),
        OutputSpec::Record { .. } if in_tee => Some(OutputFormat::Mp4),
        OutputSpec::Record { .. } => Some(OutputFormat::MpegTs),
        OutputSpec::Whep { .. } | OutputSpec::DiscordVoice { .. } => None,
    }
}

/// Refuses an output whose profile carries a codec its container cannot
/// hold. Passthrough (`Copy`) streams are accepted: their real codec is
/// only known at runtime.
fn validate_output_codecs(r: &Resolved, in_tee: bool) -> Result<(), PipelineError> {
    let Some(format) = output_format(r.output, in_tee) else {
        return Ok(());
    };
    codec::check_pair(
        format,
        VideoFamily::of(&r.profile.video),
        AudioChoice::of(&r.profile.audio),
    )
    .map_err(|err| {
        tracing::warn!(
            profile = %r.profile.name,
            error = %err,
            "pipeline refused: codec cannot be muxed into the requested output"
        );
        PipelineError::InvalidSpec(format!("profile {:?}: {err}", r.profile.name))
    })
}

/// The encoder backend `profile`'s video is encoded with. Passthrough and
/// non-video-codec profiles report [`EncoderBackend::Software`] (they run
/// no hardware encode).
fn encoder_backend(profile: &TranscodeProfile, paths: &Paths) -> EncoderBackend {
    VideoFamily::of(&profile.video)
        .map(|family| paths.encoders.backend_for(family))
        .unwrap_or(EncoderBackend::Software)
}

/// One encode a pipeline will run: which profile, which codec family, and
/// on which backend. Surfaced for logs/metrics at pipeline start.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EncodeInfo {
    pub profile: String,
    pub family: VideoFamily,
    pub backend: EncoderBackend,
}

/// Lists the video encodes `spec` will run under `paths` (one per distinct
/// non-passthrough profile an output actually references). Pure.
pub fn encode_summary(spec: &PipelineSpec, paths: &Paths) -> Vec<EncodeInfo> {
    let mut seen: Vec<&str> = Vec::new();
    let mut out = Vec::new();
    for output in &spec.outputs {
        // DiscordVoice (audio-only PCM) has no profile; an unresolvable
        // profile is reported by `build_argv`, not here.
        let Ok(name) = output_profile_name(output, spec) else {
            continue;
        };
        let Some(profile) = spec.profiles.iter().find(|p| p.name == name) else {
            continue;
        };
        if seen.contains(&profile.name.as_str()) {
            continue;
        }
        seen.push(profile.name.as_str());
        if let Some(family) = VideoFamily::of(&profile.video) {
            out.push(EncodeInfo {
                profile: profile.name.clone(),
                family,
                backend: paths.encoders.backend_for(family),
            });
        }
    }
    out
}

/// A tee slave that discards everything and can never fail. Appended to a
/// push-only tee group so the group always has a live slave -- see the
/// comment in [`build_argv`].
const TEE_GUARD_SLAVE: &str = "[f=null]-";

struct Resolved<'a> {
    output: &'a OutputSpec,
    profile: &'a TranscodeProfile,
}

fn resolve_profiles<'a>(
    spec: &'a PipelineSpec,
    outputs: &[&'a OutputSpec],
) -> Result<Vec<Resolved<'a>>, PipelineError> {
    outputs
        .iter()
        .map(|&output| {
            let name = output_profile_name(output, spec)?;
            let profile = find_profile(spec, &name)?;
            Ok(Resolved { output, profile })
        })
        .collect()
}

/// Resolves the [`TranscodeProfile`] name an output applies. A push
/// output without an explicit `profile` binds to `spec.profiles[0]`.
fn output_profile_name(output: &OutputSpec, spec: &PipelineSpec) -> Result<String, PipelineError> {
    match output {
        OutputSpec::RtmpPush { profile, .. } | OutputSpec::SrtPush { profile, .. } => match profile
        {
            Some(name) => Ok(name.clone()),
            None => spec
                .profiles
                .first()
                .map(|p| p.name.clone())
                .ok_or_else(|| {
                    PipelineError::InvalidSpec("pipeline has no transcode profiles".into())
                }),
        },
        OutputSpec::Hls { profile, .. }
        | OutputSpec::Whep { profile }
        | OutputSpec::Record { profile, .. } => Ok(profile.clone()),
        OutputSpec::DiscordVoice { .. } => Err(PipelineError::InvalidSpec(
            "a DiscordVoice output is audio-only and has no transcode profile".into(),
        )),
    }
}

fn find_profile<'a>(
    spec: &'a PipelineSpec,
    name: &str,
) -> Result<&'a TranscodeProfile, PipelineError> {
    spec.profiles
        .iter()
        .find(|p| p.name == name)
        .ok_or_else(|| {
            PipelineError::InvalidSpec(format!("output references unknown profile {name:?}"))
        })
}

fn is_full_copy(profile: &TranscodeProfile) -> bool {
    matches!(profile.video, VideoCodec::Copy) && matches!(profile.audio, AudioCodec::Copy)
}

/// A pure-copy WHIP input feeding only Whep outputs whose profiles are
/// also full-copy needs no `ffmpeg` process at all (spec §1, §7 case 6) --
/// `webrtc-rs` forwards RTP directly, SFU-style.
fn is_pure_whip_to_whep_copy(
    spec: &PipelineSpec,
    input: &InputSpec,
) -> Result<bool, PipelineError> {
    if !matches!(input, InputSpec::Whip { .. }) {
        return Ok(false);
    }
    for output in &spec.outputs {
        match output {
            OutputSpec::Whep { profile } => {
                if !is_full_copy(find_profile(spec, profile)?) {
                    return Ok(false);
                }
            }
            _ => return Ok(false),
        }
    }
    Ok(true)
}

fn input_args(input: &InputSpec, paths: &Paths) -> Result<Vec<String>, PipelineError> {
    match input {
        InputSpec::Rtmp { .. } => Ok(vec![
            "-f".into(),
            "flv".into(),
            "-i".into(),
            "pipe:0".into(),
        ]),
        InputSpec::Srt { .. } => Ok(vec![
            "-f".into(),
            "mpegts".into(),
            "-i".into(),
            "pipe:0".into(),
        ]),
        InputSpec::Whip { .. } => {
            let sdp = paths.whip_sdp_paths.get(&0).ok_or_else(|| {
                PipelineError::InvalidSpec(
                    "Whip input requires an SDP file path in Paths::whip_sdp_paths (written by ingest::whip before spawn)"
                        .into(),
                )
            })?;
            Ok(vec![
                "-protocol_whitelist".into(),
                "file,rtp,udp".into(),
                "-i".into(),
                sdp.to_string_lossy().into_owned(),
            ])
        }
        InputSpec::Pull { url } => {
            // `-reconnect*` are input-scoped AVOptions and must precede
            // `-i` to bind to it (spec §2 shows them after `-i`, which is
            // shorthand, not a literal, functionally-correct ffmpeg
            // command -- see module docs). HTTP(S)-only per spec note;
            // `rtmp://` pull sources rely on supervisor restart-on-exit.
            let mut a = Vec::new();
            if url.starts_with("http://") || url.starts_with("https://") {
                a.extend([
                    "-reconnect".into(),
                    "1".into(),
                    "-reconnect_streamed".into(),
                    "1".into(),
                    "-reconnect_delay_max".into(),
                    "5".into(),
                ]);
            }
            a.push("-i".into());
            a.push(url.clone());
            Ok(a)
        }
    }
}

/// Resolution-derived filter-graph label for a ladder rung -- `v<height>`
/// when it has a resolution, `v_<name>` otherwise.
fn video_label(profile: &TranscodeProfile) -> String {
    match profile.resolution {
        Some((_, h)) => format!("v{h}"),
        None => format!("v_{}", sanitize(&profile.name)),
    }
}

fn sanitize(name: &str) -> String {
    name.chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect()
}

/// Filter-graph labels for each ladder rung, keyed by profile name. Two
/// rungs that would share a label (same height, or names that sanitize
/// alike -- e.g. an H.265 and an H.264 profile at the same resolution) get
/// a `_<index>` suffix so the graph never declares one pad twice.
fn ladder_labels(ladder: &[&TranscodeProfile]) -> HashMap<String, String> {
    let mut used: Vec<String> = Vec::with_capacity(ladder.len());
    let mut labels = HashMap::with_capacity(ladder.len());
    for (index, profile) in ladder.iter().enumerate() {
        let mut label = video_label(profile);
        if used.contains(&label) {
            label = format!("{label}_{index}");
        }
        used.push(label.clone());
        labels.insert(profile.name.clone(), label);
    }
    labels
}

/// Builds `[0:v]split=N[label1][label2]...;[label1]<chain>[label1s];...`
/// per spec §1/§3/§7 case 7 -- only the non-copy rungs participate; a
/// `Copy` rung always maps `0:v` directly (never through the filter
/// graph). A rung's `<chain>` is `scale=W:H` when it has a resolution, the
/// VA-API upload when its encoder needs surfaces, both comma-joined, or the
/// no-op `null` filter when it has neither -- so several codecs can share
/// one decode at the source resolution without any rung needing a size.
fn build_filter_complex(
    ladder: &[&TranscodeProfile],
    labels: &HashMap<String, String>,
    paths: &Paths,
) -> Result<String, PipelineError> {
    let ordered: Vec<&String> = ladder
        .iter()
        .map(|p| {
            labels.get(&p.name).ok_or_else(|| {
                PipelineError::InvalidSpec(format!("profile {:?} has no ladder label", p.name))
            })
        })
        .collect::<Result<_, _>>()?;
    let split_targets: String = ordered.iter().map(|l| format!("[{l}]")).collect();
    let mut filter = format!("[0:v]split={}{}", ladder.len(), split_targets);
    for (profile, label) in ladder.iter().zip(ordered.iter()) {
        let mut chain: Vec<String> = Vec::new();
        if let Some((w, h)) = profile.resolution {
            chain.push(format!("scale={w}:{h}"));
        }
        if encoder_backend(profile, paths) == EncoderBackend::Vaapi {
            chain.push(VAAPI_UPLOAD_FILTER.to_string());
        }
        let chain = if chain.is_empty() {
            "null".to_string()
        } else {
            chain.join(",")
        };
        filter.push_str(&format!(";[{label}]{chain}[{label}s]"));
    }
    Ok(filter)
}

/// Pushes this group's `-map`/codec block. Shared between tee-eligible
/// groups and standalone WHEP output blocks -- both need identical
/// map-then-codec construction, differing only in their final muxer.
fn push_map_and_codec(
    argv: &mut Vec<String>,
    profile: &TranscodeProfile,
    use_filter_complex: bool,
    labels: &HashMap<String, String>,
    paths: &Paths,
) {
    let filtered_label = if use_filter_complex && !is_full_copy(profile) {
        labels.get(&profile.name).map(|label| format!("{label}s"))
    } else {
        None
    };

    match &filtered_label {
        Some(label) => {
            argv.push("-map".into());
            argv.push(format!("[{label}]"));
        }
        None => {
            argv.push("-map".into());
            argv.push("0:v".into());
        }
    }
    argv.push("-map".into());
    argv.push("0:a".into());

    if is_full_copy(profile) {
        if use_filter_complex {
            // Mixed command (some other branch needs a real encode) --
            // per-output explicit flags, matching spec §7 case 7's copy
            // branch (`-c:v copy -c:a copy`), not the `-c copy` shorthand.
            argv.push("-c:v".into());
            argv.push("copy".into());
            argv.push("-c:a".into());
            argv.push("copy".into());
        } else {
            argv.push("-c".into());
            argv.push("copy".into());
        }
    } else {
        let backend = encoder_backend(profile, paths);
        argv.extend(video_codec_args(
            &profile.video,
            profile.fps.unwrap_or(30),
            backend,
        ));
        if filtered_label.is_none() {
            // Not going through the filter graph -- scale/fps are plain
            // output options (spec §7 case 2). When filtered, scale is
            // already embedded in the `-filter_complex` chain.
            let mut chain: Vec<String> = Vec::new();
            if let Some((w, h)) = profile.resolution {
                chain.push(format!("scale={w}:{h}"));
            }
            if backend == EncoderBackend::Vaapi {
                chain.push(VAAPI_UPLOAD_FILTER.to_string());
            }
            if !chain.is_empty() {
                argv.push("-vf".into());
                argv.push(chain.join(","));
            }
            if let Some(fps) = profile.fps {
                argv.push("-r".into());
                argv.push(fps.to_string());
            }
        }
        argv.extend(audio_codec_args(&profile.audio));
    }
}

/// Rate-control, keyframe-interval and tuning knobs of an encode recipe.
fn video_tuning(codec: &VideoCodec) -> Option<(&str, Option<u8>, Option<u32>)> {
    match codec {
        VideoCodec::Copy => None,
        VideoCodec::H264 {
            preset,
            crf,
            bitrate_kbps,
        }
        | VideoCodec::H265 {
            preset,
            crf,
            bitrate_kbps,
        }
        | VideoCodec::Av1Svt {
            preset,
            crf,
            bitrate_kbps,
        } => Some((preset.as_str(), *crf, *bitrate_kbps)),
    }
}

/// Spec §3 video codec recipes. `2*fps` keyframe interval throughout. The
/// CPU recipes (`libx264`/`libx265`/`libsvtav1`) are the ones spec §3
/// defines and the guaranteed fallback; a GPU `backend` swaps in the
/// equivalent NVENC/VA-API recipe from [`hardware_video_args`].
fn video_codec_args(codec: &VideoCodec, fps: u32, backend: EncoderBackend) -> Vec<String> {
    if backend != EncoderBackend::Software {
        if let (Some(family), Some((preset, crf, bitrate_kbps))) =
            (VideoFamily::of(codec), video_tuning(codec))
        {
            return hardware_video_args(backend, family, preset, crf, bitrate_kbps, fps);
        }
    }
    match codec {
        VideoCodec::Copy => vec!["-c:v".into(), "copy".into()],
        VideoCodec::H264 {
            preset,
            crf,
            bitrate_kbps,
        } => {
            let mut a = vec![
                "-c:v".into(),
                "libx264".into(),
                "-preset".into(),
                preset.clone(),
                "-tune".into(),
                "zerolatency".into(),
                "-profile:v".into(),
                "high".into(),
                "-g".into(),
                (2 * fps).to_string(),
                "-sc_threshold".into(),
                "0".into(),
            ];
            if let Some(kbps) = bitrate_kbps {
                a.extend([
                    "-b:v".into(),
                    format!("{kbps}k"),
                    "-maxrate".into(),
                    format!("{kbps}k"),
                    "-bufsize".into(),
                    format!("{}k", kbps * 2),
                ]);
            }
            if let Some(crf) = crf {
                a.push("-crf".into());
                a.push(crf.to_string());
            }
            a
        }
        VideoCodec::H265 {
            preset,
            crf,
            bitrate_kbps,
        } => {
            let keyint = 2 * fps;
            let mut params = format!("keyint={keyint}:min-keyint={keyint}:scenecut=0");
            if let Some(crf) = crf {
                params.push_str(&format!(":crf={crf}"));
            }
            let mut a = vec![
                "-c:v".into(),
                "libx265".into(),
                "-preset".into(),
                preset.clone(),
                "-x265-params".into(),
                params,
            ];
            if let Some(kbps) = bitrate_kbps {
                a.extend([
                    "-b:v".into(),
                    format!("{kbps}k"),
                    "-maxrate".into(),
                    format!("{kbps}k"),
                    "-bufsize".into(),
                    format!("{}k", kbps * 2),
                ]);
            }
            // HLS fMP4 players (Safari/iOS) only accept `hvc1`; ffmpeg's
            // default `hev1` plays nowhere on Apple devices. Harmless for
            // the MPEG-TS and rtp muxers, which keep no such tag table.
            a.extend(["-tag:v".into(), "hvc1".into()]);
            a
        }
        VideoCodec::Av1Svt {
            preset,
            crf,
            bitrate_kbps,
        } => {
            let mut a = vec![
                "-c:v".into(),
                "libsvtav1".into(),
                "-preset".into(),
                preset.clone(),
                "-svtav1-params".into(),
                "tune=0:fast-decode=1".into(),
                "-g".into(),
                (2 * fps).to_string(),
            ];
            if let Some(kbps) = bitrate_kbps {
                a.extend(["-b:v".into(), format!("{kbps}k")]);
            }
            if let Some(crf) = crf {
                a.push("-crf".into());
                a.push(crf.to_string());
            }
            a
        }
    }
}

/// Spec §3 audio codec recipes. Opus always maps to `libopus` (the real
/// ffmpeg encoder name) -- spec §7 case 5's `-c:a opus` cell is shorthand.
fn audio_codec_args(codec: &AudioCodec) -> Vec<String> {
    match codec {
        AudioCodec::Copy => vec!["-c:a".into(), "copy".into()],
        AudioCodec::Aac { bitrate_kbps } => vec![
            "-c:a".into(),
            "aac".into(),
            "-b:a".into(),
            format!("{bitrate_kbps}k"),
            "-ar".into(),
            "48000".into(),
        ],
        AudioCodec::Opus { bitrate_kbps } => vec![
            "-c:a".into(),
            "libopus".into(),
            "-b:a".into(),
            format!("{bitrate_kbps}k"),
            "-ar".into(),
            "48000".into(),
            "-ac".into(),
            "2".into(),
        ],
    }
}

/// Matches `egress::record::RecordSink::segment_dir`'s layout exactly
/// (`{STREAM_DATA_DIR}/rec/{prefix}/{pipeline_id}`) -- S12 integration fix:
/// this previously used `records/{prefix}` (no `pipeline_id`, wrong root
/// directory name), which meant ffmpeg's `-f segment` muxer wrote `.ts`
/// files to a location `egress::record`'s upload watcher never scanned.
/// Only the solo (non-tee) muxer path below reuses this; see
/// [`tee_slave`]'s own doc comment for the still-unfixed tee-grouped gap.
fn record_dir(paths: &Paths, spec_id: PipelineId, target: &ObjectStoreRef) -> PathBuf {
    paths
        .stream_data_dir
        .join("rec")
        .join(&target.prefix)
        .join(spec_id.to_string())
}

fn resolve_secret(secret_ref: &SecretRef, paths: &Paths) -> Result<String, PipelineError> {
    paths
        .resolved_secrets
        .get(&secret_ref_key(secret_ref))
        .cloned()
        .ok_or_else(|| {
            PipelineError::InvalidSpec(format!(
                "no resolved value for secret ref {secret_ref:?} -- populate Paths::resolved_secrets before calling build_argv"
            ))
        })
}

/// Full trailing argv fragment (`-f <muxer> [options...] <target>`) for an
/// output that is the sole member of its profile group.
fn solo_muxer_args(
    output: &OutputSpec,
    spec_id: PipelineId,
    paths: &Paths,
) -> Result<Vec<String>, PipelineError> {
    match output {
        OutputSpec::RtmpPush { url_secret_ref, .. } => {
            let url = resolve_secret(url_secret_ref, paths)?;
            Ok(vec!["-f".into(), "flv".into(), url])
        }
        OutputSpec::SrtPush { url_secret_ref, .. } => {
            let url = resolve_secret(url_secret_ref, paths)?;
            Ok(vec!["-f".into(), "mpegts".into(), url])
        }
        OutputSpec::Hls { variant, profile } => {
            // Delegates to `egress::hls::output::HlsOutputTarget` -- the
            // single source of truth for the HLS argv fragment (spec (issue
            // #287) S7 §4), also consumed by `HlsSink`'s own doc comments as
            // the contract this builder was always supposed to call. This
            // used to be a hand-rolled, drifted duplicate here that omitted
            // `-master_pl_name`/`-hls_segment_filename`/
            // `-hls_fmp4_init_filename` -- ffmpeg never wrote a
            // `master.m3u8` and segments landed under ffmpeg's *default*
            // naming (`index<N>.m4s`) instead of `HlsOutputTarget`'s
            // `segment_%05d.m4s`, so `GET /live/{cid}/.../master.m3u8` (the
            // exact URL the `/live/{cid}` listing hands back) 404'd forever
            // even while the pipeline was running and writing real segments
            // -- see this crate's `README.md` Runtime Wiring / issue #287
            // S13 postmortem.
            let target = HlsOutputTarget::new(&paths.stream_data_dir, spec_id, profile, *variant);
            Ok(target.ffmpeg_output_args())
        }
        OutputSpec::Record { target, .. } => {
            let pattern = format!(
                "{}/%Y%m%d%H%M%S.ts",
                record_dir(paths, spec_id, target).to_string_lossy()
            );
            Ok(vec![
                "-f".into(),
                "segment".into(),
                "-segment_time".into(),
                "60".into(),
                "-reset_timestamps".into(),
                "1".into(),
                "-strftime".into(),
                "1".into(),
                pattern,
            ])
        }
        OutputSpec::Whep { .. } | OutputSpec::DiscordVoice { .. } => {
            Err(PipelineError::InvalidSpec(
                "Whep/DiscordVoice outputs never go through solo_muxer_args".into(),
            ))
        }
    }
}

/// True if `c` cannot appear verbatim in a `tee` slave target: the muxer
/// splits slaves on `|` and then honours `\` and `'` quoting at two nested
/// parse levels, so those (and control characters) cannot be represented
/// reliably -- and a `|` inside a secret URL would silently inject an extra
/// slave.
fn tee_unsafe_char(c: char) -> bool {
    matches!(c, '\\' | '\'' | '|') || c.is_control()
}

/// Validates a slave *target* (URL or path -- the part after the closing
/// `]`). Never echoes the value: it may be a secret URL.
fn check_tee_target(value: &str) -> Result<(), PipelineError> {
    if value.chars().any(tee_unsafe_char) {
        return Err(PipelineError::InvalidSpec(
            "an output target contains a character ffmpeg's tee muxer cannot carry (backslash, quote, '|' or a control character)"
                .into(),
        ));
    }
    Ok(())
}

/// Validates a slave *option value* (inside the `[...]` list), which
/// additionally cannot hold the option separators `:`, `[` and `]`.
fn check_tee_option_value(value: &str) -> Result<(), PipelineError> {
    if value
        .chars()
        .any(|c| tee_unsafe_char(c) || matches!(c, ':' | '[' | ']'))
    {
        return Err(PipelineError::InvalidSpec(
            "an output option contains a character ffmpeg's tee muxer cannot carry (colon, bracket, backslash, quote, '|' or a control character)"
                .into(),
        ));
    }
    Ok(())
}

/// `[<options>]<target>` fragment for an output grouped with siblings
/// inside `-f tee`.
///
/// Failure policy per slave:
/// - **RTMP/SRT pushes keep `onfail=ignore`**: an external destination
///   being down must not take the HLS output or the other destinations with
///   it.
/// - **HLS has no `onfail` (the default, `abort`)**: it is the playback
///   surface the live listing advertises, so a runtime write failure ends
///   the ffmpeg process and the supervisor restarts it, rather than leaving
///   viewers on a stalled playlist while the pipeline reports healthy.
/// - **Record keeps `onfail=ignore`**: archival is best-effort relative to
///   live delivery.
///
/// `onfail` only governs *runtime* write failures. A slave whose container
/// cannot hold the stream fails at header time and ffmpeg 5.1 skips it
/// regardless of `onfail` -- which is why [`build_argv`] rejects
/// incompatible codec/container pairs before any slave is built.
///
/// HLS slaves get the complete [`HlsOutputTarget`] option set (segment
/// type, init/segment filenames, master playlist), identical to what the
/// standalone path emits.
fn tee_slave(
    output: &OutputSpec,
    spec_id: PipelineId,
    paths: &Paths,
) -> Result<String, PipelineError> {
    match output {
        OutputSpec::RtmpPush { url_secret_ref, .. } => {
            let url = resolve_secret(url_secret_ref, paths)?;
            check_tee_target(&url)?;
            Ok(format!("[f=flv:onfail=ignore]{url}"))
        }
        OutputSpec::SrtPush { url_secret_ref, .. } => {
            let url = resolve_secret(url_secret_ref, paths)?;
            check_tee_target(&url)?;
            Ok(format!("[f=mpegts:onfail=ignore]{url}"))
        }
        OutputSpec::Hls { variant, profile } => {
            let target = HlsOutputTarget::new(&paths.stream_data_dir, spec_id, profile, *variant);
            let mut options = String::from("f=hls");
            for (name, value) in target.muxer_options() {
                check_tee_option_value(&value)?;
                options.push(':');
                options.push_str(name);
                options.push('=');
                options.push_str(&value);
            }
            let playlist = target.media_playlist_path().to_string_lossy().into_owned();
            check_tee_target(&playlist)?;
            Ok(format!("[{options}]{playlist}"))
        }
        // NOTE (S12 integration gap, not fixed here): this single-file mp4
        // target is never picked up by `egress::record`'s watcher, which
        // only globs `%Y%m%d%H%M%S.ts` segment files (the `solo_muxer_args`
        // shape) -- a Record output sharing a tee group with another output
        // on the same profile does not actually get uploaded to object
        // storage in this MVP. Still pointed at the same `rec/` root as the
        // solo path (was previously a third, divergent `records/` root) so
        // at least the two paths agree on where recordings live on disk.
        OutputSpec::Record { target, .. } => {
            let file = format!(
                "{}/{}.mp4",
                record_dir(paths, spec_id, target).to_string_lossy(),
                spec_id
            );
            check_tee_target(&file)?;
            Ok(format!("[f=mp4:onfail=ignore]{file}"))
        }
        OutputSpec::Whep { .. } | OutputSpec::DiscordVoice { .. } => {
            Err(PipelineError::InvalidSpec(
                "Whep/DiscordVoice outputs never participate in -f tee".into(),
            ))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pipeline::model::HlsVariant;
    use uuid::Uuid;

    fn paths_with_secrets(pairs: &[(SecretRef, &str)]) -> Paths {
        let mut resolved_secrets = HashMap::new();
        for (secret_ref, value) in pairs {
            resolved_secrets.insert(secret_ref_key(secret_ref), (*value).to_string());
        }
        Paths {
            stream_data_dir: PathBuf::from("/var/lib/svc-streaming"),
            resolved_secrets,
            whip_sdp_paths: HashMap::new(),
            rtp_base_port: 40000,
            ..Default::default()
        }
    }

    fn rtmp_url_ref() -> SecretRef {
        SecretRef::Env {
            var: "RELAY_URL".into(),
        }
    }

    fn spec(
        inputs: Vec<InputSpec>,
        profiles: Vec<TranscodeProfile>,
        outputs: Vec<OutputSpec>,
    ) -> PipelineSpec {
        PipelineSpec {
            id: Uuid::nil(),
            tenant: "tenant-1".into(),
            community_id: "community-1".into(),
            inputs,
            profiles,
            outputs,
        }
    }

    fn copy_profile(name: &str) -> TranscodeProfile {
        TranscodeProfile {
            name: name.into(),
            video: VideoCodec::Copy,
            audio: AudioCodec::Copy,
            resolution: None,
            fps: None,
        }
    }

    /// Checks that each fragment in `fragments` appears, in order, as a
    /// contiguous run of tokens somewhere in `argv` -- i.e. exact argv
    /// fragments, exact relative order, gaps between fragments allowed.
    fn assert_fragments_in_order(argv: &[String], fragments: &[&[&str]]) {
        let mut cursor = 0usize;
        for frag in fragments {
            let pos = argv[cursor..]
                .windows(frag.len().max(1))
                .position(|w| w.iter().map(String::as_str).eq(frag.iter().copied()))
                .unwrap_or_else(|| {
                    panic!("fragment {frag:?} not found in order at/after index {cursor} in argv: {argv:?}")
                });
            cursor += pos + frag.len();
        }
    }

    // Case 1: RTMP in -> RTMP push, Copy.
    #[test]
    fn golden_case_1_rtmp_copy_to_rtmp_push() {
        let s = spec(
            vec![InputSpec::Rtmp {
                stream_key: "sk1".into(),
            }],
            vec![copy_profile("copy")],
            vec![OutputSpec::RtmpPush {
                url_secret_ref: rtmp_url_ref(),
                profile: None,
            }],
        );
        let paths = paths_with_secrets(&[(rtmp_url_ref(), "rtmp://push/key1")]);
        let argv = build_argv(&s, &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-f", "flv", "-i", "pipe:0"],
                &["-map", "0:v", "-map", "0:a", "-c", "copy"],
                &["-f", "flv", "rtmp://push/key1"],
            ],
        );
    }

    // Case 2: Pull(HLS) -> HLS std, H264 720p30.
    #[test]
    fn golden_case_2_pull_h264_to_hls_std() {
        let s = spec(
            vec![InputSpec::Pull {
                url: "https://src.example/live.m3u8".into(),
            }],
            vec![TranscodeProfile {
                name: "720p30".into(),
                video: VideoCodec::H264 {
                    preset: "veryfast".into(),
                    crf: None,
                    bitrate_kbps: Some(2500),
                },
                audio: AudioCodec::Aac { bitrate_kbps: 128 },
                resolution: Some((1280, 720)),
                fps: Some(30),
            }],
            vec![OutputSpec::Hls {
                variant: HlsVariant::Std,
                profile: "720p30".into(),
            }],
        );
        let paths = paths_with_secrets(&[]);
        let argv = build_argv(&s, &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &[
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_delay_max",
                    "5",
                    "-i",
                    "https://src.example/live.m3u8",
                ],
                &["-c:v", "libx264", "-preset", "veryfast"],
                &["-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k"],
                &["-vf", "scale=1280:720", "-r", "30"],
                &["-c:a", "aac", "-b:a", "128k", "-ar", "48000"],
                &["-f", "hls", "-hls_time", "4", "-hls_list_size", "6"],
                &["-hls_segment_type", "fmp4"],
            ],
        );
        assert!(argv.last().unwrap().ends_with("/720p30/index.m3u8"));
    }

    /// Regression guard (issue #287 S13): a solo (non-tee) HLS output's argv
    /// must come from `egress::hls::output::HlsOutputTarget::
    /// ffmpeg_output_args` -- this function used to hand-roll an incomplete
    /// duplicate that omitted `-master_pl_name`/`-hls_segment_filename`/
    /// `-hls_fmp4_init_filename`, so ffmpeg never wrote `master.m3u8` (the
    /// exact file the `/live/{cid}` listing's `url` field points at) even
    /// while it was actively writing (default-named) segments -- listing a
    /// pipeline whose playback URL 404'd forever.
    #[test]
    fn solo_hls_output_argv_matches_hls_output_target_exactly() {
        let s = spec(
            vec![InputSpec::Pull {
                url: "https://src.example/live.m3u8".into(),
            }],
            vec![copy_profile("copy")],
            vec![OutputSpec::Hls {
                variant: HlsVariant::Std,
                profile: "copy".into(),
            }],
        );
        let paths = paths_with_secrets(&[]);
        let argv = build_argv(&s, &paths).expect("builds");

        let expected = HlsOutputTarget::new(&paths.stream_data_dir, s.id, "copy", HlsVariant::Std)
            .ffmpeg_output_args();

        // The Hls output's own argv fragment is the trailing portion of the
        // full argv (after the shared input/map/codec args this test
        // doesn't otherwise assert on) -- it must appear verbatim, in the
        // exact order `HlsOutputTarget` produces it, not just as scattered
        // sub-fragments.
        assert!(
            argv.windows(expected.len())
                .any(|w| w == expected.as_slice()),
            "expected the exact HlsOutputTarget fragment {expected:?} inside argv: {argv:?}"
        );
        assert!(argv.iter().any(|a| a == "-master_pl_name"));
        assert!(argv.iter().any(|a| a == "master.m3u8"));
    }

    // Case 3: SRT in -> SRT push, x265.
    #[test]
    fn golden_case_3_srt_x265_to_srt_push() {
        let srt_ref = SecretRef::File {
            path: "/secrets/srt-url".into(),
        };
        let s = spec(
            vec![InputSpec::Srt {
                stream_id: "srt1".into(),
            }],
            vec![TranscodeProfile {
                name: "x265".into(),
                video: VideoCodec::H265 {
                    preset: "fast".into(),
                    crf: None,
                    bitrate_kbps: Some(4000),
                },
                audio: AudioCodec::Aac { bitrate_kbps: 160 },
                resolution: None,
                fps: Some(30),
            }],
            vec![OutputSpec::SrtPush {
                url_secret_ref: srt_ref.clone(),
                profile: None,
            }],
        );
        let paths = paths_with_secrets(&[(srt_ref, "srt://push:9000?streamid=abc&latency=200")]);
        let argv = build_argv(&s, &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-f", "mpegts", "-i", "pipe:0"],
                &[
                    "-c:v",
                    "libx265",
                    "-preset",
                    "fast",
                    "-x265-params",
                    "keyint=60:min-keyint=60:scenecut=0",
                ],
                &["-b:v", "4000k", "-maxrate", "4000k", "-bufsize", "8000k"],
                &["-c:a", "aac"],
                &["-f", "mpegts", "srt://push:9000?streamid=abc&latency=200"],
            ],
        );
    }

    // Case 4: RTMP in -> (RTMP push + Record), same Copy profile -> tee.
    #[test]
    fn golden_case_4_rtmp_copy_tee_push_and_record() {
        let s = spec(
            vec![InputSpec::Rtmp {
                stream_key: "sk1".into(),
            }],
            vec![copy_profile("copy")],
            vec![
                OutputSpec::RtmpPush {
                    url_secret_ref: rtmp_url_ref(),
                    profile: None,
                },
                OutputSpec::Record {
                    profile: "copy".into(),
                    target: ObjectStoreRef {
                        store: "s3-recordings".into(),
                        prefix: "tenant-1/community-1".into(),
                    },
                },
            ],
        );
        let paths = paths_with_secrets(&[(rtmp_url_ref(), "rtmp://push/key1")]);
        let argv = build_argv(&s, &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-f", "flv", "-i", "pipe:0"],
                &["-map", "0:v", "-map", "0:a", "-c", "copy"],
                &["-f", "tee"],
            ],
        );
        let tee_arg = argv.last().unwrap();
        assert!(tee_arg.contains("[f=flv:onfail=ignore]rtmp://push/key1"));
        assert!(tee_arg.contains("[f=mp4:onfail=ignore]"));
        assert!(tee_arg.contains("tenant-1/community-1"));
        assert!(tee_arg.contains('|'));
    }

    // Case 5: Pull -> HLS, AV1 SVT.
    #[test]
    fn golden_case_5_pull_av1svt_to_hls() {
        let s = spec(
            vec![InputSpec::Pull {
                url: "https://src.example/live.m3u8".into(),
            }],
            vec![TranscodeProfile {
                name: "av1".into(),
                video: VideoCodec::Av1Svt {
                    preset: "10".into(),
                    crf: None,
                    bitrate_kbps: Some(3000),
                },
                audio: AudioCodec::Opus { bitrate_kbps: 128 },
                resolution: None,
                fps: Some(30),
            }],
            vec![OutputSpec::Hls {
                variant: HlsVariant::Std,
                profile: "av1".into(),
            }],
        );
        let paths = paths_with_secrets(&[]);
        let argv = build_argv(&s, &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-i", "https://src.example/live.m3u8"],
                &[
                    "-c:v",
                    "libsvtav1",
                    "-preset",
                    "10",
                    "-svtav1-params",
                    "tune=0:fast-decode=1",
                ],
                &["-c:a", "libopus"],
                &["-f", "hls"],
                &["-hls_segment_type", "fmp4"],
            ],
        );
    }

    // Case 6: WHIP in -> WHEP out, Copy -> no ffmpeg process needed.
    #[test]
    fn golden_case_6_whip_to_whep_copy_needs_no_ffmpeg() {
        let s = spec(
            vec![InputSpec::Whip {
                token: "whip-tok".into(),
            }],
            vec![copy_profile("copy")],
            vec![OutputSpec::Whep {
                profile: "copy".into(),
            }],
        );
        let paths = paths_with_secrets(&[]);
        let err = build_argv(&s, &paths).unwrap_err();
        assert!(matches!(err, PipelineError::NoFfmpegNeeded));
    }

    // Case 7: RTMP in -> 3 mixed outputs: Copy->RTMP + 720p H264->HLS +
    // 480p H264->HLS.
    #[test]
    fn golden_case_7_rtmp_mixed_ladder_three_outputs() {
        let h264 = |name: &str, w, h, kbps| TranscodeProfile {
            name: name.into(),
            video: VideoCodec::H264 {
                preset: "veryfast".into(),
                crf: None,
                bitrate_kbps: Some(kbps),
            },
            audio: AudioCodec::Aac { bitrate_kbps: 128 },
            resolution: Some((w, h)),
            fps: Some(30),
        };
        let s = spec(
            vec![InputSpec::Rtmp {
                stream_key: "sk1".into(),
            }],
            vec![
                copy_profile("copy"),
                h264("720p", 1280, 720, 2500),
                h264("480p", 854, 480, 1000),
            ],
            vec![
                OutputSpec::RtmpPush {
                    url_secret_ref: rtmp_url_ref(),
                    profile: None,
                },
                OutputSpec::Hls {
                    variant: HlsVariant::Std,
                    profile: "720p".into(),
                },
                OutputSpec::Hls {
                    variant: HlsVariant::Std,
                    profile: "480p".into(),
                },
            ],
        );
        let paths = paths_with_secrets(&[(rtmp_url_ref(), "rtmp://push/key1")]);
        let argv = build_argv(&s, &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-f", "flv", "-i", "pipe:0"],
                &[
                    "-filter_complex",
                    "[0:v]split=2[v720][v480];[v720]scale=1280:720[v720s];[v480]scale=854:480[v480s]",
                ],
                &["-map", "0:v", "-map", "0:a", "-c:v", "copy", "-c:a", "copy"],
                // The lone push stays isolated behind a tee with a null guard
                // slave so a dead destination cannot take the HLS outputs
                // down with it.
                &["-f", "tee", "[f=flv:onfail=ignore]rtmp://push/key1|[f=null]-"],
                &["-map", "[v720s]", "-map", "0:a", "-c:v", "libx264"],
                &["-b:v", "2500k"],
                &["-f", "hls"],
                &["-map", "[v480s]", "-map", "0:a", "-c:v", "libx264"],
                &["-b:v", "1000k"],
                &["-f", "hls"],
            ],
        );
        // Each output has a distinct profile, so no tee group has two
        // members; the only tee is the lone push's guarded isolation tee.
        assert_eq!(argv.iter().filter(|a| *a == "tee").count(), 1);
    }

    // Case 8: RTMP in -> Discord voice.
    #[test]
    fn golden_case_8_rtmp_to_discord_voice() {
        let s = spec(
            vec![InputSpec::Rtmp {
                stream_key: "sk1".into(),
            }],
            vec![copy_profile("copy")],
            vec![OutputSpec::DiscordVoice {
                guild_id: "guild-1".into(),
                channel_id: "channel-1".into(),
            }],
        );
        let paths = paths_with_secrets(&[]);
        let argv = build_argv(&s, &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-f", "flv", "-i", "pipe:0"],
                &[
                    "-map",
                    "0:a",
                    "-vn",
                    "-c:a",
                    "pcm_s16le",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-f",
                    "s16le",
                    "pipe:1",
                ],
            ],
        );
    }

    // Case 9: Pull -> Record only, Copy.
    #[test]
    fn golden_case_9_pull_record_only() {
        let s = spec(
            vec![InputSpec::Pull {
                url: "https://src.example/live.m3u8".into(),
            }],
            vec![copy_profile("copy")],
            vec![OutputSpec::Record {
                profile: "copy".into(),
                target: ObjectStoreRef {
                    store: "s3-recordings".into(),
                    prefix: "tenant-1/community-1".into(),
                },
            }],
        );
        let paths = paths_with_secrets(&[]);
        let argv = build_argv(&s, &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-i", "https://src.example/live.m3u8"],
                &["-c", "copy"],
                &[
                    "-f",
                    "segment",
                    "-segment_time",
                    "60",
                    "-reset_timestamps",
                    "1",
                    "-strftime",
                    "1",
                ],
            ],
        );
        assert!(argv.last().unwrap().ends_with("%Y%m%d%H%M%S.ts"));
    }

    // Case 10: 2x RTMP in -> 1 HLS out, overlay composite -> P2, Unsupported.
    #[test]
    fn golden_case_10_multi_input_is_unsupported() {
        let s = spec(
            vec![
                InputSpec::Rtmp {
                    stream_key: "sk1".into(),
                },
                InputSpec::Rtmp {
                    stream_key: "sk2".into(),
                },
            ],
            vec![TranscodeProfile {
                name: "1080p".into(),
                video: VideoCodec::H264 {
                    preset: "veryfast".into(),
                    crf: None,
                    bitrate_kbps: Some(3000),
                },
                audio: AudioCodec::Aac { bitrate_kbps: 128 },
                resolution: Some((1920, 1080)),
                fps: Some(30),
            }],
            vec![OutputSpec::Hls {
                variant: HlsVariant::Std,
                profile: "1080p".into(),
            }],
        );
        let paths = paths_with_secrets(&[]);
        let err = build_argv(&s, &paths).unwrap_err();
        assert!(matches!(err, PipelineError::Unsupported(_)));
    }

    // Extra coverage beyond the 10 golden cases: RTMP -> WHEP (transcode,
    // not the pure-copy shortcut) exercises the `-f rtp` egress block and
    // confirms rtp_legs() ports line up with the embedded argv target.
    #[test]
    fn whep_transcode_emits_rtp_block_matching_rtp_legs() {
        let s = spec(
            vec![InputSpec::Rtmp {
                stream_key: "sk1".into(),
            }],
            vec![TranscodeProfile {
                name: "720p".into(),
                video: VideoCodec::H264 {
                    preset: "veryfast".into(),
                    crf: None,
                    bitrate_kbps: Some(2000),
                },
                audio: AudioCodec::Opus { bitrate_kbps: 128 },
                resolution: Some((1280, 720)),
                fps: Some(30),
            }],
            vec![OutputSpec::Whep {
                profile: "720p".into(),
            }],
        );
        let paths = Paths {
            rtp_base_port: 41000,
            ..paths_with_secrets(&[])
        };
        let legs = rtp_legs(&s, &paths);
        assert_eq!(legs.len(), 1);
        assert_eq!(legs[0].direction, RtpLegDirection::ToWhep);
        assert_eq!(legs[0].local_port, 41000);

        let argv = build_argv(&s, &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-map", "0:v", "-map", "0:a", "-c:v", "libx264"],
                &["-c:a", "libopus"],
                &["-f", "rtp", "udp://127.0.0.1:41000"],
            ],
        );
    }

    #[test]
    fn rtp_legs_enumerates_a_whip_input_leg() {
        let s = spec(
            vec![InputSpec::Whip {
                token: "whip-tok".into(),
            }],
            vec![copy_profile("copy")],
            vec![OutputSpec::Record {
                profile: "copy".into(),
                target: ObjectStoreRef {
                    store: "s3".into(),
                    prefix: "t".into(),
                },
            }],
        );
        let paths = Paths {
            rtp_base_port: 42000,
            ..paths_with_secrets(&[])
        };
        let legs = rtp_legs(&s, &paths);
        assert_eq!(legs.len(), 1);
        assert_eq!(legs[0].direction, RtpLegDirection::FromWhip);
        assert_eq!(legs[0].local_port, 42000);
        assert!(legs[0].profile.is_none());
    }

    #[test]
    fn rtp_legs_enumerates_both_whip_and_whep_legs_in_order() {
        let s = spec(
            vec![InputSpec::Whip {
                token: "whip-tok".into(),
            }],
            vec![
                copy_profile("copy"),
                TranscodeProfile {
                    name: "720p".into(),
                    video: VideoCodec::H264 {
                        preset: "veryfast".into(),
                        crf: None,
                        bitrate_kbps: Some(2000),
                    },
                    audio: AudioCodec::Opus { bitrate_kbps: 128 },
                    resolution: Some((1280, 720)),
                    fps: Some(30),
                },
            ],
            vec![OutputSpec::Whep {
                profile: "720p".into(),
            }],
        );
        let paths = Paths {
            rtp_base_port: 43000,
            ..paths_with_secrets(&[])
        };
        let legs = rtp_legs(&s, &paths);
        assert_eq!(legs.len(), 2);
        assert_eq!(legs[0].direction, RtpLegDirection::FromWhip);
        assert_eq!(legs[0].local_port, 43000);
        assert_eq!(legs[1].direction, RtpLegDirection::ToWhep);
        assert_eq!(legs[1].local_port, 43001);
        assert_eq!(legs[1].profile.as_deref(), Some("720p"));
    }

    #[test]
    fn missing_secret_ref_is_invalid_spec_not_a_panic() {
        let s = spec(
            vec![InputSpec::Rtmp {
                stream_key: "sk1".into(),
            }],
            vec![copy_profile("copy")],
            vec![OutputSpec::RtmpPush {
                url_secret_ref: rtmp_url_ref(),
                profile: None,
            }],
        );
        let paths = paths_with_secrets(&[]); // secret intentionally unresolved
        let err = build_argv(&s, &paths).unwrap_err();
        assert!(matches!(err, PipelineError::InvalidSpec(_)));
    }

    #[test]
    fn output_referencing_unknown_profile_is_invalid_spec() {
        let s = spec(
            vec![InputSpec::Rtmp {
                stream_key: "sk1".into(),
            }],
            vec![copy_profile("copy")],
            vec![OutputSpec::Hls {
                variant: HlsVariant::Std,
                profile: "does-not-exist".into(),
            }],
        );
        let paths = paths_with_secrets(&[]);
        let err = build_argv(&s, &paths).unwrap_err();
        assert!(matches!(err, PipelineError::InvalidSpec(_)));
    }

    #[test]
    fn empty_inputs_is_invalid_spec() {
        let s = spec(vec![], vec![copy_profile("copy")], vec![]);
        let paths = paths_with_secrets(&[]);
        let err = build_argv(&s, &paths).unwrap_err();
        assert!(matches!(err, PipelineError::InvalidSpec(_)));
    }

    #[test]
    fn secret_ref_key_distinguishes_env_and_file() {
        assert_ne!(
            secret_ref_key(&SecretRef::Env { var: "X".into() }),
            secret_ref_key(&SecretRef::File { path: "X".into() })
        );
    }

    // ------------------------------------------------------------------
    // Codec selection, GPU/CPU encoders, ladder, fan-out polish
    // ------------------------------------------------------------------

    fn profile(name: &str, video: VideoCodec, audio: AudioCodec) -> TranscodeProfile {
        TranscodeProfile {
            name: name.into(),
            video,
            audio,
            resolution: None,
            fps: None,
        }
    }

    fn h264_video() -> VideoCodec {
        VideoCodec::H264 {
            preset: "veryfast".into(),
            crf: None,
            bitrate_kbps: Some(2500),
        }
    }

    fn h265_video() -> VideoCodec {
        VideoCodec::H265 {
            preset: "veryfast".into(),
            crf: None,
            bitrate_kbps: Some(2500),
        }
    }

    fn av1_video() -> VideoCodec {
        VideoCodec::Av1Svt {
            preset: "10".into(),
            crf: None,
            bitrate_kbps: Some(2500),
        }
    }

    fn hls_out(profile: &str) -> OutputSpec {
        OutputSpec::Hls {
            variant: HlsVariant::Std,
            profile: profile.into(),
        }
    }

    fn rtmp_out(profile: Option<&str>) -> OutputSpec {
        OutputSpec::RtmpPush {
            url_secret_ref: rtmp_url_ref(),
            profile: profile.map(str::to_string),
        }
    }

    fn srt_ref() -> SecretRef {
        SecretRef::Env {
            var: "SRT_URL".into(),
        }
    }

    fn srt_out(profile: Option<&str>) -> OutputSpec {
        OutputSpec::SrtPush {
            url_secret_ref: srt_ref(),
            profile: profile.map(str::to_string),
        }
    }

    fn rtmp_input() -> Vec<InputSpec> {
        vec![InputSpec::Rtmp {
            stream_key: "sk1".into(),
        }]
    }

    fn push_paths() -> Paths {
        paths_with_secrets(&[
            (rtmp_url_ref(), "rtmp://push/key1"),
            (srt_ref(), "srt://push:9000?streamid=abc"),
        ])
    }

    fn invalid_spec_message(err: PipelineError) -> String {
        match err {
            PipelineError::InvalidSpec(msg) => msg,
            other => panic!("expected InvalidSpec, got {other:?}"),
        }
    }

    // --- H.265 / AV1 are reachable where the container supports them ---

    #[test]
    fn hevc_to_hls_uses_libx265_and_the_hvc1_tag_players_need() {
        let s = spec(
            rtmp_input(),
            vec![profile("default", h265_video(), AudioCodec::Copy)],
            vec![hls_out("default")],
        );
        let argv = build_argv(&s, &push_paths()).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-c:v", "libx265", "-preset", "veryfast"],
                &["-tag:v", "hvc1"],
                &["-c:a", "copy"],
                &["-f", "hls"],
                &["-hls_segment_type", "fmp4"],
            ],
        );
    }

    #[test]
    fn av1_to_hls_uses_svt_av1_with_a_numeric_preset() {
        let s = spec(
            rtmp_input(),
            vec![profile(
                "default",
                av1_video(),
                AudioCodec::Aac { bitrate_kbps: 128 },
            )],
            vec![hls_out("default")],
        );
        let argv = build_argv(&s, &push_paths()).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-c:v", "libsvtav1", "-preset", "10"],
                &["-c:a", "aac"],
                &["-f", "hls"],
            ],
        );
    }

    #[test]
    fn hevc_over_srt_alongside_hls_shares_one_encode_through_tee() {
        let s = spec(
            rtmp_input(),
            vec![profile("default", h265_video(), AudioCodec::Copy)],
            vec![hls_out("default"), srt_out(Some("default"))],
        );
        let argv = build_argv(&s, &push_paths()).expect("builds");
        assert_eq!(
            argv.iter().filter(|a| *a == "libx265").count(),
            1,
            "one shared encode, not one per output: {argv:?}"
        );
        let tee = argv.last().unwrap();
        assert!(tee.contains("[f=hls:"), "{tee}");
        assert!(
            tee.contains("[f=mpegts:onfail=ignore]srt://push:9000?streamid=abc"),
            "{tee}"
        );
    }

    // --- incompatible codec/container pairs fail loudly -----------------

    #[test]
    fn rtmp_push_of_hevc_or_av1_is_rejected_with_the_flv_message() {
        for video in [h265_video(), av1_video()] {
            let s = spec(
                rtmp_input(),
                vec![profile("default", video, AudioCodec::Copy)],
                vec![rtmp_out(None)],
            );
            let msg = invalid_spec_message(build_argv(&s, &push_paths()).unwrap_err());
            assert!(
                msg.contains(
                    "FLV/RTMP cannot carry HEVC/AV1 (needs enhanced-RTMP); use HLS/SRT or H.264"
                ),
                "message was: {msg}"
            );
        }
    }

    #[test]
    fn the_original_silent_tee_drop_is_now_a_loud_error() {
        // HLS + an RTMP push sharing one H.265 profile used to build a tee
        // whose FLV slave ffmpeg skipped at header time (exit 0, no push).
        let s = spec(
            rtmp_input(),
            vec![profile("default", h265_video(), AudioCodec::Copy)],
            vec![hls_out("default"), rtmp_out(Some("default"))],
        );
        let msg = invalid_spec_message(build_argv(&s, &push_paths()).unwrap_err());
        assert!(msg.contains("FLV/RTMP cannot carry HEVC/AV1"), "{msg}");
        assert!(msg.contains("\"default\""), "names the profile: {msg}");
    }

    #[test]
    fn srt_push_of_av1_is_rejected_because_ffmpeg_5_muxes_it_as_private_data() {
        let s = spec(
            rtmp_input(),
            vec![profile("default", av1_video(), AudioCodec::Copy)],
            vec![srt_out(None)],
        );
        let msg = invalid_spec_message(build_argv(&s, &push_paths()).unwrap_err());
        assert!(msg.contains("MPEG-TS/SRT cannot carry AV1"), "{msg}");
    }

    #[test]
    fn rtmp_push_of_opus_audio_is_rejected_but_hls_and_srt_carry_it() {
        let opus = AudioCodec::Opus { bitrate_kbps: 128 };
        let rtmp = spec(
            rtmp_input(),
            vec![profile("default", h264_video(), opus.clone())],
            vec![rtmp_out(None)],
        );
        let msg = invalid_spec_message(build_argv(&rtmp, &push_paths()).unwrap_err());
        assert!(msg.contains("cannot carry Opus"), "{msg}");

        for out in [hls_out("default"), srt_out(None)] {
            let ok = spec(
                rtmp_input(),
                vec![profile("default", h264_video(), opus.clone())],
                vec![out],
            );
            assert!(build_argv(&ok, &push_paths()).is_ok());
        }
    }

    #[test]
    fn passthrough_profiles_are_never_rejected_by_the_codec_check() {
        let s = spec(
            rtmp_input(),
            vec![copy_profile("copy")],
            vec![rtmp_out(None), srt_out(None), hls_out("copy")],
        );
        assert!(build_argv(&s, &push_paths()).is_ok());
    }

    #[test]
    fn recording_validates_as_mpegts_alone_and_as_mp4_inside_a_tee() {
        let record = |profile: &str| OutputSpec::Record {
            profile: profile.into(),
            target: ObjectStoreRef {
                store: "s3".into(),
                prefix: "t/c".into(),
            },
        };
        // Solo: `-f segment` writes `.ts`, which cannot hold AV1 on ffmpeg 5.1.
        let solo = spec(
            rtmp_input(),
            vec![profile("default", av1_video(), AudioCodec::Copy)],
            vec![record("default")],
        );
        assert!(build_argv(&solo, &push_paths()).is_err());

        // In a tee group it is a single MP4, which carries everything.
        let grouped = spec(
            rtmp_input(),
            vec![profile("default", av1_video(), AudioCodec::Copy)],
            vec![hls_out("default"), record("default")],
        );
        assert!(build_argv(&grouped, &push_paths()).is_ok());
    }

    // --- GPU-preferred, CPU-fallback ------------------------------------

    fn hls_h264_spec() -> PipelineSpec {
        spec(
            rtmp_input(),
            vec![profile("default", h264_video(), AudioCodec::Copy)],
            vec![hls_out("default")],
        )
    }

    #[test]
    fn default_paths_encode_on_the_cpu() {
        let argv = build_argv(&hls_h264_spec(), &push_paths()).expect("builds");
        assert!(argv.windows(2).any(|w| w == ["-c:v", "libx264"]));
        assert!(!argv
            .iter()
            .any(|a| a.contains("nvenc") || a.contains("vaapi")));
    }

    #[test]
    fn nvenc_backend_swaps_in_the_hardware_encoder_with_a_valid_preset() {
        let paths = Paths {
            encoders: EncoderSelection::software().with(VideoFamily::H264, EncoderBackend::Nvenc),
            ..push_paths()
        };
        let argv = build_argv(&hls_h264_spec(), &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-c:v", "h264_nvenc", "-preset", "p2", "-tune", "ll"],
                &["-b:v", "2500k"],
                &["-f", "hls"],
            ],
        );
        assert!(!argv.iter().any(|a| a == "libx264" || a == "veryfast"));
    }

    #[test]
    fn each_codec_family_picks_its_own_backend() {
        let paths = Paths {
            encoders: EncoderSelection::software().with(VideoFamily::H265, EncoderBackend::Nvenc),
            ..push_paths()
        };
        // H.265 goes to the GPU...
        let s = spec(
            rtmp_input(),
            vec![profile("default", h265_video(), AudioCodec::Copy)],
            vec![hls_out("default")],
        );
        let argv = build_argv(&s, &paths).expect("builds");
        assert!(argv.windows(2).any(|w| w == ["-c:v", "hevc_nvenc"]));
        assert!(argv.windows(2).any(|w| w == ["-tag:v", "hvc1"]));
        // ...while H.264 on the same host stays on the CPU.
        let argv = build_argv(&hls_h264_spec(), &paths).expect("builds");
        assert!(argv.windows(2).any(|w| w == ["-c:v", "libx264"]));
    }

    #[test]
    fn av1_stays_on_the_cpu_when_no_gpu_av1_encoder_was_selected() {
        let paths = Paths {
            encoders: EncoderSelection::software()
                .with(VideoFamily::H264, EncoderBackend::Nvenc)
                .with(VideoFamily::H265, EncoderBackend::Nvenc),
            ..push_paths()
        };
        let s = spec(
            rtmp_input(),
            vec![profile("default", av1_video(), AudioCodec::Copy)],
            vec![hls_out("default")],
        );
        let argv = build_argv(&s, &paths).expect("builds");
        assert!(argv.windows(2).any(|w| w == ["-c:v", "libsvtav1"]));
    }

    #[test]
    fn vaapi_registers_the_device_globally_and_uploads_frames_per_output() {
        let paths = Paths {
            encoders: EncoderSelection::software()
                .with(VideoFamily::H264, EncoderBackend::Vaapi)
                .with_vaapi_device("/dev/dri/renderD128"),
            ..push_paths()
        };
        let mut s = hls_h264_spec();
        s.profiles[0].resolution = Some((1280, 720));
        let argv = build_argv(&s, &paths).expect("builds");
        // `-nostdin` leads; global device registration follows, before the input.
        assert_eq!(argv[0], "-nostdin");
        assert_eq!(
            &argv[1..5],
            [
                "-init_hw_device",
                "vaapi=va:/dev/dri/renderD128",
                "-filter_hw_device",
                "va"
            ]
        );
        assert_fragments_in_order(
            &argv,
            &[
                &["-f", "flv", "-i", "pipe:0"],
                &["-c:v", "h264_vaapi"],
                &["-vf", "scale=1280:720,format=nv12,hwupload"],
            ],
        );
    }

    #[test]
    fn vaapi_without_a_render_node_is_an_invalid_spec_not_a_broken_command() {
        let paths = Paths {
            encoders: EncoderSelection::software().with(VideoFamily::H264, EncoderBackend::Vaapi),
            ..push_paths()
        };
        let msg = invalid_spec_message(build_argv(&hls_h264_spec(), &paths).unwrap_err());
        assert!(msg.contains("no render node"), "{msg}");
    }

    #[test]
    fn a_passthrough_pipeline_never_asks_for_a_gpu() {
        let paths = Paths {
            encoders: EncoderSelection::software().with(VideoFamily::H264, EncoderBackend::Vaapi),
            ..push_paths()
        };
        let s = spec(
            rtmp_input(),
            vec![copy_profile("copy")],
            vec![hls_out("copy")],
        );
        let argv = build_argv(&s, &paths).expect("builds");
        assert!(!argv.iter().any(|a| a == "-init_hw_device"));
    }

    #[test]
    fn encode_summary_reports_each_distinct_encode_and_its_backend() {
        let paths = Paths {
            encoders: EncoderSelection::software().with(VideoFamily::H265, EncoderBackend::Nvenc),
            ..push_paths()
        };
        let s = spec(
            rtmp_input(),
            vec![
                profile("default", h265_video(), AudioCodec::Copy),
                profile("h264-copy", h264_video(), AudioCodec::Copy),
                copy_profile("record"),
            ],
            vec![
                hls_out("default"),
                srt_out(Some("default")),
                rtmp_out(Some("h264-copy")),
                OutputSpec::Record {
                    profile: "record".into(),
                    target: ObjectStoreRef {
                        store: "s3".into(),
                        prefix: "t".into(),
                    },
                },
            ],
        );
        assert_eq!(
            encode_summary(&s, &paths),
            vec![
                EncodeInfo {
                    profile: "default".into(),
                    family: VideoFamily::H265,
                    backend: EncoderBackend::Nvenc,
                },
                EncodeInfo {
                    profile: "h264-copy".into(),
                    family: VideoFamily::H264,
                    backend: EncoderBackend::Software,
                },
            ]
        );
    }

    #[test]
    fn encode_summary_skips_outputs_it_cannot_resolve() {
        let s = spec(
            rtmp_input(),
            vec![copy_profile("copy")],
            vec![hls_out("missing"), rtmp_out(Some("also-missing"))],
        );
        assert!(encode_summary(&s, &push_paths()).is_empty());
    }

    // --- ladder ----------------------------------------------------------

    #[test]
    fn codec_ladder_without_resolutions_splits_the_decode_with_null_filters() {
        // HLS + SRT on H.265 (shared encode), RTMP on its own H.264 rung.
        let s = spec(
            rtmp_input(),
            vec![
                profile("default", h265_video(), AudioCodec::Copy),
                profile("h264-copy", h264_video(), AudioCodec::Copy),
            ],
            vec![
                hls_out("default"),
                rtmp_out(Some("h264-copy")),
                srt_out(Some("default")),
            ],
        );
        let argv = build_argv(&s, &push_paths()).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[
                &["-f", "flv", "-i", "pipe:0"],
                &[
                    "-filter_complex",
                    "[0:v]split=2[v_default][v_h264_copy];[v_default]null[v_defaults];[v_h264_copy]null[v_h264_copys]",
                ],
                // First group: the H.265 rung feeding HLS + SRT through tee.
                &["-map", "[v_defaults]", "-map", "0:a", "-c:v", "libx265"],
                &["-f", "tee"],
                // Second group: the H.264 rung feeding the RTMP push alone,
                // isolated behind a guarded tee.
                &["-map", "[v_h264_copys]", "-map", "0:a", "-c:v", "libx264"],
                &["-f", "tee", "[f=flv:onfail=ignore]rtmp://push/key1|[f=null]-"],
            ],
        );
        assert_eq!(argv.iter().filter(|a| *a == "libx265").count(), 1);
        assert_eq!(argv.iter().filter(|a| *a == "libx264").count(), 1);
    }

    #[test]
    fn ladder_rungs_at_the_same_height_get_distinct_labels() {
        let mut a = profile("hevc", h265_video(), AudioCodec::Copy);
        a.resolution = Some((1280, 720));
        let mut b = profile("avc", h264_video(), AudioCodec::Copy);
        b.resolution = Some((1280, 720));
        let s = spec(
            rtmp_input(),
            vec![a, b],
            vec![hls_out("hevc"), rtmp_out(Some("avc"))],
        );
        let argv = build_argv(&s, &push_paths()).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[&[
                "-filter_complex",
                "[0:v]split=2[v720][v720_1];[v720]scale=1280:720[v720s];[v720_1]scale=1280:720[v720_1s]",
            ]],
        );
        assert!(argv.windows(2).any(|w| w == ["-map", "[v720s]"]));
        assert!(argv.windows(2).any(|w| w == ["-map", "[v720_1s]"]));
    }

    #[test]
    fn vaapi_ladder_rungs_upload_inside_the_filter_graph() {
        let paths = Paths {
            encoders: EncoderSelection::software()
                .with(VideoFamily::H264, EncoderBackend::Vaapi)
                .with_vaapi_device("/dev/dri/renderD128"),
            ..push_paths()
        };
        let s = spec(
            rtmp_input(),
            vec![
                profile("default", h265_video(), AudioCodec::Copy),
                profile("h264-copy", h264_video(), AudioCodec::Copy),
            ],
            vec![hls_out("default"), rtmp_out(Some("h264-copy"))],
        );
        let argv = build_argv(&s, &paths).expect("builds");
        assert_fragments_in_order(
            &argv,
            &[&[
                "-filter_complex",
                "[0:v]split=2[v_default][v_h264_copy];[v_default]null[v_defaults];[v_h264_copy]format=nv12,hwupload[v_h264_copys]",
            ]],
        );
        // The H.265 rung stays on the CPU; only H.264 went to VA-API.
        assert!(argv.windows(2).any(|w| w == ["-c:v", "libx265"]));
        assert!(argv.windows(2).any(|w| w == ["-c:v", "h264_vaapi"]));
        // The filter graph already uploads, so no per-output `-vf`.
        assert!(!argv.iter().any(|a| a == "-vf"));
    }

    // --- push profile binding -------------------------------------------

    #[test]
    fn a_push_output_binds_to_its_named_profile() {
        let s = spec(
            rtmp_input(),
            vec![
                profile("first", h265_video(), AudioCodec::Copy),
                profile("second", h264_video(), AudioCodec::Copy),
            ],
            vec![hls_out("first"), rtmp_out(Some("second"))],
        );
        let argv = build_argv(&s, &push_paths()).expect("builds");
        // The RTMP push is on the H.264 rung, not profiles[0] (H.265).
        let flv_pos = argv
            .iter()
            .position(|a| a.starts_with("[f=flv"))
            .expect("the RTMP push slave");
        let x264_pos = argv.iter().position(|a| a == "libx264").expect("x264");
        let x265_pos = argv.iter().position(|a| a == "libx265").expect("x265");
        assert!(x265_pos < x264_pos && x264_pos < flv_pos, "{argv:?}");
    }

    #[test]
    fn a_push_output_naming_an_unknown_profile_is_an_invalid_spec() {
        let s = spec(
            rtmp_input(),
            vec![copy_profile("copy")],
            vec![rtmp_out(Some("nope"))],
        );
        let msg = invalid_spec_message(build_argv(&s, &push_paths()).unwrap_err());
        assert!(msg.contains("unknown profile"), "{msg}");
    }

    #[test]
    fn an_unbound_push_with_no_profiles_at_all_is_an_invalid_spec() {
        let s = spec(rtmp_input(), vec![], vec![rtmp_out(None)]);
        let msg = invalid_spec_message(build_argv(&s, &push_paths()).unwrap_err());
        assert!(msg.contains("no transcode profiles"), "{msg}");
    }

    // --- tee slaves ------------------------------------------------------

    fn tee_arg(s: &PipelineSpec, paths: &Paths) -> String {
        let argv = build_argv(s, paths).expect("builds");
        let pos = argv.iter().position(|a| a == "tee").expect("a tee output");
        argv[pos + 1].clone()
    }

    #[test]
    fn hls_in_a_tee_group_carries_the_full_hls_option_set() {
        let s = spec(
            rtmp_input(),
            vec![profile("default", h264_video(), AudioCodec::Copy)],
            vec![hls_out("default"), rtmp_out(Some("default"))],
        );
        let paths = push_paths();
        let tee = tee_arg(&s, &paths);

        let target = HlsOutputTarget::new(&paths.stream_data_dir, s.id, "default", HlsVariant::Std);
        let mut expected = String::from("[f=hls");
        for (name, value) in target.muxer_options() {
            expected.push_str(&format!(":{name}={value}"));
        }
        expected.push_str(&format!("]{}", target.media_playlist_path().display()));
        assert!(
            tee.starts_with(&expected),
            "tee HLS slave must equal the standalone option set\nexpected prefix: {expected}\nactual:          {tee}"
        );
        // The regression the minimal `f=hls` slave caused: no master playlist
        // and ffmpeg's default segment names the file server never serves.
        assert!(tee.contains("master_pl_name=master.m3u8"));
        assert!(tee.contains("hls_segment_filename="));
        assert!(tee.contains("hls_fmp4_init_filename=init.mp4"));
        assert!(tee.contains("hls_segment_type=fmp4"));
    }

    #[test]
    fn tee_failure_policy_isolates_pushes_but_not_the_hls_surface() {
        let s = spec(
            rtmp_input(),
            vec![profile("default", h264_video(), AudioCodec::Copy)],
            vec![
                hls_out("default"),
                rtmp_out(Some("default")),
                srt_out(Some("default")),
            ],
        );
        let tee = tee_arg(&s, &push_paths());
        let slaves: Vec<&str> = tee.split('|').collect();
        assert_eq!(slaves.len(), 3);
        // A dead external destination must not take the others down...
        assert!(
            slaves[1].starts_with("[f=flv:onfail=ignore]"),
            "{}",
            slaves[1]
        );
        assert!(
            slaves[2].starts_with("[f=mpegts:onfail=ignore]"),
            "{}",
            slaves[2]
        );
        // ...but the HLS slave aborts the process on a write failure so the
        // supervisor restarts it instead of serving a frozen playlist.
        assert!(slaves[0].starts_with("[f=hls:"), "{}", slaves[0]);
        assert!(!slaves[0].contains("onfail"), "{}", slaves[0]);
    }

    #[test]
    fn tee_target_with_a_pipe_character_is_rejected_without_echoing_it() {
        let secret = SecretRef::Env { var: "EVIL".into() };
        let s = spec(
            rtmp_input(),
            vec![profile("default", h264_video(), AudioCodec::Copy)],
            vec![
                hls_out("default"),
                OutputSpec::RtmpPush {
                    url_secret_ref: secret.clone(),
                    profile: None,
                },
            ],
        );
        for bad in [
            "rtmp://h/a/key|[f=flv]rtmp://attacker/steal",
            "rtmp://h/a/ke'y",
            "rtmp://h/a/ke\\y",
            "rtmp://h/a/ke\ny",
        ] {
            let paths = paths_with_secrets(&[(secret.clone(), bad)]);
            let msg = invalid_spec_message(build_argv(&s, &paths).unwrap_err());
            assert!(msg.contains("tee muxer cannot carry"), "{msg}");
            assert!(
                !msg.contains("attacker") && !msg.contains("key"),
                "leaked: {msg}"
            );
        }
    }

    #[test]
    fn tee_option_values_cannot_contain_option_separators() {
        let s = spec(
            rtmp_input(),
            vec![profile("default", h264_video(), AudioCodec::Copy)],
            vec![hls_out("default"), rtmp_out(Some("default"))],
        );
        for dir in ["/data/a:b", "/data/[x]", "/data/it's"] {
            let paths = Paths {
                stream_data_dir: PathBuf::from(dir),
                ..push_paths()
            };
            assert!(build_argv(&s, &paths).is_err(), "{dir} must be refused");
        }
    }

    #[test]
    fn rtmps_urls_reach_the_flv_muxer_solo_and_in_a_tee() {
        let url = "rtmps://live-api-s.facebook.com:443/rtmp/FB-key";
        let paths = paths_with_secrets(&[(rtmp_url_ref(), url)]);

        let solo = spec(
            rtmp_input(),
            vec![copy_profile("copy")],
            vec![rtmp_out(None)],
        );
        let argv = build_argv(&solo, &paths).expect("builds");
        assert_fragments_in_order(&argv, &[&["-f", "flv", url]]);

        let grouped = spec(
            rtmp_input(),
            vec![copy_profile("copy")],
            vec![rtmp_out(None), hls_out("copy")],
        );
        let tee = tee_arg(&grouped, &paths);
        assert!(
            tee.contains(&format!("[f=flv:onfail=ignore]{url}")),
            "{tee}"
        );
    }

    // --- destination cap --------------------------------------------------

    fn spec_with_pushes(count: usize) -> PipelineSpec {
        let mut outputs = vec![hls_out("copy")];
        for i in 0..count {
            outputs.push(if i % 2 == 0 {
                rtmp_out(Some("copy"))
            } else {
                srt_out(Some("copy"))
            });
        }
        spec(rtmp_input(), vec![copy_profile("copy")], outputs)
    }

    #[test]
    fn the_free_tier_cap_applies_to_the_argv_itself() {
        let paths = Paths {
            relay_policy: Some(RelayPolicy::FREE),
            ..push_paths()
        };
        assert!(build_argv(&spec_with_pushes(3), &paths).is_ok());
        let msg = invalid_spec_message(build_argv(&spec_with_pushes(4), &paths).unwrap_err());
        assert_eq!(msg, "relay limit: max 3 destinations on this tier");
    }

    #[test]
    fn the_cap_counts_rtmp_and_srt_pushes_but_not_hls_or_record() {
        let paths = Paths {
            relay_policy: Some(RelayPolicy::FREE),
            ..push_paths()
        };
        let mut s = spec_with_pushes(3);
        s.outputs.push(OutputSpec::Record {
            profile: "copy".into(),
            target: ObjectStoreRef {
                store: "s3".into(),
                prefix: "t".into(),
            },
        });
        s.outputs.push(hls_out("copy"));
        assert!(build_argv(&s, &paths).is_ok());
    }

    #[test]
    fn a_higher_tier_policy_raises_the_cap_and_no_policy_means_unbounded() {
        let pro = Paths {
            relay_policy: Some(RelayPolicy::PROFESSIONAL),
            ..push_paths()
        };
        assert!(build_argv(&spec_with_pushes(10), &pro).is_ok());
        assert!(build_argv(&spec_with_pushes(11), &pro).is_err());
        assert!(build_argv(&spec_with_pushes(11), &push_paths()).is_ok());
    }

    #[test]
    fn a_lone_push_beside_other_outputs_is_still_isolated_behind_a_tee() {
        // HLS on an H.265 rung + an RTMP push on its own H.264 rung: the
        // push is alone in its group, but a dead destination must not abort
        // the process (it would take HLS with it).
        let s = spec(
            rtmp_input(),
            vec![
                profile("default", h265_video(), AudioCodec::Copy),
                profile("h264-copy", h264_video(), AudioCodec::Copy),
            ],
            vec![hls_out("default"), rtmp_out(Some("h264-copy"))],
        );
        let argv = build_argv(&s, &push_paths()).expect("builds");
        assert_eq!(
            &argv[argv.len() - 3..],
            [
                "-f",
                "tee",
                "[f=flv:onfail=ignore]rtmp://push/key1|[f=null]-"
            ]
        );
    }

    #[test]
    fn a_push_only_tee_group_beside_hls_gets_a_null_guard_so_it_can_never_fully_fail() {
        // HLS on H.265; RTMP + a second RTMP both on the H.264 rung.
        let s = spec(
            rtmp_input(),
            vec![
                profile("default", h265_video(), AudioCodec::Copy),
                profile("h264-copy", h264_video(), AudioCodec::Copy),
            ],
            vec![
                hls_out("default"),
                rtmp_out(Some("h264-copy")),
                srt_out(Some("h264-copy")),
            ],
        );
        let argv = build_argv(&s, &push_paths()).expect("builds");
        let tee = argv.last().unwrap();
        assert_eq!(
            tee,
            "[f=flv:onfail=ignore]rtmp://push/key1|[f=mpegts:onfail=ignore]srt://push:9000?streamid=abc|[f=null]-"
        );
    }

    #[test]
    fn a_shared_tee_with_a_local_slave_needs_no_guard() {
        let s = spec(
            rtmp_input(),
            vec![copy_profile("copy")],
            vec![hls_out("copy"), rtmp_out(Some("copy"))],
        );
        assert!(!tee_arg(&s, &push_paths()).contains("f=null"));
    }

    #[test]
    fn a_pushes_only_pipeline_gets_no_guard_so_total_failure_is_not_silent() {
        let s = spec(
            rtmp_input(),
            vec![copy_profile("copy")],
            vec![rtmp_out(None), srt_out(None)],
        );
        let tee = tee_arg(&s, &push_paths());
        assert!(!tee.contains("f=null"), "{tee}");
    }

    #[test]
    fn a_pipeline_whose_only_output_is_a_push_stays_a_plain_muxer() {
        let s = spec(
            rtmp_input(),
            vec![copy_profile("copy")],
            vec![rtmp_out(None)],
        );
        let argv = build_argv(&s, &push_paths()).expect("builds");
        assert!(!argv.iter().any(|a| a == "tee"));
        assert_eq!(&argv[argv.len() - 3..], ["-f", "flv", "rtmp://push/key1"]);
    }

    #[test]
    fn a_lone_srt_push_is_isolated_the_same_way() {
        let s = spec(
            rtmp_input(),
            vec![copy_profile("copy")],
            vec![hls_out("copy"), srt_out(Some("copy"))],
        );
        // Same profile -> shared tee group, not a separate muxer.
        let tee = tee_arg(&s, &push_paths());
        assert!(tee.contains("[f=mpegts:onfail=ignore]srt://push:9000?streamid=abc"));
    }

    #[test]
    fn encode_summary_ignores_the_audio_only_discord_voice_output() {
        let s = spec(
            rtmp_input(),
            vec![profile("default", h264_video(), AudioCodec::Copy)],
            vec![
                hls_out("default"),
                OutputSpec::DiscordVoice {
                    guild_id: "g".into(),
                    channel_id: "c".into(),
                },
            ],
        );
        let summary = encode_summary(&s, &push_paths());
        assert_eq!(summary.len(), 1);
        assert_eq!(summary[0].family, VideoFamily::H264);
        // ...and building the argv for the same spec still works.
        assert!(build_argv(&s, &push_paths()).is_ok());
    }

    #[test]
    fn resolving_a_profile_for_discord_voice_is_an_error_not_a_panic() {
        let s = spec(rtmp_input(), vec![copy_profile("copy")], vec![]);
        let voice = OutputSpec::DiscordVoice {
            guild_id: "g".into(),
            channel_id: "c".into(),
        };
        let msg = invalid_spec_message(output_profile_name(&voice, &s).unwrap_err());
        assert!(msg.contains("audio-only"), "{msg}");
    }
}
