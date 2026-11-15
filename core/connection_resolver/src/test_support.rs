//! Real-path test fixtures for dependents (feature `test-support`, never in a service build):
//! a real in-process tonic `ProviderConnectionService` server on loopback (optionally real TLS)
//! that scripted tests point a real [`crate::ConnectionResolver`] at -- nothing between the
//! resolver and the wire is mocked.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use hub_client::pb::provider_connection_service_server::{
    ProviderConnectionService, ProviderConnectionServiceServer,
};
use hub_client::pb::{
    ListBindingsRequest, ListBindingsResponse, ResolveConnectionRequest, ResolveConnectionResponse,
};
use tonic::transport::{Identity, Server, ServerTlsConfig};
use tonic::{Request, Response, Status};

/// `(bearer token seen, request)` -> reply, for one RPC.
pub type ResolveFn = Arc<
    dyn Fn(&str, ResolveConnectionRequest) -> Result<ResolveConnectionResponse, Status>
        + Send
        + Sync,
>;
pub type ListFn =
    Arc<dyn Fn(&str, ListBindingsRequest) -> Result<ListBindingsResponse, Status> + Send + Sync>;

/// Scriptable fake hub. Counts every call that reaches it.
pub struct Hub {
    pub resolve: ResolveFn,
    pub list: ListFn,
    pub resolve_calls: Arc<AtomicUsize>,
    pub list_calls: Arc<AtomicUsize>,
    /// Sleep before answering (timeout tests).
    pub delay: Duration,
}

impl Hub {
    /// A hub answering `resolve`/`list` from the given closures.
    pub fn new(resolve: ResolveFn, list: ListFn) -> Self {
        Self {
            resolve,
            list,
            resolve_calls: Arc::default(),
            list_calls: Arc::default(),
            delay: Duration::ZERO,
        }
    }

    /// A hub whose resolve always answers `reply` and whose list is `Unimplemented`.
    pub fn resolving(
        reply: impl Fn(&str, ResolveConnectionRequest) -> Result<ResolveConnectionResponse, Status>
            + Send
            + Sync
            + 'static,
    ) -> Self {
        Self::new(
            Arc::new(reply),
            Arc::new(|_, _| Err(Status::unimplemented("list not scripted"))),
        )
    }
}

fn bearer(request: &Request<impl Sized>) -> String {
    request
        .metadata()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or_default()
        .to_string()
}

struct Svc {
    resolve: ResolveFn,
    list: ListFn,
    resolve_calls: Arc<AtomicUsize>,
    list_calls: Arc<AtomicUsize>,
    delay: Duration,
}

#[tonic::async_trait]
impl ProviderConnectionService for Svc {
    async fn list_bindings(
        &self,
        request: Request<ListBindingsRequest>,
    ) -> Result<Response<ListBindingsResponse>, Status> {
        self.list_calls.fetch_add(1, Ordering::SeqCst);
        tokio::time::sleep(self.delay).await;
        let token = bearer(&request);
        (self.list)(&token, request.into_inner()).map(Response::new)
    }

    async fn resolve_connection(
        &self,
        request: Request<ResolveConnectionRequest>,
    ) -> Result<Response<ResolveConnectionResponse>, Status> {
        self.resolve_calls.fetch_add(1, Ordering::SeqCst);
        tokio::time::sleep(self.delay).await;
        let token = bearer(&request);
        (self.resolve)(&token, request.into_inner()).map(Response::new)
    }
}

/// A running fake hub: its endpoint and a handle to stop it.
pub struct Running {
    pub endpoint: String,
    pub addr: SocketAddr,
    task: tokio::task::JoinHandle<()>,
    shutdown: tokio::sync::oneshot::Sender<()>,
}

impl Running {
    /// Graceful shutdown: the listener closes AND open connections are drained (GOAWAY), so
    /// a client sees a real dropped connection -- not just a closed listener.
    pub async fn stop(self) {
        let _ = self.shutdown.send(());
        let _ = self.task.await;
    }
}

fn free_addr() -> SocketAddr {
    let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = probe.local_addr().unwrap();
    drop(probe);
    addr
}

/// Serve `hub` (plaintext) on a fresh loopback port.
pub async fn start(hub: &Hub) -> Running {
    start_at(hub, free_addr(), None).await
}

/// Serve `hub` on `addr`, with TLS when `identity` is given (PEM cert, PEM key).
pub async fn start_at(hub: &Hub, addr: SocketAddr, identity: Option<(String, String)>) -> Running {
    let svc = Svc {
        resolve: hub.resolve.clone(),
        list: hub.list.clone(),
        resolve_calls: hub.resolve_calls.clone(),
        list_calls: hub.list_calls.clone(),
        delay: hub.delay,
    };
    let scheme = if identity.is_some() { "https" } else { "http" };
    let mut builder = Server::builder();
    if let Some((cert, key)) = identity {
        builder = builder
            .tls_config(ServerTlsConfig::new().identity(Identity::from_pem(cert, key)))
            .unwrap();
    }
    let (shutdown, stopped) = tokio::sync::oneshot::channel::<()>();
    let task = tokio::spawn(async move {
        let _ = builder
            .add_service(ProviderConnectionServiceServer::new(svc))
            .serve_with_shutdown(addr, async {
                let _ = stopped.await;
            })
            .await;
    });
    tokio::time::sleep(Duration::from_millis(150)).await;
    Running {
        endpoint: format!("{scheme}://{addr}"),
        addr,
        task,
        shutdown,
    }
}

