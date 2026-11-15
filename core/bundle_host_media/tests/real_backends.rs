//! Real-path tests: Valkey job store + SeaweedFS (S3) object store through the
//! real `MediaHost`, with only the Gemini API faked. Requires the `docker` CLI
//! (containers are unique-named, loopback-published and force-removed on drop).
#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use bundle_host_media::host::{MediaConfig, MediaHost, SystemClock};
use bundle_host_media::jobs::{JobRecord, JobStore, RecState, ValkeyJobStore};
use bundle_host_media::store::{MediaStore, ObjectStoreMediaStore};
use bundle_host_media::{JobState, MediaError, MediaKind, Tier};
use common::*;

#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::{start_container, DockerContainer};

const VALKEY_IMAGE: &str = "valkey/valkey:8-alpine";
const SEAWEED_IMAGE: &str =
    "chrislusf/seaweedfs@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d";

struct Seaweed {
    name: String,
    port: u16,
}
impl Drop for Seaweed {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "-f", "-v", &self.name])
            .output();
    }
}

async fn start_seaweed() -> Seaweed {
    let name = format!("media-sw-{}-{}", std::process::id(), uuid_like());
    let out = Command::new("docker")
        .args([
            "run",
            "-d",
            "--name",
            &name,
            "-p",
            "127.0.0.1::8333",
            SEAWEED_IMAGE,
            "server",
            "-s3",
            "-dir=/data",
            "-volume.max=2",
        ])
        .output()
        .expect("docker runs");
    assert!(
        out.status.success(),
        "seaweed start: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let mut sw = Seaweed {
        name: name.clone(),
        port: 0,
    };
    let port_out = Command::new("docker")
        .args(["port", &name, "8333/tcp"])
        .output()
        .unwrap();
    let port: u16 = String::from_utf8_lossy(&port_out.stdout)
        .lines()
        .find_map(|l| l.rsplit(':').next()?.trim().parse().ok())
        .expect("port");
    sw.port = port;
    // Ready when the S3 gateway accepts a bucket create.
    let http = reqwest::Client::new();
    for _ in 0..240 {
        if let Ok(r) = http
            .put(format!("http://127.0.0.1:{port}/media-bucket"))
            .send()
            .await
        {
            if r.status().is_success() || r.status().as_u16() == 409 {
                return sw;
            }
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    panic!("seaweedfs not ready");
}

fn uuid_like() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

async fn valkey() -> (DockerContainer, redis::aio::ConnectionManager) {
    let c = start_container(
        VALKEY_IMAGE,
        6379,
        &[],
        &["valkey-cli", "ping"],
        Duration::from_secs(120),
    )
    .await;
    let conn = redis::Client::open(format!("redis://{}:{}/", c.host(), c.port()))
        .unwrap()
        .get_connection_manager()
        .await
        .unwrap();
    (c, conn)
}

fn s3(sw: &Seaweed) -> ObjectStoreMediaStore {
    ObjectStoreMediaStore::new(
        &format!("http://127.0.0.1:{}", sw.port),
        "media-bucket",
        "us-east-1",
        "any",
        "any",
    )
    .unwrap()
}

#[tokio::test]
async fn valkey_job_store_scopes_keys_limits_rate_and_inflight() {
    let (_c, conn) = valkey().await;
    let store = ValkeyJobStore::new(conn.clone());
    let a = scope("app-a");
    let mut other_comm = a.clone();
    other_comm.community_id = 43;
    let rec = JobRecord {
        app_id: "app-a".into(),
        kind: MediaKind::Video,
        deadline_ms: 5,
        state: RecState::Pending,
    };
    let id = "a".repeat(32);
    store.put(&a, &id, &rec, 60).await.unwrap();
    assert_eq!(store.get(&a, &id).await.unwrap(), Some(rec.clone()));
    assert_eq!(
        store.get(&other_comm, &id).await.unwrap(),
        None,
        "community-scoped keys"
    );
    let failed = JobRecord {
        state: RecState::Failed {
            code: "timeout".into(),
            detail: String::new(),
        },
        ..rec.clone()
    };
    store.put(&a, &id, &failed, 60).await.unwrap();
    assert_eq!(store.get(&a, &id).await.unwrap(), Some(failed));
    // TTL is set.
    let mut c = conn.clone();
    let ttl: i64 = redis::cmd("TTL")
        .arg(format!("media:job:7:42:{id}"))
        .query_async(&mut c)
        .await
        .unwrap();
    assert!((1..=60).contains(&ttl), "ttl={ttl}");
    // Rate: 6 per minute window.
    for _ in 0..6 {
        assert!(store.acquire_rate(&a, 6, 120_000).await.unwrap());
    }
    assert!(!store.acquire_rate(&a, 6, 120_000).await.unwrap());
    assert!(
        store.acquire_rate(&a, 6, 180_000).await.unwrap(),
        "next window"
    );
    assert!(
        store.acquire_rate(&other_comm, 6, 120_000).await.unwrap(),
        "other community"
    );
    // In-flight cap + release + expired-slot pruning.
    assert!(store.inflight_start(&a, "j1", 1_000, 0, 2).await.unwrap());
    assert!(store
        .inflight_start(&a, "j2", 9_999_999, 0, 2)
        .await
        .unwrap());
    assert!(!store
        .inflight_start(&a, "j3", 9_999_999, 0, 2)
        .await
        .unwrap());
    store.inflight_end(&a, "j2").await.unwrap();
    assert!(store
        .inflight_start(&a, "j3", 9_999_999, 0, 2)
        .await
        .unwrap());
    assert!(
        store
            .inflight_start(&a, "j4", 9_999_999, 5_000, 2)
            .await
            .unwrap(),
        "j1's deadline passed -> pruned"
    );
}

#[tokio::test]
async fn seaweedfs_store_tags_owner_and_presigns() {
    let sw = start_seaweed().await;
    let store = s3(&sw);
    let key = "media-harness/7/42/".to_string() + &"b".repeat(32) + ".png";
    store
        .put(&key, bytes::Bytes::from(png()), "image/png", "app-a")
        .await
        .unwrap();
    assert_eq!(store.owner(&key).await.unwrap().as_deref(), Some("app-a"));
    assert_eq!(
        store.owner("media-harness/7/42/missing.png").await.unwrap(),
        None
    );
    let url = store
        .presign_get(&key, Duration::from_secs(60))
        .await
        .unwrap();
    let resp = reqwest::get(&url).await.unwrap();
    assert_eq!(resp.status().as_u16(), 200);
    assert_eq!(resp.headers().get("content-type").unwrap(), "image/png");
    assert_eq!(resp.bytes().await.unwrap().to_vec(), png());
}

#[tokio::test]
async fn end_to_end_image_and_video_job_over_valkey_and_seaweedfs() {
    let (_c, conn) = valkey().await;
    let sw = start_seaweed().await;
    let (fake, base) = fake_gemini().await;
    fake.pending_polls
        .store(2, std::sync::atomic::Ordering::SeqCst);
    let host = Arc::new(MediaHost::new(
        Arc::new(FixedTier(Ok(Tier::Professional))),
        Arc::new(FixedCreds(Some(GOOD_KEY))),
        Arc::new(provider(base, Duration::from_secs(10))),
        Arc::new(s3(&sw)) as Arc<dyn MediaStore>,
        Arc::new(ValkeyJobStore::new(conn)),
        Arc::new(SystemClock),
        MediaConfig {
            poll_interval: Duration::from_millis(20),
            ..MediaConfig::default()
        },
    ));
    let sc = scope("app-a");
    let img = host.generate_image(&sc, "a cat").await.unwrap();
    let url = host.object_url(&sc, &img).await.unwrap();
    assert_eq!(
        reqwest::get(&url)
            .await
            .unwrap()
            .bytes()
            .await
            .unwrap()
            .to_vec(),
        png()
    );

    let id = host.submit(&sc, MediaKind::Video, "a clip").await.unwrap();
    let JobState::Done(h) = wait_done(&host, &sc, &id).await else {
        panic!("not done")
    };
    let vurl = host.object_url(&sc, &h).await.unwrap();
    assert_eq!(
        reqwest::get(&vurl)
            .await
            .unwrap()
            .bytes()
            .await
            .unwrap()
            .to_vec(),
        mp4()
    );

    // Another bundle in the same community cannot resolve either output.
    let other = scope("app-b");
    assert_eq!(
        host.object_url(&other, &img).await.unwrap_err(),
        MediaError::NotFound
    );
    assert_eq!(
        host.object_url(&other, &h).await.unwrap_err(),
        MediaError::NotFound
    );
    assert_eq!(
        host.job_status(&other, &id).await.unwrap_err(),
        MediaError::NotFound
    );
}
