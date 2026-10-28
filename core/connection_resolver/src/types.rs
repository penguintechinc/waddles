//! Public value types of the resolver contract and the wire DTOs they are parsed from.

use std::collections::{BTreeMap, HashMap};

use serde::Deserialize;

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
#[derive(Clone, PartialEq, Eq, Default, Deserialize)]
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

// ---- wire DTOs (hub-api resolve v2 / data-plane list; unknown fields ignored) ----

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ResolveWire {
    community_id: i64,
    connection_id: i64,
    provider: String,
    kind: i16,
    access_token: Option<SecretString>,
    expires_at: Option<String>,
    #[serde(default)]
    endpoint: Endpoint,
    secret: Option<SecretWire>,
}

#[derive(Deserialize)]
struct SecretWire {
    scheme: i16,
    #[serde(default)]
    fields: HashMap<String, Option<SecretString>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ListWire {
    #[serde(default)]
    items: Vec<BindingWire>,
    next_after: Option<i64>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct BindingWire {
    community_id: i64,
    connection_id: i64,
    provider: String,
    #[serde(default = "default_kind")]
    kind: i16,
    #[serde(default)]
    metadata: BTreeMap<String, String>,
}

fn default_kind() -> i16 {
    ConnectionKind::PlatformOauth.code()
}

/// Remove a required secret field or fail `Malformed`.
fn take(
    fields: &mut HashMap<String, Option<SecretString>>,
    key: &str,
) -> Result<SecretString, ResolveError> {
    fields
        .remove(key)
        .flatten()
        .ok_or(ResolveError::Malformed("secret field missing"))
}

/// Remove an optional secret field (absent or null -> `None`).
fn take_opt(fields: &mut HashMap<String, Option<SecretString>>, key: &str) -> Option<SecretString> {
    fields.remove(key).flatten()
}

/// Remove a required non-secret field as a plain `String`.
fn take_plain(
    fields: &mut HashMap<String, Option<SecretString>>,
    key: &str,
) -> Result<String, ResolveError> {
    take(fields, key).map(|s| s.expose().to_owned())
}

/// Map the wire secret document to a [`ConnectionSecret`] by scheme code.
fn map_secret(wire: SecretWire) -> Result<ConnectionSecret, ResolveError> {
    let mut f = wire.fields;
    match wire.scheme {
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

/// Parse a 200 resolve body into a [`ResolvedConnection`], enforcing that it answers `want`.
pub(crate) fn parse_resolved(
    body: &[u8],
    want: crate::ConnectionRef,
) -> Result<ResolvedConnection, ResolveError> {
    let wire: ResolveWire = serde_json::from_slice(body)
        .map_err(|_| ResolveError::Malformed("invalid resolve body"))?;
    if wire.community_id != want.community_id || wire.connection_id != want.connection_id {
        return Err(ResolveError::Malformed(
            "resolve body is for another connection",
        ));
    }
    let kind = ConnectionKind::from_code(wire.kind)?;
    let secret = match (wire.access_token, wire.secret) {
        (Some(access_token), None) if kind == ConnectionKind::PlatformOauth => {
            ConnectionSecret::OAuthAccessToken { access_token }
        }
        (None, Some(secret)) if kind != ConnectionKind::PlatformOauth => {
            let mapped = map_secret(secret)?;
            if !scheme_fits_kind(kind, &mapped) {
                return Err(ResolveError::Malformed("secret scheme does not match kind"));
            }
            mapped
        }
        _ => return Err(ResolveError::Malformed("credential does not match kind")),
    };
    let expires_at = match wire.expires_at {
        None => None,
        Some(s) => Some(parse_rfc3339(&s).ok_or(ResolveError::Malformed("invalid expiresAt"))?),
    };
    Ok(ResolvedConnection {
        connection_id: wire.connection_id,
        community_id: wire.community_id,
        provider: wire.provider,
        kind,
        endpoint: wire.endpoint,
        secret,
        expires_at,
    })
}

/// Parse a 200 list body into a [`BindingPage`].
pub(crate) fn parse_page(body: &[u8]) -> Result<BindingPage, ResolveError> {
    let wire: ListWire =
        serde_json::from_slice(body).map_err(|_| ResolveError::Malformed("invalid list body"))?;
    let items = wire
        .items
        .into_iter()
        .map(|b| {
            Ok(BindingSummary {
                community_id: b.community_id,
                connection_id: b.connection_id,
                provider: b.provider,
                kind: ConnectionKind::from_code(b.kind)?,
                metadata: b.metadata,
            })
        })
        .collect::<Result<Vec<_>, ResolveError>>()?;
    Ok(BindingPage {
        items,
        next_after: wire.next_after,
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
    }

    fn resolved_body(kind: i64, secret: &str, token: &str) -> Vec<u8> {
        format!(
            r#"{{"success":true,"communityId":7,"connectionId":12,"provider":"p","kind":{kind},
            "accessToken":{token},"expiresAt":null,"endpoint":{{}},"metadata":{{}},"secret":{secret}}}"#
        )
        .into_bytes()
    }

    const REF: crate::ConnectionRef = crate::ConnectionRef {
        community_id: 7,
        connection_id: 12,
    };

    #[test]
    fn unknown_kind_or_scheme_code_is_malformed() {
        let bad_kind = resolved_body(9, "null", "\"t\"");
        assert!(matches!(
            parse_resolved(&bad_kind, REF),
            Err(ResolveError::Malformed("unknown connection kind"))
        ));
        let bad_scheme = resolved_body(2, r#"{"scheme":9,"fields":{}}"#, "null");
        assert!(matches!(
            parse_resolved(&bad_scheme, REF),
            Err(ResolveError::Malformed("unknown secret scheme"))
        ));
    }

    #[test]
    fn every_scheme_maps_and_missing_fields_are_malformed() {
        let cases = [
            (r#"{"scheme":1,"fields":{"token":"a"}}"#, true),
            (
                r#"{"scheme":2,"fields":{"username":"u","password":"p"}}"#,
                true,
            ),
            (
                r#"{"scheme":3,"fields":{"header":"X-K","value":"v"}}"#,
                true,
            ),
            (
                r#"{"scheme":4,"fields":{"accessKeyId":"a","secretAccessKey":"s","sessionToken":"t"}}"#,
                true,
            ),
            (
                r#"{"scheme":5,"fields":{"clientEmail":"e","privateKey":"k","tokenUri":"u"}}"#,
                true,
            ),
            (r#"{"scheme":1,"fields":{}}"#, false),
            (r#"{"scheme":2,"fields":{"username":"u"}}"#, false),
            (
                r#"{"scheme":3,"fields":{"header":null,"value":"v"}}"#,
                false,
            ),
            (r#"{"scheme":4,"fields":{"accessKeyId":"a"}}"#, false),
            (r#"{"scheme":5,"fields":{"clientEmail":"e"}}"#, false),
        ];
        for (secret, ok) in cases {
            let kind = if secret.contains("\"scheme\":4") {
                3
            } else if secret.contains("\"scheme\":5") {
                4
            } else {
                2
            };
            let r = parse_resolved(&resolved_body(kind, secret, "null"), REF);
            assert_eq!(r.is_ok(), ok, "{secret}: {r:?}");
        }
    }

    #[test]
    fn kind_and_scheme_must_agree() {
        let schemes = [
            r#"{"scheme":1,"fields":{"token":"a"}}"#,
            r#"{"scheme":2,"fields":{"username":"u","password":"p"}}"#,
            r#"{"scheme":3,"fields":{"header":"X-K","value":"v"}}"#,
            r#"{"scheme":4,"fields":{"accessKeyId":"a","secretAccessKey":"s"}}"#,
            r#"{"scheme":5,"fields":{"clientEmail":"e","privateKey":"k"}}"#,
        ];
        // allowed[kind-2][scheme-1]
        let allowed = [
            [true, true, true, false, false],
            [false, false, false, true, false],
            [false, false, false, false, true],
        ];
        for (ki, row) in allowed.iter().enumerate() {
            for (si, ok) in row.iter().enumerate() {
                let kind = ki as i64 + 2;
                let r = parse_resolved(&resolved_body(kind, schemes[si], "null"), REF);
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
        // kind 1 without accessToken, kind 2 with accessToken only, both present, mismatched ids.
        assert!(parse_resolved(&resolved_body(1, "null", "null"), REF).is_err());
        assert!(parse_resolved(&resolved_body(2, "null", "\"t\""), REF).is_err());
        let both = resolved_body(1, r#"{"scheme":1,"fields":{"token":"a"}}"#, "\"t\"");
        assert!(parse_resolved(&both, REF).is_err());
        let other = crate::ConnectionRef {
            community_id: 8,
            connection_id: 12,
        };
        assert!(parse_resolved(&resolved_body(1, "null", "\"t\""), other).is_err());
        assert!(parse_resolved(b"not json", REF).is_err());
    }

    #[test]
    fn list_page_parses_and_defaults_kind() {
        let body = br#"{"success":true,"items":[{"communityId":1,"connectionId":2,"tenantId":3,
            "provider":"twitch","metadata":{"a":"b"}},{"communityId":1,"connectionId":3,"kind":2,
            "provider":"x","metadata":{}}],"nextAfter":9}"#;
        let page = parse_page(body).unwrap();
        assert_eq!(page.items.len(), 2);
        assert_eq!(page.items[0].kind, ConnectionKind::PlatformOauth);
        assert_eq!(page.items[1].kind, ConnectionKind::ExternalHttp);
        assert_eq!(page.next_after, Some(9));
        let bad = br#"{"items":[{"communityId":1,"connectionId":2,"provider":"x","kind":9}]}"#;
        assert!(parse_page(bad).is_err());
        assert!(parse_page(b"{").is_err());
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
            };
            assert!(!format!("{rc:?}").contains("PLAIN"));
            assert!(format!("{rc:?}").contains("us-east-1"));
            assert!(!format!("{rc:#?}").contains("PLAIN"));
        }
    }
}
