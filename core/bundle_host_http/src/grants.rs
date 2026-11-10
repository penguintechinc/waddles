//! Platform-controlled secret-ref grants for first-party bundles, and the
//! process-wide snapshot of each ACTIVE bundle's egress + grants that the
//! guard consults.
//!
//! Before this module nothing in production populated an egress catalog, so
//! every bundle `http.send` was refused `host_not_declared`. The active-set
//! consumers now call [`ActiveEgressSnapshot::replace_all`] / `replace_scope` with the approved
//! install summary's `egress` list; the secret-ref grants come from the
//! PLATFORM table below (keyed by the reserved `waddles.core.*` app id) --
//! never from anything a bundle author can self-declare, so a third-party
//! bundle can never name `spotify.oauth` and get a credential.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, OnceLock, RwLock};

use crate::credentials::{AppCredentialConfig, ClientAuth, QuotaPolicy};
use crate::egress::{EgressRuleRow, EgressRuleSource};

/// One first-party bundle's platform-granted credential surface.
pub struct FirstPartyGrant {
    pub app_id: &'static str,
    /// `(secret_ref, grant source)` -- see [`crate::credentials::GrantSource`].
    pub refs: &'static [(&'static str, &'static str)],
    /// FQDNs whose first-hop 3xx is returned unfollowed.
    pub unfollowed_redirect_hosts: &'static [&'static str],
}

/// The platform grant table (reserved first-party namespace only).
pub const FIRST_PARTY_GRANTS: &[FirstPartyGrant] = &[
    FirstPartyGrant {
        app_id: "waddles.core.service.music-spotify",
        refs: &[
            ("spotify.app", "app:spotify.app"),
            ("spotify.oauth", "connection:spotify"),
        ],
        unfollowed_redirect_hosts: &[],
    },
    FirstPartyGrant {
        app_id: "waddles.core.service.music-soundcloud",
        // The community's own `soundcloud` connection holds client id/secret;
        // the host exchanges them for a token (cached per community).
        refs: &[("soundcloud.access_token", "cc:soundcloud")],
        unfollowed_redirect_hosts: &["api.soundcloud.com"],
    },
    FirstPartyGrant {
        app_id: "waddles.core.service.music-youtube",
        refs: &[
            // The community's own Google API key (`youtube_api` connection),
            // raw -- bundles inject it into a `?key` query slot.
            ("YOUTUBE_API_KEY", "connection:youtube_api#raw"),
            ("YOUTUBE_OAUTH_AUTHORIZATION", "connection:youtube"),
        ],
        unfollowed_redirect_hosts: &[],
    },
    FirstPartyGrant {
        app_id: "waddles.core.service.wix",
        refs: &[
            // Wix auth = `Authorization: <api key>` + `wix-site-id: <site id>`;
            // both come from the scheme-6 credential (static-resolve scope).
            ("wix.api_key", "connection:wix#raw"),
            ("wix.site_id", "connection:wix#field.siteId"),
        ],
        unfollowed_redirect_hosts: &[],
    },
];

/// The grant entry for `app_id`, if it is a first-party bundle.
pub fn first_party_grant(app_id: &str) -> Option<&'static FirstPartyGrant> {
    FIRST_PARTY_GRANTS.iter().find(|g| g.app_id == app_id)
}

/// Default budgets for shared credentials (operator-overridable at wiring).
pub fn default_quota_policies() -> HashMap<String, QuotaPolicy> {
    let p = |per_minute, per_day, frac| QuotaPolicy {
        per_minute: Some(per_minute),
        per_day: Some(per_day),
        community_day_fraction: Some(frac),
    };
    HashMap::from([
        ("YOUTUBE_API_KEY".to_string(), p(100, 10_000, 0.25)),
        ("spotify.app".to_string(), p(150, 100_000, 0.2)),
        ("soundcloud.access_token".to_string(), p(60, 15_000, 0.25)),
    ])
}

