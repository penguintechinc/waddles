//! Language-partitioned executors: the executor PROFILE a bundle-executor
//! announces at host-api connect, the ONE language -> class table, and the pure
//! decision "may this session hold a bundle written in language X".
//!
//! # Why compiled and scripted executors are SEPARATE, permanently
//!
//! The two bundle families must be handled completely differently, so they
//! intentionally live on different executor Deployments (owner decision
//! 2026-11-14), not as a temporary Python workaround:
//!
//! | | COMPILED (`rust`, `csharp`, ...) | SCRIPTED (`python`) |
//! |---|---|---|
//! | artifact | deterministic build; strict artifact-digest (byte) check | `componentize-py` snapshot, NOT byte-reproducible; no byte check (source-hash change detection planned, #1320 for v3.1.x) |
//! | size / load | small, fast loads | large components, minutes to JIT-compile (pyping: 880 s) |
//! | compile cache | meaningful, small | huge, per-executor |
//! | resources | modest | different (CPU/RAM heavy compiles) |
//!
//! Sharing one executor made the scripted compiles starve everything else
//! (alpha 2026-11-14: ~160 bundles, `pool_exhausted`, 120 s load timeouts).
//! Partitioning only works if the stages (`svc_process`, `svc_action`) route
//! load/unload and invokes by language -- this module is that single decision
//! point, shared by both stages.
//!
//! # ONE source of truth for language -> class
//!
//! [`LANGUAGE_CLASS_TABLE`] mirrors `COMPILED_LANGUAGES` / `SCRIPTED_LANGUAGES`
//! in `hub_api/cli/seed_core_bundles.py`. The test
//! `table_matches_the_python_seeder_sets` PARSES that Python file and fails if
//! the two ever disagree (so adding `go` to the seeder -- PR #1321 -- forces
//! the one-line Rust change). A language in neither set is UNKNOWN and fails
//! LOUD at routing; it is never guessed onto an executor.
//!
//! # Wire carrier (backward compatible)
//!
//! `HelloBody` belongs to the external pinned `penguin-bundle-host` crate and
//! has no profile field. The profile therefore rides inside the informational
//! `executor_version` string as SemVer-style build metadata:
//! `"0.1.0+profile=scripted"` ([`HELLO_PROFILE_MARKER`]). Build metadata is
//! ignored by SemVer precedence, so an old stage that only logs the string is
//! unaffected, and an OLD executor (no marker) is parsed as
//! [`ExecutorProfile::All`] -- exactly today's behavior. A typed `profile` field
//! in `penguin-bundle-host` is the follow-up; this carrier then retires.
//!
//! # Decision table ([`ExecutorProfile::decide`])
//!
//! | profile    | compiled lang | scripted lang | unknown language |
//! |------------|---------------|---------------|------------------|
//! | `all`      | accept        | accept        | accept (as today)|
//! | `compiled` | accept        | reject        | UNKNOWN (loud)   |
//! | `scripted` | reject        | accept        | UNKNOWN (loud)   |
//!
//! `all` exists ONLY for local/dev single-executor installs; the executor logs
//! a WARN at startup when it runs `all` outside dev (`EXECUTOR_ALLOW_ALL_PROFILE`).

use std::collections::HashMap;
use std::hash::Hash;

use crate::session_sync::SessionId;

/// Marker inside `HelloBody.executor_version` that introduces the profile
/// (SemVer-style build metadata: `<version>+profile=<name>`).
pub const HELLO_PROFILE_MARKER: &str = "+profile=";

/// The class a bundle language belongs to -- decides which executor family
/// hosts it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum LanguageClass {
    /// Deterministic compiled build: strict artifact-digest check, small/fast
    /// loads, meaningful compile cache.
    Compiled,
    /// `componentize-py` snapshot: not byte-reproducible, large/slow compile.
    Scripted,
}

