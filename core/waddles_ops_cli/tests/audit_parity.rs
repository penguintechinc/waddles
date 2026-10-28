//! Audit-chain parity against `hub_api/services/audit_chain.py` (vectors from
//! `scripts/gen_ops_cli_vectors.py`). The ensure_ascii + sorted-key + domain-prefix canonical
//! bytes are the risky part: they must match to the byte.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use serde::Deserialize;
use serde_json::{Map, Value};
use waddles_ops_cli::audit_chain::*;
use waddles_ops_cli::audit_cli::{parse_pins, report_line, Report};
use waddles_ops_cli::pyjson::dumps_canonical;

#[derive(Deserialize, Clone)]
struct Rec {
    chain_id: String,
    seq: i64,
    event_id: String,
    occurred_at: String,
    actor_uuid: Option<String>,
    actor_kind: String,
    category: String,
    action: String,
    outcome: String,
    target_type: Option<String>,
    target_id: Option<String>,
    details: Map<String, Value>,
    prev_hash: String,
    record_hash: String,
    hash_version: String,
}

impl From<Rec> for ChainRecord {
    fn from(r: Rec) -> Self {
        ChainRecord {
            chain_id: r.chain_id,
            seq: r.seq,
            event_id: r.event_id,
            occurred_at: r.occurred_at,
            actor_uuid: r.actor_uuid,
            actor_kind: r.actor_kind,
            category: r.category,
            action: r.action,
            outcome: r.outcome,
            target_type: r.target_type,
            target_id: r.target_id,
            details: r.details,
            prev_hash: r.prev_hash,
            record_hash: r.record_hash,
            hash_version: r.hash_version,
        }
    }
}

#[derive(Deserialize)]
struct Canon {
    record: Rec,
    canonical_hex: String,
    hash: String,
}

#[derive(Deserialize)]
struct Chain {
    name: String,
    records: Vec<Rec>,
    pin_seq: Option<i128>,
    pin_hash: Option<String>,
    status: String,
    examined: u64,
    head_seq: Option<i64>,
    head_hash: Option<String>,
    break_seq: Option<i64>,
    break_reason: Option<String>,
    break_detail: Option<String>,
}

#[derive(Deserialize)]
struct Vectors {
    canonical: Vec<Canon>,
    chains: Vec<Chain>,
}

