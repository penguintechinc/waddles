//! Static per-output HLS configuration: disk layout + the `ffmpeg` argv
//! fragment for one `OutputSpec::Hls` target (spec (issue #287) S7 §4).
//! Deliberately synchronous and independent of `HlsSink`'s `OutputSink`
//! lifecycle (`super`) -- `pipeline::ffmpeg`'s (S3) argv builder needs this
//! fragment *before* the ffmpeg process is even spawned, while
//! `HlsSink::start` only takes over supervision once ffmpeg is already
//! producing segments into the directory this module computes.

use std::path::{Path, PathBuf};

use crate::pipeline::model::{HlsVariant, PipelineId};

/// Segment target duration (seconds), std variant (`-hls_time`).
const SEGMENT_TIME_SECONDS: &str = "4";
/// Rolling playlist window (`-hls_list_size`) -- segments kept referenced
/// in the live playlist; older segments are deleted (`delete_segments`
/// flag) once the window slides past them.
const PLAYLIST_WINDOW_SEGMENTS: &str = "6";
/// LL-HLS partial-segment duration (`-hls_part_time`), seconds.
const LL_PART_TIME_SECONDS: &str = "0.5";

/// `pub(crate)` (not private) -- `super::serve` needs both names: to point
/// the `GET /live/{community_id}` listing at the always-playable media
/// playlist, and to detect+synthesize a `master.m3u8` request against an
/// on-disk file ffmpeg wrote before it knew its own stream parameters (see
/// `serve::synthesize_master_playlist`'s doc comment).
pub(crate) const MASTER_PLAYLIST_NAME: &str = "master.m3u8";
pub(crate) const MEDIA_PLAYLIST_NAME: &str = "index.m3u8";
const INIT_SEGMENT_NAME: &str = "init.mp4";
const SEGMENT_FILENAME_PATTERN: &str = "segment_%05d.m4s";

/// `{STREAM_DATA_DIR}/hls` -- the root every pipeline's HLS output lives
/// under. Shared by [`HlsOutputTarget::directory`] (sink, writes) and
/// `super::serve` (server, reads) so both sides of this chunk compute the
/// identical layout from a single definition.
pub(crate) fn hls_root(data_dir: &Path) -> PathBuf {
    data_dir.join("hls")
}

/// True if `segment` is safe to use as a single filesystem path component:
/// non-empty, no path separator, no `.`/`..` traversal sentinel, no NUL.
/// Applied to every dynamic path component (both the sink's `profile` name
/// and the server's `community_id`/`pipeline_id`/`profile`/`filename` path
/// params) before it touches the filesystem -- `rules/security.md` Input
/// Validation.
pub(crate) fn is_safe_path_segment(segment: &str) -> bool {
    !segment.is_empty()
        && segment != "."
        && segment != ".."
        && !segment.contains('/')
        && !segment.contains('\\')
        && !segment.contains('\0')
}

/// One HLS output's disk location + delivery variant. Computing this and
/// calling [`Self::ffmpeg_output_args`] requires no async runtime.
///
/// `output_dir` is keyed by `pipeline_id`/`profile` only, *not*
/// `community_id` -- the frozen `OutputSink::start(pipeline_id, spec)`
/// signature (`egress::mod`, owned by S1) never receives `community_id`, so
/// this module can't key the disk layout by it. `egress::hls::serve`
/// resolves `community_id` -> running `pipeline_id`s via
/// [`super::RunningPipelines`] at the HTTP layer instead, independent of
/// this disk layout; see `HlsSink::register_pipeline`'s doc comment for how
/// `community_id` is threaded through the sink's own lifecycle without
/// widening the trait signature.
#[derive(Debug, Clone)]
pub struct HlsOutputTarget {
    pub output_dir: PathBuf,
    pub variant: HlsVariant,
}

impl HlsOutputTarget {
    /// Computes (without creating) the output directory for `pipeline_id`'s
    /// `profile` under `data_dir` (`STREAM_DATA_DIR`).
    pub fn directory(data_dir: &Path, pipeline_id: PipelineId, profile: &str) -> PathBuf {
        hls_root(data_dir)
            .join(pipeline_id.to_string())
            .join(profile)
    }

    pub fn new(
        data_dir: &Path,
        pipeline_id: PipelineId,
        profile: &str,
        variant: HlsVariant,
    ) -> Self {
        Self {
            output_dir: Self::directory(data_dir, pipeline_id, profile),
            variant,
        }
    }

    /// The primary/media playlist ffmpeg writes to -- its trailing
    /// positional output argument, see [`Self::ffmpeg_output_args`].
    pub fn media_playlist_path(&self) -> PathBuf {
        self.output_dir.join(MEDIA_PLAYLIST_NAME)
    }

    /// The hls muxer options for this target as ordered `(name, value)`
    /// pairs (names without the leading dash): the single source of truth
    /// for both the standalone argv ([`Self::ffmpeg_output_args`]) and the
    /// per-slave option list inside `-f tee` (`pipeline::ffmpeg`'s
    /// `tee_slave`), so the two shapes cannot drift apart.
    pub fn muxer_options(&self) -> Vec<(&'static str, String)> {
        let dir = self.output_dir.display().to_string();
        let mut hls_flags = "delete_segments+independent_segments".to_string();
        let is_ll = self.variant == HlsVariant::Ll;

        let mut options: Vec<(&'static str, String)> = vec![
            ("hls_time", SEGMENT_TIME_SECONDS.into()),
            ("hls_list_size", PLAYLIST_WINDOW_SEGMENTS.into()),
        ];

        if is_ll {
            options.push(("hls_playlist_type", "event".into()));
            options.push(("hls_part_time", LL_PART_TIME_SECONDS.into()));
            hls_flags.push_str("+program_date_time");
        }

        options.push(("hls_flags", hls_flags));
        options.push(("hls_segment_type", "fmp4".into()));
        options.push(("hls_fmp4_init_filename", INIT_SEGMENT_NAME.into()));
        options.push((
            "hls_segment_filename",
            format!("{dir}/{SEGMENT_FILENAME_PATTERN}"),
        ));
        options.push(("master_pl_name", MASTER_PLAYLIST_NAME.into()));

        if is_ll {
            options.push(("lhls", "1".into()));
        }
        options
    }

