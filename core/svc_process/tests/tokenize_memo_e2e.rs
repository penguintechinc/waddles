//! Real-path acceptance test for the once-per-message tokenization memo
//! (`svc_process::tokenize_memo`).
//!
//! Nothing but hub-api itself is faked: the production `HubClientMinter` /
//! `hub_client::HubClient` talk real gRPC (HTTP/2 on loopback, machine-JWT
//! bearer from an in-process `ServiceAuthService`) to an in-process tonic
//! `IdentityService` that counts every `MintEphemeralPseudonyms` call. The
//! scenario is the alpha 2026-11-13 incident: ~160 per-app consumers read the
//! SAME Discord message; before the memo that was ~160 mint RPCs, now exactly 1.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use hub_client::pb::identity_service_server::{IdentityService, IdentityServiceServer};
use hub_client::pb::{
    EphemeralPseudonym, MintEphemeralPseudonymsRequest, MintEphemeralPseudonymsResponse,
    ResolveDisplayNamesRequest, ResolveDisplayNamesResponse, ResolveHandleRequest,
    ResolveHandleResponse, ResolvePlatformUserRequest, ResolvePlatformUserResponse,
};
use penguin_spine::PlatformEvent;
use service_auth::test_support::{MintReply, MockHub};
use service_auth::MachineJwtClient;
use svc_process::pii_tokenize::{HubClientMinter, TokenizeError};
use svc_process::stage_metrics::StageMetrics;
use svc_process::tokenize_memo::{MemoKey, TokenizeMemo};
use tonic::{Request, Response, Status};

/// Fake hub-api `IdentityService`: counts mint calls, records the tenants it was
/// asked for, mints a deterministic pseudonym per `(tenant, platform_user_id)`
/// (mint-or-return, exactly like hub-api) and can be switched to fail.
#[derive(Clone)]
struct FakeIdentity {
    calls: Arc<AtomicUsize>,
    tenants: Arc<Mutex<Vec<String>>>,
    fail: Arc<Mutex<Option<tonic::Code>>>,
    delay: Duration,
}

impl FakeIdentity {
    fn new(delay_ms: u64) -> Self {
        Self {
            calls: Arc::new(AtomicUsize::new(0)),
            tenants: Arc::new(Mutex::new(Vec::new())),
            fail: Arc::new(Mutex::new(None)),
            delay: Duration::from_millis(delay_ms),
        }
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }

    /// Serves on a loopback port and returns the `http://` endpoint.
    async fn serve(&self) -> String {
        let probe = std::net::TcpListener::bind("127.0.0.1:0").expect("bind probe");
        let addr: SocketAddr = probe.local_addr().expect("addr");
        drop(probe);
        let svc = self.clone();
        tokio::spawn(async move {
            let _ = tonic::transport::Server::builder()
                .add_service(IdentityServiceServer::new(svc))
                .serve(addr)
                .await;
        });
        tokio::time::sleep(Duration::from_millis(150)).await;
        format!("http://{addr}")
    }
}

#[tonic::async_trait]
impl IdentityService for FakeIdentity {
    async fn mint_ephemeral_pseudonyms(
        &self,
        request: Request<MintEphemeralPseudonymsRequest>,
    ) -> Result<Response<MintEphemeralPseudonymsResponse>, Status> {
        assert!(
            request.metadata().get("authorization").is_some(),
            "every mint carries the machine JWT"
        );
        self.calls.fetch_add(1, Ordering::SeqCst);
        tokio::time::sleep(self.delay).await;
        if let Some(code) = *self.fail.lock().unwrap() {
            return Err(Status::new(code, "simulated hub-api failure"));
        }
        let items = request.into_inner().items;
        let mut out = Vec::new();
        for i in items {
            self.tenants.lock().unwrap().push(i.tenant_id.clone());
            let pseudonym = format!(
                "00000000-0000-4000-8000-{:012x}",
                i.tenant_id.len() * 1000 + i.platform_user_id.len()
            );
            let token = format!("{}-{}", i.tenant_id, pseudonym);
            out.push(EphemeralPseudonym {
                platform_user_id: i.platform_user_id,
                pseudonym: token,
            });
        }
        Ok(Response::new(MintEphemeralPseudonymsResponse {
            pseudonyms: out,
        }))
    }

    async fn resolve_display_names(
        &self,
        _: Request<ResolveDisplayNamesRequest>,
    ) -> Result<Response<ResolveDisplayNamesResponse>, Status> {
        Err(Status::unimplemented("not used"))
    }

