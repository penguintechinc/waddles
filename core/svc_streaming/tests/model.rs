//! Serde round-trip coverage for every variant of the public pipeline
//! model (`svc_streaming::pipeline::model`) -- complements the
//! narrower-scope unit tests colocated in `src/pipeline/model.rs`.

use svc_streaming::pipeline::{
    AudioCodec, HlsVariant, InputSpec, ObjectStoreRef, OutputSpec, PipelineSpec, PipelineState,
    PipelineStatus, TranscodeProfile, VideoCodec,
};
use svc_streaming::store::SecretRef;
use uuid::Uuid;

fn all_input_specs() -> Vec<InputSpec> {
    vec![
        InputSpec::Rtmp {
            stream_key: "sk_123".into(),
        },
        InputSpec::Srt {
            stream_id: "srt_456".into(),
        },
        InputSpec::Whip {
            token: "whip_789".into(),
        },
        InputSpec::Pull {
            url: "rtmp://upstream.example/live".into(),
        },
    ]
}

fn all_video_codecs() -> Vec<VideoCodec> {
    vec![
        VideoCodec::Copy,
        VideoCodec::H264 {
            preset: "veryfast".into(),
            crf: Some(21),
            bitrate_kbps: None,
        },
        VideoCodec::H265 {
            preset: "medium".into(),
            crf: None,
            bitrate_kbps: Some(4000),
        },
        VideoCodec::Av1Svt {
            preset: "8".into(),
            crf: Some(30),
            bitrate_kbps: None,
        },
    ]
}

fn all_audio_codecs() -> Vec<AudioCodec> {
    vec![
        AudioCodec::Copy,
        AudioCodec::Aac { bitrate_kbps: 160 },
        AudioCodec::Opus { bitrate_kbps: 128 },
    ]
}

fn all_output_specs() -> Vec<OutputSpec> {
    vec![
        OutputSpec::RtmpPush {
            url_secret_ref: SecretRef::Env {
                var: "RELAY_URL".into(),
            },
            profile: None,
        },
        OutputSpec::SrtPush {
            url_secret_ref: SecretRef::File {
                path: "/secrets/srt-url".into(),
            },
            profile: None,
        },
        OutputSpec::Hls {
            variant: HlsVariant::Ll,
            profile: "1080p60".into(),
        },
        OutputSpec::Hls {
            variant: HlsVariant::Std,
            profile: "720p30".into(),
        },
        OutputSpec::Whep {
            profile: "1080p60".into(),
        },
        OutputSpec::Record {
            profile: "1080p60".into(),
            target: ObjectStoreRef {
                store: "s3-recordings".into(),
                prefix: "tenant-1/community-1".into(),
            },
        },
        OutputSpec::DiscordVoice {
            guild_id: "guild-1".into(),
            channel_id: "channel-1".into(),
        },
    ]
}

#[test]
fn every_input_spec_variant_round_trips() {
    for input in all_input_specs() {
        let json = serde_json::to_string(&input).expect("serialize");
        let back: InputSpec = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(
            serde_json::to_value(&input).unwrap(),
            serde_json::to_value(&back).unwrap()
        );
    }
}

#[test]
fn every_video_codec_variant_round_trips() {
    for codec in all_video_codecs() {
        let json = serde_json::to_string(&codec).expect("serialize");
        let back: VideoCodec = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(
            serde_json::to_value(&codec).unwrap(),
            serde_json::to_value(&back).unwrap()
        );
    }
}

#[test]
fn every_audio_codec_variant_round_trips() {
    for codec in all_audio_codecs() {
        let json = serde_json::to_string(&codec).expect("serialize");
        let back: AudioCodec = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(
            serde_json::to_value(&codec).unwrap(),
            serde_json::to_value(&back).unwrap()
        );
    }
}

#[test]
fn every_output_spec_variant_round_trips() {
    for output in all_output_specs() {
        let json = serde_json::to_string(&output).expect("serialize");
        let back: OutputSpec = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(
            serde_json::to_value(&output).unwrap(),
            serde_json::to_value(&back).unwrap()
        );
    }
}

#[test]
fn full_pipeline_spec_with_every_input_and_output_round_trips() {
    let spec = PipelineSpec {
        id: Uuid::new_v4(),
        tenant: "tenant-1".into(),
        community_id: "community-1".into(),
        inputs: all_input_specs(),
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
        outputs: all_output_specs(),
    };

    let json = serde_json::to_string_pretty(&spec).expect("serialize");
    let back: PipelineSpec = serde_json::from_str(&json).expect("deserialize");

    assert_eq!(back.id, spec.id);
    assert_eq!(back.inputs.len(), spec.inputs.len());
    assert_eq!(back.outputs.len(), spec.outputs.len());
}

#[test]
fn pipeline_status_round_trips_with_detail() {
    let status = PipelineStatus {
        id: Uuid::new_v4(),
        state: PipelineState::Degraded,
        detail: Some("ffmpeg exited with code 1".into()),
    };
    let json = serde_json::to_string(&status).unwrap();
    let back: PipelineStatus = serde_json::from_str(&json).unwrap();
    assert_eq!(back.state, PipelineState::Degraded);
    assert_eq!(back.detail, status.detail);
}
