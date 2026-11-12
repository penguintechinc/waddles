//! Unit tests for the reputation service tool logic, over fakes of the three
//! seams (`RepHost`, `ConfigStore`, `Platform`).

use super::*;
use std::collections::HashMap;
use std::sync::Mutex;

const U1: &str = "11111111-1111-1111-1111-111111111111";
const U2: &str = "22222222-2222-2222-2222-222222222222";
const ADMIN: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";

#[derive(Default)]
struct FakeHost {
    scores: Mutex<HashMap<String, i64>>,
    calls: Mutex<Vec<String>>,
    fail: Mutex<Option<HostError>>,
    soft: Mutex<HashMap<String, String>>,
    tenant: Mutex<Option<TenantPolicy>>,
    tenant_scores: Mutex<HashMap<String, i64>>,
    /// When set, the next apply reports `applied = false` (an idempotent replay).
    replay: Mutex<bool>,
}

impl FakeHost {
    fn with(scores: &[(&str, i64)]) -> Arc<Self> {
        let h = Self::default();
        *h.scores.lock().unwrap() = scores.iter().map(|(u, s)| (u.to_string(), *s)).collect();
        Arc::new(h)
    }
    fn failing(e: HostError) -> Arc<Self> {
        let h = Self::default();
        *h.fail.lock().unwrap() = Some(e);
        Arc::new(h)
    }
    fn log(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }
    fn gate(&self, what: &str) -> Result<(), HostError> {
        self.calls.lock().unwrap().push(what.to_string());
        self.fail.lock().unwrap().clone().map_or(Ok(()), Err)
    }
    fn bump(&self, user: &str, delta: i32) -> Result<Applied, HostError> {
        if std::mem::take(&mut *self.replay.lock().unwrap()) {
            let s = self.scores.lock().unwrap();
            return Ok(Applied {
                new_score: *s.get(user).ok_or(HostError::NotAMember)?,
                applied: false,
            });
        }
        let mut s = self.scores.lock().unwrap();
        let v = s.get_mut(user).ok_or(HostError::NotAMember)?;
        *v = (*v + i64::from(delta)).clamp(SCALE_MIN, SCALE_MAX);
        Ok(Applied {
            new_score: *v,
            applied: true,
        })
    }
}

impl RepHost for FakeHost {
    fn get_many(&self, users: &[String]) -> Result<Vec<Entry>, HostError> {
        self.gate(&format!("get_many:{}", users.len()))?;
        let s = self.scores.lock().unwrap();
        users
            .iter()
            .map(|u| {
                s.get(u)
                    .map(|v| Entry {
                        user: u.clone(),
                        score: *v,
                    })
                    .ok_or(HostError::NotAMember)
            })
            .collect()
    }
    fn apply(&self, user: &str, delta: i32, reason: &str) -> Result<Applied, HostError> {
        self.gate(&format!("apply:{user}:{delta}:{reason}"))?;
        self.bump(user, delta)
    }
    fn apply_moderation(&self, user: &str, delta: i32, reason: &str) -> Result<Applied, HostError> {
        self.gate(&format!("apply_moderation:{user}:{delta}:{reason}"))?;
        self.bump(user, delta)
    }
    fn override_score(&self, user: &str, absolute: i64, reason: &str) -> Result<i64, HostError> {
        self.gate(&format!("override:{user}:{absolute}:{reason}"))?;
        let v = absolute.clamp(SCALE_MIN, SCALE_MAX);
        self.scores.lock().unwrap().insert(user.to_string(), v);
        Ok(v)
    }
    fn leaderboard(&self, limit: u32) -> Result<Vec<Entry>, HostError> {
        self.gate(&format!("leaderboard:{limit}"))?;
        let mut v: Vec<Entry> = self
            .scores
            .lock()
            .unwrap()
            .iter()
            .map(|(u, s)| Entry {
                user: u.clone(),
                score: *s,
            })
            .collect();
        v.sort_by(|a, b| b.score.cmp(&a.score).then(a.user.cmp(&b.user)));
        v.truncate(limit as usize);
        Ok(v)
    }
    fn get_tenant(&self, user: &str) -> Result<TenantScore, HostError> {
        self.gate(&format!("get_tenant:{user}"))?;
        let score = self
            .tenant_scores
            .lock()
            .unwrap()
            .get(user)
            .copied()
            .unwrap_or(600);
        Ok(TenantScore {
            score,
            communities: 2,
        })
    }
    fn tenant_policy(&self) -> Result<Option<TenantPolicy>, HostError> {
        self.gate("tenant_policy")?;
        Ok(*self.tenant.lock().unwrap())
    }
    fn history(
        &self,
        user: &str,
        limit: u32,
        before: Option<i64>,
    ) -> Result<Vec<HistoryRow>, HostError> {
        self.gate(&format!("history:{user}:{limit}:{before:?}"))?;
        Ok(vec![HistoryRow {
            id: 7,
            delta: 1,
            reason_code: "activity:evt".into(),
            kind: "apply".into(),
            balance_after: Some(601),
            occurred_at: "2026-11-11T00:00:00Z".into(),
        }])
    }
    fn below_threshold(&self, threshold: i64, limit: u32) -> Result<Vec<Entry>, HostError> {
        self.gate(&format!("below:{threshold}:{limit}"))?;
        let mut v: Vec<Entry> = self
            .scores
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, s)| **s < threshold)
            .map(|(u, s)| Entry {
                user: u.clone(),
                score: *s,
            })
            .collect();
        v.sort_by(|a, b| a.score.cmp(&b.score).then(a.user.cmp(&b.user)));
        v.truncate(limit as usize);
        Ok(v)
    }
    fn is_soft_banned(&self, user: &str) -> Result<bool, HostError> {
        self.gate(&format!("is_soft_banned:{user}"))?;
        Ok(self.soft.lock().unwrap().contains_key(user))
    }
    fn soft_ban(&self, user: &str, reason: &str) -> Result<bool, HostError> {
        self.gate(&format!("soft_ban:{user}:{reason}"))?;
        let mut s = self.soft.lock().unwrap();
        if s.contains_key(user) {
            return Ok(false);
        }
        s.insert(user.to_string(), reason.to_string());
        Ok(true)
    }
    fn lift_soft_ban(&self, user: &str, only_reason: &str) -> Result<bool, HostError> {
        self.gate(&format!("lift:{user}:{only_reason}"))?;
        let mut s = self.soft.lock().unwrap();
        if s.get(user).is_some_and(|r| r == only_reason) {
            s.remove(user);
            return Ok(true);
        }
        Ok(false)
    }
}

