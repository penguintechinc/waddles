//! Turns a `streaming_configs` row and its `streaming_targets` into the
//! transcode profiles and push outputs of a [`crate::pipeline::PipelineSpec`].
//!
//! Both spec builders -- the ingest-triggered one in `orchestrator` and the
//! control-plane one in `api::lifecycle` -- and both validation points (the
//! API write handlers and spec-build time) go through [`plan_push_outputs`],
//! so a codec choice is accepted or rejected by the same rules everywhere
//! and a row that slipped past the API (a manual DB edit) still fails loudly
//! when a pipeline is built from it, instead of being silently dropped by
//! `ffmpeg -f tee`.
//!
//! # Selection model
//!
//! - `streaming_configs.video_codec`/`audio_codec` choose the *default*
//!   encode: it feeds HLS and every target that does not say otherwise.
//! - `streaming_targets.protocol` (`rtmp`/`srt`) picks the container; its
//!   optional `video_codec`/`audio_codec` override the default for that one
//!   target. An RTMP target with no video override uses H.264 (the only
//!   codec FLV carries) rather than inheriting the config's; an SRT target
//!   inherits it.
//! - Targets whose resulting (video, audio) pair equals the default share
//!   its single encode through `-f tee`. A different pair becomes an extra
//!   profile, which `pipeline::ffmpeg` runs as a second encode off a shared
//!   decode (`-filter_complex split`) -- the "ladder" path.
//! - Any non-default choice needs `transcode_enabled`: re-encoding is the
//!   token-gated transcode feature, so codec fields cannot bypass it.

use crate::db::entities::{streaming_config, streaming_target};
use crate::error::ApiError;
use crate::pipeline::codec::{self, AudioChoice, CodecError, TargetProtocol, VideoFamily};
use crate::pipeline::{AudioCodec, OutputSpec, TranscodeProfile, VideoCodec};
use crate::store::SecretRef;

/// Profile name every HLS output and every target without an override uses.
pub const DEFAULT_PROFILE: &str = "default";

/// A (video codec, audio handling) pair -- the unit that decides whether two
/// outputs can share one encode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct EncodeChoice {
    pub video: VideoFamily,
    pub audio: AudioChoice,
}

impl EncodeChoice {
    /// What a freshly created config selects (`h264` + `copy`), and the only
    /// choice allowed while `transcode_enabled` is off.
    pub const DEFAULT: EncodeChoice = EncodeChoice {
        video: VideoFamily::H264,
        audio: AudioChoice::Copy,
    };

    /// The [`TranscodeProfile`] realizing this choice at `bitrate_kbps`.
    fn profile(self, name: &str, bitrate_kbps: Option<u32>) -> TranscodeProfile {
        TranscodeProfile {
            name: name.to_string(),
            video: self.video.to_video_codec(bitrate_kbps),
            audio: self.audio.to_audio_codec(),
            resolution: None,
            fps: None,
        }
    }
}

