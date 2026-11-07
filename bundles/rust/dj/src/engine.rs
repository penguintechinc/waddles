//! The DJ engine: storage, song requests, queue moderation, advance/playback
//! and the AI DJ fill. Generic over [`Host`], so the identical code runs in
//! the component and natively under test.
//!
//! User-facing denials are `Ok(reply text)`; only infrastructure failures
//! (kv down, corrupt state) are `Err(DjError)` and fail loud.

use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::json;

use crate::ai;
use crate::host::{Host, Level};
use crate::model::*;
use crate::music;
use crate::policy::{self, Config, Violation};

pub const KEY_CONFIG: &str = "dj.config";
pub const KEY_STATE: &str = "dj.state";
pub const KEY_AUDIT: &str = "dj.audit";
pub const KEY_PUBLIC: &str = "dj.public_state";
pub const ECONOMY_TARGET: &str = "waddles.core.service.economy";

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

fn parse_id(tok: Option<&&str>) -> Option<u64> {
    tok?.trim_start_matches('#').parse().ok()
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

    /// Persists state and refreshes the overlay-facing public snapshot.
    pub(crate) fn save_state(&self, st: &State) -> Result<(), DjError> {
        self.save(KEY_STATE, st, 0)?;
        let public = PublicState {
            now_playing: st.now.as_ref().map(PublicTrack::from),
            queue: st
                .queue
                .iter()
                .filter(|i| i.status == Status::Queued)
                .take(25)
                .map(PublicTrack::from)
                .collect(),
            updated_ms: self.host.now_ms(),
        };
        self.save(KEY_PUBLIC, &public, 0)
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

    /// Routes one chat line. `Ok(None)` = not a DJ command.
    pub fn handle(&self, text: &str) -> Result<Option<String>, DjError> {
        let text = text.trim();
        let first = match text.split_whitespace().next() {
            Some(f) => f,
            None => return Ok(None),
        };
        let args = text[first.len()..].trim();
        match first.to_ascii_lowercase().as_str() {
            "!sr" | "!songrequest" => self.request(args).map(Some),
            "!music" | "!queue" => self.music(args).map(Some),
            "!dj" => self.dj(args).map(Some),
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

    /// Deterministic policy, approval gate, then the optional AI judge for
    /// fuzzy rules. AI failure or low confidence holds for a moderator.
    pub fn decide(&self, cfg: &Config, st: &State, track: &Track) -> Decision {
        if let Some(v) = policy::evaluate(cfg, st, track, self.host.now_ms(), None) {
            return Decision::Reject(v);
        }
        if cfg.approval_required {
            return Decision::Hold {
                rule: "approval_required".into(),
                reason: "waiting for moderator approval".into(),
                by: DecidedBy::Deterministic,
            };
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
                        return if cfg.auto_remove {
                            Decision::Reject(Violation {
                                rule: "ai_fuzzy",
                                reason,
                                decided_by: DecidedBy::Ai,
                            })
                        } else {
                            Decision::Hold {
                                rule: "ai_fuzzy".into(),
                                reason,
                                by: DecidedBy::Ai,
                            }
                        };
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

    // ---- song requests --------------------------------------------------

    fn resolve_candidates(&self, cfg: &Config, query: &str) -> Result<Vec<Track>, String> {
        if query.starts_with("http://") || query.starts_with("https://") {
            let provider =
                music::provider_for_url(query).ok_or("that link is not a supported music site")?;
            if !cfg.providers.iter().any(|p| p == provider) {
                return Err(format!("{provider} is not enabled here"));
            }
            return match music::resolve(self.host, provider, query) {
                Ok(Some(t)) => Ok(vec![t]),
                Ok(None) => Err("I could not find that song".into()),
                Err(e) => Err(e.to_string()),
            };
        }
        let mut last_err = None;
        for provider in &cfg.providers {
            match music::search(self.host, provider, query, 3) {
                Ok(t) if !t.is_empty() => return Ok(t),
                Ok(_) => {}
                Err(e) => last_err = Some(e.to_string()),
            }
        }
        Err(last_err.unwrap_or_else(|| "no songs found".to_string()))
    }

    fn charge(&self, cfg: &Config, actor: &str) -> Result<u64, String> {
        if cfg.request_cost == 0 {
            return Ok(0);
        }
        let key = format!("dj.sr.{}", self.ctx.message_id);
        self.host
            .invoke(
                ECONOMY_TARGET,
                "adjust_balance",
                &json!({"user": actor, "delta": -(cfg.request_cost as i64)}),
                Some(&key),
            )
            .map(|_| cfg.request_cost)
            .map_err(|e| {
                self.host.log(
                    Level::Warn,
                    "request charge failed",
                    json!({"error": e.to_string()}),
                );
                "you cannot afford that request (or points are unavailable)".to_string()
            })
    }

    /// Refunds an item's cost (idempotent per item id). Returns a reply suffix.
    pub(crate) fn refund(&self, item: &Item) -> String {
        let Some(user) = item.requester.as_deref().filter(|_| item.paid > 0) else {
            return String::new();
        };
        let key = format!("dj.refund.{}", item.id);
        match self.host.invoke(
            ECONOMY_TARGET,
            "adjust_balance",
            &json!({"user": user, "delta": item.paid as i64}),
            Some(&key),
        ) {
            Ok(_) => " (refunded)".to_string(),
            Err(e) => {
                self.host.log(
                    Level::Error,
                    "refund failed",
                    json!({"item": item.id, "error": e.to_string()}),
                );
                " (refund failed - a moderator will fix it)".to_string()
            }
        }
    }

    /// `!sr <query|url>`.
    pub fn request(&self, args: &str) -> Reply {
        let cfg = self.config()?;
        if !cfg.requests_enabled {
            return Ok("song requests are closed".into());
        }
        let Some(actor) = self.ctx.actor.clone() else {
            return Ok("link your account to request songs".into());
        };
        let query = clean_text(args, 200);
        if query.is_empty() {
            return Ok("usage: !sr <song name or link>".into());
        }
        let cd_key = format!("dj.cd.{actor}");
        if cfg.cooldown_s > 0
            && self
                .host
                .kv_get(&cd_key)
                .map_err(DjError::Storage)?
                .is_some()
        {
            return Ok(format!(
                "slow down, you can request again every {}s",
                cfg.cooldown_s
            ));
        }
        let mut st = self.state()?;
        if st.queue.len() >= cfg.max_queue as usize {
            return Ok("the queue is full".into());
        }
        let mine = st
            .queue
            .iter()
            .filter(|i| i.requester.as_deref() == Some(actor.as_str()))
            .count();
        if mine >= cfg.max_per_user as usize {
            return Ok(format!(
                "you already have {mine} requests queued (max {})",
                cfg.max_per_user
            ));
        }
        let candidates = match self.resolve_candidates(&cfg, &query) {
            Ok(c) => c,
            Err(msg) => return Ok(msg),
        };

        let mut first_hold: Option<(Track, String, String, DecidedBy)> = None;
        let mut first_reject: Option<(Track, Violation)> = None;
        let mut chosen: Option<Track> = None;
        for t in candidates {
            match self.decide(&cfg, &st, &t) {
                Decision::Allow => {
                    chosen = Some(t);
                    break;
                }
                Decision::Hold { rule, reason, by } => {
                    if first_hold.is_none() {
                        first_hold = Some((t, rule, reason, by));
                    }
                }
                Decision::Reject(v) => {
                    if first_reject.is_none() {
                        first_reject = Some((t, v));
                    }
                }
            }
        }

        let (track, status, reason, rule, by) = if let Some(t) = chosen {
            (
                t,
                Status::Queued,
                None,
                "allowed".to_string(),
                DecidedBy::Deterministic,
            )
        } else if let Some((t, rule, reason, by)) = first_hold {
            (t, Status::Pending, Some(reason), rule, by)
        } else if let Some((t, v)) = first_reject {
            let id = st.next_id + 1;
            st.next_id = id;
            let item = Item {
                id,
                track: t,
                requester: Some(actor),
                status: Status::Pending,
                source: Source::Request,
                added_ms: self.host.now_ms(),
                reason: Some(v.reason.clone()),
                paid: 0,
            };
            st.rejected.push(Rejected {
                item,
                rule: v.rule.to_string(),
            });
            if st.rejected.len() > REJECTED_CAP {
                st.rejected.remove(0);
            }
            self.save_state(&st)?;
            self.audit(id, v.rule, v.decided_by, Outcome::Rejected)?;
            return Ok(format!("request #{id} removed: {}", v.reason));
        } else {
            return Ok("no songs found".into());
        };

        let paid = match self.charge(&cfg, &actor) {
            Ok(p) => p,
            Err(msg) => return Ok(msg),
        };
        let id = st.next_id + 1;
        st.next_id = id;
        let line = fmt_track(&track);
        let item = Item {
            id,
            track,
            requester: Some(actor),
            status,
            source: Source::Request,
            added_ms: self.host.now_ms(),
            reason: reason.clone(),
            paid,
        };
        st.queue.push(item.clone());
        let pos = st
            .queue
            .iter()
            .filter(|i| i.status == Status::Queued)
            .count();
        if let Err(e) = self.save_state(&st) {
            // Do not keep the user's points for a request that was never stored.
            self.refund(&item);
            return Err(e);
        }
        if cfg.cooldown_s > 0 {
            self.host
                .kv_set(&cd_key, b"1", cfg.cooldown_s)
                .map_err(DjError::Storage)?;
        }
        let outcome = if status == Status::Queued {
            Outcome::Enqueued
        } else {
            Outcome::Held
        };
        self.audit(id, &rule, by, outcome)?;
        Ok(match status {
            Status::Queued => format!("queued #{id}: {line} (position {pos})"),
            Status::Pending => format!("request #{id} is waiting for moderator approval: {line}"),
        })
    }

    // ---- !music ----------------------------------------------------------

    fn music(&self, args: &str) -> Reply {
        let toks: Vec<&str> = args.split_whitespace().collect();
        let verb = toks
            .first()
            .map(|s| s.to_ascii_lowercase())
            .unwrap_or_default();
        match verb.as_str() {
            "" | "list" | "queue" => self.show(),
            "now" => self.now_playing(),
            "remove" => self.remove(parse_id(toks.get(1))),
            "next" | "skip" => match self.need_mod() {
                Some(m) => Ok(m),
                None => self.skip(),
            },
            "approve" => self.mod_only(|| self.approve(parse_id(toks.get(1)))),
            "ban" => self.mod_only(|| self.ban(parse_id(toks.get(1)))),
            "restore" => self.mod_only(|| self.restore(parse_id(toks.get(1)))),
            "clear" => self.mod_only(|| self.clear()),
            "move" => self.mod_only(|| self.move_item(parse_id(toks.get(1)), toks.get(2).and_then(|t| t.parse::<usize>().ok()))),
            _ => Ok("usage: !music [now|remove <id>|skip|approve <id>|ban <id>|restore <id>|move <id> <pos>|clear]".into()),
        }
    }

    pub(crate) fn mod_only(&self, f: impl FnOnce() -> Reply) -> Reply {
        match self.need_mod() {
            Some(m) => Ok(m),
            None => f(),
        }
    }

    fn now_playing(&self) -> Reply {
        let st = self.state()?;
        Ok(match st.now {
            Some(i) => format!("now playing: {}", fmt_track(&i.track)),
            None => "nothing is playing".into(),
        })
    }

    fn show(&self) -> Reply {
        let st = self.state()?;
        let mut parts = Vec::new();
        if let Some(n) = &st.now {
            parts.push(format!("now: {}", fmt_track(&n.track)));
        }
        let queued: Vec<String> = st
            .queue
            .iter()
            .filter(|i| i.status == Status::Queued)
            .take(5)
            .map(|i| format!("#{} {}", i.id, fmt_track(&i.track)))
            .collect();
        if queued.is_empty() {
            parts.push("queue is empty".into());
        } else {
            parts.push(format!("next: {}", queued.join(" | ")));
        }
        let pending = st
            .queue
            .iter()
            .filter(|i| i.status == Status::Pending)
            .count();
        if pending > 0 && self.ctx.is_mod {
            parts.push(format!("{pending} awaiting approval"));
        }
        Ok(parts.join(" || "))
    }

    fn remove(&self, id: Option<u64>) -> Reply {
        let Some(id) = id else {
            return Ok("usage: !music remove <id>".into());
        };
        let mut st = self.state()?;
        let Some(pos) = st.queue.iter().position(|i| i.id == id) else {
            return Ok(format!("no request #{id}"));
        };
        let owner = st.queue[pos].requester.is_some() && st.queue[pos].requester == self.ctx.actor;
        if !(owner || self.ctx.is_mod) {
            return Ok("you can only remove your own requests".into());
        }
        let item = st.queue.remove(pos);
        self.save_state(&st)?;
        self.audit(
            id,
            if owner { "owner_remove" } else { "mod_remove" },
            DecidedBy::Moderator,
            Outcome::Removed,
        )?;
        Ok(format!("removed #{id}{}", self.refund(&item)))
    }

    fn skip(&self) -> Reply {
        let cfg = self.config()?;
        let mut st = self.state()?;
        if let Some(n) = &st.now {
            self.audit(n.id, "mod_skip", DecidedBy::Moderator, Outcome::Skipped)?;
        }
        self.advance(&cfg, &mut st)
    }

    fn approve(&self, id: Option<u64>) -> Reply {
        let Some(id) = id else {
            return Ok("usage: !music approve <id>".into());
        };
        let mut st = self.state()?;
        let Some(item) = st
            .queue
            .iter_mut()
            .find(|i| i.id == id && i.status == Status::Pending)
        else {
            return Ok(format!("no pending request #{id}"));
        };
        item.status = Status::Queued;
        item.reason = None;
        self.save_state(&st)?;
        self.audit(id, "mod_approve", DecidedBy::Moderator, Outcome::Approved)?;
        Ok(format!("approved #{id}"))
    }

    fn ban(&self, id: Option<u64>) -> Reply {
        let Some(id) = id else {
            return Ok("usage: !music ban <id>".into());
        };
        let mut st = self.state()?;
        let key = st
            .queue
            .iter()
            .find(|i| i.id == id)
            .or(st.now.as_ref().filter(|i| i.id == id))
            .map(|i| i.track.key())
            .or_else(|| {
                st.rejected
                    .iter()
                    .find(|r| r.item.id == id)
                    .map(|r| r.item.track.key())
            });
        let Some(key) = key else {
            return Ok(format!("no request #{id}"));
        };
        if st.banned.len() >= 200 {
            return Ok("the ban list is full".into());
        }
        if !st.banned.contains(&key) {
            st.banned.push(key.clone());
        }
        let mut suffix = String::new();
        let removed: Vec<Item> = st
            .queue
            .iter()
            .filter(|i| i.track.key() == key)
            .cloned()
            .collect();
        st.queue.retain(|i| i.track.key() != key);
        for r in &removed {
            suffix.push_str(&self.refund(r));
        }
        self.save_state(&st)?;
        self.audit(id, "mod_ban", DecidedBy::Moderator, Outcome::Banned)?;
        Ok(format!("banned that song{suffix}"))
    }

    fn restore(&self, id: Option<u64>) -> Reply {
        let Some(id) = id else {
            return Ok("usage: !music restore <id>".into());
        };
        let cfg = self.config()?;
        let mut st = self.state()?;
        let Some(pos) = st.rejected.iter().position(|r| r.item.id == id) else {
            return Ok(format!("no removed request #{id}"));
        };
        if st.queue.len() >= cfg.max_queue as usize {
            return Ok("the queue is full".into());
        }
        let rej = st.rejected.remove(pos);
        if st.banned.contains(&rej.item.track.key()) {
            st.rejected.insert(pos, rej);
            return Ok("that song is banned; use !dj unban first".into());
        }
        let mut item = rej.item;
        item.status = Status::Queued;
        item.reason = None;
        st.queue.push(item);
        self.save_state(&st)?;
        // A moderator override skips the AI judge by design; the decision is audited.
        self.audit(id, &rej.rule, DecidedBy::Moderator, Outcome::Restored)?;
        Ok(format!("restored #{id}"))
    }

    fn clear(&self) -> Reply {
        let mut st = self.state()?;
        let items: Vec<Item> = std::mem::take(&mut st.queue);
        self.save_state(&st)?;
        for i in &items {
            self.refund(i);
        }
        self.audit(0, "mod_clear", DecidedBy::Moderator, Outcome::Removed)?;
        Ok(format!("cleared {} requests", items.len()))
    }

    fn move_item(&self, id: Option<u64>, pos: Option<usize>) -> Reply {
        let (Some(id), Some(pos)) = (id, pos) else {
            return Ok("usage: !music move <id> <position>".into());
        };
        let mut st = self.state()?;
        let Some(from) = st.queue.iter().position(|i| i.id == id) else {
            return Ok(format!("no request #{id}"));
        };
        let to = pos.saturating_sub(1).min(st.queue.len() - 1);
        let item = st.queue.remove(from);
        st.queue.insert(to, item);
        self.save_state(&st)?;
        Ok(format!("moved #{id} to position {}", to + 1))
    }

    // ---- advance / playback ----------------------------------------------

    /// Finishes the current track and starts the next playable one.
    pub fn advance(&self, cfg: &Config, st: &mut State) -> Reply {
        let now = self.host.now_ms();
        if let Some(cur) = st.now.take() {
            st.history.push(Played {
                key: cur.track.key(),
                ms: now,
            });
            if st.history.len() > HISTORY_CAP {
                st.history.remove(0);
            }
        }
        let mut note = String::new();
        let mut next = self.take_queued(st);
        if next.is_none() && cfg.mode == Mode::Playlist {
            next = self.playlist_next(cfg, st)?;
        }
        if next.is_none() && (cfg.ai.enabled || cfg.mode == Mode::Ai) {
            if !cfg.ai.enabled {
                self.save_state(st)?;
                return Ok("the AI DJ is off; turn it on with !dj ai on".into());
            }
            match self.ai_fill(cfg, st) {
                Ok(n) if n > 0 => next = self.take_queued(st),
                Ok(_) => note = " (AI DJ found nothing playable)".into(),
                Err(msg) => note = format!(" (AI DJ: {msg})"),
            }
        }
        let reply = match next {
            Some(item) => {
                if let Err(e) = music::handoff_play(self.host, &item.track, item.id) {
                    note.push_str(&format!(" (player handoff unavailable: {e})"));
                }
                let line = fmt_track(&item.track);
                st.now = Some(item);
                format!("now playing: {line}{note}")
            }
            None => format!("the queue is empty{note}"),
        };
        self.save_state(st)?;
        Ok(reply)
    }

    fn take_queued(&self, st: &mut State) -> Option<Item> {
        let pos = st.queue.iter().position(|i| i.status == Status::Queued)?;
        Some(st.queue.remove(pos))
    }

    /// Next playable track of the active playlist (static checks only: bans,
    /// deny-lists, duration, explicit; replay/duplicate guards do not apply to
    /// curated loops).
    fn playlist_next(&self, cfg: &Config, st: &mut State) -> Result<Option<Item>, DjError> {
        let Some(name) = cfg.active_playlist.clone() else {
            return Ok(None);
        };
        let tracks = self.playlist_tracks(&name)?;
        if tracks.is_empty() {
            return Ok(None);
        }
        let probe = State {
            banned: st.banned.clone(),
            ..State::default()
        };
        for _ in 0..tracks.len() {
            let t = tracks[st.cursor % tracks.len()].clone();
            st.cursor = (st.cursor + 1) % tracks.len();
            if policy::evaluate(cfg, &probe, &t, self.host.now_ms(), None).is_none() {
                st.next_id += 1;
                return Ok(Some(Item {
                    id: st.next_id,
                    track: t,
                    requester: None,
                    status: Status::Queued,
                    source: Source::Playlist,
                    added_ms: self.host.now_ms(),
                    reason: None,
                    paid: 0,
                }));
            }
        }
        Ok(None)
    }

    // ---- AI DJ -------------------------------------------------------------

    /// Fills the queue with AI-suggested tracks, resolved through real
    /// provider searches and pushed through the same policy as any request.
    /// Returns the number added, or a user-facing reason it could not.
    pub fn ai_fill(&self, cfg: &Config, st: &mut State) -> Result<u32, String> {
        let now = self.host.now_ms();
        if now.saturating_sub(st.fill_window_ms) >= 3_600_000 {
            st.fill_window_ms = now;
            st.fill_count = 0;
        }
        let room = cfg.ai.hourly_cap.saturating_sub(st.fill_count);
        if room == 0 {
            return Err("hourly limit reached".into());
        }
        let want = cfg.ai.batch.min(room);
        let avoid: Vec<String> = st
            .queue
            .iter()
            .chain(st.now.iter())
            .map(|i| i.track.title.clone())
            .collect();
        let hints = ai::suggest(self.host, &cfg.ai.genre, &cfg.ai.instructions, want, &avoid)
            .map_err(|e| e.to_string())?;
        let mut added = 0u32;
        for (title, artist) in hints {
            if st.queue.len() >= cfg.max_queue as usize {
                break;
            }
            let query = format!("{title} {artist}");
            let Some(track) = self.resolve_hint(cfg, &query, &title) else {
                continue;
            };
            match self.decide(cfg, st, &track) {
                Decision::Allow => {
                    st.next_id += 1;
                    let id = st.next_id;
                    st.queue.push(Item {
                        id,
                        track,
                        requester: None,
                        status: Status::Queued,
                        source: Source::Ai,
                        added_ms: now,
                        reason: None,
                        paid: 0,
                    });
                    st.fill_count += 1;
                    added += 1;
                    let _ = self.audit(id, "ai_fill", DecidedBy::Ai, Outcome::Filled);
                }
                Decision::Hold { rule, .. } => {
                    let _ = self.audit(0, &rule, DecidedBy::Ai, Outcome::Rejected);
                }
                Decision::Reject(v) => {
                    let _ = self.audit(0, v.rule, v.decided_by, Outcome::Rejected);
                }
            }
        }
        Ok(added)
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