#[derive(Default)]
struct FakeCfg(Mutex<HashMap<String, Vec<u8>>>, bool);
impl FakeCfg {
    fn with(pairs: &[(&str, &str)]) -> Arc<Self> {
        Arc::new(Self(
            Mutex::new(
                pairs
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.as_bytes().to_vec()))
                    .collect(),
            ),
            false,
        ))
    }
    fn broken() -> Arc<Self> {
        Arc::new(Self(Mutex::new(HashMap::new()), true))
    }
}
impl ConfigStore for FakeCfg {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        if self.1 {
            return Err("kv down".into());
        }
        Ok(self.0.lock().unwrap().get(key).cloned())
    }
    fn set(&self, key: &str, value: &[u8], _ttl_seconds: u32) -> Result<(), String> {
        if self.1 {
            return Err("kv down".into());
        }
        self.0.lock().unwrap().insert(key.into(), value.to_vec());
        Ok(())
    }
}

struct FakePlatform {
    flag: bool,
    bans: Mutex<Vec<String>>,
    ban_err: Option<String>,
}
impl FakePlatform {
    fn new(flag: bool) -> Arc<Self> {
        Arc::new(Self {
            flag,
            bans: Mutex::new(vec![]),
            ban_err: None,
        })
    }
    fn failing_ban(code: &str) -> Arc<Self> {
        Arc::new(Self {
            flag: true,
            bans: Mutex::new(vec![]),
            ban_err: Some(code.into()),
        })
    }
}
impl Platform for FakePlatform {
    fn flag_enabled(&self, key: &str) -> bool {
        assert_eq!(key, FLAG_AUTO_BAN, "only the auto-ban flag is read");
        self.flag
    }
    fn ban(&self, platform: &str, user: &str, reason: &str, key: &str) -> Result<(), String> {
        self.bans
            .lock()
            .unwrap()
            .push(format!("{platform}:{user}:{reason}:{key}"));
        self.ban_err.clone().map_or(Ok(()), Err)
    }
}

struct Rig {
    host: Arc<FakeHost>,
    cfg: Arc<FakeCfg>,
    platform: Arc<FakePlatform>,
}

fn rig(host: Arc<FakeHost>, cfg: Arc<FakeCfg>, platform: Arc<FakePlatform>) -> Rig {
    Rig {
        host,
        cfg,
        platform,
    }
}

fn plain(scores: &[(&str, i64)]) -> Rig {
    rig(
        FakeHost::with(scores),
        FakeCfg::with(&[]),
        FakePlatform::new(false),
    )
}

fn ctx(key: Option<&str>, actor: Option<&str>, scopes: &[&str]) -> ToolContext {
    ToolContext {
        idempotency_key: key.map(str::to_string),
        actor: actor.map(str::to_string),
        actor_scopes: scopes.iter().map(|s| s.to_string()).collect(),
    }
}

fn run(r: &Rig, tool: &str, args: Value, c: ToolContext) -> Result<Value, ToolError> {
    let reg = build_registry(Env {
        host: r.host.clone(),
        cfg: r.cfg.clone(),
        platform: r.platform.clone(),
    });
    let mut args = args;
    if let Some(k) = &c.idempotency_key {
        args[waddle_sdk::tool::IDEMPOTENCY_ARG] = json!(k);
    }
    if c.actor.is_some() || !c.actor_scopes.is_empty() {
        args[waddle_sdk::tool::CTX_ARG] = json!({"actor": c.actor, "scopes": c.actor_scopes});
    }
    reg.call(tool, &serde_json::to_vec(&args).unwrap())
        .map(|b| serde_json::from_slice(&b).unwrap())
}

fn read(r: &Rig, tool: &str, args: Value) -> Result<Value, ToolError> {
    run(r, tool, args, ctx(None, None, &[]))
}

fn write(r: &Rig, tool: &str, args: Value) -> Result<Value, ToolError> {
    run(r, tool, args, ctx(Some("k1"), None, &[]))
}

fn as_admin(r: &Rig, tool: &str, args: Value) -> Result<Value, ToolError> {
    run(r, tool, args, ctx(Some("k1"), Some(ADMIN), &[ADMIN_SCOPE]))
}

