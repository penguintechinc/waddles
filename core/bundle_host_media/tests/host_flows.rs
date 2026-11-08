//! Host orchestration against a fake Gemini API: tier x BYOK matrix, adapter
//! request shapes, long-running polling, storage scoping, rate/in-flight limits,
//! typed provider failures, output validation.
#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use std::sync::atomic::Ordering;
use std::time::Duration;

use bundle_host_media::tier::TierError;
use bundle_host_media::{JobState, MediaConfig, MediaError, MediaKind, Tier};
use common::*;

#[tokio::test]
async fn tier_matrix_for_image_generation() {
    for (tier, want_ok, want_err) in [
        (
            Ok(Tier::Free),
            false,
            Some(MediaError::TierRequired("professional".into())),
        ),
        (Ok(Tier::Professional), true, None),
        (Ok(Tier::Enterprise), true, None),
        (
            Err(TierError::Unknown),
            false,
            Some(MediaError::TierRequired("license_unknown".into())),
        ),
        (
            Err(TierError::Unavailable("down".into())),
            false,
            Some(MediaError::TierRequired("license_unknown".into())),
        ),
    ] {
        let label = format!("{tier:?}");
        let r = rig(tier, Some(GOOD_KEY), fast_cfg()).await;
        let res = r.host.generate_image(&scope("app-a"), "a cat").await;
        assert_eq!(res.is_ok(), want_ok, "{label}: {res:?}");
        if let Some(e) = want_err {
            assert_eq!(res.unwrap_err(), e, "{label}");
            // A refused tier must never reach the provider.
            assert!(
                r.fake.posts.lock().unwrap().is_empty(),
                "{label} leaked a provider call"
            );
        }
    }
}

#[tokio::test]
async fn byok_missing_is_typed_and_present_works_and_wrong_key_is_not_missing() {
    let r = rig(Ok(Tier::Professional), None, fast_cfg()).await;
    assert_eq!(
        r.host.generate_image(&scope("a"), "cat").await.unwrap_err(),
        MediaError::ByokMissing("gemini".into())
    );
    assert!(r.fake.posts.lock().unwrap().is_empty());

    let r = rig(Ok(Tier::Professional), Some("k-wrong"), fast_cfg()).await;
    assert!(
        matches!(r.host.generate_image(&scope("a"), "cat").await.unwrap_err(), MediaError::Backend(m) if m == "provider_auth_rejected")
    );
}

#[tokio::test]
async fn image_request_shape_headers_and_stored_output() {
    let r = rig(Ok(Tier::Professional), Some(GOOD_KEY), fast_cfg()).await;
    let handle = r
        .host
        .generate_image(&scope("app-a"), "a cat")
        .await
        .unwrap();
    assert!(
        handle.starts_with("media://7/42/") && handle.ends_with(".png"),
        "{handle}"
    );
    let posts = r.fake.posts.lock().unwrap();
    assert_eq!(posts.len(), 1);
    let (body, key, rev) = &posts[0];
    assert_eq!(key, GOOD_KEY);
    assert_eq!(rev, "2026-05-20");
    assert_eq!(body["model"], "gemini-nano-banana-2.1");
    assert_eq!(
        body["input"][0],
        serde_json::json!({"type":"text","text":"a cat"})
    );
    // Stored under the media-harness prefix, tagged for the requesting bundle.
    let key = format!("media-harness/7/42/{}", handle.rsplit('/').next().unwrap());
    let (bytes, ct, owner) = r.store.get(&key).expect("object stored");
    assert_eq!(bytes.to_vec(), png());
    assert_eq!(ct, "image/png");
    assert_eq!(owner, "app-a");
}

