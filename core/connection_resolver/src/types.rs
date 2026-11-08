//! Public value types of the resolver contract and the wire DTOs they are parsed from.

use std::collections::{BTreeMap, HashMap};

use hub_client::pb;

use crate::error::ResolveError;
use crate::secret::SecretString;

/// Numeric identity of one connection within one community (never names/slugs).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct ConnectionRef {
    pub community_id: i64,
    pub connection_id: i64,
}

/// What kind of target a connection is (hub-api's `ConnectionKind` int code).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(i16)]
pub enum ConnectionKind {
    PlatformOauth = 1,
    ExternalHttp = 2,
    AwsLambda = 3,
    GcpFunction = 4,
}

impl ConnectionKind {
    /// Decode a wire code; an unknown code is `Malformed`, never a default.
    pub fn from_code(c: i16) -> Result<Self, ResolveError> {
        match c {
            1 => Ok(Self::PlatformOauth),
            2 => Ok(Self::ExternalHttp),
            3 => Ok(Self::AwsLambda),
            4 => Ok(Self::GcpFunction),
            _ => Err(ResolveError::Malformed("unknown connection kind")),
        }
    }

    /// The wire code.
    pub fn code(self) -> i16 {
        self as i16
    }
}

/// Non-secret target facts of a static connection; all `None` for OAuth rows.
#[derive(Clone, PartialEq, Eq, Default)]
pub struct Endpoint {
    pub url: Option<String>,
    pub region: Option<String>,
    pub function: Option<String>,
    pub project: Option<String>,
}

impl std::fmt::Debug for Endpoint {
    /// `url` is redacted: webhook-style URLs can embed a token in the path or query.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Endpoint")
            .field("url", &self.url.as_ref().map(|_| "[REDACTED]"))
            .field("region", &self.region)
            .field("function", &self.function)
            .field("project", &self.project)
            .finish()
    }
}

/// A decrypted credential, shaped by the wire `secret.scheme` (or OAuth access token).
pub enum ConnectionSecret {
    /// scheme 1.
    Bearer { token: SecretString },
    /// scheme 2.
    Basic {
        username: SecretString,
        password: SecretString,
    },
    /// scheme 3.
    ApiKeyHeader { header: String, value: SecretString },
    /// scheme 4.
    AwsAccessKey {
        access_key_id: SecretString,
        secret_access_key: SecretString,
        session_token: Option<SecretString>,
    },
    /// scheme 5.
    GcpServiceAccount {
        client_email: String,
        private_key_pem: SecretString,
        token_uri: Option<String>,
    },
    /// kind 1 (`accessToken` field).
    OAuthAccessToken { access_token: SecretString },
}

impl std::fmt::Debug for ConnectionSecret {
    /// Names the variant and non-secret fields only; every secret prints `[REDACTED]`.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Bearer { .. } => f
                .debug_struct("Bearer")
                .field("token", &"[REDACTED]")
                .finish(),
            Self::Basic { .. } => f
                .debug_struct("Basic")
                .field("username", &"[REDACTED]")
                .field("password", &"[REDACTED]")
                .finish(),
            Self::ApiKeyHeader { header, .. } => f
                .debug_struct("ApiKeyHeader")
                .field("header", header)
                .field("value", &"[REDACTED]")
                .finish(),
            Self::AwsAccessKey { session_token, .. } => f
                .debug_struct("AwsAccessKey")
                .field("access_key_id", &"[REDACTED]")
                .field("secret_access_key", &"[REDACTED]")
                .field(
                    "session_token",
                    &session_token.as_ref().map(|_| "[REDACTED]"),
                )
                .finish(),
            Self::GcpServiceAccount {
                client_email,
                token_uri,
                ..
            } => f
                .debug_struct("GcpServiceAccount")
                .field("client_email", client_email)
                .field("private_key_pem", &"[REDACTED]")
                .field("token_uri", token_uri)
                .finish(),
            Self::OAuthAccessToken { .. } => f
                .debug_struct("OAuthAccessToken")
                .field("access_token", &"[REDACTED]")
                .finish(),
        }
    }
}

