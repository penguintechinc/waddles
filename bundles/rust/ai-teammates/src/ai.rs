//! Typed clients for the tools this bundle CONSUMES. Shapes follow the authoritative contracts
//! (`bundles/contracts/ai-tools.v1.json`, `live-channels-contract.v1.json`); the fixtures in
//! `contracts/` are generated from them and every flow test validates live calls against them.
//! Tools are only ever called from code here; the model never selects a tool. Inputs are
//! validated against the producer bounds BEFORE the call and fail loud (never silently truncated,
//! which would cut a closing untrusted-data delimiter).

use serde::Deserialize;
use serde_json::{Value, json};
use waddle_sdk::tool::ToolError;

use crate::host::Host;

/// The AI service bundle that owns `ai.*`.
pub const AI_SERVICE: &str = "waddles.core.service.ai";
pub const TOOL_COMPLETE: &str = "ai.complete";
pub const TOOL_CLASSIFY: &str = "ai.classify";
pub const TOOL_RAG_QUERY: &str = "ai.rag.query";

/// Producer bounds from `ai-tools.v1.json`.
pub const MAX_SYSTEM_CHARS: usize = 1200;
pub const MAX_PROMPT_CHARS: usize = 3500;
pub const MAX_CLASSIFY_TEXT_CHARS: usize = 2000;
pub const MAX_QUESTION_CHARS: usize = 1000;
/// Collection used when the community configures none.
pub const DEFAULT_COLLECTION: &str = "knowledge";

/// Every `(target service, tool)` this bundle consumes; the manifest's `requested_tools` and
/// `tool.call:` permissions, and the contract fixtures, must match this list exactly.
pub const CONSUMED: [(&str, &str); 6] = [
    (AI_SERVICE, TOOL_COMPLETE),
    (AI_SERVICE, TOOL_CLASSIFY),
    (AI_SERVICE, TOOL_RAG_QUERY),
    ("waddles.core.service.twitch", "twitch.live.channels"),
    ("waddles.core.service.discord", "discord.live.channels"),
    ("waddles.core.service.youtube", "youtube.live.channels"),
];

/// Platform service app id + `live.channels` tool for a platform, if it has one.
pub fn live_tool(platform: &str) -> Option<(&'static str, &'static str)> {
    match platform {
        "twitch" => Some(("waddles.core.service.twitch", "twitch.live.channels")),
        "discord" => Some(("waddles.core.service.discord", "discord.live.channels")),
        "youtube" => Some(("waddles.core.service.youtube", "youtube.live.channels")),
        _ => None,
    }
}

/// True for a valid KB collection id (`^[a-z0-9][a-z0-9_.-]{0,39}$`).
pub fn collection_ok(c: &str) -> bool {
    let mut it = c.chars();
    c.len() <= 40
        && it
            .next()
            .is_some_and(|f| f.is_ascii_lowercase() || f.is_ascii_digit())
        && it.all(|ch| {
            ch.is_ascii_lowercase() || ch.is_ascii_digit() || matches!(ch, '_' | '.' | '-')
        })
}

fn invalid(msg: &str) -> ToolError {
    ToolError::InvalidArgs(msg.to_string())
}

/// `ai.complete` args (`system` is trusted bundle text, `prompt` carries wrapped untrusted data).
pub fn complete_args(system: &str, prompt: &str, max_tokens: u32, temperature: f64) -> Value {
    json!({"system": system, "prompt": prompt, "max_tokens": max_tokens, "temperature": temperature})
}

/// `ai.classify` args.
pub fn classify_args(text: &str, labels: &[&str], instructions: &str) -> Value {
    json!({"text": text, "labels": labels, "instructions": instructions})
}

/// `ai.rag.query` args (`collection` is required by the contract).
pub fn rag_args(question: &str, top_k: u32, collection: &str) -> Value {
    json!({"collection": collection, "question": question, "top_k": top_k})
}

/// `ai.live.channels` args (host resolves the community's connected channels itself).
pub fn live_args() -> Value {
    json!({"scope": "community", "limit": 100})
}

#[derive(Deserialize)]
struct CompleteOut {
    text: String,
}

/// KB answer: a synthesized answer plus the citations it is grounded in.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct RagAnswer {
    pub answer: String,
    pub citations: Vec<Citation>,
}

/// One grounding citation.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Citation {
    pub doc_id: String,
    pub chunk: u32,
    #[serde(default)]
    pub score: Option<f64>,
}