/// Default host-minted application credentials (client id/secret from env).
pub fn default_app_credentials() -> HashMap<String, AppCredentialConfig> {
    HashMap::from([
        (
            "spotify.app".to_string(),
            AppCredentialConfig {
                token_url: "https://accounts.spotify.com/api/token".to_string(),
                client_id_env: "SPOTIFY_CLIENT_ID".to_string(),
                client_secret_env: "SPOTIFY_CLIENT_SECRET".to_string(),
                scheme: "Bearer".to_string(),
                auth: ClientAuth::BasicHeader,
            },
        ),
        (
            // Token endpoint for `cc:soundcloud`: the client id/secret are the
            // community's own (connection), so the env names are unused.
            "soundcloud".to_string(),
            AppCredentialConfig {
                token_url: "https://secure.soundcloud.com/oauth/token".to_string(),
                client_id_env: "SOUNDCLOUD_CLIENT_ID".to_string(),
                client_secret_env: "SOUNDCLOUD_CLIENT_SECRET".to_string(),
                scheme: "OAuth".to_string(),
                auth: ClientAuth::BasicHeader,
            },
        ),
    ])
}

/// `(tenant_id, community_id)` of the activation scope a row belongs to
/// (`community_id` 0 = tenant-wide activation).
type ScopeKey = (i64, i64);

/// One active bundle's approved egress in a scope, as the active-set consumers
/// hand it over: `(app_id, egress, egressRps)`.
pub type ActiveEgressEntry = (String, Vec<(String, Vec<String>)>, Option<u32>);

/// Egress rows for the bundles currently active, keyed by activation scope
/// `(tenant_id, community_id)` THEN `app_id`.
///
/// Isolation rules (security review): rows are never merged across scopes; a
/// call resolves with its host-verified [`crate::credentials::CallScope`] to
/// exactly its own community's activation, else its tenant's tenant-wide
/// (`community_id` 0) activation, else nothing (fail closed). Writers replace
/// a scope (or the whole snapshot) wholesale each tick, so a deactivated
/// bundle drops out on the next tick.
#[derive(Default)]
pub struct ActiveEgressSnapshot {
    rows: RwLock<HashMap<ScopeKey, HashMap<String, EgressRuleRow>>>,
}

/// Builds one app's row: manifest egress + first-party platform grants.
fn build_row(app_id: &str, egress: Vec<(String, Vec<String>)>, rps: Option<u32>) -> EgressRuleRow {
    let (refs, unfollowed): (HashMap<String, String>, HashSet<String>) =
        match first_party_grant(app_id) {
            Some(g) => (
                g.refs
                    .iter()
                    .map(|(r, s)| ((*r).to_string(), (*s).to_string()))
                    .collect(),
                g.unfollowed_redirect_hosts
                    .iter()
                    .map(|h| (*h).to_string())
                    .collect(),
            ),
            None => (HashMap::new(), HashSet::new()),
        };
    let mut row = EgressRuleRow::from_legacy_patterns(egress, rps, refs);
    row.unfollowed_redirect_hosts = unfollowed;
    row
}

impl ActiveEgressSnapshot {
    /// The process-wide snapshot shared by the active-set consumers (writers)
    /// and the egress guards (readers).
    pub fn global() -> Arc<Self> {
        static GLOBAL: OnceLock<Arc<ActiveEgressSnapshot>> = OnceLock::new();
        Arc::clone(GLOBAL.get_or_init(|| Arc::new(Self::default())))
    }

    /// Replaces the ENTIRE snapshot (multi-scope consumers): every
    /// `(tenant_id, community_id, app_id, egress, egressRps)` currently
    /// active. Anything not listed -- a deactivated bundle, an emptied scope
    /// -- is gone.
    pub fn replace_all(
        &self,
        entries: impl IntoIterator<Item = (i64, i64, String, Vec<(String, Vec<String>)>, Option<u32>)>,
    ) {
        let mut next: HashMap<ScopeKey, HashMap<String, EgressRuleRow>> = HashMap::new();
        for (tenant, community, app_id, egress, rps) in entries {
            let row = build_row(&app_id, egress, rps);
            next.entry((tenant, community))
                .or_default()
                .insert(app_id, row);
        }
        *self.rows.write().unwrap_or_else(|e| e.into_inner()) = next;
    }