/// One resolved connection: endpoint plus credential. Deliberately not `Clone`
/// so plaintext copies stay countable; `Debug` redacts the secret.
pub struct ResolvedConnection {
    pub connection_id: i64,
    pub community_id: i64,
    pub provider: String,
    pub kind: ConnectionKind,
    pub endpoint: Endpoint,
    pub secret: ConnectionSecret,
    /// Expiry in epoch seconds, when the credential is time-limited.
    pub expires_at: Option<i64>,
    /// Non-secret binding metadata AS OF THIS RESOLVE (same public keys as the
    /// binding list). Lets a caller re-assert facts it read from a list (e.g.
    /// channel ownership) against the record it is about to use. Empty when
    /// the server sent none.
    pub metadata: BTreeMap<String, String>,
}

impl std::fmt::Debug for ResolvedConnection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ResolvedConnection")
            .field("connection_id", &self.connection_id)
            .field("community_id", &self.community_id)
            .field("provider", &self.provider)
            .field("kind", &self.kind)
            .field("endpoint", &self.endpoint)
            .field("secret", &self.secret)
            .field("expires_at", &self.expires_at)
            .finish()
    }
}

/// One binding the data plane should run (no credential).
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct BindingSummary {
    pub community_id: i64,
    pub connection_id: i64,
    /// Server-derived tenant of the community the grant points at (hub-api
    /// `communities.tenant_id`), always `>= 1`. NEVER taken from `metadata`.
    pub tenant_id: i64,
    /// Credential-rotation counter (`>= 1`): bumped when an admin replaces the
    /// credential, not on a token refresh. Part of the connection fingerprint
    /// so a rotation restarts the connection. A counter, never secret material.
    pub secret_version: i64,
    pub provider: String,
    pub kind: ConnectionKind,
    pub metadata: BTreeMap<String, String>,
}

/// One page of bindings; `next_after` is the cursor for the next page.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct BindingPage {
    pub items: Vec<BindingSummary>,
    pub next_after: Option<i64>,
}

// ---- proto -> domain conversion (hub-api `ProviderConnectionService`) ----

/// Remove a required secret field or fail `Malformed`.
fn take(
    fields: &mut HashMap<String, SecretString>,
    key: &str,
) -> Result<SecretString, ResolveError> {
    fields
        .remove(key)
        .ok_or(ResolveError::Malformed("secret field missing"))
}

/// Remove an optional secret field (absent -> `None`; hub-api omits null fields).
fn take_opt(fields: &mut HashMap<String, SecretString>, key: &str) -> Option<SecretString> {
    fields.remove(key)
}

/// Remove a required non-secret field as a plain `String`.
fn take_plain(
    fields: &mut HashMap<String, SecretString>,
    key: &str,
) -> Result<String, ResolveError> {
    take(fields, key).map(|s| s.expose().to_owned())
}

/// Map the secret document to a [`ConnectionSecret`] by scheme code.
fn map_secret(
    scheme: i32,
    mut f: HashMap<String, SecretString>,
) -> Result<ConnectionSecret, ResolveError> {
    match scheme {
        1 => Ok(ConnectionSecret::Bearer {
            token: take(&mut f, "token")?,
        }),
        2 => Ok(ConnectionSecret::Basic {
            username: take(&mut f, "username")?,
            password: take(&mut f, "password")?,
        }),
        3 => Ok(ConnectionSecret::ApiKeyHeader {
            header: take_plain(&mut f, "header")?,
            value: take(&mut f, "value")?,
        }),
        4 => Ok(ConnectionSecret::AwsAccessKey {
            access_key_id: take(&mut f, "accessKeyId")?,
            secret_access_key: take(&mut f, "secretAccessKey")?,
            session_token: take_opt(&mut f, "sessionToken"),
        }),
        5 => Ok(ConnectionSecret::GcpServiceAccount {
            client_email: take_plain(&mut f, "clientEmail")?,
            private_key_pem: take(&mut f, "privateKey")?,
            token_uri: take_opt(&mut f, "tokenUri").map(|s| s.expose().to_owned()),
        }),
        _ => Err(ResolveError::Malformed("unknown secret scheme")),
    }
}

