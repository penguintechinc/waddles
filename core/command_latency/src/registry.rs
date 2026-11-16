//! Bounded label vocabularies for the command-latency metrics.
//!
//! The `command` label must come from the REGISTERED command set, the `bundle` label from the
//! registered app bundles and the `bundle_lang` label from a closed language set, so none of them can
//! mint unbounded series or leak a message. The process-wide registry is fed ONLY by a cached read of
//! the database read replica (see [`crate::labels`]): seeded once at start, refreshed periodically and
//! re-read (rate limited) when a registry miss is observed. There is no environment baseline.
//!
//! Anything not registered folds to [`NO_COMMAND`] / [`NO_BUNDLE`] (`other`), an app whose language is
//! not mapped to [`UNKNOWN_LANG`], and each set is capped ([`MAX_COMMANDS`], [`MAX_BUNDLES`]).

use std::collections::{HashMap, HashSet};
use std::sync::{OnceLock, RwLock};

use tokio::sync::Notify;

use crate::stamp::is_valid_command;

/// Hard cap on registered command names (a registry is bounded by construction).
pub const MAX_COMMANDS: usize = 256;
/// Hard cap on registered app bundles (`app_id -> language` entries); bounds the `bundle` label.
pub const MAX_BUNDLES: usize = 256;

/// The closed `bundle_lang` vocabulary; anything else folds to `other`, an unmapped app to `unknown`.
pub const KNOWN_LANGS: [&str; 6] = ["rust", "python", "csharp", "go", "js", "other"];

/// Commands that mean "no registered command": the label for a non-command or unregistered message.
pub const NO_COMMAND: &str = "other";
/// The `bundle` label for an app that is not a registered bundle.
pub const NO_BUNDLE: &str = "other";
/// The label for a bundle whose language is not mapped.
pub const UNKNOWN_LANG: &str = "unknown";

/// A command-name and bundle-language registry. The process-wide instance is [`global`]; tests build
/// their own.
#[derive(Default)]
pub struct CommandRegistry {
    commands: RwLock<HashSet<String>>,
    langs: RwLock<HashMap<String, &'static str>>,
    /// Woken (never blocks) when a command-shaped or bundle-shaped value is not registered, so the
    /// refresher can lazy-load. Lossy by design: many misses coalesce into one pending wake.
    miss: Notify,
}

fn fold_lang(raw: &str) -> &'static str {
    let lower = raw.trim().to_ascii_lowercase();
    KNOWN_LANGS
        .iter()
        .find(|l| **l == lower)
        .copied()
        .unwrap_or("other")
}

impl CommandRegistry {
    /// An empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Registers a command name; `false` (and no change) when it is not a valid label or the cap is
    /// reached. Names are stored lowercase.
    pub fn register(&self, name: &str) -> bool {
        let name = name.trim().to_ascii_lowercase();
        if !is_valid_command(&name) {
            return false;
        }
        let mut set = self.commands.write().unwrap_or_else(|e| e.into_inner());
        if set.len() >= MAX_COMMANDS && !set.contains(&name) {
            return false;
        }
        set.insert(name);
        true
    }

    /// `true` when `name` is a registered command.
    pub fn contains(&self, name: &str) -> bool {
        self.commands
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .contains(name)
    }

    /// Number of registered commands.
    pub fn command_count(&self) -> usize {
        self.commands
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .len()
    }

    /// Finds the REGISTERED command a chat message invokes: the first whitespace token, with one
    /// leading `!` or `/` stripped, lowercased -- returned only when it is in the registry. The text
    /// is read in place and never stored, logged or returned; an unregistered token yields `None`.
    pub fn detect(&self, text: &str) -> Option<String> {
        let token = text.split_whitespace().next()?;
        let stripped = token.strip_prefix('!').or_else(|| token.strip_prefix('/'));
        let prefixed = stripped.is_some();
        let token = stripped.unwrap_or(token);
        if token.len() > 32 {
            return None;
        }
        let lower = token.to_ascii_lowercase();
        if self.contains(&lower) {
            return Some(lower);
        }
        // A prefixed, label-shaped, unregistered token may be a command registered after the last
        // refresh: ask the refresher to re-read (it rate limits). Plain chat never triggers this.
        if prefixed && is_valid_command(&lower) {
            self.miss.notify_one();
        }
        None
    }

