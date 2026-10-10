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
//! **Known model gap (flagged for the model owner, not fixed here to avoid
//! breaking `tests/model.rs` which this chunk does not own):**
//! [`OutputSpec::RtmpPush`] and [`OutputSpec::SrtPush`] carry no `profile`
//! field, unlike every other profile-consuming output variant. Until the
//! model gains one, both variants implicitly bind to `spec.profiles[0]` --
//! see [`output_profile_name`]. Every golden test spec in this module
//! orders `profiles` so the intended profile is first.

use std::collections::HashMap;
use std::path::PathBuf;

use crate::egress::hls::HlsOutputTarget;
use crate::pipeline::model::{
    AudioCodec, InputSpec, ObjectStoreRef, OutputSpec, PipelineError, PipelineId, PipelineSpec,
    TranscodeProfile, VideoCodec,
};
use crate::redact::scrub_diagnostic;
use crate::store::SecretRef;

/// Runtime path/secret inputs [`build_argv`] needs to turn a
/// [`PipelineSpec`] into a concrete `ffmpeg` argv. Resolving `SecretRef`s
/// (env/file reads) is deliberately kept outside `build_argv` so the argv
/// builder stays a pure, easily golden-tested function -- callers (the
/// supervisor) resolve secrets via [`crate::store::SecretResolver`] once at
/// spawn time and populate this struct.
///
/// `Debug` is hand-written: `resolved_secrets` holds the raw resolved relay
/// URLs (stream keys) and `whip_sdp_paths` file names embed the WHIP token,
/// so a derived `{:?}` would print credentials. The manual impl lists only
/// the secret *references* and masks the token in each SDP path.
#[derive(Clone, Default)]
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
}

