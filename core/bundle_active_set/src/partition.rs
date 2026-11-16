//! Executor language partition -- compiled vs scripted executor pools.
//!
//! Owner decision (2026-11-14, architecture exception): compiled bundles (`rust`, `csharp`,
//! `go`) and scripted bundles (`python`, `javascript`, `nodejs`, `typescript`, `ruby`) run on
//! SEPARATE executor pods so a heavy interpreter-bearing scripted bundle can never starve (or
//! be co-located with) the compiled data path, and the scripted pool can move to Kata later.
//!
//! ```text
//!   app_versions.language --LanguageTable--> LanguageClass (compiled | scripted)
//!   executor pod: EXECUTOR_LANGUAGE_CLASS --hello.executor_id "class:pod"--> ExecutorClass
//!   SessionRouter: a bundle is only ever placed on executors whose class accepts its class
//! ```
//!
//! **Why the class rides in the `executor_id`.** The host-API `HelloBody`/`LoadBody` wire
//! types live in the pinned `penguin-bundle-host` crate (penguin-libs) and carry no class
//! field. The executor therefore qualifies its identity as `compiled:<pod>` / `scripted:<pod>`
//! (an `any` executor and every pre-partition executor stay unqualified), which the stages
//! parse back with [`parse_executor_id`]. A first-class `HelloBody` field replaces this when
//! penguin-libs next bumps; the qualified form is stable per pod, so slot hashing is unaffected.
//!
//! **Fail loud.** A bundle whose language is in no class, or whose class has no live eligible
//! executor, is never placed anywhere else -- the router returns no targets and the stages log
//! at ERROR and count `executor_partition_unroutable_total`.

use std::collections::BTreeMap;
use std::sync::OnceLock;

use thiserror::Error;

/// The class a bundle's language belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum LanguageClass {
    /// Ahead-of-time compiled languages (`rust`, `csharp`, `go` by default).
    Compiled,
    /// Interpreter-bearing languages (`python`, `javascript`, ... by default).
    Scripted,
}

impl LanguageClass {
    /// Stable label for logs, metrics and the executor-id qualifier.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            LanguageClass::Compiled => "compiled",
            LanguageClass::Scripted => "scripted",
        }
    }
}

/// Which bundle classes an executor pool accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ExecutorClass {
    /// Accepts both classes (the unpartitioned default and every pre-partition executor).
    #[default]
    Any,
    /// Accepts only compiled bundles.
    Compiled,
    /// Accepts only scripted bundles.
    Scripted,
}

impl ExecutorClass {
    /// Whether an executor of this class may host a bundle of `class`.
    #[must_use]
    pub fn accepts(self, class: LanguageClass) -> bool {
        match self {
            ExecutorClass::Any => true,
            ExecutorClass::Compiled => class == LanguageClass::Compiled,
            ExecutorClass::Scripted => class == LanguageClass::Scripted,
        }
    }

    /// Stable label (`any` / `compiled` / `scripted`).
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            ExecutorClass::Any => "any",
            ExecutorClass::Compiled => "compiled",
            ExecutorClass::Scripted => "scripted",
        }
    }

    /// Parses `EXECUTOR_LANGUAGE_CLASS`: `compiled`, `scripted` or `any` (case-insensitive,
    /// blank = `any`). Anything else is a typed error -- a typo must never silently widen a
    /// pool to `any`.
    pub fn parse(raw: &str) -> Result<Self, PartitionError> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "" | "any" => Ok(ExecutorClass::Any),
            "compiled" => Ok(ExecutorClass::Compiled),
            "scripted" => Ok(ExecutorClass::Scripted),
            other => Err(PartitionError::UnknownClass(other.to_string())),
        }
    }
}

/// Where a bundle may be placed, resolved by the caller from the bundle's language.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Placement {
    /// The legacy single-bundle env path: no language known, no partition applied.
    Unpartitioned,
    /// A bundle of a known class: only executors that accept it.
    Class(LanguageClass),
    /// A bundle whose language is in no class: placed nowhere (fail loud).
    UnknownLanguage,
}

impl Placement {
    /// Metric/log label for the placement.
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Placement::Unpartitioned => "unpartitioned",
            Placement::Class(c) => c.as_str(),
            Placement::UnknownLanguage => "unknown_language",
        }
    }
}

