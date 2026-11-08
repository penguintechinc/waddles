//! Platform-controlled secret-ref grants for first-party bundles, and the
//! process-wide snapshot of each ACTIVE bundle's egress + grants that the
//! guard consults.
//!
//! Before this module nothing in production populated an egress catalog, so
//! every bundle `http.send` was refused `host_not_declared`. The active-set
//! consumers now call [`ActiveEgressSnapshot::publish`] with the approved
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

/// Per-`app_id` egress rows for the bundles currently active in this process.
#[derive(Default)]
pub struct ActiveEgressSnapshot {
    rows: RwLock<HashMap<String, EgressRuleRow>>,
}

impl ActiveEgressSnapshot {
    /// The process-wide snapshot shared by the active-set consumers (writers)
    /// and the egress guards (readers).
    pub fn global() -> Arc<Self> {
        static GLOBAL: OnceLock<Arc<ActiveEgressSnapshot>> = OnceLock::new();
        Arc::clone(GLOBAL.get_or_init(|| Arc::new(Self::default())))
    }

    /// Replaces `app_id`'s row from its approved manifest egress list
    /// (`(host_or_ip_pattern, methods)`) plus the platform grants for
    /// first-party bundles.
    pub fn publish(&self, app_id: &str, egress: Vec<(String, Vec<String>)>, rps: Option<u32>) {
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
        self.rows
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .insert(app_id.to_string(), row);
    }

    /// Parses an approval summary's `egress` array (`[{host, methods}]`) and
    /// `egressRps`; malformed entries are dropped (fail closed: less egress).
    pub fn publish_from_summary(&self, app_id: &str, summary: &serde_json::Value) {
        let egress: Vec<(String, Vec<String>)> = summary
            .get("egress")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|e| {
                        let host = e.get("host")?.as_str()?.to_string();
                        let methods = e
                            .get("methods")?
                            .as_array()?
                            .iter()
                            .filter_map(|m| m.as_str().map(str::to_string))
                            .collect::<Vec<_>>();
                        Some((host, methods))
                    })
                    .collect()
            })
            .unwrap_or_default();
        let rps = summary
            .get("egressRps")
            .and_then(|v| v.as_u64())
            .and_then(|v| u32::try_from(v).ok());
        self.publish(app_id, egress, rps);
    }

    /// Forgets `app_id` (bundle no longer active).
    pub fn remove(&self, app_id: &str) {
        self.rows
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .remove(app_id);
    }
}

impl EgressRuleSource for ActiveEgressSnapshot {
    fn resolve(&self, app_id: &str) -> Option<EgressRuleRow> {
        self.rows
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(app_id)
            .cloned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
        let eg = vec![("api.spotify.com".to_string(), vec!["GET".to_string()])];
        s.publish("waddles.core.service.music-spotify", eg.clone(), None);
        s.publish("vendor.evil.spotify", eg, None);
        let ok = s.resolve("waddles.core.service.music-spotify").unwrap();
        assert_eq!(
            ok.granted_secret_refs["spotify.oauth"],
            "connection:spotify"
        );
        let evil = s.resolve("vendor.evil.spotify").unwrap();
        assert!(evil.granted_secret_refs.is_empty());
    }

    #[test]
    fn summary_parsing_drops_malformed_entries_and_reads_rps() {
        let s = ActiveEgressSnapshot::default();
        let sum = serde_json::json!({
            "egress": [{"host": "a.example", "methods": ["GET"]}, {"host": 5}, {"methods": []}],
            "egressRps": 7
        });
        s.publish_from_summary("x", &sum);
        let r = s.resolve("x").unwrap();
        assert_eq!(r.fqdn_grants.len(), 1);
        assert_eq!(r.egress_rps, Some(7));
        s.remove("x");
        assert!(s.resolve("x").is_none());
    }

    #[test]
    fn soundcloud_row_marks_api_host_unfollowed() {
        let s = ActiveEgressSnapshot::default();
        s.publish("waddles.core.service.music-soundcloud", vec![], None);
        let r = s.resolve("waddles.core.service.music-soundcloud").unwrap();
        assert!(r.unfollowed_redirect_hosts.contains("api.soundcloud.com"));
    }
}