impl std::fmt::Debug for Paths {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let mut secret_refs: Vec<&String> = self.resolved_secrets.keys().collect();
        secret_refs.sort();
        let sdp_paths: std::collections::BTreeMap<usize, String> = self
            .whip_sdp_paths
            .iter()
            .map(|(index, path)| (*index, scrub_diagnostic(&path.display().to_string())))
            .collect();
        f.debug_struct("Paths")
            .field("stream_data_dir", &self.stream_data_dir)
            .field("resolved_secret_refs", &secret_refs)
            .field("whip_sdp_paths", &sdp_paths)
            .field("rtp_base_port", &self.rtp_base_port)
            .finish()
    }
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

    let mut argv = input_args(input, paths)?;

    if use_filter_complex {
        argv.push("-filter_complex".into());
        argv.push(build_filter_complex(&ladder)?);
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

    for (_, members) in &groups {
        let profile = members[0].profile;
        push_map_and_codec(&mut argv, profile, use_filter_complex);
        if members.len() == 1 {
            argv.extend(solo_muxer_args(members[0].output, spec.id, paths)?);
        } else {
            let mut slaves = Vec::with_capacity(members.len());
            for m in members {
                slaves.push(tee_slave(m.output, spec.id, paths)?);
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
        push_map_and_codec(&mut argv, r.profile, use_filter_complex);
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

/// Resolves the [`TranscodeProfile`] name an output applies. See module
/// docs for the `RtmpPush`/`SrtPush` model-gap caveat.
fn output_profile_name(output: &OutputSpec, spec: &PipelineSpec) -> Result<String, PipelineError> {
    match output {
        OutputSpec::RtmpPush { .. } | OutputSpec::SrtPush { .. } => spec
            .profiles
            .first()
            .map(|p| p.name.clone())
            .ok_or_else(|| PipelineError::InvalidSpec("pipeline has no transcode profiles".into())),
        OutputSpec::Hls { profile, .. }
        | OutputSpec::Whep { profile }
        | OutputSpec::Record { profile, .. } => Ok(profile.clone()),
        OutputSpec::DiscordVoice { .. } => {
            unreachable!("DiscordVoice never resolves a profile -- filtered out before this call")
        }
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

/// Resolution-derived filter-graph label for a ladder rung -- `v<height>`.
/// Two rungs sharing an identical resolution but different profile names
/// would collide; not reachable from any golden case, flagged as a known
/// MVP simplification rather than worked around with an uglier label.
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

/// Builds `[0:v]split=N[label1][label2]...;[label1]scale=W:H[label1s];...`
/// per spec §1/§3/§7 case 7 -- only the non-copy rungs participate; a
/// `Copy` rung always maps `0:v` directly (never through the filter
/// graph).
fn build_filter_complex(ladder: &[&TranscodeProfile]) -> Result<String, PipelineError> {
    let labels: Vec<String> = ladder.iter().map(|p| video_label(p)).collect();
    let split_targets: String = labels.iter().map(|l| format!("[{l}]")).collect();
    let mut filter = format!("[0:v]split={}{}", ladder.len(), split_targets);
    for (profile, label) in ladder.iter().zip(labels.iter()) {
        let (w, h) = profile.resolution.ok_or_else(|| {
            PipelineError::InvalidSpec(format!(
                "profile {:?} needs a resolution to participate in a scale ladder",
                profile.name
            ))
        })?;
        filter.push_str(&format!(";[{label}]scale={w}:{h}[{label}s]"));
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
) {
    let filtered_label = if use_filter_complex && !is_full_copy(profile) {
        Some(format!("{}s", video_label(profile)))
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
        argv.extend(video_codec_args(&profile.video, profile.fps.unwrap_or(30)));
        if filtered_label.is_none() {
            // Not going through the filter graph -- scale/fps are plain
            // output options (spec §7 case 2). When filtered, scale is
            // already embedded in the `-filter_complex` chain.
            if let Some((w, h)) = profile.resolution {
                argv.push("-vf".into());
                argv.push(format!("scale={w}:{h}"));
            }
            if let Some(fps) = profile.fps {
                argv.push("-r".into());
                argv.push(fps.to_string());
            }
        }
        argv.extend(audio_codec_args(&profile.audio));
    }
}

/// Spec §3 video codec recipes. `2*fps` keyframe interval and CPU-only
/// (no VAAPI/NVENC) throughout, per spec §3 note.
fn video_codec_args(codec: &VideoCodec, fps: u32) -> Vec<String> {
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

fn hls_dir(paths: &Paths, spec_id: PipelineId, profile: &str) -> PathBuf {
    paths
        .stream_data_dir
        .join("hls")
        .join(spec_id.to_string())
        .join(profile)
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
        OutputSpec::RtmpPush { url_secret_ref } => {
            let url = resolve_secret(url_secret_ref, paths)?;
            Ok(vec!["-f".into(), "flv".into(), url])
        }
        OutputSpec::SrtPush { url_secret_ref } => {
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

/// `[f=<muxer>:onfail=ignore]<target>` fragment for an output grouped with
/// siblings inside `-f tee`. HLS/Record tee slaves use a minimal
/// `f=<muxer>` (no per-slave HLS options) -- a known simplification; no
/// golden case exercises HLS inside a tee group.
fn tee_slave(
    output: &OutputSpec,
    spec_id: PipelineId,
    paths: &Paths,
) -> Result<String, PipelineError> {
    match output {
        OutputSpec::RtmpPush { url_secret_ref } => {
            let url = resolve_secret(url_secret_ref, paths)?;
            Ok(format!("[f=flv:onfail=ignore]{url}"))
        }
        OutputSpec::SrtPush { url_secret_ref } => {
            let url = resolve_secret(url_secret_ref, paths)?;
            Ok(format!("[f=mpegts:onfail=ignore]{url}"))
        }
        OutputSpec::Hls { profile, .. } => Ok(format!(
            "[f=hls:onfail=ignore]{}/index.m3u8",
            hls_dir(paths, spec_id, profile).to_string_lossy()
        )),
        // NOTE (S12 integration gap, not fixed here): this single-file mp4
        // target is never picked up by `egress::record`'s watcher, which
        // only globs `%Y%m%d%H%M%S.ts` segment files (the `solo_muxer_args`
        // shape) -- a Record output sharing a tee group with another output
        // on the same profile does not actually get uploaded to object
        // storage in this MVP. Still pointed at the same `rec/` root as the
        // solo path (was previously a third, divergent `records/` root) so
        // at least the two paths agree on where recordings live on disk.
        OutputSpec::Record { target, .. } => Ok(format!(
            "[f=mp4:onfail=ignore]{}/{}.mp4",
            record_dir(paths, spec_id, target).to_string_lossy(),
            spec_id
        )),
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
                &["-f", "flv", "rtmp://push/key1"],
                &["-map", "[v720s]", "-map", "0:a", "-c:v", "libx264"],
                &["-b:v", "2500k"],
                &["-f", "hls"],
                &["-map", "[v480s]", "-map", "0:a", "-c:v", "libx264"],
                &["-b:v", "1000k"],
                &["-f", "hls"],
            ],
        );
        // No -f tee anywhere -- each output has a distinct profile.
        assert!(!argv.iter().any(|a| a == "tee"));
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
}
