//! Connector-overlay golden-payload parity harness (P6b platform overlays).
//!
//! Feeds every corpus payload for a platform through an overlay component's
//! REAL `receiver.on-frame` (production `ConnectorHost::load` path, real
//! `identity` bridge) and diffs the `normalized-event` list against
//! `tests/data_plane_parity/expected/<platform>/*.json` field by field.
//! Workflow + env vars: `docs/connector-parity-harness.md`; entry point:
//! `make test-connector-parity PLATFORM=<twitch|discord|youtube>`.
//!
//! Tests here:
//! - `fixture_selfcheck` (always runs): proves the harness against the compiled
//!   `connector_tools_fixture.wasm`, incl. mutation tests that a changed field
//!   is NAMED in the report.
//! - `overlay_parity` (`--ignored`, driven by the make target): the real gate.
//!   `PARITY_OVERLAY_WASM` set -> that component; unset -> the fixture is run
//!   instead (harness proof only, loudly bannered). A set-but-missing path
//!   FAILS; nothing here ever skips.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use bundle_executor::connector::{
    ConnectionCtx, ConnectorCallError, ConnectorHost, ConnectorLimits, FrameError, RawFrame,
};
use bundle_executor::engine::{build_engine, spawn_epoch_ticker, LinkerCache};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use bundle_executor::parity_support::{pseudonym_uuid, scripted_bridge, CallLog};
use serde_json::Value;
use sha2::{Digest, Sha256};

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/connector_tools_fixture.wasm");
const RECEIVED_AT: &str = "2026-11-04T00:00:00.000Z";

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("repo root")
}

fn expected_dir(platform: &str) -> PathBuf {
    repo_root()
        .join("tests/data_plane_parity/expected")
        .join(platform)
}

fn fixture_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/parity/fixture")
}

// ---------------------------------------------------------------- vectors

/// One expected record (see `scripts/ci/gen-connector-parity-expected.py`).
#[derive(Clone, Debug)]
struct Vector {
    id: String,
    platform: String,
    input: Vec<u8>,
    context: Value,
    /// "event" | "dropped" | "frame_error"
    outcome: String,
    frame_error_kind: Option<String>,
    events: Vec<ExpEvent>,
}

#[derive(Clone, Debug)]
struct ExpEvent {
    platform: String,
    event_type: String,
    /// `(expected uuid, raw handle that must NOT leak)`
    actor: Option<(String, String)>,
    payload: Value,
}

fn s(v: &Value, k: &str) -> Result<String, String> {
    v.get(k)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| format!("missing string field `{k}`"))
}

fn parse_vector(v: &Value) -> Result<Vector, String> {
    let input = v.get("input").ok_or("missing `input`")?;
    let events = v
        .get("events")
        .and_then(Value::as_array)
        .ok_or("missing `events`")?
        .iter()
        .map(|e| {
            let actor = match e.get("actor") {
                None | Some(Value::Null) => None,
                Some(a) => Some((s(a, "uuid")?, s(a, "raw_handle")?)),
            };
            Ok(ExpEvent {
                platform: s(e, "platform")?,
                event_type: s(e, "event_type")?,
                actor,
                payload: e.get("payload").cloned().ok_or("missing `payload`")?,
            })
        })
        .collect::<Result<Vec<_>, String>>()?;
    Ok(Vector {
        id: s(v, "id")?,
        platform: s(v, "platform")?,
        input: s(input, "text")?.into_bytes(),
        context: v.get("context").cloned().unwrap_or(Value::Null),
        outcome: s(v, "outcome")?,
        frame_error_kind: v
            .get("frame_error_kind")
            .and_then(Value::as_str)
            .map(str::to_string),
        events,
    })
}

/// Loads every `*.json` expectation in `dir` except `divergences.json`; an
/// absent/empty directory is an error (zero vectors is never a pass).
fn load_vectors(dir: &Path) -> Result<Vec<Vector>, String> {
    let rd = std::fs::read_dir(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let mut paths: Vec<PathBuf> = rd
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| {
            p.extension().is_some_and(|x| x == "json")
                && p.file_name().is_some_and(|n| n != "divergences.json")
        })
        .collect();
    paths.sort();
    if paths.is_empty() {
        return Err(format!("{}: zero expected vectors", dir.display()));
    }
    paths
        .iter()
        .map(|p| {
            let raw = std::fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()))?;
            let v: Value =
                serde_json::from_str(&raw).map_err(|e| format!("{}: {e}", p.display()))?;
            parse_vector(&v).map_err(|e| format!("{}: {e}", p.display()))
        })
        .collect()
}

