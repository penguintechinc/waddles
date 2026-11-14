//! Real-Valkey integration test: a delivered command's stamps survive real stream hops and every
//! stage records its samples.
//!
//! The three services live in separate crates, so this drives the SHARED code each of them calls --
//! with a REAL Valkey in the middle: real `XADD` entry ids (the publish time the queue-wait hop is
//! computed from), real consumer groups (`XREADGROUP`), the real tracestate carried inside the stream
//! entry, and the real canary result list. The stage functions called are exactly the ones
//! `svc_ingest::publish`, `svc_process::spine` and `svc_action::{dispatch,capabilities}` call.
//!
//! Needs a REAL Valkey: `VALKEY_URL` (CI exports it via `rust-crate-ci.yml`'s `valkey: true`) or
//! `COMMAND_LATENCY_VALKEY_URL`. The test FAILS, never skips, without one (a silently skipped real-path
//! test proves nothing). Run against a throwaway container:
//! `docker run --rm -d -p 127.0.0.1:16379:6379 valkey/valkey:8` +
//! `VALKEY_URL=redis://127.0.0.1:16379/ cargo test --test valkey_pipeline`.

use std::collections::HashMap;
use std::time::Duration;

use command_latency::canary;
use command_latency::stage::{self, Kind};
use command_latency::stamp::{now_ms, Door, Stamps};
use command_latency::{Hop, Outcome};
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};

async fn connect() -> redis::aio::MultiplexedConnection {
    let url = std::env::var("VALKEY_URL")
        .or_else(|_| std::env::var("COMMAND_LATENCY_VALKEY_URL"))
        .expect("VALKEY_URL must be set: this test needs a REAL Valkey (no silent skip)");
    let client = redis::Client::open(url).expect("valid valkey url");
    tokio::time::timeout(
        Duration::from_secs(5),
        client.get_multiplexed_async_connection(),
    )
    .await
    .expect("valkey connect timed out")
    .expect("valkey connect failed")
}

/// `XADD stream * tracestate <ts>` and return the real entry id.
async fn xadd(
    conn: &mut redis::aio::MultiplexedConnection,
    stream: &str,
    tracestate: &str,
) -> String {
    redis::cmd("XADD")
        .arg(stream)
        .arg("*")
        .arg("tracestate")
        .arg(tracestate)
        .query_async(conn)
        .await
        .expect("XADD")
}

/// `XREADGROUP` one entry as `(entry_id, tracestate)`.
async fn read_group(
    conn: &mut redis::aio::MultiplexedConnection,
    stream: &str,
    group: &str,
) -> (String, String) {
    let _: Result<(), _> = redis::cmd("XGROUP")
        .arg("CREATE")
        .arg(stream)
        .arg(group)
        .arg("0")
        .query_async(conn)
        .await;
    let reply: redis::Value = redis::cmd("XREADGROUP")
        .arg("GROUP")
        .arg(group)
        .arg("c1")
        .arg("COUNT")
        .arg(1)
        .arg("BLOCK")
        .arg(2000)
        .arg("STREAMS")
        .arg(stream)
        .arg(">")
        .query_async(conn)
        .await
        .expect("XREADGROUP");
    let (id, fields) = first_entry(&reply).expect("an entry was delivered");
    (
        id,
        fields.get("tracestate").cloned().expect("tracestate field"),
    )
}

