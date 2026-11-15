//! The DJ engine: storage, audit, the policy pipeline, old-queue migration and
//! the AI DJ planner over the shared music-requests queue. Generic over [`Host`],
//! so the identical code runs in the component and natively under test.
//!
//! The DJ owns NO request queue: it reads the shared queue (`requests.list`),
//! adds `kind: "dj"` picks (`requests.add`) and reads/sets the single stored
//! AI DJ switch (`dj.get`/`dj.set`). User-facing denials are `Ok(reply text)`;
//! only infrastructure failures (kv down, corrupt state) are `Err(DjError)`.

use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

use crate::ai;
use crate::host::{Host, Level};
use crate::model::*;
use crate::music;
use crate::policy::{self, Config, Violation};
use crate::requests::{self, ReqError};

pub const KEY_CONFIG: &str = "dj.config";
pub const KEY_STATE: &str = "dj.state";
pub const KEY_AUDIT: &str = "dj.audit";
/// Set once the pre-1.1.0 kv queue has been exported to the service.
pub const KEY_MIGRATED: &str = "dj.migrated";
/// Old overlay snapshot key (deleted by the migration; music owns the overlay now).
const KEY_OLD_PUBLIC: &str = "dj.public_state";

/// Infrastructure failure (never a user mistake).
#[derive(Debug, thiserror::Error)]
pub enum DjError {
    #[error("storage failure: {0}")]
    Storage(String),
    #[error("corrupt stored value at {0}")]
    Corrupt(String),
}

/// Per-invocation caller facts. `actor` is the host-resolved community
/// `user_uuid`; `is_mod` comes from the platform's own role flags.
#[derive(Debug, Clone, Default)]
pub struct Ctx {
    pub actor: Option<String>,
    pub is_mod: bool,
    pub message_id: String,
}

/// Outcome of the full policy pipeline for one candidate track.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    Allow,
    Hold {
        rule: String,
        reason: String,
        by: DecidedBy,
    },
    Reject(Violation),
}

/// The engine bound to one host + caller.
pub struct Engine<'a> {
    pub host: &'a dyn Host,
    pub ctx: &'a Ctx,
}

pub type Reply = Result<String, DjError>;

/// `m:ss` duration.
pub fn fmt_dur(s: u32) -> String {
    format!("{}:{:02}", s / 60, s % 60)
}

fn fmt_track(t: &Track) -> String {
    format!("{} - {} [{}]", t.title, t.artist, fmt_dur(t.duration_s))
}

/// Hard cap on `requests.add` calls per plan (bounds DMCA-safe retries).
const MAX_ADD_ATTEMPTS: u32 = 8;

/// Per-plan working set: what the shared queue holds (dedupe keys), the DJ's own queued
/// titles, the service's DMCA-safe flag and the bounded-attempt counters.
#[derive(Default)]
struct Live {
    keys: Vec<String>,
    titles: Vec<String>,
    safe: bool,
    attempts: u32,
    unsafe_skipped: u32,
}

/// The community's play history as hints (`music.stats.top`): normalized top artists
/// (most played first) and top `(title, artist)` songs.
#[derive(Default)]
struct Stats {
    artists: Vec<String>,
    songs: Vec<(String, String)>,
}

impl Stats {
    /// Sort key: position among the top artists, unknown artists last.
    fn rank(&self, artist: &str) -> usize {
        let n = policy::norm(artist);
        self.artists
            .iter()
            .position(|a| *a == n)
            .unwrap_or(usize::MAX)
    }
}

impl<'a> Engine<'a> {
    pub fn new(host: &'a dyn Host, ctx: &'a Ctx) -> Self {
        Engine { host, ctx }
    }

    // ---- storage -------------------------------------------------------

    pub(crate) fn load<T: DeserializeOwned + Default>(&self, key: &str) -> Result<T, DjError> {
        match self.host.kv_get(key).map_err(DjError::Storage)? {
            None => Ok(T::default()),
            Some(bytes) => {
                serde_json::from_slice(&bytes).map_err(|_| DjError::Corrupt(key.to_string()))
            }
        }
    }

