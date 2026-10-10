//! GPU-preferred, CPU-fallback video encoder selection.
//!
//! The CPU encoders (`libx264`/`libx265`/`libsvtav1`) work on every host and
//! are the guaranteed fallback. When a usable GPU is present the hardware
//! encoders (`*_nvenc`, `*_vaapi`) take over per codec family, because a
//! live transcode on a GPU frees whole cores for ingest, fan-out and the
//! rest of the service.
//!
//! # Why a trial encode, not just a device check
//!
//! A device node is necessary but nowhere near sufficient: the encoder must
//! be compiled into the `ffmpeg` binary, the user-space driver must be in
//! the image, and the session must actually open. On the Waddles alpha host
//! (`penguin-frame`, AMD Phoenix1) `/dev/dri/renderD128` exists, yet the
//! runtime image has no VA driver, so `h264_vaapi` fails with `Failed to
//! initialise VAAPI connection`. [`detect_encoders`] therefore only selects
//! a hardware encoder after a real one-second trial encode succeeds, and
//! the per-codec result is what makes AV1 fall back to CPU on a build whose
//! `ffmpeg` has no `av1_nvenc`/`av1_vaapi` (Debian's 5.1.x).
//!
//! Detection runs once at startup and is a plain value
//! ([`EncoderSelection`]) afterwards, handed to the pure argv builder via
//! `pipeline::ffmpeg::Paths`, so `build_argv` stays I/O-free and golden-
//! testable on both branches.
//!
//! Known limit: NVENC on consumer cards caps concurrent sessions; a session
//! that fails to open at runtime is a pipeline failure the supervisor
//! restarts, not a per-start CPU re-fallback.

use std::collections::HashSet;
use std::fmt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::str::FromStr;
use std::time::Duration;

use opentelemetry::{global, KeyValue};
use tokio::process::Command;
use tokio::time::timeout;

use crate::pipeline::codec::VideoFamily;

/// Upper bound for the `ffmpeg -encoders` listing.
const LIST_TIMEOUT: Duration = Duration::from_secs(15);
/// Upper bound for one trial encode (driver init can be slow on first use).
const TRIAL_TIMEOUT: Duration = Duration::from_secs(20);
/// Frame size of the trial encode -- large enough for every hardware
/// encoder's minimum-dimension constraint.
const TRIAL_FRAME_SIZE: &str = "640x360";
/// ffmpeg's filter chain that moves a software frame onto a VAAPI surface.
pub const VAAPI_UPLOAD_FILTER: &str = "format=nv12,hwupload";
/// Name the VAAPI device is registered under inside one ffmpeg command.
const VAAPI_DEVICE_NAME: &str = "va";

/// How a codec family is encoded.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EncoderBackend {
    /// CPU: `libx264`/`libx265`/`libsvtav1`. Works everywhere.
    Software,
    /// NVIDIA NVENC: `h264_nvenc`/`hevc_nvenc`/`av1_nvenc`.
    Nvenc,
    /// VA-API (Intel/AMD): `h264_vaapi`/`hevc_vaapi`/`av1_vaapi`.
    Vaapi,
}

impl EncoderBackend {
    /// Stable lowercase label (log fields, metric attributes).
    pub fn as_str(self) -> &'static str {
        match self {
            EncoderBackend::Software => "cpu",
            EncoderBackend::Nvenc => "nvenc",
            EncoderBackend::Vaapi => "vaapi",
        }
    }

    /// The ffmpeg encoder name for `family` on this backend.
    pub fn encoder_name(self, family: VideoFamily) -> &'static str {
        match (self, family) {
            (EncoderBackend::Software, VideoFamily::H264) => "libx264",
            (EncoderBackend::Software, VideoFamily::H265) => "libx265",
            (EncoderBackend::Software, VideoFamily::Av1) => "libsvtav1",
            (EncoderBackend::Nvenc, VideoFamily::H264) => "h264_nvenc",
            (EncoderBackend::Nvenc, VideoFamily::H265) => "hevc_nvenc",
            (EncoderBackend::Nvenc, VideoFamily::Av1) => "av1_nvenc",
            (EncoderBackend::Vaapi, VideoFamily::H264) => "h264_vaapi",
            (EncoderBackend::Vaapi, VideoFamily::H265) => "hevc_vaapi",
            (EncoderBackend::Vaapi, VideoFamily::Av1) => "av1_vaapi",
        }
    }
}

impl fmt::Display for EncoderBackend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Operator preference (`STREAM_ENCODER`). Every value still falls back to
/// the CPU per codec when its GPU path is unusable -- the CPU path must
/// work everywhere, so no preference can make the service unable to encode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum EncoderPreference {
    /// Use a GPU when a trial encode proves it works, else the CPU.
    #[default]
    Auto,
    /// Never use a GPU.
    Cpu,
    /// Prefer NVENC only.
    Nvenc,
    /// Prefer VA-API only.
    Vaapi,
}

impl fmt::Display for EncoderPreference {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            EncoderPreference::Auto => "auto",
            EncoderPreference::Cpu => "cpu",
            EncoderPreference::Nvenc => "nvenc",
            EncoderPreference::Vaapi => "vaapi",
        })
    }
}

impl FromStr for EncoderPreference {
    type Err = String;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "auto" => Ok(EncoderPreference::Auto),
            "cpu" | "software" => Ok(EncoderPreference::Cpu),
            "nvenc" => Ok(EncoderPreference::Nvenc),
            "vaapi" => Ok(EncoderPreference::Vaapi),
            other => Err(format!(
                "unknown encoder preference {:?}; expected one of auto, cpu, nvenc, vaapi",
                other.chars().take(32).collect::<String>()
            )),
        }
    }
}

