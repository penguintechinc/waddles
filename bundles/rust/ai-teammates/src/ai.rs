//! Typed clients for the tools this bundle CONSUMES. The request builders and response
//! shapes are pinned by the JSON-Schema contract fixtures in `contracts/` (see
//! `tests/contract.rs`), so a mismatch with the service that owns the tool surfaces in CI.
//! Tools are only ever called from code here; the model never selects a tool.

use serde::Deserialize;
use serde_json::{Value, json};
use waddle_sdk::tool::ToolError;

use crate::host::Host;

/// The AI service bundle that owns `ai.*`.
pub const AI_SERVICE: &str = "waddles.core.service.ai";
pub const TOOL_COMPLETE: &str = "ai.complete";
pub const TOOL_CLASSIFY: &str = "ai.classify";
pub const TOOL_RAG_QUERY: &str = "ai.rag.query";

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

/// `ai.complete` args (`system` is trusted bundle text, `prompt` carries wrapped untrusted data).
pub fn complete_args(
    system: &str,
    prompt: &str,
    max_tokens: u32,
    temperature: f64,
    purpose: &str,
) -> Value {
    json!({"system": system, "prompt": prompt, "max_tokens": max_tokens, "temperature": temperature, "purpose": purpose})
}

/// `ai.classify` args.
pub fn classify_args(text: &str, labels: &[&str], purpose: &str) -> Value {
    json!({"text": text, "labels": labels, "purpose": purpose})
}

/// `ai.rag.query` args.
pub fn rag_args(query: &str, top_k: u32, collection: Option<&str>) -> Value {
    let mut v = json!({"query": query, "top_k": top_k});
    if let Some(c) = collection {
        v["collection"] = Value::String(c.to_string());
    }
    v
}

#[derive(Deserialize)]
struct CompleteOut {
    text: String,
}

/// One retrieved knowledge-base chunk.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct RagMatch {
    pub text: String,
    #[serde(default)]
    pub source: Option<String>,
    pub score: f64,
}

#[derive(Deserialize)]
struct RagOut {
    matches: Vec<RagMatch>,
}

/// Classification verdict.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Classification {
    pub label: String,
    pub score: f64,
}

/// One channel a platform service reports.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct LiveChannel {
    pub channel_id: String,
    pub live: bool,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub game: Option<String>,
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

/// `ai.complete` -> the model text (untrusted; sanitize before use).
pub fn complete<H: Host>(
    h: &H,
    system: &str,
    prompt: &str,
    max_tokens: u32,
    temperature: f64,
    purpose: &str,
) -> Result<String, ToolError> {
    call::<H, CompleteOut>(
        h,
        AI_SERVICE,
        TOOL_COMPLETE,
        &complete_args(system, prompt, max_tokens, temperature, purpose),
    )
    .map(|o| o.text)
}

/// `ai.classify`.
pub fn classify<H: Host>(
    h: &H,
    text: &str,
    labels: &[&str],
    purpose: &str,
) -> Result<Classification, ToolError> {
    call(
        h,
        AI_SERVICE,
        TOOL_CLASSIFY,
        &classify_args(text, labels, purpose),
    )
}

/// `ai.rag.query` -> matches, best first.
pub fn rag_query<H: Host>(
    h: &H,
    query: &str,
    top_k: u32,
    collection: Option<&str>,
) -> Result<Vec<RagMatch>, ToolError> {
    call::<H, RagOut>(
        h,
        AI_SERVICE,
        TOOL_RAG_QUERY,
        &rag_args(query, top_k, collection),
    )
    .map(|o| o.matches)
}

/// `<platform>.live.channels`.
pub fn live_channels<H: Host>(h: &H, platform: &str) -> Result<Vec<LiveChannel>, ToolError> {
    let (target, tool) = live_tool(platform)
        .ok_or_else(|| ToolError::InvalidArgs(format!("no live.channels tool for {platform}")))?;
    call::<H, LiveOut>(h, target, tool, &json!({})).map(|o| o.channels)
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
