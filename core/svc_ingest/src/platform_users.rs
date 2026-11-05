//! Production [`PlatformUserResolver`] (P6a T6d-2): community-scoped
//! detokenization of a user UUID to the RAW Discord user id through hub-api's
//! `IdentityService.ResolvePlatformUser` (`hub_client`).
//!
//! This is the ONE place the executor turns a tokenized identity back into a
//! platform id (security.md PII boundary): the call is authenticated with
//! svc-ingest's machine JWT carrying scope [`PLATFORM_USER_SCOPE`], names the
//! stamp's numeric tenant and the numeric community, and the answer is held in
//! a zeroizing [`SlotValue`] only -- never logged, never cached, resolved again
//! on every operation.
//!
//! **No oracle.** Hub-api answers ANY miss (unknown uuid, another community, a
//! non-member, a platform mismatch) with a single `NOT_FOUND` for the whole
//! batch. This resolver therefore sends exactly ONE RPC per call and never
//! retries per uuid to probe which one missed ([`UserResolveError::NotFound`]
//! is final). Failure classes stay distinct for the caller:
//!
//! | gRPC | [`UserResolveError`] | meaning |
//! |---|---|---|
//! | `NOT_FOUND` | `NotFound` | refused; not a member of this community |
//! | `PERMISSION_DENIED` / `UNAUTHENTICATED` / token bootstrap rejected | `Denied` | operator problem (scope/tenant), not retryable |
//! | `RESOURCE_EXHAUSTED` | `RateLimited` | back off and retry; never poisons the entry |
//! | `INVALID_ARGUMENT` / `FAILED_PRECONDITION` / client-side bound | `Invalid` | caller bug |
//! | anything else (transport, deadline, circuit open) | `Unavailable` | retryable |

use std::collections::HashMap;
use std::sync::{Arc, LazyLock};

use hub_client::{HubClient, HubClientError};
use service_auth::{MachineJwtClient, ServiceAuthError};

use crate::connector::sender_exec::discord_duties::{PlatformUserResolver, UserResolveError};
use crate::connector::sender_exec::SlotValue;

/// Machine-JWT scope hub-api requires for `ResolvePlatformUser`.
pub const PLATFORM_USER_SCOPE: &str = "identity:platform-user:resolve";
/// The only platform this resolver detokenizes for.
const PLATFORM: &str = "discord";
/// Most uuids one RPC may carry (hub-api bound).
pub const MAX_BATCH: usize = 25;

static RESOLVE: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_platform_user_resolve_total",
            "Community-scoped user detokenizations, labeled by a fixed outcome code",
        ),
        &["outcome"],
    )
    .expect("valid metric definition")
});

/// Registers the detokenization counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(RESOLVE.clone()))
}

/// Count of one outcome (test/observability helper).
#[must_use]
pub fn resolve_count(outcome: &str) -> u64 {
    RESOLVE.with_label_values(&[outcome]).get()
}

fn outcome(e: Option<UserResolveError>) -> &'static str {
    match e {
        None => "ok",
        Some(UserResolveError::NotFound) => "not_found",
        Some(UserResolveError::Denied) => "denied",
        Some(UserResolveError::RateLimited) => "rate_limited",
        Some(UserResolveError::Invalid) => "invalid",
        Some(UserResolveError::Unavailable) => "unavailable",
    }
}

/// Canonical lowercase 8-4-4-4-12 hex UUID.
fn is_canonical_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_digit() || (b'a'..=b'f').contains(c),
        })
}

/// Maps a client failure to its distinct typed class. Carries no content.
fn classify(e: &HubClientError) -> UserResolveError {
    match e {
        HubClientError::Grpc(s) => match s.code() {
            tonic::Code::NotFound => UserResolveError::NotFound,
            tonic::Code::PermissionDenied | tonic::Code::Unauthenticated => {
                UserResolveError::Denied
            }
            tonic::Code::ResourceExhausted => UserResolveError::RateLimited,
            tonic::Code::InvalidArgument | tonic::Code::FailedPrecondition => {
                UserResolveError::Invalid
            }
            _ => UserResolveError::Unavailable,
        },
        HubClientError::Auth(ServiceAuthError::BootstrapRejected(_)) => UserResolveError::Denied,
        _ => UserResolveError::Unavailable,
    }
}

/// The hub-api backed [`PlatformUserResolver`].
pub struct HubPlatformUserResolver {
    client: HubClient,
}

