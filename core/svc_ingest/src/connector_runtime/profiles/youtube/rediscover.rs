//! YouTube live-chat rediscovery: the host-side [`Rediscover`] hook and the
//! per-connection override table (the "target update" that changes the plan
//! fingerprint and so restarts the connection cleanly).
//!
//! A stream ending answers the live-chat poll with `403 liveChatEnded`. The
//! poll transport then asks `liveBroadcasts?broadcastStatus=active` (OAuth,
//! same host, same pinned/guarded client as the poll) and hands the body to
//! [`YoutubeRediscover`]. The hook accepts a broadcast only when
//!
//! * its `liveChatId` passes [`is_live_chat_id`],
//! * its `channelId` equals the binding's `channel_id` when the binding pins
//!   one (the API already scopes the list to the credential's own channel), and
//! * it is not the chat that just ended.
//!
//! The accepted id is recorded in [`LiveChatOverrides`], keyed by the numeric
//! connection identity AND the binding's own `live_chat_id`: when hub-api
//! re-pins the binding, the override is dropped and the binding wins. The next
//! reconcile derives the plan with the override applied, so the feed's
//! cross-community claim check (fail closed) sees the NEW chat too.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use serde::Deserialize;

use crate::connector::{Rediscover, RediscoverError, RediscoverOutcome};
use crate::connector_runtime::feed::ConnKey;
use crate::connector_runtime::plan::PlanError;
use crate::outbound_creds_youtube::is_live_chat_id;

/// Host-constant rediscovery endpoint (same host as the poll endpoint).
pub const DISCOVERY_URL: &str = "https://www.googleapis.com/youtube/v3/liveBroadcasts";
/// `part` requested (carries `liveChatId` and `channelId`).
pub const DISCOVERY_PART: &str = "snippet";
/// Broadcasts requested; only the first acceptable one is used.
pub const DISCOVERY_MAX_RESULTS: u32 = 5;
/// Metadata key optionally pinning the bound channel.
pub const META_CHANNEL_ID: &str = "channel_id";
/// Most overrides held (one per connection; bounds memory).
pub const MAX_OVERRIDES: usize = 10_000;

const OUTCOME_NONE: u8 = 0;
const OUTCOME_FOUND: u8 = 1;
const OUTCOME_WAITING: u8 = 2;

