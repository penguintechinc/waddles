//! Moderator-only `!dj ...` commands: settings, lists, static playlists
//! (add / remove / reorder / import from a provider playlist), AI DJ setup
//! and the instruction-policy confirmation flow.

use serde_json::json;

use crate::ai;
use crate::engine::{DjError, Engine, Reply, fmt_dur};
use crate::host::Level;
use crate::model::*;
use crate::music;
use crate::policy::{self, Config};

const KEY_PL_INDEX: &str = "dj.pl_index";

/// Playlist names: 1-32 chars of `[a-z0-9_-]` (kv-key safe, no ':').
fn valid_name(n: &str) -> bool {
    !n.is_empty()
        && n.len() <= 32
        && n.chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
}

fn pl_key(name: &str) -> String {
    format!("dj.pl.{name}")
}

impl Engine<'_> {
    /// Tracks of one static playlist (empty when it does not exist).
    pub(crate) fn playlist_tracks(&self, name: &str) -> Result<Vec<Track>, DjError> {
        if !valid_name(name) {
            return Ok(Vec::new());
        }
        self.load(&pl_key(name))
    }

    /// `!dj ...` (moderators only).
    pub fn dj(&self, args: &str) -> Reply {
        if let Some(m) = self.need_mod() {
            return Ok(m);
        }
        let toks: Vec<&str> = args.split_whitespace().collect();
        let verb = toks
            .first()
            .map_or_else(String::new, |s| s.to_ascii_lowercase());
        let rest = args[verb.len().min(args.len())..].trim_start();
        match verb.as_str() {
            "set" => self.set(&toks),
            "list" => self.edit_list(&toks),
            "mode" => self.mode(toks.get(1).copied()),
            "ai" => self.ai_cmd(rest, &toks),
            "policy" => self.policy_cmd(toks.get(1).copied()),
            "playlist" | "pl" => self.playlist_cmd(&toks, rest),
            "fill" => self.fill(),
            "audit" => self.audit_show(toks.get(1).and_then(|t| t.parse().ok()).unwrap_or(5)),
            "unban" => self.unban(toks.get(1).copied()),
            "config" | "" => self.config_summary(),
            _ => Ok("usage: !dj set|list|mode|ai|policy|playlist|fill|audit|unban|config".into()),
        }
    }

    fn set(&self, toks: &[&str]) -> Reply {
        let (Some(k), Some(v)) = (toks.get(1), toks.get(2)) else {
            return Ok("usage: !dj set <setting> <value>".into());
        };
        let mut cfg = self.config()?;
        match cfg.set(&k.to_ascii_lowercase(), v) {
            Ok(()) => {
                self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
                Ok(format!("{k} updated"))
            }
            Err(e) => Ok(e.to_string()),
        }
    }

    fn edit_list(&self, toks: &[&str]) -> Reply {
        let (Some(list), Some(op)) = (toks.get(1), toks.get(2)) else {
            return Ok("usage: !dj list <allow-channel|allow-artist|deny-artist|deny-term|deny-track> add|remove <value>".into());
        };
        let add = match op.to_ascii_lowercase().as_str() {
            "add" => true,
            "remove" => false,
            _ => return Ok("expected add or remove".into()),
        };
        let value = toks.get(3..).map_or_else(String::new, |v| v.join(" "));
        let mut cfg = self.config()?;
        match cfg.edit_list(&list.to_ascii_lowercase(), add, &value) {
            Ok(()) => {
                self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
                Ok(format!("{list} updated"))
            }
            Err(e) => Ok(e.to_string()),
        }
    }

    fn mode(&self, arg: Option<&str>) -> Reply {
        let mut cfg = self.config()?;
        cfg.mode = match arg.map(str::to_ascii_lowercase).as_deref() {
            Some("requests") => Mode::Requests,
            Some("playlist") => Mode::Playlist,
            Some("ai") => Mode::Ai,
            _ => return Ok("usage: !dj mode requests|playlist|ai".into()),
        };
        if cfg.mode == Mode::Ai && !cfg.ai.enabled {
            return Ok("turn the AI DJ on first: !dj ai on".into());
        }
        self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
        Ok(format!("mode set to {}", arg.unwrap_or("")))
    }

    fn ai_cmd(&self, text: &str, toks: &[&str]) -> Reply {
        let sub = toks
            .get(1)
            .map_or_else(String::new, |s| s.to_ascii_lowercase());
        let mut cfg = self.config()?;
        match sub.as_str() {
            "on" => {
                cfg.ai.enabled = true;
                self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
                Ok("AI DJ is on (it fills gaps and obeys the same policy as requests)".into())
            }
            "off" => {
                cfg.ai.enabled = false;
                if cfg.mode == Mode::Ai {
                    cfg.mode = Mode::Requests;
                }
                self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
                Ok("AI DJ is off".into())
            }
            "genre" => {
                let g = clean_text(text.strip_prefix("genre").unwrap_or(text), 80);
                if g.is_empty() {
                    return Ok("usage: !dj ai genre <genre>".into());
                }
                cfg.ai.genre = g;
                self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
                Ok("genre saved".into())
            }
            "instructions" => {
                let ins = clean_text(text.strip_prefix("instructions").unwrap_or(text), 600);
                if ins.is_empty() {
                    return Ok("usage: !dj ai instructions <plain-language rules>".into());
                }
                match ai::compile_instructions(self.host, &cfg.ai.genre, &ins) {
                    Ok(rules) => {
                        let summary = rules.summary();
                        cfg.ai.instructions = ins;
                        cfg.pending_rules = Some(rules);
                        self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
                        self.audit(0, "policy_compiled", DecidedBy::Ai, Outcome::PolicyCompiled)?;
                        Ok(format!(
                            "compiled policy (not active yet): {summary} -- confirm with !dj policy confirm"
                        ))
                    }
                    Err(e) => Ok(format!("could not compile those instructions: {e}")),
                }
            }
            _ => Ok("usage: !dj ai on|off|genre <g>|instructions <text>".into()),
        }
    }

    fn policy_cmd(&self, sub: Option<&str>) -> Reply {
        let mut cfg = self.config()?;
        match sub.map(str::to_ascii_lowercase).as_deref() {
            Some("show") | None => Ok(format!(
                "active: {} | pending: {}",
                cfg.rules
                    .as_ref()
                    .map_or("none".to_string(), |r| r.summary()),
                cfg.pending_rules
                    .as_ref()
                    .map_or("none".to_string(), |r| r.summary())
            )),
            Some("confirm") => match cfg.pending_rules.take() {
                Some(r) => {
                    cfg.rules = Some(r);
                    self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
                    self.audit(
                        0,
                        "policy_confirmed",
                        DecidedBy::Moderator,
                        Outcome::PolicyConfirmed,
                    )?;
                    Ok("policy active".into())
                }
                None => Ok("no pending policy".into()),
            },
            Some("discard") => {
                cfg.pending_rules = None;
                self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
                Ok("pending policy discarded".into())
            }
            Some("clear") => {
                cfg.rules = None;
                self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
                self.audit(
                    0,
                    "policy_cleared",
                    DecidedBy::Moderator,
                    Outcome::PolicyConfirmed,
                )?;
                Ok("active policy cleared".into())
            }
            _ => Ok("usage: !dj policy show|confirm|discard|clear".into()),
        }
    }

    fn fill(&self) -> Reply {
        let cfg = self.config()?;
        if !cfg.ai.enabled {
            return Ok("the AI DJ is off".into());
        }
        let mut st = self.state()?;
        let r = self.ai_fill(&cfg, &mut st);
        self.save_state(&st)?;
        Ok(match r {
            Ok(n) => format!("AI DJ queued {n} songs"),
            Err(m) => format!("AI DJ: {m}"),
        })
    }

    fn audit_show(&self, n: usize) -> Reply {
        let rows: Vec<AuditRow> = self.load("dj.audit")?;
        let take = n.clamp(1, 10);
        let lines: Vec<String> = rows
            .iter()
            .rev()
            .take(take)
            .map(|r| {
                format!(
                    "#{} {} o{} d{}",
                    r.req_id,
                    r.rule,
                    u8::from(r.outcome),
                    u8::from(r.decided_by)
                )
            })
            .collect();
        Ok(if lines.is_empty() {
            "no audit rows".into()
        } else {
            lines.join(" | ")
        })
    }

    fn unban(&self, key: Option<&str>) -> Reply {
        let Some(key) = key else {
            return Ok("usage: !dj unban <provider/id>".into());
        };
        let mut st = self.state()?;
        let before = st.banned.len();
        st.banned.retain(|b| b != key);
        if st.banned.len() == before {
            return Ok("that song is not banned".into());
        }
        self.save_state(&st)?;
        Ok("unbanned".into())
    }

    fn config_summary(&self) -> Reply {
        let c = self.config()?;
        Ok(format!(
            "requests {} | providers {} | max/user {} | cooldown {}s | max {} | queue {} | replay {}s | explicit {} | dmca_safe {} | approval {} | auto_remove {} | cost {} | mode {} | ai {}",
            if c.requests_enabled { "on" } else { "off" },
            c.providers.join(","),
            c.max_per_user,
            c.cooldown_s,
            fmt_dur(c.max_duration_s),
            c.max_queue,
            c.replay_cooldown_s,
            c.explicit_allowed,
            c.dmca_safe,
            c.approval_required,
            c.auto_remove,
            c.request_cost,
            u8::from(c.mode),
            if c.ai.enabled { "on" } else { "off" },
        ))
    }

    // ---- playlists -------------------------------------------------------

    fn playlist_cmd(&self, toks: &[&str], rest: &str) -> Reply {
        let sub = toks
            .get(1)
            .map_or_else(String::new, |s| s.to_ascii_lowercase());
        let name = toks.get(2).map(|s| s.to_ascii_lowercase());
        let need_name = |f: &dyn Fn(&str) -> Reply| match &name {
            Some(n) if valid_name(n) => f(n),
            _ => Ok("playlist names are 1-32 chars of a-z 0-9 _ -".to_string()),
        };
        match sub.as_str() {
            "list" => {
                let idx: Vec<String> = self.load(KEY_PL_INDEX)?;
                Ok(if idx.is_empty() {
                    "no playlists".into()
                } else {
                    idx.join(", ")
                })
            }
            "create" => need_name(&|n| self.pl_create(n)),
            "delete" => need_name(&|n| self.pl_delete(n)),
            "show" => need_name(&|n| self.pl_show(n)),
            "use" => need_name(&|n| self.pl_use(n)),
            "add" => need_name(&|n| {
                let after = rest
                    .splitn(3, char::is_whitespace)
                    .nth(2)
                    .unwrap_or("")
                    .trim();
                self.pl_add(n, after)
            }),
            "remove" => need_name(&|n| self.pl_remove(n, toks.get(3).and_then(|t| t.parse().ok()))),
            "move" => need_name(&|n| {
                self.pl_move(
                    n,
                    toks.get(3).and_then(|t| t.parse().ok()),
                    toks.get(4).and_then(|t| t.parse().ok()),
                )
            }),
            "import" => {
                need_name(&|n| self.pl_import(n, toks.get(3).copied(), toks.get(4).copied()))
            }
            _ => Ok(
                "usage: !dj playlist list|create|delete|show|use|add|remove|move|import <name> ..."
                    .into(),
            ),
        }
    }

    fn pl_create(&self, name: &str) -> Reply {
        let mut idx: Vec<String> = self.load(KEY_PL_INDEX)?;
        if idx.iter().any(|n| n == name) {
            return Ok("that playlist already exists".into());
        }
        if idx.len() >= MAX_PLAYLISTS {
            return Ok("too many playlists".into());
        }
        idx.push(name.to_string());
        self.save(&pl_key(name), &Vec::<Track>::new(), 0)?;
        self.save(KEY_PL_INDEX, &idx, 0)?;
        Ok(format!("created playlist {name}"))
    }

    fn pl_delete(&self, name: &str) -> Reply {
        let mut idx: Vec<String> = self.load(KEY_PL_INDEX)?;
        if !idx.iter().any(|n| n == name) {
            return Ok("no such playlist".into());
        }
        idx.retain(|n| n != name);
        self.host
            .kv_delete(&pl_key(name))
            .map_err(DjError::Storage)?;
        self.save(KEY_PL_INDEX, &idx, 0)?;
        let mut cfg = self.config()?;
        if cfg.active_playlist.as_deref() == Some(name) {
            cfg.active_playlist = None;
            self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
        }
        Ok(format!("deleted playlist {name}"))
    }

    fn require_pl(&self, name: &str) -> Result<Option<Vec<Track>>, DjError> {
        let idx: Vec<String> = self.load(KEY_PL_INDEX)?;
        if idx.iter().any(|n| n == name) {
            self.playlist_tracks(name).map(Some)
        } else {
            Ok(None)
        }
    }

    fn pl_show(&self, name: &str) -> Reply {
        let Some(t) = self.require_pl(name)? else {
            return Ok("no such playlist".into());
        };
        let lines: Vec<String> = t
            .iter()
            .enumerate()
            .take(8)
            .map(|(i, t)| format!("{}. {} - {}", i + 1, t.title, t.artist))
            .collect();
        Ok(format!(
            "{name}: {} songs{}{}",
            t.len(),
            if lines.is_empty() { "" } else { " | " },
            lines.join(" | ")
        ))
    }

    fn pl_use(&self, name: &str) -> Reply {
        if self.require_pl(name)?.is_none() {
            return Ok("no such playlist".into());
        }
        let mut cfg = self.config()?;
        cfg.active_playlist = Some(name.to_string());
        self.save(crate::engine::KEY_CONFIG, &cfg, 0)?;
        let mut st = self.state()?;
        st.cursor = 0;
        self.save_state(&st)?;
        Ok(format!(
            "active playlist is {name}; switch playback with !dj mode playlist"
        ))
    }

    /// Static admission check for curated lists: bans, deny-lists, provider,
    /// allowlists, duration, explicit; no replay/duplicate guard.
    fn static_ok(&self, cfg: &Config, st: &State, t: &Track) -> Option<String> {
        let probe = State {
            banned: st.banned.clone(),
            ..State::default()
        };
        policy::evaluate(cfg, &probe, t, self.host.now_ms(), None).map(|v| v.reason)
    }

    fn pl_add(&self, name: &str, query: &str) -> Reply {
        let Some(mut tracks) = self.require_pl(name)? else {
            return Ok("no such playlist".into());
        };
        if tracks.len() >= MAX_PLAYLIST_TRACKS {
            return Ok("playlist is full".into());
        }
        let q = clean_text(query, 200);
        if q.is_empty() {
            return Ok("usage: !dj playlist add <name> <song name or link>".into());
        }
        let cfg = self.config()?;
        let track = if q.starts_with("http") {
            let Some(p) = music::provider_for_url(&q) else {
                return Ok("that link is not a supported music site".into());
            };
            match music::resolve(self.host, p, &q) {
                Ok(Some(t)) => t,
                Ok(None) => return Ok("I could not find that song".into()),
                Err(e) => return Ok(e.to_string()),
            }
        } else {
            let mut found = None;
            for p in &cfg.providers {
                match music::search(self.host, p, &q, 1) {
                    Ok(mut v) if !v.is_empty() => {
                        found = Some(v.remove(0));
                        break;
                    }
                    Ok(_) => {}
                    Err(e) => return Ok(e.to_string()),
                }
            }
            match found {
                Some(t) => t,
                None => return Ok("no songs found".into()),
            }
        };
        let st = self.state()?;
        if let Some(why) = self.static_ok(&cfg, &st, &track) {
            return Ok(format!("cannot add: {why}"));
        }
        if tracks.iter().any(|t| t.key() == track.key()) {
            return Ok("already in that playlist".into());
        }
        let line = track.title.clone();
        tracks.push(track);
        self.save(&pl_key(name), &tracks, 0)?;
        Ok(format!("added {line} to {name} ({} songs)", tracks.len()))
    }

    fn pl_remove(&self, name: &str, index: Option<usize>) -> Reply {
        let Some(mut tracks) = self.require_pl(name)? else {
            return Ok("no such playlist".into());
        };
        let Some(i) = index.filter(|i| *i >= 1 && *i <= tracks.len()) else {
            return Ok("usage: !dj playlist remove <name> <number>".into());
        };
        let t = tracks.remove(i - 1);
        self.save(&pl_key(name), &tracks, 0)?;
        Ok(format!("removed {}", t.title))
    }

    fn pl_move(&self, name: &str, from: Option<usize>, to: Option<usize>) -> Reply {
        let Some(mut tracks) = self.require_pl(name)? else {
            return Ok("no such playlist".into());
        };
        let (Some(f), Some(t)) = (from, to) else {
            return Ok("usage: !dj playlist move <name> <from> <to>".into());
        };
        if f < 1 || f > tracks.len() || t < 1 || t > tracks.len() {
            return Ok(format!("positions must be 1-{}", tracks.len()));
        }
        let item = tracks.remove(f - 1);
        tracks.insert(t - 1, item);
        self.save(&pl_key(name), &tracks, 0)?;
        Ok(format!("moved to position {t}"))
    }

    /// `!dj playlist import <name> <provider> <playlist id/url>`.
    fn pl_import(&self, name: &str, provider: Option<&str>, reference: Option<&str>) -> Reply {
        let (Some(provider), Some(reference)) = (provider, reference) else {
            return Ok(
                "usage: !dj playlist import <name> <youtube|spotify|soundcloud> <playlist id>"
                    .into(),
            );
        };
        let provider = provider.to_ascii_lowercase();
        if !PROVIDERS.contains(&provider.as_str()) {
            return Ok(format!("provider must be one of {}", PROVIDERS.join(", ")));
        }
        let id_ok = !reference.is_empty()
            && reference.len() <= 64
            && reference
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
        if !id_ok {
            return Ok(
                "use the provider's playlist id (letters, digits, - and _), not a link".into(),
            );
        }
        let Some(mut tracks) = self.require_pl(name)? else {
            return Ok("no such playlist".into());
        };
        let cfg = self.config()?;
        let st = self.state()?;
        let fetched = match music::playlist(self.host, &provider, reference, MAX_PLAYLIST_TRACKS) {
            Ok(t) => t,
            Err(e) => return Ok(e.to_string()),
        };
        let (mut added, mut skipped) = (0usize, 0usize);
        for t in fetched {
            if tracks.len() >= MAX_PLAYLIST_TRACKS
                || tracks.iter().any(|x| x.key() == t.key())
                || self.static_ok(&cfg, &st, &t).is_some()
            {
                skipped += 1;
                continue;
            }
            tracks.push(t);
            added += 1;
        }
        self.save(&pl_key(name), &tracks, 0)?;
        self.host.log(
            Level::Info,
            "dj playlist import",
            json!({"provider": provider, "added": added, "skipped": skipped}),
        );
        Ok(format!(
            "imported {added} songs into {name} ({skipped} skipped by policy/duplicates)"
        ))
    }
}