const FLOORS_ON: &str = r#"{"soft_ban_enabled":true,"hard_ban_enabled":true}"#;

#[test]
fn the_tool_table_has_no_tenant_wide_leaderboard_and_routes_every_tool() {
    assert_eq!(TOOL_NAMES.len(), 14);
    assert!(!TOOL_NAMES.iter().any(|n| n.contains("tenant_leaderboard")));
    let r = plain(&[(U1, 600)]);
    for (t, a) in [
        ("get_score", json!({"user": U1})),
        ("get_many", json!({"users": [U1]})),
        ("leaderboard", json!({})),
        ("rep", json!({"user": U1})),
        ("check", json!({"user": U1})),
        ("is_soft_banned", json!({"user": U1})),
    ] {
        assert!(read(&r, t, a).is_ok(), "{t}");
    }
    let reg = build_registry(Env {
        host: r.host.clone(),
        cfg: r.cfg.clone(),
        platform: r.platform.clone(),
    });
    assert!(
        matches!(reg.call("nope", b"{}"), Err(ToolError::InvalidArgs(m)) if m.contains("unknown tool"))
    );
}

#[test]
fn get_score_uses_the_legacy_fico_tiers_and_canonicalizes_the_uuid() {
    let r = plain(&[(U1, 600)]);
    let out = read(&r, "get_score", json!({"user": U1.to_uppercase()})).unwrap();
    assert_eq!(out, json!({"user": U1, "score": 600, "tier": "fair"}));
    for (score, tier) in [
        (300, "poor"),
        (579, "poor"),
        (580, "fair"),
        (669, "fair"),
        (670, "good"),
        (739, "good"),
        (740, "very_good"),
        (799, "very_good"),
        (800, "exceptional"),
        (850, "exceptional"),
    ] {
        let r = plain(&[(U1, score)]);
        assert_eq!(
            read(&r, "get_score", json!({"user": U1})).unwrap()["tier"],
            tier,
            "{score}"
        );
    }
}

#[test]
fn adjust_takes_a_caller_delta_bounded_to_the_activity_cap_and_needs_a_key() {
    let r = plain(&[(U1, 600)]);
    let out = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": 3, "reason": "activity.follow"}),
    )
    .unwrap();
    assert_eq!(out["score"], 603);
    assert_eq!(out["delta"], 3);
    assert_eq!(out["applied"], true);
    assert_eq!(
        out["floors"],
        json!({"community": "disabled", "tenant": "disabled"})
    );
    assert!(
        r.host
            .log()
            .contains(&format!("apply:{U1}:3:activity.follow"))
    );
    // No weights/event mapping any more: the old `event` argument is unknown.
    assert!(matches!(
        write(&r, "adjust", json!({"user": U1, "event": "win"})),
        Err(ToolError::InvalidArgs(m)) if m.contains("unknown field")
    ));
    // A write without an idempotency key is refused before any host call.
    let before = r.host.log().len();
    assert!(matches!(
        read(&r, "adjust", json!({"user": U1, "delta": 1, "reason": "a.b"})),
        Err(ToolError::InvalidArgs(m)) if m.contains("idempotency")
    ));
    assert_eq!(r.host.log().len(), before);
    for bad in [
        json!({"user": U1, "delta": 0, "reason": "a.b"}),
        json!({"user": U1, "delta": 6, "reason": "a.b"}),
        json!({"user": U1, "delta": -6, "reason": "a.b"}),
        json!({"user": U1, "delta": i64::from(i32::MAX) + 1, "reason": "a.b"}),
        json!({"user": U1, "delta": 1, "reason": "Has Space"}),
        json!({"user": U1, "delta": 1}),
        json!({"user": "bob", "delta": 1, "reason": "a.b"}),
        json!({"user": U1, "delta": 1, "reason": "a.b", "platform": "irc"}),
    ] {
        assert!(
            matches!(
                write(&r, "adjust", bad.clone()),
                Err(ToolError::InvalidArgs(_))
            ),
            "{bad}"
        );
    }
    assert_eq!(
        r.host.log().len(),
        before,
        "validation precedes every host call"
    );
}

#[test]
fn apply_moderation_allows_the_larger_cap_and_clamps_at_the_scale_floor() {
    let r = plain(&[(U1, 600)]);
    let out = write(
        &r,
        "apply_moderation",
        json!({"user": U1, "delta": -200, "reason": "moderation.ban"}),
    )
    .unwrap();
    assert_eq!(out["score"], 400);
    assert!(
        write(
            &r,
            "apply_moderation",
            json!({"user": U1, "delta": -201, "reason": "m.x"})
        )
        .is_err()
    );
    assert!(
        write(
            &r,
            "apply_moderation",
            json!({"user": U1, "delta": 0, "reason": "m.x"})
        )
        .is_err()
    );
    // Activity path stays at +/-5.
    assert!(
        write(
            &r,
            "adjust",
            json!({"user": U1, "delta": -200, "reason": "m.x"})
        )
        .is_err()
    );
    let r = plain(&[(U1, 320)]);
    assert_eq!(
        write(
            &r,
            "apply_moderation",
            json!({"user": U1, "delta": -200, "reason": "m.x"})
        )
        .unwrap()["score"],
        300,
        "the scale floor is 300"
    );
}

