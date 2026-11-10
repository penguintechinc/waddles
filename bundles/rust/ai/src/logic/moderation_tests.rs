//! Regression tests: moderation must match the normalized form that is
//! actually sent to the model / returned to the caller (zero-width, soft
//! hyphen, compat/fullwidth and case evasions), in both directions.

use std::sync::{Arc, Mutex};

use serde_json::json;

use super::*;

struct Ai(Mutex<String>, Mutex<usize>);

impl AiHost for Ai {
    fn generate(&self, _p: &str, _o: &GenOpts) -> Result<String, AiError> {
        *self.1.lock().unwrap() += 1;
        Ok(self.0.lock().unwrap().clone())
    }
}

struct Kv(Option<String>);

impl Store for Kv {
    fn get(&self, _k: &str) -> Result<Option<Vec<u8>>, String> {
        Ok(self.0.as_ref().map(|s| s.as_bytes().to_vec()))
    }
    fn increment(&self, _k: &str, d: i64, _t: u32) -> Result<i64, String> {
        Ok(d)
    }
}

struct Now;

impl Clock for Now {
    fn now_secs(&self) -> u64 {
        1_800_000_000
    }
}

fn svc(reply: &str, cfg: Option<&str>) -> (AiService, Arc<Ai>) {
    let ai = Arc::new(Ai(Mutex::new(reply.to_string()), Mutex::new(0)));
    let s = AiService::new(
        Arc::clone(&ai) as Arc<dyn AiHost>,
        Arc::new(Kv(cfg.map(str::to_string))),
        Arc::new(Now),
    );
    (s, ai)
}

fn admin() -> ToolContext {
    ToolContext {
        idempotency_key: None,
        actor: Some("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa".to_string()),
        actor_scopes: vec![ADMIN_SCOPE.to_string()],
    }
}

fn msg(e: ToolError) -> String {
    match e {
        ToolError::InvalidArgs(m) => m,
        o => format!("{o:?}"),
    }
}

const CFG: &str = r#"{"blocklist":["badword"]}"#;

/// Variants of "badword": invisibles, fullwidth/compat forms, mixed case.
const EVASIONS: [&str; 8] = [
    "bad\u{200b}word",
    "bad\u{2060}word",
    "bad\u{00ad}word",
    "b\u{200d}a\u{feff}d\u{034f}word",
    "\u{ff42}\u{ff41}\u{ff44}\u{ff57}\u{ff4f}\u{ff52}\u{ff44}",
    "BaDWoRd",
    "B\u{200b}A\u{00ad}D\u{ff57}ORD",
    "bad\u{e0041}word",
];

#[test]
fn evasions_blocked_on_input() {
    for ev in EVASIONS {
        let (s, ai) = svc("fine", Some(CFG));
        let e = s
            .complete(&admin(), json!({"prompt": format!("x {ev} y")}))
            .unwrap_err();
        assert_eq!(msg(e), "input_rejected", "{ev:?}");
        let e = s
            .complete(&admin(), json!({"prompt": "p", "system": ev}))
            .unwrap_err();
        assert_eq!(msg(e), "input_rejected", "{ev:?}");
        let e = s
            .classify(
                &admin(),
                json!({"text": "t", "labels": ["a", "b"], "instructions": ev}),
            )
            .unwrap_err();
        assert_eq!(msg(e), "input_rejected", "{ev:?}");
        assert_eq!(*ai.1.lock().unwrap(), 0, "{ev:?} must not reach the host");
    }
}

#[test]
fn evasions_blocked_on_output() {
    for ev in EVASIONS {
        let (s, _) = svc(&format!("a {ev} reply"), Some(CFG));
        let e = s
            .complete(&admin(), json!({"prompt": "clean"}))
            .unwrap_err();
        assert_eq!(msg(e), "content_filtered", "{ev:?}");
    }
}

#[test]
fn evasions_blocked_in_classify_reason() {
    for ev in EVASIONS {
        let reply = json!({"label": "a", "confidence": 0.5, "reason": format!("has {ev}")});
        let (s, _) = svc(&reply.to_string(), Some(CFG));
        let e = s
            .classify(&admin(), json!({"text": "t", "labels": ["a", "b"]}))
            .unwrap_err();
        assert_eq!(msg(e), "content_filtered", "{ev:?}");
    }
}

#[test]
fn configured_term_is_canonicalized() {
    let (s, _) = svc("ok", Some("{\"blocklist\":[\"B\\u200bAD\\uff57ord\"]}"));
    let e = s
        .complete(&admin(), json!({"prompt": "badword"}))
        .unwrap_err();
    assert_eq!(msg(e), "input_rejected");
}

#[test]
fn invisible_only_term_is_invalid_config() {
    let (s, _) = svc("ok", Some("{\"blocklist\":[\"\\u200b\"]}"));
    let e = s.complete(&admin(), json!({"prompt": "p"})).unwrap_err();
    assert_eq!(msg(e), "invalid_config");
}

#[test]
fn terms_with_neutralized_chars_block_input_and_output() {
    for (term, hit) in [
        ("<script>", "x <script> y"),
        ("a<b", "a<b"),
        (">", "1 > 0"),
        ("<", "if a < b"),
        ("<script>", "x \u{ff1c}script\u{ff1e} y"),
        ("<script>", "x <\u{200b}script> y"),
    ] {
        let cfg = json!({ "blocklist": [term] }).to_string();
        let (s, ai) = svc("fine", Some(&cfg));
        let e = s.complete(&admin(), json!({"prompt": hit})).unwrap_err();
        assert_eq!(msg(e), "input_rejected", "{term:?} in {hit:?}");
        assert_eq!(*ai.1.lock().unwrap(), 0);
        let (s, _) = svc(&format!("reply {hit} end"), Some(&cfg));
        let e = s
            .complete(&admin(), json!({"prompt": "clean"}))
            .unwrap_err();
        assert_eq!(msg(e), "content_filtered", "{term:?} in {hit:?}");
    }
}

#[test]
fn canonical_is_invariant_under_every_neutralize_rewrite() {
    let mut rewritten = 0;
    for cp in 0..=0x10FFFFu32 {
        let Some(c) = char::from_u32(cp) else {
            continue;
        };
        let s = c.to_string();
        let n = neutralize(&s);
        if n != s && !n.is_empty() {
            rewritten += 1;
            assert_eq!(canonical(&s), canonical(&n), "U+{cp:04X}");
        }
    }
    assert!(rewritten >= 2, "neutralize rewrote {rewritten} chars");
}

#[test]
fn clean_text_still_passes() {
    let (s, _) = svc("all good here", Some(CFG));
    assert!(s.complete(&admin(), json!({"prompt": "hello"})).is_ok());
    assert_eq!(canonical("a\u{200b}\u{00ad}B"), "ab");
}