/// Why a spec could not be planned. Messages never contain a stream key or
/// URL -- only ids and codec names.
#[derive(Debug, thiserror::Error)]
pub enum PlanError {
    /// A codec problem with a value the caller just supplied (no row id yet).
    #[error(transparent)]
    Codec(#[from] CodecError),
    #[error("streaming_config {config_id}: {source}")]
    Config {
        config_id: i32,
        #[source]
        source: CodecError,
    },
    #[error("streaming_target {target_id}: {source}")]
    Target {
        target_id: i32,
        #[source]
        source: CodecError,
    },
    #[error("video_codec/audio_codec other than h264/copy require transcode_enabled=true (re-encoding is the token-gated transcode feature)")]
    CodecWithoutTranscode,
    #[error("stored streaming_target {target_id} has a non-secret_ref forward_url")]
    BadSecretRef { target_id: i32 },
}

impl From<PlanError> for ApiError {
    fn from(err: PlanError) -> Self {
        match err {
            PlanError::BadSecretRef { .. } => ApiError::Internal(err.into()),
            other => ApiError::BadRequest(other.to_string()),
        }
    }
}

/// The profiles and push outputs a config's targets need. `profiles[0]` is
/// always the [`DEFAULT_PROFILE`]; the caller adds its own HLS/record
/// outputs on top.
#[derive(Debug, Clone)]
pub struct PushPlan {
    pub profiles: Vec<TranscodeProfile>,
    pub outputs: Vec<OutputSpec>,
}

/// Parses a config's stored codec strings.
pub fn parse_config_choice(video: &str, audio: &str) -> Result<EncodeChoice, CodecError> {
    Ok(EncodeChoice {
        video: video.parse()?,
        audio: audio.parse()?,
    })
}

/// Resolves one target's protocol and effective choice against the config
/// default, then proves its container can carry it.
///
/// `video`/`audio` are the target's stored overrides (`None` = inherit; see
/// the module docs for the RTMP video special case).
pub fn resolve_target_choice(
    default: EncodeChoice,
    protocol: &str,
    video: Option<&str>,
    audio: Option<&str>,
) -> Result<(TargetProtocol, EncodeChoice), CodecError> {
    let protocol: TargetProtocol = protocol.parse()?;
    let video = match (video, protocol) {
        (Some(raw), _) => raw.parse()?,
        (None, TargetProtocol::Rtmp) => VideoFamily::H264,
        (None, TargetProtocol::Srt) => default.video,
    };
    let audio = match audio {
        Some(raw) => raw.parse()?,
        None => default.audio,
    };
    codec::check_pair(protocol.format(), Some(video), audio)?;
    Ok((protocol, EncodeChoice { video, audio }))
}

struct PlannedTarget {
    secret: SecretRef,
    protocol: TargetProtocol,
    choice: EncodeChoice,
    explicit: bool,
}

/// Plans the profiles and push outputs for `config` and its enabled
/// `targets`.
///
/// `transcode_applied` is whether re-encoding will actually happen: the
/// config must have `transcode_enabled`, and the caller's token admission
/// (if any) must have succeeded. When it is false every output is a
/// passthrough of the ingest stream (the existing "fall back to passthrough
/// rather than refuse the stream" behaviour), but the codec choices are
/// still validated so a bad row never goes unnoticed.
pub fn plan_push_outputs(
    config: &streaming_config::Model,
    targets: &[streaming_target::Model],
    transcode_applied: bool,
) -> Result<PushPlan, PlanError> {
    let default_choice =
        parse_config_choice(&config.video_codec, &config.audio_codec).map_err(|source| {
            PlanError::Config {
                config_id: config.id,
                source,
            }
        })?;

    let mut planned = Vec::with_capacity(targets.len());
    for target in targets {
        let secret: SecretRef =
            serde_json::from_str(&target.forward_url).map_err(|_| PlanError::BadSecretRef {
                target_id: target.id,
            })?;
        let (protocol, choice) = resolve_target_choice(
            default_choice,
            &target.protocol,
            target.video_codec.as_deref(),
            target.audio_codec.as_deref(),
        )
        .map_err(|source| PlanError::Target {
            target_id: target.id,
            source,
        })?;
        planned.push(PlannedTarget {
            secret,
            protocol,
            choice,
            explicit: target.video_codec.is_some() || target.audio_codec.is_some(),
        });
    }

    if !config.transcode_enabled
        && (default_choice != EncodeChoice::DEFAULT || planned.iter().any(|t| t.explicit))
    {
        return Err(PlanError::CodecWithoutTranscode);
    }

    let transcode = config.transcode_enabled && transcode_applied;
    let bitrate_kbps = Some(config.transcode_bitrate_kbps.max(0) as u32);

    let default_profile = if transcode {
        default_choice.profile(DEFAULT_PROFILE, bitrate_kbps)
    } else {
        TranscodeProfile {
            name: DEFAULT_PROFILE.to_string(),
            video: VideoCodec::Copy,
            audio: AudioCodec::Copy,
            resolution: None,
            fps: None,
        }
    };
    let mut profiles = vec![default_profile];
    let mut outputs = Vec::with_capacity(planned.len());

    for target in planned {
        let profile = if !transcode || target.choice == default_choice {
            DEFAULT_PROFILE.to_string()
        } else {
            let name = format!("{}-{}", target.choice.video, target.choice.audio);
            if !profiles.iter().any(|p| p.name == name) {
                profiles.push(target.choice.profile(&name, bitrate_kbps));
            }
            name
        };
        outputs.push(match target.protocol {
            TargetProtocol::Rtmp => OutputSpec::RtmpPush {
                url_secret_ref: target.secret,
                profile: Some(profile),
            },
            TargetProtocol::Srt => OutputSpec::SrtPush {
                url_secret_ref: target.secret,
                profile: Some(profile),
            },
        });
    }

    tracing::debug!(
        config_id = config.id,
        transcode,
        profiles = profiles.len(),
        pushes = outputs.len(),
        "planned push outputs"
    );
    Ok(PushPlan { profiles, outputs })
}

/// Validates `config` (and the already-enabled `targets` hanging off it)
/// without building anything -- the check the config write handlers run.
pub fn validate_config(
    config: &streaming_config::Model,
    targets: &[streaming_target::Model],
) -> Result<(), PlanError> {
    plan_push_outputs(config, targets, config.transcode_enabled).map(|_| ())
}

/// Validates adding `candidate` to `config`'s `existing` enabled targets.
/// The candidate is checked first, on its own, so the error names the codec
/// problem without a row id the caller has not been given yet.
pub fn validate_new_target(
    config: &streaming_config::Model,
    existing: &[streaming_target::Model],
    candidate: &streaming_target::Model,
) -> Result<(), PlanError> {
    let default_choice =
        parse_config_choice(&config.video_codec, &config.audio_codec).map_err(|source| {
            PlanError::Config {
                config_id: config.id,
                source,
            }
        })?;
    resolve_target_choice(
        default_choice,
        &candidate.protocol,
        candidate.video_codec.as_deref(),
        candidate.audio_codec.as_deref(),
    )?;
    let mut all = existing.to_vec();
    all.push(candidate.clone());
    validate_config(config, &all)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(video: &str, audio: &str, transcode: bool) -> streaming_config::Model {
        streaming_config::Model {
            id: 1,
            community_id: 42,
            source_url: "sk".into(),
            source_type: "rtmp".into(),
            enabled: true,
            record_enabled: false,
            transcode_enabled: transcode,
            transcode_bitrate_kbps: 4000,
            video_codec: video.into(),
            audio_codec: audio.into(),
        }
    }

    fn secret_json(var: &str) -> String {
        serde_json::to_string(&SecretRef::Env { var: var.into() }).unwrap()
    }

    fn target(
        id: i32,
        protocol: &str,
        video: Option<&str>,
        audio: Option<&str>,
    ) -> streaming_target::Model {
        streaming_target::Model {
            id,
            config_id: 1,
            platform: "custom".into(),
            forward_url: secret_json(&format!("URL_{id}")),
            enabled: true,
            protocol: protocol.into(),
            video_codec: video.map(str::to_string),
            audio_codec: audio.map(str::to_string),
        }
    }

    fn bound_profile(output: &OutputSpec) -> &str {
        match output {
            OutputSpec::RtmpPush { profile, .. } | OutputSpec::SrtPush { profile, .. } => {
                profile.as_deref().expect("planned pushes are always bound")
            }
            other => panic!("not a push output: {other:?}"),
        }
    }

    #[test]
    fn default_config_without_transcode_is_a_pure_passthrough() {
        let plan = plan_push_outputs(
            &config("h264", "copy", false),
            &[target(1, "rtmp", None, None), target(2, "srt", None, None)],
            false,
        )
        .expect("plans");
        assert_eq!(plan.profiles.len(), 1);
        assert!(matches!(plan.profiles[0].video, VideoCodec::Copy));
        assert!(matches!(plan.profiles[0].audio, AudioCodec::Copy));
        assert_eq!(plan.outputs.len(), 2);
        assert!(matches!(plan.outputs[0], OutputSpec::RtmpPush { .. }));
        assert!(matches!(plan.outputs[1], OutputSpec::SrtPush { .. }));
        assert!(plan
            .outputs
            .iter()
            .all(|o| bound_profile(o) == DEFAULT_PROFILE));
    }

    #[test]
    fn transcoding_h264_keeps_one_shared_profile() {
        let plan = plan_push_outputs(
            &config("h264", "copy", true),
            &[target(1, "rtmp", None, None), target(2, "srt", None, None)],
            true,
        )
        .expect("plans");
        assert_eq!(plan.profiles.len(), 1);
        assert!(matches!(
            plan.profiles[0].video,
            VideoCodec::H264 {
                bitrate_kbps: Some(4000),
                ..
            }
        ));
    }

    #[test]
    fn h265_config_puts_srt_on_the_default_and_rtmp_on_an_h264_ladder_rung() {
        let plan = plan_push_outputs(
            &config("h265", "copy", true),
            &[target(1, "rtmp", None, None), target(2, "srt", None, None)],
            true,
        )
        .expect("plans");
        assert_eq!(plan.profiles.len(), 2);
        assert_eq!(plan.profiles[0].name, DEFAULT_PROFILE);
        assert!(matches!(plan.profiles[0].video, VideoCodec::H265 { .. }));
        assert_eq!(plan.profiles[1].name, "h264-copy");
        assert!(matches!(plan.profiles[1].video, VideoCodec::H264 { .. }));
        assert_eq!(bound_profile(&plan.outputs[0]), "h264-copy");
        assert_eq!(bound_profile(&plan.outputs[1]), DEFAULT_PROFILE);
    }

    #[test]
    fn two_targets_with_the_same_override_share_one_extra_profile() {
        let plan = plan_push_outputs(
            &config("h265", "copy", true),
            &[
                target(1, "rtmp", None, None),
                target(2, "rtmp", Some("h264"), None),
                target(3, "srt", Some("h264"), None),
            ],
            true,
        )
        .expect("plans");
        assert_eq!(plan.profiles.len(), 2, "one extra rung, not three");
        assert!(plan.outputs.iter().all(|o| bound_profile(o) == "h264-copy"));
    }

    #[test]
    fn audio_override_creates_its_own_profile() {
        let plan = plan_push_outputs(
            &config("h264", "copy", true),
            &[target(1, "srt", None, Some("opus"))],
            true,
        )
        .expect("plans");
        assert_eq!(plan.profiles.len(), 2);
        assert_eq!(plan.profiles[1].name, "h264-opus");
        assert!(matches!(
            plan.profiles[1].audio,
            AudioCodec::Opus { bitrate_kbps: 128 }
        ));
    }

    #[test]
    fn av1_config_with_an_inheriting_srt_target_is_rejected_loudly() {
        let err = plan_push_outputs(
            &config("av1", "copy", true),
            &[target(7, "srt", None, None)],
            true,
        )
        .unwrap_err();
        assert!(
            matches!(
                err,
                PlanError::Target {
                    target_id: 7,
                    source: CodecError::SrtAv1
                }
            ),
            "got {err:?}"
        );
        assert!(err.to_string().contains("streaming_target 7"));
    }

    #[test]
    fn av1_config_is_fine_for_hls_and_an_rtmp_target_defaults_to_h264() {
        let plan = plan_push_outputs(
            &config("av1", "copy", true),
            &[target(1, "rtmp", None, None)],
            true,
        )
        .expect("plans");
        assert!(matches!(plan.profiles[0].video, VideoCodec::Av1Svt { .. }));
        assert_eq!(bound_profile(&plan.outputs[0]), "h264-copy");
    }

    #[test]
    fn rtmp_target_requesting_hevc_or_av1_is_rejected_with_the_flv_message() {
        for codec in ["h265", "av1"] {
            let err = plan_push_outputs(
                &config("h264", "copy", true),
                &[target(3, "rtmp", Some(codec), None)],
                true,
            )
            .unwrap_err();
            let msg = err.to_string();
            assert!(
                msg.contains(
                    "FLV/RTMP cannot carry HEVC/AV1 (needs enhanced-RTMP); use HLS/SRT or H.264"
                ),
                "message was: {msg}"
            );
        }
    }

    #[test]
    fn rtmp_target_cannot_inherit_opus_audio() {
        let err = plan_push_outputs(
            &config("h264", "opus", true),
            &[target(1, "rtmp", None, None)],
            true,
        )
        .unwrap_err();
        assert!(matches!(
            err,
            PlanError::Target {
                source: CodecError::RtmpAudio,
                ..
            }
        ));
        // ...but an explicit AAC override makes it valid.
        assert!(plan_push_outputs(
            &config("h264", "opus", true),
            &[target(1, "rtmp", None, Some("aac"))],
            true,
        )
        .is_ok());
    }

    #[test]
    fn non_default_codec_without_transcode_is_rejected() {
        for cfg in [config("h265", "copy", false), config("h264", "aac", false)] {
            assert!(matches!(
                plan_push_outputs(&cfg, &[], false).unwrap_err(),
                PlanError::CodecWithoutTranscode
            ));
        }
        // An explicit per-target override also needs transcoding.
        assert!(matches!(
            plan_push_outputs(
                &config("h264", "copy", false),
                &[target(1, "srt", Some("h265"), None)],
                false
            )
            .unwrap_err(),
            PlanError::CodecWithoutTranscode
        ));
    }

    #[test]
    fn denied_admission_falls_back_to_passthrough_but_still_validates() {
        let cfg = config("h265", "copy", true);
        let plan =
            plan_push_outputs(&cfg, &[target(1, "srt", None, None)], false).expect("falls back");
        assert_eq!(plan.profiles.len(), 1);
        assert!(matches!(plan.profiles[0].video, VideoCodec::Copy));
        assert_eq!(bound_profile(&plan.outputs[0]), DEFAULT_PROFILE);

        // A broken row is still an error even when admission was denied.
        assert!(plan_push_outputs(
            &config("av1", "copy", true),
            &[target(1, "srt", None, None)],
            false
        )
        .is_err());
    }

    #[test]
    fn unknown_stored_values_are_errors_not_defaults() {
        assert!(matches!(
            plan_push_outputs(&config("vp9", "copy", true), &[], true).unwrap_err(),
            PlanError::Config { config_id: 1, .. }
        ));
        assert!(matches!(
            plan_push_outputs(&config("h264", "flac", true), &[], true).unwrap_err(),
            PlanError::Config { .. }
        ));
        assert!(matches!(
            plan_push_outputs(
                &config("h264", "copy", true),
                &[target(5, "whip", None, None)],
                true
            )
            .unwrap_err(),
            PlanError::Target {
                target_id: 5,
                source: CodecError::UnknownProtocol(_)
            }
        ));
    }

    #[test]
    fn a_non_secret_ref_forward_url_is_reported_without_echoing_it() {
        let mut bad = target(9, "rtmp", None, None);
        bad.forward_url = "rtmp://live.example/app/SUPERSECRETKEY".into();
        let err = plan_push_outputs(&config("h264", "copy", false), &[bad], false).unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("forward_url"));
        assert!(msg.contains('9'));
        assert!(!msg.contains("SUPERSECRETKEY"));
    }