/// Failures parsing partition configuration.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum PartitionError {
    /// `EXECUTOR_LANGUAGE_CLASS` was not `compiled|scripted|any`.
    #[error("unknown executor language class {0:?} (expected compiled, scripted or any)")]
    UnknownClass(String),
    /// A language appears in more than one class (or twice).
    #[error(
        "language {language:?} appears in more than one class (or twice): {first} and {second}"
    )]
    DuplicateLanguage {
        /// The offending language token.
        language: String,
        /// Class it was first seen in.
        first: &'static str,
        /// Class it was seen in again.
        second: &'static str,
    },
    /// A class entry in the spec was malformed.
    #[error("malformed language-class spec entry {0:?} (expected `compiled=a,b;scripted=c,d`)")]
    Malformed(String),
    /// The spec named no languages at all (a zero-language table routes nothing).
    #[error("language-class spec is empty: at least one language is required")]
    Empty,
    /// `EXECUTOR_LANGUAGES` lists a language of the wrong class for this executor.
    #[error("EXECUTOR_LANGUAGES lists {language:?} (class {actual}) but this executor's class is {configured}")]
    LanguageOutsideClass {
        /// The offending language token.
        language: String,
        /// The class the table gives that language.
        actual: &'static str,
        /// This executor's configured class.
        configured: &'static str,
    },
    /// `EXECUTOR_LANGUAGES` lists a language the table does not know.
    #[error("EXECUTOR_LANGUAGES lists {0:?}, which is in no language class")]
    UnknownLanguage(String),
}

/// Language -> class map. The built-in default mirrors the chart's
/// `bundleExecutor.languageClasses`; the chart also renders it into
/// `EXECUTOR_LANGUAGE_CLASSES` on every stage so the two cannot drift.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LanguageTable {
    by_language: BTreeMap<String, LanguageClass>,
}

/// Built-in compiled languages (chart default `languageClasses.compiled`).
pub const DEFAULT_COMPILED: [&str; 3] = ["rust", "csharp", "go"];
/// Built-in scripted languages (chart default `languageClasses.scripted`).
pub const DEFAULT_SCRIPTED: [&str; 5] = ["python", "javascript", "nodejs", "typescript", "ruby"];

impl Default for LanguageTable {
    fn default() -> Self {
        let mut by_language = BTreeMap::new();
        for l in DEFAULT_COMPILED {
            by_language.insert(l.to_string(), LanguageClass::Compiled);
        }
        for l in DEFAULT_SCRIPTED {
            by_language.insert(l.to_string(), LanguageClass::Scripted);
        }
        Self { by_language }
    }
}

impl LanguageTable {
    /// Parses `compiled=rust,csharp;scripted=python,ruby` (the chart renders this form).
    /// A language in two classes, a malformed entry, an unknown class name or an empty table
    /// is an error -- never a silent fallback to the default.
    pub fn parse(spec: &str) -> Result<Self, PartitionError> {
        let mut by_language: BTreeMap<String, LanguageClass> = BTreeMap::new();
        for entry in spec.split(';').map(str::trim).filter(|e| !e.is_empty()) {
            let (name, langs) = entry
                .split_once('=')
                .ok_or_else(|| PartitionError::Malformed(entry.to_string()))?;
            let class = match name.trim().to_ascii_lowercase().as_str() {
                "compiled" => LanguageClass::Compiled,
                "scripted" => LanguageClass::Scripted,
                other => return Err(PartitionError::UnknownClass(other.to_string())),
            };
            for lang in langs.split(',').map(str::trim).filter(|l| !l.is_empty()) {
                let lang = lang.to_ascii_lowercase();
                if let Some(first) = by_language.insert(lang.clone(), class) {
                    return Err(PartitionError::DuplicateLanguage {
                        language: lang,
                        first: first.as_str(),
                        second: class.as_str(),
                    });
                }
            }
        }
        if by_language.is_empty() {
            return Err(PartitionError::Empty);
        }
        Ok(Self { by_language })
    }

    /// The class of `language` (case-insensitive), or `None` when it is in no class.
    #[must_use]
    pub fn class_of(&self, language: &str) -> Option<LanguageClass> {
        self.by_language
            .get(language.trim().to_ascii_lowercase().as_str())
            .copied()
    }

    /// The [`Placement`] for a bundle written in `language`.
    #[must_use]
    pub fn placement(&self, language: &str) -> Placement {
        self.class_of(language)
            .map_or(Placement::UnknownLanguage, Placement::Class)
    }