/// THE language -> class table (one source of truth in the Rust data plane).
/// Mirrors `COMPILED_LANGUAGES` / `SCRIPTED_LANGUAGES` in
/// `hub_api/cli/seed_core_bundles.py`; kept equal by the
/// `table_matches_the_python_seeder_sets` test.
pub const LANGUAGE_CLASS_TABLE: [(&str, LanguageClass); 3] = [
    ("rust", LanguageClass::Compiled),
    ("csharp", LanguageClass::Compiled),
    ("python", LanguageClass::Scripted),
];

/// The class of `language` (trimmed, case-insensitive), or `None` for a
/// language in neither set -- callers must treat `None` as UNKNOWN (loud).
pub fn language_class(language: &str) -> Option<LanguageClass> {
    let lang = language.trim().to_ascii_lowercase();
    LANGUAGE_CLASS_TABLE
        .iter()
        .find(|(name, _)| *name == lang)
        .map(|(_, class)| *class)
}

/// What an executor replica is willing to host.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum ExecutorProfile {
    /// Accepts every bundle. LOCAL/DEV single-executor installs only (also what
    /// a stage assumes for an old executor that announces nothing).
    #[default]
    All,
    /// Compiled-language bundles only (rust, csharp, ...).
    Compiled,
    /// Scripted-language bundles only (python / componentize-py).
    Scripted,
}

/// Outcome of [`ExecutorProfile::decide`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProfileDecision {
    /// This profile may host the language.
    Accept,
    /// A known language this profile deliberately does not host.
    Reject,
    /// A partitioned profile met a language it cannot classify -- never
    /// silently loaded; callers must fail LOUD.
    UnknownLanguage,
}

/// An `EXECUTOR_PROFILE` / hello value that is not `all|compiled|scripted`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("unknown executor profile {0:?}: expected one of all, compiled, scripted")]
pub struct ParseProfileError(pub String);

impl ExecutorProfile {
    /// The stable lowercase name used on the wire, in env config and as the
    /// `profile` metric label.
    pub fn as_str(self) -> &'static str {
        match self {
            ExecutorProfile::All => "all",
            ExecutorProfile::Compiled => "compiled",
            ExecutorProfile::Scripted => "scripted",
        }
    }

    /// Parses `all|compiled|scripted` (case-insensitive, trimmed).
    ///
    /// # Errors
    /// [`ParseProfileError`] for anything else -- never defaults silently.
    pub fn parse(raw: &str) -> Result<Self, ParseProfileError> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "all" => Ok(ExecutorProfile::All),
            "compiled" => Ok(ExecutorProfile::Compiled),
            "scripted" => Ok(ExecutorProfile::Scripted),
            _ => Err(ParseProfileError(raw.to_string())),
        }
    }

    /// Extracts the profile an executor announced in `HelloBody.
    /// executor_version`. No marker (an old executor) => [`Self::All`].
    ///
    /// # Errors
    /// [`ParseProfileError`] when a marker IS present but its value is not a
    /// known profile -- a typo'd profile must refuse the connection, never
    /// quietly widen to `all`.
    pub fn from_hello_version(executor_version: &str) -> Result<Self, ParseProfileError> {
        match executor_version.split_once(HELLO_PROFILE_MARKER) {
            None => Ok(ExecutorProfile::All),
            Some((_, rest)) => {
                let name = rest.split(['+', '.', ' ']).next().unwrap_or("");
                Self::parse(name)
            }
        }
    }

    /// Renders `version` with this profile appended as build metadata -- the
    /// executor side of the wire carrier. [`Self::All`] appends nothing, so
    /// an unprofiled executor is byte-identical to today's hello.
    pub fn hello_version(self, version: &str) -> String {
        match self {
            ExecutorProfile::All => version.to_string(),
            other => format!("{version}{HELLO_PROFILE_MARKER}{}", other.as_str()),
        }
    }

    /// May a session with this profile host a bundle written in `language`?
    /// See the module decision table.
    pub fn decide(self, language: &str) -> ProfileDecision {
        if self == ExecutorProfile::All {
            return ProfileDecision::Accept;
        }
        match (self, language_class(language)) {
            (_, None) => ProfileDecision::UnknownLanguage,
            (ExecutorProfile::Compiled, Some(LanguageClass::Compiled))
            | (ExecutorProfile::Scripted, Some(LanguageClass::Scripted)) => {
                ProfileDecision::Accept
            }
            _ => ProfileDecision::Reject,
        }
    }

    /// `true` only for [`ProfileDecision::Accept`] -- the eligibility test
    /// the planner, the divergence check and the invoke pickers share.
    pub fn accepts(self, language: &str) -> bool {
        self.decide(language) == ProfileDecision::Accept
    }
}

