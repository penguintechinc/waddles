//! The guard must stay pure logic: this crate declares ZERO dependencies, so it can
//! never grow an HTTP, DB or async-runtime path. Checked against Cargo.toml and Cargo.lock.

const MANIFEST: &str = include_str!("../Cargo.toml");
const LOCK: &str = include_str!("../Cargo.lock");

const FORBIDDEN: &[&str] = &[
    "reqwest",
    "hyper",
    "tonic",
    "axum",
    "ureq",
    "surf",
    "isahc",
    "tokio",
    "async-std",
    "futures",
    "sqlx",
    "sea-orm",
    "diesel",
    "redis",
    "postgres",
    "rusqlite",
    "mysql",
    "object_store",
    "libc",
];

fn dependency_lines(manifest: &str) -> Vec<String> {
    let mut in_deps = false;
    let mut out = Vec::new();
    for line in manifest.lines() {
        let t = line.trim();
        if t.starts_with('[') {
            in_deps = t.contains("dependencies");
            continue;
        }
        if in_deps && !t.is_empty() && !t.starts_with('#') {
            out.push(t.to_string());
        }
    }
    out
}

#[test]
fn manifest_has_no_dependencies_at_all() {
    assert!(MANIFEST.contains("[dependencies]"), "manifest parse sanity");
    let deps = dependency_lines(MANIFEST);
    assert!(
        deps.is_empty(),
        "analytics-policy must stay dependency-free: {deps:?}"
    );
}

#[test]
fn lockfile_contains_only_this_crate() {
    let pkgs: Vec<&str> = LOCK.lines().filter(|l| l.trim() == "[[package]]").collect();
    assert_eq!(pkgs.len(), 1, "Cargo.lock must list only analytics-policy");
    for bad in FORBIDDEN {
        assert!(
            !LOCK.contains(&format!("name = \"{bad}\"")),
            "forbidden crate {bad}"
        );
    }
}

#[test]
fn parser_detects_an_injected_dependency() {
    let injected = format!("{MANIFEST}\nreqwest = \"=0.12.28\"\n");
    assert_eq!(
        dependency_lines(&injected),
        vec!["reqwest = \"=0.12.28\"".to_string()]
    );
}