/// kind 2 takes bearer/basic/api-key; kind 3 takes AWS keys; kind 4 takes a GCP service account.
fn scheme_fits_kind(kind: ConnectionKind, secret: &ConnectionSecret) -> bool {
    matches!(
        (kind, secret),
        (
            ConnectionKind::ExternalHttp,
            ConnectionSecret::Bearer { .. }
                | ConnectionSecret::Basic { .. }
                | ConnectionSecret::ApiKeyHeader { .. }
        ) | (
            ConnectionKind::AwsLambda,
            ConnectionSecret::AwsAccessKey { .. }
        ) | (
            ConnectionKind::GcpFunction,
            ConnectionSecret::GcpServiceAccount { .. }
        )
    )
}

/// Decode a wire kind code; out of `i16` range or unknown is `Malformed`, never a default.
fn kind_from_wire(code: i32) -> Result<ConnectionKind, ResolveError> {
    i16::try_from(code)
        .map_err(|_| ResolveError::Malformed("unknown connection kind"))
        .and_then(ConnectionKind::from_code)
}

/// Convert a `ResolveConnectionResponse` into a [`ResolvedConnection`], enforcing that it
/// answers `want`.
///
/// Every secret string is MOVED out of the message into a zeroizing [`SecretString`] (no
/// copy); the emptied message is dropped. The transport's own decode buffers are outside this
/// crate's control and remain an accepted residual.
pub(crate) fn resolved_from_pb(
    mut msg: pb::ResolveConnectionResponse,
    want: crate::ConnectionRef,
) -> Result<ResolvedConnection, ResolveError> {
    if msg.community_id != want.community_id || msg.connection_id != want.connection_id {
        return Err(ResolveError::Malformed(
            "resolve response is for another connection",
        ));
    }
    let kind = kind_from_wire(msg.kind)?;
    let access_token = msg.access_token.take().map(SecretString::new);
    let secret_doc = msg.secret.take();
    let secret = match (access_token, secret_doc) {
        (Some(access_token), None) if kind == ConnectionKind::PlatformOauth => {
            ConnectionSecret::OAuthAccessToken { access_token }
        }
        (None, Some(doc)) if kind != ConnectionKind::PlatformOauth => {
            let fields = doc
                .fields
                .into_iter()
                .map(|(k, v)| (k, SecretString::new(v)))
                .collect();
            let mapped = map_secret(doc.scheme, fields)?;
            if !scheme_fits_kind(kind, &mapped) {
                return Err(ResolveError::Malformed("secret scheme does not match kind"));
            }
            mapped
        }
        _ => return Err(ResolveError::Malformed("credential does not match kind")),
    };
    let expires_at = match msg.expires_at.take() {
        None => None,
        Some(s) => Some(parse_rfc3339(&s).ok_or(ResolveError::Malformed("invalid expiresAt"))?),
    };
    let endpoint = msg
        .endpoint
        .take()
        .map_or_else(Endpoint::default, |e| Endpoint {
            url: e.url,
            region: e.region,
            function: e.function,
            project: e.project,
        });
    Ok(ResolvedConnection {
        connection_id: msg.connection_id,
        community_id: msg.community_id,
        provider: msg.provider,
        kind,
        endpoint,
        secret,
        expires_at,
        metadata: msg.metadata.into_iter().collect(),
    })
}

/// Convert a `ListBindingsResponse` into a [`BindingPage`].
pub(crate) fn page_from_pb(msg: pb::ListBindingsResponse) -> Result<BindingPage, ResolveError> {
    let items = msg
        .items
        .into_iter()
        .map(|b| {
            // The binding-feed contract (docs/PROVIDER_CONNECTIONS.md) makes both
            // >= 1 on every served row; anything else is a hub-api bug and the
            // whole page fails closed rather than running a tenant-less binding.
            if b.tenant_id < 1 {
                return Err(ResolveError::Malformed("binding tenantId must be >= 1"));
            }
            if b.secret_version < 1 {
                return Err(ResolveError::Malformed(
                    "binding secretVersion must be >= 1",
                ));
            }
            Ok(BindingSummary {
                community_id: b.community_id,
                connection_id: b.connection_id,
                tenant_id: b.tenant_id,
                secret_version: b.secret_version,
                provider: b.provider,
                kind: kind_from_wire(b.kind)?,
                metadata: b.metadata.into_iter().collect(),
            })
        })
        .collect::<Result<Vec<_>, ResolveError>>()?;
    Ok(BindingPage {
        items,
        next_after: msg.next_after,
    })
}

