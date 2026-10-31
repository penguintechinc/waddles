//! Shared helpers for the generic WebSocket and poll transports: endpoint
//! validation, per-call secret resolution and handshake-slot substitution.
//! Everything platform-specific (URL, auth slots, cursor shape) arrives via
//! the overlay's config; nothing here names a platform.

use std::collections::HashMap;

use connection_resolver::{ConnectionRef, SecretString};
use sha2::{Digest, Sha256};
use zeroize::Zeroizing;

use super::transport::TransportError;
use super::webhook::{SecretError, WebhookSecretSource};
use super::webhook_verify::hex_encode;

/// Validates a transport endpoint: `https`/`wss` only (plain `http`/`ws` is
/// accepted solely for a loopback host and only when `allow_loopback_insecure`
/// is set -- test harnesses), no embedded userinfo, a host is present.
pub(crate) fn validate_endpoint(
    raw: &str,
    secure_schemes: &[&str],
    insecure_scheme: &str,
    allow_loopback_insecure: bool,
) -> Result<reqwest::Url, String> {
    let url = reqwest::Url::parse(raw).map_err(|_| "endpoint is not a valid URL".to_string())?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err("endpoint must not embed credentials".into());
    }
    let host = url.host_str().ok_or("endpoint has no host")?;
    let scheme = url.scheme();
    if secure_schemes.contains(&scheme) {
        return Ok(url);
    }
    let loopback = matches!(host, "127.0.0.1" | "localhost" | "[::1]");
    if scheme == insecure_scheme && allow_loopback_insecure && loopback {
        return Ok(url);
    }
    Err(format!("endpoint scheme `{scheme}` is not permitted"))
}

/// Opaque `source_id` for a transport instance: a hash of the host-side
/// source key with a family prefix, never the URL, channel or any name.
pub(crate) fn opaque_id(prefix: &str, source_key: &str) -> String {
    let d = Sha256::digest(source_key.as_bytes());
    format!("{prefix}-{}", &hex_encode(&d)[..16])
}

/// Resolves one handle through the secret source, fresh on every call (the
/// source MUST NOT cache; this layer never stores the value).
pub(crate) async fn resolve_handle<S: WebhookSecretSource>(
    secrets: &S,
    handles: &HashMap<String, ConnectionRef>,
    handle: &str,
) -> Result<SecretString, TransportError> {
    let r = handles
        .get(handle)
        .copied()
        .ok_or_else(|| TransportError::Handshake("unknown secret handle".into()))?;
    secrets.resolve(r).await.map_err(|e| match e {
        SecretError::Missing => TransportError::Fatal("secret unavailable for handle".into()),
        SecretError::Unavailable => TransportError::Handshake("secret backend unavailable".into()),
    })
}

/// A secret is spliced into a text frame verbatim, so anything that could
/// break out of a quoted field or inject framing is refused (fail closed).
fn splice_safe(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| !c.is_control() && c != '"' && c != '\\')
}

/// Max `(slot, handle)` pairs per handshake.
pub(crate) const MAX_SECRET_REFS: usize = 8;

/// One uniform failure for every slot/handle problem (unknown handle, absent
/// slot, bad slot shape): the guest-controlled inputs get no oracle.
fn uniform() -> TransportError {
    TransportError::Handshake("handshake secret substitution failed".into())
}

