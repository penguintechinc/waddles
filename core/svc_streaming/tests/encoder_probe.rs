//! GPU-preferred / CPU-fallback detection against a **fake ffmpeg**: a shell
//! script that lists encoders and passes or fails trial encodes the way a
//! real driver would. Exercises `detect_encoders` end to end through real
//! process spawns on both branches (GPU proven usable, GPU present but
//! unusable -> CPU) without needing a GPU.
//!
//! Lives in its own integration-test binary, with a lock serializing the
//! tests, because write-script-then-exec races with any other thread that
//! forks in the same process (the child inherits the writable fd until it
//! execs, so the exec of the script fails with `ETXTBSY`). Keeping these
//! out of the lib's unit-test binary also keeps them from perturbing the
//! supervisor tests that spawn real children.

#![cfg(unix)]

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use svc_streaming::pipeline::{
    detect_encoders, DeviceHints, EncoderBackend, EncoderPreference, VideoFamily,
};

/// One test at a time: see the module docs.
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// Self-cleaning scratch directory (no `tempfile` dependency in this crate).
struct TempDir(PathBuf);

impl TempDir {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "svc-streaming-encoder-probe-{}",
            uuid::Uuid::new_v4()
        ));
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
    let _serial = SERIAL.lock().await;
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
    let _serial = SERIAL.lock().await;
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
    let _serial = SERIAL.lock().await;
    let dir = TempDir::new();
    let ffmpeg = fake_ffmpeg(dir.path(), " V....D h264_vaapi  VAAPI\n", &["h264_vaapi"]);
    let sel = detect_encoders(&ffmpeg, EncoderPreference::Auto, &vaapi_hints()).await;
    assert_eq!(sel.backend_for(VideoFamily::H264), EncoderBackend::Vaapi);
    assert_eq!(sel.backend_for(VideoFamily::H265), EncoderBackend::Software);
    assert_eq!(sel.vaapi_device(), Some(Path::new("/dev/dri/renderD128")));
}

#[tokio::test]
async fn auto_falls_through_a_broken_nvenc_to_a_working_vaapi() {
    let _serial = SERIAL.lock().await;
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
    let _serial = SERIAL.lock().await;
    let dir = TempDir::new();
    // `h264_nvenc` would pass a trial, but is not listed as compiled in.
    let ffmpeg = fake_ffmpeg(dir.path(), " V....D libx264  x264\n", &["h264_nvenc"]);
    let sel = detect_encoders(&ffmpeg, EncoderPreference::Auto, &nvidia_hints()).await;
    assert!(sel.is_software_only());
}

#[tokio::test]
async fn explicit_cpu_preference_never_spawns_ffmpeg() {
    let _serial = SERIAL.lock().await;
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
    let _serial = SERIAL.lock().await;
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
    let _serial = SERIAL.lock().await;
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
    let _serial = SERIAL.lock().await;
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
    let _serial = SERIAL.lock().await;
    let dir = TempDir::new();
    let ffmpeg = fake_ffmpeg(dir.path(), "", &["h264_nvenc"]);
    let sel = detect_encoders(&ffmpeg, EncoderPreference::Auto, &nvidia_hints()).await;
    assert!(sel.is_software_only());
}

#[tokio::test]
async fn failing_listing_command_degrades_to_cpu() {
    let _serial = SERIAL.lock().await;
    let dir = TempDir::new();
    let path = dir.path().join("ffmpeg");
    std::fs::write(&path, "#!/bin/sh\nexit 3\n").expect("write");
    let mut perms = std::fs::metadata(&path).expect("stat").permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&path, perms).expect("chmod");
    let sel = detect_encoders(&path, EncoderPreference::Auto, &nvidia_hints()).await;
    assert!(sel.is_software_only());
}