/// `divergences.json`: `{ "<vector id>": [ {"path","reason","tracking"} ] }`.
/// A declared path MUST occur (stale declarations fail) and every other
/// difference still fails.
fn load_divergences(dir: &Path) -> Result<BTreeMap<String, BTreeSet<String>>, String> {
    let p = dir.join("divergences.json");
    if !p.is_file() {
        return Ok(BTreeMap::new());
    }
    let raw = std::fs::read_to_string(&p).map_err(|e| format!("{}: {e}", p.display()))?;
    let v: Value = serde_json::from_str(&raw).map_err(|e| format!("{}: {e}", p.display()))?;
    let obj = v
        .as_object()
        .ok_or_else(|| format!("{}: not an object", p.display()))?;
    let mut out = BTreeMap::new();
    for (id, list) in obj {
        let mut paths = BTreeSet::new();
        for d in list.as_array().ok_or("divergence list must be an array")? {
            for k in ["path", "reason", "tracking"] {
                s(d, k).map_err(|e| format!("{}: {id}: {e}", p.display()))?;
            }
            paths.insert(s(d, "path")?);
        }
        out.insert(id.clone(), paths);
    }
    Ok(out)
}

// ------------------------------------------------------------------- diff

/// Field-level diff of two JSON values; paths are dotted (`payload.a.b`,
/// `payload.list[1]`). Returns one human line per differing leaf.
fn diff_json(path: &str, exp: &Value, got: &Value, out: &mut Vec<(String, String)>) {
    match (exp, got) {
        (Value::Object(e), Value::Object(g)) => {
            for (k, ev) in e {
                let p = format!("{path}.{k}");
                match g.get(k) {
                    Some(gv) => diff_json(&p, ev, gv, out),
                    None => out.push((p, format!("expected {ev}, got <absent>"))),
                }
            }
            for (k, gv) in g {
                if !e.contains_key(k) {
                    out.push((format!("{path}.{k}"), format!("unexpected field, got {gv}")));
                }
            }
        }
        (Value::Array(e), Value::Array(g)) => {
            for i in 0..e.len().max(g.len()) {
                let p = format!("{path}[{i}]");
                match (e.get(i), g.get(i)) {
                    (Some(ev), Some(gv)) => diff_json(&p, ev, gv, out),
                    (Some(ev), None) => out.push((p, format!("expected {ev}, got <absent>"))),
                    (None, Some(gv)) => out.push((p, format!("unexpected element, got {gv}"))),
                    (None, None) => {}
                }
            }
        }
        _ if exp == got => {}
        _ => out.push((path.to_string(), format!("expected {exp}, got {got}"))),
    }
}

/// RFC 3339 shape check only (`occurred-at` is an arrival timestamp).
fn rfc3339_shaped(t: &str) -> bool {
    let b = t.as_bytes();
    b.len() >= 20
        && b[4] == b'-'
        && b[7] == b'-'
        && b[10] == b'T'
        && b[13] == b':'
        && b[16] == b':'
        && (t.ends_with('Z') || t[19..].contains('+') || t[19..].contains('-'))
}

// ----------------------------------------------------------------- report

#[derive(Debug, Default)]
struct Report {
    label: String,
    examined: usize,
    passed: usize,
    /// `(vector id, [(path, detail)])`
    failures: Vec<(String, Vec<(String, String)>)>,
    host_calls: Vec<String>,
}

impl Report {
    fn failed(&self) -> usize {
        self.failures.len()
    }

    /// Greppable denominator + per-vector field diffs. Values are fixture data
    /// (synthetic handles/ids), never live PII.
    fn render(&self) -> String {
        let mut o = String::new();
        for (id, diffs) in &self.failures {
            o.push_str(&format!("FAIL {id}\n"));
            for (p, d) in diffs {
                o.push_str(&format!("    {p}: {d}\n"));
            }
        }
        let identity = self
            .host_calls
            .iter()
            .filter(|c| *c == "identity.lookup")
            .count();
        o.push_str(&format!(
            "connector_parity: {} examined={} passed={} failed={} identity_lookups={identity}\n",
            self.label,
            self.examined,
            self.passed,
            self.failed()
        ));
        o
    }
}