/// Per-live-session profiles, as last announced at each session's hello. A
/// session absent from the map is treated as [`ExecutorProfile::All`]
/// (single-executor deployments and tests that never set one behave exactly
/// as before this feature existed).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SessionProfiles {
    by_session: HashMap<SessionId, ExecutorProfile>,
}

impl SessionProfiles {
    /// An empty map: every session is `all`.
    pub fn new() -> Self {
        Self::default()
    }

    /// Builds from `(session, profile)` pairs.
    pub fn from_pairs(pairs: impl IntoIterator<Item = (SessionId, ExecutorProfile)>) -> Self {
        Self {
            by_session: pairs.into_iter().collect(),
        }
    }

    /// Records `session`'s announced profile.
    pub fn set(&mut self, session: SessionId, profile: ExecutorProfile) {
        self.by_session.insert(session, profile);
    }

    /// The profile of `session` (`all` when never recorded).
    pub fn get(&self, session: SessionId) -> ExecutorProfile {
        self.by_session.get(&session).copied().unwrap_or_default()
    }

    /// Live-session count per profile (for the sessions-by-profile gauge),
    /// restricted to `live_sessions`.
    pub fn counts(&self, live_sessions: &[SessionId]) -> HashMap<ExecutorProfile, usize> {
        let mut out = HashMap::new();
        for &s in live_sessions {
            *out.entry(self.get(s)).or_insert(0) += 1;
        }
        out
    }

    /// Live sessions among `live_sessions` whose profile accepts `language`.
    pub fn eligible_sessions(&self, live_sessions: &[SessionId], language: &str) -> Vec<SessionId> {
        live_sessions
            .iter()
            .copied()
            .filter(|&s| self.get(s).accepts(language))
            .collect()
    }
}

/// Why an ACTIVE bundle has no live session that may host it
/// ([`placement_gaps`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlacementGapKind {
    /// A live partitioned session met a language it cannot classify.
    UnknownLanguage,
    /// The language is known but no live session's profile accepts it (e.g.
    /// the python executor is down / still restarting).
    NoMatchingSession,
}

impl PlacementGapKind {
    /// The `reason` label of `waddles_executor_profile_mismatch_total`.
    pub fn as_str(self) -> &'static str {
        match self {
            PlacementGapKind::UnknownLanguage => "unknown_language",
            PlacementGapKind::NoMatchingSession => "no_matching_session",
        }
    }
}

/// One active bundle that cannot be placed on any live session.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlacementGap<K> {
    pub key: K,
    pub language: String,
    pub kind: PlacementGapKind,
}