#[test]
fn floors_do_nothing_while_the_auto_ban_flag_is_off() {
    let r = rig(
        FakeHost::with(&[(U1, 600)]),
        FakeCfg::with(&[("floors", FLOORS_ON)]),
        FakePlatform::new(false),
    );
    let out = write(
        &r,
        "apply_moderation",
        json!({"user": U1, "delta": -200, "reason": "m.x", "platform": "twitch"}),
    )
    .unwrap();
    assert_eq!(
        out["floors"],
        json!({"community": "disabled", "tenant": "disabled"})
    );
    assert!(r.platform.bans.lock().unwrap().is_empty());
    assert!(r.host.soft.lock().unwrap().is_empty());
    assert!(
        !r.host
            .log()
            .iter()
            .any(|c| c.starts_with("soft_ban") || c.starts_with("tenant_policy"))
    );
}

#[test]
fn the_hard_floor_bans_on_the_platform_with_its_own_audit_reason() {
    let r = rig(
        FakeHost::with(&[(U1, 600)]),
        FakeCfg::with(&[("floors", FLOORS_ON)]),
        FakePlatform::new(true),
    );
    // 600 -> 399 is not reachable in one step (cap 200): 600 - 200 = 400 -> not below 400.
    let out = write(
        &r,
        "apply_moderation",
        json!({"user": U1, "delta": -200, "reason": "m.x", "platform": "twitch"}),
    )
    .unwrap();
    assert_eq!(out["score"], 400);
    assert_eq!(
        out["floors"]["community"], "soft_ban",
        "400 is below the soft floor (550) but not below hard (400)"
    );
    let out = write(
        &r,
        "apply_moderation",
        json!({"user": U1, "delta": -1, "reason": "m.x", "platform": "twitch"}),
    )
    .unwrap();
    assert_eq!(out["score"], 399);
    assert_eq!(out["floors"]["community"], "ban");
    assert_eq!(
        r.platform.bans.lock().unwrap().clone(),
        vec![format!("twitch:{U1}:community_hard_floor:k1:hard")]
    );
}

#[test]
fn the_soft_floor_soft_bans_then_recovery_lifts_only_the_floor_triggered_ban() {
    let r = rig(
        FakeHost::with(&[(U1, 553)]),
        FakeCfg::with(&[("floors", FLOORS_ON)]),
        FakePlatform::new(true),
    );
    // 553 - 5 = 548 < 550 -> soft ban, reason community_soft_floor.
    let out = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -5, "reason": "a.x"}),
    )
    .unwrap();
    assert_eq!(out["floors"]["community"], "soft_ban");
    assert_eq!(r.host.soft.lock().unwrap().get(U1).unwrap(), REASON_SOFT);
    // Back above the floor -> lifted.
    let out = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": 5, "reason": "a.x"}),
    )
    .unwrap();
    assert_eq!(out["floors"]["community"], "soft_ban_lifted");
    assert!(r.host.soft.lock().unwrap().is_empty());
    // A MANUAL soft ban (another reason) is never cleared by recovery.
    r.host
        .soft
        .lock()
        .unwrap()
        .insert(U1.into(), "manual".into());
    let out = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": 5, "reason": "a.x"}),
    )
    .unwrap();
    assert_eq!(out["floors"]["community"], "none");
    assert_eq!(r.host.soft.lock().unwrap().get(U1).unwrap(), "manual");
}

#[test]
fn a_disabled_floor_never_fires_even_below_it() {
    let r = rig(
        FakeHost::with(&[(U1, 420)]),
        FakeCfg::with(&[(
            "floors",
            r#"{"soft_ban_enabled":false,"hard_ban_enabled":false}"#,
        )]),
        FakePlatform::new(true),
    );
    let out = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -5, "reason": "a.x", "platform": "twitch"}),
    )
    .unwrap();
    assert_eq!(out["floors"]["community"], "none");
    assert!(r.platform.bans.lock().unwrap().is_empty());
}

#[test]
fn the_tenant_floor_bans_with_the_tenant_reason_independent_of_the_community_floors() {
    let host = FakeHost::with(&[(U1, 700)]);
    *host.tenant.lock().unwrap() = Some(TenantPolicy {
        auto_ban_enabled: true,
        auto_ban_floor: DEFAULT_TENANT_FLOOR,
        at_risk_threshold: 450,
    });
    host.tenant_scores.lock().unwrap().insert(U1.into(), 340);
    let r = rig(host, FakeCfg::with(&[]), FakePlatform::new(true));
    let out = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": 1, "reason": "a.x", "platform": "discord"}),
    )
    .unwrap();
    assert_eq!(out["floors"], json!({"community": "none", "tenant": "ban"}));
    assert_eq!(
        r.platform.bans.lock().unwrap().clone(),
        vec![format!("discord:{U1}:tenant_floor:k1:tenant")]
    );
    // A tenant aggregate at/above the floor, or a disabled tenant policy: no ban.
    r.host.tenant_scores.lock().unwrap().insert(U1.into(), 350);
    r.platform.bans.lock().unwrap().clear();
    assert_eq!(
        write(
            &r,
            "adjust",
            json!({"user": U1, "delta": 1, "reason": "a.x", "platform": "discord"})
        )
        .unwrap()["floors"]["tenant"],
        "none"
    );
    r.host
        .tenant
        .lock()
        .unwrap()
        .as_mut()
        .unwrap()
        .auto_ban_enabled = false;
    r.host.tenant_scores.lock().unwrap().insert(U1.into(), 301);
    assert_eq!(
        write(
            &r,
            "adjust",
            json!({"user": U1, "delta": 1, "reason": "a.x", "platform": "discord"})
        )
        .unwrap()["floors"]["tenant"],
        "none"
    );
    assert!(r.platform.bans.lock().unwrap().is_empty());
}