    /// The wake signal raised by a registry miss; awaited by [`crate::labels::spawn_refresher`].
    pub fn miss_signal(&self) -> &Notify {
        &self.miss
    }

    /// Atomically replaces the command set with `names` (invalid names skipped, capped at
    /// [`MAX_COMMANDS`]); a command deleted in the database drops out on the next refresh.
    pub fn replace_commands<I, S>(&self, names: I)
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut next = HashSet::new();
        for n in names {
            let n = n.as_ref().trim().to_ascii_lowercase();
            if next.len() >= MAX_COMMANDS {
                break;
            }
            if is_valid_command(&n) {
                next.insert(n);
            }
        }
        *self.commands.write().unwrap_or_else(|e| e.into_inner()) = next;
    }

    /// Atomically replaces the `app_id -> language` map (capped at [`MAX_BUNDLES`]; languages folded
    /// into the closed set; app ids that are not label-safe skipped).
    pub fn replace_bundles<I, A, L>(&self, bundles: I)
    where
        I: IntoIterator<Item = (A, L)>,
        A: AsRef<str>,
        L: AsRef<str>,
    {
        let mut next = HashMap::new();
        for (app, lang) in bundles {
            if next.len() >= MAX_BUNDLES {
                break;
            }
            let app = app.as_ref().trim();
            if is_valid_bundle(app) {
                next.insert(app.to_string(), fold_lang(lang.as_ref()));
            }
        }
        *self.langs.write().unwrap_or_else(|e| e.into_inner()) = next;
    }

    /// Number of registered bundles.
    pub fn bundle_count(&self) -> usize {
        self.langs.read().unwrap_or_else(|e| e.into_inner()).len()
    }

    /// The bounded `bundle` label: the `app_id` when it is a registered bundle, else
    /// [`NO_BUNDLE`]. A miss on a label-shaped id wakes the refresher.
    pub fn bundle_label(&self, app_id: &str) -> String {
        if self
            .langs
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .contains_key(app_id)
        {
            return app_id.to_string();
        }
        if is_valid_bundle(app_id) {
            self.miss.notify_one();
        }
        NO_BUNDLE.to_string()
    }

    /// Maps a bundle `app_id` to a language in the closed set (anything else becomes `other`);
    /// `false` (no change) when the id is not label-safe or the cap is reached.
    pub fn set_bundle_lang(&self, app_id: &str, lang: &str) -> bool {
        if !is_valid_bundle(app_id) {
            return false;
        }
        let mut map = self.langs.write().unwrap_or_else(|e| e.into_inner());
        if map.len() >= MAX_BUNDLES && !map.contains_key(app_id) {
            return false;
        }
        map.insert(app_id.to_string(), fold_lang(lang));
        true
    }

    /// The bounded language label for a bundle (`unknown` when unmapped).
    pub fn bundle_lang(&self, app_id: &str) -> &'static str {
        self.langs
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(app_id)
            .copied()
            .unwrap_or(UNKNOWN_LANG)
    }
}

/// The process-wide registry; empty until [`crate::labels::spawn_refresher`] seeds it from the
/// database (an empty registry labels everything `other` / `unknown`, never an error).
pub fn global() -> &'static CommandRegistry {
    static GLOBAL: OnceLock<CommandRegistry> = OnceLock::new();
    GLOBAL.get_or_init(CommandRegistry::new)
}