/// The per-codec encoder backends chosen at startup. [`Default`] is
/// all-software, which is also what a host without a GPU ends up with.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct EncoderSelection {
    h264: Option<EncoderBackend>,
    h265: Option<EncoderBackend>,
    av1: Option<EncoderBackend>,
    vaapi_device: Option<PathBuf>,
}

impl EncoderSelection {
    /// Every codec on the CPU.
    pub fn software() -> Self {
        Self::default()
    }

    /// The backend `family` is encoded with.
    pub fn backend_for(&self, family: VideoFamily) -> EncoderBackend {
        let slot = match family {
            VideoFamily::H264 => self.h264,
            VideoFamily::H265 => self.h265,
            VideoFamily::Av1 => self.av1,
        };
        slot.unwrap_or(EncoderBackend::Software)
    }

    /// Returns `self` with `family` encoded by `backend`.
    pub fn with(mut self, family: VideoFamily, backend: EncoderBackend) -> Self {
        let slot = match family {
            VideoFamily::H264 => &mut self.h264,
            VideoFamily::H265 => &mut self.h265,
            VideoFamily::Av1 => &mut self.av1,
        };
        *slot = Some(backend);
        self
    }

    /// Returns `self` with the VA-API render node every VA-API encoder uses.
    pub fn with_vaapi_device(mut self, device: impl Into<PathBuf>) -> Self {
        self.vaapi_device = Some(device.into());
        self
    }

    /// The VA-API render node, if one was selected.
    pub fn vaapi_device(&self) -> Option<&Path> {
        self.vaapi_device.as_deref()
    }

    /// True when no codec uses a GPU.
    pub fn is_software_only(&self) -> bool {
        VideoFamily::ALL
            .iter()
            .all(|f| self.backend_for(*f) == EncoderBackend::Software)
    }

    /// Records one `encoder_backend{codec,backend,encoder} = 1` gauge point
    /// per codec so dashboards show what this process actually encodes with.
    pub fn record_metrics(&self) {
        let gauge = global::meter("svc_streaming_pipeline")
            .i64_gauge("encoder_backend")
            .with_description("Encoder backend selected per codec at startup (value is always 1)")
            .build();
        for family in VideoFamily::ALL {
            let backend = self.backend_for(family);
            gauge.record(
                1,
                &[
                    KeyValue::new("codec", family.as_str()),
                    KeyValue::new("backend", backend.as_str()),
                    KeyValue::new("encoder", backend.encoder_name(family)),
                ],
            );
        }
    }
}

/// GPU device nodes found on this host -- the cheap pre-check that decides
/// whether spawning `ffmpeg` trial encodes is worth it at all.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DeviceHints {
    /// An NVIDIA device node (`/dev/nvidiactl`, `/dev/nvidia0`) exists.
    pub nvidia: bool,
    /// The VA-API render node to use (`/dev/dri/renderD*`).
    pub vaapi_device: Option<PathBuf>,
}

impl DeviceHints {
    /// Probes the real filesystem. `vaapi_override` (`STREAM_VAAPI_DEVICE`)
    /// wins over auto-discovery when it exists.
    pub fn detect(vaapi_override: Option<&Path>) -> Self {
        Self::detect_in(Path::new("/"), vaapi_override)
    }

    /// Like [`Self::detect`], but rooted at `root` so tests do not depend
    /// on the GPUs of the machine they run on.
    pub fn detect_in(root: &Path, vaapi_override: Option<&Path>) -> Self {
        let nvidia = ["dev/nvidiactl", "dev/nvidia0"]
            .iter()
            .any(|node| root.join(node).exists());

        let vaapi_device = match vaapi_override {
            Some(explicit) => {
                let candidate = if explicit.is_absolute() {
                    root.join(explicit.strip_prefix("/").unwrap_or(explicit))
                } else {
                    root.join(explicit)
                };
                candidate.exists().then(|| explicit.to_path_buf())
            }
            None => first_render_node(root),
        };

        Self {
            nvidia,
            vaapi_device,
        }
    }
}

/// Lowest-numbered `renderD*` node under `root/dev/dri`, reported as its
/// absolute (`/dev/dri/...`) path -- that is the path ffmpeg opens.
fn first_render_node(root: &Path) -> Option<PathBuf> {
    let entries = std::fs::read_dir(root.join("dev/dri")).ok()?;
    let mut nodes: Vec<String> = entries
        .filter_map(|entry| entry.ok())
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| name.starts_with("renderD"))
        .collect();
    nodes.sort();
    nodes
        .into_iter()
        .next()
        .map(|name| Path::new("/dev/dri").join(name))
}

/// Backends worth trying, in preference order, given the operator's
/// preference and the devices present.
fn candidate_backends(pref: EncoderPreference, hints: &DeviceHints) -> Vec<EncoderBackend> {
    let nvenc = hints.nvidia;
    let vaapi = hints.vaapi_device.is_some();
    match pref {
        EncoderPreference::Cpu => Vec::new(),
        EncoderPreference::Nvenc => nvenc.then_some(EncoderBackend::Nvenc).into_iter().collect(),
        EncoderPreference::Vaapi => vaapi.then_some(EncoderBackend::Vaapi).into_iter().collect(),
        EncoderPreference::Auto => {
            let mut order = Vec::new();
            if nvenc {
                order.push(EncoderBackend::Nvenc);
            }
            if vaapi {
                order.push(EncoderBackend::Vaapi);
            }
            order
        }
    }
}