#[test]
fn a_ban_that_cannot_run_is_reported_loudly_and_never_fails_the_applied_score() {
    let r = rig(
        FakeHost::with(&[(U1, 399)]),
        FakeCfg::with(&[("floors", FLOORS_ON)]),
        FakePlatform::failing_ban("forbidden"),
    );
    let out = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -1, "reason": "a.x", "platform": "twitch"}),
    )
    .unwrap();
    assert_eq!(out["score"], 398, "the score change already applied");
    assert_eq!(out["floors"]["community"], "ban_failed:forbidden");
    // No platform -> a hard-floor ban cannot be attempted: explicit, not silent.
    let r = rig(
        FakeHost::with(&[(U1, 399)]),
        FakeCfg::with(&[("floors", FLOORS_ON)]),
        FakePlatform::new(true),
    );
    let out = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -1, "reason": "a.x"}),
    )
    .unwrap();
    assert_eq!(out["floors"]["community"], "ban_failed:platform_required");
}

#[test]
fn a_corrupt_floor_policy_fails_loud_it_is_never_silently_defaulted() {
    for bad in [
        "not json",
        r#"{"unknown":1}"#,
        r#"{"hard_ban_floor":600}"#,
        r#"{"soft_ban_floor":300}"#,
        r#"{"at_risk_threshold":851}"#,
        r#"{"hard_ban_floor":299}"#,
    ] {
        let r = rig(
            FakeHost::with(&[(U1, 600)]),
            FakeCfg::with(&[("floors", bad)]),
            FakePlatform::new(true),
        );
        assert!(
            matches!(read(&r, "check", json!({"user": U1})), Err(ToolError::InvalidArgs(m)) if m == "invalid floors config"),
            "{bad}"
        );
    }
    // A broken kv is unavailable, not defaulted.
    let r = rig(
        FakeHost::with(&[(U1, 600)]),
        FakeCfg::broken(),
        FakePlatform::new(false),
    );
    assert!(matches!(
        read(&r, "check", json!({"user": U1})),
        Err(ToolError::TargetUnavailable)
    ));
}

#[test]
fn floor_defaults_are_the_documented_disabled_values() {
    let p = FloorPolicy::default();
    assert_eq!(
        (
            p.hard_ban_enabled,
            p.hard_ban_floor,
            p.soft_ban_enabled,
            p.soft_ban_floor,
            p.at_risk_threshold
        ),
        (false, 400, false, 550, 600)
    );
    assert_eq!(DEFAULT_TENANT_FLOOR, 350);
    assert!(p.validate().is_ok());
}

#[test]
fn set_policy_is_admin_only_validates_merges_and_persists() {
    let r = plain(&[]);
    // Not admin -> forbidden, nothing stored.
    assert!(matches!(
        write(&r, "set_policy", json!({"soft_ban_enabled": true})),
        Err(ToolError::Forbidden)
    ));
    assert!(r.cfg.0.lock().unwrap().is_empty());
    // A moderator (not admin) is also refused.
    assert!(matches!(
        run(
            &r,
            "set_policy",
            json!({"soft_ban_enabled": true}),
            ctx(Some("k"), Some(U2), &["community:moderate"])
        ),
        Err(ToolError::Forbidden)
    ));
    // Needs a key.
    assert!(matches!(
        run(
            &r,
            "set_policy",
            json!({}),
            ctx(None, Some(ADMIN), &[ADMIN_SCOPE])
        ),
        Err(ToolError::InvalidArgs(_))
    ));
    // Merge over defaults.
    let out = as_admin(
        &r,
        "set_policy",
        json!({"hard_ban_enabled": true, "hard_ban_floor": 420}),
    )
    .unwrap();
    assert_eq!(out["hard_ban_enabled"], true);
    assert_eq!(out["hard_ban_floor"], 420);
    assert_eq!(out["soft_ban_floor"], 550);
    // Second call merges over the stored value.
    let out = as_admin(&r, "set_policy", json!({"soft_ban_floor": 500})).unwrap();
    assert_eq!(
        (out["hard_ban_floor"].clone(), out["soft_ban_floor"].clone()),
        (json!(420), json!(500))
    );
    // Invalid ordering / bounds are rejected and leave the stored policy intact.
    for bad in [
        json!({"hard_ban_floor": 500}),
        json!({"soft_ban_floor": 420}),
        json!({"at_risk_threshold": 500}),
        json!({"at_risk_threshold": 900}),
        json!({"hard_ban_floor": 200}),
        json!({"bogus": 1}),
    ] {
        assert!(as_admin(&r, "set_policy", bad.clone()).is_err(), "{bad}");
    }
    assert_eq!(load_floors(r.cfg.as_ref()).unwrap().soft_ban_floor, 500);
}