    pub(crate) fn save<T: Serialize>(&self, key: &str, v: &T, ttl: u32) -> Result<(), DjError> {
        let bytes = serde_json::to_vec(v).map_err(|e| DjError::Storage(e.to_string()))?;
        self.host.kv_set(key, &bytes, ttl).map_err(DjError::Storage)
    }

    pub fn config(&self) -> Result<Config, DjError> {
        self.load(KEY_CONFIG)
    }

    pub fn state(&self) -> Result<State, DjError> {
        self.load(KEY_STATE)
    }

    /// Persists the DJ state (bans, recent picks, cursors).
    pub(crate) fn save_state(&self, st: &State) -> Result<(), DjError> {
        self.save(KEY_STATE, st, 0)
    }

    /// Appends one PII-free audit row and a matching log line.
    pub(crate) fn audit(
        &self,
        req_id: u64,
        rule: &str,
        by: DecidedBy,
        outcome: Outcome,
    ) -> Result<(), DjError> {
        let mut rows: Vec<AuditRow> = self.load(KEY_AUDIT)?;
        rows.push(AuditRow {
            ts: self.host.now_ms(),
            req_id,
            rule: rule.to_string(),
            decided_by: by,
            outcome,
            actor: self.ctx.actor.clone(),
        });
        if rows.len() > AUDIT_CAP {
            let drop = rows.len() - AUDIT_CAP;
            rows.drain(..drop);
        }
        self.save(KEY_AUDIT, &rows, 0)?;
        self.host.log(Level::Info, "dj decision", json!({"req_id": req_id, "rule": rule, "decided_by": u8::from(by), "outcome": u8::from(outcome)}));
        Ok(())
    }

    // ---- dispatch ------------------------------------------------------

    /// Routes one chat line. `Ok(None)` = not a DJ command. Only `!dj` exists:
    /// viewer request commands belong to the music bundle.
    pub fn handle(&self, text: &str) -> Result<Option<String>, DjError> {
        let text = text.trim();
        let first = match text.split_whitespace().next() {
            Some(f) => f,
            None => return Ok(None),
        };
        let args = text[first.len()..].trim();
        match first.to_ascii_lowercase().as_str() {
            "!dj" => {
                self.migrate_old_queue()?;
                self.dj(args).map(Some)
            }
            _ => Ok(None),
        }
    }

    pub(crate) fn need_mod(&self) -> Option<String> {
        if self.ctx.is_mod {
            None
        } else {
            Some("only moderators can do that".to_string())
        }
    }

    // ---- policy pipeline ----------------------------------------------

    /// Deterministic policy, then the optional AI judge for fuzzy rules. AI failure
    /// or low confidence HOLDS: the pick is not made and the decision is audited.
    pub fn decide(&self, cfg: &Config, st: &State, track: &Track, live: &[String]) -> Decision {
        if let Some(v) = policy::evaluate(cfg, st, track, self.host.now_ms(), live) {
            return Decision::Reject(v);
        }
        let meta = format!(
            "{} - {} (channel: {}) provider: {}",
            track.title,
            track.artist,
            track.channel_id.as_deref().unwrap_or("unknown"),
            track.provider
        );
        for rule in policy::fuzzy_rules(cfg) {
            match ai::judge(self.host, &meta, &rule) {
                Ok((violates, conf)) if conf >= ai::MIN_CONFIDENCE => {
                    if violates {
                        let reason = format!("does not fit the channel rules ({rule})");
                        return Decision::Reject(Violation {
                            rule: "ai_fuzzy",
                            reason,
                            decided_by: DecidedBy::Ai,
                        });
                    }
                }
                Ok(_) => {
                    return Decision::Hold {
                        rule: "ai_low_confidence".into(),
                        reason: "needs moderator review".into(),
                        by: DecidedBy::Ai,
                    };
                }
                Err(_) => {
                    return Decision::Hold {
                        rule: "ai_unavailable".into(),
                        reason: "AI check unavailable, needs moderator review".into(),
                        by: DecidedBy::Ai,
                    };
                }
            }
        }
        Decision::Allow
    }

