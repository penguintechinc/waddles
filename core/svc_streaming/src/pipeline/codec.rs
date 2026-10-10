//! Output codec selection and container compatibility rules.
//!
//! The API/DB speak in short codec names (`h264`/`h265`/`av1`,
//! `copy`/`aac`/`opus`); [`crate::pipeline::model`] speaks in
//! [`VideoCodec`]/[`AudioCodec`] recipes; `pipeline::ffmpeg` turns those
//! into an argv. This module is the one place the three vocabularies meet,
//! and the one place that knows which codec/container pairs the bundled
//! `ffmpeg` (Debian 5.1.x) can actually mux -- so an unmuxable request is
//! rejected loudly at validation time instead of being dropped silently
//! by `ffmpeg -f tee` at runtime (verified against the runtime image:
//! a `tee` slave whose header write fails is skipped with the process
//! still exiting 0, even with the default `onfail=abort`).
//!
//! # Compatibility matrix (ffmpeg 5.1.x)
//!
//! | Container            | H.264 | H.265 | AV1 | AAC | Opus |
//! |----------------------|-------|-------|-----|-----|------|
//! | FLV (RTMP push)      | yes   | no    | no  | yes | no   |
//! | MPEG-TS (SRT push)   | yes   | yes   | no  | yes | yes  |
//! | fMP4 (HLS, mp4 rec.) | yes   | yes   | yes | yes | yes  |
//!
//! FLV needs enhanced-RTMP for HEVC/AV1 and MPEG-TS needs a muxer that
//! tags AV1 properly (ffmpeg 5.1 writes it as an opaque private data
//! stream no player can decode); both land with a newer ffmpeg build.
//! When that build lands, flip the matching arm in
//! [`check_video`]/[`check_audio`] -- nothing else changes.

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

use crate::pipeline::model::{AudioCodec, VideoCodec};

/// Longest user-supplied value echoed back inside an error message --
/// keeps a hostile oversized request body from bloating logs/responses.
const MAX_ECHO_CHARS: usize = 32;

/// Default AAC/Opus bitrate (kbps) used when a profile requests a
/// re-encode of audio without a bitrate of its own.
pub const DEFAULT_AUDIO_BITRATE_KBPS: u32 = 128;

/// Video codec family a user can select. Distinct from [`VideoCodec`]
/// (which also models `Copy` and carries encoder tuning): this is only the
/// *what*, never the *how* -- whether it is encoded on a GPU or the CPU is
/// decided later by `pipeline::encoder`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VideoFamily {
    H264,
    H265,
    Av1,
}

impl VideoFamily {
    /// Every selectable family, in the order they are documented.
    pub const ALL: [VideoFamily; 3] = [VideoFamily::H264, VideoFamily::H265, VideoFamily::Av1];

    /// The wire/DB spelling (`h264`/`h265`/`av1`).
    pub fn as_str(self) -> &'static str {
        match self {
            VideoFamily::H264 => "h264",
            VideoFamily::H265 => "h265",
            VideoFamily::Av1 => "av1",
        }
    }

    /// Human label used in error messages.
    pub fn label(self) -> &'static str {
        match self {
            VideoFamily::H264 => "H.264",
            VideoFamily::H265 => "HEVC",
            VideoFamily::Av1 => "AV1",
        }
    }

    /// The software encoder preset used when a caller does not choose one:
    /// x264/x265 speak `veryfast`; SVT-AV1 only understands numeric
    /// presets (passing `veryfast` makes it refuse to start).
    pub fn default_preset(self) -> &'static str {
        match self {
            VideoFamily::H264 | VideoFamily::H265 => "veryfast",
            VideoFamily::Av1 => "10",
        }
    }

    /// Builds the [`VideoCodec`] recipe for this family at `bitrate_kbps`.
    pub fn to_video_codec(self, bitrate_kbps: Option<u32>) -> VideoCodec {
        let preset = self.default_preset().to_string();
        match self {
            VideoFamily::H264 => VideoCodec::H264 {
                preset,
                crf: None,
                bitrate_kbps,
            },
            VideoFamily::H265 => VideoCodec::H265 {
                preset,
                crf: None,
                bitrate_kbps,
            },
            VideoFamily::Av1 => VideoCodec::Av1Svt {
                preset,
                crf: None,
                bitrate_kbps,
            },
        }
    }

    /// The family of an existing recipe, or `None` for passthrough
    /// ([`VideoCodec::Copy`]) whose real codec is only known at runtime.
    pub fn of(codec: &VideoCodec) -> Option<VideoFamily> {
        match codec {
            VideoCodec::Copy => None,
            VideoCodec::H264 { .. } => Some(VideoFamily::H264),
            VideoCodec::H265 { .. } => Some(VideoFamily::H265),
            VideoCodec::Av1Svt { .. } => Some(VideoFamily::Av1),
        }
    }
}

