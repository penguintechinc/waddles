//! Spec `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`
//! SS4.5/SS14.6 test 16: "The executor binary links a networking or
//! database crate" must fail the build. `deny.toml`'s `[bans] deny = [...]`
//! enforces this at resolve time; this test enforces the same thing
//! directly against the committed `Cargo.lock` so the assertion survives
//! even if `cargo deny` isn't run in a given CI path, and so it reports a
//! real denominator (`rules/critical-rules.md` Verification Integrity: "a
//! zero denominator is a failure, not a pass").

use std::path::Path;

const BANNED: &[&str] = &["redis", "deadpool-redis", "sea-orm", "sqlx", "reqwest"];

#[test]
fn no_networking_or_database_crate_in_the_dependency_graph() {
    let lock_path = Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.lock");
    let lock_text = std::fs::read_to_string(&lock_path)
        .unwrap_or_else(|e| panic!("failed to read {}: {e}", lock_path.display()));

    let package_names: Vec<String> = lock_text
        .lines()
        .filter_map(|line| line.trim().strip_prefix("name = \""))
        .filter_map(|rest| rest.strip_suffix('"'))
        .map(str::to_string)
        .collect();

    // Verification Integrity: a scanner examining zero packages is a
    // failure, not clean -- assert a real denominator before asserting
    // anything about its contents.
    assert!(
        package_names.len() > 20,
        "expected a populated Cargo.lock (found {} package entries) -- \
         the lockfile may be stale or the parser may be pointed at the \
         wrong file",
        package_names.len()
    );

    let found: Vec<&String> = package_names
        .iter()
        .filter(|name| BANNED.contains(&name.as_str()))
        .collect();

    assert!(
        found.is_empty(),
        "banned crate(s) present in Cargo.lock: {found:?} -- bundle-executor \
         must hold no Valkey/Postgres/free-form-HTTP client (spec SS4.5/SS14.6 \
         test 16); examined {} package entries",
        package_names.len()
    );

    println!(
        "dependency_policy: examined {} Cargo.lock package entries, 0 banned crates found",
        package_names.len()
    );
}