/// Classification verdict.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Classification {
    pub label: String,
    pub confidence: f64,
    pub reason: String,
}

/// One channel a platform service reports (`live-channels-contract.v1.json`).
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct LiveChannel {
    pub channel_ref: String,
    pub display_name: String,
    pub is_live: bool,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub category: Option<String>,
}

#[derive(Deserialize)]
struct LiveOut {
    channels: Vec<LiveChannel>,
}

fn call<H: Host, T: for<'de> Deserialize<'de>>(
    h: &H,
    target: &str,
    tool: &str,
    args: &Value,
) -> Result<T, ToolError> {
    let bytes = serde_json::to_vec(args).map_err(|e| ToolError::InvalidArgs(e.to_string()))?;
    let out = h.invoke(target, tool, &bytes, None)?;
    serde_json::from_slice(&out)
        .map_err(|e| ToolError::InvalidArgs(format!("bad {tool} response: {e}")))
}

/// `ai.complete` -> the model text (untrusted; sanitize before use). Bounds are checked first.
pub fn complete<H: Host>(
    h: &H,
    system: &str,
    prompt: &str,
    max_tokens: u32,
    temperature: f64,
) -> Result<String, ToolError> {
    if prompt.is_empty() || prompt.chars().count() > MAX_PROMPT_CHARS {
        return Err(invalid("prompt outside 1..=3500 chars"));
    }
    if system.chars().count() > MAX_SYSTEM_CHARS {
        return Err(invalid("system over 1200 chars"));
    }
    call::<H, CompleteOut>(
        h,
        AI_SERVICE,
        TOOL_COMPLETE,
        &complete_args(
            system,
            prompt,
            max_tokens.clamp(1, 512),
            temperature.clamp(0.0, 2.0),
        ),
    )
    .map(|o| o.text)
}

/// `ai.classify`.
pub fn classify<H: Host>(
    h: &H,
    text: &str,
    labels: &[&str],
    instructions: &str,
) -> Result<Classification, ToolError> {
    if text.is_empty() || text.chars().count() > MAX_CLASSIFY_TEXT_CHARS {
        return Err(invalid("text outside 1..=2000 chars"));
    }
    call(
        h,
        AI_SERVICE,
        TOOL_CLASSIFY,
        &classify_args(text, labels, instructions),
    )
}

/// `ai.rag.query` -> the grounded answer and citations.
pub fn rag_query<H: Host>(
    h: &H,
    question: &str,
    top_k: u32,
    collection: &str,
) -> Result<RagAnswer, ToolError> {
    if question.is_empty() || question.chars().count() > MAX_QUESTION_CHARS {
        return Err(invalid("question outside 1..=1000 chars"));
    }
    if !collection_ok(collection) {
        return Err(invalid("invalid collection id"));
    }
    call(
        h,
        AI_SERVICE,
        TOOL_RAG_QUERY,
        &rag_args(question, top_k.clamp(1, 8), collection),
    )
}

/// `<platform>.live.channels` (first page, up to 100 channels). Failures are loud tool errors.
pub fn live_channels<H: Host>(h: &H, platform: &str) -> Result<Vec<LiveChannel>, ToolError> {
    let (target, tool) = live_tool(platform)
        .ok_or_else(|| ToolError::InvalidArgs(format!("no live.channels tool for {platform}")))?;
    call::<H, LiveOut>(h, target, tool, &live_args()).map(|o| o.channels)
}

/// Closed vocabulary for a tool failure, safe to log and to show a user.
pub fn tool_code(e: &ToolError) -> &'static str {
    match e {
        ToolError::NotGranted => "not_granted",
        ToolError::Forbidden => "forbidden",
        ToolError::Timeout => "timeout",
        ToolError::DepthExceeded => "depth_exceeded",
        ToolError::PoolExhausted => "pool_exhausted",
        ToolError::TargetUnavailable => "unavailable",
        ToolError::InvalidArgs(_) => "invalid_args",
        ToolError::NoTransport => "no_transport",
    }
}

/// User-facing refusal text for a failed AI call (never echoes backend detail).
pub fn user_message(e: &ToolError) -> &'static str {
    match e {
        ToolError::InvalidArgs(_) | ToolError::Forbidden => "I can't help with that message.",
        ToolError::NotGranted => "The AI teammate isn't permitted to do that here yet.",
        _ => "The AI teammate is unavailable right now, please try again later.",
    }
}