/// Selects an encoder backend per codec family: the first GPU backend (in
/// `pref` order) whose encoder is compiled into `ffmpeg` and passes a trial
/// encode, else the CPU. Never fails -- every error path degrades to the CPU
/// encoder with a log line saying why.
pub async fn detect_encoders(
    ffmpeg: &Path,
    pref: EncoderPreference,
    hints: &DeviceHints,
) -> EncoderSelection {
    let selection = select_encoders(ffmpeg, pref, hints).await;
    for family in VideoFamily::ALL {
        let backend = selection.backend_for(family);
        tracing::info!(
            codec = family.as_str(),
            backend = backend.as_str(),
            encoder = backend.encoder_name(family),
            "encoder selected"
        );
    }
    selection.record_metrics();
    selection
}

async fn select_encoders(
    ffmpeg: &Path,
    pref: EncoderPreference,
    hints: &DeviceHints,
) -> EncoderSelection {
    if pref == EncoderPreference::Cpu {
        tracing::info!("STREAM_ENCODER=cpu: GPU encoders disabled by configuration");
        return EncoderSelection::software();
    }

    let candidates = candidate_backends(pref, hints);
    if candidates.is_empty() {
        if pref == EncoderPreference::Auto {
            tracing::info!("no GPU device node found, using CPU encoders");
        } else {
            tracing::warn!(
                preference = %pref,
                "requested GPU encoder backend has no device node on this host, using CPU encoders"
            );
        }
        return EncoderSelection::software();
    }

    let compiled_in = match list_encoders(ffmpeg).await {
        Ok(names) => names,
        Err(reason) => {
            tracing::warn!(%reason, "could not list ffmpeg encoders, using CPU encoders");
            return EncoderSelection::software();
        }
    };

    let mut selection = EncoderSelection::software();
    for family in VideoFamily::ALL {
        for backend in &candidates {
            let encoder = backend.encoder_name(family);
            if !compiled_in.contains(encoder) {
                tracing::debug!(
                    encoder,
                    "encoder not compiled into this ffmpeg build, skipping"
                );
                continue;
            }
            match trial_encode(ffmpeg, *backend, family, hints.vaapi_device.as_deref()).await {
                Ok(()) => {
                    selection = selection.with(family, *backend);
                    break;
                }
                Err(reason) => {
                    tracing::warn!(
                        encoder,
                        %reason,
                        "GPU encoder is present but unusable, falling back"
                    );
                }
            }
        }
    }

    if VideoFamily::ALL
        .iter()
        .any(|f| selection.backend_for(*f) == EncoderBackend::Vaapi)
    {
        if let Some(device) = &hints.vaapi_device {
            selection = selection.with_vaapi_device(device.clone());
        }
    }
    selection
}

/// Parses `ffmpeg -encoders` output into the set of encoder names.
///
/// Each entry line is `<6 flag chars> <name> <description>` and entries
/// follow a `------` separator; the legend above it (`V..... = Video`) has
/// the same flag shape, so it is skipped by position, not by guessing.
pub fn parse_encoder_listing(listing: &str) -> HashSet<String> {
    listing
        .lines()
        .skip_while(|line| line.trim() != "------")
        .skip(1)
        .filter_map(|line| {
            let mut parts = line.split_whitespace();
            let flags = parts.next()?;
            let name = parts.next()?;
            let is_flags = flags.len() == 6
                && flags.starts_with(['V', 'A', 'S'])
                && flags[1..]
                    .chars()
                    .all(|c| c == '.' || c.is_ascii_uppercase());
            is_flags.then(|| name.to_string())
        })
        .collect()
}

