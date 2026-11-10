//! Shared `!<command>` grammar parser -- the Rust port of
//! `sdk/waddle-sdk/src/waddle_sdk/command.py`.
//!
//! Same grammar and semantics as the Python SDK, verified by the shared
//! golden-vector fixture `sdk/testdata/command-vectors.json` (read by both
//! `sdk/waddle-sdk/tests/test_command_vectors.py` and
//! `sdk/waddle-sdk-rs/tests/command_vectors.rs`). Pure text in, structured data
//! out: no host imports, no panics, wasm32-wasip2 safe (no `SystemTime`).
//!
//! Three layers:
//! * [`parse_command`] -- `!cmd [sub-module] [verb] <args>` with the fixed
//!   [`VERBS`] vocabulary (`set/add/sub/enable/disable/remove/delete/list/reset`;
//!   a bare command is a read).
//! * [`parse_cli_command`] -- shlex-quoted `--opt value` / `--flag` / positionals
//!   against a declarative [`CliCommandSpec`] (argparse-compatible subset).
//! * [`substitute_placeholders`] -- `$(name)` template expansion.
//!
//! Rust-only helpers (additive, no Python counterpart yet): [`parse_duration`]
//! (`10m`, `2h`, `1h30m`) and [`parse_mention`] (`<@123>`, `@handle`).
//!
//! Every input is capped at [`MAX_COMMAND_CHARS`] (the Python parser enforces the
//! same cap); errors are typed ([`CommandError`]), never panics.

use std::collections::BTreeMap;
use std::time::Duration;

use thiserror::Error;

/// Maximum accepted command text length in characters (identical in Python).
pub const MAX_COMMAND_CHARS: usize = 4096;

/// Maximum accepted length of a single duration or mention token.
pub const MAX_TOKEN_CHARS: usize = 64;

/// The fixed verb vocabulary every bundle command grammar shares.
pub const VERBS: [&str; 9] = [
    "set", "add", "sub", "enable", "disable", "remove", "delete", "list", "reset",
];

/// Failure parsing a command: usage errors are user-facing replies, schema
/// errors are bundle bugs (a malformed [`CliCommandSpec`]).
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum CommandError {
    /// Input did not match the declared grammar; the message is a ready usage reply.
    #[error("{0}")]
    Usage(String),
    /// The declared spec itself is invalid (duplicate subcommand, bad flag, ...).
    #[error("invalid command schema: {0}")]
    Schema(String),
}

fn usage<T>(msg: impl Into<String>) -> Result<T, CommandError> {
    Err(CommandError::Usage(msg.into()))
}

/// A `$(name)` placeholder had no supplied value (a bundle bug; fail loud).
#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("missing placeholder value for {0:?}")]
pub struct MissingPlaceholderError(pub String);

/// A bundle's command name (no leading `!`) and closed set of sub-modules.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandSpec {
    /// Command name without the leading `!`.
    pub name: String,
    /// Recognized sub-module names (lowercase); empty for none.
    pub sub_modules: Vec<String>,
}

impl CommandSpec {
    /// Build a spec; sub-module names are matched case-insensitively (stored lowercase).
    pub fn new(name: &str, sub_modules: &[&str]) -> Self {
        Self {
            name: name.to_string(),
            sub_modules: sub_modules.iter().map(|s| s.to_lowercase()).collect(),
        }
    }
}

/// Structured result of [`parse_command`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedCommand {
    /// Command name (from the spec).
    pub command: String,
    /// Sub-module, when present.
    pub sub_module: Option<String>,
    /// Verb (lowercase); `None` for a bare read.
    pub option: Option<String>,
    /// Free-text remainder, argument case preserved.
    pub args: Option<String>,
}

/// Python `str.isspace` equivalent (Rust's `White_Space` plus U+001C..U+001F).
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

fn py_strip(s: &str) -> &str {
    s.trim_matches(py_space)
}

