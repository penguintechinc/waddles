//! Proves `Orchestrator::wire_whep` connects a pipeline's WHEP output to the
//! REAL WHIP publisher (media flows to a WHEP viewer), and fails loud (no
//! registration) for sources it cannot serve.

mod rtc_common;

use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use axum::http::StatusCode;
use tokio::sync::mpsc;
use uuid::Uuid;
use webrtc::media_stream::track_local::TrackLocal;

use rtc_common::*;
use svc_streaming::egress::hls::HlsSink;
use svc_streaming::egress::relay::RelaySink;
use svc_streaming::egress::whep::{self, WhepState, DEFAULT_MAX_VIEWERS};
use svc_streaming::ingest::whip::{self, WhipState};
use svc_streaming::ingest::IngestKind;
use svc_streaming::orchestrator::{Orchestrator, PipelineRegistry};
use svc_streaming::pipeline::{
    AudioCodec, FfmpegSupervisor, InputSpec, OutputSpec, PipelineSpec, TranscodeProfile, VideoCodec,
};
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

fn spec(input: InputSpec, copy: bool) -> PipelineSpec {
    PipelineSpec {
        id: Uuid::new_v4(),
        tenant: "t".into(),
        community_id: "7".into(),
        inputs: vec![input],
        profiles: vec![TranscodeProfile {
            name: "p".into(),
            video: if copy {
                VideoCodec::Copy
            } else {
                VideoCodec::H264 {
                    preset: "veryfast".into(),
                    crf: Some(23),
                    bitrate_kbps: None,
                }
            },
            audio: AudioCodec::Copy,
            resolution: None,
            fps: None,
        }],
        outputs: vec![OutputSpec::Whep {
            profile: "p".into(),
        }],
    }
}

fn orchestrator(whip: Arc<WhipState>, whep: Arc<WhepState>, dir: PathBuf) -> Orchestrator {
    let cli = clap::Parser::parse_from(["svc-streaming"]);
    let config = svc_streaming::config::Config {
        cli,
        db_password: svc_streaming::config::Secret::new("x"),
        cache_password: None,
        service_api_key: svc_streaming::config::Secret::new("x"),
        jwt_hmac_secret: None,
    };
    let reg = prometheus::Registry::new();
    let supervisor = Arc::new(FfmpegSupervisor::new(
        PathBuf::from("/nonexistent/ffmpeg"),
        dir.clone(),
        41000,
        Arc::new(svc_streaming::store::DefaultSecretResolver),
        svc_streaming::pipeline::SupervisorConfig::default(),
    ));
    let hls = Arc::new(HlsSink::new(dir, &reg));
    let relay = Arc::new(RelaySink::new());
    Orchestrator::new(
        config,
        supervisor,
        hls,
        relay,
        None,
        Arc::new(PipelineRegistry::new()),
        whip,
    )
    .with_whep_state(whep)
}

#[tokio::test]
async fn whep_output_is_wired_to_the_real_whip_publisher_and_media_flows() {
    let whip_factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((42000, 42009))).unwrap());
    let publisher_factory =
        PeerConnectionFactory::new(loopback_rtc_config((42010, 42019))).unwrap();
    let whep_factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((42020, 42029))).unwrap());
    let viewer_factory = PeerConnectionFactory::new(loopback_rtc_config((42030, 42039))).unwrap();
    let dir = std::env::temp_dir().join(format!("svc-streaming-wire-{}", Uuid::new_v4()));
    let (tx, _rx) = mpsc::channel(4);
    let whip_state = Arc::new(WhipState::new(
        whip_factory,
        Arc::new(AllowAllAuthorizer),
        tx,
        dir.clone(),
        loopback_ip(),
        RtcMetrics::register(&prometheus::Registry::new()).unwrap(),
    ));
    let whep_state = Arc::new(WhepState::new(
        whep_factory,
        RtcMetrics::register(&prometheus::Registry::new()).unwrap(),
        DEFAULT_MAX_VIEWERS,
    ));
    let orch = orchestrator(whip_state.clone(), whep_state.clone(), dir);

    // Fail loud first: a transcoded profile / non-WHIP source registers nothing.
    let rtmp = spec(
        InputSpec::Rtmp {
            stream_key: "k".into(),
        },
        true,
    );
    orch.wire_whep(rtmp.id, IngestKind::Rtmp, "k", &rtmp).await;
    assert!(!whep_state.has_fanout(rtmp.id).await);
    let transcoded = spec(
        InputSpec::Whip {
            token: "tok".into(),
        },
        false,
    );
    orch.wire_whep(transcoded.id, IngestKind::Whip, "tok", &transcoded)
        .await;
    assert!(!whep_state.has_fanout(transcoded.id).await);

    // Real publisher.
    let (track, ssrc) = opus_publisher_track();
    let ph = TestPeerHandler::new();
    let ppc = publisher_factory.build(Arc::clone(&ph)).await.unwrap();
    ppc.add_track(Arc::clone(&track) as Arc<dyn TrackLocal>)
        .await
        .unwrap();
    let offer = offer_sdp(&ppc, &ph.gather_complete).await;
    let res = oneshot(
        whip::router(whip_state.clone()),
        post_sdp("/whip/tok", offer),
    )
    .await;
    assert_status(&res, StatusCode::CREATED);
    apply_answer(&ppc, body_text(res).await).await;
    tokio::time::timeout(Duration::from_secs(5), ph.connected.notified())
        .await
        .unwrap();

    let copy = spec(
        InputSpec::Whip {
            token: "tok".into(),
        },
        true,
    );
    orch.wire_whep(copy.id, IngestKind::Whip, "tok", &copy)
        .await;
    assert!(whep_state.has_fanout(copy.id).await);

    let (vpc, vh) = build_recvonly_peer(&viewer_factory).await;
    let voffer = offer_sdp(&vpc, &vh.gather_complete).await;
    let res = oneshot(
        whep::router(whep_state.clone()),
        post_sdp(&format!("/whep/7/{}", copy.id), voffer),
    )
    .await;
    assert_status(&res, StatusCode::CREATED);
    apply_answer(&vpc, body_text(res).await).await;
    tokio::time::timeout(Duration::from_secs(5), vh.connected.notified())
        .await
        .unwrap();

    let mut seq = 0u16;
    let mut received = 0;
    for _ in 0..200 {
        track.write_rtp(synthetic_packet(seq, ssrc)).await.unwrap();
        seq = seq.wrapping_add(1);
        tokio::time::sleep(Duration::from_millis(20)).await;
        received = vh.packets_received.load(Ordering::SeqCst);
        if received >= 5 {
            break;
        }
    }
    println!("WHEP viewer of the wired pipeline received {received} packets");
    assert!(received >= 5);

    orch.stop_pipeline(copy.id).await;
    assert!(
        !whep_state.has_fanout(copy.id).await,
        "teardown unregisters"
    );
    let _ = ppc.close().await;
    let _ = vpc.close().await;
}
