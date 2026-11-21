//! Real-path proof that svc-process's OWN kv Valkey ACL user (the one the Helm chart ships in
//! `infrastructure.valkeyAcl.users.svc-process.kv`) does NOT block anything `bundle_host_kv`
//! issues. A real `valkey/valkey` container is started exactly the way the chart renders it:
//! `--user default reset` plus `--user svc-process reset on #<sha256> resetchannels <keys> <cmds>`
//! with the keys and `kvBaseCommands` parsed out of `k8s/helm/waddlebot/values.yaml` (not a copy).
//! Every `KvHost` op (plain GET/SET, the Lua scripts, `SCAN`, TTL, CAS/list) then runs over a
//! connection authenticated as `svc-process` and nobody else.
//!
//! Why it exists: when bundle kv calls fail on alpha the ACL is the first suspect; this keeps it
//! provably ruled out and fails loudly if a chart ACL edit starts denying a command the host
//! needs. Negative controls prove the ACL is really enforced, so a green run cannot be a no-op:
//! `FLUSHALL` is `NOPERM` for svc-process, and an unauthenticated connection gets `NOAUTH`
//! (`default` is `reset`).
//!
//! Requires Docker (the `docker` CLI); CI runs on `ubuntu-latest`. A missing docker is a FAILURE.

use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bundle_host_kv::{CapabilitySnapshot, KvHost, KvScope};
use redis::aio::MultiplexedConnection;
use sha2::{Digest, Sha256};

/// Same exact, immutable image tag `valkey_integration.rs` pins.
const VALKEY_IMAGE: &str = "valkey/valkey:8.1.10-alpine";
/// Throwaway password for the throwaway container only.
const TEST_PASSWORD: &str = "acl-real-path-test-password";
/// The chart values the ACL is read from at test time.
const CHART_VALUES: &str = include_str!("../../../k8s/helm/waddlebot/values.yaml");
/// The ACL user under test: svc-process's own kv user.
const KV_USER: &str = "svc-process";

/// Indent (leading spaces) of a line.
fn indent(line: &str) -> usize {
    line.len() - line.trim_start().len()
}

/// The `kvBaseCommands` string and `svc-process`'s kv `keys` read out of `values.yaml` with an
/// explicit line parser (no YAML dependency): asserts every shape it relies on, so a layout
/// change fails loudly instead of silently testing nothing.
fn chart_kv_acl() -> (String, Vec<String>) {
    let lines: Vec<&str> = CHART_VALUES.lines().collect();

    let base = lines
        .iter()
        .find_map(|l| l.trim().strip_prefix("kvBaseCommands:"))
        .expect("values.yaml has infrastructure.valkeyAcl.kvBaseCommands")
        .trim()
        .trim_matches('"')
        .to_string();
    assert!(
        base.starts_with("-@all") && base.contains("-@dangerous") && base.contains("+config|get"),
        "kvBaseCommands is no longer an allow-list with -@dangerous + config|get: {base}"
    );

    let users_at = lines
        .iter()
        .position(|l| l.trim_end() == "    users:")
        .expect("valkeyAcl.users block");
    let user_at = users_at
        + lines[users_at..]
            .iter()
            .position(|l| l.trim_end() == format!("      {KV_USER}:"))
            .expect("svc-process user is configured");
    let user_end = lines[user_at + 1..]
        .iter()
        .position(|l| !l.trim().is_empty() && !l.trim_start().starts_with('#') && indent(l) <= 6)
        .map_or(lines.len(), |n| user_at + 1 + n);
    let block = &lines[user_at + 1..user_end];

    let kv_at = block
        .iter()
        .position(|l| l.trim_end() == "        kv:")
        .expect("svc-process has a kv instance block");
    let mut keys = Vec::new();
    let mut in_keys = false;
    for l in &block[kv_at + 1..] {
        let t = l.trim();
        if t.is_empty() || t.starts_with('#') {
            continue;
        }
        if indent(l) <= 8 {
            break; // left the kv block
        }
        assert!(
            !t.starts_with("commands:") && !t.starts_with("selectors:"),
            "svc-process kv now overrides commands/selectors; extend this test: {t}"
        );
        if t == "keys:" {
            in_keys = true;
        } else if in_keys {
            keys.push(
                t.strip_prefix("- ")
                    .expect("keys entries are YAML list items")
                    .trim_matches('"')
                    .to_string(),
            );
        }
    }
    assert!(
        keys.iter().any(|k| k == "~bundlekv:*"),
        "svc-process kv keys must include ~bundlekv:*: {keys:?}"
    );
    (base, keys)
}

