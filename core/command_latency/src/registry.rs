//! Bounded label vocabularies for the command-latency metrics.
//!
//! The `command` label must come from the REGISTERED command set and never from user text, and the
//! `bundle_lang` label from a closed language set, so neither can mint unbounded series or leak a
//! message. Both are process-wide registries fed by (a) an environment baseline and (b) the
//! [`CommandRegistry::register`] / [`CommandRegistry::set_bundle_lang`] API for a catalog feed.
//!
//! | Env var | Format |
//! |---|---|
//! | `WADDLES_COMMAND_LATENCY_COMMANDS` | `ping,roll,8ball` |
//! | `WADDLES_BUNDLE_LANGS` | `waddles.core.example.ping:rust,waddles.fun.roll:python` |

use std::collections::{HashMap, HashSet};
use std::sync::{OnceLock, RwLock};

use crate::stamp::is_valid_command;

/// Hard cap on registered command names (a registry is bounded by construction).
pub const MAX_COMMANDS: usize = 256;
/// Hard cap on `app_id -> language` entries.
pub const MAX_BUNDLES: usize = 1024;

/// The closed `bundle_lang` vocabulary; anything else folds to `other`, an unmapped app to `unknown`.
pub const KNOWN_LANGS: [&str; 6] = ["rust", "python", "csharp", "go", "js", "other"];

/// Commands that mean "no registered command": the label for a non-command or unregistered message.
pub const NO_COMMAND: &str = "other";
/// The label for a bundle whose language is not mapped.
pub const UNKNOWN_LANG: &str = "unknown";

/// A command-name and bundle-language registry. The process-wide instance is [`global`]; tests build
/// their own.
#[derive(Default)]
pub struct CommandRegistry {
    commands: RwLock<HashSet<String>>,
    langs: RwLock<HashMap<String, &'static str>>,
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
        let token = token
            .strip_prefix('!')
            .or_else(|| token.strip_prefix('/'))
            .unwrap_or(token);
        if token.len() > 32 {
            return None;
        }
        let lower = token.to_ascii_lowercase();
        self.contains(&lower).then_some(lower)
    }

    /// Maps a bundle `app_id` to a language in the closed set (anything else becomes `other`).
    pub fn set_bundle_lang(&self, app_id: &str, lang: &str) {
        let mut map = self.langs.write().unwrap_or_else(|e| e.into_inner());
        if map.len() >= MAX_BUNDLES && !map.contains_key(app_id) {
            return;
        }
        map.insert(app_id.to_string(), fold_lang(lang));
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

    /// Seeds both registries from the documented environment variables.
    pub fn load_env(&self) {
        if let Ok(raw) = std::env::var("WADDLES_COMMAND_LATENCY_COMMANDS") {
            for name in raw.split(',') {
                self.register(name);
            }
        }
        if let Ok(raw) = std::env::var("WADDLES_BUNDLE_LANGS") {
            for pair in raw.split(',') {
                if let Some((app, lang)) = pair.split_once(':') {
                    self.set_bundle_lang(app.trim(), lang);
                }
            }
        }
    }
}

/// The process-wide registry, seeded from the environment on first use.
pub fn global() -> &'static CommandRegistry {
    static GLOBAL: OnceLock<CommandRegistry> = OnceLock::new();
    GLOBAL.get_or_init(|| {
        let reg = CommandRegistry::new();
        reg.load_env();
        reg
    })
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
}