#[tokio::test]
async fn video_and_music_are_long_running_jobs_polled_to_completion() {
    for (kind, ext, model) in [
        (MediaKind::Video, ".mp4", "gemini-omni-1.2-flash"),
        (MediaKind::Music, ".mp3", "lyria-3.5"),
    ] {
        let r = rig(Ok(Tier::Enterprise), Some(GOOD_KEY), fast_cfg()).await;
        r.fake.pending_polls.store(3, Ordering::SeqCst);
        let sc = scope("app-a");
        let id = r.host.submit(&sc, kind, "a clip").await.unwrap();
        assert_eq!(
            r.host.job_status(&sc, &id).await.unwrap(),
            JobState::Pending
        );
        let done = wait_done(&r.host, &sc, &id).await;
        match done {
            JobState::Done(h) => assert!(h.ends_with(ext), "{h}"),
            other => panic!("{other:?}"),
        }
        let posts = r.fake.posts.lock().unwrap();
        assert_eq!(posts[0].0["model"], model);
        assert_eq!(posts[0].0["background"], true);
        // The BYOK key is sent on every poll too, to the operation path only.
        let gets = r.fake.gets.lock().unwrap();
        assert!(
            gets.len() >= 4,
            "expected >=3 pending polls then done, got {}",
            gets.len()
        );
        assert!(gets.iter().all(|(_, k)| k == GOOD_KEY));
    }
}

#[tokio::test]
async fn image_through_submit_is_invalid_and_bad_prompts_are_refused() {
    let r = rig(Ok(Tier::Professional), Some(GOOD_KEY), fast_cfg()).await;
    let sc = scope("a");
    assert!(matches!(
        r.host.submit(&sc, MediaKind::Image, "x").await,
        Err(MediaError::Invalid(_))
    ));
    assert!(matches!(
        r.host.generate_image(&sc, "   ").await,
        Err(MediaError::Invalid(_))
    ));
    assert!(matches!(
        r.host.generate_image(&sc, &"x".repeat(2001)).await,
        Err(MediaError::Invalid(_))
    ));
    assert!(matches!(
        r.host.generate_image(&sc, "bad\u{0}char").await,
        Err(MediaError::Invalid(_))
    ));
}

#[tokio::test]
async fn jobs_and_handles_are_scoped_to_bundle_and_community() {
    let r = rig(Ok(Tier::Professional), Some(GOOD_KEY), fast_cfg()).await;
    let owner = scope("app-owner");
    let id = r
        .host
        .submit(&owner, MediaKind::Video, "clip")
        .await
        .unwrap();
    let JobState::Done(handle) = wait_done(&r.host, &owner, &id).await else {
        panic!("not done")
    };

    // Another bundle in the SAME community: job and object both look absent, even
    // though it would hold a media-prefix grant -- the tag is what authorizes.
    let other_app = scope("app-other");
    assert_eq!(
        r.host.job_status(&other_app, &id).await.unwrap_err(),
        MediaError::NotFound
    );
    assert_eq!(
        r.host.object_url(&other_app, &handle).await.unwrap_err(),
        MediaError::NotFound
    );
    // Same bundle id, different community / tenant.
    let mut other_comm = owner.clone();
    other_comm.community_id = 43;
    assert_eq!(
        r.host.job_status(&other_comm, &id).await.unwrap_err(),
        MediaError::NotFound
    );
    assert_eq!(
        r.host.object_url(&other_comm, &handle).await.unwrap_err(),
        MediaError::NotFound
    );
    let mut other_tenant = owner.clone();
    other_tenant.tenant_id = 8;
    assert_eq!(
        r.host.object_url(&other_tenant, &handle).await.unwrap_err(),
        MediaError::NotFound
    );
    // The owner resolves it, with a short TTL.
    let url = r.host.object_url(&owner, &handle).await.unwrap();
    assert!(
        url.contains("media-harness/7/42/") && url.ends_with("ttl=300"),
        "{url}"
    );
    // Forged / malformed / traversal handles.
    for bad in [
        "media://7/42/../../x.png",
        "media://7/42/zz.png",
        "http://evil/x",
        "media://7/42/00000000000000000000000000000000.exe",
        "media://07/42/00000000000000000000000000000000.png",
    ] {
        assert_eq!(
            r.host.object_url(&owner, bad).await.unwrap_err(),
            MediaError::NotFound,
            "{bad}"
        );
    }
    // A well-formed handle for a nonexistent object is NotFound too.
    let ghost = format!("media://7/42/{}.png", "a".repeat(32));
    assert_eq!(
        r.host.object_url(&owner, &ghost).await.unwrap_err(),
        MediaError::NotFound
    );
    assert_eq!(
        r.host.job_status(&owner, "not-an-id").await.unwrap_err(),
        MediaError::NotFound
    );
}