struct Overlay {
    host: ConnectorHost,
    calls: CallLog,
    _ticker: tokio::task::JoinHandle<()>,
}

fn limits() -> ConnectorLimits {
    ConnectorLimits {
        fuel: 500_000_000,
        deadline_ms: 10_000,
        memory_limit_mb: 128,
        pool_size: 4,
        pool_wait: Duration::from_millis(500),
        max_tool_args_bytes: 900_000,
        max_tool_result_bytes: 900_000,
    }
}

/// Loads `bytes` through the production connector path with the scripted
/// identity bridge. `expect_digest` (if any) must equal the bytes' sha256.
async fn load_overlay(
    app_id: &str,
    bytes: Vec<u8>,
    world: WitWorld,
    expect_digest: Option<&str>,
) -> Result<Overlay, String> {
    use clap::Parser;
    let digest = format!("sha256:{:x}", Sha256::digest(&bytes));
    if let Some(want) = expect_digest {
        if want != digest {
            return Err(format!(
                "overlay digest mismatch: PARITY_OVERLAY_DIGEST={want} actual={digest}"
            ));
        }
    }
    let cfg = bundle_executor::config::CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-ingest:8301",
    ])
    .map_err(|e| e.to_string())?;
    let engine = build_engine(&cfg).map_err(|e| e.to_string())?;
    let ticker = spawn_epoch_ticker(&engine);
    let manifest = VerifiedManifest::new(
        app_id,
        digest,
        world,
        // Overlays are first-party core connectors holding `connector.pii.read`.
        HashSet::from([bundle_executor::manifest::PERM_CONNECTOR_PII_READ.to_string()]),
    );
    let (bridge, calls) = scripted_bridge();
    let host = ConnectorHost::load(
        &engine,
        &LinkerCache::new(engine.clone()),
        &manifest,
        bytes,
        limits(),
        Some(Arc::clone(&bridge)),
    )
    .await
    .map_err(|e| format!("overlay failed to load: {e}"))?;
    Ok(Overlay {
        host,
        calls,
        _ticker: ticker,
    })
}

fn ctx_for(v: &Vector) -> ConnectionCtx {
    ConnectionCtx {
        platform: v.platform.clone(),
        source_id: "parity-source".to_string(),
        connection_id: format!("parity-{}", v.id),
        // Harness convention (hint only, never parsed by production hosts): the
        // corpus vector's context (self nick, guild roles, ...) as JSON.
        session_token: match &v.context {
            Value::Object(m) if !m.is_empty() => Some(v.context.to_string()),
            _ => None,
        },
    }
}

fn frame_error_kind(e: &FrameError) -> &'static str {
    match e {
        FrameError::Unsupported(_) => "unsupported",
        FrameError::Malformed(_) => "malformed",
        FrameError::Backend(_) => "backend",
    }
}

/// Runs every vector through `on-frame` and diffs. `Err` only for a harness
/// precondition failure (zero vectors, stale/undeclared divergence file).
async fn run(
    overlay: &Overlay,
    label: &str,
    vectors: &[Vector],
    divergences: &BTreeMap<String, BTreeSet<String>>,
) -> Result<Report, String> {
    if vectors.is_empty() {
        return Err(format!("{label}: zero vectors examined"));
    }
    let ids: BTreeSet<&str> = vectors.iter().map(|v| v.id.as_str()).collect();
    if let Some(unknown) = divergences.keys().find(|k| !ids.contains(k.as_str())) {
        return Err(format!("divergences.json names unknown vector `{unknown}`"));
    }
    let mut rep = Report {
        label: label.to_string(),
        ..Report::default()
    };
    for v in vectors {
        rep.examined += 1;
        let reply = overlay
            .host
            .on_frame(RawFrame {
                ctx: ctx_for(v),
                bytes: v.input.clone(),
                received_at: RECEIVED_AT.to_string(),
            })
            .await;
        let mut diffs: Vec<(String, String)> = Vec::new();
        match reply {
            Ok(r) => compare_events(v, &r.value, &mut diffs),
            Err(ConnectorCallError::Guest(fe)) => {
                let kind = frame_error_kind(&fe);
                match (v.outcome.as_str(), v.frame_error_kind.as_deref()) {
                    ("frame_error", Some(want)) if want == kind => {}
                    (o, _) => diffs.push((
                        "outcome".into(),
                        format!("expected {o}, overlay returned frame-error({kind})"),
                    )),
                }
            }
            Err(e) => diffs.push((
                "outcome".into(),
                format!("sandbox/host error `{}`", e.kind()),
            )),
        }
        // Apply declared divergences: they must actually occur.
        if let Some(declared) = divergences.get(&v.id) {
            let seen: BTreeSet<String> = diffs.iter().map(|(p, _)| p.clone()).collect();
            diffs.retain(|(p, _)| !declared.contains(p));
            for d in declared.difference(&seen) {
                diffs.push((
                    d.clone(),
                    "STALE declared divergence (no longer occurs)".into(),
                ));
            }
        }
        if diffs.is_empty() {
            rep.passed += 1;
        } else {
            rep.failures.push((v.id.clone(), diffs));
        }
    }
    rep.host_calls = overlay.calls.lock().expect("call log").clone();
    Ok(rep)
}

