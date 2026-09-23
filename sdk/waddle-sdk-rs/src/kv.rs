//! Idiomatic wrapper over the WIT `kv` interface (bundle-scoped key/value,
//! always granted; `wit/waddle-bundle/stage.wit` `interface kv`).

#[cfg(target_arch = "wasm32")]
use serde::Serialize;
#[cfg(target_arch = "wasm32")]
use serde::de::DeserializeOwned;

#[cfg(target_arch = "wasm32")]
use crate::error::SdkError;

/// `ttl_seconds = 0` means "no expiry"; the host clamps to its configured
/// `KV_MAX_TTL_S`, matching the WIT `set` doc comment.
pub const NO_EXPIRY: u32 = 0;

/// Reads `key`. `None` means the key does not exist (not an error).
///
/// Only compiles for `wasm32` targets -- see `crate::bindings_glue`'s
/// module doc comment for the resulting host coverage carve-out.
#[cfg(target_arch = "wasm32")]
pub fn get(key: &str) -> Result<Option<Vec<u8>>, SdkError> {
    crate::bindings_glue::kv_get(key)
}

/// Reads `key` and deserializes it as JSON into `T`.
#[cfg(target_arch = "wasm32")]
pub fn get_json<T: DeserializeOwned>(key: &str) -> Result<Option<T>, SdkError> {
    match get(key)? {
        Some(bytes) => Ok(Some(
            serde_json::from_slice(&bytes).map_err(SdkError::from)?,
        )),
        None => Ok(None),
    }
}

/// Writes `value` under `key` with the given `ttl_seconds` ([`NO_EXPIRY`]
/// for no expiry).
#[cfg(target_arch = "wasm32")]
pub fn set(key: &str, value: &[u8], ttl_seconds: u32) -> Result<(), SdkError> {
    crate::bindings_glue::kv_set(key, value, ttl_seconds)
}

/// Serializes `value` as JSON and writes it under `key`.
#[cfg(target_arch = "wasm32")]
pub fn set_json<T: Serialize>(key: &str, value: &T, ttl_seconds: u32) -> Result<(), SdkError> {
    let bytes = serde_json::to_vec(value).map_err(SdkError::from)?;
    set(key, &bytes, ttl_seconds)
}

/// Deletes `key`. Deleting a missing key is not an error.
#[cfg(target_arch = "wasm32")]
pub fn delete(key: &str) -> Result<(), SdkError> {
    crate::bindings_glue::kv_delete(key)
}

/// Atomically adds `delta` to the integer stored at `key` (creating it at
/// `delta` if absent) and returns the new value.
#[cfg(target_arch = "wasm32")]
pub fn increment(key: &str, delta: i64, ttl_seconds: u32) -> Result<i64, SdkError> {
    crate::bindings_glue::kv_increment(key, delta, ttl_seconds)
}

#[cfg(test)]
mod tests {
    use super::NO_EXPIRY;

    #[test]
    fn no_expiry_is_zero_per_wit_contract() {
        assert_eq!(NO_EXPIRY, 0);
    }
}
