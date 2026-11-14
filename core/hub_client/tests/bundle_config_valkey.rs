//! Real-Valkey test of the bundle-config version-key probe (no mocks of the probe or the store).
//!
//! Starts its own `valkey/valkey:8` container on a unique port and removes it afterwards. Docker
//! being absent is a hard FAILURE, never a skip. The config source is the in-memory
//! `StaticConfigSource`, so the only thing under test is `ValkeyVersionProbe` + the cache's
//! reload decision against a real server.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::net::TcpListener;
use std::process::Command;
use std::sync::Arc;
use std::time::{Duration, Instant};

use hub_client::bundle_config::{
    version_key, BundleConfigCache, CacheConfig, ConfigKey, StaticConfigSource, ValkeyVersionProbe,
    VersionProbe,
};

/// A throwaway Valkey container, force-removed on drop.
struct Valkey {
    name: String,
    port: u16,
}

impl Valkey {
    fn start() -> Self {
        let port = TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let name = format!("waddles-hubclient-valkey-{}-{port}", std::process::id());
        let out = Command::new("docker")
            .args(["run", "-d", "--name", &name, "-p"])
            .arg(format!("127.0.0.1:{port}:6379"))
            .arg("valkey/valkey:8")
            .output()
            .expect("docker CLI must be available (a skip would be a gate failure)");
        assert!(
            out.status.success(),
            "docker run valkey failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        Self { name, port }
    }

    async fn connect(&self) -> redis::aio::MultiplexedConnection {
        let client = redis::Client::open(format!("redis://127.0.0.1:{}/", self.port)).unwrap();
        let deadline = Instant::now() + Duration::from_secs(180);
        loop {
            if let Ok(mut conn) = client.get_multiplexed_async_connection().await {
                let pong: redis::RedisResult<String> =
                    redis::cmd("PING").query_async(&mut conn).await;
                if pong.is_ok() {
                    return conn;
                }
            }
            assert!(Instant::now() < deadline, "valkey never became ready");
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    }
}

impl Drop for Valkey {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "-f", &self.name])
            .output();
    }
}

#[tokio::test]
async fn version_key_set_makes_cache_reload_against_real_valkey() {
    let valkey = Valkey::start();
    let mut conn = valkey.connect().await;
    let key = ConfigKey::new(7, 42, "waddles.socials.demo.default");

    // Wire format must match hub-api's `version_key()` (python: waddles:bundle_config:v:T:C:APP).
    assert_eq!(
        version_key(&key),
        "waddles:bundle_config:v:7:42:waddles.socials.demo.default"
    );

    let source = Arc::new(StaticConfigSource::new());
    source.set(key.clone(), 1, r#"{"greeting":"v1"}"#);
    let probe = Arc::new(ValkeyVersionProbe::new(valkey.connect().await));
    let cache = BundleConfigCache::new(
        source.clone(),
        Some(probe.clone() as Arc<dyn VersionProbe>),
        CacheConfig {
            ttl: Duration::from_secs(300),
            recheck: Duration::from_millis(50),
        },
    );

    // Nothing announced yet: probe reads None.
    assert_eq!(probe.latest(&key).await, None);
    assert_eq!(&*cache.lookup(&key).await, r#"{"greeting":"v1"}"#);

    // The source moves to v2 but nothing was announced: cache keeps v1 (no needless reload).
    source.set(key.clone(), 2, r#"{"greeting":"v2"}"#);
    tokio::time::sleep(Duration::from_millis(120)).await;
    assert_eq!(&*cache.lookup(&key).await, r#"{"greeting":"v1"}"#);

    // hub-api announces version 2 exactly as `publish_version` does (SET key "2" EX ...).
    let _: () = redis::cmd("SET")
        .arg(version_key(&key))
        .arg("2")
        .arg("EX")
        .arg(60)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(probe.latest(&key).await, Some(2));
    tokio::time::sleep(Duration::from_millis(120)).await;
    assert_eq!(&*cache.lookup(&key).await, r#"{"greeting":"v2"}"#);

    // Garbage in the key is ignored (stays on the cached value, never panics).
    let _: () = redis::cmd("SET")
        .arg(version_key(&key))
        .arg("not-a-number")
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(probe.latest(&key).await, None);
    tokio::time::sleep(Duration::from_millis(120)).await;
    assert_eq!(&*cache.lookup(&key).await, r#"{"greeting":"v2"}"#);
}