impl HubPlatformUserResolver {
    /// Production wiring: TLS to hub-api's internal gRPC endpoint, machine JWT
    /// minted with [`PLATFORM_USER_SCOPE`].
    ///
    /// # Errors
    /// [`HubClientError`] if the CA bundle cannot be read or the endpoint is
    /// invalid/unreachable.
    pub async fn connect(
        endpoint: impl Into<String>,
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        ca_cert_path: Option<&str>,
    ) -> Result<Self, HubClientError> {
        let client = HubClient::connect(
            endpoint,
            token_endpoint,
            sa_token_path,
            PLATFORM_USER_SCOPE,
            ca_cert_path,
        )
        .await?;
        Ok(Self { client })
    }

    /// Wraps an already-built client (its token client must carry
    /// [`PLATFORM_USER_SCOPE`]); also the seam tests use with a loopback server.
    #[must_use]
    pub fn from_client(client: HubClient) -> Self {
        Self { client }
    }

    /// A loopback/plaintext channel with the correct scope (tests, dev).
    #[must_use]
    pub fn from_channel(
        channel: tonic::transport::Channel,
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
    ) -> Self {
        let tokens = Arc::new(MachineJwtClient::new(
            token_endpoint,
            sa_token_path,
            PLATFORM_USER_SCOPE,
        ));
        Self::from_client(HubClient::with_channel(channel, tokens))
    }

    /// Resolves 1..=[`MAX_BATCH`] canonical uuids in ONE call. Returns uuid ->
    /// raw id; every requested uuid is present or the whole call is an error.
    ///
    /// # Errors
    /// [`UserResolveError`]; a bound/shape violation is `Invalid` WITHOUT any
    /// network call.
    pub async fn resolve_many(
        &self,
        tenant_id: i64,
        community_id: i64,
        uuids: &[String],
    ) -> Result<HashMap<String, SlotValue>, UserResolveError> {
        let result = self
            .resolve_many_inner(tenant_id, community_id, uuids)
            .await;
        let label = outcome(result.as_ref().err().copied());
        RESOLVE.with_label_values(&[label]).inc();
        match &result {
            Ok(m) => tracing::debug!(community_id, count = m.len(), "platform users resolved"),
            // Fixed codes and the numeric community only; no uuid, no raw id.
            Err(_) => tracing::warn!(
                community_id,
                outcome = label,
                "platform user resolution refused"
            ),
        }
        result
    }

    async fn resolve_many_inner(
        &self,
        tenant_id: i64,
        community_id: i64,
        uuids: &[String],
    ) -> Result<HashMap<String, SlotValue>, UserResolveError> {
        if tenant_id <= 0
            || community_id <= 0
            || uuids.is_empty()
            || uuids.len() > MAX_BATCH
            || !uuids.iter().all(|u| is_canonical_uuid(u))
        {
            return Err(UserResolveError::Invalid);
        }
        let resp = self
            .client
            .resolve_platform_user(
                tenant_id.to_string(),
                community_id,
                PLATFORM.to_string(),
                uuids.to_vec(),
            )
            .await
            .map_err(|e| classify(&e))?;
        let mut out: HashMap<String, SlotValue> = HashMap::with_capacity(resp.users.len());
        for mut u in resp.users {
            // Move the raw id straight into a zeroizing value (no leftover copy).
            let raw = std::mem::take(&mut u.platform_user_id);
            if raw.is_empty() || !uuids.contains(&u.uuid) {
                return Err(UserResolveError::Unavailable);
            }
            out.insert(u.uuid, SlotValue::new(raw));
        }
        // A "success" that omits a requested uuid is a server fault, never a partial answer.
        if uuids.iter().any(|u| !out.contains_key(u)) {
            return Err(UserResolveError::Unavailable);
        }
        Ok(out)
    }
}