impl fmt::Display for VideoFamily {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for VideoFamily {
    type Err = CodecError;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "h264" => Ok(VideoFamily::H264),
            "h265" | "hevc" => Ok(VideoFamily::H265),
            "av1" => Ok(VideoFamily::Av1),
            _ => Err(CodecError::UnknownVideoCodec(echo(raw))),
        }
    }
}

/// Audio handling a user can select.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AudioChoice {
    /// Passthrough -- the ingest audio is forwarded untouched.
    Copy,
    Aac,
    Opus,
}

impl AudioChoice {
    /// The wire/DB spelling (`copy`/`aac`/`opus`).
    pub fn as_str(self) -> &'static str {
        match self {
            AudioChoice::Copy => "copy",
            AudioChoice::Aac => "aac",
            AudioChoice::Opus => "opus",
        }
    }

    /// Builds the [`AudioCodec`] recipe for this choice.
    pub fn to_audio_codec(self) -> AudioCodec {
        match self {
            AudioChoice::Copy => AudioCodec::Copy,
            AudioChoice::Aac => AudioCodec::Aac {
                bitrate_kbps: DEFAULT_AUDIO_BITRATE_KBPS,
            },
            AudioChoice::Opus => AudioCodec::Opus {
                bitrate_kbps: DEFAULT_AUDIO_BITRATE_KBPS,
            },
        }
    }

    /// The choice an existing recipe corresponds to.
    pub fn of(codec: &AudioCodec) -> AudioChoice {
        match codec {
            AudioCodec::Copy => AudioChoice::Copy,
            AudioCodec::Aac { .. } => AudioChoice::Aac,
            AudioCodec::Opus { .. } => AudioChoice::Opus,
        }
    }
}

impl fmt::Display for AudioChoice {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for AudioChoice {
    type Err = CodecError;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "copy" => Ok(AudioChoice::Copy),
            "aac" => Ok(AudioChoice::Aac),
            "opus" => Ok(AudioChoice::Opus),
            _ => Err(CodecError::UnknownAudioCodec(echo(raw))),
        }
    }
}

/// The push protocol of a `streaming_targets` row. It decides the
/// container ffmpeg muxes ([`TargetProtocol::format`]), and therefore
/// which codecs the target can carry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TargetProtocol {
    /// `rtmp://` or `rtmps://` -- FLV container.
    Rtmp,
    /// `srt://` -- MPEG-TS container.
    Srt,
}

impl TargetProtocol {
    /// The wire/DB spelling (`rtmp`/`srt`).
    pub fn as_str(self) -> &'static str {
        match self {
            TargetProtocol::Rtmp => "rtmp",
            TargetProtocol::Srt => "srt",
        }
    }

    /// The container ffmpeg muxes for this protocol.
    pub fn format(self) -> OutputFormat {
        match self {
            TargetProtocol::Rtmp => OutputFormat::Flv,
            TargetProtocol::Srt => OutputFormat::MpegTs,
        }
    }
}

impl fmt::Display for TargetProtocol {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for TargetProtocol {
    type Err = CodecError;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "rtmp" => Ok(TargetProtocol::Rtmp),
            "srt" => Ok(TargetProtocol::Srt),
            _ => Err(CodecError::UnknownProtocol(echo(raw))),
        }
    }
}

/// A container an output muxes into.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum OutputFormat {
    /// RTMP push.
    Flv,
    /// SRT push, and the `.ts` recording segments.
    MpegTs,
    /// HLS with fMP4 segments.
    HlsFmp4,
    /// Single-file MP4 (a recording inside a `tee` group).
    Mp4,
}

/// Why a codec choice is not usable. The `Display` text of the
/// incompatibility variants is part of the API contract -- it is surfaced
/// verbatim to callers as a `400`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CodecError {
    #[error("unknown video_codec {0:?}; expected one of h264, h265, av1")]
    UnknownVideoCodec(String),
    #[error("unknown audio_codec {0:?}; expected one of copy, aac, opus")]
    UnknownAudioCodec(String),
    #[error("unknown protocol {0:?}; expected one of rtmp, srt")]
    UnknownProtocol(String),
    #[error("{} requested: FLV/RTMP cannot carry HEVC/AV1 (needs enhanced-RTMP); use HLS/SRT or H.264", .0.label())]
    RtmpVideo(VideoFamily),
    #[error("opus requested: FLV/RTMP cannot carry Opus audio; use aac or copy for RTMP targets")]
    RtmpAudio,
    #[error("av1 requested: MPEG-TS/SRT cannot carry AV1 with the bundled ffmpeg 5.1 (it muxes AV1 as an unreadable private data stream; needs ffmpeg 6.1+); use HLS for AV1, or h264/h265 for SRT")]
    SrtAv1,
}