/// The `--user` rule tokens exactly as the chart renders them (after the password hash):
/// `resetchannels <keys> <kvBaseCommands tokens>`.
fn chart_acl_rules() -> Vec<String> {
    let (base, keys) = chart_kv_acl();
    let mut rules = vec!["resetchannels".to_string()];
    rules.extend(keys);
    rules.extend(base.split_whitespace().map(str::to_string));
    rules
}

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// A running ACL-configured Valkey, force-removed on drop.
struct AclValkey {
    name: String,
    port: u16,
}

impl Drop for AclValkey {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "-f", "-v", &self.name])
            .output();
    }
}

/// Starts Valkey with the chart's rendered ACL (`default reset` + svc-process) + `noeviction` (the chart hard-wires it) and waits ready.
fn start_acl_valkey() -> AclValkey {
    let name = format!(
        "wtc-kvacl-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    let hash: String = Sha256::digest(TEST_PASSWORD.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let mut args: Vec<String> = vec![
        "run".into(),
        "-d".into(),
        "--name".into(),
        name.clone(),
        "-p".into(),
        "127.0.0.1::6379".into(),
        VALKEY_IMAGE.into(),
        "valkey-server".into(),
        "--user".into(),
        "default".into(),
        "reset".into(),
        "--user".into(),
        KV_USER.into(),
        "reset".into(),
        "on".into(),
        format!("#{hash}"),
    ];
    args.extend(chart_acl_rules());
    args.extend(["--maxmemory-policy".into(), "noeviction".into()]);
    let run = Command::new("docker")
        .args(&args)
        .output()
        .expect("docker CLI runs");
    let mut v = AclValkey { name, port: 0 };
    assert!(
        run.status.success(),
        "docker run failed: {}",
        String::from_utf8_lossy(&run.stderr)
    );

    let deadline = Instant::now() + Duration::from_secs(120);
    loop {
        let ping = Command::new("docker")
            .args([
                "exec",
                &v.name,
                "valkey-cli",
                "--user",
                KV_USER,
                "--pass",
                TEST_PASSWORD,
                "--no-auth-warning",
                "ping",
            ])
            .output()
            .expect("docker exec runs");
        if String::from_utf8_lossy(&ping.stdout).contains("PONG") {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "valkey not ready: {}",
            String::from_utf8_lossy(
                &Command::new("docker")
                    .args(["logs", "--tail", "30", &v.name])
                    .output()
                    .unwrap()
                    .stderr
            )
        );
        std::thread::sleep(Duration::from_millis(250));
    }
    let mapped = Command::new("docker")
        .args(["port", &v.name, "6379/tcp"])
        .output()
        .unwrap();
    v.port = String::from_utf8_lossy(&mapped.stdout)
        .lines()
        .find_map(|l| l.rsplit(':').next()?.trim().parse().ok())
        .expect("mapped port");
    v
}

async fn connect(v: &AclValkey) -> MultiplexedConnection {
    redis::Client::open(format!(
        "redis://{KV_USER}:{TEST_PASSWORD}@127.0.0.1:{}/",
        v.port
    ))
    .expect("client opens")
    .get_multiplexed_async_connection()
    .await
    .expect("authenticated connection")
}

fn granted(app_id: &str) -> Arc<CapabilitySnapshot> {
    let s = CapabilitySnapshot::new();
    s.update(app_id, ["storage.kv".to_string()]);
    Arc::new(s)
}