    async fn resolve_handle(
        &self,
        _: Request<ResolveHandleRequest>,
    ) -> Result<Response<ResolveHandleResponse>, Status> {
        Err(Status::unimplemented("not used"))
    }

    async fn resolve_platform_user(
        &self,
        _: Request<ResolvePlatformUserRequest>,
    ) -> Result<Response<ResolvePlatformUserResponse>, Status> {
        Err(Status::unimplemented("not used"))
    }
}

/// The production minter wired to the fake hub through a real `HubClient`.
async fn real_minter(fake: &FakeIdentity, tag: &str) -> (Arc<HubClientMinter>, MockHub) {
    let mint = MockHub::builder()
        .mint(vec![MintReply::token("e2e-machine-jwt", 3600)])
        .start()
        .await;
    let sa = std::env::temp_dir().join(format!(
        "svc-process-memo-e2e-sa-{tag}-{}",
        std::process::id()
    ));
    std::fs::write(&sa, "projected-sa-token").expect("write sa token");
    let tokens = Arc::new(MachineJwtClient::new(
        mint.endpoint.clone(),
        sa.to_string_lossy().to_string(),
        "identity:ephemeral:mint",
    ));
    let channel = hub_client::lazy_channel(&fake.serve().await, None).expect("channel");
    let client = hub_client::HubClient::with_channel(channel, tokens);
    (Arc::new(HubClientMinter(Arc::new(client))), mint)
}

fn memo(ttl_ms: u64, max: usize) -> Arc<TokenizeMemo> {
    // A private, no-exporter meter provider: this test asserts on the hub-api
    // call count, not on metrics (those are covered by the crate unit tests).
    let meter = opentelemetry::global::meter("tokenize-memo-e2e");
    Arc::new(TokenizeMemo::new(
        Duration::from_millis(ttl_ms),
        max,
        Arc::new(StageMetrics::new(&meter)),
    ))
}

fn discord_event(user_id: &str, handle: &str, text: &str) -> PlatformEvent {
    let payload = serde_json::json!({ "user_id": user_id, "text": text });
    PlatformEvent {
        platform: "discord".to_string(),
        event_type: "chat.message".to_string(),
        actor: Some(handle.to_string()),
        payload: payload.as_object().cloned().unwrap_or_default(),
        occurred_at: "2026-11-13T00:00:00.000Z".to_string(),
        source: None,
    }
}