#[test]
fn get_policy_and_at_risk_are_admin_only_and_use_the_at_risk_threshold() {
    let r = plain(&[(U1, 590), (U2, 650)]);
    assert!(matches!(
        read(&r, "get_policy", json!({})),
        Err(ToolError::Forbidden)
    ));
    assert!(matches!(
        read(&r, "at_risk", json!({})),
        Err(ToolError::Forbidden)
    ));
    let p = as_admin(&r, "get_policy", json!({})).unwrap();
    assert_eq!(p["community"]["at_risk_threshold"], 600);
    assert_eq!(p["tenant"], Value::Null);
    assert_eq!(p["auto_ban_flag"], false);
    let out = as_admin(&r, "at_risk", json!({})).unwrap();
    assert_eq!(out["threshold"], 600);
    assert_eq!(out["entries"].as_array().unwrap().len(), 1);
    assert_eq!(out["entries"][0]["user"], U1);
}

#[test]
fn get_tenant_score_is_the_verified_actors_own_aggregate_only() {
    let r = plain(&[(U1, 600)]);
    r.host.tenant_scores.lock().unwrap().insert(U1.into(), 512);
    let out = run(&r, "get_tenant_score", json!({}), ctx(None, Some(U1), &[])).unwrap();
    assert_eq!(out["score"], 512);
    assert_eq!(out["user"], U1);
    assert_eq!(out["communities"], 2);
    assert!(r.host.log().contains(&format!("get_tenant:{U1}")));
    // No `user` argument exists: a caller cannot read someone else's aggregate.
    assert!(matches!(
        run(&r, "get_tenant_score", json!({"user": U2}), ctx(None, Some(U1), &[])),
        Err(ToolError::InvalidArgs(m)) if m.contains("unknown field")
    ));
    // No verified actor -> forbidden, host untouched.
    let before = r.host.log().len();
    assert!(matches!(
        read(&r, "get_tenant_score", json!({})),
        Err(ToolError::Forbidden)
    ));
    assert_eq!(r.host.log().len(), before);
}

#[test]
fn history_is_own_or_admin_and_pages_with_a_bounded_cursor() {
    let r = plain(&[(U1, 600)]);
    // Own history.
    let out = run(
        &r,
        "history",
        json!({"user": U1, "limit": 5, "before": 9}),
        ctx(None, Some(U1), &[]),
    )
    .unwrap();
    assert_eq!(out["entries"][0]["id"], 7);
    assert!(r.host.log().contains(&format!("history:{U1}:5:Some(9)")));
    // Someone else's -> forbidden for a member, allowed for an admin.
    assert!(matches!(
        run(&r, "history", json!({"user": U1}), ctx(None, Some(U2), &[])),
        Err(ToolError::Forbidden)
    ));
    assert!(matches!(
        read(&r, "history", json!({"user": U1})),
        Err(ToolError::Forbidden)
    ));
    assert!(
        run(
            &r,
            "history",
            json!({"user": U1}),
            ctx(None, Some(ADMIN), &[ADMIN_SCOPE])
        )
        .is_ok()
    );
    for bad in [
        json!({"user": U1, "limit": 0}),
        json!({"user": U1, "limit": 101}),
        json!({"user": U1, "before": 0}),
    ] {
        assert!(matches!(
            run(&r, "history", bad, ctx(None, Some(U1), &[])),
            Err(ToolError::InvalidArgs(_))
        ));
    }
}

#[test]
fn check_reports_soft_ban_and_at_risk_and_is_soft_banned_is_cheap() {
    let r = plain(&[(U1, 590)]);
    r.host
        .soft
        .lock()
        .unwrap()
        .insert(U1.into(), "manual".into());
    let out = read(&r, "check", json!({"user": U1})).unwrap();
    assert_eq!(out["soft_banned"], true);
    assert_eq!(out["at_risk"], true);
    assert_eq!(out["tier"], "fair");
    assert_eq!(
        read(&r, "is_soft_banned", json!({"user": U1})).unwrap(),
        json!({"user": U1, "soft_banned": true})
    );
    assert_eq!(
        read(&r, "is_soft_banned", json!({"user": U2})).unwrap()["soft_banned"],
        false
    );
}

#[test]
fn override_is_admin_only_keyed_and_checked_before_any_host_call() {
    let r = plain(&[(U1, 600)]);
    let args = json!({"user": U1, "absolute_score": 450, "reason": "admin.fix"});
    assert!(matches!(
        write(&r, "override", args.clone()),
        Err(ToolError::Forbidden)
    ));
    assert!(!r.host.log().iter().any(|c| c.starts_with("override")));
    let out = as_admin(&r, "override", args).unwrap();
    assert_eq!(out["score"], 450);
    // Clamped by the host to the scale.
    assert_eq!(
        as_admin(&r, "override", json!({"user": U1, "absolute_score": 9999})).unwrap()["score"],
        850
    );
}

#[test]
fn leaderboard_ranks_entries_community_scoped_and_validates_limit() {
    let r = plain(&[(U1, 700), (U2, 800)]);
    let out = read(&r, "leaderboard", json!({"limit": 5})).unwrap();
    assert_eq!(out["entries"][0]["rank"], 1);
    assert_eq!(out["entries"][0]["user"], U2);
    assert!(read(&r, "leaderboard", json!({"limit": 0})).is_err());
    assert!(read(&r, "leaderboard", json!({"limit": 101})).is_err());
    assert!(read(&r, "leaderboard", json!({"scope": "tenant"})).is_err());
}