/// Python `str.partition(" ")`: split on the first single ASCII space.
fn partition_space(s: &str) -> (&str, &str) {
    match s.split_once(' ') {
        Some((a, b)) => (a, b),
        None => (s, ""),
    }
}

fn non_empty_stripped(s: &str) -> Option<String> {
    let t = py_strip(s);
    if t.is_empty() {
        None
    } else {
        Some(t.to_string())
    }
}

/// Python `repr(str)` for error-message parity.
fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn check_len(text: &str) -> Result<(), CommandError> {
    if text.chars().count() > MAX_COMMAND_CHARS {
        return usage(format!(
            "command text exceeds {MAX_COMMAND_CHARS} characters"
        ));
    }
    Ok(())
}

fn is_verb(s: &str) -> bool {
    VERBS.contains(&s)
}

/// Parse one chat message against `spec`'s grammar (port of Python `parse_command`).
///
/// Recognizes `!cmd`, `!cmd <verb> <args>`, `!cmd <sub-module> <verb> <args>` and
/// the toggle shape `!cmd enable|disable <sub-module>` (or `!cmd <sub-module>
/// enable|disable`). Never returns a best-guess partial parse.
pub fn parse_command(text: &str, spec: &CommandSpec) -> Result<ParsedCommand, CommandError> {
    check_len(text)?;
    let stripped = py_strip(text);
    if stripped.is_empty() {
        return usage("empty command text");
    }
    let (head, rest) = partition_space(stripped);
    if head.to_lowercase() != format!("!{}", spec.name).to_lowercase() {
        return usage(format!(
            "unrecognized command {}, expected !{}",
            py_repr(head),
            spec.name
        ));
    }
    let rest = py_strip(rest);
    let mk =
        |sub_module: Option<String>, option: Option<String>, args: Option<String>| ParsedCommand {
            command: spec.name.clone(),
            sub_module,
            option,
            args,
        };
    if rest.is_empty() {
        return Ok(mk(None, None, None));
    }
    let (tok1, tail) = partition_space(rest);
    let tail_text = non_empty_stripped(tail);
    let tok1_lower = tok1.to_lowercase();

    if is_verb(&tok1_lower) {
        return finish_option(spec, None, tok1_lower, tail_text);
    }
    if spec.sub_modules.contains(&tok1_lower) {
        let Some(tail_text) = tail_text else {
            return Ok(mk(Some(tok1_lower), None, None));
        };
        let (tok2, tail2) = partition_space(&tail_text);
        let tail2_text = non_empty_stripped(tail2);
        let tok2_lower = tok2.to_lowercase();
        if !is_verb(&tok2_lower) {
            return usage(format!(
                "!{} {}: unknown option {}",
                spec.name,
                tok1_lower,
                py_repr(tok2)
            ));
        }
        return finish_option(spec, Some(tok1_lower), tok2_lower, tail2_text);
    }
    usage(format!(
        "!{}: unknown option or sub-module {}",
        spec.name,
        py_repr(tok1)
    ))
}

fn finish_option(
    spec: &CommandSpec,
    sub_module: Option<String>,
    option: String,
    tail: Option<String>,
) -> Result<ParsedCommand, CommandError> {
    if option == "enable" || option == "disable" {
        if let (Some(sm), Some(_)) = (&sub_module, &tail) {
            return usage(format!(
                "!{} {} {}: takes no further arguments",
                spec.name, sm, option
            ));
        }
        let Some(target) = sub_module.or(tail) else {
            return usage(format!(
                "!{} {} requires a sub-module name",
                spec.name, option
            ));
        };
        if target.contains(' ') {
            return usage(format!(
                "!{} {}: takes exactly one sub-module name",
                spec.name, option
            ));
        }
        let target_lower = target.to_lowercase();
        if !spec.sub_modules.is_empty() && !spec.sub_modules.contains(&target_lower) {
            return usage(format!(
                "!{} {}: unknown sub-module {}",
                spec.name,
                option,
                py_repr(&target)
            ));
        }
        return Ok(ParsedCommand {
            command: spec.name.clone(),
            sub_module: Some(target_lower),
            option: Some(option),
            args: None,
        });
    }
    Ok(ParsedCommand {
        command: spec.name.clone(),
        sub_module,
        option: Some(option),
        args: tail,
    })
}