fn compare_events(
    v: &Vector,
    got: &[bundle_executor::connector::NormalizedEvent],
    diffs: &mut Vec<(String, String)>,
) {
    if v.outcome == "frame_error" {
        diffs.push((
            "outcome".into(),
            format!(
                "expected frame-error, overlay returned {} event(s)",
                got.len()
            ),
        ));
        return;
    }
    if got.len() != v.events.len() {
        diffs.push((
            "events.len".into(),
            format!("expected {}, got {}", v.events.len(), got.len()),
        ));
    }
    for (i, (e, g)) in v.events.iter().zip(got).enumerate() {
        let at = |f: &str| format!("events[{i}].{f}");
        if e.platform != g.platform {
            diffs.push((
                at("platform"),
                format!("expected {:?}, got {:?}", e.platform, g.platform),
            ));
        }
        if e.event_type != g.event_type {
            diffs.push((
                at("event_type"),
                format!("expected {:?}, got {:?}", e.event_type, g.event_type),
            ));
        }
        match (&e.actor, &g.actor) {
            (None, None) => {}
            (Some((uuid, _)), Some(a)) if uuid == a => {}
            (Some((_, raw)), Some(a)) if a == raw => diffs.push((
                at("actor"),
                "RAW HANDLE LEAKED: actor must be the tokenized pseudonym".into(),
            )),
            (exp, got_a) => diffs.push((
                at("actor"),
                format!("expected {:?}, got {got_a:?}", exp.as_ref().map(|(u, _)| u)),
            )),
        }
        if !rfc3339_shaped(&g.occurred_at) {
            diffs.push((
                at("occurred_at"),
                format!("not RFC 3339: {:?}", g.occurred_at),
            ));
        }
        match serde_json::from_str::<Value>(&g.payload_json) {
            Ok(gv) => diff_json(&at("payload"), &e.payload, &gv, diffs),
            Err(err) => diffs.push((at("payload_json"), format!("not JSON: {err}"))),
        }
    }
}

// ------------------------------------------------------------------ tests

/// The real gate. `PARITY_PLATFORM` (twitch|discord|youtube) is required.
/// Env: `PARITY_OVERLAY_WASM`, `PARITY_OVERLAY_DIGEST`, `PARITY_OVERLAY_APP_ID`,
/// `PARITY_OVERLAY_WORLD` (connector-tools|connector).
#[tokio::test]
#[ignore = "driven by `make test-connector-parity` (needs PARITY_PLATFORM)"]
async fn overlay_parity() {
    let platform = std::env::var("PARITY_PLATFORM")
        .expect("PARITY_PLATFORM must be set to twitch|discord|youtube");
    assert!(
        ["twitch", "discord", "youtube"].contains(&platform.as_str()),
        "unknown PARITY_PLATFORM {platform:?}"
    );
    let vectors = load_vectors(&expected_dir(&platform)).expect("expected vectors");
    let div = load_divergences(&expected_dir(&platform)).expect("divergences");
    println!(
        "connector_parity: corpus_ready platform={platform} vectors={}",
        vectors.len()
    );
    match std::env::var("PARITY_OVERLAY_WASM") {
        Ok(path) => {
            let bytes = std::fs::read(&path).unwrap_or_else(|e| {
                panic!("PARITY_OVERLAY_WASM={path} not readable: {e} (overlay path missing is a FAILURE, never a skip)")
            });
            let world = match std::env::var("PARITY_OVERLAY_WORLD").as_deref() {
                Ok("connector") => WitWorld::Connector,
                Ok("connector-tools") | Err(_) => WitWorld::ConnectorTools,
                Ok(other) => {
                    panic!("PARITY_OVERLAY_WORLD={other:?}: want connector|connector-tools")
                }
            };
            let app = std::env::var("PARITY_OVERLAY_APP_ID")
                .unwrap_or_else(|_| format!("waddles.core.parity.{platform}"));
            let digest = std::env::var("PARITY_OVERLAY_DIGEST").ok();
            let overlay = load_overlay(&app, bytes, world, digest.as_deref())
                .await
                .expect("overlay load");
            let rep = run(
                &overlay,
                &format!("platform={platform} overlay={path}"),
                &vectors,
                &div,
            )
            .await
            .expect("run");
            let out = rep.render();
            println!("{out}");
            assert_eq!(rep.failed(), 0, "overlay diverges from the corpus:\n{out}");
        }
        Err(_) => {
            println!(
                "connector_parity: *** NO PARITY_OVERLAY_WASM -- HARNESS SELF-CHECK against the fixture component, NOT overlay parity ***"
            );
            let rep = fixture_run(&platform, None).await;
            println!("{}", rep.render());
            assert_eq!(rep.failed(), 0, "{}", rep.render());
        }
    }
}

