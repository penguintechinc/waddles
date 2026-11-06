//! Object tags: `^[a-z0-9-]{1,32}:[A-Za-z0-9._-]{1,128}$`, <= 8 per object.

use crate::error::StorageError;

pub const MAX_TAGS: usize = 8;
const APP_BUNDLE: &str = "app-bundle";
/// Immutable host-written system tags (`<key>=<value>`); guests can never set them.
pub const TAG_TENANT: &str = "waddles-tenant";
pub const TAG_COMMUNITY: &str = "waddles-community";
/// Provenance only (audit/correlation) -- NOT an access input in v3.0.
pub const TAG_REQUEST_ID: &str = "waddles-request-id";

/// `waddles-<kind>=<value>` system tag.
pub fn system_tag(key: &str, value: impl std::fmt::Display) -> String {
    format!("{key}={value}")
}

/// Request ids are host-assigned; still charset-checked before becoming a tag value.
pub fn validate_request_id(id: &str) -> Result<(), StorageError> {
    if !id.is_empty()
        && id.len() <= 64
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    {
        Ok(())
    } else {
        Err(StorageError::Invalid("bad request id".into()))
    }
}

/// The tag a consumer bundle must carry to be visible to it: `app-bundle:<app_id>`.
pub fn bundle_tag(app_id: &str) -> String {
    format!("{APP_BUNDLE}:{app_id}")
}

/// Validates one tag string.
pub fn validate_tag(tag: &str) -> Result<(), StorageError> {
    if tag.starts_with("waddles-") {
        return Err(StorageError::Invalid("system tags are host-only".into()));
    }
    let Some((k, v)) = tag.split_once(':') else {
        return Err(StorageError::Invalid("tag must be key:value".into()));
    };
    let k_ok = !k.is_empty()
        && k.len() <= 32
        && k.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
    let v_ok = !v.is_empty()
        && v.len() <= 128
        && v.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'));
    if k_ok && v_ok {
        Ok(())
    } else {
        Err(StorageError::Invalid("malformed tag".into()))
    }
}

/// Validates + dedups a producer-supplied tag list.
pub fn normalize_tags(tags: &[String]) -> Result<Vec<String>, StorageError> {
    let mut out: Vec<String> = Vec::new();
    for t in tags {
        validate_tag(t)?;
        if !out.contains(t) {
            out.push(t.clone());
        }
    }
    if out.len() > MAX_TAGS {
        return Err(StorageError::Invalid("too many tags".into()));
    }
    out.sort();
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tag_rules() {
        assert!(validate_tag("app-bundle:waddles.media").is_ok());
        for t in ["", "x", "a:", ":b", "A:b", "a:b c", "a:b:c", "a:b/c"] {
            assert!(validate_tag(t).is_err(), "{t}");
        }
        let many: Vec<String> = (0..9).map(|i| format!("k:v{i}")).collect();
        assert!(normalize_tags(&many).is_err());
        assert_eq!(
            normalize_tags(&["b:1".into(), "a:1".into(), "b:1".into()]).unwrap(),
            vec!["a:1", "b:1"]
        );
        assert_eq!(bundle_tag("x"), "app-bundle:x");
    }
}