#[tokio::test]
async fn per_community_rate_limit_is_six_per_minute_and_resets_next_window() {
    let r = rig(Ok(Tier::Professional), Some(GOOD_KEY), fast_cfg()).await;
    let sc = scope("a");
    for _ in 0..6 {
        r.host.generate_image(&sc, "cat").await.unwrap();
    }
    assert_eq!(
        r.host.generate_image(&sc, "cat").await.unwrap_err(),
        MediaError::Quota("minute".into())
    );
    // Another community has its own budget.
    let mut other = sc.clone();
    other.community_id = 99;
    r.host.generate_image(&other, "cat").await.unwrap();
    r.clock.0.fetch_add(61_000, Ordering::SeqCst);
    r.host.generate_image(&sc, "cat").await.unwrap();
}

#[tokio::test]
async fn inflight_cap_bounds_concurrent_jobs_per_community() {
    let mut cfg = fast_cfg();
    cfg.max_inflight = 2;
    cfg.rate_per_minute = 100;
    let r = rig(Ok(Tier::Professional), Some(GOOD_KEY), cfg).await;
    r.fake.pending_polls.store(1_000_000, Ordering::SeqCst);
    let sc = scope("a");
    r.host.submit(&sc, MediaKind::Video, "1").await.unwrap();
    r.host.submit(&sc, MediaKind::Video, "2").await.unwrap();
    assert_eq!(
        r.host.submit(&sc, MediaKind::Video, "3").await.unwrap_err(),
        MediaError::Quota("inflight".into())
    );
}

#[tokio::test]
async fn provider_failures_map_to_the_typed_vocabulary() {
    let r = rig(
        Ok(Tier::Professional),
        Some(GOOD_KEY),
        MediaConfig {
            rate_per_minute: 100,
            ..fast_cfg()
        },
    )
    .await;
    let sc = scope("a");
    assert_eq!(
        r.host.generate_image(&sc, "BLOCKME").await.unwrap_err(),
        MediaError::ProviderRefused("safety".into())
    );
    assert_eq!(
        r.host.generate_image(&sc, "QUOTA").await.unwrap_err(),
        MediaError::Quota("provider".into())
    );
    assert_eq!(
        r.host.generate_image(&sc, "SILENT").await.unwrap_err(),
        MediaError::ProviderRefused("no_output".into())
    );
    // Same through the job path: submit succeeds, the failure is persisted and typed.
    let id = r
        .host
        .submit(&sc, MediaKind::Video, "BLOCKME")
        .await
        .unwrap();
    assert_eq!(
        wait_done(&r.host, &sc, &id).await,
        JobState::Failed(MediaError::ProviderRefused("safety".into()))
    );
}

#[tokio::test]
async fn image_timeout_is_typed_and_slow_provider_is_not_awaited_forever() {
    let mut cfg = fast_cfg();
    cfg.image_timeout = Duration::from_millis(100);
    let r = rig(Ok(Tier::Professional), Some(GOOD_KEY), cfg).await;
    r.fake.image_delay_ms.store(3000, Ordering::SeqCst);
    let t = std::time::Instant::now();
    assert_eq!(
        r.host.generate_image(&scope("a"), "cat").await.unwrap_err(),
        MediaError::Timeout
    );
    assert!(t.elapsed() < Duration::from_secs(2));
    assert!(r.store.is_empty());
}

#[tokio::test]
async fn job_timeout_is_persisted_as_timeout() {
    let mut cfg = fast_cfg();
    cfg.video_timeout = Duration::from_millis(150);
    let r = rig(Ok(Tier::Professional), Some(GOOD_KEY), cfg).await;
    r.fake.pending_polls.store(1_000_000, Ordering::SeqCst);
    let sc = scope("a");
    let id = r.host.submit(&sc, MediaKind::Video, "slow").await.unwrap();
    assert_eq!(
        wait_done(&r.host, &sc, &id).await,
        JobState::Failed(MediaError::Timeout)
    );
}