/// Truncates a user-supplied value for inclusion in an error message.
fn echo(raw: &str) -> String {
    raw.chars().take(MAX_ECHO_CHARS).collect()
}

/// Checks that `video` can be muxed into `format`. `None` (passthrough) is
/// always accepted: the real codec of a copied stream is only known at
/// runtime, and the ingest protocols this service accepts deliver H.264.
pub fn check_video(format: OutputFormat, video: Option<VideoFamily>) -> Result<(), CodecError> {
    match (format, video) {
        (_, None) => Ok(()),
        (OutputFormat::Flv, Some(VideoFamily::H264)) => Ok(()),
        (OutputFormat::Flv, Some(other)) => Err(CodecError::RtmpVideo(other)),
        (OutputFormat::MpegTs, Some(VideoFamily::Av1)) => Err(CodecError::SrtAv1),
        (OutputFormat::MpegTs, Some(_)) => Ok(()),
        (OutputFormat::HlsFmp4 | OutputFormat::Mp4, Some(_)) => Ok(()),
    }
}

/// Checks that `audio` can be muxed into `format`.
pub fn check_audio(format: OutputFormat, audio: AudioChoice) -> Result<(), CodecError> {
    match (format, audio) {
        (OutputFormat::Flv, AudioChoice::Opus) => Err(CodecError::RtmpAudio),
        _ => Ok(()),
    }
}

