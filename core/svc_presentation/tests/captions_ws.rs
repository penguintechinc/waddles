//! Real-socket tests for the caption websocket: a genuinely bound listener
//! running the real `http::router`, a real `tokio-tungstenite` client, and a
//! real `PresentationHub` -- only the two external dependencies (the
//! credential/history database and the JWKS endpoint) are faked. Covers the
//! handshake-to-frame path, history replay, isolation between communities
//! and surfaces, keep-alive, backpressure and teardown.

mod common;

use std::time::Duration;

use axum::routing::get;
use futures::{SinkExt, StreamExt};
use overlay_auth::generate_view_token;
use overlay_schema::{OverlayPush, Surface};
use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

use common::*;
use svc_presentation::http::captions::run_caption_ws;
use svc_presentation::http::{router, AppState};
use svc_presentation::overlay::hub::{register_hub_metrics, PresentationHub};

type Client = WebSocketStream<MaybeTlsStream<TcpStream>>;

const WAIT: Duration = Duration::from_secs(5);

async fn connect(addr: std::net::SocketAddr, community: i64, key: &str) -> Client {
    let (client, _) =
        tokio_tungstenite::connect_async(format!("ws://{addr}/ws/captions/{community}?key={key}"))
            .await
            .expect("websocket handshake succeeds");
    client
}

/// The next *text* frame as JSON, skipping protocol ping/pong frames.
async fn next_json(client: &mut Client) -> serde_json::Value {
    loop {
        let message = tokio::time::timeout(WAIT, client.next())
            .await
            .expect("a frame arrives before the timeout")
            .expect("stream not ended")
            .expect("no transport error");
        match message {
            WsMessage::Text(text) => return serde_json::from_str(text.as_str()).unwrap(),
            WsMessage::Ping(_) | WsMessage::Pong(_) => continue,
            other => panic!("unexpected frame {other:?}"),
        }
    }
}

/// Asserts no text frame arrives within a short window.
async fn expect_silence(client: &mut Client) {
    let outcome = tokio::time::timeout(Duration::from_millis(150), async {
        loop {
            match client.next().await {
                Some(Ok(WsMessage::Ping(_) | WsMessage::Pong(_))) => continue,
                other => return other,
            }
        }
    })
    .await;
    assert!(outcome.is_err(), "expected silence, got {outcome:?}");
}

struct Harness {
    state: AppState,
    addr: std::net::SocketAddr,
    token: String,
    store: std::sync::Arc<FakeCaptionStore>,
}

async fn harness_with(store: FakeCaptionStore) -> Harness {
    let token = generate_view_token();
    let store = arc_store(store);
    let mut state = state_with_view_credential(42, &token, true);
    state.caption_store = store.clone();
    let addr = serve(router(state.clone())).await;
    Harness {
        state,
        addr,
        token,
        store,
    }
}