    // ---- old-queue migration ----------------------------------------------

    /// One-shot, per community: exports the pre-1.1.0 kv queue to the service via
    /// `requests.import`, then drops it from `dj.state` and sets `dj.migrated`. An
    /// "already imported" refusal counts as done; any other failure keeps the old
    /// queue and retries on the next invocation. Never fails the command itself.
    pub(crate) fn migrate_old_queue(&self) -> Result<(), DjError> {
        if self
            .host
            .kv_get(KEY_MIGRATED)
            .map_err(DjError::Storage)?
            .is_some()
        {
            return Ok(());
        }
        let mut raw: Value = match self.host.kv_get(KEY_STATE).map_err(DjError::Storage)? {
            None => json!({}),
            Some(b) => {
                serde_json::from_slice(&b).map_err(|_| DjError::Corrupt(KEY_STATE.to_string()))?
            }
        };
        let items = old_queue_items(&raw);
        if !items.is_empty() {
            match requests::import(self.host, &items, "dj.migrate.queue") {
                Ok(n) => self.host.log(
                    Level::Info,
                    "dj old queue migrated",
                    json!({"imported": n, "old": items.len()}),
                ),
                Err(e) if requests::import_already_done(&e) => self.host.log(
                    Level::Info,
                    "dj old queue already imported",
                    json!({"old": items.len()}),
                ),
                Err(e) => {
                    self.host.log(
                        Level::Warn,
                        "dj old queue migration deferred",
                        json!({"error": e.to_string()}),
                    );
                    return Ok(());
                }
            }
        }
        if let Some(o) = raw.as_object_mut() {
            for k in ["queue", "now", "rejected", "next_id"] {
                o.remove(k);
            }
            let bytes = serde_json::to_vec(&raw).map_err(|e| DjError::Storage(e.to_string()))?;
            self.host
                .kv_set(KEY_STATE, &bytes, 0)
                .map_err(DjError::Storage)?;
        }
        self.host
            .kv_delete(KEY_OLD_PUBLIC)
            .map_err(DjError::Storage)?;
        self.host
            .kv_set(KEY_MIGRATED, b"1", 0)
            .map_err(DjError::Storage)
    }

    // ---- planner (fill / next) -------------------------------------------