/// Python `\w` approximation: Unicode alphanumeric or underscore.
fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Find `$(name)` placeholders as `(start, end, name)` byte spans, left to right.
fn scan_placeholders(template: &str) -> Vec<(usize, usize, &str)> {
    let mut found = Vec::new();
    let bytes = template.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'$' && bytes.get(i + 1) == Some(&b'(') {
            let name_start = i + 2;
            let mut end = name_start;
            for (off, c) in template[name_start..].char_indices() {
                if is_word(c) {
                    end = name_start + off + c.len_utf8();
                } else {
                    break;
                }
            }
            if end > name_start && bytes.get(end) == Some(&b')') {
                found.push((i, end + 1, &template[name_start..end]));
                i = end + 1;
                continue;
            }
        }
        i += 1;
    }
    found
}

/// Every `$(name)` in `template`, first-seen order, deduplicated.
pub fn extract_placeholders(template: &str) -> Vec<String> {
    let mut seen: Vec<String> = Vec::new();
    for (_, _, name) in scan_placeholders(template) {
        if !seen.iter().any(|s| s == name) {
            seen.push(name.to_string());
        }
    }
    seen
}

/// Replace every `$(name)` with `values[name]`; a missing value fails loud.
pub fn substitute_placeholders(
    template: &str,
    values: &BTreeMap<String, String>,
) -> Result<String, MissingPlaceholderError> {
    let mut out = String::with_capacity(template.len());
    let mut last = 0;
    for (start, end, name) in scan_placeholders(template) {
        let Some(v) = values.get(name) else {
            return Err(MissingPlaceholderError(name.to_string()));
        };
        out.push_str(&template[last..start]);
        out.push_str(v);
        last = end;
    }
    out.push_str(&template[last..]);
    Ok(out)
}

/// Value type of a declared CLI argument.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CliKind {
    /// Free string.
    String,
    /// Canonical bounded decimal integer (`-?[0-9]{1,20}`).
    Integer,
}

/// A typed CLI value (`Absent` mirrors Python `None`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CliValue {
    /// String value.
    Str(String),
    /// Integer value (i128 holds any 20-digit decimal).
    Int(i128),
    /// Optional argument not supplied and without a default.
    Absent,
}

/// One declarative CLI argument: positional (no flags) or flag(s) taking a value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliArgument {
    /// `[a-z][a-z0-9_]*` result key.
    pub name: String,
    /// `-x` / `--long` spellings; empty = positional.
    pub flags: Vec<String>,
    /// Whether omission is an error.
    pub required: bool,
    /// Value type.
    pub kind: CliKind,
    /// Value used when omitted.
    pub default: CliValue,
    /// Help text.
    pub help: String,
}

impl CliArgument {
    /// A required string positional.
    pub fn positional(name: &str) -> Self {
        Self {
            name: name.into(),
            flags: vec![],
            required: true,
            kind: CliKind::String,
            default: CliValue::Absent,
            help: String::new(),
        }
    }

    /// A required string flag with the given spellings.
    pub fn flag(name: &str, flags: &[&str]) -> Self {
        Self {
            flags: flags.iter().map(|s| s.to_string()).collect(),
            ..Self::positional(name)
        }
    }
}

/// A command's argument schema, optional subcommands and default subcommand.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CliCommandSpec {
    /// Command name without `!`.
    pub name: String,
    /// Arguments of this (sub)command.
    pub arguments: Vec<CliArgument>,
    /// Subcommands (first token selects one).
    pub subcommands: Vec<CliCommandSpec>,
    /// Subcommand used when the first token names none.
    pub default_subcommand: Option<String>,
    /// Description shown in help.
    pub description: String,
}

