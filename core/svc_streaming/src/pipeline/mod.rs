//! A/V pipeline model + lifecycle engine contract.
//!
//! `model` defines the public spec/status types every other module (http
//! API, ingest, egress) builds against; `ffmpeg` and `supervisor` (owned by
//! a later chunk, S3) implement the actual transcode/lifecycle machinery.

pub mod codec;
pub mod encoder;
pub mod ffmpeg;
pub mod model;
pub mod supervisor;

pub use codec::{AudioChoice, CodecError, OutputFormat, TargetProtocol, VideoFamily};
pub use encoder::{
    detect_encoders, DeviceHints, EncoderBackend, EncoderPreference, EncoderSelection,
};
pub use ffmpeg::{
    build_argv, encode_summary, rtp_legs, secret_ref_key, EncodeInfo, Paths, RtpLeg,
    RtpLegDirection,
};
pub use model::{
    AudioCodec, HlsVariant, InputSpec, ObjectStoreRef, OutputSpec, PipelineEngine, PipelineError,
    PipelineHandle, PipelineId, PipelineSpec, PipelineState, PipelineStatus, TranscodeProfile,
    VideoCodec,
};
pub use supervisor::{
    FfmpegSupervisor, PipelineProgress, StdinHandle, StubSupervisor, SupervisorConfig,
};