/// An OAuth (kind 1) resolve reply for `(community, connection)`.
pub fn oauth_reply(community: i64, connection: i64) -> ResolveConnectionResponse {
    ResolveConnectionResponse {
        community_id: community,
        connection_id: connection,
        provider: "twitch".into(),
        kind: 1,
        access_token: Some("PLAIN-ACCESS".into()),
        endpoint: Some(hub_client::pb::ResolveEndpoint::default()),
        ..Default::default()
    }
}

/// Builds the `ResolveConnectionResponse` hub-api's servicer produces from the camelCase JSON
/// view the retired REST route used (and the golden fixtures keep): `communityId`,
/// `connectionId`, `provider`, `kind`, `accessToken`, `expiresAt`, `endpoint{url,region,
/// function,project}`, `secret{scheme,fields}`, `metadata`. Null/absent optionals are absent on
/// the proto wire, exactly as the servicer omits them. Panics on a malformed fixture (test-only).
#[allow(clippy::unwrap_used)]
pub fn reply_from_json(v: &serde_json::Value) -> ResolveConnectionResponse {
    use hub_client::pb::{ResolveEndpoint, ResolveSecret};
    let text = |x: &serde_json::Value| x.as_str().map(str::to_owned);
    let string_map = |x: &serde_json::Value| -> std::collections::HashMap<String, String> {
        x.as_object()
            .map(|m| {
                m.iter()
                    .filter_map(|(k, x)| text(x).map(|t| (k.clone(), t)))
                    .collect()
            })
            .unwrap_or_default()
    };
    let endpoint = &v["endpoint"];
    ResolveConnectionResponse {
        community_id: v["communityId"].as_i64().unwrap(),
        connection_id: v["connectionId"].as_i64().unwrap(),
        provider: text(&v["provider"]).unwrap(),
        kind: i32::try_from(v["kind"].as_i64().unwrap()).unwrap(),
        access_token: text(&v["accessToken"]),
        expires_at: text(&v["expiresAt"]),
        endpoint: Some(ResolveEndpoint {
            url: text(&endpoint["url"]),
            region: text(&endpoint["region"]),
            function: text(&endpoint["function"]),
            project: text(&endpoint["project"]),
        }),
        secret: v["secret"].as_object().map(|s| ResolveSecret {
            scheme: i32::try_from(s["scheme"].as_i64().unwrap()).unwrap(),
            fields: string_map(&s["fields"]),
        }),
        metadata: string_map(&v["metadata"]),
    }
}

/// Builds the `ListBindingsResponse` from the camelCase JSON view the retired REST list route
/// used: `{"items":[{communityId, connectionId, tenantId, secretVersion, provider, kind?,
/// metadata?, ...}], "nextAfter": n|null}`. Panics on a malformed fixture (test-only).
#[allow(clippy::unwrap_used)]
pub fn list_reply_from_json(v: &serde_json::Value) -> ListBindingsResponse {
    use hub_client::pb::ConnectionBinding;
    let text = |x: &serde_json::Value| x.as_str().map(str::to_owned);
    let items = v["items"]
        .as_array()
        .map(|rows| {
            rows.iter()
                .map(|r| ConnectionBinding {
                    grant_id: r["grantId"].as_i64().unwrap_or(0),
                    connection_id: r["connectionId"].as_i64().unwrap(),
                    community_id: r["communityId"].as_i64().unwrap(),
                    tenant_id: r["tenantId"].as_i64().unwrap(),
                    provider: text(&r["provider"]).unwrap(),
                    kind: i32::try_from(r["kind"].as_i64().unwrap_or(1)).unwrap(),
                    secret_version: r["secretVersion"].as_i64().unwrap(),
                    metadata: r["metadata"]
                        .as_object()
                        .map(|m| {
                            m.iter()
                                .filter_map(|(k, x)| text(x).map(|t| (k.clone(), t)))
                                .collect()
                        })
                        .unwrap_or_default(),
                    ..Default::default()
                })
                .collect()
        })
        .unwrap_or_default();
    ListBindingsResponse {
        items,
        next_after: v["nextAfter"].as_i64(),
    }
}

/// A hub that answers every list with `list` and every resolve with `resolve` (both in the
/// retired REST routes' camelCase JSON view -- see [`reply_from_json`] /
/// [`list_reply_from_json`]).
pub fn json_hub(list: &serde_json::Value, resolve: &serde_json::Value) -> Hub {
    let list_reply = list_reply_from_json(list);
    let resolve_reply = reply_from_json(resolve);
    Hub::new(
        Arc::new(move |_, _| Ok(resolve_reply.clone())),
        Arc::new(move |_, _| Ok(list_reply.clone())),
    )
}