/// Result of [`parse_cli_command`]; a help request returns `help` and no values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedCliCommand {
    /// Command name.
    pub command: String,
    /// Selected subcommand.
    pub subcommand: Option<String>,
    /// Argument name to typed value.
    pub values: BTreeMap<String, CliValue>,
    /// Generated help text when `--help` was requested.
    pub help: Option<String>,
}

/// POSIX `shlex.split` (comments off): `'..'` literal, `".."` with `\"`/`\\`
/// escapes, bare `\x` escapes `x`; unclosed quote or trailing backslash errors.
pub fn shlex_split(text: &str) -> Result<Vec<String>, CommandError> {
    const ERR: &str = "unclosed command quote";
    let mut tokens = Vec::new();
    let mut cur = String::new();
    let mut started = false;
    let mut it = text.chars();
    while let Some(c) = it.next() {
        match c {
            ' ' | '\t' | '\r' | '\n' => {
                if started {
                    tokens.push(std::mem::take(&mut cur));
                    started = false;
                }
            }
            '\\' => {
                let Some(n) = it.next() else {
                    return usage(ERR);
                };
                cur.push(n);
                started = true;
            }
            '\'' => {
                started = true;
                loop {
                    match it.next() {
                        Some('\'') => break,
                        Some(ch) => cur.push(ch),
                        None => return usage(ERR),
                    }
                }
            }
            '"' => {
                started = true;
                loop {
                    match it.next() {
                        Some('"') => break,
                        Some('\\') => match it.next() {
                            Some(n @ ('"' | '\\')) => cur.push(n),
                            Some(n) => {
                                cur.push('\\');
                                cur.push(n);
                            }
                            None => return usage(ERR),
                        },
                        Some(ch) => cur.push(ch),
                        None => return usage(ERR),
                    }
                }
            }
            ch => {
                cur.push(ch);
                started = true;
            }
        }
    }
    if started {
        tokens.push(cur);
    }
    Ok(tokens)
}

fn schema<T>(msg: &str) -> Result<T, CommandError> {
    Err(CommandError::Schema(msg.to_string()))
}

fn valid_arg_name(n: &str) -> bool {
    let mut cs = n.chars();
    matches!(cs.next(), Some('a'..='z'))
        && cs.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

fn valid_flag(f: &str) -> bool {
    let b = f.as_bytes();
    if b.len() == 2 && b[0] == b'-' && b[1].is_ascii_alphabetic() {
        return true;
    }
    f.strip_prefix("--").is_some_and(|r| {
        let mut cs = r.chars();
        matches!(cs.next(), Some('a'..='z'))
            && cs.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
    })
}

fn parse_int(raw: &str) -> Result<i128, CommandError> {
    let digits = raw.strip_prefix('-').unwrap_or(raw);
    if raw.len() > 20 || digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return usage("expected a decimal integer");
    }
    raw.parse::<i128>()
        .or_else(|_| usage("expected a decimal integer"))
}

/// Argparse's `_negative_number_matcher`: `^-\d+$|^-\d*\.\d+$`.
fn looks_negative_number(s: &str) -> bool {
    let Some(r) = s.strip_prefix('-') else {
        return false;
    };
    if !r.is_empty() && r.bytes().all(|b| b.is_ascii_digit()) {
        return true;
    }
    match r.split_once('.') {
        Some((a, b)) => {
            a.bytes().all(|x| x.is_ascii_digit())
                && !b.is_empty()
                && b.bytes().all(|x| x.is_ascii_digit())
        }
        None => false,
    }
}

/// One classified argv token.
enum Tok<'a> {
    Positional(&'a str),
    /// Matched option string (index into `opts`) plus explicit `=value` / `-xVALUE`.
    Opt(usize, Option<String>),
    Unknown,
}