    /// Replaces ONE scope's rows wholesale (single-scope consumers); other
    /// scopes are untouched.
    pub fn replace_scope(
        &self,
        tenant_id: i64,
        community_id: i64,
        entries: impl IntoIterator<Item = ActiveEgressEntry>,
    ) {
        let scope_rows: HashMap<String, EgressRuleRow> = entries
            .into_iter()
            .map(|(app_id, egress, rps)| {
                let row = build_row(&app_id, egress, rps);
                (app_id, row)
            })
            .collect();
        let mut rows = self.rows.write().unwrap_or_else(|e| e.into_inner());
        if scope_rows.is_empty() {
            rows.remove(&(tenant_id, community_id));
        } else {
            rows.insert((tenant_id, community_id), scope_rows);
        }
    }

    /// The row for `app_id` as seen by a call in `scope`: its own activation,
    /// else its tenant's tenant-wide activation. Never any other scope, never
    /// a merge.
    pub fn resolve_for(
        &self,
        scope: &crate::credentials::CallScope,
        app_id: &str,
    ) -> Option<EgressRuleRow> {
        let rows = self.rows.read().unwrap_or_else(|e| e.into_inner());
        let exact = rows
            .get(&(scope.tenant_id, scope.community_id))
            .and_then(|m| m.get(app_id));
        let found = exact.or_else(|| {
            (scope.community_id != 0)
                .then(|| rows.get(&(scope.tenant_id, 0)).and_then(|m| m.get(app_id)))
                .flatten()
        });
        found.cloned()
    }
}

impl EgressRuleSource for ActiveEgressSnapshot {
    /// Unscoped lookups never match: a row is only meaningful for a verified
    /// scope (fail closed).
    fn resolve(&self, _app_id: &str) -> Option<EgressRuleRow> {
        None
    }