type Mutator<'a> = &'a dyn Fn(&mut Vec<Vector>);

async fn fixture_run(label: &str, mutate: Option<Mutator<'_>>) -> Report {
    let mut vectors = load_vectors(&fixture_dir()).expect("fixture vectors");
    if let Some(m) = mutate {
        m(&mut vectors);
    }
    let overlay = load_overlay(
        "waddles.core.parity.fixture",
        FIXTURE_WASM.to_vec(),
        WitWorld::ConnectorTools,
        None,
    )
    .await
    .expect("fixture loads");
    run(
        &overlay,
        &format!("self-check({label})"),
        &vectors,
        &BTreeMap::new(),
    )
    .await
    .expect("run")
}

#[tokio::test]
async fn fixture_selfcheck_passes_and_counts() {
    let rep = fixture_run("fixture", None).await;
    assert!(rep.examined >= 3, "{}", rep.render());
    assert_eq!(rep.failed(), 0, "{}", rep.render());
    assert_eq!(rep.passed, rep.examined);
}

#[tokio::test]
async fn mutating_one_expected_field_names_that_field() {
    let rep = fixture_run(
        "mutation",
        Some(&|vs: &mut Vec<Vector>| {
            vs[0].events[0].payload["text"] = Value::String("MUTATED".into());
        }),
    )
    .await;
    let out = rep.render();
    assert_eq!(rep.failed(), 1, "{out}");
    assert!(out.contains("events[0].payload.text"), "{out}");
    assert!(out.contains("MUTATED"), "{out}");
    assert!(out.contains("failed=1"), "{out}");
}

#[tokio::test]
async fn wrong_event_type_actor_and_count_are_all_reported() {
    let rep = fixture_run(
        "shape",
        Some(&|vs: &mut Vec<Vector>| {
            vs[0].events[0].event_type = "follow".into();
            vs[0].events[0].actor = Some(("u".into(), "raw".into()));
            let dup = vs[0].events[0].clone();
            vs[0].events.push(dup);
        }),
    )
    .await;
    let out = rep.render();
    assert!(out.contains("events.len"), "{out}");
    assert!(out.contains("events[0].event_type"), "{out}");
    assert!(out.contains("events[0].actor"), "{out}");
}

