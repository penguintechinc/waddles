//! Public pipeline model shared across `ingest`, `egress`, `http`/`api`,
//! and `pipeline::supervisor`. Owned by the pipeline chunk (S3); every
//! other module builds against these types without depending on
//! supervisor/ffmpeg internals.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::store::SecretRef;

/// Unique identifier for a configured or running pipeline.
pub type PipelineId = Uuid;

/// Full description of an A/V pipeline: 1..N inputs feeding 1..N transcode
/// profiles feeding 1..N outputs.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineSpec {
    pub id: PipelineId,
    pub tenant: String,
    pub community_id: String,
    pub inputs: Vec<InputSpec>,
    pub profiles: Vec<TranscodeProfile>,
    pub outputs: Vec<OutputSpec>,
}

/// A single ingest source for a pipeline.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum InputSpec {
    /// RTMP push ingest, keyed by stream key (owned by `ingest::rtmp`).
    Rtmp { stream_key: String },
    /// SRT push/pull ingest, keyed by SRT stream id (owned by `ingest::srt`).
    Srt { stream_id: String },
    /// WHIP (WebRTC-HTTP Ingestion Protocol) ingest (owned by `ingest::whip`).
    Whip { token: String },
    /// Pull ingest from an arbitrary upstream URL (RTMP/SRT/HLS source).
    Pull { url: String },
}

/// A named transcode profile: one video + one audio codec configuration,
/// plus optional resolution/fps overrides. `Copy` skips transcoding that
/// track (remux only).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscodeProfile {
    pub name: String,
    pub video: VideoCodec,
    pub audio: AudioCodec,
    pub resolution: Option<(u32, u32)>,
    pub fps: Option<u32>,
}

/// Video codec + rate-control configuration for a [`TranscodeProfile`].
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "codec", rename_all = "snake_case")]
pub enum VideoCodec {
    /// Passthrough -- no video transcode, remux only.
    Copy,
    H264 {
        preset: String,
        #[serde(default)]
        crf: Option<u8>,
        #[serde(default)]
        bitrate_kbps: Option<u32>,
    },
    H265 {
        preset: String,
        #[serde(default)]
        crf: Option<u8>,
        #[serde(default)]
        bitrate_kbps: Option<u32>,
    },
    Av1Svt {
        preset: String,
        #[serde(default)]
        crf: Option<u8>,
        #[serde(default)]
        bitrate_kbps: Option<u32>,
    },
}

/// Audio codec + bitrate configuration for a [`TranscodeProfile`].
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "codec", rename_all = "snake_case")]
pub enum AudioCodec {
    /// Passthrough -- no audio transcode, remux only.
    Copy,
    Aac {
        bitrate_kbps: u32,
    },
    Opus {
        bitrate_kbps: u32,
    },
}

/// A single egress sink for a pipeline.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum OutputSpec {
    /// Forward to an external RTMP endpoint (owned by `egress::relay`).
    /// `profile` names the [`TranscodeProfile`] this push encodes with;
    /// `None` keeps the legacy binding to `PipelineSpec::profiles[0]`.
    RtmpPush {
        url_secret_ref: SecretRef,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        profile: Option<String>,
    },
    /// Forward to an external SRT endpoint (owned by `egress::relay`).
    /// `profile` is the same optional binding as on [`Self::RtmpPush`].
    SrtPush {
        url_secret_ref: SecretRef,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        profile: Option<String>,
    },
    /// Serve HLS (owned by `egress::hls`).
    Hls {
        variant: HlsVariant,
        profile: String,
    },
    /// Serve WHEP -- WebRTC-HTTP Egress Protocol (owned by `egress::whep`).
    Whep { profile: String },
    /// Record to object storage (owned by `egress::record`).
    Record {
        profile: String,
        target: ObjectStoreRef,
    },
    /// Bridge into a Discord voice channel (owned by `egress::discord_voice`).
    DiscordVoice {
        guild_id: String,
        channel_id: String,
    },
}

/// HLS delivery variant.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HlsVariant {
    /// Low-latency HLS (partial segments, blocking playlist reload).
    Ll,
    /// Standard HLS (segment-level latency, widest player compatibility).
    Std,
}

/// Reference to an `object_store`-backed recording target. Resolution
/// (bucket/prefix/credentials) is owned by `egress::record`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObjectStoreRef {
    pub store: String,
    pub prefix: String,
}

/// Handle to a running pipeline, returned by [`PipelineEngine::start`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineHandle {
    pub id: PipelineId,
}

/// Current lifecycle state of a pipeline.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PipelineState {
    Starting,
    Running,
    Degraded,
    Stopping,
    Stopped,
    Failed,
}

/// Point-in-time status snapshot for a pipeline.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineStatus {
    pub id: PipelineId,
    pub state: PipelineState,
    pub detail: Option<String>,
}