/// `true` for a label-safe bundle `app_id`: 1..=128 of `[A-Za-z0-9._-]`.
pub fn is_valid_bundle(app_id: &str) -> bool {
    !app_id.is_empty()
        && app_id.len() <= 128
        && app_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'_' || b == b'-')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detect_returns_only_registered_commands_never_user_text() {
        let reg = CommandRegistry::new();
        assert!(reg.register("Ping"));
        assert_eq!(reg.detect("!ping"), Some("ping".into()));
        assert_eq!(reg.detect("/PING now please"), Some("ping".into()));
        assert_eq!(reg.detect("ping"), Some("ping".into()));
        assert_eq!(
            reg.detect("!pong"),
            None,
            "unregistered token is not a label"
        );
        assert_eq!(reg.detect("hello world"), None);
        assert_eq!(reg.detect(""), None);
        assert_eq!(reg.detect("   "), None);
        assert_eq!(reg.detect(&format!("!{}", "a".repeat(40))), None);
    }

    #[test]
    fn register_rejects_invalid_names_and_caps_the_set() {
        let reg = CommandRegistry::new();
        assert!(!reg.register("has space"));
        assert!(!reg.register("semi;colon"));
        assert!(!reg.register(""));
        for i in 0..MAX_COMMANDS {
            assert!(reg.register(&format!("c{i}")));
        }
        assert!(!reg.register("one-too-many"));
        assert!(
            reg.register("c0"),
            "re-registering an existing name is fine"
        );
        assert_eq!(reg.command_count(), MAX_COMMANDS);
    }

    #[test]
    fn bundle_langs_fold_into_the_closed_set() {
        let reg = CommandRegistry::new();
        reg.set_bundle_lang("a", "Rust");
        reg.set_bundle_lang("b", "cobol");
        assert_eq!(reg.bundle_lang("a"), "rust");
        assert_eq!(reg.bundle_lang("b"), "other");
        assert_eq!(reg.bundle_lang("never-seen"), UNKNOWN_LANG);
    }

    #[test]
    fn replace_swaps_atomically_drops_stale_and_caps() {
        let reg = CommandRegistry::new();
        reg.replace_commands(["Ping", "roll", "bad name", ""]);
        assert!(reg.contains("ping") && reg.contains("roll") && !reg.contains("bad name"));
        reg.replace_commands(["roll"]);
        assert!(!reg.contains("ping"), "a deleted command drops out");
        reg.replace_commands((0..MAX_COMMANDS + 50).map(|i| format!("c{i}")));
        assert_eq!(reg.command_count(), MAX_COMMANDS);

        reg.replace_bundles([("waddles.core.ping", "Rust"), ("has space", "rust")]);
        assert_eq!(reg.bundle_count(), 1);
        assert_eq!(reg.bundle_label("waddles.core.ping"), "waddles.core.ping");
        assert_eq!(reg.bundle_label("never-seen"), NO_BUNDLE);
        reg.replace_bundles((0..MAX_BUNDLES + 10).map(|i| (format!("app{i}"), "python")));
        assert_eq!(reg.bundle_count(), MAX_BUNDLES);
    }

    #[tokio::test]
    async fn a_prefixed_unregistered_token_wakes_the_refresher_but_plain_chat_does_not() {
        let reg = CommandRegistry::new();
        assert_eq!(reg.detect("hello there"), None);
        let none = tokio::time::timeout(
            std::time::Duration::from_millis(30),
            reg.miss_signal().notified(),
        )
        .await;
        assert!(none.is_err(), "plain chat must not trigger a refresh");
        assert_eq!(reg.detect("!newcmd arg"), None);
        tokio::time::timeout(
            std::time::Duration::from_millis(200),
            reg.miss_signal().notified(),
        )
        .await
        .expect("a prefixed miss wakes the refresher");
        assert_eq!(reg.bundle_label("waddles.new.app"), NO_BUNDLE);
        tokio::time::timeout(
            std::time::Duration::from_millis(200),
            reg.miss_signal().notified(),
        )
        .await
        .expect("a bundle miss wakes the refresher");
    }
}