    /// Plans DJ picks from the SHARED queue. Every invocation re-reads the queue
    /// (`advance_due` then `list`) before deciding; a periodic hook is still a host gap.
    /// When the service says the AI DJ is off (or the DJ is not granted) NO pick is made.
    /// The service's DMCA-safe flag is read here each time; stats weight the candidates.
    pub fn plan(&self, cfg: &Config) -> Reply {
        const OFF: &str = "the AI DJ is off, so no picks were made (turn it on with !dj ai on)";
        let on = match requests::dj_get(self.host) {
            Ok(b) => b,
            Err(e) => return Ok(e.user_msg()),
        };
        if !on {
            return Ok(OFF.into());
        }
        if let Err(e) = requests::advance_due(self.host) {
            return Ok(e.user_msg());
        }
        let snap = match requests::list(self.host, 100) {
            Ok(s) => s,
            Err(e) => return Ok(e.user_msg()),
        };
        if !snap.ai_dj_on {
            return Ok(OFF.into());
        }
        let viewer = snap.queue.iter().filter(|i| i.kind != "dj").count() as u32;
        let dj_queued = snap.queue.iter().filter(|i| i.kind == "dj").count() as u32;
        if viewer >= cfg.dj_min_queue {
            return Ok(format!(
                "{viewer} viewer requests are queued (the DJ steps in below {}); no pick needed",
                cfg.dj_min_queue
            ));
        }
        let want = cfg.dj_min_queue - viewer;
        // Interleave: after N viewer requests the DJ may hold one more pick; with fewer
        // it may still hold one so an idle queue is never silent. Viewer requests are
        // never displaced: picks are only ever appended.
        let allowed = (viewer / cfg.dj_requests_per_pick.max(1)).max(1);
        let picks = want.min(allowed.saturating_sub(dj_queued));
        if picks == 0 {
            return Ok(format!(
                "the queue already has {dj_queued} DJ pick(s) for {viewer} viewer request(s); waiting"
            ));
        }
        // Dedupe against everything queued/playing; only the DJ's OWN titles may reach the
        // model as "avoid" (viewer-influenced text never reaches ai.complete).
        let mut live = Live {
            safe: snap.dmca_safe,
            ..Live::default()
        };
        for i in snap.queue.iter().chain(snap.now_playing.iter()) {
            live.keys.push(policy::live_key(&i.title, &i.artist));
            if i.kind == "dj" {
                live.titles.push(i.title.clone());
            }
        }
        let stats = self.load_stats();
        let mut st = self.state()?;
        let mut lines: Vec<String> = Vec::new();
        let mut stop = String::new();
        let mut info = String::new();
        if cfg.mode == Mode::Playlist {
            stop = self.feed_playlist(cfg, &mut st, &mut live, &stats, picks, &mut lines)?;
        }
        if (lines.len() as u32) < picks && stop.is_empty() {
            let want_ai = picks - lines.len() as u32;
            match self.feed_ai(cfg, &mut st, &mut live, &stats, want_ai, &mut lines) {
                Ok(n) => stop = n,
                Err(msg) => info = format!(" (AI DJ: {msg})"),
            }
        }
        if (lines.len() as u32) < picks && stop.is_empty() {
            let want_s = picks - lines.len() as u32;
            stop = self.feed_stats(cfg, &mut st, &mut live, &stats, want_s, &mut lines);
        }
        self.save_state(&st)?;
        let skipped = if live.unsafe_skipped > 0 {
            format!(" ({} skipped by DMCA-safe mode)", live.unsafe_skipped)
        } else {
            String::new()
        };
        self.host.log(
            Level::Info,
            "dj plan",
            json!({"viewer": viewer, "dj_queued": dj_queued, "added": lines.len(),
                   "attempts": live.attempts, "unsafe_skipped": live.unsafe_skipped}),
        );
        Ok(if lines.is_empty() {
            format!("the DJ found nothing to add{stop}{info}{skipped}")
        } else {
            format!("DJ picked: {}{stop}{info}{skipped}", lines.join(" | "))
        })
    }

    /// Community history as DJ hints: top artists and songs (aggregate counts only).
    /// A missing grant or outage just means no weighting.
    fn load_stats(&self) -> Stats {
        let artists = requests::stats_top(self.host, "artist", 10)
            .map(|r| r.into_iter().map(|a| policy::norm(&a.artist)).collect())
            .unwrap_or_default();
        let songs = requests::stats_top(self.host, "song", 10)
            .map(|r| {
                r.into_iter()
                    .filter_map(|s| Some((s.title?, s.artist)))
                    .collect()
            })
            .unwrap_or_default();
        Stats { artists, songs }
    }