#[test]
fn rep_reports_progress_to_the_next_tier_and_the_top_rung() {
    let r = plain(&[(U1, 600), (U2, 820)]);
    let out = read(&r, "rep", json!({"user": U1})).unwrap();
    assert_eq!(out["next_tier"], json!({"name": "good", "needed": 70}));
    let out = read(&r, "rep", json!({"user": U2})).unwrap();
    assert_eq!(out["next_tier"], Value::Null);
    assert!(out["text"].as_str().unwrap().contains("top tier"));
}

#[test]
fn tier_config_is_validated_and_drives_the_tier_names() {
    let cfg = |s: &str| {
        rig(
            FakeHost::with(&[(U1, 600)]),
            FakeCfg::with(&[("tiers", s)]),
            FakePlatform::new(false),
        )
    };
    let r = cfg(r#"[{"name":"low","min_score":300},{"name":"high","min_score":700}]"#);
    assert_eq!(
        read(&r, "get_score", json!({"user": U1})).unwrap()["tier"],
        "low"
    );
    for bad in [
        "[]",
        "nope",
        r#"[{"name":"a","min_score":400}]"#,
        r#"[{"name":"a","min_score":300},{"name":"b","min_score":300}]"#,
        r#"[{"name":"A","min_score":300}]"#,
        r#"[{"name":"a","min_score":300},{"name":"b","min_score":900}]"#,
    ] {
        assert!(
            matches!(read(&cfg(bad), "get_score", json!({"user": U1})), Err(ToolError::InvalidArgs(m)) if m == "invalid tier config"),
            "{bad}"
        );
    }
}

#[test]
fn host_failures_map_to_stable_tool_errors_without_leaking_detail() {
    for (e, expect) in [
        (HostError::NotAMember, "not_a_member"),
        (HostError::DailyCapExceeded, "daily_cap_exceeded"),
        (HostError::Backend("pg: secret dsn".into()), "backend"),
        (
            HostError::Denied("quota_exceeded".into()),
            "denied:quota_exceeded",
        ),
    ] {
        let r = rig(
            FakeHost::failing(e),
            FakeCfg::with(&[]),
            FakePlatform::new(false),
        );
        match read(&r, "get_score", json!({"user": U1})) {
            Err(ToolError::InvalidArgs(m)) => assert_eq!(m, expect),
            other => panic!("{other:?}"),
        }
    }
    let r = rig(
        FakeHost::failing(HostError::Denied("not_granted".into())),
        FakeCfg::with(&[]),
        FakePlatform::new(false),
    );
    assert!(matches!(
        read(&r, "get_score", json!({"user": U1})),
        Err(ToolError::NotGranted)
    ));
    let r = rig(
        FakeHost::failing(HostError::Denied("forbidden".into())),
        FakeCfg::with(&[]),
        FakePlatform::new(false),
    );
    assert!(matches!(
        read(&r, "get_score", json!({"user": U1})),
        Err(ToolError::Forbidden)
    ));
    let r = rig(
        FakeHost::failing(HostError::Unavailable("x".into())),
        FakeCfg::with(&[]),
        FakePlatform::new(false),
    );
    assert!(matches!(
        read(&r, "get_score", json!({"user": U1})),
        Err(ToolError::TargetUnavailable)
    ));
    // A rate-limited soft ban surfaces as a typed denial in the floor result.
    assert_eq!(
        host_code(&HostError::Denied("rate_limited".into())),
        "rate_limited"
    );
}

#[test]
fn unknown_tool_names_are_never_logged_verbatim() {
    assert_eq!(tool_log_label("adjust"), "adjust");
    assert_eq!(tool_log_label("someone@example.com"), "unknown_tool");
}

#[test]
fn a_replayed_event_never_re_fires_a_floor_side_effect() {
    let r = rig(
        FakeHost::with(&[(U1, 399)]),
        FakeCfg::with(&[("floors", FLOORS_ON)]),
        FakePlatform::new(true),
    );
    // First delivery crosses the hard floor: banned once.
    let first = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -1, "reason": "a.x", "platform": "twitch"}),
    )
    .unwrap();
    assert_eq!(first["floors"]["community"], "ban");
    assert_eq!(r.platform.bans.lock().unwrap().len(), 1);
    // The platform ban is then manually cleared and a soft ban removed; the SAME
    // event is redelivered (host answers applied = false): no new ban, no soft ban.
    r.platform.bans.lock().unwrap().clear();
    r.host.soft.lock().unwrap().clear();
    *r.host.replay.lock().unwrap() = true;
    let calls_before = r.host.log().len();
    let again = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -1, "reason": "a.x", "platform": "twitch"}),
    )
    .unwrap();
    assert_eq!(again["applied"], false);
    assert_eq!(
        again["floors"],
        json!({"community": "replay_skipped", "tenant": "replay_skipped"})
    );
    assert!(r.platform.bans.lock().unwrap().is_empty(), "no re-ban");
    assert!(r.host.soft.lock().unwrap().is_empty(), "no re-soft-ban");
    let new_calls: Vec<String> = r.host.log()[calls_before..].to_vec();
    assert!(
        new_calls.iter().all(|c| c.starts_with("apply:")),
        "only the idempotent apply ran, no floor reads/writes: {new_calls:?}"
    );
    // Same for the moderation class.
    *r.host.replay.lock().unwrap() = true;
    let m = write(
        &r,
        "apply_moderation",
        json!({"user": U1, "delta": -50, "reason": "m.x", "platform": "twitch"}),
    )
    .unwrap();
    assert_eq!(m["floors"]["community"], "replay_skipped");
    assert!(r.platform.bans.lock().unwrap().is_empty());
}

