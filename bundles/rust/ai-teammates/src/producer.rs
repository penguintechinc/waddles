//! Producer teammate (first draft): when a LIVE channel's chat has been quiet for N minutes,
//! posts one short conversation starter. Off by default; hard rate limits (interval floor,
//! hourly ceiling), quiet hours, kill switch, deterministic blocklist AND `ai.classify`
//! moderation. Chat content is never read into the prompt (only admin topics and the
//! platform-reported stream title/game, wrapped as untrusted); the post never targets a user.
//! On ANY failure it posts nothing.

use crate::ai;
use crate::config::ProducerCfg;
use crate::engine::Reply;
use crate::guard::{self, UNTRUSTED_DATA_NOTICE};
use crate::host::{Host, Level, key_part};

const THROTTLE_S: u32 = 30;
const MODERATION_INSTRUCTIONS: &str = "Decide whether this single chat message is safe to post in a live community stream chat. It is unsafe if it contains harassment, hate, sexual content, scams, spam, politics, personal data, links, mentions or commands.";
const PAUSED_KEY: &str = "producer.paused";

/// Result of one tick (the reason is a closed vocabulary, safe to log).
#[derive(Debug, PartialEq, Eq)]
pub enum Tick {
    Posted(Reply),
    Skipped(&'static str),
}

fn last_key(platform: &str, channel: &str) -> String {
    format!("producer.last.{}.{}", key_part(platform), key_part(channel))
}

fn log(h: &impl Host, level: Level, op: &str, reason: &str) {
    h.log(
        level,
        "ai-teammates",
        &serde_json::json!({"op": op, "reason": reason}).to_string(),
    );
}

/// Records human activity in a configured channel (called for every chat line).
pub fn note_activity<H: Host>(h: &H, cfg: &ProducerCfg, platform: &str, channel: &str) {
    if cfg.enabled
        && cfg
            .channels
            .iter()
            .any(|c| c.platform == platform && c.channel_id == channel)
    {
        let now = h.now_millis().to_string();
        if h.kv_set(&last_key(platform, channel), now.as_bytes(), 86_400)
            .is_err()
        {
            log(h, Level::Warn, "producer_note_failed", "kv");
        }
    }
}

/// Whether the kill switch is on.
pub fn is_paused<H: Host>(h: &H) -> Result<bool, crate::host::HostError> {
    Ok(h.kv_get(PAUSED_KEY)?.is_some_and(|v| v == b"1"))
}

/// Sets/clears the kill switch.
pub fn set_paused<H: Host>(h: &H, paused: bool) -> Result<(), crate::host::HostError> {
    h.kv_set(PAUSED_KEY, if paused { b"1" } else { b"0" }, 0)
}

/// True when `now_ms` falls inside the quiet-hours window.
pub fn in_quiet_hours(cfg: &ProducerCfg, now_ms: u64) -> bool {
    let Some(q) = cfg.quiet_hours else {
        return false;
    };
    let local_min = (now_ms as i64 / 60_000 + i64::from(q.utc_offset_minutes)).rem_euclid(1440);
    let hour = (local_min / 60) as u8;
    if q.start_hour < q.end_hour {
        (q.start_hour..q.end_hour).contains(&hour)
    } else {
        hour >= q.start_hour || hour < q.end_hour
    }
}

fn topic_hint(cfg: &ProducerCfg, now_ms: u64) -> Option<&str> {
    if cfg.topics.is_empty() {
        None
    } else {
        Some(&cfg.topics[(now_ms / 60_000 / 20) as usize % cfg.topics.len()])
    }
}

/// Runs one Producer tick: at most one post.
pub fn tick<H: Host>(h: &H, cfg: &ProducerCfg) -> Tick {
    if !cfg.enabled {
        return Tick::Skipped("disabled");
    }
    match is_paused(h) {
        Ok(false) => {}
        Ok(true) => return Tick::Skipped("paused"),
        Err(_) => return Tick::Skipped("kv_unavailable"),
    }
    match h.kv_incr("producer.tickgate", 1, THROTTLE_S) {
        Ok(1) => {}
        Ok(_) => return Tick::Skipped("throttled"),
        Err(_) => return Tick::Skipped("kv_unavailable"),
    }
    let now = h.now_millis();
    if in_quiet_hours(cfg, now) {
        return Tick::Skipped("quiet_hours");
    }
    let hour_key = format!("producer.hour.{}", now / 3_600_000);
    let posted_this_hour = match h.kv_get(&hour_key) {
        Ok(v) => v
            .and_then(|b| String::from_utf8(b).ok())
            .and_then(|s| s.parse::<u32>().ok())
            .unwrap_or(0),
        Err(_) => return Tick::Skipped("kv_unavailable"),
    };
    if posted_this_hour >= cfg.max_per_hour {
        return Tick::Skipped("hourly_cap");
    }
    // Find the first live + quiet channel (live status per platform fetched once).
    let mut live_cache: Vec<(String, Vec<ai::LiveChannel>)> = Vec::new();
    let mut target: Option<(String, String, Option<String>, Option<String>)> = None;
    for ch in &cfg.channels {
        if !live_cache.iter().any(|(p, _)| *p == ch.platform) {
            match ai::live_channels(h, &ch.platform) {
                Ok(v) => live_cache.push((ch.platform.clone(), v)),
                Err(e) => {
                    log(h, Level::Warn, "producer_live_failed", ai::tool_code(&e));
                    continue;
                }
            }
        }
        let live = live_cache
            .iter()
            .find(|(p, _)| *p == ch.platform)
            .and_then(|(_, v)| {
                v.iter()
                    .find(|c| c.channel_ref == ch.channel_id && c.is_live)
            });
        let Some(live) = live else { continue };
        let last = h
            .kv_get(&last_key(&ch.platform, &ch.channel_id))
            .ok()
            .flatten()
            .and_then(|b| String::from_utf8(b).ok())
            .and_then(|s| s.parse::<u64>().ok());
        match last {
            // No activity record yet (cold start): start the quiet window now, post later.
            None => note_activity(h, cfg, &ch.platform, &ch.channel_id),
            Some(t) if now.saturating_sub(t) >= u64::from(cfg.quiet_minutes) * 60_000 => {
                target = Some((
                    ch.platform.clone(),
                    ch.channel_id.clone(),
                    live.title.clone(),
                    live.category.clone(),
                ));
                break;
            }
            Some(_) => {}
        }
    }
    let Some((platform, channel, title, game)) = target else {
        return Tick::Skipped("no_quiet_live_channel");
    };
    // Reserve the interval slot BEFORE generating: a failed attempt still counts, so a broken
    // model backend can never be hammered or flood the channel.
    match h.kv_incr("producer.lock", 1, cfg.min_interval_minutes * 60) {
        Ok(1) => {}
        Ok(_) => return Tick::Skipped("min_interval"),
        Err(_) => return Tick::Skipped("kv_unavailable"),
    }
    let context = format!(
        "title: {}\ngame: {}",
        title.unwrap_or_default(),
        game.unwrap_or_default()
    );
    let mut prompt = guard::wrap_untrusted(&context, "channel_context", 200);
    if let Some(t) = topic_hint(cfg, now) {
        prompt.push_str(&format!("\nSuggested topic area: {t}"));
    }
    prompt.push_str("\nWrite the conversation starter now.");
    let system = format!(
        "You write ONE short, friendly conversation-starter question (at most 25 words) for a live stream chat that has gone quiet. Address the whole chat, never a specific person. No links, mentions, commands or hashtags. {UNTRUSTED_DATA_NOTICE}"
    );
    let text = match ai::complete(h, &system, &prompt, 80, 0.8) {
        Ok(t) => t,
        Err(e) => {
            log(
                h,
                Level::Warn,
                "producer_generate_failed",
                ai::tool_code(&e),
            );
            return Tick::Skipped("generate_failed");
        }
    };
    let Some(text) = guard::sanitize_model_output(&text, cfg.max_chars) else {
        return Tick::Skipped("empty_output");
    };
    if guard::hits_blocklist(&text, &cfg.blocklist) || !guard::scan_for_injection(&text).is_empty()
    {
        return Tick::Skipped("blocklist");
    }
    match ai::classify(h, &text, &["safe", "unsafe"], MODERATION_INSTRUCTIONS) {
        Ok(c) if c.label == "safe" && c.confidence >= 0.5 => {}
        Ok(_) => return Tick::Skipped("moderation_rejected"),
        Err(e) => {
            log(
                h,
                Level::Warn,
                "producer_moderation_failed",
                ai::tool_code(&e),
            );
            return Tick::Skipped("moderation_unavailable");
        }
    }
    if h.kv_incr(&hour_key, 1, 7200).is_err() {
        return Tick::Skipped("kv_unavailable");
    }
    note_activity(h, cfg, &platform, &channel); // our own post restarts the quiet window
    h.log(
        Level::Info,
        "ai-teammates",
        r#"{"op":"producer_post","outcome":"posted"}"#,
    );
    Tick::Posted(Reply {
        platform,
        channel_id: channel,
        text,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::QuietHours;

    fn q(start: u8, end: u8, off: i32) -> ProducerCfg {
        ProducerCfg {
            quiet_hours: Some(QuietHours {
                start_hour: start,
                end_hour: end,
                utc_offset_minutes: off,
            }),
            ..Default::default()
        }
    }

    #[test]
    fn quiet_hours_plain_and_wrapping_windows() {
        let h = |hour: u64| hour * 3_600_000;
        assert!(in_quiet_hours(&q(9, 17, 0), h(9)));
        assert!(!in_quiet_hours(&q(9, 17, 0), h(17)));
        assert!(in_quiet_hours(&q(22, 6, 0), h(23)));
        assert!(in_quiet_hours(&q(22, 6, 0), h(5)));
        assert!(!in_quiet_hours(&q(22, 6, 0), h(12)));
        // UTC-5: 03:00 UTC is 22:00 local, inside 22-06
        assert!(in_quiet_hours(&q(22, 6, -300), h(3)));
        assert!(!in_quiet_hours(&q(22, 6, -300), h(12)));
        assert!(!in_quiet_hours(&ProducerCfg::default(), h(3)));
    }
}
