//! Real-path proof that the kv Valkey ACL the Helm chart ships does NOT block anything
//! `bundle_host_kv` issues: a real `valkey/valkey` container is started with the EXACT ACL rules
//! parsed out of `k8s/helm/waddlebot/templates/infrastructure/valkey-kv.yaml` (not a copy), and
//! every `KvHost` op (plain GET/SET, the Lua `EVALSHA`/`SCRIPT LOAD` scripts, `SCAN`, TTL,
//! CAS/list) runs against it as the same authenticated `default` user the services use.
//!
//! Why it exists: when bundle kv calls fail on alpha ("Something went wrong updating the counter
//! storage") the ACL is the first suspect. The cause that time was elsewhere (a missing
//! `storage.kv` capability in the install summary), and this file keeps the ACL provably ruled
//! out -- and fails loudly if a chart ACL edit ever does start denying a command the host needs.
//! A negative control (`FLUSHALL` must be `NOPERM`) proves the ACL is really enforced, so a green
//! run cannot be a no-op.
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
/// The chart template the ACL is read from at test time.
const CHART_TEMPLATE: &str =
    include_str!("../../../k8s/helm/waddlebot/templates/infrastructure/valkey-kv.yaml");

/// The ACL rule tokens that follow the password hash on the chart's `--user default on ...`
/// line (quotes stripped, exactly as the shell in the pod would pass them).
fn chart_acl_rules() -> Vec<String> {
    let line = CHART_TEMPLATE
        .lines()
        .find(|l| l.contains("--user default on"))
        .expect("chart template has a `--user default on` ACL line");
    let after = line.split("--user default on").nth(1).unwrap();
    let mut tokens = after.split_whitespace().filter(|t| *t != "\\");
    let hash_token = tokens.next().expect("password hash token");
    assert!(
        hash_token.contains("APP_HASH"),
        "unexpected ACL shape: {line}"
    );
    let rules: Vec<String> = tokens
        .map(|t| t.trim_matches('\'').trim_matches('"').to_string())
        .collect();
    assert!(
        rules.iter().any(|r| r == "-@all") && rules.iter().any(|r| r == "-@dangerous"),
        "chart ACL no longer an allow-list with -@dangerous: {rules:?}"
    );
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

/// Starts Valkey with the chart's ACL + `noeviction` (the chart hard-wires it) and waits ready.
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
                "default",
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
    redis::Client::open(format!("redis://:{TEST_PASSWORD}@127.0.0.1:{}/", v.port))
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

/// Every op `svc_process`/`svc_action` issue through `KvHost`, as the chart-ACL `default` user.
#[tokio::test]
async fn every_kv_host_op_succeeds_under_the_chart_acl() {
    let v = start_acl_valkey();
    let host = KvHost::new(connect(&v).await, granted("waddles.core.example.count"));
    let scope = KvScope::new(
        "default",
        Some("waddlebot".to_string()),
        "waddles.core.example.count",
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