#[test]
fn a_failed_first_enforcement_is_finished_by_the_retry_not_skipped_forever() {
    // First delivery: the score applies but enforcement fails (corrupt floors
    // config -> the tool errors AFTER the host applied the delta).
    let r = rig(
        FakeHost::with(&[(U1, 545)]),
        FakeCfg::with(&[("floors", "corrupt")]),
        FakePlatform::new(true),
    );
    assert!(
        write(
            &r,
            "adjust",
            json!({"user": U1, "delta": -1, "reason": "a.x"})
        )
        .is_err()
    );
    assert_eq!(
        *r.host.scores.lock().unwrap().get(U1).unwrap(),
        544,
        "score applied once"
    );
    assert!(r.host.soft.lock().unwrap().is_empty());
    // Config repaired; the retry of the SAME key replays the host apply
    // (applied = false) but no marker exists, so enforcement runs now.
    r.cfg
        .0
        .lock()
        .unwrap()
        .insert("floors".into(), FLOORS_ON.as_bytes().to_vec());
    *r.host.replay.lock().unwrap() = true;
    let out = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -1, "reason": "a.x"}),
    )
    .unwrap();
    assert_eq!(out["applied"], false);
    assert_eq!(out["floors"]["community"], "soft_ban");
    assert_eq!(r.host.soft.lock().unwrap().get(U1).unwrap(), REASON_SOFT);
    // Now complete: a further replay is a pure no-op.
    *r.host.replay.lock().unwrap() = true;
    let again = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -1, "reason": "a.x"}),
    )
    .unwrap();
    assert_eq!(again["floors"]["community"], "replay_skipped");
}

#[test]
fn a_ban_that_failed_is_retried_on_replay_until_it_completes() {
    let r = rig(
        FakeHost::with(&[(U1, 399)]),
        FakeCfg::with(&[("floors", FLOORS_ON)]),
        FakePlatform::failing_ban("timeout"),
    );
    let first = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -1, "reason": "a.x", "platform": "twitch"}),
    )
    .unwrap();
    assert_eq!(first["floors"]["community"], "ban_failed:timeout");
    // No marker was written (enforcement incomplete): the replay retries the ban
    // under the same derived ban key (idempotent at the platform bundle).
    *r.host.replay.lock().unwrap() = true;
    let _ = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -1, "reason": "a.x", "platform": "twitch"}),
    )
    .unwrap();
    let bans = r.platform.bans.lock().unwrap().clone();
    assert_eq!(bans.len(), 2);
    assert_eq!(bans[0], bans[1], "identical ban key both times");
}

#[test]
fn a_manually_cleared_soft_ban_stays_cleared_when_a_completed_event_replays() {
    let r = rig(
        FakeHost::with(&[(U1, 549)]),
        FakeCfg::with(&[("floors", FLOORS_ON)]),
        FakePlatform::new(true),
    );
    let first = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -1, "reason": "a.x"}),
    )
    .unwrap();
    assert_eq!(first["floors"]["community"], "soft_ban");
    // A moderator manually clears the soft ban.
    r.host.soft.lock().unwrap().clear();
    *r.host.replay.lock().unwrap() = true;
    let again = write(
        &r,
        "adjust",
        json!({"user": U1, "delta": -1, "reason": "a.x"}),
    )
    .unwrap();
    assert_eq!(again["floors"]["community"], "replay_skipped");
    assert!(r.host.soft.lock().unwrap().is_empty(), "stays cleared");
    // A DIFFERENT event (different key) is evaluated normally again.
    let reg_key = write_with_key(&r, "k2", json!({"user": U1, "delta": -1, "reason": "a.x"}));
    assert_eq!(reg_key["floors"]["community"], "soft_ban");
}

fn write_with_key(r: &Rig, key: &str, args: Value) -> Value {
    run(r, "adjust", args, ctx(Some(key), None, &[])).unwrap()
}

#[test]
fn the_completion_marker_key_is_a_hash_never_the_raw_idempotency_key() {
    let m = marker_key("tool:abc:def");
    assert!(m.starts_with("fd.") && !m.contains(':') && m.len() == 19);
    assert_ne!(marker_key("a"), marker_key("b"));
    assert_eq!(marker_key("a"), marker_key("a"));
}

#[test]
fn a_positive_moderation_delta_needs_the_admin_scope() {
    let r = plain(&[(U1, 600)]);
    let args = json!({"user": U1, "delta": 50, "reason": "moderation.restore"});
    let before = r.host.log().len();
    assert!(matches!(
        write(&r, "apply_moderation", args.clone()),
        Err(ToolError::Forbidden)
    ));
    assert!(matches!(
        run(
            &r,
            "apply_moderation",
            args.clone(),
            ctx(Some("k"), Some(U2), &["community:moderate"])
        ),
        Err(ToolError::Forbidden)
    ));
    assert_eq!(
        r.host.log().len(),
        before,
        "no host call for a refused caller"
    );
    assert_eq!(
        as_admin(&r, "apply_moderation", args).unwrap()["score"],
        650
    );
    // Negative moderation stays open to the logic bundle.
    assert!(
        write(
            &r,
            "apply_moderation",
            json!({"user": U1, "delta": -50, "reason": "moderation.ban"})
        )
        .is_ok()
    );
}