/// `^[A-Za-z0-9_-]{1,64}$` -- a YouTube channel id.
#[must_use]
pub fn is_channel_id(s: &str) -> bool {
    (1..=64).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// Validates the optional `channel_id` metadata value.
///
/// # Errors
/// [`PlanError::InvalidValue`] when present but not a channel id.
pub fn channel_of(raw: Option<&String>) -> Result<Option<String>, PlanError> {
    match raw {
        None => Ok(None),
        Some(c) if is_channel_id(c) => Ok(Some(c.clone())),
        Some(_) => Err(PlanError::InvalidValue(META_CHANNEL_ID)),
    }
}

/// The request URL (query pairs percent-encoded by the url crate).
///
/// # Errors
/// [`PlanError::Config`] when the constant is not a URL.
pub fn discovery_url() -> Result<String, PlanError> {
    let mut url = reqwest::Url::parse(DISCOVERY_URL)
        .map_err(|_| PlanError::Config("discovery endpoint constant is not a URL"))?;
    url.query_pairs_mut()
        .append_pair("part", DISCOVERY_PART)
        .append_pair("broadcastStatus", "active")
        .append_pair("maxResults", &DISCOVERY_MAX_RESULTS.to_string());
    Ok(url.into())
}

/// One recorded retarget.
#[derive(Debug)]
struct Override {
    /// The binding's own `live_chat_id` the override was made against.
    base: String,
    /// The rediscovered live chat.
    chat: String,
}

/// Rediscovered live chats, keyed by connection identity.
#[derive(Debug, Default)]
pub struct LiveChatOverrides {
    map: Mutex<HashMap<ConnKey, Override>>,
}

impl LiveChatOverrides {
    /// An empty table.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The map; a poisoned lock still holds a valid map (no invariant spans a
    /// panic), so its guard is recovered and the event logged loudly.
    fn guard(&self) -> MutexGuard<'_, HashMap<ConnKey, Override>> {
        match self.map.lock() {
            Ok(g) => g,
            Err(poisoned) => {
                tracing::error!("live chat override table lock was poisoned; recovering");
                poisoned.into_inner()
            }
        }
    }

    /// The chat to poll for `key`: the rediscovered one while it was made
    /// against this same binding `base`, else `base` (a re-pinned binding
    /// drops the stale override).
    #[must_use]
    pub fn effective(&self, key: &ConnKey, base: &str) -> String {
        let mut g = self.guard();
        match g.get(key) {
            Some(o) if o.base == base => o.chat.clone(),
            Some(_) => {
                g.remove(key);
                base.to_owned()
            }
            None => base.to_owned(),
        }
    }

    /// Records `chat` for `key` against binding `base`.
    pub(super) fn record(
        &self,
        key: &ConnKey,
        base: &str,
        chat: &str,
    ) -> Result<(), RediscoverError> {
        let mut g = self.guard();
        if !g.contains_key(key) && g.len() >= MAX_OVERRIDES {
            tracing::error!("live chat override table is full; refusing a new override");
            return Err(RediscoverError::Refused);
        }
        g.insert(
            key.clone(),
            Override {
                base: base.to_owned(),
                chat: chat.to_owned(),
            },
        );
        Ok(())
    }

    /// Entries held (tests/metrics).
    #[must_use]
    pub fn len(&self) -> usize {
        self.guard().len()
    }

    /// Whether the table is empty.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// `liveBroadcasts.list` response (only the fields used).
#[derive(Debug, Deserialize)]
struct BroadcastList {
    #[serde(default)]
    items: Vec<Broadcast>,
}

#[derive(Debug, Deserialize)]
struct Broadcast {
    snippet: Option<Snippet>,
}

#[derive(Debug, Deserialize)]
struct Snippet {
    #[serde(rename = "liveChatId")]
    live_chat_id: Option<String>,
    #[serde(rename = "channelId")]
    channel_id: Option<String>,
}

/// The per-connection rediscovery hook, built by the profile's `plan`.
#[derive(Debug)]
pub struct YoutubeRediscover {
    key: ConnKey,
    /// The binding's own `live_chat_id`.
    base: String,
    /// The chat currently polled (the one that just ended).
    current: String,
    /// The bound channel, when the binding pins one.
    channel: Option<String>,
    overrides: Arc<LiveChatOverrides>,
    outcome: AtomicU8,
}

impl YoutubeRediscover {
    /// A hook for the connection `key` polling `current`.
    #[must_use]
    pub fn new(
        key: ConnKey,
        base: String,
        current: String,
        channel: Option<String>,
        overrides: Arc<LiveChatOverrides>,
    ) -> Self {
        Self {
            key,
            base,
            current,
            channel,
            overrides,
            outcome: AtomicU8::new(OUTCOME_NONE),
        }
    }

    /// The first acceptable live chat in `list`, if any.
    fn pick<'a>(&self, list: &'a BroadcastList) -> Option<&'a str> {
        list.items.iter().find_map(|b| {
            let s = b.snippet.as_ref()?;
            let chat = s.live_chat_id.as_deref()?;
            let channel_ok = match &self.channel {
                Some(want) => s.channel_id.as_deref() == Some(want.as_str()),
                None => true,
            };
            (channel_ok && is_live_chat_id(chat) && chat != self.current).then_some(chat)
        })
    }
}

impl Rediscover for YoutubeRediscover {
    fn apply(&self, body: &[u8]) -> Result<RediscoverOutcome, RediscoverError> {
        let list: BroadcastList =
            serde_json::from_slice(body).map_err(|_| RediscoverError::Malformed)?;
        match self.pick(&list) {
            Some(chat) => {
                self.overrides.record(&self.key, &self.base, chat)?;
                self.outcome.store(OUTCOME_FOUND, Ordering::SeqCst);
                Ok(RediscoverOutcome::Rediscovered)
            }
            None => {
                self.outcome.store(OUTCOME_WAITING, Ordering::SeqCst);
                Ok(RediscoverOutcome::WaitingForLive)
            }
        }
    }

    fn take_outcome(&self) -> Option<RediscoverOutcome> {
        match self.outcome.swap(OUTCOME_NONE, Ordering::SeqCst) {
            OUTCOME_FOUND => Some(RediscoverOutcome::Rediscovered),
            OUTCOME_WAITING => Some(RediscoverOutcome::WaitingForLive),
            _ => None,
        }
    }
}