    /// Builds the `ffmpeg` argv fragment for this target per spec §4 -- std
    /// HLS (fmp4 segments, 4s/6-segment rolling window) or LL-HLS (adds
    /// partial segments + `program_date_time` + `-lhls 1`) depending on
    /// [`Self::variant`]. `pipeline::ffmpeg`'s (S3) builder appends this
    /// after the shared input/encode options; the final element is the
    /// positional output path (the media playlist), matching ffmpeg's argv
    /// convention of a trailing output file.
    pub fn ffmpeg_output_args(&self) -> Vec<String> {
        let mut args: Vec<String> = vec!["-f".into(), "hls".into()];
        for (name, value) in self.muxer_options() {
            args.push(format!("-{name}"));
            args.push(value);
        }
        args.push(self.media_playlist_path().display().to_string());
        args
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;

    fn target(variant: HlsVariant) -> HlsOutputTarget {
        HlsOutputTarget::new(Path::new("/data"), Uuid::nil(), "1080p60", variant)
    }

    #[test]
    fn directory_layout_is_pipeline_then_profile() {
        let dir = HlsOutputTarget::directory(Path::new("/data"), Uuid::nil(), "1080p60");
        assert_eq!(
            dir,
            PathBuf::from(format!("/data/hls/{}/1080p60", Uuid::nil()))
        );
    }

    #[test]
    fn std_args_match_spec_and_omit_ll_flags() {
        let args = target(HlsVariant::Std).ffmpeg_output_args();
        assert!(args.windows(2).any(|w| w == ["-hls_time", "4"]));
        assert!(args.windows(2).any(|w| w == ["-hls_list_size", "6"]));
        assert!(args
            .iter()
            .any(|a| a == "delete_segments+independent_segments"));
        assert!(args.windows(2).any(|w| w == ["-hls_segment_type", "fmp4"]));
        assert!(args
            .windows(2)
            .any(|w| w == ["-master_pl_name", "master.m3u8"]));
        assert!(!args.iter().any(|a| a == "-lhls"));
        assert!(!args.iter().any(|a| a == "-hls_playlist_type"));
        assert!(args.last().unwrap().ends_with("/index.m3u8"));
    }

    #[test]
    fn ll_args_add_low_latency_flags() {
        let args = target(HlsVariant::Ll).ffmpeg_output_args();
        assert!(args
            .windows(2)
            .any(|w| w == ["-hls_playlist_type", "event"]));
        assert!(args.windows(2).any(|w| w == ["-hls_part_time", "0.5"]));
        assert!(args
            .iter()
            .any(|a| a == "delete_segments+independent_segments+program_date_time"));
        assert!(args.windows(2).any(|w| w == ["-lhls", "1"]));
    }

    #[test]
    fn muxer_options_and_argv_never_drift() {
        for variant in [HlsVariant::Std, HlsVariant::Ll] {
            let t = target(variant);
            let args = t.ffmpeg_output_args();
            let rebuilt: Vec<String> = ["-f".to_string(), "hls".to_string()]
                .into_iter()
                .chain(
                    t.muxer_options()
                        .into_iter()
                        .flat_map(|(k, v)| [format!("-{k}"), v]),
                )
                .chain(std::iter::once(
                    t.media_playlist_path().display().to_string(),
                ))
                .collect();
            assert_eq!(args, rebuilt);
        }
    }

    #[test]
    fn muxer_options_are_named_without_a_dash_and_keep_the_documented_order() {
        let names: Vec<&str> = target(HlsVariant::Std)
            .muxer_options()
            .into_iter()
            .map(|(k, _)| k)
            .collect();
        assert_eq!(
            names,
            [
                "hls_time",
                "hls_list_size",
                "hls_flags",
                "hls_segment_type",
                "hls_fmp4_init_filename",
                "hls_segment_filename",
                "master_pl_name"
            ]
        );
        assert!(target(HlsVariant::Ll)
            .muxer_options()
            .iter()
            .any(|(k, v)| *k == "lhls" && v == "1"));
    }

    #[test]
    fn segment_filename_pattern_is_under_output_dir() {
        let args = target(HlsVariant::Std).ffmpeg_output_args();
        let idx = args
            .iter()
            .position(|a| a == "-hls_segment_filename")
            .unwrap();
        assert!(args[idx + 1].starts_with("/data/hls/"));
        assert!(args[idx + 1].ends_with("segment_%05d.m4s"));
    }

    #[test]
    fn hls_root_appends_hls_segment() {
        assert_eq!(hls_root(Path::new("/data")), PathBuf::from("/data/hls"));
    }

    #[test]
    fn is_safe_path_segment_rejects_traversal_and_separators() {
        assert!(!is_safe_path_segment(".."));
        assert!(!is_safe_path_segment("."));
        assert!(!is_safe_path_segment(""));
        assert!(!is_safe_path_segment("a/b"));
        assert!(!is_safe_path_segment("a\\b"));
        assert!(!is_safe_path_segment("a\0b"));
        assert!(is_safe_path_segment("master.m3u8"));
        assert!(is_safe_path_segment("1080p60"));
    }
}
