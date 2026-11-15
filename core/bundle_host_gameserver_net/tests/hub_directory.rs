//! `HubServerDirectory` against a recording resolver double: scope isolation, tenant check,
//! duplicate-id fail-closed, wrong-provider/secret-shape refusal, canonical-id enforcement. The
//! hub-api wire itself (scopes, 404/403 rules) is proven by hub_api's real-route tests.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;
use std::future::Future;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use bundle_host_gameserver_net::{HubServerDirectory, NetError, Scope, ServerDirectory};
use connection_resolver::{
    BindingPage, BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolve,
    ConnectionSecret, Endpoint, ResolveError, ResolvedConnection, SecretString,
};

const SID: &str = "123e4567-e89b-12d3-a456-426614174000";

#[derive(Default)]
struct Fake {
    bindings: Vec<BindingSummary>,
    provider: &'static str,
    token: &'static str,
    list_calls: AtomicUsize,
    resolve_calls: AtomicUsize,
}

fn binding(community: i64, conn: i64, tenant: i64, sid: &str) -> BindingSummary {
    BindingSummary {
        community_id: community,
        connection_id: conn,
        tenant_id: tenant,
        secret_version: 1,
        provider: "gameserver".into(),
        kind: ConnectionKind::PlatformOauth,
        metadata: BTreeMap::from([("server_id".to_string(), sid.to_string())]),
    }
}

struct Shared(Arc<Fake>);

impl ConnectionResolve for Shared {
    fn resolve(
        &self,
        r: ConnectionRef,
    ) -> impl Future<Output = Result<ResolvedConnection, ResolveError>> + Send {
        self.0.resolve_calls.fetch_add(1, Ordering::SeqCst);
        let out = ResolvedConnection {
            connection_id: r.connection_id,
            community_id: r.community_id,
            provider: self.0.provider.to_string(),
            kind: ConnectionKind::PlatformOauth,
            endpoint: Endpoint::default(),
            secret: ConnectionSecret::OAuthAccessToken {
                access_token: SecretString::new(self.0.token.to_string()),
            },
            expires_at: None,
            metadata: BTreeMap::new(),
        };
        async move { Ok(out) }
    }

    fn list_bindings(
        &self,
        provider: &str,
        _capability: Option<&str>,
        _after: i64,
        _limit: u32,
    ) -> impl Future<Output = Result<BindingPage, ResolveError>> + Send {
        self.0.list_calls.fetch_add(1, Ordering::SeqCst);
        assert_eq!(
            provider, "gameserver",
            "only the gameserver provider is ever listed"
        );
        let page = BindingPage {
            items: self.0.bindings.clone(),
            next_after: None,
        };
        async move { Ok(page) }
    }
}

fn dir(f: Fake) -> (HubServerDirectory<Shared>, Arc<Fake>) {
    let f = Arc::new(f);
    (HubServerDirectory::new(Shared(Arc::clone(&f))), f)
}

fn ok_fake(bindings: Vec<BindingSummary>) -> Fake {
    Fake {
        bindings,
        provider: "gameserver",
        token: "mc.example.com|25575|pa|ss",
        ..Fake::default()
    }
}

const SCOPE: Scope = Scope {
    tenant_id: 7,
    community_id: 3,
};

#[tokio::test]
async fn resolves_only_for_the_exact_tenant_and_community() {
    let (d, f) = dir(ok_fake(vec![binding(3, 11, 7, SID)]));
    let t = d.lookup(&SCOPE, SID).await.unwrap();
    assert_eq!(
        (t.host.as_str(), t.port, t.password.expose()),
        ("mc.example.com", 25575, "pa|ss")
    );
    assert_eq!(t.server_id, SID);
    // other community: same id is not registered there
    let other = Scope {
        tenant_id: 7,
        community_id: 4,
    };
    assert_eq!(
        d.lookup(&other, SID).await.err().unwrap(),
        NetError::Denied("not_registered")
    );
    // tenant mismatch with a matching community: refused, and the secret is never fetched for it
    let before = f.resolve_calls.load(Ordering::SeqCst);
    let wrong_tenant = Scope {
        tenant_id: 8,
        community_id: 3,
    };
    assert_eq!(
        d.lookup(&wrong_tenant, SID).await.err().unwrap(),
        NetError::Denied("not_registered")
    );
    assert_eq!(f.resolve_calls.load(Ordering::SeqCst), before);
}

#[tokio::test]
async fn noncanonical_or_unknown_ids_never_reach_the_hub() {
    let (d, f) = dir(ok_fake(vec![binding(3, 11, 7, SID)]));
    for bad in [
        "",
        "11",
        "10.0.0.1:25575",
        "{123e4567-e89b-12d3-a456-426614174000}",
    ] {
        assert_eq!(
            d.lookup(&SCOPE, bad).await.err().unwrap(),
            NetError::Denied("not_registered")
        );
    }
    assert_eq!(f.list_calls.load(Ordering::SeqCst), 0);
    assert_eq!(f.resolve_calls.load(Ordering::SeqCst), 0);
    // upper-case spelling folds to the canonical id
    assert!(d.lookup(&SCOPE, &SID.to_uppercase()).await.is_ok());
}

#[tokio::test]
async fn duplicate_server_ids_in_one_community_fail_closed() {
    let (d, _) = dir(ok_fake(vec![
        binding(3, 11, 7, SID),
        binding(3, 12, 7, SID),
    ]));
    assert_eq!(
        d.lookup(&SCOPE, SID).await.err().unwrap(),
        NetError::Denied("not_registered")
    );
}

#[tokio::test]
async fn wrong_provider_or_malformed_secret_is_refused() {
    let mut f = ok_fake(vec![binding(3, 11, 7, SID)]);
    f.provider = "twitch";
    let (d, _) = dir(f);
    assert_eq!(
        d.lookup(&SCOPE, SID).await.err().unwrap(),
        NetError::Denied("not_registered")
    );

    for token in ["", "host", "host|abc|pw", "host|0|pw"] {
        let mut f = ok_fake(vec![binding(3, 11, 7, SID)]);
        f.token = token;
        let (d, _) = dir(f);
        assert_eq!(
            d.lookup(&SCOPE, SID).await.err().unwrap(),
            NetError::Backend,
            "{token:?}"
        );
    }
}

#[tokio::test]
async fn bindings_are_cached_between_lookups() {
    let (d, f) = dir(ok_fake(vec![binding(3, 11, 7, SID)]));
    d.lookup(&SCOPE, SID).await.unwrap();
    d.lookup(&SCOPE, SID).await.unwrap();
    assert_eq!(f.list_calls.load(Ordering::SeqCst), 1);
    assert_eq!(
        f.resolve_calls.load(Ordering::SeqCst),
        2,
        "the secret is never cached"
    );
}