    fn resolve_scoped(
        &self,
        scope: &crate::credentials::CallScope,
        app_id: &str,
    ) -> Option<EgressRuleRow> {
        self.resolve_for(scope, app_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::credentials::CallScope;

    fn sc(t: i64, c: i64) -> CallScope {
        CallScope {
            tenant_id: t,
            community_id: c,
        }
    }

    fn eg(host: &str) -> Vec<(String, Vec<String>)> {
        vec![(host.to_string(), vec!["GET".to_string()])]
    }

    const SPOTIFY: &str = "waddles.core.service.music-spotify";

    #[test]
    fn every_table_ref_has_a_parseable_source_and_a_wired_app_credential() {
        let apps = default_app_credentials();
        for g in FIRST_PARTY_GRANTS {
            for (r, src) in g.refs {
                let parsed = crate::credentials::GrantSource::parse(src)
                    .unwrap_or_else(|| panic!("{} {r}: unparseable {src}", g.app_id));
                match parsed {
                    crate::credentials::GrantSource::App(name)
                    | crate::credentials::GrantSource::ClientCredentials(name) => {
                        assert!(apps.contains_key(&name), "{r}: no app credential {name}");
                    }
                    _ => {}
                }
            }
        }
        for (r, p) in default_quota_policies() {
            assert!(p.validate().is_some(), "{r}");
        }
    }

    #[test]
    fn only_first_party_apps_get_grants() {
        let s = ActiveEgressSnapshot::default();
        s.replace_scope(
            1,
            5,
            [
                (SPOTIFY.to_string(), eg("api.spotify.com"), None),
                (
                    "vendor.evil.spotify".to_string(),
                    eg("api.spotify.com"),
                    None,
                ),
            ],
        );
        let ok = s.resolve_for(&sc(1, 5), SPOTIFY).unwrap();
        assert_eq!(
            ok.granted_secret_refs["spotify.oauth"],
            "connection:spotify"
        );
        let evil = s.resolve_for(&sc(1, 5), "vendor.evil.spotify").unwrap();
        assert!(evil.granted_secret_refs.is_empty());
    }

    /// Regression (security review HIGH): one scope's approved egress must
    /// never widen another's.
    #[test]
    fn egress_is_isolated_across_tenants_and_communities() {
        let s = ActiveEgressSnapshot::default();
        s.replace_all([
            (1, 5, "app.x".to_string(), eg("a.example"), None),
            (1, 6, "app.x".to_string(), eg("b.example"), None),
            (2, 5, "app.x".to_string(), eg("c.example"), None),
        ]);
        let hosts = |t, c| -> Vec<String> {
            s.resolve_for(&sc(t, c), "app.x")
                .map(|r| r.fqdn_grants.iter().map(|(h, _)| h.clone()).collect())
                .unwrap_or_default()
        };
        assert_eq!(hosts(1, 5), ["a.example"]);
        assert_eq!(hosts(1, 6), ["b.example"]);
        assert_eq!(hosts(2, 5), ["c.example"]);
        // no row for another community / tenant, and unscoped lookups fail closed
        assert!(hosts(1, 7).is_empty());
        assert!(hosts(3, 5).is_empty());
        assert!(hosts(0, 0).is_empty());
        assert!(s.resolve("app.x").is_none());
    }

    #[test]
    fn tenant_wide_activation_serves_only_its_own_tenants_communities() {
        let s = ActiveEgressSnapshot::default();
        s.replace_all([
            (1, 0, "app.x".to_string(), eg("tenant1.example"), None),
            (1, 5, "app.x".to_string(), eg("c5.example"), None),
        ]);
        let first = |t, c| {
            s.resolve_for(&sc(t, c), "app.x")
                .map(|r| r.fqdn_grants[0].0.clone())
        };
        // community 5 has its own activation: used alone, NOT merged with tenant-wide
        assert_eq!(first(1, 5).as_deref(), Some("c5.example"));
        assert_eq!(
            s.resolve_for(&sc(1, 5), "app.x").unwrap().fqdn_grants.len(),
            1
        );
        // another community of tenant 1 falls back to the tenant-wide row
        assert_eq!(first(1, 9).as_deref(), Some("tenant1.example"));
        // a different tenant never sees tenant 1's tenant-wide row
        assert_eq!(first(2, 9), None);
    }

    /// Regression: deactivated bundles drop out when the snapshot is rebuilt.
    #[test]
    fn deactivation_removes_rows_on_the_next_wholesale_replace() {
        let s = ActiveEgressSnapshot::default();
        s.replace_all([
            (1, 5, "app.x".to_string(), eg("a.example"), None),
            (1, 5, "app.y".to_string(), eg("y.example"), None),
            (1, 6, "app.x".to_string(), eg("b.example"), None),
        ]);
        s.replace_all([(1, 6, "app.x".to_string(), eg("b.example"), None)]);
        assert!(s.resolve_for(&sc(1, 5), "app.x").is_none());
        assert!(s.resolve_for(&sc(1, 5), "app.y").is_none());
        assert!(s.resolve_for(&sc(1, 6), "app.x").is_some());

        // single-scope replace: removes the app, leaves other scopes alone,
        // and an empty scope disappears entirely
        s.replace_scope(1, 6, [("app.z".to_string(), eg("z.example"), None)]);
        assert!(s.resolve_for(&sc(1, 6), "app.x").is_none());
        assert!(s.resolve_for(&sc(1, 6), "app.z").is_some());
        s.replace_scope(1, 6, std::iter::empty());
        assert!(s.resolve_for(&sc(1, 6), "app.z").is_none());
    }

    #[test]
    fn soundcloud_row_marks_api_host_unfollowed() {
        let s = ActiveEgressSnapshot::default();
        s.replace_scope(
            1,
            5,
            [(
                "waddles.core.service.music-soundcloud".to_string(),
                vec![],
                None,
            )],
        );
        let r = s
            .resolve_for(&sc(1, 5), "waddles.core.service.music-soundcloud")
            .unwrap();
        assert!(r.unfollowed_redirect_hosts.contains("api.soundcloud.com"));
    }
}