#[tokio::test]
async fn stale_pending_job_reads_as_timeout_after_its_deadline() {
    // A worker that died (restart) leaves `pending`; the read side times it out.
    let r = rig(
        Ok(Tier::Professional),
        Some(GOOD_KEY),
        MediaConfig {
            poll_interval: Duration::from_secs(3600),
            ..fast_cfg()
        },
    )
    .await;
    r.fake.pending_polls.store(1_000_000, Ordering::SeqCst);
    let sc = scope("a");
    let id = r.host.submit(&sc, MediaKind::Video, "x").await.unwrap();
    assert_eq!(
        r.host.job_status(&sc, &id).await.unwrap(),
        JobState::Pending
    );
    r.clock.0.fetch_add(601_000, Ordering::SeqCst);
    assert_eq!(
        r.host.job_status(&sc, &id).await.unwrap(),
        JobState::Failed(MediaError::Timeout)
    );
}

#[tokio::test]
async fn non_image_bytes_from_the_provider_are_rejected_not_stored() {
    let r = rig(Ok(Tier::Professional), Some(GOOD_KEY), fast_cfg()).await;
    *r.fake.bogus_image_bytes.lock().unwrap() = true;
    assert!(
        matches!(r.host.generate_image(&scope("a"), "cat").await.unwrap_err(), MediaError::Backend(m) if m == "output_content_type")
    );
    assert!(r.store.is_empty());
}

#[tokio::test]
async fn tier_unknown_or_free_never_consumes_rate_budget_or_calls_provider() {
    let r = rig(Ok(Tier::Free), Some(GOOD_KEY), fast_cfg()).await;
    for _ in 0..20 {
        assert!(matches!(
            r.host.generate_image(&scope("a"), "cat").await,
            Err(MediaError::TierRequired(_))
        ));
    }
    assert!(r.fake.posts.lock().unwrap().is_empty());
}

#[tokio::test]
async fn pending_job_deadline_boundary_is_exclusive() {
    let r = rig(
        Ok(Tier::Professional),
        Some(GOOD_KEY),
        MediaConfig {
            poll_interval: Duration::from_secs(3600),
            ..fast_cfg()
        },
    )
    .await;
    r.fake.pending_polls.store(1_000_000, Ordering::SeqCst);
    let sc = scope("a");
    let id = r.host.submit(&sc, MediaKind::Video, "x").await.unwrap();
    // submit at t=1_000_000 with a 600 s video bound => deadline 1_600_000.
    r.clock.0.store(1_600_000, Ordering::SeqCst);
    assert_eq!(
        r.host.job_status(&sc, &id).await.unwrap(),
        JobState::Pending,
        "at the deadline it is still pending"
    );
    r.clock.0.store(1_600_001, Ordering::SeqCst);
    assert_eq!(
        r.host.job_status(&sc, &id).await.unwrap(),
        JobState::Failed(MediaError::Timeout)
    );
}

struct TierRequiredCreds;
#[async_trait::async_trait]
impl bundle_host_media::credentials::CredentialResolver for TierRequiredCreds {
    async fn resolve(
        &self,
        _s: &bundle_host_media::Scope,
        _p: &str,
    ) -> Result<
        bundle_host_media::credentials::ApiKey,
        bundle_host_media::credentials::CredentialError,
    > {
        Err(bundle_host_media::credentials::CredentialError::TierRequired)
    }
}

#[tokio::test]
async fn hub_tier_required_from_the_byok_resolver_maps_to_tier_required() {
    let (_f, base) = fake_gemini().await;
    let host = bundle_host_media::MediaHost::new(
        std::sync::Arc::new(FixedTier(Ok(Tier::Professional))),
        std::sync::Arc::new(TierRequiredCreds),
        std::sync::Arc::new(provider(base, Duration::from_secs(5))),
        std::sync::Arc::new(bundle_host_media::store::InMemoryStore::default()),
        std::sync::Arc::new(bundle_host_media::jobs::InMemoryJobStore::default()),
        std::sync::Arc::new(bundle_host_media::SystemClock),
        fast_cfg(),
    );
    assert_eq!(
        host.generate_image(&scope("a"), "cat").await.unwrap_err(),
        MediaError::TierRequired("professional".into())
    );
}