fn vectors() -> Vectors {
    // serde_json (default features) parses the vector file; integer details stay integers.
    serde_json::from_str(include_str!("audit_vectors.json")).unwrap()
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

#[test]
fn canonical_bytes_and_hash_are_byte_identical_to_python() {
    let v = vectors();
    assert_eq!(v.canonical.len(), 6, "zero/short denominator");
    for (i, c) in v.canonical.iter().enumerate() {
        let rec: ChainRecord = c.record.clone().into();
        let bytes = canonical_bytes(&rec).unwrap();
        assert_eq!(
            hex(&bytes),
            c.canonical_hex,
            "canonical bytes diverge for vector {i}"
        );
        assert!(bytes.starts_with(b"waddles.audit.chain.v1\n"));
        assert!(bytes.is_ascii(), "ensure_ascii violated for vector {i}");
        assert_eq!(
            compute_hash(&rec).unwrap(),
            c.hash,
            "hash diverges for vector {i}"
        );
    }
}

#[test]
fn verifier_matches_python_on_every_scenario() {
    let v = vectors();
    assert_eq!(v.chains.len(), 12, "zero/short denominator");
    for c in &v.chains {
        let recs: Vec<ChainRecord> = c.records.iter().cloned().map(Into::into).collect();
        let res = verify_records(&recs, "tenant:7", c.pin_seq, c.pin_hash.as_deref());
        assert_eq!(res.status.as_str(), c.status, "{}: status", c.name);
        assert_eq!(res.examined, c.examined, "{}: examined", c.name);
        assert_eq!(res.head_seq, c.head_seq, "{}: head_seq", c.name);
        assert_eq!(res.head_hash, c.head_hash, "{}: head_hash", c.name);
        assert_eq!(
            res.break_.as_ref().and_then(|b| b.seq),
            c.break_seq,
            "{}: break seq",
            c.name
        );
        assert_eq!(
            res.break_.as_ref().map(|b| b.reason.as_str().to_string()),
            c.break_reason,
            "{}: reason",
            c.name
        );
        assert_eq!(
            res.break_.as_ref().map(|b| b.detail.clone()),
            c.break_detail,
            "{}: detail text",
            c.name
        );
    }
}

/// Expected CPython output written with `@` standing for a backslash (keeps this file free of
/// literal escape sequences that tooling may rewrite).
fn bs(s: &str) -> String {
    s.replace('@', "\\")
}

#[test]
fn ensure_ascii_serializer_edge_cases_match_cpython() {
    // Expected strings produced by CPython json.dumps(..., ensure_ascii=True).
    let cases: Vec<(Value, String)> = vec![
        (serde_json::json!("caf\u{e9}"), bs(r#""caf@u00e9""#)),
        (serde_json::json!("\u{1f600}"), bs(r#""@ud83d@ude00""#)),
        (serde_json::json!("a\u{7f}b"), bs(r#""a@u007fb""#)),
        (
            serde_json::json!("\u{2028}\u{2029}"),
            bs(r#""@u2028@u2029""#),
        ),
        (
            serde_json::json!("\n\r\t\u{8}\u{c}\u{1f}\u{0}"),
            bs(r#""@n@r@t@b@f@u001f@u0000""#),
        ),
        (serde_json::json!("q\"\\"), bs(r#""q@"@@""#)),
        (
            serde_json::json!({"b": [1, null, true], "a": {}}),
            r#"{"a":{},"b":[1,null,true]}"#.to_string(),
        ),
        (
            serde_json::json!(u64::MAX),
            "18446744073709551615".to_string(),
        ),
        (
            serde_json::json!(i64::MIN),
            "-9223372036854775808".to_string(),
        ),
    ];
    for (value, want) in cases {
        assert_eq!(dumps_canonical(&value).unwrap(), want);
    }
    // Floats are refused (fail-closed), never silently re-spelled.
    assert!(dumps_canonical(&serde_json::json!(1.5)).is_err());
}

#[test]
fn key_order_is_by_code_point_not_utf16() {
    // U+FFFF (BMP) sorts BEFORE U+1F600 by code point; UTF-16 order would reverse them.
    let mut m = Map::new();
    m.insert(String::from('\u{1f600}'), Value::from(1));
    m.insert(String::from('\u{ffff}'), Value::from(2));
    assert_eq!(
        dumps_canonical(&Value::Object(m)).unwrap(),
        bs(r#"{"@uffff":2,"@ud83d@ude00":1}"#)
    );
}

#[test]
fn pins_parse_like_the_python_regex() {
    let h = "a".repeat(64);
    let ok = parse_pins(&[format!("tenant:7={}:{h}", 5), format!("p.q-r_s=1:{h}\n")]).unwrap();
    assert_eq!(ok["tenant:7"].seq, 5);
    assert!(ok.contains_key("p.q-r_s"));
    for bad in [
        format!("c=0:{h}"),
        format!("c=05:{h}"),
        format!("c=1:{}", "A".repeat(64)),
        format!("c=1:{}", "a".repeat(63)),
        format!("=1:{h}"),
        format!("{}=1:{h}", "c".repeat(65)),
        "no-equals".to_string(),
        format!("c d=1:{h}"),
    ] {
        assert!(
            parse_pins(std::slice::from_ref(&bad)).is_err(),
            "should reject {bad:?}"
        );
    }
}

#[test]
fn report_lines_and_exit_codes_match_python_format() {
    let intact = Report {
        chain_id: "tenant:7".into(),
        verification: ChainVerification {
            status: ChainStatus::Intact,
            examined: 5,
            head_seq: Some(5),
            head_hash: Some("ab".repeat(32)),
            break_: None,
        },
        complete: true,
    };
    assert_eq!(
        report_line(&intact),
        format!(
            "chain=tenant:7 status=intact examined=5 complete=true head_seq=5 head_hash={}",
            "ab".repeat(32)
        )
    );
    let broken = Report {
        chain_id: "c".into(),
        verification: ChainVerification {
            status: ChainStatus::Broken,
            examined: 3,
            head_seq: Some(2),
            head_hash: None,
            break_: Some(ChainBreak {
                seq: Some(3),
                reason: BreakReason::HashMismatch,
                detail: String::new(),
            }),
        },
        complete: false,
    };
    assert_eq!(
        report_line(&broken),
        "chain=c status=broken examined=3 complete=false first_bad_seq=3 reason=hash_mismatch"
    );
    use waddles_ops_cli::audit_cli::RunSummary;
    let s = |i, b, e| RunSummary {
        chains: i + b + e,
        intact: i,
        broken: b,
        empty: e,
        records: 0,
    };
    assert_eq!(s(1, 0, 0).exit_code(false), 0);
    assert_eq!(s(1, 1, 0).exit_code(true), 1);
    assert_eq!(s(0, 0, 2).exit_code(false), 2);
    assert_eq!(
        s(0, 0, 0).exit_code(false),
        2,
        "zero chains examined must fail"
    );
    assert_eq!(s(0, 0, 2).exit_code(true), 0);
}