    /// Languages of `class`, sorted.
    #[must_use]
    pub fn languages_of(&self, class: LanguageClass) -> Vec<String> {
        self.by_language
            .iter()
            .filter(|(_, c)| **c == class)
            .map(|(l, _)| l.clone())
            .collect()
    }

    /// Validates an executor's own config: every language in `languages` must be known and, for
    /// a `compiled`/`scripted` executor, of that class. `any` executors accept any known language.
    pub fn validate_executor(
        &self,
        class: ExecutorClass,
        languages: &[String],
    ) -> Result<(), PartitionError> {
        for l in languages {
            let actual = self
                .class_of(l)
                .ok_or_else(|| PartitionError::UnknownLanguage(l.clone()))?;
            if !class.accepts(actual) {
                return Err(PartitionError::LanguageOutsideClass {
                    language: l.clone(),
                    actual: actual.as_str(),
                    configured: class.as_str(),
                });
            }
        }
        Ok(())
    }
}

/// Env var the chart renders on both stages: the language -> class table.
pub const ENV_LANGUAGE_CLASSES: &str = "EXECUTOR_LANGUAGE_CLASSES";

static TABLE: OnceLock<LanguageTable> = OnceLock::new();

/// Reads [`ENV_LANGUAGE_CLASSES`] and installs the process-wide table. Unset/blank = the
/// built-in default; a malformed value is an ERROR the stage must refuse to start on (never a
/// silent default). Idempotent: the first install wins.
pub fn init_table_from_env() -> Result<&'static LanguageTable, PartitionError> {
    let table = match std::env::var(ENV_LANGUAGE_CLASSES) {
        Ok(v) if !v.trim().is_empty() => LanguageTable::parse(&v)?,
        _ => LanguageTable::default(),
    };
    Ok(TABLE.get_or_init(|| table))
}

/// The process-wide table (the default until [`init_table_from_env`] ran).
#[must_use]
pub fn table() -> &'static LanguageTable {
    TABLE.get_or_init(LanguageTable::default)
}

/// Qualifies a pod name into the `executor_id` an executor reports: `compiled:<pod>` /
/// `scripted:<pod>`; an `any` executor reports the bare pod name (pre-partition wire shape).
#[must_use]
pub fn qualify_executor_id(class: ExecutorClass, pod: &str) -> String {
    match class {
        ExecutorClass::Any => pod.to_string(),
        c => format!("{}:{pod}", c.as_str()),
    }
}