fn first_entry(v: &redis::Value) -> Option<(String, HashMap<String, String>)> {
    // [[stream, [[id, [k, v, ...]]]]]  (RESP2)  or map form (RESP3)
    fn s(v: &redis::Value) -> Option<String> {
        match v {
            redis::Value::BulkString(b) => Some(String::from_utf8_lossy(b).into_owned()),
            redis::Value::SimpleString(s) => Some(s.clone()),
            _ => None,
        }
    }
    let streams: Vec<&redis::Value> = match v {
        redis::Value::Array(a) => a.iter().collect(),
        redis::Value::Map(m) => m.iter().flat_map(|(k, v)| [k, v]).collect(),
        _ => return None,
    };
    let entries = match streams.as_slice() {
        [redis::Value::Array(pair)] => match pair.as_slice() {
            [_, redis::Value::Array(entries)] => entries,
            _ => return None,
        },
        [_, redis::Value::Array(entries)] => entries,
        _ => return None,
    };
    let redis::Value::Array(entry) = entries.first()? else {
        return None;
    };
    let id = s(entry.first()?)?;
    let redis::Value::Array(kv) = entry.get(1)? else {
        return None;
    };
    let mut map = HashMap::new();
    for pair in kv.chunks(2) {
        map.insert(s(&pair[0])?, s(pair.get(1)?)?);
    }
    Some((id, map))
}

fn histogram_counts(exporter: &InMemoryMetricExporter) -> HashMap<(String, String), u64> {
    // (metric name, hop|command label) -> sample count
    let mut out = HashMap::new();
    for rm in exporter.get_finished_metrics().unwrap() {
        for sm in rm.scope_metrics() {
            for m in sm.metrics() {
                if let AggregatedMetrics::F64(MetricData::Histogram(h)) = m.data() {
                    for dp in h.data_points() {
                        let label = dp
                            .attributes()
                            .find(|kv| matches!(kv.key.as_str(), "hop" | "command" | "outcome"))
                            .map(|kv| kv.value.to_string())
                            .unwrap_or_default();
                        *out.entry((m.name().to_string(), label)).or_default() += dp.count();
                    }
                }
            }
        }
    }
    out
}