async fn wait_for_channels(state: &AppState, expected: usize) {
    let deadline = tokio::time::Instant::now() + WAIT;
    while state.hub.channel_count() != expected {
        assert!(
            tokio::time::Instant::now() < deadline,
            "hub channel count stuck at {}, wanted {expected}",
            state.hub.channel_count()
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

fn ws_metric(state: &AppState, outcome: &str) -> u64 {
    let rendered = svc_presentation::telemetry::render_metrics(&state.metrics).unwrap();
    let needle = format!("svc_presentation_caption_ws_connections_total{{outcome=\"{outcome}\"}} ");
    rendered
        .lines()
        .find_map(|line| line.strip_prefix(&needle))
        .and_then(|value| value.trim().parse().ok())
        .unwrap_or(0)
}

#[tokio::test]
async fn replays_history_oldest_first_then_streams_live_captions() {
    let now = chrono::Utc::now();
    let h = harness_with(FakeCaptionStore {
        history: std::sync::Mutex::new(vec![
            stored_caption("first", now - chrono::Duration::seconds(30)),
            stored_caption("second", now - chrono::Duration::seconds(10)),
        ]),
        ..Default::default()
    })
    .await;
    let mut client = connect(h.addr, 42, &h.token).await;

    // Replayed history: oldest first, no attribution name, never the author's
    // UUID or platform.
    for expected in ["first", "second"] {
        let frame = next_json(&mut client).await;
        assert_eq!(frame["type"], "caption");
        assert_eq!(frame["original"], expected);
        assert_eq!(frame["translated"], format!("{expected}-translated"));
        assert_eq!(frame["target_lang"], "en");
        assert!(frame.get("display_name").is_none(), "{frame}");
        assert!(frame.get("user").is_none());
        assert!(frame.get("platform").is_none());
        assert!(frame["timestamp"].as_str().unwrap().ends_with('Z'));
    }

    // A live caption now streams, with its display name.
    h.state
        .hub
        .publish(42, Surface::Caption, caption_push("live one"));
    let live = next_json(&mut client).await;
    assert_eq!(live["original"], "live one");
    assert_eq!(live["translated"], "hello");
    assert_eq!(live["display_name"], "Display Name");
    assert!(live.get("user").is_none());
    assert!(live.get("username").is_none());

    // History was queried for this community, over the 5-minute window,
    // capped at 10 -- the Python module's exact replay contract.
    let calls = h.store.recent_calls.lock().unwrap();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].0, 42);
    assert_eq!(calls[0].2, 10);
    let window = chrono::Utc::now() - calls[0].1;
    assert!((window.num_seconds() - 300).abs() < 10, "{window}");
    assert_eq!(ws_metric(&h.state, "accepted"), 1);
}

#[tokio::test]
async fn never_delivers_another_communitys_or_surfaces_frames() {
    let h = harness_with(FakeCaptionStore::default()).await;
    let mut client = connect(h.addr, 42, &h.token).await;

    h.state
        .hub
        .publish(43, Surface::Caption, caption_push("other community"));
    h.state.hub.publish(
        42,
        Surface::Chat,
        OverlayPush {
            text: Some("other surface".into()),
            ..Default::default()
        },
    );
    expect_silence(&mut client).await;

    h.state
        .hub
        .publish(42, Surface::Caption, caption_push("mine"));
    assert_eq!(next_json(&mut client).await["original"], "mine");
}

#[tokio::test]
async fn answers_both_ping_forms_with_a_pong_and_ignores_other_text() {
    let h = harness_with(FakeCaptionStore::default()).await;
    let mut client = connect(h.addr, 42, &h.token).await;

    client.send(WsMessage::Text("ping".into())).await.unwrap();
    assert_eq!(next_json(&mut client).await["type"], "pong");
    client
        .send(WsMessage::Text(r#"{"type":"ping"}"#.into()))
        .await
        .unwrap();
    assert_eq!(next_json(&mut client).await["type"], "pong");

    // Unrecognized text and binary frames get no reply and don't kill the
    // connection.
    client.send(WsMessage::Text("hello".into())).await.unwrap();
    client
        .send(WsMessage::Binary(vec![1, 2, 3].into()))
        .await
        .unwrap();
    expect_silence(&mut client).await;
    h.state
        .hub
        .publish(42, Surface::Caption, caption_push("still alive"));
    assert_eq!(next_json(&mut client).await["original"], "still alive");
}

#[tokio::test]
async fn keeps_streaming_live_captions_when_history_is_unavailable() {
    let h = harness_with(FakeCaptionStore {
        fail_recent: true,
        ..Default::default()
    })
    .await;
    let mut client = connect(h.addr, 42, &h.token).await;

    h.state.hub.publish(
        42,
        Surface::Caption,
        caption_push("live despite no history"),
    );
    assert_eq!(
        next_json(&mut client).await["original"],
        "live despite no history"
    );
    assert_eq!(ws_metric(&h.state, "accepted"), 1);
    assert_eq!(ws_metric(&h.state, "history_unavailable"), 1);
}

#[tokio::test]
async fn skips_an_invalid_hub_frame_and_keeps_streaming() {
    let h = harness_with(FakeCaptionStore::default()).await;
    let mut client = connect(h.addr, 42, &h.token).await;

    // Nothing but the validated ingest path publishes to this surface, so a
    // caption-less frame means an upstream bug -- it must be skipped loudly,
    // not crash the connection.
    h.state
        .hub
        .publish(42, Surface::Caption, OverlayPush::default());
    h.state
        .hub
        .publish(42, Surface::Caption, caption_push("after the bad one"));
    assert_eq!(
        next_json(&mut client).await["original"],
        "after the bad one"
    );
}

#[tokio::test]
async fn a_lagging_subscriber_skips_dropped_frames_and_keeps_the_newest() {
    let token = generate_view_token();
    let mut state = state_with_view_credential(42, &token, true);
    state.hub = std::sync::Arc::new(PresentationHub::with_capacity(
        2,
        register_hub_metrics(&prometheus::Registry::new()),
    ));
    let addr = serve(router(state.clone())).await;
    let mut client = connect(addr, 42, &token).await;

    // Publish a burst with no `.await` in between: on this single-threaded
    // test runtime the connection task cannot run until the burst is done,
    // so it is guaranteed to be many frames behind a capacity-2 buffer.
    for i in 0..50 {
        state
            .hub
            .publish(42, Surface::Caption, caption_push(&format!("c{i}")));
    }
    assert_eq!(next_json(&mut client).await["original"], "c48");
    assert_eq!(next_json(&mut client).await["original"], "c49");
}

#[tokio::test]
async fn closing_the_client_releases_the_hub_subscription() {
    let h = harness_with(FakeCaptionStore::default()).await;
    let mut client = connect(h.addr, 42, &h.token).await;
    wait_for_channels(&h.state, 1).await;

    client.close(None).await.unwrap();
    drop(client);
    wait_for_channels(&h.state, 0).await;
}

#[tokio::test]
async fn an_oversized_client_message_ends_the_connection() {
    let h = harness_with(FakeCaptionStore::default()).await;
    let mut client = connect(h.addr, 42, &h.token).await;
    wait_for_channels(&h.state, 1).await;

    // The overlay only ever sends a tiny keep-alive; a 64 KiB frame is abuse
    // and the websocket layer refuses it.
    client
        .send(WsMessage::Text("x".repeat(64 * 1024).into()))
        .await
        .ok();
    let ended = tokio::time::timeout(WAIT, async {
        loop {
            match client.next().await {
                None | Some(Err(_)) | Some(Ok(WsMessage::Close(_))) => return,
                Some(Ok(_)) => continue,
            }
        }
    })
    .await;
    assert!(ended.is_ok(), "server must end an oversized-frame session");
    wait_for_channels(&h.state, 0).await;
}

#[tokio::test]
async fn an_idle_connection_receives_heartbeat_pings() {
    // `run_caption_ws` is driven directly with a short interval (the routed
    // handler uses the real 15s one).
    let token = generate_view_token();
    let state = state_with_view_credential(42, &token, true);
    let subscription = state.hub.subscribe(42, Surface::Caption);
    let store: std::sync::Arc<dyn svc_presentation::overlay::CaptionStore> =
        arc_store(FakeCaptionStore::default());
    let metrics = state.caption_metrics.clone();

    let once = std::sync::Arc::new(tokio::sync::Mutex::new(Some(subscription)));
    let app = axum::Router::new().route(
        "/ws",
        get(move |ws: axum::extract::ws::WebSocketUpgrade| {
            let once = once.clone();
            let store = store.clone();
            let metrics = metrics.clone();
            async move {
                let subscription = once.lock().await.take().expect("handler called once");
                ws.on_upgrade(move |socket| {
                    run_caption_ws(
                        socket,
                        subscription,
                        store,
                        42,
                        metrics,
                        Duration::from_millis(20),
                    )
                })
            }
        }),
    );
    let addr = serve(app).await;
    let (mut client, _) = tokio_tungstenite::connect_async(format!("ws://{addr}/ws"))
        .await
        .unwrap();
    let ping = tokio::time::timeout(WAIT, client.next())
        .await
        .expect("a heartbeat ping arrives")
        .expect("stream item")
        .expect("no transport error");
    assert!(matches!(ping, WsMessage::Ping(_)), "{ping:?}");
}

// -- the whole pipeline behind the REAL guards: signed JWT in, websocket out --

#[tokio::test]
async fn a_signed_push_reaches_a_connected_viewer_end_to_end() {
    let token = generate_view_token();
    let store = arc_store(FakeCaptionStore::default());
    let mut state = state_with_real_push_trust_and_view(42, &token, true).await;
    state.caption_store = store.clone();
    let addr = serve(router(state.clone())).await;

    let mut client = connect(addr, 42, &token).await;
    wait_for_channels(&state, 1).await;

    let response = reqwest::Client::new()
        .post(format!("http://{addr}/overlay/42/caption/push"))
        .bearer_auth(sign_push_token(42))
        .json(&caption_push("end to end"))
        .send()
        .await
        .expect("push request");
    assert_eq!(response.status(), 200);
    let body: serde_json::Value = response.json().await.unwrap();
    assert_eq!(body["persisted"], true);

    let frame = next_json(&mut client).await;
    assert_eq!(frame["original"], "end to end");
    assert_eq!(frame["display_name"], "Display Name");
    assert_eq!(store.inserted.lock().unwrap().len(), 1);
}