/// Days in `month` (1-12) of `year`, leap-year aware.
fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// Days since 1970-01-01 for a proleptic Gregorian date (Hinnant's algorithm).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Parse `YYYY-MM-DDTHH:MM:SS[.frac][Z|+HH:MM|-HH:MM]` to epoch seconds (no offset = UTC).
pub(crate) fn parse_rfc3339(s: &str) -> Option<i64> {
    let num = |range: std::ops::Range<usize>| -> Option<i64> {
        let part = s.get(range)?;
        if part.is_empty() || !part.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        part.parse().ok()
    };
    let b = s.as_bytes();
    if b.get(4) != Some(&b'-')
        || b.get(7) != Some(&b'-')
        || !matches!(b.get(10), Some(b'T' | b' '))
        || b.get(13) != Some(&b':')
        || b.get(16) != Some(&b':')
    {
        return None;
    }
    let (year, month, day) = (num(0..4)?, num(5..7)?, num(8..10)?);
    let (hour, min, sec) = (num(11..13)?, num(14..16)?, num(17..19)?);
    if !(1..=12).contains(&month)
        || day < 1
        || day > days_in_month(year, month)
        || hour > 23
        || min > 59
        || sec > 60
    {
        return None;
    }
    let mut rest = s.get(19..)?;
    if let Some(frac) = rest.strip_prefix('.') {
        let digits = frac.bytes().take_while(u8::is_ascii_digit).count();
        if digits == 0 {
            return None;
        }
        rest = frac.get(digits..)?;
    }
    let offset = match rest {
        "" | "Z" | "z" => 0,
        _ => {
            let sign = match rest.as_bytes().first()? {
                b'+' => 1,
                b'-' => -1,
                _ => return None,
            };
            let tz = rest.get(1..)?;
            if tz.len() != 5 || tz.as_bytes().get(2) != Some(&b':') {
                return None;
            }
            let digit_pair = |r: std::ops::Range<usize>| -> Option<i64> {
                let p = tz.get(r)?;
                if !p.bytes().all(|b| b.is_ascii_digit()) {
                    return None;
                }
                p.parse().ok()
            };
            let (oh, om) = (digit_pair(0..2)?, digit_pair(3..5)?);
            if oh > 23 || om > 59 {
                return None;
            }
            sign * (oh * 3600 + om * 60)
        }
    };
    Some(days_from_civil(year, month, day) * 86_400 + hour * 3600 + min * 60 + sec - offset)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kind_codes_roundtrip_and_unknown_is_malformed() {
        for k in [
            ConnectionKind::PlatformOauth,
            ConnectionKind::ExternalHttp,
            ConnectionKind::AwsLambda,
            ConnectionKind::GcpFunction,
        ] {
            assert_eq!(ConnectionKind::from_code(k.code()).unwrap(), k);
        }
        for bad in [0, 5, 9, -1] {
            assert!(matches!(
                ConnectionKind::from_code(bad),
                Err(ResolveError::Malformed(_))
            ));
        }
        // A wire code outside i16 never wraps into a valid kind.
        assert!(matches!(
            kind_from_wire(65_537),
            Err(ResolveError::Malformed("unknown connection kind"))
        ));
    }

    const REF: crate::ConnectionRef = crate::ConnectionRef {
        community_id: 7,
        connection_id: 12,
    };

    fn response(
        kind: i32,
        secret: Option<pb::ResolveSecret>,
        token: Option<&str>,
    ) -> pb::ResolveConnectionResponse {
        pb::ResolveConnectionResponse {
            community_id: 7,
            connection_id: 12,
            provider: "p".into(),
            kind,
            access_token: token.map(str::to_owned),
            expires_at: None,
            endpoint: Some(pb::ResolveEndpoint::default()),
            secret,
            metadata: HashMap::new(),
        }
    }

    fn secret(scheme: i32, fields: &[(&str, &str)]) -> Option<pb::ResolveSecret> {
        Some(pb::ResolveSecret {
            scheme,
            fields: fields
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
                .collect(),
        })
    }

    /// regression: p6a-t6b codex-2 -- resolve surfaces the record's own
    /// metadata so callers can re-assert list-derived facts; absent -> empty.
    #[test]
    fn resolve_metadata_is_surfaced() {
        let mut msg = response(1, None, Some("t"));
        msg.metadata.insert("channel".into(), "abc".into());
        let rc = resolved_from_pb(msg, REF).unwrap();
        assert_eq!(rc.metadata.get("channel").map(String::as_str), Some("abc"));
        assert!(
            !format!("{rc:?}").contains("abc"),
            "Debug must not print metadata"
        );
        let none = resolved_from_pb(response(1, None, Some("t")), REF).unwrap();
        assert!(none.metadata.is_empty());
    }

    #[test]
    fn unknown_kind_or_scheme_code_is_malformed() {
        assert!(matches!(
            resolved_from_pb(response(9, None, Some("t")), REF),
            Err(ResolveError::Malformed("unknown connection kind"))
        ));
        assert!(matches!(
            resolved_from_pb(response(2, secret(9, &[]), None), REF),
            Err(ResolveError::Malformed("unknown secret scheme"))
        ));
    }

    #[test]
    fn every_scheme_maps_and_missing_fields_are_malformed() {
        type Case<'a> = (i32, &'a [(&'a str, &'a str)], i32, bool);
        // (scheme, fields, kind, expect_ok)
        let cases: Vec<Case> = vec![
            (1, &[("token", "a")], 2, true),
            (2, &[("username", "u"), ("password", "p")], 2, true),
            (3, &[("header", "X-K"), ("value", "v")], 2, true),
            (
                4,
                &[
                    ("accessKeyId", "a"),
                    ("secretAccessKey", "s"),
                    ("sessionToken", "t"),
                ],
                3,
                true,
            ),
            (
                5,
                &[("clientEmail", "e"), ("privateKey", "k"), ("tokenUri", "u")],
                4,
                true,
            ),
            (1, &[], 2, false),
            (2, &[("username", "u")], 2, false),
            (3, &[("value", "v")], 2, false), // header absent
            (4, &[("accessKeyId", "a")], 3, false),
            (5, &[("clientEmail", "e")], 4, false),
        ];
        for (scheme, fields, kind, ok) in cases {
            let r = resolved_from_pb(response(kind, secret(scheme, fields), None), REF);
            assert_eq!(r.is_ok(), ok, "scheme {scheme} {fields:?}: {r:?}");
        }
    }

    #[test]
    fn kind_and_scheme_must_agree() {
        let schemes: [(i32, &[(&str, &str)]); 5] = [
            (1, &[("token", "a")]),
            (2, &[("username", "u"), ("password", "p")]),
            (3, &[("header", "X-K"), ("value", "v")]),
            (4, &[("accessKeyId", "a"), ("secretAccessKey", "s")]),
            (5, &[("clientEmail", "e"), ("privateKey", "k")]),
        ];
        // allowed[kind-2][scheme-1]
        let allowed = [
            [true, true, true, false, false],
            [false, false, false, true, false],
            [false, false, false, false, true],
        ];
        for (ki, row) in allowed.iter().enumerate() {
            for (si, ok) in row.iter().enumerate() {
                let kind = ki as i32 + 2;
                let (scheme, fields) = schemes[si];
                let r = resolved_from_pb(response(kind, secret(scheme, fields), None), REF);
                assert_eq!(r.is_ok(), *ok, "kind {kind} scheme {}: {r:?}", si + 1);
                if !ok {
                    assert!(matches!(
                        r,
                        Err(ResolveError::Malformed("secret scheme does not match kind"))
                    ));
                }
            }
        }
    }

    #[test]
    fn credential_must_match_kind_and_identity() {
        // kind 1 without accessToken, kind 2 with accessToken only, both present.
        assert!(resolved_from_pb(response(1, None, None), REF).is_err());
        assert!(resolved_from_pb(response(2, None, Some("t")), REF).is_err());
        let both = response(1, secret(1, &[("token", "a")]), Some("t"));
        assert!(resolved_from_pb(both, REF).is_err());
        let other = crate::ConnectionRef {
            community_id: 8,
            connection_id: 12,
        };
        assert!(matches!(
            resolved_from_pb(response(1, None, Some("t")), other),
            Err(ResolveError::Malformed(
                "resolve response is for another connection"
            ))
        ));
    }

    #[test]
    fn expiry_endpoint_and_optional_fields_convert() {
        let mut msg = response(
            3,
            secret(4, &[("accessKeyId", "a"), ("secretAccessKey", "s")]),
            None,
        );
        msg.expires_at = Some("2026-10-10T16:00:00+00:00".into());
        msg.endpoint = Some(pb::ResolveEndpoint {
            url: None,
            region: Some("us-east-1".into()),
            function: Some("fn".into()),
            project: None,
        });
        let rc = resolved_from_pb(msg, REF).unwrap();
        assert_eq!(rc.expires_at, Some(1_791_648_000));
        assert_eq!(rc.endpoint.region.as_deref(), Some("us-east-1"));
        assert!(rc.endpoint.url.is_none());
        match rc.secret {
            ConnectionSecret::AwsAccessKey { session_token, .. } => {
                assert!(session_token.is_none())
            }
            other => panic!("{other:?}"),
        }
        let mut bad = response(1, None, Some("t"));
        bad.expires_at = Some("not-a-time".into());
        assert!(matches!(
            resolved_from_pb(bad, REF),
            Err(ResolveError::Malformed("invalid expiresAt"))
        ));
        // An absent endpoint message is an empty endpoint, not an error.
        let mut no_ep = response(1, None, Some("t"));
        no_ep.endpoint = None;
        assert_eq!(
            resolved_from_pb(no_ep, REF).unwrap().endpoint,
            Endpoint::default()
        );
    }

    fn binding(kind: i32, tenant_id: i64, secret_version: i64) -> pb::ConnectionBinding {
        pb::ConnectionBinding {
            community_id: 1,
            connection_id: 2,
            tenant_id,
            secret_version,
            provider: "twitch".into(),
            kind,
            metadata: [("a".to_owned(), "b".to_owned())].into(),
            ..Default::default()
        }
    }

    #[test]
    fn list_page_converts_kinds_cursor_and_metadata() {
        let page = page_from_pb(pb::ListBindingsResponse {
            items: vec![binding(1, 3, 4), binding(2, 3, 1)],
            next_after: Some(9),
        })
        .unwrap();
        assert_eq!(page.items.len(), 2);
        assert_eq!(page.items[0].kind, ConnectionKind::PlatformOauth);
        assert_eq!(page.items[1].kind, ConnectionKind::ExternalHttp);
        assert_eq!(page.next_after, Some(9));
        assert_eq!(
            page.items[0].metadata.get("a").map(String::as_str),
            Some("b")
        );
        let last = page_from_pb(pb::ListBindingsResponse::default()).unwrap();
        assert!(last.items.is_empty() && last.next_after.is_none());
        // An unknown (or unset = 0) kind fails the whole page closed -- no silent default.
        for bad in [0, 9] {
            assert!(page_from_pb(pb::ListBindingsResponse {
                items: vec![binding(bad, 3, 1)],
                next_after: None,
            })
            .is_err());
        }
    }

    #[test]
    fn list_page_carries_the_feed_tenant_and_secret_version() {
        let page = page_from_pb(pb::ListBindingsResponse {
            items: vec![binding(1, 42, 5)],
            next_after: None,
        })
        .unwrap();
        assert_eq!(
            (page.items[0].tenant_id, page.items[0].secret_version),
            (42, 5)
        );
    }

    /// regression: a binding without a server-derived tenant (or a rotation
    /// counter) must fail the page closed, never run tenant-less.
    #[test]
    fn list_page_refuses_a_missing_or_non_positive_tenant_or_version() {
        for (tenant, version) in [(0, 1), (3, 0), (-1, 1), (3, -2)] {
            let page = pb::ListBindingsResponse {
                items: vec![binding(1, tenant, version)],
                next_after: None,
            };
            assert!(
                page_from_pb(page).is_err(),
                "tenant {tenant} version {version} must be refused"
            );
        }
        let ok = pb::ListBindingsResponse {
            items: vec![binding(1, 3, 1)],
            next_after: None,
        };
        assert!(page_from_pb(ok).is_ok());
    }

    #[test]
    fn rfc3339_known_values_and_rejects() {
        assert_eq!(parse_rfc3339("1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(
            parse_rfc3339("2026-10-10T16:00:00+00:00"),
            Some(1_791_648_000)
        );
        assert_eq!(parse_rfc3339("2026-10-10T16:00:00"), Some(1_791_648_000));
        assert_eq!(
            parse_rfc3339("2026-10-10T18:00:00.123456+02:00"),
            Some(1_791_648_000)
        );
        assert_eq!(
            parse_rfc3339("2026-10-10T11:30:00-04:30"),
            Some(1_791_648_000)
        );
        assert_eq!(parse_rfc3339("2000-02-29T00:00:00Z"), Some(951_782_400));
        assert!(parse_rfc3339("2024-02-29T00:00:00Z").is_some());
        assert_eq!(parse_rfc3339("2026-02-31T00:00:00Z"), None);
        assert_eq!(parse_rfc3339("2024-02-30T00:00:00Z"), None);
        assert_eq!(parse_rfc3339("2026-02-29T00:00:00Z"), None);
        assert_eq!(parse_rfc3339("1900-02-29T00:00:00Z"), None);
        assert_eq!(parse_rfc3339("2026-04-31T00:00:00Z"), None);
        assert!(parse_rfc3339("2026-01-31T00:00:00Z").is_some());
        for bad in [
            "",
            "garbage",
            "2026-13-10T16:00:00Z",
            "2026-10-10T25:00:00Z",
            "2026-10-10T16:00:00.Z",
            "2026-10-10T16:00:00+0000",
            "2026-10-10T16:00:00+25:00",
            "2026-10-10T16:00:00*",
            "2026-1a-10T16:00:00Z",
            "2026-10-10X16:00:00Z",
        ] {
            assert_eq!(parse_rfc3339(bad), None, "{bad}");
        }
    }

    #[test]
    fn debug_output_redacts_every_secret_field() {
        use ConnectionSecret as S;
        let s = |v: &str| SecretString::new(v.to_string());
        let secrets = vec![
            S::Bearer {
                token: s("PLAIN-token"),
            },
            S::Basic {
                username: s("PLAIN-user"),
                password: s("PLAIN-pass"),
            },
            S::ApiKeyHeader {
                header: "X-Key".into(),
                value: s("PLAIN-value"),
            },
            S::AwsAccessKey {
                access_key_id: s("PLAIN-akid"),
                secret_access_key: s("PLAIN-sak"),
                session_token: Some(s("PLAIN-session")),
            },
            S::GcpServiceAccount {
                client_email: "svc@x".into(),
                private_key_pem: s("PLAIN-pem"),
                token_uri: Some("https://t".into()),
            },
            S::OAuthAccessToken {
                access_token: s("PLAIN-oauth"),
            },
        ];
        for secret in secrets {
            let out = format!("{secret:?}");
            assert!(!out.contains("PLAIN"), "{out}");
            assert!(out.contains("[REDACTED]"), "{out}");
            let rc = ResolvedConnection {
                connection_id: 1,
                community_id: 2,
                provider: "p".into(),
                kind: ConnectionKind::ExternalHttp,
                endpoint: Endpoint {
                    url: Some("https://h.example/hook/PLAIN-url-token?k=PLAIN-q".into()),
                    region: Some("us-east-1".into()),
                    ..Endpoint::default()
                },
                secret,
                expires_at: None,
                metadata: BTreeMap::new(),
            };
            assert!(!format!("{rc:?}").contains("PLAIN"));
            assert!(format!("{rc:?}").contains("us-east-1"));
            assert!(!format!("{rc:#?}").contains("PLAIN"));
        }
    }
}