    /// Adds one policy-approved track to the shared queue as `kind: "dj"`.
    /// `Ok(Some(line))` = added, `Ok(None)` = skipped, `Err(note)` = stop feeding.
    /// Bounded: at most [`MAX_ADD_ATTEMPTS`] add calls per plan, so a DMCA-safe
    /// community full of unsafe candidates can never loop.
    fn push_pick(
        &self,
        st: &mut State,
        live: &mut Live,
        track: Track,
        by: DecidedBy,
        rule: &str,
    ) -> Result<Option<String>, String> {
        if live.safe && track.url.is_none() {
            // The service derives the provider from source_url; no url cannot be safe-listed.
            live.unsafe_skipped += 1;
            return Ok(None);
        }
        if live.attempts >= MAX_ADD_ATTEMPTS {
            return Err(format!(" (stopped after {MAX_ADD_ATTEMPTS} attempts)"));
        }
        live.attempts += 1;
        let idem = format!("dj.add.{}.{}", self.ctx.message_id, live.attempts);
        match requests::add_dj(self.host, &track, &idem) {
            Ok(pos) => {
                live.keys
                    .push(policy::live_key(&track.title, &track.artist));
                live.titles.push(track.title.clone());
                st.history.push(Played {
                    key: track.key(),
                    ms: self.host.now_ms(),
                });
                if st.history.len() > HISTORY_CAP {
                    st.history.remove(0);
                }
                let _ = self.audit(0, rule, by, Outcome::Filled);
                Ok(Some(format!("{} (position {pos})", fmt_track(&track))))
            }
            Err(ReqError::Rejected(c)) if c == "queue_full" => Err(" (queue is full)".into()),
            Err(e) if requests::is_not_dmca_safe(&e) => {
                live.unsafe_skipped += 1;
                let _ = self.audit(0, "not_dmca_safe", by, Outcome::Rejected);
                Ok(None)
            }
            Err(ReqError::Rejected(c)) if c == "duplicate" || c == "invalid_url" => Ok(None),
            Err(e) => Err(format!(" ({})", e.user_msg())),
        }
    }

    /// Feeds picks from the active playlist (static checks: bans, deny-lists, duration,
    /// explicit, replay cooldown on prior DJ picks, live-queue duplicates). Tracks by the
    /// community's most played artists are tried first; the rotation cursor still advances.
    fn feed_playlist(
        &self,
        cfg: &Config,
        st: &mut State,
        live: &mut Live,
        stats: &Stats,
        picks: u32,
        lines: &mut Vec<String>,
    ) -> Result<String, DjError> {
        let Some(name) = cfg.active_playlist.clone() else {
            return Ok(String::new());
        };
        let tracks = self.playlist_tracks(&name)?;
        if tracks.is_empty() {
            return Ok(String::new());
        }
        let start = st.cursor % tracks.len();
        let mut order: Vec<usize> = (0..tracks.len())
            .map(|i| (start + i) % tracks.len())
            .collect();
        order.sort_by_key(|i| stats.rank(&tracks[*i].artist));
        for i in order {
            if lines.len() as u32 >= picks {
                break;
            }
            let t = tracks[i].clone();
            st.cursor = (i + 1) % tracks.len();
            if policy::evaluate(cfg, st, &t, self.host.now_ms(), &live.keys).is_some() {
                continue;
            }
            match self.push_pick(st, live, t, DecidedBy::Deterministic, "playlist_pick") {
                Ok(Some(l)) => lines.push(l),
                Ok(None) => {}
                Err(note) => return Ok(note),
            }
        }
        Ok(String::new())
    }

    /// Feeds AI-suggested picks (candidates by the community's top artists first), resolved
    /// through real provider searches and pushed through the same policy. Returns a stop
    /// note, or a user-facing failure reason. Stats never go to the model.
    fn feed_ai(
        &self,
        cfg: &Config,
        st: &mut State,
        live: &mut Live,
        stats: &Stats,
        want: u32,
        lines: &mut Vec<String>,
    ) -> Result<String, String> {
        let now = self.host.now_ms();
        if now.saturating_sub(st.fill_window_ms) >= 3_600_000 {
            st.fill_window_ms = now;
            st.fill_count = 0;
        }
        let room = cfg.ai.hourly_cap.saturating_sub(st.fill_count);
        if room == 0 {
            return Err("hourly limit reached".into());
        }
        // Ask for a full batch (spare candidates for policy rejections); stop at `want` adds.
        let want = want.min(room);
        let n = cfg.ai.batch.max(want).min(room);
        let mut hints = ai::suggest(
            self.host,
            &cfg.ai.genre,
            &cfg.ai.instructions,
            n,
            &live.titles,
        )
        .map_err(|e| e.to_string())?;
        hints.sort_by_key(|(_, artist)| stats.rank(artist));
        let before = lines.len();
        let stop = self.feed_hints(cfg, st, live, hints, want, "ai_fill", DecidedBy::Ai, lines);
        st.fill_count += (lines.len() - before) as u32;
        Ok(stop)
    }

