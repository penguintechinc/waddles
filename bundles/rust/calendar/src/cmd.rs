//! Chat command grammar for `!calendar` / `!event` (alias) and `!rsvp`. The shared verb
//! vocabulary (add/list/set/remove/enable/disable, bare = read) is applied here because the Rust
//! SDK has no verb parser yet; `create`/`delete`/`view`/`approve`/`reject`/`rsvp` are accepted
//! as the legacy `!event` aliases and calendar-specific verbs.

use serde::{Deserialize, Serialize};

use crate::model::{Rsvp, Target};
use crate::timeutil::has_control;

pub const HEADS: [&str; 3] = ["!calendar", "!event", "!rsvp"];
pub const USAGE: &str = "Usage: !calendar [list] | view <id> | add <title> @ <when> [tz=Zone] [end=<when>] [loc=\"Place\"] | set <id> <title|description|start|end|tz|location> <value> | remove <id> | approve <id> | reject <id> [reason] | enable|disable sync <discord|twitch|wix> | !rsvp <id> [yes|no|maybe]";

/// A parsed command (serialized across the process -> action stage boundary).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case")]
pub enum Command {
    List,
    View {
        id: u64,
    },
    Add {
        title: String,
        when: String,
        tz: Option<String>,
        end: Option<String>,
        location: Option<String>,
    },
    Set {
        id: u64,
        field: String,
        value: String,
    },
    Remove {
        id: u64,
    },
    Approve {
        id: u64,
    },
    Reject {
        id: u64,
        reason: Option<String>,
    },
    Sync {
        target: Target,
        on: bool,
    },
    Rsvp {
        id: u64,
        answer: Option<Rsvp>,
    },
    Usage,
}

fn parse_id(tok: &str) -> Option<u64> {
    let tok = tok.trim().trim_start_matches('#');
    if tok.is_empty() || tok.len() > 9 || !tok.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    tok.parse::<u64>().ok().filter(|n| *n >= 1)
}

/// Splits on whitespace honouring double-quoted spans (quotes removed).
fn tokens(s: &str) -> Option<Vec<String>> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut in_q = false;
    let mut had = false;
    for c in s.chars() {
        match c {
            '"' => {
                in_q = !in_q;
                had = true;
            }
            c if c.is_whitespace() && !in_q => {
                if had || !cur.is_empty() {
                    out.push(std::mem::take(&mut cur));
                    had = false;
                }
            }
            c => cur.push(c),
        }
    }
    if in_q {
        return None;
    }
    if had || !cur.is_empty() {
        out.push(cur);
    }
    Some(out)
}

fn parse_add(rest: &str) -> Command {
    let Some((title_part, tail)) = rest.split_once(" @ ") else {
        return Command::Usage;
    };
    let title = title_part.trim().trim_matches('"').trim().to_string();
    let Some(toks) = tokens(tail) else {
        return Command::Usage;
    };
    let (mut tz, mut end, mut location) = (None, None, None);
    let mut when_parts: Vec<String> = Vec::new();
    for t in toks {
        if let Some(v) = t.strip_prefix("tz=") {
            tz = Some(v.to_string());
        } else if let Some(v) = t.strip_prefix("end=") {
            end = Some(v.to_string());
        } else if let Some(v) = t
            .strip_prefix("loc=")
            .or_else(|| t.strip_prefix("location="))
        {
            location = Some(v.to_string());
        } else {
            when_parts.push(t);
        }
    }
    let when = when_parts.join(" ");
    if title.is_empty() || when.is_empty() {
        return Command::Usage;
    }
    Command::Add {
        title,
        when,
        tz,
        end,
        location,
    }
}

/// Parses a `head`-prefixed message (`head` already lower-cased, `rest` the remainder).
pub fn parse(head: &str, rest: &str) -> Command {
    let rest = rest.trim();
    if has_control(rest) {
        return Command::Usage;
    }
    if head == "!rsvp" {
        let mut it = rest.split_whitespace();
        let Some(id) = it.next().and_then(parse_id) else {
            return Command::Usage;
        };
        return match it.next() {
            None => Command::Rsvp { id, answer: None },
            Some(a) => match (Rsvp::parse(a), it.next()) {
                (Some(answer), None) => Command::Rsvp {
                    id,
                    answer: Some(answer),
                },
                _ => Command::Usage,
            },
        };
    }
    let (verb, tail) = rest.split_once(' ').unwrap_or((rest, ""));
    let (verb, tail) = (verb.to_ascii_lowercase(), tail.trim());
    match verb.as_str() {
        "" | "list" if tail.is_empty() => Command::List,
        "view" | "show" => parse_id(tail).map_or(Command::Usage, |id| Command::View { id }),
        "add" | "create" => parse_add(tail),
        "set" => {
            let mut it = tail.splitn(3, ' ');
            let (Some(id), Some(field), Some(value)) =
                (it.next().and_then(parse_id), it.next(), it.next())
            else {
                return Command::Usage;
            };
            let value = value.trim().trim_matches('"').to_string();
            if value.is_empty() {
                return Command::Usage;
            }
            Command::Set {
                id,
                field: field.to_ascii_lowercase(),
                value,
            }
        }
        "remove" | "delete" | "cancel" => {
            parse_id(tail).map_or(Command::Usage, |id| Command::Remove { id })
        }
        "approve" => parse_id(tail).map_or(Command::Usage, |id| Command::Approve { id }),
        "reject" => {
            let (id_tok, reason) = tail.split_once(' ').unwrap_or((tail, ""));
            let reason = reason.trim();
            match parse_id(id_tok) {
                Some(id) => Command::Reject {
                    id,
                    reason: (!reason.is_empty()).then(|| reason.to_string()),
                },
                None => Command::Usage,
            }
        }
        "rsvp" => {
            let mut full = String::from(tail);
            full.insert(0, ' ');
            parse("!rsvp", &full)
        }
        "enable" | "disable" => {
            let mut it = tail.split_whitespace();
            match (
                it.next(),
                it.next()
                    .and_then(|t| Target::parse(&t.to_ascii_lowercase())),
                it.next(),
            ) {
                (Some("sync"), Some(target), None) => Command::Sync {
                    target,
                    on: verb == "enable",
                },
                _ => Command::Usage,
            }
        }
        _ => Command::Usage,
    }
}

/// Recognizes a chat line: `Some((head, rest))` when it starts with one of [`HEADS`].
pub fn split_head(text: &str) -> Option<(String, String)> {
    let t = text.trim();
    let (head, rest) = t.split_once(' ').unwrap_or((t, ""));
    let head = head.to_ascii_lowercase();
    HEADS
        .contains(&head.as_str())
        .then(|| (head, rest.to_string()))
}
