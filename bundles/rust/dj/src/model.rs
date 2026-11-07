//! Persisted + wire data model for the DJ bundle: tracks, queue items,
//! per-community state, config and audit rows.
//!
//! Status/source/mode/outcome values are int-enum codes (never strings or
//! booleans) per the repo's entity-typing rule; ids that cross a boundary are
//! the requester's community `user_uuid`, never a handle or display name.

use serde::{Deserialize, Serialize};

/// Providers this bundle may consume (one `music-<provider>` service bundle each).
pub const PROVIDERS: [&str; 3] = ["youtube", "spotify", "soundcloud"];

/// Maximum queue length persisted in one kv value (kv values are size-capped).
pub const MAX_QUEUE_HARD: usize = 100;
/// Maximum tracks in one static playlist.
pub const MAX_PLAYLIST_TRACKS: usize = 200;
/// Maximum number of static playlists per community.
pub const MAX_PLAYLISTS: usize = 20;
/// Ring-buffer sizes for history / audit / rejected records.
pub const HISTORY_CAP: usize = 100;
pub const AUDIT_CAP: usize = 100;
pub const REJECTED_CAP: usize = 50;

/// Declares an int-coded enum that (de)serializes as its `u8` code.
macro_rules! int_enum {
    ($(#[$m:meta])* $name:ident { $($variant:ident = $code:literal),+ $(,)? }) => {
        $(#[$m])*
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
        #[serde(try_from = "u8", into = "u8")]
        pub enum $name { $($variant = $code),+ }

        impl From<$name> for u8 {
            fn from(v: $name) -> u8 { v as u8 }
        }

        impl TryFrom<u8> for $name {
            type Error = String;
            fn try_from(v: u8) -> Result<Self, String> {
                match v {
                    $($code => Ok($name::$variant),)+
                    other => Err(format!("invalid {} code {other}", stringify!($name))),
                }
            }
        }
    };
}

int_enum!(
    /// Whether a queue entry is playable or waiting for a moderator.
    Status { Queued = 0, Pending = 1 }
);
int_enum!(
    /// Where a queue entry came from.
    Source { Request = 0, Playlist = 1, Ai = 2 }
);
int_enum!(
    /// Which feed drives playback when nothing is requested.
    Mode { Requests = 0, Playlist = 1, Ai = 2 }
);
int_enum!(
    /// Who made an audited decision.
    DecidedBy { Deterministic = 0, Ai = 1, Moderator = 2 }
);
int_enum!(
    /// Audited outcome code.
    Outcome {
        Enqueued = 0, Held = 1, Rejected = 2, Removed = 3, Approved = 4,
        Restored = 5, Skipped = 6, Banned = 7, Filled = 8,
        PolicyCompiled = 9, PolicyConfirmed = 10
    }
);

/// A provider track, always sanitized by [`Track::sanitized`] before use.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Track {
    pub provider: String,
    pub id: String,
    pub title: String,
    pub artist: String,
    #[serde(default)]
    pub channel_id: Option<String>,
    pub duration_s: u32,
    #[serde(default)]
    pub explicit: bool,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub license: Option<String>,
}

/// Strips control chars, collapses whitespace and caps the length (in chars).
pub fn clean_text(raw: &str, max_chars: usize) -> String {
    let mut out = String::new();
    let mut last_space = true;
    for c in raw.chars() {
        let c = if c.is_control() || c.is_whitespace() {
            ' '
        } else {
            c
        };
        if c == ' ' {
            if last_space {
                continue;
            }
            last_space = true;
        } else {
            last_space = false;
        }
        out.push(c);
        if out.chars().count() >= max_chars {
            break;
        }
    }
    out.trim().to_string()
}

impl Track {
    /// Validates and normalizes an untrusted provider track. `None` means the
    /// record is unusable (unknown provider, bad id, empty title) and must be dropped.
    pub fn sanitized(mut self, expected_provider: &str) -> Option<Track> {
        if self.provider != expected_provider || !PROVIDERS.contains(&self.provider.as_str()) {
            return None;
        }
        let id_ok = !self.id.is_empty()
            && self.id.len() <= 128
            && self
                .id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "_-.:/".contains(c));
        if !id_ok {
            return None;
        }
        self.title = clean_text(&self.title, 200);
        self.artist = clean_text(&self.artist, 120);
        if self.title.is_empty() {
            return None;
        }
        self.channel_id = self
            .channel_id
            .map(|c| clean_text(&c, 128))
            .filter(|c| !c.is_empty());
        self.license = self
            .license
            .map(|c| clean_text(&c, 64))
            .filter(|c| !c.is_empty());
        self.url = self.url.filter(|u| {
            u.len() <= 300
                && u.starts_with("https://")
                && !u.chars().any(|c| c.is_control() || c.is_whitespace())
        });
        Some(self)
    }

    /// Stable dedupe / ban key.
    pub fn key(&self) -> String {
        format!("{}/{}", self.provider, self.id)
    }
}

/// One queue entry.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Item {
    pub id: u64,
    pub track: Track,
    /// Requester community `user_uuid` (None for playlist / AI sourced entries).
    #[serde(default)]
    pub requester: Option<String>,
    pub status: Status,
    pub source: Source,
    pub added_ms: u64,
    /// Human-readable hold reason shown to moderators (Pending only).
    #[serde(default)]
    pub reason: Option<String>,
    /// Request cost charged for this entry (refunded if it is removed unplayed).
    #[serde(default)]
    pub paid: u64,
}

/// A recently played track (replay-cooldown input).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Played {
    pub key: String,
    pub ms: u64,
}

/// A removed/rejected request kept so a moderator can restore it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Rejected {
    pub item: Item,
    pub rule: String,
}

/// Per-community mutable state, one kv value.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct State {
    pub next_id: u64,
    pub queue: Vec<Item>,
    pub now: Option<Item>,
    pub history: Vec<Played>,
    /// Banned track keys (`provider/id`).
    pub banned: Vec<String>,
    pub rejected: Vec<Rejected>,
    /// Round-robin cursor into the active playlist.
    pub cursor: usize,
    /// AI-fill rate window (start ms, count).
    pub fill_window_ms: u64,
    pub fill_count: u32,
}

/// One audit-log row. PII-free: numeric/opaque ids, rule id, outcome code only.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditRow {
    pub ts: u64,
    pub req_id: u64,
    pub rule: String,
    pub decided_by: DecidedBy,
    pub outcome: Outcome,
    #[serde(default)]
    pub actor: Option<String>,
}

/// Public read model consumed by the Music Station overlay.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PublicTrack {
    pub id: u64,
    pub provider: String,
    pub track_id: String,
    pub title: String,
    pub artist: String,
    pub duration_s: u32,
    pub url: Option<String>,
}

/// The overlay-facing snapshot (`dj.public_state`): no requester ids, no pending items.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PublicState {
    pub now_playing: Option<PublicTrack>,
    pub queue: Vec<PublicTrack>,
    pub updated_ms: u64,
}

impl From<&Item> for PublicTrack {
    fn from(i: &Item) -> Self {
        PublicTrack {
            id: i.id,
            provider: i.track.provider.clone(),
            track_id: i.track.id.clone(),
            title: i.track.title.clone(),
            artist: i.track.artist.clone(),
            duration_s: i.track.duration_s,
            url: i.track.url.clone(),
        }
    }
}