async fn list_encoders(ffmpeg: &Path) -> Result<HashSet<String>, String> {
    let mut command = Command::new(ffmpeg);
    command
        .args(["-hide_banner", "-encoders"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let output = timeout(LIST_TIMEOUT, command.output())
        .await
        .map_err(|_| "timed out listing encoders".to_string())?
        .map_err(|err| format!("could not run ffmpeg: {err}"))?;
    if !output.status.success() {
        return Err(format!("ffmpeg -encoders exited with {}", output.status));
    }
    let listing = parse_encoder_listing(&String::from_utf8_lossy(&output.stdout));
    if listing.is_empty() {
        return Err("ffmpeg -encoders listed no encoders".to_string());
    }
    Ok(listing)
}

/// The ffmpeg argv of the trial encode for `backend`/`family`: three frames
/// of black video through the real encoder into the `null` muxer.
pub fn trial_args(
    backend: EncoderBackend,
    family: VideoFamily,
    vaapi_device: Option<&Path>,
) -> Vec<String> {
    let mut args: Vec<String> = ["-hide_banner", "-nostdin", "-loglevel", "error"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    if backend == EncoderBackend::Vaapi {
        args.extend(vaapi_global_args(
            vaapi_device.unwrap_or(Path::new("/dev/dri/renderD128")),
        ));
    }
    args.extend([
        "-f".to_string(),
        "lavfi".to_string(),
        "-i".to_string(),
        format!("color=c=black:s={TRIAL_FRAME_SIZE}:r=10:d=1"),
    ]);
    if backend == EncoderBackend::Vaapi {
        args.extend(["-vf".to_string(), VAAPI_UPLOAD_FILTER.to_string()]);
    }
    args.extend([
        "-frames:v".to_string(),
        "3".to_string(),
        "-c:v".to_string(),
        backend.encoder_name(family).to_string(),
        "-f".to_string(),
        "null".to_string(),
        "-".to_string(),
    ]);
    args
}

async fn trial_encode(
    ffmpeg: &Path,
    backend: EncoderBackend,
    family: VideoFamily,
    vaapi_device: Option<&Path>,
) -> Result<(), String> {
    let mut command = Command::new(ffmpeg);
    command
        .args(trial_args(backend, family, vaapi_device))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let output = timeout(TRIAL_TIMEOUT, command.output())
        .await
        .map_err(|_| "trial encode timed out".to_string())?
        .map_err(|err| format!("could not run ffmpeg: {err}"))?;
    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    let first = stderr
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty() && !line.starts_with("set_mempolicy"))
        .unwrap_or("no diagnostic output");
    Err(format!(
        "trial encode exited with {}: {}",
        output.status,
        first.chars().take(200).collect::<String>()
    ))
}

/// Global ffmpeg arguments that register the VA-API render node every
/// VA-API encode in the command uploads frames to.
pub fn vaapi_global_args(device: &Path) -> Vec<String> {
    vec![
        "-init_hw_device".to_string(),
        format!("vaapi={VAAPI_DEVICE_NAME}:{}", device.display()),
        "-filter_hw_device".to_string(),
        VAAPI_DEVICE_NAME.to_string(),
    ]
}

/// Maps an x264/x265 preset name onto the nearest NVENC `p1`..`p7` preset
/// (`p1` fastest). x264's `veryfast` is not a valid NVENC preset; an
/// already-NVENC value passes through; anything unknown lands on the
/// balanced `p4`.
pub fn nvenc_preset(software_preset: &str) -> &'static str {
    match software_preset.to_ascii_lowercase().as_str() {
        "ultrafast" | "superfast" | "p1" => "p1",
        "veryfast" | "p2" => "p2",
        "faster" | "p3" => "p3",
        "fast" | "p4" => "p4",
        "medium" | "p5" => "p5",
        "slow" | "p6" => "p6",
        "slower" | "veryslow" | "placebo" | "p7" => "p7",
        _ => "p4",
    }
}

/// Rate-control flags shared by the hardware encoders: a target bitrate
/// (with a 2x VBV buffer) when one is configured.
fn bitrate_args(kbps: u32) -> Vec<String> {
    vec![
        "-b:v".to_string(),
        format!("{kbps}k"),
        "-maxrate".to_string(),
        format!("{kbps}k"),
        "-bufsize".to_string(),
        format!("{}k", kbps.saturating_mul(2)),
    ]
}

/// The `-c:v ...` argv fragment for a hardware encode of `family` on
/// `backend`. Software encodes keep their own recipes in
/// `pipeline::ffmpeg::video_codec_args`.
///
/// `preset` is the (x264-vocabulary) preset from the profile; NVENC maps it
/// via [`nvenc_preset`], VA-API has no preset and ignores it.
pub fn hardware_video_args(
    backend: EncoderBackend,
    family: VideoFamily,
    preset: &str,
    crf: Option<u8>,
    bitrate_kbps: Option<u32>,
    fps: u32,
) -> Vec<String> {
    let mut args = vec!["-c:v".to_string(), backend.encoder_name(family).to_string()];
    match backend {
        EncoderBackend::Nvenc => {
            args.extend([
                "-preset".to_string(),
                nvenc_preset(preset).to_string(),
                "-tune".to_string(),
                "ll".to_string(),
            ]);
            match family {
                VideoFamily::H264 => args.extend(["-profile:v".to_string(), "high".to_string()]),
                VideoFamily::H265 => args.extend(["-profile:v".to_string(), "main".to_string()]),
                VideoFamily::Av1 => {}
            }
            args.extend([
                "-g".to_string(),
                (2 * fps).to_string(),
                "-forced-idr".to_string(),
                "1".to_string(),
            ]);
            match (bitrate_kbps, crf) {
                (Some(kbps), _) => args.extend(bitrate_args(kbps)),
                (None, Some(crf)) => args.extend([
                    "-rc".to_string(),
                    "vbr".to_string(),
                    "-cq".to_string(),
                    crf.to_string(),
                    "-b:v".to_string(),
                    "0".to_string(),
                ]),
                (None, None) => {}
            }
        }
        EncoderBackend::Vaapi => {
            match family {
                VideoFamily::H264 => args.extend(["-profile:v".to_string(), "high".to_string()]),
                VideoFamily::H265 => args.extend(["-profile:v".to_string(), "main".to_string()]),
                VideoFamily::Av1 => {}
            }
            args.extend(["-g".to_string(), (2 * fps).to_string()]);
            match (bitrate_kbps, crf) {
                (Some(kbps), _) => args.extend(bitrate_args(kbps)),
                (None, Some(crf)) => args.extend(["-qp".to_string(), crf.to_string()]),
                (None, None) => {}
            }
        }
        EncoderBackend::Software => {
            // Software recipes live in `pipeline::ffmpeg`; reaching this
            // arm means a caller routed a CPU encode through the hardware
            // helper -- emit just the encoder so the argv is still valid.
        }
    }
    if family == VideoFamily::H265 {
        // HLS fMP4 players (Safari/iOS) only accept `hvc1`; ffmpeg's
        // default `hev1` plays nowhere on Apple devices.
        args.extend(["-tag:v".to_string(), "hvc1".to_string()]);
    }
    args
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encoder_names_cover_every_backend_and_family() {
        let expected = [
            (
                EncoderBackend::Software,
                ["libx264", "libx265", "libsvtav1"],
            ),
            (
                EncoderBackend::Nvenc,
                ["h264_nvenc", "hevc_nvenc", "av1_nvenc"],
            ),
            (
                EncoderBackend::Vaapi,
                ["h264_vaapi", "hevc_vaapi", "av1_vaapi"],
            ),
        ];
        for (backend, names) in expected {
            for (family, name) in VideoFamily::ALL.iter().zip(names) {
                assert_eq!(backend.encoder_name(*family), name);
            }
        }
    }

    #[test]
    fn backend_labels_are_stable() {
        assert_eq!(EncoderBackend::Software.as_str(), "cpu");
        assert_eq!(EncoderBackend::Nvenc.to_string(), "nvenc");
        assert_eq!(EncoderBackend::Vaapi.to_string(), "vaapi");
    }

    #[test]
    fn preference_parses_and_displays() {
        for (raw, pref) in [
            ("auto", EncoderPreference::Auto),
            ("CPU", EncoderPreference::Cpu),
            ("software", EncoderPreference::Cpu),
            (" nvenc ", EncoderPreference::Nvenc),
            ("vaapi", EncoderPreference::Vaapi),
        ] {
            assert_eq!(raw.parse::<EncoderPreference>().unwrap(), pref);
        }
        assert_eq!(EncoderPreference::default(), EncoderPreference::Auto);
        for pref in [
            EncoderPreference::Auto,
            EncoderPreference::Cpu,
            EncoderPreference::Nvenc,
            EncoderPreference::Vaapi,
        ] {
            assert_eq!(pref.to_string().parse::<EncoderPreference>().unwrap(), pref);
        }
        let err = "qsv".parse::<EncoderPreference>().unwrap_err();
        assert!(err.contains("auto, cpu, nvenc, vaapi"), "{err}");
    }

    #[test]
    fn default_selection_is_all_software() {
        let sel = EncoderSelection::default();
        assert!(sel.is_software_only());
        assert_eq!(sel, EncoderSelection::software());
        for family in VideoFamily::ALL {
            assert_eq!(sel.backend_for(family), EncoderBackend::Software);
        }
        assert!(sel.vaapi_device().is_none());
    }

    #[test]
    fn selection_builders_set_one_family_at_a_time() {
        let sel = EncoderSelection::software()
            .with(VideoFamily::H264, EncoderBackend::Nvenc)
            .with(VideoFamily::H265, EncoderBackend::Vaapi)
            .with_vaapi_device("/dev/dri/renderD129");
        assert!(!sel.is_software_only());
        assert_eq!(sel.backend_for(VideoFamily::H264), EncoderBackend::Nvenc);
        assert_eq!(sel.backend_for(VideoFamily::H265), EncoderBackend::Vaapi);
        assert_eq!(sel.backend_for(VideoFamily::Av1), EncoderBackend::Software);
        assert_eq!(sel.vaapi_device(), Some(Path::new("/dev/dri/renderD129")));
    }

    #[test]
    fn record_metrics_is_safe_without_a_configured_meter_provider() {
        EncoderSelection::software()
            .with(VideoFamily::H264, EncoderBackend::Nvenc)
            .record_metrics();
    }

    /// Self-cleaning scratch directory (no `tempfile` dependency in this crate).
    struct TempDir(PathBuf);

    impl TempDir {
        fn new() -> Self {
            let path = std::env::temp_dir()
                .join(format!("svc-streaming-encoder-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir_all(&path).expect("create scratch dir");
            Self(path)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    // --- device hints ---------------------------------------------------

    fn fake_root(nodes: &[&str]) -> TempDir {
        let root = TempDir::new();
        for node in nodes {
            let path = root.path().join(node.trim_start_matches('/'));
            std::fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
            std::fs::write(&path, b"").expect("touch");
        }
        root
    }

    #[test]
    fn hints_find_nothing_on_a_bare_host() {
        let root = fake_root(&[]);
        assert_eq!(
            DeviceHints::detect_in(root.path(), None),
            DeviceHints::default()
        );
    }

    #[test]
    fn hints_find_an_nvidia_node() {
        let root = fake_root(&["/dev/nvidiactl"]);
        let hints = DeviceHints::detect_in(root.path(), None);
        assert!(hints.nvidia);
        assert!(hints.vaapi_device.is_none());

        let root = fake_root(&["/dev/nvidia0"]);
        assert!(DeviceHints::detect_in(root.path(), None).nvidia);
    }

    #[test]
    fn hints_pick_the_lowest_render_node_and_ignore_card_nodes() {
        let root = fake_root(&[
            "/dev/dri/card0",
            "/dev/dri/renderD129",
            "/dev/dri/renderD128",
        ]);
        let hints = DeviceHints::detect_in(root.path(), None);
        assert_eq!(
            hints.vaapi_device.as_deref(),
            Some(Path::new("/dev/dri/renderD128"))
        );
        assert!(!hints.nvidia);
    }

    #[test]
    fn hints_with_only_card_nodes_find_no_vaapi_device() {
        let root = fake_root(&["/dev/dri/card0"]);
        assert!(DeviceHints::detect_in(root.path(), None)
            .vaapi_device
            .is_none());
    }

    #[test]
    fn hints_honour_an_existing_explicit_vaapi_device() {
        let root = fake_root(&["/dev/dri/renderD128", "/dev/dri/renderD130"]);
        let hints = DeviceHints::detect_in(root.path(), Some(Path::new("/dev/dri/renderD130")));
        assert_eq!(
            hints.vaapi_device.as_deref(),
            Some(Path::new("/dev/dri/renderD130"))
        );
    }

    #[test]
    fn hints_ignore_an_explicit_vaapi_device_that_does_not_exist() {
        let root = fake_root(&["/dev/dri/renderD128"]);
        let hints = DeviceHints::detect_in(root.path(), Some(Path::new("/dev/dri/renderD199")));
        assert!(hints.vaapi_device.is_none());
    }

    #[test]
    fn hints_resolve_a_relative_explicit_device_under_the_root() {
        let root = fake_root(&["/dev/dri/renderD131"]);
        let hints = DeviceHints::detect_in(root.path(), Some(Path::new("dev/dri/renderD131")));
        assert_eq!(
            hints.vaapi_device.as_deref(),
            Some(Path::new("dev/dri/renderD131"))
        );
    }

    #[test]
    fn detect_against_the_real_filesystem_does_not_panic() {
        let _ = DeviceHints::detect(None);
        let _ = DeviceHints::detect(Some(Path::new("/dev/dri/definitely-not-there")));
    }

    #[test]
    fn candidates_follow_preference_and_devices() {
        let both = DeviceHints {
            nvidia: true,
            vaapi_device: Some(PathBuf::from("/dev/dri/renderD128")),
        };
        let none = DeviceHints::default();
        assert_eq!(
            candidate_backends(EncoderPreference::Auto, &both),
            vec![EncoderBackend::Nvenc, EncoderBackend::Vaapi]
        );
        assert_eq!(
            candidate_backends(EncoderPreference::Nvenc, &both),
            vec![EncoderBackend::Nvenc]
        );
        assert_eq!(
            candidate_backends(EncoderPreference::Vaapi, &both),
            vec![EncoderBackend::Vaapi]
        );
        assert!(candidate_backends(EncoderPreference::Cpu, &both).is_empty());
        assert!(candidate_backends(EncoderPreference::Auto, &none).is_empty());
        assert!(candidate_backends(EncoderPreference::Nvenc, &none).is_empty());
        assert!(candidate_backends(EncoderPreference::Vaapi, &none).is_empty());
    }

    // --- listing + trial argv ------------------------------------------

    const LISTING: &str = "Encoders:\n V..... = Video\n A..... = Audio\n ------\n V....D libx264              libx264 H.264 / AVC\n V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)\n V....D hevc_vaapi           H.265/HEVC (VAAPI) (codec hevc)\n A..... aac                  AAC (Advanced Audio Coding)\n";

    #[test]
    fn listing_parser_extracts_names_and_skips_headers() {
        let names = parse_encoder_listing(LISTING);
        assert!(names.contains("libx264"));
        assert!(names.contains("h264_nvenc"));
        assert!(names.contains("hevc_vaapi"));
        assert!(names.contains("aac"));
        assert!(!names.contains("Encoders:"));
        assert!(!names.contains("="));
        assert_eq!(names.len(), 4);
    }

    #[test]
    fn listing_parser_returns_empty_for_garbage() {
        assert!(parse_encoder_listing("").is_empty());
        assert!(parse_encoder_listing("not an encoder listing\nat all").is_empty());
        // Entry-shaped lines without the `------` separator are not trusted.
        assert!(parse_encoder_listing(" V....D libx264  x264\n").is_empty());
        // Header only, no entries.
        assert!(parse_encoder_listing("Encoders:\n V..... = Video\n ------\n").is_empty());
    }

    #[test]
    fn nvenc_trial_has_no_hardware_device_flags() {
        let args = trial_args(EncoderBackend::Nvenc, VideoFamily::H264, None);
        assert!(args.windows(2).any(|w| w == ["-c:v", "h264_nvenc"]));
        assert!(!args.iter().any(|a| a == "-init_hw_device"));
        assert!(!args.iter().any(|a| a == "-vf"));
        assert_eq!(args.last().map(String::as_str), Some("-"));
    }

    #[test]
    fn vaapi_trial_registers_the_device_and_uploads_frames() {
        let args = trial_args(
            EncoderBackend::Vaapi,
            VideoFamily::H265,
            Some(Path::new("/dev/dri/renderD129")),
        );
        assert!(args
            .windows(2)
            .any(|w| w == ["-init_hw_device", "vaapi=va:/dev/dri/renderD129"]));
        assert!(args.windows(2).any(|w| w == ["-filter_hw_device", "va"]));
        assert!(args.windows(2).any(|w| w == ["-vf", VAAPI_UPLOAD_FILTER]));
        assert!(args.windows(2).any(|w| w == ["-c:v", "hevc_vaapi"]));
    }

    #[test]
    fn vaapi_trial_defaults_to_the_first_render_node() {
        let args = trial_args(EncoderBackend::Vaapi, VideoFamily::H264, None);
        assert!(args.iter().any(|a| a == "vaapi=va:/dev/dri/renderD128"));
    }

    #[test]
    fn software_trial_names_the_cpu_encoder() {
        let args = trial_args(EncoderBackend::Software, VideoFamily::Av1, None);
        assert!(args.windows(2).any(|w| w == ["-c:v", "libsvtav1"]));
    }

    #[test]
    fn vaapi_global_args_register_a_named_device() {
        assert_eq!(
            vaapi_global_args(Path::new("/dev/dri/renderD128")),
            vec![
                "-init_hw_device",
                "vaapi=va:/dev/dri/renderD128",
                "-filter_hw_device",
                "va"
            ]
        );
    }

    // --- preset + hardware argv ----------------------------------------

    #[test]
    fn nvenc_preset_maps_x264_names_and_passes_nvenc_names_through() {
        for (software, hardware) in [
            ("ultrafast", "p1"),
            ("superfast", "p1"),
            ("veryfast", "p2"),
            ("faster", "p3"),
            ("fast", "p4"),
            ("medium", "p5"),
            ("slow", "p6"),
            ("slower", "p7"),
            ("veryslow", "p7"),
            ("P3", "p3"),
            ("p7", "p7"),
            ("10", "p4"),
            ("nonsense", "p4"),
        ] {
            assert_eq!(nvenc_preset(software), hardware, "preset {software}");
        }
    }

    #[test]
    fn nvenc_h264_uses_a_mapped_preset_low_latency_tune_and_bitrate() {
        let args = hardware_video_args(
            EncoderBackend::Nvenc,
            VideoFamily::H264,
            "veryfast",
            None,
            Some(2500),
            30,
        );
        assert_eq!(&args[..2], ["-c:v", "h264_nvenc"]);
        assert!(args.windows(2).any(|w| w == ["-preset", "p2"]));
        assert!(args.windows(2).any(|w| w == ["-tune", "ll"]));
        assert!(args.windows(2).any(|w| w == ["-profile:v", "high"]));
        assert!(args.windows(2).any(|w| w == ["-g", "60"]));
        assert!(args.windows(2).any(|w| w == ["-forced-idr", "1"]));
        assert!(args.windows(2).any(|w| w == ["-b:v", "2500k"]));
        assert!(args.windows(2).any(|w| w == ["-bufsize", "5000k"]));
        assert!(!args.iter().any(|a| a == "-tag:v"));
        // `veryfast` is not an NVENC preset and must never leak through.
        assert!(!args.iter().any(|a| a == "veryfast"));
    }

    #[test]
    fn nvenc_hevc_tags_hvc1_and_uses_cq_when_only_crf_is_set() {
        let args = hardware_video_args(
            EncoderBackend::Nvenc,
            VideoFamily::H265,
            "fast",
            Some(23),
            None,
            25,
        );
        assert_eq!(&args[..2], ["-c:v", "hevc_nvenc"]);
        assert!(args.windows(2).any(|w| w == ["-profile:v", "main"]));
        assert!(args.windows(2).any(|w| w == ["-rc", "vbr"]));
        assert!(args.windows(2).any(|w| w == ["-cq", "23"]));
        assert!(args.windows(2).any(|w| w == ["-b:v", "0"]));
        assert!(args.windows(2).any(|w| w == ["-g", "50"]));
        assert!(args.windows(2).any(|w| w == ["-tag:v", "hvc1"]));
    }

    #[test]
    fn nvenc_av1_has_no_profile_and_no_rate_control_by_default() {
        let args = hardware_video_args(
            EncoderBackend::Nvenc,
            VideoFamily::Av1,
            "10",
            None,
            None,
            30,
        );
        assert_eq!(&args[..2], ["-c:v", "av1_nvenc"]);
        assert!(!args.iter().any(|a| a == "-profile:v"));
        assert!(!args.iter().any(|a| a == "-b:v"));
        assert!(!args.iter().any(|a| a == "-tag:v"));
    }

    #[test]
    fn vaapi_h264_ignores_the_software_preset() {
        let args = hardware_video_args(
            EncoderBackend::Vaapi,
            VideoFamily::H264,
            "veryfast",
            None,
            Some(4000),
            30,
        );
        assert_eq!(&args[..2], ["-c:v", "h264_vaapi"]);
        assert!(!args.iter().any(|a| a == "-preset"));
        assert!(args.windows(2).any(|w| w == ["-profile:v", "high"]));
        assert!(args.windows(2).any(|w| w == ["-b:v", "4000k"]));
    }

    #[test]
    fn vaapi_hevc_uses_qp_for_crf_and_tags_hvc1() {
        let args = hardware_video_args(
            EncoderBackend::Vaapi,
            VideoFamily::H265,
            "fast",
            Some(26),
            None,
            30,
        );
        assert_eq!(&args[..2], ["-c:v", "hevc_vaapi"]);
        assert!(args.windows(2).any(|w| w == ["-profile:v", "main"]));
        assert!(args.windows(2).any(|w| w == ["-qp", "26"]));
        assert!(args.windows(2).any(|w| w == ["-tag:v", "hvc1"]));
    }

    #[test]
    fn vaapi_av1_is_just_the_encoder_and_keyframe_interval() {
        let args = hardware_video_args(
            EncoderBackend::Vaapi,
            VideoFamily::Av1,
            "10",
            None,
            None,
            30,
        );
        assert_eq!(args, ["-c:v", "av1_vaapi", "-g", "60"]);
    }

    #[test]
    fn routing_a_cpu_encode_through_the_hardware_helper_is_still_valid_argv() {
        let args = hardware_video_args(
            EncoderBackend::Software,
            VideoFamily::H264,
            "veryfast",
            None,
            None,
            30,
        );
        assert_eq!(args, ["-c:v", "libx264"]);
    }

    // --- detection against a fake ffmpeg --------------------------------

    #[cfg(unix)]
    mod probe {
        use super::*;
        use std::os::unix::fs::PermissionsExt;

        /// Writes an executable shell script standing in for ffmpeg.
        /// `-encoders` prints `listing`; any other invocation exits 0 iff
        /// its `-c:v` encoder is in `working`, else prints a diagnostic and
        /// exits 1 -- the same shape a real driver failure has.
        fn fake_ffmpeg(dir: &Path, listing: &str, working: &[&str]) -> PathBuf {
            let path = dir.join("ffmpeg");
            let ok_cases: String = working
                .iter()
                .map(|name| format!("    {name}) exit 0 ;;\n"))
                .collect();
            let body = format!(
                "#!/bin/sh\n\
                 for a in \"$@\"; do\n\
                   if [ \"$a\" = \"-encoders\" ]; then\n\
                     cat <<'LISTING'\nEncoders:\n V..... = Video\n ------\n{listing}LISTING\n\
                     exit 0\n\
                   fi\n\
                 done\n\
                 enc=\"\"\n\
                 prev=\"\"\n\
                 for a in \"$@\"; do\n\
                   if [ \"$prev\" = \"-c:v\" ]; then enc=\"$a\"; fi\n\
                   prev=\"$a\"\n\
                 done\n\
                 case \"$enc\" in\n{ok_cases}\
                   *) echo 'Failed to initialise hardware device' >&2; exit 1 ;;\n\
                 esac\n"
            );
            std::fs::write(&path, body).expect("write fake ffmpeg");
            let mut perms = std::fs::metadata(&path).expect("stat").permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&path, perms).expect("chmod");
            path
        }

        fn nvidia_hints() -> DeviceHints {
            DeviceHints {
                nvidia: true,
                vaapi_device: None,
            }
        }

        fn vaapi_hints() -> DeviceHints {
            DeviceHints {
                nvidia: false,
                vaapi_device: Some(PathBuf::from("/dev/dri/renderD128")),
            }
        }

        #[tokio::test]
        async fn gpu_branch_selects_nvenc_per_codec_when_trial_encodes_pass() {
            let dir = TempDir::new();
            let ffmpeg = fake_ffmpeg(
                dir.path(),
                " V....D h264_nvenc  NVENC\n V....D hevc_nvenc  NVENC\n V....D libx264 x264\n",
                &["h264_nvenc", "hevc_nvenc"],
            );
            let sel = detect_encoders(&ffmpeg, EncoderPreference::Auto, &nvidia_hints()).await;
            assert_eq!(sel.backend_for(VideoFamily::H264), EncoderBackend::Nvenc);
            assert_eq!(sel.backend_for(VideoFamily::H265), EncoderBackend::Nvenc);
            // This ffmpeg has no av1_nvenc (like Debian 5.1.x): CPU for AV1.
            assert_eq!(sel.backend_for(VideoFamily::Av1), EncoderBackend::Software);
            assert!(!sel.is_software_only());
        }

        #[tokio::test]
        async fn cpu_fallback_when_the_device_exists_but_the_trial_encode_fails() {
            let dir = TempDir::new();
            // Encoder is compiled in (listed) but the driver is unusable.
            let ffmpeg = fake_ffmpeg(
                dir.path(),
                " V....D h264_vaapi  VAAPI\n V....D hevc_vaapi  VAAPI\n",
                &[],
            );
            let sel = detect_encoders(&ffmpeg, EncoderPreference::Auto, &vaapi_hints()).await;
            assert!(sel.is_software_only());
            assert!(sel.vaapi_device().is_none());
        }

        #[tokio::test]
        async fn vaapi_branch_records_the_render_node() {
            let dir = TempDir::new();
            let ffmpeg = fake_ffmpeg(dir.path(), " V....D h264_vaapi  VAAPI\n", &["h264_vaapi"]);
            let sel = detect_encoders(&ffmpeg, EncoderPreference::Auto, &vaapi_hints()).await;
            assert_eq!(sel.backend_for(VideoFamily::H264), EncoderBackend::Vaapi);
            assert_eq!(sel.backend_for(VideoFamily::H265), EncoderBackend::Software);
            assert_eq!(sel.vaapi_device(), Some(Path::new("/dev/dri/renderD128")));
        }

        #[tokio::test]
        async fn auto_falls_through_a_broken_nvenc_to_a_working_vaapi() {
            let dir = TempDir::new();
            let ffmpeg = fake_ffmpeg(
                dir.path(),
                " V....D h264_nvenc  NVENC\n V....D h264_vaapi  VAAPI\n",
                &["h264_vaapi"],
            );
            let hints = DeviceHints {
                nvidia: true,
                vaapi_device: Some(PathBuf::from("/dev/dri/renderD128")),
            };
            let sel = detect_encoders(&ffmpeg, EncoderPreference::Auto, &hints).await;
            assert_eq!(sel.backend_for(VideoFamily::H264), EncoderBackend::Vaapi);
        }

        #[tokio::test]
        async fn encoder_missing_from_the_build_is_skipped_without_a_trial() {
            let dir = TempDir::new();
            // `h264_nvenc` would pass a trial, but is not listed as compiled in.
            let ffmpeg = fake_ffmpeg(dir.path(), " V....D libx264  x264\n", &["h264_nvenc"]);
            let sel = detect_encoders(&ffmpeg, EncoderPreference::Auto, &nvidia_hints()).await;
            assert!(sel.is_software_only());
        }

        #[tokio::test]
        async fn explicit_cpu_preference_never_spawns_ffmpeg() {
            // A path that cannot be executed proves nothing was spawned.
            let sel = detect_encoders(
                Path::new("/nonexistent/ffmpeg"),
                EncoderPreference::Cpu,
                &nvidia_hints(),
            )
            .await;
            assert!(sel.is_software_only());
        }

        #[tokio::test]
        async fn no_device_hints_means_cpu_without_spawning_ffmpeg() {
            let sel = detect_encoders(
                Path::new("/nonexistent/ffmpeg"),
                EncoderPreference::Auto,
                &DeviceHints::default(),
            )
            .await;
            assert!(sel.is_software_only());
        }

        #[tokio::test]
        async fn explicit_gpu_preference_without_its_device_falls_back_to_cpu() {
            let sel = detect_encoders(
                Path::new("/nonexistent/ffmpeg"),
                EncoderPreference::Nvenc,
                &vaapi_hints(),
            )
            .await;
            assert!(sel.is_software_only());
        }

        #[tokio::test]
        async fn unrunnable_ffmpeg_degrades_to_cpu() {
            let sel = detect_encoders(
                Path::new("/nonexistent/ffmpeg"),
                EncoderPreference::Auto,
                &nvidia_hints(),
            )
            .await;
            assert!(sel.is_software_only());
        }

        #[tokio::test]
        async fn empty_encoder_listing_degrades_to_cpu() {
            let dir = TempDir::new();
            let ffmpeg = fake_ffmpeg(dir.path(), "", &["h264_nvenc"]);
            let sel = detect_encoders(&ffmpeg, EncoderPreference::Auto, &nvidia_hints()).await;
            assert!(sel.is_software_only());
        }

        #[tokio::test]
        async fn failing_listing_command_degrades_to_cpu() {
            let dir = TempDir::new();
            let path = dir.path().join("ffmpeg");
            std::fs::write(&path, "#!/bin/sh\nexit 3\n").expect("write");
            let mut perms = std::fs::metadata(&path).expect("stat").permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&path, perms).expect("chmod");
            let sel = detect_encoders(&path, EncoderPreference::Auto, &nvidia_hints()).await;
            assert!(sel.is_software_only());
        }
    }
}
