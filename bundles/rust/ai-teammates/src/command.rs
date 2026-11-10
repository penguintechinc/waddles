//! Chat command grammar. Mirrors the shared `waddle_sdk.command` grammar
//! (`!<command> [sub-module] [verb] <input>` with the fixed verb vocabulary
//! `set/enable/disable/list/reset`) because `waddle-sdk-rs` has no verb parser yet; the
//! teammates are the "sub-modules". Pure text in, structured value out.

use crate::config::TEAMMATES;

/// Parsed `!teammate` request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TeammateCmd {
    List,
    Status(String),
    Enable(String),
    Disable(String),
    Set {
        teammate: String,
        key: String,
        value: String,
    },
    Reset(String),
}

/// Parsed `!producer` request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProducerCmd {
    Pause,
    Resume,
    Status,
}

/// Which command head the text starts with.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Parsed {
    Research(String),
    Onboard(String),
    Teammate(Result<TeammateCmd, String>),
    Producer(Result<ProducerCmd, String>),
}

/// Usage text for `!teammate`.
pub const TEAMMATE_USAGE: &str = "Usage: !teammate list | !teammate enable|disable [researcher|receptionist|producer] | !teammate set [teammate] [key] [value] | !teammate reset [teammate]";

fn is_teammate(s: &str) -> bool {
    TEAMMATES.contains(&s)
}

fn parse_teammate(rest: &str) -> Result<TeammateCmd, String> {
    let toks: Vec<String> = rest
        .split_whitespace()
        .map(|t| t.to_ascii_lowercase())
        .collect();
    let err = || TEAMMATE_USAGE.to_string();
    match toks.as_slice() {
        [] => Ok(TeammateCmd::List),
        [v] if v == "list" => Ok(TeammateCmd::List),
        [m] if is_teammate(m) => Ok(TeammateCmd::Status(m.clone())),
        [a, b] => match (a.as_str(), b.as_str()) {
            ("enable", m) if is_teammate(m) => Ok(TeammateCmd::Enable(m.to_string())),
            (m, "enable") if is_teammate(m) => Ok(TeammateCmd::Enable(m.to_string())),
            ("disable", m) if is_teammate(m) => Ok(TeammateCmd::Disable(m.to_string())),
            (m, "disable") if is_teammate(m) => Ok(TeammateCmd::Disable(m.to_string())),
            ("reset", m) if is_teammate(m) => Ok(TeammateCmd::Reset(m.to_string())),
            (m, "reset") if is_teammate(m) => Ok(TeammateCmd::Reset(m.to_string())),
            _ => Err(err()),
        },
        [a, b, key, value] if a == "set" && is_teammate(b) => Ok(TeammateCmd::Set {
            teammate: b.clone(),
            key: key.clone(),
            value: value.clone(),
        }),
        [a, b, key, value] if b == "set" && is_teammate(a) => Ok(TeammateCmd::Set {
            teammate: a.clone(),
            key: key.clone(),
            value: value.clone(),
        }),
        _ => Err(err()),
    }
}

fn parse_producer(rest: &str) -> Result<ProducerCmd, String> {
    match rest.trim().to_ascii_lowercase().as_str() {
        "pause" => Ok(ProducerCmd::Pause),
        "resume" => Ok(ProducerCmd::Resume),
        "" | "status" => Ok(ProducerCmd::Status),
        _ => Err("Usage: !producer pause | resume | status".to_string()),
    }
}

/// Splits a chat line into a recognised command, or `None` for ordinary chat. The head
/// must be followed by whitespace or end of text (`!researchfoo` is not `!research`).
pub fn parse(text: &str) -> Option<Parsed> {
    let text = text.trim();
    let body = text.strip_prefix('!')?;
    let (head, rest) = match body.find(char::is_whitespace) {
        Some(i) => (&body[..i], body[i..].trim()),
        None => (body, ""),
    };
    match head.to_ascii_lowercase().as_str() {
        "research" => Some(Parsed::Research(rest.to_string())),
        "onboard" => Some(Parsed::Onboard(rest.to_string())),
        "teammate" => Some(Parsed::Teammate(parse_teammate(rest))),
        "producer" => Some(Parsed::Producer(parse_producer(rest))),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heads_require_a_word_boundary() {
        assert_eq!(
            parse("!research who is x"),
            Some(Parsed::Research("who is x".into()))
        );
        assert_eq!(parse("  !RESEARCH  q "), Some(Parsed::Research("q".into())));
        assert_eq!(parse("!researcher x"), None);
        assert_eq!(parse("research x"), None);
        assert_eq!(parse("!ask x"), None);
        assert_eq!(
            parse("!onboard where are rules"),
            Some(Parsed::Onboard("where are rules".into()))
        );
    }

    #[test]
    fn teammate_verbs_in_both_orders() {
        use TeammateCmd::*;
        let p = |s: &str| match parse(s) {
            Some(Parsed::Teammate(r)) => r,
            other => panic!("{other:?}"),
        };
        assert_eq!(p("!teammate"), Ok(List));
        assert_eq!(p("!teammate list"), Ok(List));
        assert_eq!(p("!teammate producer"), Ok(Status("producer".into())));
        assert_eq!(
            p("!teammate enable researcher"),
            Ok(Enable("researcher".into()))
        );
        assert_eq!(
            p("!teammate researcher enable"),
            Ok(Enable("researcher".into()))
        );
        assert_eq!(
            p("!teammate DISABLE Producer"),
            Ok(Disable("producer".into()))
        );
        assert_eq!(
            p("!teammate reset receptionist"),
            Ok(Reset("receptionist".into()))
        );
        assert_eq!(
            p("!teammate set producer max_per_hour 2"),
            Ok(Set {
                teammate: "producer".into(),
                key: "max_per_hour".into(),
                value: "2".into()
            })
        );
        assert_eq!(
            p("!teammate producer set quiet_minutes 15"),
            Ok(Set {
                teammate: "producer".into(),
                key: "quiet_minutes".into(),
                value: "15".into()
            })
        );
    }

    #[test]
    fn teammate_garbage_is_a_usage_error_never_a_guess() {
        for s in [
            "!teammate enable",
            "!teammate enable bogus",
            "!teammate set producer x",
            "!teammate nope",
            "!teammate set producer a b c",
        ] {
            match parse(s) {
                Some(Parsed::Teammate(Err(u))) => assert_eq!(u, TEAMMATE_USAGE),
                other => panic!("{s}: {other:?}"),
            }
        }
    }

    #[test]
    fn producer_verbs() {
        let p = |s: &str| match parse(s) {
            Some(Parsed::Producer(r)) => r,
            other => panic!("{other:?}"),
        };
        assert_eq!(p("!producer pause"), Ok(ProducerCmd::Pause));
        assert_eq!(p("!producer Resume"), Ok(ProducerCmd::Resume));
        assert_eq!(p("!producer"), Ok(ProducerCmd::Status));
        assert!(p("!producer fire").is_err());
    }
}