#[tokio::test]
async fn zero_vectors_is_a_failure_not_a_pass() {
    let overlay = load_overlay(
        "waddles.core.parity.fixture",
        FIXTURE_WASM.to_vec(),
        WitWorld::ConnectorTools,
        None,
    )
    .await
    .unwrap();
    let err = run(&overlay, "x", &[], &BTreeMap::new()).await.unwrap_err();
    assert!(err.contains("zero vectors"), "{err}");
    let dir = std::env::temp_dir().join(format!("parity-empty-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let e = load_vectors(&dir).unwrap_err();
    std::fs::remove_dir_all(&dir).unwrap();
    assert!(e.contains("zero expected vectors"), "{e}");
    assert!(load_vectors(&dir).is_err(), "absent dir must error");
}

#[tokio::test]
async fn declared_divergence_is_honoured_and_stale_ones_fail() {
    let overlay = load_overlay(
        "waddles.core.parity.fixture",
        FIXTURE_WASM.to_vec(),
        WitWorld::ConnectorTools,
        None,
    )
    .await
    .unwrap();
    let mut vs = load_vectors(&fixture_dir()).unwrap();
    vs[0].events[0].payload["text"] = Value::String("MUTATED".into());
    let id = vs[0].id.clone();
    let declared = |p: &str| BTreeMap::from([(id.clone(), BTreeSet::from([p.to_string()]))]);

    let ok = run(&overlay, "d", &vs, &declared("events[0].payload.text"))
        .await
        .unwrap();
    assert_eq!(ok.failed(), 0, "{}", ok.render());

    let stale = run(&overlay, "d", &vs, &declared("events[0].payload.nope"))
        .await
        .unwrap();
    let out = stale.render();
    assert!(out.contains("STALE declared divergence"), "{out}");
    assert!(out.contains("events[0].payload.text"), "{out}");

    let err = run(
        &overlay,
        "d",
        &vs,
        &BTreeMap::from([("no/such".to_string(), BTreeSet::new())]),
    )
    .await
    .unwrap_err();
    assert!(err.contains("unknown vector"), "{err}");
}

#[tokio::test]
async fn bad_overlay_inputs_fail_loud() {
    let err = load_overlay(
        "waddles.core.parity.x",
        b"not wasm".to_vec(),
        WitWorld::ConnectorTools,
        None,
    )
    .await
    .err()
    .expect("garbage must not load");
    assert!(err.contains("failed to load"), "{err}");
    let err = load_overlay(
        "waddles.core.parity.fixture",
        FIXTURE_WASM.to_vec(),
        WitWorld::ConnectorTools,
        Some("sha256:00"),
    )
    .await
    .err()
    .expect("digest pin");
    assert!(err.contains("digest mismatch"), "{err}");
}

/// The expected files must stay current with the corpus + the Rust pseudonym
/// function (the Python generator mirrors it); counts are printed, not assumed.
#[test]
fn expected_files_cover_the_corpus_and_actors_match_the_stub() {
    let corpus = repo_root().join("tests/data_plane_parity/corpus");
    let mut total = 0;
    for (platform, min) in [("twitch", 23), ("discord", 10), ("youtube", 1)] {
        let vectors = load_vectors(&expected_dir(platform)).expect("expected vectors");
        let corpus_n = std::fs::read_dir(corpus.join(platform))
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
            .count();
        assert_eq!(
            vectors.len(),
            corpus_n,
            "{platform}: expected/ vs corpus count"
        );
        assert!(
            vectors.len() >= min,
            "{platform}: {} < {min}",
            vectors.len()
        );
        for v in &vectors {
            for e in &v.events {
                if let Some((uuid, raw)) = &e.actor {
                    assert_ne!(uuid, raw, "{}", v.id);
                    assert_eq!(uuid.len(), 36, "{}", v.id);
                }
            }
        }
        total += vectors.len();
    }
    // Direct cross-check Python <-> Rust pseudonym for a known expectation.
    let v = parse_vector(
        &serde_json::from_str::<Value>(
            &std::fs::read_to_string(expected_dir("twitch").join("irc_full_tag_set.json")).unwrap(),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(
        v.events[0].actor.as_ref().unwrap().0,
        pseudonym_uuid("twitch", "999")
    );
    println!("connector_parity: expected_current examined={total}");
}

/// Proves the identity bridge is the REAL host-call path: a compiled connector
/// whose `on-connect` performs `identity.lookup` reaches the scripted stage.
#[tokio::test]
async fn identity_bridge_serves_real_lookups() {
    const IDENTITY_CONNECTOR: &[u8] = include_bytes!("fixtures/connector_fixture.wasm");
    let overlay = load_overlay(
        "waddles.core.parity.identity",
        IDENTITY_CONNECTOR.to_vec(),
        WitWorld::Connector,
        None,
    )
    .await
    .unwrap();
    overlay
        .host
        .on_connect(ConnectionCtx {
            platform: "twitch".into(),
            source_id: "999".into(),
            connection_id: "c".into(),
            session_token: None,
        })
        .await
        .unwrap();
    let calls = overlay.calls.lock().unwrap().clone();
    assert!(calls.iter().any(|c| c == "identity.lookup"), "{calls:?}");
}