const HELP_FLAG: &str = "--help";

fn classify<'a>(
    arg: &'a str,
    opts: &[(String, Option<usize>)],
    has_neg_opt: bool,
) -> Result<Tok<'a>, CommandError> {
    if arg.is_empty() || !arg.starts_with('-') {
        return Ok(Tok::Positional(arg));
    }
    if let Some(i) = opts.iter().position(|(s, _)| s == arg) {
        return Ok(Tok::Opt(i, None));
    }
    if arg.len() == 1 {
        return Ok(Tok::Positional(arg));
    }
    if let Some((os, ex)) = arg.split_once('=')
        && let Some(i) = opts.iter().position(|(s, _)| s == os)
    {
        return Ok(Tok::Opt(i, Some(ex.to_string())));
    }
    let mut matches: Vec<(usize, Option<String>)> = Vec::new();
    if arg.starts_with("--") {
        let (prefix, ex) = match arg.split_once('=') {
            Some((p, e)) => (p, Some(e.to_string())),
            None => (arg, None),
        };
        for (i, (s, _)) in opts.iter().enumerate() {
            if s.starts_with(prefix) {
                matches.push((i, ex.clone()));
            }
        }
    } else {
        let short_prefix: String = arg.chars().take(2).collect();
        let short_explicit: String = arg.chars().skip(2).collect();
        for (i, (s, _)) in opts.iter().enumerate() {
            if *s == short_prefix {
                matches.push((i, Some(short_explicit.clone())));
            } else if s.starts_with(arg) {
                matches.push((i, None));
            }
        }
    }
    if matches.len() > 1 {
        return usage(format!("ambiguous option: {arg}"));
    }
    if let Some((i, ex)) = matches.pop() {
        return Ok(Tok::Opt(i, ex));
    }
    if looks_negative_number(arg) && !has_neg_opt {
        return Ok(Tok::Positional(arg));
    }
    if arg.contains(' ') {
        return Ok(Tok::Positional(arg));
    }
    Ok(Tok::Unknown)
}

fn render_help(prog: &str, desc: &str, args: &[CliArgument], subs: &[&str]) -> String {
    let mut s = format!("usage: {prog} [--help]");
    for a in args {
        if let Some(f) = a.flags.first() {
            if a.required {
                s.push_str(&format!(" {f} {}", a.name.to_uppercase()));
            } else {
                s.push_str(&format!(" [{f} {}]", a.name.to_uppercase()));
            }
        } else if a.required {
            s.push_str(&format!(" {}", a.name));
        } else {
            s.push_str(&format!(" [{}]", a.name));
        }
    }
    if !desc.is_empty() {
        s.push_str(&format!("\n\n{desc}"));
    }
    s.push_str("\n\narguments:\n  --help  show command help");
    for a in args {
        let label = if a.flags.is_empty() {
            a.name.clone()
        } else {
            a.flags.join(", ")
        };
        s.push_str(&format!("\n  {label}  {}", a.help));
    }
    if !subs.is_empty() {
        s.push_str(&format!("\n\nSubcommands: {}", subs.join(", ")));
    }
    s.trim().to_string()
}