/// Checks a full video + audio pair against `format`.
pub fn check_pair(
    format: OutputFormat,
    video: Option<VideoFamily>,
    audio: AudioChoice,
) -> Result<(), CodecError> {
    check_video(format, video)?;
    check_audio(format, audio)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn video_family_parses_every_spelling_case_insensitively() {
        assert_eq!("h264".parse::<VideoFamily>().unwrap(), VideoFamily::H264);
        assert_eq!(" H265 ".parse::<VideoFamily>().unwrap(), VideoFamily::H265);
        assert_eq!("HEVC".parse::<VideoFamily>().unwrap(), VideoFamily::H265);
        assert_eq!("av1".parse::<VideoFamily>().unwrap(), VideoFamily::Av1);
    }

    #[test]
    fn video_family_rejects_unknown_and_truncates_the_echo() {
        let long = "x".repeat(500);
        let err = long.parse::<VideoFamily>().unwrap_err();
        match &err {
            CodecError::UnknownVideoCodec(echoed) => assert_eq!(echoed.chars().count(), 32),
            other => panic!("unexpected error {other:?}"),
        }
        assert!(err.to_string().contains("expected one of h264, h265, av1"));
        assert!("vp9".parse::<VideoFamily>().is_err());
    }

    #[test]
    fn video_family_round_trips_through_its_wire_spelling() {
        for family in VideoFamily::ALL {
            assert_eq!(family.as_str().parse::<VideoFamily>().unwrap(), family);
            assert_eq!(family.to_string(), family.as_str());
            let json = serde_json::to_string(&family).unwrap();
            assert_eq!(json, format!("\"{}\"", family.as_str()));
            assert_eq!(serde_json::from_str::<VideoFamily>(&json).unwrap(), family);
        }
    }

    #[test]
    fn video_family_labels_and_presets() {
        assert_eq!(VideoFamily::H264.label(), "H.264");
        assert_eq!(VideoFamily::H265.label(), "HEVC");
        assert_eq!(VideoFamily::Av1.label(), "AV1");
        assert_eq!(VideoFamily::H264.default_preset(), "veryfast");
        assert_eq!(VideoFamily::H265.default_preset(), "veryfast");
        // SVT-AV1 refuses non-numeric presets.
        assert_eq!(VideoFamily::Av1.default_preset(), "10");
    }

    #[test]
    fn to_video_codec_and_of_are_inverse() {
        for family in VideoFamily::ALL {
            let codec = family.to_video_codec(Some(4000));
            assert_eq!(VideoFamily::of(&codec), Some(family));
        }
        assert_eq!(VideoFamily::of(&VideoCodec::Copy), None);
        match VideoFamily::Av1.to_video_codec(Some(2500)) {
            VideoCodec::Av1Svt {
                preset,
                bitrate_kbps,
                crf,
            } => {
                assert_eq!(preset, "10");
                assert_eq!(bitrate_kbps, Some(2500));
                assert_eq!(crf, None);
            }
            other => panic!("unexpected codec {other:?}"),
        }
    }

    #[test]
    fn audio_choice_parses_and_round_trips() {
        for (raw, choice) in [
            ("copy", AudioChoice::Copy),
            ("AAC", AudioChoice::Aac),
            (" opus", AudioChoice::Opus),
        ] {
            assert_eq!(raw.parse::<AudioChoice>().unwrap(), choice);
            assert_eq!(choice.to_string(), choice.as_str());
            assert_eq!(AudioChoice::of(&choice.to_audio_codec()), choice);
        }
        let err = "flac".parse::<AudioChoice>().unwrap_err();
        assert!(err.to_string().contains("expected one of copy, aac, opus"));
    }

    #[test]
    fn audio_choice_builds_recipes_with_the_default_bitrate() {
        assert!(matches!(
            AudioChoice::Aac.to_audio_codec(),
            AudioCodec::Aac { bitrate_kbps: 128 }
        ));
        assert!(matches!(
            AudioChoice::Opus.to_audio_codec(),
            AudioCodec::Opus { bitrate_kbps: 128 }
        ));
        assert!(matches!(
            AudioChoice::Copy.to_audio_codec(),
            AudioCodec::Copy
        ));
    }

    #[test]
    fn target_protocol_parses_and_maps_to_its_container() {
        assert_eq!(
            "rtmp".parse::<TargetProtocol>().unwrap(),
            TargetProtocol::Rtmp
        );
        assert_eq!(
            "SRT".parse::<TargetProtocol>().unwrap(),
            TargetProtocol::Srt
        );
        assert_eq!(TargetProtocol::Rtmp.format(), OutputFormat::Flv);
        assert_eq!(TargetProtocol::Srt.format(), OutputFormat::MpegTs);
        assert_eq!(TargetProtocol::Srt.to_string(), "srt");
        let err = "whip".parse::<TargetProtocol>().unwrap_err();
        assert!(err.to_string().contains("expected one of rtmp, srt"));
    }

    #[test]
    fn rtmp_rejects_hevc_and_av1_with_the_documented_message() {
        for family in [VideoFamily::H265, VideoFamily::Av1] {
            let err = check_video(OutputFormat::Flv, Some(family)).unwrap_err();
            assert_eq!(err, CodecError::RtmpVideo(family));
            assert!(
                err.to_string().contains(
                    "FLV/RTMP cannot carry HEVC/AV1 (needs enhanced-RTMP); use HLS/SRT or H.264"
                ),
                "message was: {err}"
            );
        }
        assert!(check_video(OutputFormat::Flv, Some(VideoFamily::H264)).is_ok());
    }

    #[test]
    fn srt_rejects_av1_but_carries_hevc() {
        let err = check_video(OutputFormat::MpegTs, Some(VideoFamily::Av1)).unwrap_err();
        assert_eq!(err, CodecError::SrtAv1);
        assert!(err.to_string().contains("needs ffmpeg 6.1+"));
        assert!(check_video(OutputFormat::MpegTs, Some(VideoFamily::H265)).is_ok());
        assert!(check_video(OutputFormat::MpegTs, Some(VideoFamily::H264)).is_ok());
    }

    #[test]
    fn hls_and_mp4_carry_every_family() {
        for format in [OutputFormat::HlsFmp4, OutputFormat::Mp4] {
            for family in VideoFamily::ALL {
                assert!(check_video(format, Some(family)).is_ok());
            }
        }
    }

    #[test]
    fn passthrough_video_is_always_accepted() {
        for format in [
            OutputFormat::Flv,
            OutputFormat::MpegTs,
            OutputFormat::HlsFmp4,
            OutputFormat::Mp4,
        ] {
            assert!(check_video(format, None).is_ok());
        }
    }

    #[test]
    fn only_flv_rejects_opus() {
        assert_eq!(
            check_audio(OutputFormat::Flv, AudioChoice::Opus).unwrap_err(),
            CodecError::RtmpAudio
        );
        for format in [
            OutputFormat::MpegTs,
            OutputFormat::HlsFmp4,
            OutputFormat::Mp4,
        ] {
            assert!(check_audio(format, AudioChoice::Opus).is_ok());
        }
        assert!(check_audio(OutputFormat::Flv, AudioChoice::Aac).is_ok());
        assert!(check_audio(OutputFormat::Flv, AudioChoice::Copy).is_ok());
    }

    #[test]
    fn check_pair_reports_the_video_problem_first() {
        let err = check_pair(
            OutputFormat::Flv,
            Some(VideoFamily::H265),
            AudioChoice::Opus,
        )
        .unwrap_err();
        assert_eq!(err, CodecError::RtmpVideo(VideoFamily::H265));
        assert_eq!(
            check_pair(
                OutputFormat::Flv,
                Some(VideoFamily::H264),
                AudioChoice::Opus
            )
            .unwrap_err(),
            CodecError::RtmpAudio
        );
        assert!(check_pair(OutputFormat::Flv, None, AudioChoice::Copy).is_ok());
    }
}