/// Errors a [`PipelineEngine`] implementation can return.
#[derive(Debug, thiserror::Error)]
pub enum PipelineError {
    #[error("pipeline {0} not found")]
    NotFound(PipelineId),
    #[error("invalid pipeline spec: {0}")]
    InvalidSpec(String),
    #[error("{0} is not yet implemented")]
    Unimplemented(&'static str),
    /// The spec requires no `ffmpeg` process at all -- e.g. a pure-copy
    /// WHIP->WHEP leg, forwarded RTP-to-RTP by `webrtc-rs` outside
    /// ffmpeg's process boundary (see `pipeline::ffmpeg` module docs and
    /// spec doc `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md`
    /// §1/§7 case 6). Not a failure -- callers should skip spawning and
    /// wire the RTP forward directly via [`crate::pipeline::ffmpeg::rtp_legs`].
    #[error("pipeline requires no ffmpeg process (pure copy forwarded via RTP)")]
    NoFfmpegNeeded,
    /// The spec is well-formed but describes a transform this MVP
    /// deliberately does not implement yet (e.g. multi-input compositing --
    /// spec doc §2/§7 case 10, tagged P2).
    #[error("{0} is not supported yet")]
    Unsupported(&'static str),
    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

/// Supervises the lifecycle of A/V pipelines built from [`PipelineSpec`].
/// Implemented by `pipeline::supervisor` (owned by a later chunk); `http`,
/// `ingest`, and `egress` modules interact with pipelines only through this
/// trait, never supervisor internals.
pub trait PipelineEngine: Send + Sync {
    /// Validates and starts a new pipeline from `spec`.
    fn start(
        &self,
        spec: PipelineSpec,
    ) -> impl std::future::Future<Output = Result<PipelineHandle, PipelineError>> + Send;
    /// Stops a running pipeline. Idempotent: stopping an already-stopped or
    /// unknown pipeline is not an error.
    fn stop(
        &self,
        id: PipelineId,
    ) -> impl std::future::Future<Output = Result<(), PipelineError>> + Send;
    /// Returns the current status of a pipeline.
    fn status(
        &self,
        id: PipelineId,
    ) -> impl std::future::Future<Output = Result<PipelineStatus, PipelineError>> + Send;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_spec() -> PipelineSpec {
        PipelineSpec {
            id: Uuid::nil(),
            tenant: "tenant-1".into(),
            community_id: "community-1".into(),
            inputs: vec![InputSpec::Rtmp {
                stream_key: "sk_abc123".into(),
            }],
            profiles: vec![TranscodeProfile {
                name: "1080p60".into(),
                video: VideoCodec::H264 {
                    preset: "veryfast".into(),
                    crf: Some(21),
                    bitrate_kbps: None,
                },
                audio: AudioCodec::Aac { bitrate_kbps: 160 },
                resolution: Some((1920, 1080)),
                fps: Some(60),
            }],
            outputs: vec![
                OutputSpec::Hls {
                    variant: HlsVariant::Ll,
                    profile: "1080p60".into(),
                },
                OutputSpec::Record {
                    profile: "1080p60".into(),
                    target: ObjectStoreRef {
                        store: "s3-recordings".into(),
                        prefix: "tenant-1/community-1".into(),
                    },
                },
            ],
        }
    }

    #[test]
    fn pipeline_spec_round_trips_through_json() {
        let spec = sample_spec();
        let json = serde_json::to_string(&spec).expect("serialize");
        let back: PipelineSpec = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(back.tenant, spec.tenant);
        assert_eq!(back.inputs.len(), 1);
        assert_eq!(back.outputs.len(), 2);
    }

    #[test]
    fn input_spec_tag_uses_snake_case_kind() {
        let input = InputSpec::Whip {
            token: "whip-token".into(),
        };
        let json = serde_json::to_value(&input).unwrap();
        assert_eq!(json["kind"], "whip");
        assert_eq!(json["token"], "whip-token");
    }

    #[test]
    fn output_spec_secret_ref_never_serializes_a_raw_secret() {
        let output = OutputSpec::RtmpPush {
            url_secret_ref: SecretRef::Env {
                var: "RELAY_TARGET_URL".into(),
            },
            profile: None,
        };
        let json = serde_json::to_string(&output).unwrap();
        // The serialized form carries the *reference* only -- the env var
        // name, never a resolved secret value.
        assert!(json.contains("RELAY_TARGET_URL"));
        assert!(json.contains("\"source\":\"env\""));
    }

    #[test]
    fn push_output_profile_is_optional_on_the_wire() {
        // A spec serialized before `profile` existed must still deserialize.
        let legacy = r#"{"kind":"rtmp_push","url_secret_ref":{"source":"env","var":"X"}}"#;
        let parsed: OutputSpec = serde_json::from_str(legacy).expect("legacy json");
        assert!(matches!(parsed, OutputSpec::RtmpPush { profile: None, .. }));

        // `None` is omitted, keeping the legacy shape byte-for-byte.
        let json = serde_json::to_string(&parsed).unwrap();
        assert!(!json.contains("profile"), "{json}");

        let bound = r#"{"kind":"srt_push","url_secret_ref":{"source":"env","var":"X"},"profile":"h265-aac"}"#;
        match serde_json::from_str::<OutputSpec>(bound).expect("bound json") {
            OutputSpec::SrtPush { profile, .. } => assert_eq!(profile.as_deref(), Some("h265-aac")),
            other => panic!("unexpected output {other:?}"),
        }
        let round_trip =
            serde_json::to_string(&serde_json::from_str::<OutputSpec>(bound).unwrap()).unwrap();
        assert!(
            round_trip.contains("\"profile\":\"h265-aac\""),
            "{round_trip}"
        );
    }

    #[test]
    fn video_codec_copy_round_trips() {
        let json = serde_json::to_string(&VideoCodec::Copy).unwrap();
        assert_eq!(json, "{\"codec\":\"copy\"}");
        let back: VideoCodec = serde_json::from_str(&json).unwrap();
        assert!(matches!(back, VideoCodec::Copy));
    }

    #[test]
    fn pipeline_status_round_trips() {
        let status = PipelineStatus {
            id: Uuid::nil(),
            state: PipelineState::Running,
            detail: None,
        };
        let json = serde_json::to_string(&status).unwrap();
        let back: PipelineStatus = serde_json::from_str(&json).unwrap();
        assert_eq!(back.state, PipelineState::Running);
    }
}
