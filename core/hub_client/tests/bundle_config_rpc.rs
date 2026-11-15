//! Real-path tests for `BundleConfigClient` + `BundleConfigCache`: a real tonic server on loopback
//! (the generated `BundleConfigService` server trait), a real `MachineJwtClient` bootstrapping
//! from a fake mint, a real lazy `Channel`. "Admin saves" is modelled by bumping the fake hub's
//! stored version, the same thing hub-api's gateway does to `bundle_community_config`.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use hub_client::bundle_config::{
    BundleConfigCache, BundleConfigClient, CacheConfig, ConfigKey, VersionProbe, API_VERSION, SCOPE,
};
use hub_client::lazy_channel;
use hub_client::pb::bundle_config_service_server::{
    BundleConfigService, BundleConfigServiceServer,
};
use hub_client::pb::{
    BatchGetCommunityConfigRequest, BatchGetCommunityConfigResponse, GetCommunityConfigRequest,
    GetCommunityConfigResponse,
};
use service_auth::test_support::{MintReply, MockHub};
use service_auth::MachineJwtClient;
use tonic::{Request, Response, Status};

const TOKEN: &str = "machine-jwt-for-bundle-config";
const APP: &str = "waddles.core.app.calendar";

/// (version, merged config json) per `(tenant, community, app)` -- the hub's stored truth.
type Store = Arc<Mutex<HashMap<(i64, i64, String), (i64, String)>>>;

struct FakeHub {
    store: Store,
    gets: Arc<Mutex<u32>>,
}

impl FakeHub {
    fn answer(&self, r: &GetCommunityConfigRequest) -> Result<GetCommunityConfigResponse, Status> {
        if r.api_version != API_VERSION {
            return Err(Status::unimplemented("api_version"));
        }
        let store = self.store.lock().unwrap();
        let (version, json) = store
            .get(&(r.tenant_id, r.community_id, r.app_id.clone()))
            .ok_or_else(|| Status::not_found("bundle not active"))?;
        Ok(GetCommunityConfigResponse {
            tenant_id: r.tenant_id,
            community_id: r.community_id,
            app_id: r.app_id.clone(),
            version: *version,
            config_json: json.clone().into_bytes(),
        })
    }
}

fn bearer_ok<T>(r: &Request<T>) -> Result<(), Status> {
    let auth = r
        .metadata()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default();
    if auth == format!("Bearer {TOKEN}") {
        Ok(())
    } else {
        Err(Status::unauthenticated("unauthorized"))
    }
}

#[tonic::async_trait]
impl BundleConfigService for FakeHub {
    async fn get_community_config(
        &self,
        request: Request<GetCommunityConfigRequest>,
    ) -> Result<Response<GetCommunityConfigResponse>, Status> {
        bearer_ok(&request)?;
        *self.gets.lock().unwrap() += 1;
        self.answer(request.get_ref()).map(Response::new)
    }

    async fn batch_get_community_config(
        &self,
        request: Request<BatchGetCommunityConfigRequest>,
    ) -> Result<Response<BatchGetCommunityConfigResponse>, Status> {
        bearer_ok(&request)?;
        let configs = request
            .get_ref()
            .scopes
            .iter()
            .filter_map(|s| {
                let mut s = s.clone();
                s.api_version = request.get_ref().api_version.clone();
                self.answer(&s).ok()
            })
            .collect();
        Ok(Response::new(BatchGetCommunityConfigResponse { configs }))
    }
}

async fn serve(hub: FakeHub) -> String {
    let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr: SocketAddr = probe.local_addr().unwrap();
    drop(probe);
    tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(BundleConfigServiceServer::new(hub))
            .serve(addr)
            .await
            .unwrap();
    });
    tokio::time::sleep(Duration::from_millis(100)).await;
    format!("http://{addr}")
}

async fn client(endpoint: &str) -> (BundleConfigClient, MockHub) {
    let mint = MockHub::builder()
        .mint(vec![MintReply::when(|c| c.scope == SCOPE, TOKEN, 3600)])
        .start()
        .await;
    let sa = std::env::temp_dir().join(format!("hub-client-bcfg-sa-{}", std::process::id()));
    std::fs::write(&sa, "projected-sa-token").unwrap();
    let token_client = Arc::new(MachineJwtClient::new(
        mint.endpoint.clone(),
        sa.to_string_lossy().to_string(),
        SCOPE,
    ));
    let channel = lazy_channel(endpoint, None).unwrap();
    (
        BundleConfigClient::with_channel(channel, token_client),
        mint,
    )
}

fn key() -> ConfigKey {
    ConfigKey::new(1, 7, APP)
}

struct StoreProbe(Store);

#[async_trait]
impl VersionProbe for StoreProbe {
    /// Stands in for hub-api's Valkey version key: the hub's stored version.
    async fn latest(&self, k: &ConfigKey) -> Option<i64> {
        self.0
            .lock()
            .unwrap()
            .get(&(k.tenant_id, k.community_id, k.app_id.clone()))
            .map(|(v, _)| *v)
    }
}

#[tokio::test]
async fn scope_is_the_bundle_config_read_scope() {
    assert_eq!(SCOPE, "bundle-config:read");
}

#[tokio::test]
async fn cache_seeds_serves_and_picks_up_a_saved_change_over_real_grpc() {
    let store: Store = Arc::default();
    store.lock().unwrap().insert(
        (1, 7, APP.into()),
        (0, r#"{"default_duration_minutes":60}"#.into()),
    );
    let gets = Arc::new(Mutex::new(0));
    let endpoint = serve(FakeHub {
        store: store.clone(),
        gets: gets.clone(),
    })
    .await;
    let (c, _mint) = client(&endpoint).await;
    let cache = BundleConfigCache::new(
        Arc::new(c),
        Some(Arc::new(StoreProbe(store.clone()))),
        CacheConfig {
            ttl: Duration::from_secs(60),
            recheck: Duration::from_millis(0),
        },
    );

    // seed at bundle load: batch RPC, no per-scope GET afterwards
    assert_eq!(cache.seed(&[key()]).await, 1);
    assert_eq!(
        &*cache.lookup(&key()).await,
        r#"{"default_duration_minutes":60}"#
    );
    assert_eq!(*gets.lock().unwrap(), 0);

    // an admin saves 45 minutes: hub-api stores version 1 and bumps the version key
    store.lock().unwrap().insert(
        (1, 7, APP.into()),
        (1, r#"{"default_duration_minutes":45}"#.into()),
    );
    assert_eq!(
        &*cache.lookup(&key()).await,
        r#"{"default_duration_minutes":45}"#
    );
    assert_eq!(
        *gets.lock().unwrap(),
        1,
        "reloaded through GetCommunityConfig"
    );
}

#[tokio::test]
async fn unknown_scope_is_refused_and_falls_back_loudly_to_empty() {
    let endpoint = serve(FakeHub {
        store: Arc::default(),
        gets: Arc::default(),
    })
    .await;
    let (c, _mint) = client(&endpoint).await;
    let cache = BundleConfigCache::new(Arc::new(c), None, CacheConfig::default());
    assert_eq!(&*cache.lookup(&ConfigKey::new(1, 99, APP)).await, "{}");
    assert!(cache.is_empty());
}