/// Every active bundle that NO live session may host. Empty when there are no
/// live sessions at all (nothing to place onto yet -- the existing
/// "no executor connection" paths own that case) and, by construction, always
/// empty when any live session is `all`. Callers log each gap at ERROR and
/// count it: an unplaceable bundle is loaded NOWHERE, never on the wrong
/// executor.
pub fn placement_gaps<K, Row>(
    active: &HashMap<K, Row>,
    live_sessions: &[SessionId],
    profiles: &SessionProfiles,
    language_of: impl Fn(&Row) -> &str,
) -> Vec<PlacementGap<K>>
where
    K: Eq + Hash + Clone,
{
    if live_sessions.is_empty() {
        return Vec::new();
    }
    let mut gaps = Vec::new();
    for (key, row) in active {
        let language = language_of(row);
        let decisions: Vec<ProfileDecision> = live_sessions
            .iter()
            .map(|&s| profiles.get(s).decide(language))
            .collect();
        if decisions.contains(&ProfileDecision::Accept) {
            continue;
        }
        let kind = if decisions.contains(&ProfileDecision::UnknownLanguage) {
            PlacementGapKind::UnknownLanguage
        } else {
            PlacementGapKind::NoMatchingSession
        };
        gaps.push(PlacementGap {
            key: key.clone(),
            language: language.to_string(),
            kind,
        });
    }
    gaps
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Extracts the quoted strings of `NAME = frozenset({"a", "b"})` from the
    /// Python seeder source. Panics (loudly) if the assignment is not found --
    /// a silently-empty parse would make the parity check vacuous.
    fn parse_python_set(source: &str, name: &str) -> Vec<String> {
        let start = source
            .lines()
            .position(|l| l.trim_start().starts_with(&format!("{name} = frozenset(")))
            .unwrap_or_else(|| panic!("{name} assignment not found in seed_core_bundles.py"));
        let mut buf = String::new();
        for line in source.lines().skip(start) {
            buf.push_str(line);
            if line.contains("})") {
                break;
            }
        }
        let inner = buf
            .split_once('{')
            .and_then(|(_, rest)| rest.split_once('}'))
            .map(|(inside, _)| inside.to_string())
            .unwrap_or_else(|| panic!("{name}: malformed frozenset literal: {buf}"));
        let mut out: Vec<String> = inner
            .split(',')
            .map(|s| s.trim().trim_matches('"').trim_matches('\'').to_string())
            .filter(|s| !s.is_empty())
            .collect();
        out.sort();
        out
    }

    /// ONE source of truth: the Rust language -> class table must equal the
    /// Python seeder's `COMPILED_LANGUAGES` / `SCRIPTED_LANGUAGES` exactly. When
    /// the seeder gains a language (e.g. `go`, PR #1321) this fails until
    /// [`LANGUAGE_CLASS_TABLE`] is updated -- by design.
    #[test]
    fn table_matches_the_python_seeder_sets() {
        let source = include_str!("../../../hub_api/cli/seed_core_bundles.py");
        let py_compiled = parse_python_set(source, "COMPILED_LANGUAGES");
        let py_scripted = parse_python_set(source, "SCRIPTED_LANGUAGES");
        assert!(
            !py_compiled.is_empty() && !py_scripted.is_empty(),
            "parsed {} compiled / {} scripted languages from the seeder -- zero is a failure",
            py_compiled.len(),
            py_scripted.len()
        );
        let mut rs_compiled: Vec<String> = LANGUAGE_CLASS_TABLE
            .iter()
            .filter(|(_, c)| *c == LanguageClass::Compiled)
            .map(|(n, _)| n.to_string())
            .collect();
        let mut rs_scripted: Vec<String> = LANGUAGE_CLASS_TABLE
            .iter()
            .filter(|(_, c)| *c == LanguageClass::Scripted)
            .map(|(n, _)| n.to_string())
            .collect();
        rs_compiled.sort();
        rs_scripted.sort();
        assert_eq!(
            rs_compiled, py_compiled,
            "Rust compiled languages != hub_api/cli/seed_core_bundles.py COMPILED_LANGUAGES"
        );
        assert_eq!(
            rs_scripted, py_scripted,
            "Rust scripted languages != hub_api/cli/seed_core_bundles.py SCRIPTED_LANGUAGES"
        );
    }

    /// The parity check itself can fail: a doctored seeder that adds `go` to
    /// COMPILED_LANGUAGES must NOT match the table.
    #[test]
    fn parity_check_detects_a_disagreeing_seeder() {
        let doctored = "COMPILED_LANGUAGES = frozenset({\"rust\", \"csharp\", \"go\"})\n\
                        SCRIPTED_LANGUAGES = frozenset({\"python\"})\n";
        let py_compiled = parse_python_set(doctored, "COMPILED_LANGUAGES");
        assert_eq!(py_compiled, vec!["csharp", "go", "rust"]);
        let rs: Vec<&str> = LANGUAGE_CLASS_TABLE
            .iter()
            .filter(|(_, c)| *c == LanguageClass::Compiled)
            .map(|(n, _)| *n)
            .collect();
        assert_ne!(rs.len(), py_compiled.len(), "doctored seeder must disagree with the table");
    }

    #[test]
    fn parse_is_case_insensitive_and_rejects_garbage() {
        assert_eq!(ExecutorProfile::parse(" Scripted "), Ok(ExecutorProfile::Scripted));
        assert_eq!(ExecutorProfile::parse("COMPILED"), Ok(ExecutorProfile::Compiled));
        assert_eq!(ExecutorProfile::parse("all"), Ok(ExecutorProfile::All));
        for bad in ["python", "native", "py", ""] {
            assert!(ExecutorProfile::parse(bad).is_err(), "{bad:?} must not parse");
        }
    }

    /// An old executor announces no marker => `all`; a present marker with a
    /// typo is an ERROR, never a silent widen to `all`.
    #[test]
    fn hello_version_missing_marker_is_all_and_bad_marker_is_loud() {
        assert_eq!(ExecutorProfile::from_hello_version("0.1.0"), Ok(ExecutorProfile::All));
        assert_eq!(ExecutorProfile::from_hello_version(""), Ok(ExecutorProfile::All));
        assert_eq!(
            ExecutorProfile::from_hello_version("0.1.0+profile=scripted"),
            Ok(ExecutorProfile::Scripted)
        );
        assert_eq!(
            ExecutorProfile::from_hello_version("0.1.0+profile=compiled"),
            Ok(ExecutorProfile::Compiled)
        );
        assert!(ExecutorProfile::from_hello_version("0.1.0+profile=scriptd").is_err());
        assert!(ExecutorProfile::from_hello_version("0.1.0+profile=").is_err());
        assert!(ExecutorProfile::from_hello_version("0.1.0+profile=python").is_err());
    }

    /// Encode/decode round trip, and `all` is byte-identical to today.
    #[test]
    fn hello_version_round_trips_and_all_is_unchanged() {
        assert_eq!(ExecutorProfile::All.hello_version("0.1.0"), "0.1.0");
        for p in [ExecutorProfile::Scripted, ExecutorProfile::Compiled] {
            let v = p.hello_version("0.1.0");
            assert_eq!(ExecutorProfile::from_hello_version(&v), Ok(p), "{v}");
        }
        // Exact wire literals -- pinned against the executor crate's own test.
        assert_eq!(ExecutorProfile::Scripted.hello_version("0.1.0"), "0.1.0+profile=scripted");
        assert_eq!(ExecutorProfile::Compiled.hello_version("0.1.0"), "0.1.0+profile=compiled");
    }

    #[test]
    fn decide_matches_the_decision_table_for_every_table_language() {
        assert_eq!(LANGUAGE_CLASS_TABLE.len(), 3, "table examined");
        for (lang, class) in LANGUAGE_CLASS_TABLE {
            assert_eq!(ExecutorProfile::All.decide(lang), ProfileDecision::Accept, "{lang}");
            let scripted = class == LanguageClass::Scripted;
            assert_eq!(
                ExecutorProfile::Scripted.decide(lang),
                if scripted { ProfileDecision::Accept } else { ProfileDecision::Reject },
                "scripted/{lang}"
            );
            assert_eq!(
                ExecutorProfile::Compiled.decide(lang),
                if scripted { ProfileDecision::Reject } else { ProfileDecision::Accept },
                "compiled/{lang}"
            );
        }
    }

    /// Unknown language (including `javascript`/`typescript`/`other`, which the
    /// manifest allows but the seeder does not classify): `all` keeps loading it
    /// (single-executor behavior unchanged); a partitioned profile never guesses.
    #[test]
    fn unknown_language_is_loud_for_partitioned_profiles_only() {
        for lang in ["cobol", "", "py", "C#", "javascript", "typescript", "other"] {
            assert_eq!(ExecutorProfile::All.decide(lang), ProfileDecision::Accept, "{lang}");
            assert_eq!(
                ExecutorProfile::Scripted.decide(lang),
                if lang == "C#" || lang.is_empty() || lang == "py" || lang == "cobol" || lang == "javascript" || lang == "typescript" || lang == "other" {
                    ProfileDecision::UnknownLanguage
                } else {
                    ProfileDecision::Reject
                },
                "{lang}"
            );
            assert_eq!(ExecutorProfile::Compiled.decide(lang), ProfileDecision::UnknownLanguage, "{lang}");
            assert!(!ExecutorProfile::Compiled.accepts(lang));
            assert!(!ExecutorProfile::Scripted.accepts(lang));
        }
        // Case/whitespace tolerant for known names.
        assert!(ExecutorProfile::Scripted.accepts(" Python "));
        assert!(ExecutorProfile::Compiled.accepts("CSharp"));
    }

    #[test]
    fn session_profiles_default_to_all_and_filter_eligibility() {
        let mut p = SessionProfiles::new();
        assert_eq!(p.get(7), ExecutorProfile::All);
        p.set(1, ExecutorProfile::Scripted);
        p.set(2, ExecutorProfile::Compiled);
        let live = [1u64, 2, 3];
        assert_eq!(p.eligible_sessions(&live, "python"), vec![1, 3]);
        assert_eq!(p.eligible_sessions(&live, "rust"), vec![2, 3]);
        assert_eq!(p.eligible_sessions(&live, "cobol"), vec![3]);
        let counts = p.counts(&live);
        assert_eq!(counts[&ExecutorProfile::Scripted], 1);
        assert_eq!(counts[&ExecutorProfile::Compiled], 1);
        assert_eq!(counts[&ExecutorProfile::All], 1);
    }

    #[test]
    fn placement_gaps_report_unplaceable_bundles_only() {
        let mut active: HashMap<&str, &str> = HashMap::new();
        active.insert("py", "python");
        active.insert("rs", "rust");
        active.insert("odd", "cobol");
        let live = [1u64, 2];

        // scripted + compiled executors: py and rs placed; odd is UNKNOWN (loud).
        let both = SessionProfiles::from_pairs([
            (1, ExecutorProfile::Scripted),
            (2, ExecutorProfile::Compiled),
        ]);
        let mut gaps = placement_gaps(&active, &live, &both, |l| l);
        gaps.sort_by_key(|g| g.key);
        assert_eq!(gaps.len(), 1, "{gaps:?}");
        assert_eq!(gaps[0].key, "odd");
        assert_eq!(gaps[0].kind, PlacementGapKind::UnknownLanguage);

        // scripted executor gone: py has NO matching session.
        let compiled_only = SessionProfiles::from_pairs([(2, ExecutorProfile::Compiled)]);
        let mut gaps = placement_gaps(&active, &[2], &compiled_only, |l| l);
        gaps.sort_by_key(|g| g.key);
        assert_eq!(gaps.iter().map(|g| g.key).collect::<Vec<_>>(), vec!["odd", "py"]);
        assert_eq!(gaps[1].kind, PlacementGapKind::NoMatchingSession);

        // Any `all` session places everything -> no gaps (today's behavior).
        assert!(placement_gaps(&active, &[9], &SessionProfiles::new(), |l| l).is_empty());
        // No live sessions -> nothing to report here.
        assert!(placement_gaps(&active, &[], &both, |l| l).is_empty());
    }
}