    /// Fills remaining gaps with the community's own most played songs (never replaying
    /// inside the replay cooldown or what is already queued).
    fn feed_stats(
        &self,
        cfg: &Config,
        st: &mut State,
        live: &mut Live,
        stats: &Stats,
        want: u32,
        lines: &mut Vec<String>,
    ) -> String {
        self.feed_hints(
            cfg,
            st,
            live,
            stats.songs.clone(),
            want,
            "stats_fill",
            DecidedBy::Deterministic,
            lines,
        )
    }

    /// Resolves each `(title, artist)` hint through a real search, runs the policy, and adds
    /// until `want` lines exist. Returns a stop note ("" = ran to the end).
    #[allow(clippy::too_many_arguments)]
    fn feed_hints(
        &self,
        cfg: &Config,
        st: &mut State,
        live: &mut Live,
        hints: Vec<(String, String)>,
        want: u32,
        rule: &str,
        by: DecidedBy,
        lines: &mut Vec<String>,
    ) -> String {
        let goal = lines.len() as u32 + want;
        for (title, artist) in hints {
            if lines.len() as u32 >= goal {
                break;
            }
            let query = format!("{title} {artist}");
            let Some(track) = self.resolve_hint(cfg, &query, &title) else {
                continue;
            };
            match self.decide(cfg, st, &track, &live.keys) {
                Decision::Allow => match self.push_pick(st, live, track, by, rule) {
                    Ok(Some(l)) => lines.push(l),
                    Ok(None) => {}
                    Err(n) => return n,
                },
                Decision::Hold { rule, .. } => {
                    let _ = self.audit(0, &rule, by, Outcome::Rejected);
                }
                Decision::Reject(v) => {
                    let _ = self.audit(0, v.rule, v.decided_by, Outcome::Rejected);
                }
            }
        }
        String::new()
    }

    /// Resolves an AI hint to a REAL provider track whose title matches the
    /// hint; anything unresolved or mismatched is dropped (never hallucinated).
    fn resolve_hint(&self, cfg: &Config, query: &str, title: &str) -> Option<Track> {
        let want = policy::norm(title);
        for provider in &cfg.providers {
            if let Ok(found) = music::search(self.host, provider, query, 3)
                && let Some(t) = found.into_iter().find(|t| {
                    let got = policy::norm(&t.title);
                    want.len() >= 3 && (got.contains(&want) || want.contains(&got))
                })
            {
                return Some(t);
            }
        }
        None
    }
}

/// Cuts `s` to `max` chars and drops control characters (the contract refuses them).
fn clean_text(s: &str, max: usize) -> String {
    s.chars().filter(|c| !c.is_control()).take(max).collect()
}

/// Queued (status 0) items of a pre-1.1.0 `dj.state` value, as `requests.import` items.
fn old_queue_items(raw: &Value) -> Vec<Value> {
    let Some(q) = raw.get("queue").and_then(Value::as_array) else {
        return Vec::new();
    };
    q.iter()
        .filter(|i| i.get("status").and_then(Value::as_u64) == Some(0))
        .filter_map(|i| {
            let t = i.get("track")?;
            let title = clean_text(t.get("title")?.as_str()?, 200);
            if title.is_empty() {
                return None;
            }
            let artist = clean_text(t.get("artist").and_then(Value::as_str).unwrap_or(""), 200);
            let url = t
                .get("url")
                .and_then(Value::as_str)
                .filter(|u| u.starts_with("https://") && u.len() <= 500)
                .unwrap_or("");
            let secs = t.get("duration_s").and_then(Value::as_u64).unwrap_or(0);
            Some(json!({
                "title": title, "artist": artist, "source_url": url,
                "duration_ms": secs.saturating_mul(1000),
            }))
        })
        .take(100)
        .collect()
}