/// Splits a reported `executor_id` into its class and bare identity. An unqualified id (or any
/// other prefix) is class `any`, with the id returned whole.
#[must_use]
pub fn parse_executor_id(id: &str) -> (ExecutorClass, &str) {
    if let Some(rest) = id.strip_prefix("compiled:") {
        return (ExecutorClass::Compiled, rest);
    }
    if let Some(rest) = id.strip_prefix("scripted:") {
        return (ExecutorClass::Scripted, rest);
    }
    (ExecutorClass::Any, id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_table_splits_the_owner_decided_languages() {
        let t = LanguageTable::default();
        for l in ["rust", "csharp", "go"] {
            assert_eq!(t.class_of(l), Some(LanguageClass::Compiled), "{l}");
        }
        for l in ["python", "javascript", "nodejs", "typescript", "ruby"] {
            assert_eq!(t.class_of(l), Some(LanguageClass::Scripted), "{l}");
        }
        assert_eq!(t.class_of("RUST"), Some(LanguageClass::Compiled));
        assert_eq!(t.class_of(" Python "), Some(LanguageClass::Scripted));
        assert_eq!(t.class_of("cobol"), None);
        assert_eq!(t.placement("cobol"), Placement::UnknownLanguage);
        assert_eq!(t.placement("go"), Placement::Class(LanguageClass::Compiled));
    }

    #[test]
    fn parse_round_trips_and_rejects_bad_specs() {
        let t = LanguageTable::parse("compiled=rust,go; scripted=python,lua").unwrap();
        assert_eq!(t.class_of("lua"), Some(LanguageClass::Scripted));
        assert_eq!(
            t.class_of("csharp"),
            None,
            "an explicit table replaces the default"
        );
        assert_eq!(
            t.languages_of(LanguageClass::Compiled),
            vec!["go".to_string(), "rust".to_string()]
        );
        assert_eq!(
            LanguageTable::parse("compiled=rust;scripted=rust"),
            Err(PartitionError::DuplicateLanguage {
                language: "rust".into(),
                first: "compiled",
                second: "scripted"
            })
        );
        assert_eq!(
            LanguageTable::parse("compiled=rust,rust").unwrap_err(),
            PartitionError::DuplicateLanguage {
                language: "rust".into(),
                first: "compiled",
                second: "compiled"
            }
        );
        assert_eq!(
            LanguageTable::parse("interpreted=lua"),
            Err(PartitionError::UnknownClass("interpreted".into()))
        );
        assert_eq!(
            LanguageTable::parse("compiled"),
            Err(PartitionError::Malformed("compiled".into()))
        );
        assert_eq!(LanguageTable::parse(" ; "), Err(PartitionError::Empty));
    }

    #[test]
    fn executor_class_parse_is_strict() {
        assert_eq!(ExecutorClass::parse(""), Ok(ExecutorClass::Any));
        assert_eq!(ExecutorClass::parse(" Any "), Ok(ExecutorClass::Any));
        assert_eq!(
            ExecutorClass::parse("COMPILED"),
            Ok(ExecutorClass::Compiled)
        );
        assert_eq!(
            ExecutorClass::parse("scripted"),
            Ok(ExecutorClass::Scripted)
        );
        assert_eq!(
            ExecutorClass::parse("compilled"),
            Err(PartitionError::UnknownClass("compilled".into()))
        );
    }

    #[test]
    fn accepts_matrix() {
        use LanguageClass::{Compiled, Scripted};
        assert!(ExecutorClass::Any.accepts(Compiled) && ExecutorClass::Any.accepts(Scripted));
        assert!(ExecutorClass::Compiled.accepts(Compiled));
        assert!(!ExecutorClass::Compiled.accepts(Scripted));
        assert!(ExecutorClass::Scripted.accepts(Scripted));
        assert!(!ExecutorClass::Scripted.accepts(Compiled));
    }

    #[test]
    fn validate_executor_catches_misconfigured_pools() {
        let t = LanguageTable::default();
        let langs = |v: &[&str]| v.iter().map(|s| (*s).to_string()).collect::<Vec<_>>();
        assert_eq!(
            t.validate_executor(ExecutorClass::Compiled, &langs(&["rust", "go"])),
            Ok(())
        );
        assert_eq!(
            t.validate_executor(ExecutorClass::Any, &langs(&["rust", "python"])),
            Ok(())
        );
        assert_eq!(
            t.validate_executor(ExecutorClass::Compiled, &langs(&["rust", "python"])),
            Err(PartitionError::LanguageOutsideClass {
                language: "python".into(),
                actual: "scripted",
                configured: "compiled"
            })
        );
        assert_eq!(
            t.validate_executor(ExecutorClass::Scripted, &langs(&["cobol"])),
            Err(PartitionError::UnknownLanguage("cobol".into()))
        );
    }

    #[test]
    fn executor_id_qualification_round_trips() {
        let pod = "waddlebot-svc-bundles-executor-script-1";
        for class in [ExecutorClass::Compiled, ExecutorClass::Scripted] {
            let id = qualify_executor_id(class, pod);
            assert_eq!(parse_executor_id(&id), (class, pod));
        }
        assert_eq!(qualify_executor_id(ExecutorClass::Any, pod), pod);
        // Wire format pinned: bundle_executor/src/config.rs reported_executor_id() emits these.
        assert_eq!(
            qualify_executor_id(ExecutorClass::Compiled, "pod-0"),
            "compiled:pod-0"
        );
        assert_eq!(
            qualify_executor_id(ExecutorClass::Scripted, "pod-0"),
            "scripted:pod-0"
        );
        assert_eq!(parse_executor_id(pod), (ExecutorClass::Any, pod));
        assert_eq!(
            parse_executor_id("other:pod"),
            (ExecutorClass::Any, "other:pod"),
            "an unknown prefix is an opaque unqualified id"
        );
    }

    #[test]
    fn placement_labels_are_a_closed_vocabulary() {
        assert_eq!(Placement::Unpartitioned.label(), "unpartitioned");
        assert_eq!(
            Placement::Class(LanguageClass::Scripted).label(),
            "scripted"
        );
        assert_eq!(Placement::UnknownLanguage.label(), "unknown_language");
    }

    #[test]
    fn process_table_defaults_without_env() {
        assert_eq!(table().class_of("rust"), Some(LanguageClass::Compiled));
    }
}
