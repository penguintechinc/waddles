//! Receptionist teammate: greets a member once (on join, or on their first message), with a
//! configurable template and optional AI personalization. The name is never put in a prompt or
//! reply as text: the greeting carries the host's `{user:<uuid>}` egress placeholder, which the
//! host resolves to a display name at send time (PII stays outside the bundle).

use crate::ai;
use crate::config::ReceptionistCfg;
use crate::engine::Reply;
use crate::guard::{self, UNTRUSTED_DATA_NOTICE};
use crate::host::{Host, Level, key_part};

fn uuid_ok(u: &str) -> bool {
    !u.is_empty() && u.len() <= 64 && u.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
}

fn fnv(s: &str) -> u64 {
    s.bytes().fold(0xcbf2_9ce4_8422_2325u64, |h, b| {
        (h ^ u64::from(b)).wrapping_mul(0x0100_0000_01b3)
    })
}

/// Deterministic template choice per member (no RNG in a snapshotted component).
pub fn pick_template<'a>(cfg: &'a ReceptionistCfg, uuid: &str) -> &'a str {
    let i = (fnv(uuid) % cfg.templates.len() as u64) as usize;
    &cfg.templates[i]
}

/// Renders a template: `{user}` becomes the egress placeholder for `uuid`.
pub fn render(template: &str, uuid: &str) -> String {
    template.replace("{user}", &format!("{{user:{uuid}}}"))
}

/// Builds the greeting text. AI personalization adds ONE sanitized sentence after the
/// templated greeting; any AI failure degrades to the plain template (and is logged).
pub fn build_greeting<H: Host>(h: &H, cfg: &ReceptionistCfg, uuid: &str) -> String {
    let base = render(pick_template(cfg, uuid), uuid);
    if !cfg.ai_personalize {
        return base;
    }
    let system = format!(
        "Write one short, warm, single-sentence welcome line for a new member of an online community. Do not include any name, mention, link or command. Plain text only. {UNTRUSTED_DATA_NOTICE}"
    );
    match ai::complete(
        h,
        &system,
        "Write the welcome line.",
        60,
        0.7,
        "receptionist",
    ) {
        Ok(text) => match guard::sanitize_model_output(
            &text,
            cfg.max_greeting_chars
                .saturating_sub(base.chars().count())
                .max(40),
        ) {
            Some(line) => format!("{base} {line}"),
            None => base,
        },
        Err(e) => {
            h.log(
                Level::Warn,
                "ai-teammates",
                &serde_json::json!({"op": "greet_ai_failed", "code": ai::tool_code(&e)})
                    .to_string(),
            );
            base
        }
    }
}

/// Why a greeting trigger produced no reply (for logs/tests).
#[derive(Debug, PartialEq, Eq)]
pub enum Skip {
    NoChannel,
    AlreadyWelcomed,
    Identity,
    Backend,
}

/// Runs the welcome flow for the triggering actor. `origin_channel` is the channel the
/// triggering event came from, if any; `welcome_channel_id` config wins when set.
pub fn greet<H: Host>(
    h: &H,
    cfg: &ReceptionistCfg,
    platform: &str,
    origin_channel: Option<&str>,
) -> Result<Reply, Skip> {
    let channel = cfg
        .welcome_channel_id
        .as_deref()
        .or(origin_channel)
        .filter(|c| !c.is_empty());
    let Some(channel) = channel else {
        h.log(
            Level::Warn,
            "ai-teammates",
            r#"{"op":"greet_skipped","reason":"no_channel"}"#,
        );
        return Err(Skip::NoChannel);
    };
    let uuid = h.resolve_actor().map_err(|_| Skip::Identity)?;
    if !uuid_ok(&uuid) {
        return Err(Skip::Identity);
    }
    // Atomic claim: only the invocation that moves the counter 0 -> 1 welcomes (at-most-once,
    // also across redelivery and concurrent first messages).
    match h.kv_incr(&format!("welcomed.{}", key_part(&uuid)), 1, 0) {
        Ok(1) => {}
        Ok(_) => return Err(Skip::AlreadyWelcomed),
        Err(_) => return Err(Skip::Backend),
    }
    h.log(
        Level::Debug,
        "ai-teammates",
        r#"{"op":"greet","outcome":"welcomed"}"#,
    );
    Ok(Reply {
        platform: platform.to_string(),
        channel_id: channel.to_string(),
        text: build_greeting(h, cfg, &uuid),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_uses_the_egress_placeholder_not_a_name() {
        assert_eq!(
            render("Hi {user}, welcome!", "u-1"),
            "Hi {user:u-1}, welcome!"
        );
    }

    #[test]
    fn template_choice_is_deterministic_and_in_range() {
        let cfg = ReceptionistCfg {
            templates: vec!["a {user}".into(), "b {user}".into(), "c {user}".into()],
            ..Default::default()
        };
        for u in ["u-1", "u-2", "u-3", "u-4"] {
            assert_eq!(pick_template(&cfg, u), pick_template(&cfg, u));
            assert!(cfg.templates.iter().any(|t| t == pick_template(&cfg, u)));
        }
    }

    #[test]
    fn uuid_shape_is_enforced_before_it_enters_a_placeholder() {
        assert!(uuid_ok("0b9f-AA12"));
        assert!(!uuid_ok("a}b"));
        assert!(!uuid_ok(""));
        assert!(!uuid_ok("a:b"));
    }
}
