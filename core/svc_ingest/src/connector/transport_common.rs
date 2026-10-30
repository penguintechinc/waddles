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

/// Substitutes every `(slot, handle)` pair into `bytes` as the last step
/// before send. `slot` is the literal placeholder bytes the connector put in
/// the handshake; each must occur at least once. The result is zeroized on
/// drop; resolved secrets drop (and zeroize) inside this call.
pub(crate) async fn substitute_slots<S: WebhookSecretSource>(
    secrets: &S,
    handles: &HashMap<String, ConnectionRef>,
    bytes: &[u8],
    refs: &[(String, String)],
) -> Result<Zeroizing<Vec<u8>>, TransportError> {
    let mut out = Zeroizing::new(bytes.to_vec());
    for (slot, handle) in refs {
        if slot.is_empty() {
            return Err(TransportError::Handshake("empty secret slot".into()));
        }
        let secret = resolve_handle(secrets, handles, handle).await?;
        if !splice_safe(secret.expose()) {
            return Err(TransportError::Fatal("secret not splice-safe".into()));
        }
        let next = replace_all(&out, slot.as_bytes(), secret.expose().as_bytes())
            .ok_or_else(|| TransportError::Handshake("secret slot not present".into()))?;
        out = Zeroizing::new(next);
    }
    Ok(out)
}

/// Replaces every occurrence of `needle`; `None` when there was none.
fn replace_all(hay: &[u8], needle: &[u8], with: &[u8]) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(hay.len());
    let (mut i, mut hit) = (0, false);
    while i < hay.len() {
        if hay[i..].starts_with(needle) {
            out.extend_from_slice(with);
            i += needle.len();
            hit = true;
        } else {
            out.push(hay[i]);
            i += 1;
        }
    }
    hit.then_some(out)
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
    fn replace_all_behaviour() {
        assert_eq!(replace_all(b"a{T}b{T}", b"{T}", b"xy").unwrap(), b"axybxy");
        assert!(replace_all(b"abc", b"{T}", b"x").is_none());
    }

    #[test]
    fn splice_safety() {
        assert!(splice_safe("abc.DEF-1_2"));
        for bad in ["", "a\"b", "a\\b", "a\nb"] {
            assert!(!splice_safe(bad));
        }
    }
}