impl PlatformUserResolver for HubPlatformUserResolver {
    async fn resolve(
        &self,
        tenant_id: i64,
        community_id: i64,
        user_uuid: &str,
    ) -> Result<SlotValue, UserResolveError> {
        let mut all = self
            .resolve_many(tenant_id, community_id, &[user_uuid.to_string()])
            .await?;
        all.remove(user_uuid).ok_or(UserResolveError::Unavailable)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;
    use std::time::Duration;

    use hub_client::pb::identity_service_server::{IdentityService, IdentityServiceServer};
    use hub_client::pb::{
        MintEphemeralPseudonymsRequest, MintEphemeralPseudonymsResponse,
        ResolveDisplayNamesRequest, ResolveDisplayNamesResponse, ResolveHandleRequest,
        ResolveHandleResponse, ResolvePlatformUserRequest, ResolvePlatformUserResponse,
        ResolvedPlatformUser,
    };
    use tonic::{Request, Response, Status};
    use wiremock::matchers::{body_partial_json, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::*;

    const U1: &str = "123e4567-e89b-12d3-a456-426614174000";
    const U2: &str = "223e4567-e89b-12d3-a456-426614174000";
    const OTHER: &str = "323e4567-e89b-12d3-a456-426614174000";
    const RAW1: &str = "700000000000000001";
    const RAW2: &str = "700000000000000002";
    const JWT: &str = "svc-ingest-jwt";

    #[derive(Clone, Copy, PartialEq)]
    enum Mode {
        Normal,
        ScopeDenied,
        RateLimited,
        Down,
        Omits,
        Extra,
    }

    /// Hub-api stand-in with the real semantics: bearer check, bounds, ONE
    /// constant NOT_FOUND for any miss, community-scoped membership.
    struct FakeHub {
        mode: Mode,
        members: HashMap<(i64, String), String>,
        calls: Arc<AtomicUsize>,
        seen: Arc<Mutex<Vec<ResolvePlatformUserRequest>>>,
    }

    #[tonic::async_trait]
    impl IdentityService for FakeHub {
        async fn mint_ephemeral_pseudonyms(
            &self,
            _: Request<MintEphemeralPseudonymsRequest>,
        ) -> Result<Response<MintEphemeralPseudonymsResponse>, Status> {
            Err(Status::unimplemented("n/a"))
        }
        async fn resolve_display_names(
            &self,
            _: Request<ResolveDisplayNamesRequest>,
        ) -> Result<Response<ResolveDisplayNamesResponse>, Status> {
            Err(Status::unimplemented("n/a"))
        }
        async fn resolve_handle(
            &self,
            _: Request<ResolveHandleRequest>,
        ) -> Result<Response<ResolveHandleResponse>, Status> {
            Err(Status::unimplemented("n/a"))
        }
        async fn resolve_platform_user(
            &self,
            req: Request<ResolvePlatformUserRequest>,
        ) -> Result<Response<ResolvePlatformUserResponse>, Status> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let bearer = req
                .metadata()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .map(str::to_string);
            if bearer.as_deref() != Some(&format!("Bearer {JWT}")) {
                return Err(Status::unauthenticated("bad token"));
            }
            let r = req.into_inner();
            self.seen.lock().unwrap().push(r.clone());
            match self.mode {
                Mode::ScopeDenied => return Err(Status::permission_denied("scope")),
                Mode::RateLimited => return Err(Status::resource_exhausted("slow down")),
                Mode::Down => return Err(Status::unavailable("down")),
                _ => {}
            }
            if r.community_id <= 0
                || r.platform != "discord"
                || r.uuids.is_empty()
                || r.uuids.len() > MAX_BATCH
            {
                return Err(Status::invalid_argument("bad request"));
            }
            let mut users = Vec::new();
            for u in &r.uuids {
                let key = (r.community_id, u.clone());
                match self.members.get(&key) {
                    Some(raw) => users.push(ResolvedPlatformUser {
                        uuid: u.clone(),
                        platform_user_id: raw.clone(),
                    }),
                    // ONE constant message for every miss: no oracle.
                    None => return Err(Status::not_found("platform user not found")),
                }
            }
            match self.mode {
                Mode::Omits => {
                    users.pop();
                }
                Mode::Extra => users.push(ResolvedPlatformUser {
                    uuid: OTHER.to_string(),
                    platform_user_id: "9".to_string(),
                }),
                _ => {}
            }
            Ok(Response::new(ResolvePlatformUserResponse { users }))
        }
    }

    struct Rig {
        resolver: HubPlatformUserResolver,
        calls: Arc<AtomicUsize>,
        seen: Arc<Mutex<Vec<ResolvePlatformUserRequest>>>,
        _hub: MockServer,
        sa: std::path::PathBuf,
    }

    impl Drop for Rig {
        fn drop(&mut self) {
            if let Err(e) = std::fs::remove_file(&self.sa) {
                eprintln!("synthetic sa token cleanup failed: {e}");
            }
        }
    }

    async fn rig(mode: Mode) -> Rig {
        let mut members = HashMap::new();
        members.insert((7, U1.to_string()), RAW1.to_string());
        members.insert((7, U2.to_string()), RAW2.to_string());
        // The same uuid in ANOTHER community is somebody else.
        members.insert((8, U1.to_string()), "800000000000000009".to_string());
        let calls = Arc::new(AtomicUsize::new(0));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let fake = FakeHub {
            mode,
            members,
            calls: calls.clone(),
            seen: seen.clone(),
        };
        let port = {
            let l = TcpListener::bind("127.0.0.1:0").unwrap();
            l.local_addr().unwrap().port()
        };
        let addr = format!("127.0.0.1:{port}").parse().unwrap();
        tokio::spawn(async move {
            let _ = tonic::transport::Server::builder()
                .add_service(IdentityServiceServer::new(fake))
                .serve(addr)
                .await;
        });
        // the machine-JWT bootstrap must ask for the platform-user scope
        let hub = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/internal/service-token"))
            .and(body_partial_json(
                serde_json::json!({"scope": PLATFORM_USER_SCOPE}),
            ))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"token": JWT, "expires_in": 3600})),
            )
            .mount(&hub)
            .await;
        let sa = std::env::temp_dir().join(format!("pu-sa-{}", uuid::Uuid::new_v4()));
        std::fs::write(&sa, "synthetic-sa").unwrap();
        let channel = tonic::transport::Endpoint::from_shared(format!("http://127.0.0.1:{port}"))
            .unwrap()
            .connect_timeout(Duration::from_secs(2))
            .connect_lazy();
        let resolver = HubPlatformUserResolver::from_channel(
            channel,
            format!("{}/internal/service-token", hub.uri()),
            sa.to_string_lossy().to_string(),
        );
        Rig {
            resolver,
            calls,
            seen,
            _hub: hub,
            sa,
        }
    }

    #[tokio::test]
    async fn resolves_a_member_with_one_call_naming_tenant_community_and_platform() {
        let r = rig(Mode::Normal).await;
        let v = r.resolver.resolve(9, 7, U1).await.unwrap();
        assert_eq!(v.expose(), RAW1);
        assert_eq!(r.calls.load(Ordering::SeqCst), 1);
        let seen = r.seen.lock().unwrap();
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].tenant_id, "9");
        assert_eq!(seen[0].community_id, 7);
        assert_eq!(seen[0].platform, "discord");
        assert_eq!(seen[0].uuids, vec![U1.to_string()]);
        assert!(
            !format!("{v:?}").contains(RAW1),
            "SlotValue never prints the raw id"
        );
    }

    #[tokio::test]
    async fn a_batch_resolves_every_uuid_and_is_per_call_uncached() {
        let r = rig(Mode::Normal).await;
        let m = r
            .resolver
            .resolve_many(9, 7, &[U1.to_string(), U2.to_string()])
            .await
            .unwrap();
        assert_eq!(m[U1].expose(), RAW1);
        assert_eq!(m[U2].expose(), RAW2);
        assert_eq!(r.calls.load(Ordering::SeqCst), 1);
        r.resolver.resolve(9, 7, U1).await.unwrap();
        r.resolver.resolve(9, 7, U1).await.unwrap();
        assert_eq!(r.calls.load(Ordering::SeqCst), 3, "never cached");
    }

    #[tokio::test]
    async fn the_wrong_community_or_any_miss_is_one_not_found_with_no_probing() {
        let r = rig(Mode::Normal).await;
        // U2 is not a member of community 8; U1 is (as someone else) -> the batch misses as a whole
        let e = r
            .resolver
            .resolve_many(9, 8, &[U1.to_string(), U2.to_string()])
            .await
            .unwrap_err();
        assert_eq!(e, UserResolveError::NotFound);
        assert_eq!(
            r.calls.load(Ordering::SeqCst),
            1,
            "no per-uuid probing after a miss"
        );
        // an unknown uuid
        assert_eq!(
            r.resolver.resolve(9, 7, OTHER).await.unwrap_err(),
            UserResolveError::NotFound
        );
        assert_eq!(r.calls.load(Ordering::SeqCst), 2);
        // community 8 resolves U1 to ITS OWN person, never community 7's
        let v = r.resolver.resolve(9, 8, U1).await.unwrap();
        assert_ne!(v.expose(), RAW1);
    }

    #[tokio::test]
    async fn scope_failure_rate_limit_and_outage_are_distinct_typed_errors() {
        for (mode, want) in [
            (Mode::ScopeDenied, UserResolveError::Denied),
            (Mode::RateLimited, UserResolveError::RateLimited),
            (Mode::Down, UserResolveError::Unavailable),
        ] {
            let r = rig(mode).await;
            assert_eq!(r.resolver.resolve(9, 7, U1).await.unwrap_err(), want);
        }
    }

    #[tokio::test]
    async fn rate_limiting_never_opens_the_breaker_or_becomes_a_refusal() {
        let r = rig(Mode::RateLimited).await;
        for _ in 0..12 {
            assert_eq!(
                r.resolver.resolve(9, 7, U1).await.unwrap_err(),
                UserResolveError::RateLimited
            );
        }
        // still reaching the server (no CircuitOpen -> Unavailable) after 12 rate limits
        assert_eq!(r.calls.load(Ordering::SeqCst), 12);
    }

    #[tokio::test]
    async fn the_bounds_are_enforced_client_side_before_any_call() {
        let r = rig(Mode::Normal).await;
        let many: Vec<String> = (0..=MAX_BATCH)
            .map(|i| format!("123e4567-e89b-12d3-a456-{i:012x}"))
            .collect();
        assert_eq!(many.len(), 26);
        for (t, c, uuids) in [
            (9, 7, many.clone()),
            (9, 7, vec![]),
            (0, 7, vec![U1.to_string()]),
            (9, 0, vec![U1.to_string()]),
            (9, -3, vec![U1.to_string()]),
            (9, 7, vec![U1.to_uppercase()]),
            (9, 7, vec!["700000000000000001".to_string()]),
            (9, 7, vec!["not-a-uuid".to_string()]),
        ] {
            assert_eq!(
                r.resolver.resolve_many(t, c, &uuids).await.unwrap_err(),
                UserResolveError::Invalid
            );
        }
        assert_eq!(r.calls.load(Ordering::SeqCst), 0);
        // exactly 25 is accepted by the client (server decides membership)
        let ok: Vec<String> = many.into_iter().take(MAX_BATCH).collect();
        assert_eq!(
            r.resolver.resolve_many(9, 7, &ok).await.unwrap_err(),
            UserResolveError::NotFound
        );
        assert_eq!(r.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn a_malformed_success_is_never_used() {
        for mode in [Mode::Omits, Mode::Extra] {
            let r = rig(mode).await;
            let e = r
                .resolver
                .resolve_many(9, 7, &[U1.to_string(), U2.to_string()])
                .await
                .unwrap_err();
            assert_eq!(e, UserResolveError::Unavailable);
        }
    }

    #[tokio::test]
    async fn a_rejected_token_bootstrap_is_denied() {
        let r = rig(Mode::Normal).await;
        // a resolver whose token endpoint refuses the bootstrap
        let hub = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(403))
            .mount(&hub)
            .await;
        let channel = tonic::transport::Endpoint::from_static("http://127.0.0.1:1").connect_lazy();
        let denied = HubPlatformUserResolver::from_channel(
            channel,
            format!("{}/internal/service-token", hub.uri()),
            r.sa.to_string_lossy().to_string(),
        );
        assert_eq!(
            denied.resolve(9, 7, U1).await.unwrap_err(),
            UserResolveError::Denied
        );
    }

    #[test]
    fn classification_and_metrics_are_total() {
        use tonic::Code;
        for (code, want) in [
            (Code::NotFound, UserResolveError::NotFound),
            (Code::PermissionDenied, UserResolveError::Denied),
            (Code::Unauthenticated, UserResolveError::Denied),
            (Code::ResourceExhausted, UserResolveError::RateLimited),
            (Code::InvalidArgument, UserResolveError::Invalid),
            (Code::FailedPrecondition, UserResolveError::Invalid),
            (Code::Unavailable, UserResolveError::Unavailable),
            (Code::Internal, UserResolveError::Unavailable),
        ] {
            assert_eq!(
                classify(&HubClientError::Grpc(Status::new(code, "x"))),
                want
            );
        }
        assert_eq!(
            classify(&HubClientError::CircuitOpen),
            UserResolveError::Unavailable
        );
        let registry = prometheus::Registry::new();
        register_metrics(&registry).unwrap();
        assert!(register_metrics(&registry).is_err());
        let _ = resolve_count("ok");
        for e in [
            None,
            Some(UserResolveError::NotFound),
            Some(UserResolveError::Denied),
            Some(UserResolveError::RateLimited),
            Some(UserResolveError::Invalid),
            Some(UserResolveError::Unavailable),
        ] {
            assert!(!outcome(e).is_empty());
        }
    }
}