/// Parse Unix-style flags and quoted positionals against `spec` (port of Python
/// `parse_cli_command`; argparse-compatible subset: exact + unique-prefix long
/// flags, `--flag=value`, `-xVALUE`, `--` terminator, negative-number values).
///
/// Usage errors carry the reason but not argparse's byte-identical wording, and
/// generated help text is Rust-formatted -- vectors assert error-ness and
/// help presence, not text.
pub fn parse_cli_command(
    text: &str,
    spec: &CliCommandSpec,
) -> Result<ParsedCliCommand, CommandError> {
    check_len(text)?;
    let tokens = shlex_split(text)?;
    match tokens.first() {
        Some(t) if t.to_lowercase() == format!("!{}", spec.name).to_lowercase() => {}
        _ => return usage("unrecognized command"),
    }
    let mut rest: Vec<String> = tokens[1..].to_vec();
    let mut selected = spec;
    let mut subcommand: Option<String> = None;
    if !spec.subcommands.is_empty() {
        for (i, c) in spec.subcommands.iter().enumerate() {
            if spec.subcommands[..i].iter().any(|o| o.name == c.name) {
                return schema("duplicate CLI subcommand in schema");
            }
        }
        let first = rest.first().map(|s| s.to_lowercase());
        let first_match = first
            .as_ref()
            .filter(|f| spec.subcommands.iter().any(|c| &c.name == *f))
            .cloned();
        subcommand = first_match
            .clone()
            .or_else(|| spec.default_subcommand.clone());
        if first_match.is_some() {
            rest.remove(0);
        }
        match &subcommand {
            Some(name) => match spec.subcommands.iter().find(|c| &c.name == name) {
                Some(c) => selected = c,
                None => return schema("unknown default CLI subcommand in schema"),
            },
            None => {
                if !rest.is_empty() && rest != ["--help"] {
                    return usage("unknown subcommand");
                }
            }
        }
    }
    let program = match &subcommand {
        Some(s) => format!("!{} {s}", spec.name),
        None => format!("!{}", spec.name),
    };
    let desc = if selected.description.is_empty() {
        &spec.description
    } else {
        &selected.description
    };

    let mut names: Vec<&str> = Vec::new();
    let mut opts: Vec<(String, Option<usize>)> = vec![(HELP_FLAG.to_string(), None)];
    for (ai, a) in selected.arguments.iter().enumerate() {
        if !valid_arg_name(&a.name) || names.contains(&a.name.as_str()) {
            return schema("invalid or duplicate CLI argument name");
        }
        names.push(&a.name);
        if a.flags.iter().any(|f| !valid_flag(f)) {
            return schema("invalid CLI argument flag");
        }
        for f in &a.flags {
            opts.push((f.clone(), Some(ai)));
        }
    }
    if rest.iter().any(|t| t == HELP_FLAG) {
        let subs: Vec<&str> = spec.subcommands.iter().map(|c| c.name.as_str()).collect();
        let help = render_help(&program, desc, &selected.arguments, &subs);
        return Ok(ParsedCliCommand {
            command: spec.name.clone(),
            subcommand,
            values: BTreeMap::new(),
            help: Some(help),
        });
    }

    let has_neg_opt = opts.iter().any(|(s, _)| looks_negative_number(s));
    let mut flag_vals: BTreeMap<usize, String> = BTreeMap::new();
    let mut positionals: Vec<String> = Vec::new();
    let mut i = 0;
    let mut only_positional = false;
    while i < rest.len() {
        let tok = &rest[i];
        i += 1;
        if only_positional {
            positionals.push(tok.clone());
            continue;
        }
        if tok == "--" {
            only_positional = true;
            continue;
        }
        match classify(tok, &opts, has_neg_opt)? {
            Tok::Positional(p) => positionals.push(p.to_string()),
            Tok::Unknown => return usage(format!("unrecognized arguments: {tok}")),
            Tok::Opt(oi, explicit) => {
                let (oname, Some(ai)) = (&opts[oi].0, opts[oi].1) else {
                    return usage(format!("argument {HELP_FLAG}: ignored explicit argument"));
                };
                let value = match explicit {
                    Some(v) => v,
                    None => {
                        let next = rest.get(i).filter(|n| {
                            *n != "--"
                                && matches!(classify(n, &opts, has_neg_opt), Ok(Tok::Positional(_)))
                        });
                        match next {
                            Some(n) => {
                                i += 1;
                                n.clone()
                            }
                            None => {
                                return usage(format!("argument {oname}: expected one argument"));
                            }
                        }
                    }
                };
                flag_vals.insert(ai, value);
            }
        }
    }

    let mut values: BTreeMap<String, CliValue> = BTreeMap::new();
    let mut pos_iter = positionals.into_iter();
    for (ai, a) in selected.arguments.iter().enumerate() {
        let raw = if a.flags.is_empty() {
            pos_iter.next()
        } else {
            flag_vals.remove(&ai)
        };
        let v = match raw {
            Some(r) => match a.kind {
                CliKind::Integer => CliValue::Int(parse_int(&r)?),
                CliKind::String => CliValue::Str(r),
            },
            None if a.required => {
                return usage(format!("the following arguments are required: {}", a.name));
            }
            None => a.default.clone(),
        };
        values.insert(a.name.clone(), v);
    }
    if let Some(extra) = pos_iter.next() {
        return usage(format!("unrecognized arguments: {extra}"));
    }
    Ok(ParsedCliCommand {
        command: spec.name.clone(),
        subcommand,
        values,
        help: None,
    })
}