/// Substitutes every `(slot, handle)` pair into `bytes` in ONE pass over the
/// IMMUTABLE original (already-substituted text is never re-scanned). `slot`
/// is the literal placeholder the connector put in the handshake; each must
/// occur at least once, none may be empty, duplicated, or a substring of
/// another slot. Output is a pre-sized zeroizing buffer (no realloc copies);
/// resolved secrets zeroize on drop inside this call.
pub(crate) async fn substitute_slots<S: WebhookSecretSource>(
    secrets: &S,
    handles: &HashMap<String, ConnectionRef>,
    bytes: &[u8],
    refs: &[(String, String)],
) -> Result<Zeroizing<Vec<u8>>, TransportError> {
    if refs.len() > MAX_SECRET_REFS {
        return Err(uniform());
    }
    for (i, (slot, handle)) in refs.iter().enumerate() {
        let bad_shape = slot.is_empty()
            || refs.iter().enumerate().any(|(j, (o, _))| {
                j != i && (o == slot || o.contains(slot.as_str()) || slot.contains(o.as_str()))
            });
        if bad_shape || !handles.contains_key(handle) || count_matches(bytes, slot.as_bytes()) == 0
        {
            return Err(uniform());
        }
    }
    let mut resolved: Vec<(&[u8], SecretString)> = Vec::with_capacity(refs.len());
    let mut extra = 0usize;
    for (slot, handle) in refs {
        let secret = resolve_handle(secrets, handles, handle).await?;
        if !splice_safe(secret.expose()) {
            return Err(TransportError::Fatal("secret not splice-safe".into()));
        }
        extra = extra.saturating_add(
            count_matches(bytes, slot.as_bytes()).saturating_mul(secret.expose().len()),
        );
        resolved.push((slot.as_bytes(), secret));
    }
    let mut out = Zeroizing::new(Vec::with_capacity(bytes.len().saturating_add(extra)));
    let mut i = 0;
    'scan: while i < bytes.len() {
        for (slot, secret) in &resolved {
            if bytes[i..].starts_with(slot) {
                out.extend_from_slice(secret.expose().as_bytes());
                i += slot.len();
                continue 'scan;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    Ok(out)
}

/// Non-overlapping occurrences of `needle` in `hay`.
fn count_matches(hay: &[u8], needle: &[u8]) -> usize {
    let (mut i, mut n) = (0, 0);
    while i < hay.len() {
        if hay[i..].starts_with(needle) {
            n += 1;
            i += needle.len();
        } else {
            i += 1;
        }
    }
    n
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoint_rules() {
        assert!(validate_endpoint("wss://a.example/x", &["wss"], "ws", false).is_ok());
        assert!(validate_endpoint("ws://a.example/x", &["wss"], "ws", true).is_err());
        assert!(validate_endpoint("ws://127.0.0.1:1/x", &["wss"], "ws", false).is_err());
        assert!(validate_endpoint("ws://127.0.0.1:1/x", &["wss"], "ws", true).is_ok());
        assert!(validate_endpoint("wss://u:p@a.example/", &["wss"], "ws", false).is_err());
        assert!(validate_endpoint("nonsense", &["wss"], "ws", false).is_err());
    }

    #[test]
    fn opaque_id_hides_key() {
        let id = opaque_id("ws", "secret-channel-name");
        assert!(id.starts_with("ws-") && !id.contains("channel"));
        assert_eq!(id, opaque_id("ws", "secret-channel-name"));
    }

    #[test]
    fn count_matches_behaviour() {
        assert_eq!(count_matches(b"a{T}b{T}", b"{T}"), 2);
        assert_eq!(count_matches(b"abc", b"{T}"), 0);
    }

    #[test]
    fn splice_safety() {
        assert!(splice_safe("abc.DEF-1_2"));
        for bad in ["", "a\"b", "a\\b", "a\nb"] {
            assert!(!splice_safe(bad));
        }
    }

    use crate::connector::webhook::{BoxFut, SecretError};

    /// Per-ref secret map (handle id -> value), counts resolves.
    struct MapSecrets(HashMap<i64, &'static str>);
    impl WebhookSecretSource for MapSecrets {
        fn resolve(&self, r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
            let v = self.0.get(&r.connection_id).copied();
            Box::pin(async move {
                v.map(|s| SecretString::new(s.into()))
                    .ok_or(SecretError::Missing)
            })
        }
    }

    fn handles() -> HashMap<String, ConnectionRef> {
        ["a", "b"]
            .iter()
            .zip([1i64, 2])
            .map(|(h, id)| {
                (
                    h.to_string(),
                    ConnectionRef {
                        community_id: 1,
                        connection_id: id,
                    },
                )
            })
            .collect()
    }

    fn refs(v: &[(&str, &str)]) -> Vec<(String, String)> {
        v.iter()
            .map(|(a, b)| (a.to_string(), b.to_string()))
            .collect()
    }

    #[tokio::test]
    async fn single_pass_never_rescans_substituted_text() {
        // Secret A contains slot B's text: must stay literal, not be re-expanded.
        let s = MapSecrets(HashMap::from([(1, "x{{B}}y"), (2, "SECOND")]));
        let out = substitute_slots(
            &s,
            &handles(),
            b"{{A}}|{{B}}|{{A}}",
            &refs(&[("{{A}}", "a"), ("{{B}}", "b")]),
        )
        .await
        .unwrap();
        assert_eq!(&out[..], b"x{{B}}y|SECOND|x{{B}}y");
    }

    #[tokio::test]
    async fn bad_slot_shapes_and_uniform_errors() {
        let s = MapSecrets(HashMap::from([(1, "s1"), (2, "s2")]));
        let h = handles();
        let cases: Vec<Vec<(&str, &str)>> = vec![
            vec![("", "a")],
            vec![("{{A}}", "a"), ("{{A}}", "b")],
            vec![("{{A}}", "a"), ("{{A}}x", "b")],
            vec![("{{A}}", "nope")],
            vec![("{{ABSENT}}", "a")],
        ];
        let mut msgs = Vec::new();
        for c in cases {
            let e = substitute_slots(&s, &h, b"{{A}} {{A}}x", &refs(&c))
                .await
                .unwrap_err();
            msgs.push(e);
        }
        assert!(msgs.windows(2).all(|w| w[0] == w[1]), "uniform error");
        let many: Vec<(String, String)> = (0..=MAX_SECRET_REFS)
            .map(|i| (format!("{{{{S{i}}}}}"), "a".into()))
            .collect();
        assert!(substitute_slots(&s, &h, b"x", &many).await.is_err());
    }
}