/// Negative control: the ACL is genuinely enforced (a dangerous command is `NOPERM`), so the
/// success assertions below are meaningful, and the policy probe `CONFIG GET` still works.
#[tokio::test]
async fn chart_acl_is_enforced_and_keeps_the_config_get_probe() {
    let v = start_acl_valkey();
    let mut conn = connect(&v).await;

    let flush: Result<String, redis::RedisError> =
        redis::cmd("FLUSHALL").query_async(&mut conn).await;
    let err = flush.expect_err("FLUSHALL must be denied by the chart ACL");
    assert!(
        err.to_string().to_lowercase().contains("no permissions"),
        "expected a no-permission denial, got {err}"
    );

    let policy = bundle_host_kv::policy::check_maxmemory_policy(&mut conn).await;
    assert!(
        matches!(policy, bundle_host_kv::policy::PolicyCheck::Compliant(_)),
        "CONFIG GET maxmemory-policy must work under the chart ACL, got {policy:?}"
    );
}

/// Negative control: `default` is `reset`, so a connection that never authenticates is refused.
#[tokio::test]
async fn unauthenticated_connection_gets_noauth() {
    let v = start_acl_valkey();
    let client = redis::Client::open(format!("redis://127.0.0.1:{}/", v.port)).expect("client");
    let mut conn = client
        .get_multiplexed_async_connection()
        .await
        .expect("an unauthenticated connection opens; commands are what get refused");
    let get: Result<Option<String>, redis::RedisError> = redis::cmd("GET")
        .arg("bundlekv:x")
        .query_async(&mut conn)
        .await;
    let err = get.expect_err("unauthenticated GET must be refused");
    assert_eq!(err.code(), Some("NOAUTH"), "expected NOAUTH, got {err}");
}

/// Every op `svc_process`/`svc_action` issue through `KvHost`, as svc-process's own user.
#[tokio::test]
async fn every_kv_host_op_succeeds_under_the_chart_acl() {
    let v = start_acl_valkey();
    let host = KvHost::new(connect(&v).await, granted("penguintechinc.waddles.count"));
    let scope = KvScope::new(
        "default",
        Some("waddlebot".to_string()),
        "penguintechinc.waddles.count",
    );

    // The `count` bundle's exact first call: a registry lookup on every `!`-prefixed message.
    assert_eq!(
        host.get(&scope, 1, "counters.registry")
            .await
            .expect("get under ACL"),
        None
    );

    host.set(&scope, 2, "counters.registry", br#"["ping"]"#, 0)
        .await
        .expect("set under ACL (Lua)");
    assert_eq!(
        host.get(&scope, 3, "counters.registry").await.unwrap(),
        Some(br#"["ping"]"#.to_vec())
    );
    assert_eq!(
        host.increment(&scope, 4, "counter.ping", 5, 0)
            .await
            .expect("increment (Lua)"),
        5
    );
    host.set(&scope, 5, "ttl.key", b"v", 60)
        .await
        .expect("set with TTL");

    let (value, version) = host
        .get_versioned(&scope, 6, "counters.registry")
        .await
        .unwrap()
        .expect("present");
    assert_eq!(value, br#"["ping"]"#.to_vec());
    host.cas(
        &scope,
        7,
        "counters.registry",
        version,
        br#"["ping","pong"]"#,
        0,
    )
    .await
    .expect("cas (Lua)");
    let page = host
        .list(&scope, 8, "counter", None, 10)
        .await
        .expect("list (Lua + lex index)");
    assert!(
        page.keys.iter().any(|k| k == "counter.ping"),
        "{:?}",
        page.keys
    );
    host.delete(&scope, 9, "counter.ping")
        .await
        .expect("delete (Lua)");
    let (_, v2) = host
        .get_versioned(&scope, 10, "counters.registry")
        .await
        .unwrap()
        .unwrap();
    host.delete_if(&scope, 11, "counters.registry", v2)
        .await
        .expect("delete_if (Lua)");
    assert_eq!(
        host.get(&scope, 12, "counters.registry").await.unwrap(),
        None
    );
}