    #[test]
    fn plan_errors_map_to_400_except_corrupt_rows() {
        let bad_request: ApiError = PlanError::CodecWithoutTranscode.into();
        assert!(matches!(bad_request, ApiError::BadRequest(_)));
        let from_codec: ApiError = PlanError::Codec(CodecError::SrtAv1).into();
        assert!(matches!(from_codec, ApiError::BadRequest(_)));
        let internal: ApiError = PlanError::BadSecretRef { target_id: 1 }.into();
        assert!(matches!(internal, ApiError::Internal(_)));
    }

    #[test]
    fn validate_new_target_reports_the_candidate_without_a_row_id() {
        let cfg = config("h264", "copy", true);
        let err =
            validate_new_target(&cfg, &[], &target(0, "rtmp", Some("h265"), None)).unwrap_err();
        assert!(matches!(err, PlanError::Codec(CodecError::RtmpVideo(_))));
        assert!(!err.to_string().contains("streaming_target"));
        assert!(validate_new_target(&cfg, &[], &target(0, "srt", Some("h265"), None)).is_ok());
    }

    #[test]
    fn validate_new_target_checks_the_candidate_against_the_transcode_flag() {
        let cfg = config("h264", "copy", false);
        assert!(matches!(
            validate_new_target(&cfg, &[], &target(0, "srt", Some("h265"), None)).unwrap_err(),
            PlanError::CodecWithoutTranscode
        ));
        assert!(validate_new_target(&cfg, &[], &target(0, "srt", None, None)).is_ok());
    }

    #[test]
    fn validate_new_target_reports_a_bad_config_row() {
        let cfg = config("vp9", "copy", true);
        assert!(matches!(
            validate_new_target(&cfg, &[], &target(0, "srt", None, None)).unwrap_err(),
            PlanError::Config { .. }
        ));
    }

    #[test]
    fn validate_config_catches_an_existing_target_the_new_codec_breaks() {
        let existing = [target(4, "srt", None, None)];
        assert!(validate_config(&config("h265", "copy", true), &existing).is_ok());
        assert!(validate_config(&config("av1", "copy", true), &existing).is_err());
    }

    #[test]
    fn resolve_target_choice_defaults_follow_the_protocol() {
        let default = EncodeChoice {
            video: VideoFamily::H265,
            audio: AudioChoice::Aac,
        };
        let (protocol, rtmp) = resolve_target_choice(default, "rtmp", None, None).unwrap();
        assert_eq!(protocol, TargetProtocol::Rtmp);
        assert_eq!(rtmp.video, VideoFamily::H264);
        assert_eq!(rtmp.audio, AudioChoice::Aac);
        let (_, srt) = resolve_target_choice(default, "srt", None, None).unwrap();
        assert_eq!(srt, default);
    }
}