fn key(tenant: &str, entry: &str, ev: &PlatformEvent) -> MemoKey {
    MemoKey::new(
        tenant,
        Some(7),
        Some(3),
        &format!("waddles:t:{tenant}:c:main:src:discord:guild-A:events"),
        entry,
        "evt-1",
        ev,
    )
    .expect("key")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn one_discord_message_160_per_app_consumers_one_hub_api_mint() {
    let fake = FakeIdentity::new(200);
    let (minter, _hub) = real_minter(&fake, "n160").await;
    let memo = memo(60_000, 1024);
    let ev = discord_event("111", "alice", "!ping <@222>");
    let k = key("acme", "1700000000000-0", &ev);

    let mut tasks = Vec::new();
    for _ in 0..160 {
        let (memo, minter, ev, k) = (
            Arc::clone(&memo),
            Arc::clone(&minter),
            ev.clone(),
            k.clone(),
        );
        tasks.push(tokio::spawn(async move {
            memo.tokenize(k, &ev, "acme", minter.as_ref()).await
        }));
    }
    let mut results = Vec::new();
    for t in tasks {
        results.push(t.await.expect("task").expect("tokenized"));
    }

    assert_eq!(results.len(), 160, "examined 160 consumers");
    assert_eq!(fake.calls(), 1, "exactly one hub-api mint call");
    let first = &results[0];
    assert!(
        results
            .iter()
            .all(|r| r.event == first.event && r.mentions == first.mentions),
        "every consumer received the identical tokenized envelope"
    );
    let rendered = serde_json::to_string(&first.event).expect("json");
    assert!(
        !rendered.contains("alice") && !rendered.contains("<@222>"),
        "no raw PII reaches a bundle: {rendered}"
    );
    assert_eq!(
        fake.tenants.lock().unwrap().len(),
        2,
        "the single call carried both identities (actor + mention)"
    );
}

#[tokio::test]
async fn two_messages_two_calls_and_two_tenants_two_calls() {
    let fake = FakeIdentity::new(0);
    let (minter, _hub) = real_minter(&fake, "distinct").await;
    let memo = memo(60_000, 1024);
    let ev = discord_event("111", "alice", "hi");

    for entry in ["1-0", "2-0"] {
        memo.tokenize(key("acme", entry, &ev), &ev, "acme", minter.as_ref())
            .await
            .expect("ok");
    }
    assert_eq!(fake.calls(), 2, "two different messages -> two calls");

    // Same stream entry id + same content, different tenant: never shared.
    let a = memo
        .tokenize(
            key("tenant-a", "9-0", &ev),
            &ev,
            "tenant-a",
            minter.as_ref(),
        )
        .await
        .expect("ok");
    let b = memo
        .tokenize(
            key("tenant-b", "9-0", &ev),
            &ev,
            "tenant-b",
            minter.as_ref(),
        )
        .await
        .expect("ok");
    assert_eq!(
        fake.calls(),
        4,
        "same message id, two tenants -> separate calls"
    );
    assert_ne!(
        a.event.actor, b.event.actor,
        "each tenant got its own pseudonym"
    );
    assert_eq!(
        fake.tenants.lock().unwrap()[2..],
        ["tenant-a".to_string(), "tenant-b".to_string()],
        "each mint was made for its own tenant"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn hub_failure_dead_letters_all_waiters_once_then_next_message_retries() {
    let fake = FakeIdentity::new(200);
    *fake.fail.lock().unwrap() = Some(tonic::Code::ResourceExhausted);
    let (minter, _hub) = real_minter(&fake, "fail").await;
    let memo = memo(60_000, 1024);
    let ev = discord_event("111", "alice", "!ping");
    let k = key("acme", "5-0", &ev);

    let mut tasks = Vec::new();
    for _ in 0..160 {
        let (memo, minter, ev, k) = (
            Arc::clone(&memo),
            Arc::clone(&minter),
            ev.clone(),
            k.clone(),
        );
        tasks.push(tokio::spawn(async move {
            memo.tokenize(k, &ev, "acme", minter.as_ref()).await
        }));
    }
    let mut failed = 0;
    for t in tasks {
        match t.await.expect("task") {
            Err(TokenizeError::ResolutionUnavailable(_)) => failed += 1,
            Ok(t) => panic!("raw/untokenized event must never be returned on failure: {t:?}"),
            Err(other) => panic!("unexpected error {other:?}"),
        }
    }
    assert_eq!(
        failed, 160,
        "examined 160 consumers, every one failed closed"
    );
    assert_eq!(
        fake.calls(),
        1,
        "the failure cost one hub-api call (no retry stampede)"
    );
    assert!(memo.is_empty(), "the failure was not cached");

    // hub-api recovers; the next delivery (same entry redelivered, and a new one) retries normally.
    *fake.fail.lock().unwrap() = None;
    memo.tokenize(k.clone(), &ev, "acme", minter.as_ref())
        .await
        .expect("redelivery retried and succeeded");
    assert_eq!(fake.calls(), 2);
    let next = discord_event("111", "alice", "!ping again");
    memo.tokenize(key("acme", "6-0", &next), &next, "acme", minter.as_ref())
        .await
        .expect("next message ok");
    assert_eq!(fake.calls(), 3);
}

#[tokio::test]
async fn memo_is_bounded_by_ttl_and_entry_cap_against_the_real_client() {
    let fake = FakeIdentity::new(0);
    let (minter, _hub) = real_minter(&fake, "bounds").await;
    let ev = discord_event("111", "alice", "hi");

    // TTL: reused inside the window, re-minted after it.
    let ttl_memo = memo(80, 1024);
    let k = key("acme", "1-0", &ev);
    ttl_memo
        .tokenize(k.clone(), &ev, "acme", minter.as_ref())
        .await
        .expect("ok");
    ttl_memo
        .tokenize(k.clone(), &ev, "acme", minter.as_ref())
        .await
        .expect("ok");
    assert_eq!(fake.calls(), 1);
    tokio::time::sleep(Duration::from_millis(160)).await;
    ttl_memo
        .tokenize(k, &ev, "acme", minter.as_ref())
        .await
        .expect("ok");
    assert_eq!(fake.calls(), 2, "an expired entry is tokenized afresh");

    // Cap: never more than `max` entries however many distinct messages arrive.
    let capped = memo(60_000, 16);
    for i in 0..60 {
        capped
            .tokenize(
                key("acme", &format!("{i}-0"), &ev),
                &ev,
                "acme",
                minter.as_ref(),
            )
            .await
            .expect("ok");
        assert!(capped.len() <= 16, "len {} exceeded the cap", capped.len());
    }
}