/// Parse a duration like `45s`, `10m`, `2h`, `1d`, `1w` or a compound `1h30m`
/// (units `s m h d w`, each at most once, strictly descending). Rejects empty,
/// zero, overlong, repeated-unit and overflowing input; uses no clock.
pub fn parse_duration(raw: &str) -> Result<Duration, CommandError> {
    if raw.is_empty() || raw.chars().count() > MAX_TOKEN_CHARS {
        return usage("invalid duration length");
    }
    let bad = || CommandError::Usage(format!("invalid duration {}", py_repr(raw)));
    let range = || CommandError::Usage(format!("duration {} out of range", py_repr(raw)));
    let mut total: u64 = 0;
    let mut digits = String::new();
    let mut last_rank: Option<u8> = None;
    for c in raw.chars() {
        if c.is_ascii_digit() {
            digits.push(c);
            continue;
        }
        let (rank, secs) = match c.to_ascii_lowercase() {
            'w' => (5u8, 604_800u64),
            'd' => (4, 86_400),
            'h' => (3, 3_600),
            'm' => (2, 60),
            's' => (1, 1),
            _ => return Err(bad()),
        };
        if digits.is_empty() || last_rank.is_some_and(|l| l <= rank) {
            return Err(bad());
        }
        let n: u64 = digits.parse().map_err(|_| range())?;
        total = n
            .checked_mul(secs)
            .and_then(|v| total.checked_add(v))
            .ok_or_else(range)?;
        digits.clear();
        last_rank = Some(rank);
    }
    if !digits.is_empty() || last_rank.is_none() || total == 0 {
        return Err(bad());
    }
    Ok(Duration::from_secs(total))
}

/// A user reference token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Mention {
    /// Platform mention markup carrying an opaque numeric id (`<@123>`, `<@!123>`).
    Id(String),
    /// Plain `@handle` (case preserved; handles are PII -- never log them).
    Handle(String),
}

/// Parse `<@id>` / `<@!id>` (1-30 ASCII digits) or `@handle`
/// (`[A-Za-z0-9_.-]`, 1-[`MAX_TOKEN_CHARS`] chars).
pub fn parse_mention(raw: &str) -> Result<Mention, CommandError> {
    if raw.chars().count() > MAX_TOKEN_CHARS + 6 {
        return usage("mention too long");
    }
    if let Some(inner) = raw.strip_prefix("<@").and_then(|r| r.strip_suffix('>')) {
        let id = inner.strip_prefix('!').unwrap_or(inner);
        if !id.is_empty() && id.len() <= 30 && id.bytes().all(|b| b.is_ascii_digit()) {
            return Ok(Mention::Id(id.to_string()));
        }
        return usage("invalid mention id");
    }
    if let Some(h) = raw.strip_prefix('@')
        && !h.is_empty()
        && h.len() <= MAX_TOKEN_CHARS
        && h.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
    {
        return Ok(Mention::Handle(h.to_string()));
    }
    usage("invalid mention")
}

#[cfg(test)]
#[path = "command_tests.rs"]
mod tests;