#[tokio::test(flavor = "multi_thread")]
async fn a_delivered_command_records_every_hop_and_the_e2e_across_real_stream_hops() {
    let mut conn = connect().await;
    command_latency::switch::set_disabled(false);
    let exporter = InMemoryMetricExporter::default();
    let provider = SdkMeterProvider::builder()
        .with_reader(PeriodicReader::builder(exporter.clone()).build())
        .build();
    opentelemetry::global::set_meter_provider(provider.clone());

    let run = now_ms();
    let ingest_stream = format!("cmdlat-test:{run}:ingest");
    let action_stream = format!("cmdlat-test:{run}:action");

    // ---- ingest: first door, then XADD with the stamped tracestate -------------------------
    let t0 = now_ms();
    let ingest_ts = Stamps {
        t0_ms: Some(t0),
        door: Some(Door::Gateway),
        ..Stamps::default()
    }
    .merge_into_tracestate(Some("vendor=1"));
    tokio::time::sleep(Duration::from_millis(30)).await; // receive/encrypt/bind
    let _ = xadd(&mut conn, &ingest_stream, &ingest_ts).await;
    command_latency::metrics::metrics().record_hop(
        Hop::IngestPublish,
        Duration::from_millis(now_ms().saturating_sub(t0)),
    );

    // ---- process: consumer-group read, queue wait from the REAL entry id -------------------
    tokio::time::sleep(Duration::from_millis(40)).await; // consumer-group lag
    let (entry_id, ts_in) = read_group(&mut conn, &ingest_stream, "process").await;
    let consumed = now_ms();
    let waited = stage::record_queue_wait(Hop::QueueWait, &entry_id, consumed)
        .expect("queue wait computed from the real entry id");
    assert!(
        waited >= 35,
        "the 40 ms we slept must show in queue wait, got {waited}"
    );
    tokio::time::sleep(Duration::from_millis(25)).await; // decrypt + tokenize + dispatch
    let ts_action = stage::process_handoff_tracestate(
        Some(&ts_in),
        &entry_id,
        consumed,
        now_ms(),
        Some("ping".to_string()),
    )
    .expect("stamped inbound is re-stamped");
    let _ = xadd(&mut conn, &action_stream, &ts_action).await;

    // ---- action: consumer-group read, invoke stamp, outbound confirm -----------------------
    tokio::time::sleep(Duration::from_millis(20)).await;
    let (action_entry, ts_a) = read_group(&mut conn, &action_stream, "action").await;
    let action_consumed = now_ms();
    stage::record_queue_wait(Hop::ActionQueueWait, &action_entry, action_consumed)
        .expect("action queue wait");
    let invoke = now_ms();
    let ts_invoke = stage::action_invoke_tracestate(Some(&ts_a), action_consumed, invoke)
        .expect("invoke stamp");
    tokio::time::sleep(Duration::from_millis(30)).await; // executor + guest
    let outbound = now_ms();
    tokio::time::sleep(Duration::from_millis(35)).await; // relay + drain + REST + ack
    let reported = stage::report_confirmed(
        Kind::User,
        Some(&ts_invoke),
        "waddles.core.example.ping",
        7,
        9,
        Outcome::Ok,
        outbound,
        now_ms(),
    )
    .expect("first door survived three real hops");

    // The e2e covers every sleep (30+40+25+20+30+35 = 180 ms) and the foreign member survived.
    assert!(reported.e2e_ms >= 170, "e2e {} ms", reported.e2e_ms);
    assert!(!reported.over_budget);
    assert!(
        ts_invoke.contains("vendor=1"),
        "foreign tracestate member preserved"
    );
    let s = Stamps::from_tracestate(Some(&ts_invoke));
    assert_eq!(s.t0_ms, Some(t0));
    assert_eq!(s.command.as_deref(), Some("ping"));
    for (name, v) in [
        ("publish", s.publish_ms),
        ("consume", s.consume_ms),
        ("handoff", s.handoff_ms),
        ("action_consume", s.action_consume_ms),
        ("invoke", s.invoke_ms),
    ] {
        assert!(v.is_some(), "stamp {name} missing after the hops");
    }

    provider.force_flush().unwrap();
    let counts = histogram_counts(&exporter);
    let hop = |h: &str| {
        counts
            .get(&("waddles_command_hop".to_string(), h.to_string()))
            .copied()
            .unwrap_or(0)
    };
    // Telemetry-validation assertion (zero = FAIL, counts printed).
    for h in [
        "ingest_publish",
        "queue_wait",
        "process_stage",
        "action_queue_wait",
        "dispatch_to_invoke",
        "executor_to_outbound",
        "outbound_call",
    ] {
        eprintln!("hop {h}: {} sample(s)", hop(h));
        assert_eq!(hop(h), 1, "hop {h} must have exactly one sample");
    }
    let e2e = counts
        .get(&("waddles_command_e2e".to_string(), "ping".to_string()))
        .copied()
        .unwrap_or(0);
    eprintln!("waddles_command_e2e{{command=ping}}: {e2e} sample(s)");
    assert_eq!(e2e, 1, "exactly one e2e sample for the delivered command");

    // ---- canary result list round trip through the REAL Valkey -----------------------------
    let _: () = redis::cmd("DEL")
        .arg(canary::RESULT_KEY)
        .query_async(&mut conn)
        .await
        .unwrap();
    let _: () = redis::cmd("LPUSH")
        .arg(canary::RESULT_KEY)
        .arg(canary::render_result("ok", reported.e2e_ms))
        .query_async(&mut conn)
        .await
        .unwrap();
    let popped: Option<String> = redis::cmd("RPOP")
        .arg(canary::RESULT_KEY)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(
        canary::parse_result(&popped.expect("canary result present")),
        Some(("ok".to_string(), reported.e2e_ms))
    );

    // cleanup (the test streams are unique per run, but leave no keys behind)
    let _: () = redis::cmd("DEL")
        .arg(&ingest_stream)
        .arg(&action_stream)
        .arg(canary::RESULT_KEY)
        .query_async(&mut conn)
        .await
        .unwrap();
}
