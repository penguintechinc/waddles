//! Size / content-type / quota / rate limits.

pub const MAX_OBJECT_BYTES: u64 = 25 * 1024 * 1024;
pub const MAX_SCOPE_BYTES: u64 = 512 * 1024 * 1024;
pub const MAX_SCOPE_OBJECTS: usize = 5000;
pub const MAX_PRESIGN_TTL_SECS: u32 = 300;
pub const MAX_LIST_LIMIT: u32 = 100;
/// Hard cap on backend entries examined by one `list` (tag-hidden ones count).
pub const MAX_LIST_SCAN: usize = 1000;
pub const RATE_OPS_PER_SEC: u32 = 20;

/// Content types a bundle may store (everything else is refused).
pub const ALLOWED_CONTENT_TYPES: &[&str] = &[
    "application/json",
    "application/octet-stream",
    "application/pdf",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/plain",
    "text/csv",
    "video/mp4",
    "video/webm",
];

/// `true` iff `ct` (parameters like `; charset=` ignored) is allowlisted.
pub fn content_type_allowed(ct: &str) -> bool {
    let base = ct
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    ALLOWED_CONTENT_TYPES.contains(&base.as_str())
}
