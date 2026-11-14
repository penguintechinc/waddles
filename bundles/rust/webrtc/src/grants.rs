//! Canonical room access policy: the ONE `grants_for`.
//!
//! This file is the single source of truth for who may publish/subscribe in a
//! room and for the numbers a join token must respect. It is deliberately free of
//! host, wasm and crate-local types so two consumers compile the very same source:
//! the `webrtc` service bundle (`mod grants;`) and the `rooms` app bundle
//! (`#[path = "../../webrtc/src/grants.rs"] mod grants;`, so access decisions in
//! the app can never drift from the service's contract). The machine-readable copy
//! lives in `contract/grants.json`; the unit tests here and
//! `scripts/ci/tests/test_rooms_webrtc_contract.py` both replay that file against
//! this code and against hub-api's REST join derivation.
//!
//! Policy in one breath: closed/outside-window/locked rooms refuse non-staff; a
//! green room is staff-only; guests (opt-in per room) can only subscribe; a stage
//! lets only staff publish; everyone else publishes and subscribes. Tokens are
//! short-lived and identify the participant by UUID only -- never a display name.
// Each consumer uses a subset (the app uses `grants_for`, the service the hand
// roles and TTLs), so unused-item warnings would be noise in either build.
#![allow(dead_code)]

/// Lifetime (seconds) of a join token minted for a room join: short on purpose,
/// the credential only has to survive the initial WHIP/WHEP exchange.
pub const JOIN_TTL_SECS: i64 = 60;
/// Longest lifetime any join path may request (hub-api REST join included).
pub const JOIN_TTL_MAX_SECS: i64 = 900;

/// Access role from the per-room ACL, ordered `Guest < Member < Moderator < Admin`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum GrantRole {
    Guest,
    Member,
    Moderator,
    Admin,
}

impl GrantRole {
    /// Parses the closed wire form (`guest|member|moderator|admin`).
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "guest" => Some(Self::Guest),
            "member" => Some(Self::Member),
            "moderator" => Some(Self::Moderator),
            "admin" => Some(Self::Admin),
            _ => None,
        }
    }

    /// Staff (moderator or admin) bypass locks, windows and the green-room gate.
    pub fn is_staff(self) -> bool {
        self >= Self::Moderator
    }
}

/// How a room came to exist (decides stage/green-room rules).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GrantKind {
    Community,
    Event,
    Breakout,
    Stage,
    GreenRoom,
}

impl GrantKind {
    /// Parses the closed wire form used by the contract file.
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "community" => Some(Self::Community),
            "event" => Some(Self::Event),
            "breakout" => Some(Self::Breakout),
            "stage" => Some(Self::Stage),
            "green_room" => Some(Self::GreenRoom),
            _ => None,
        }
    }
}

/// Where `now` sits relative to the room's optional scheduled window.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Window {
    Unscheduled,
    Inside,
    Outside,
}

impl Window {
    /// Parses the closed wire form used by the contract file.
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "none" => Some(Self::Unscheduled),
            "inside" => Some(Self::Inside),
            "outside" => Some(Self::Outside),
            _ => None,
        }
    }
}

/// Everything about the room (besides kind and role) the policy looks at.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GrantCtx {
    pub closed: bool,
    pub locked: bool,
    pub allow_guests: bool,
    pub window: Window,
}

/// The media directions a token may carry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Grants {
    pub publish: bool,
    pub subscribe: bool,
}

/// Publish/subscribe grants `role` gets in a `kind` room under `ctx`, or the closed
/// reason it is refused. Pure and total; the first matching rule wins:
/// closed, window (non-staff), locked (non-staff), green room, guest, stage.
pub fn grants_for(kind: GrantKind, role: GrantRole, ctx: GrantCtx) -> Result<Grants, &'static str> {
    if ctx.closed {
        return Err("room is closed");
    }
    let staff = role.is_staff();
    if ctx.window == Window::Outside && !staff {
        return Err("room is outside its scheduled window");
    }
    if ctx.locked && !staff {
        return Err("room is locked");
    }
    let (publish, subscribe) = match (kind, role) {
        (GrantKind::GreenRoom, r) if !r.is_staff() => {
            return Err("green room is for moderators");
        }
        (_, GrantRole::Guest) if !ctx.allow_guests => {
            return Err("guests are not allowed in this room");
        }
        (_, GrantRole::Guest) => (false, true),
        (GrantKind::Stage, r) if !r.is_staff() => (false, true),
        _ => (true, true),
    };
    Ok(Grants { publish, subscribe })
}

/// The raised-hand tools. Authorization is split by TOOL, so the manifest
/// `required_scope` (enforced by the host before dispatch) does the gating and no
/// tool derives a privilege from a caller-supplied role: the own-hand tools need
/// `community:member`, everything that touches anyone else's hand or the whole
/// queue needs `community:moderate`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HandOp {
    Raise,
    Lower,
    LowerOther,
    List,
    ListAll,
    Ack,
    Reset,
}

impl HandOp {
    /// Every hand tool, for table-driven checks.
    pub const ALL: [HandOp; 7] = [
        Self::Raise,
        Self::Lower,
        Self::LowerOther,
        Self::List,
        Self::ListAll,
        Self::Ack,
        Self::Reset,
    ];

    /// The tool name.
    pub fn tool(self) -> &'static str {
        match self {
            Self::Raise => "room.hand.raise",
            Self::Lower => "room.hand.lower",
            Self::LowerOther => "room.hand.lower_other",
            Self::List => "room.hand.list",
            Self::ListAll => "room.hand.list_all",
            Self::Ack => "room.hand.ack",
            Self::Reset => "room.hand.reset",
        }
    }

    /// The svc-streaming host op behind the tool (`lower_other` and `list_all`
    /// share the op of their own-hand sibling; the host decides by the forwarded
    /// actor, never by the tool name).
    pub fn host_op(self) -> &'static str {
        match self {
            Self::Raise => "room.hand.raise",
            Self::Lower | Self::LowerOther => "room.hand.lower",
            Self::List | Self::ListAll => "room.hand.list",
            Self::Ack => "room.hand.ack",
            Self::Reset => "room.hand.reset",
        }
    }

    /// Lowest role that may call the tool at all.
    pub fn min_role(self) -> GrantRole {
        match self {
            Self::Raise | Self::Lower | Self::List => GrantRole::Member,
            Self::LowerOther | Self::ListAll | Self::Ack | Self::Reset => GrantRole::Moderator,
        }
    }

    /// The manifest `required_scope` the host verifies before dispatch.
    pub fn scope(self) -> &'static str {
        scope_for(self.min_role())
    }
}

/// The community scope a role bundle maps to (what the host verifies).
pub fn scope_for(role: GrantRole) -> &'static str {
    if role.is_staff() {
        "community:moderate"
    } else {
        "community:member"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    /// The contract file, embedded so the test never depends on the cwd.
    const CONTRACT: &str = include_str!("../contract/grants.json");

    fn contract() -> Value {
        serde_json::from_str(CONTRACT).unwrap()
    }

    fn ctx_of(c: &Value) -> GrantCtx {
        let b = |k: &str| c.get(k).and_then(Value::as_bool).unwrap_or(false);
        GrantCtx {
            closed: b("closed"),
            locked: b("locked"),
            allow_guests: b("allow_guests"),
            window: Window::parse(c.get("window").and_then(Value::as_str).unwrap_or("none"))
                .unwrap(),
        }
    }

    #[test]
    fn every_contract_case_matches_grants_for() {
        let c = contract();
        let cases = c["cases"].as_array().unwrap();
        assert!(
            cases.len() >= 20,
            "only {} contract cases examined",
            cases.len()
        );
        for case in cases {
            let id = case["id"].as_str().unwrap();
            let kind = GrantKind::parse(case["kind"].as_str().unwrap()).unwrap();
            let role = GrantRole::parse(case["role"].as_str().unwrap()).unwrap();
            let got = grants_for(kind, role, ctx_of(case));
            match (&case["expect"]["denied"], got) {
                (Value::String(want), Err(m)) => assert_eq!(m, want, "{id}"),
                (Value::Null, Ok(g)) => {
                    assert_eq!(g.publish, case["expect"]["publish"], "{id} publish");
                    assert_eq!(g.subscribe, case["expect"]["subscribe"], "{id} subscribe");
                }
                (want, got) => panic!("{id}: contract wants {want}, code gave {got:?}"),
            }
        }
    }

    #[test]
    fn a_viewer_never_publishes_and_staff_always_can() {
        for kind in [
            GrantKind::Community,
            GrantKind::Event,
            GrantKind::Breakout,
            GrantKind::Stage,
            GrantKind::GreenRoom,
        ] {
            for locked in [false, true] {
                for window in [Window::Unscheduled, Window::Inside, Window::Outside] {
                    let ctx = GrantCtx {
                        closed: false,
                        locked,
                        allow_guests: true,
                        window,
                    };
                    if let Ok(g) = grants_for(kind, GrantRole::Guest, ctx) {
                        assert!(!g.publish && g.subscribe, "{kind:?} guest");
                    }
                    for staff in [GrantRole::Moderator, GrantRole::Admin] {
                        assert_eq!(
                            grants_for(kind, staff, ctx),
                            Ok(Grants {
                                publish: true,
                                subscribe: true
                            }),
                            "{kind:?} {staff:?}"
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn token_policy_numbers_match_the_contract() {
        let c = contract();
        let t = &c["token"];
        assert_eq!(t["join_ttl_secs"], JOIN_TTL_SECS);
        assert_eq!(t["join_ttl_max_secs"], JOIN_TTL_MAX_SECS);
        const { assert!(JOIN_TTL_SECS <= JOIN_TTL_MAX_SECS) };
        assert!(JOIN_TTL_MAX_SECS <= t["ttl_max_secs"].as_i64().unwrap());
        assert!(JOIN_TTL_SECS >= t["ttl_min_secs"].as_i64().unwrap());
        assert_eq!(t["alg"], "ES256");
        assert!(!t["forbidden_claims"].as_array().unwrap().is_empty());
    }

    #[test]
    fn hand_scopes_and_roles_match_the_contract() {
        let c = contract();
        let hands = c["hands"].as_object().unwrap();
        assert_eq!(hands.len(), HandOp::ALL.len());
        for op in HandOp::ALL {
            let e = &hands[op.tool()];
            assert_eq!(e["scope"], op.scope(), "{}", op.tool());
            assert_eq!(e["host_op"], op.host_op(), "{}", op.tool());
            assert_eq!(
                GrantRole::parse(e["min_role"].as_str().unwrap()),
                Some(op.min_role()),
                "{}",
                op.tool()
            );
        }
        // Gating is by scope: nothing that touches another participant's hand or
        // the whole queue is reachable with the member scope.
        for op in [
            HandOp::LowerOther,
            HandOp::ListAll,
            HandOp::Ack,
            HandOp::Reset,
        ] {
            assert_eq!(op.scope(), "community:moderate", "{}", op.tool());
        }
        for op in [HandOp::Raise, HandOp::Lower, HandOp::List] {
            assert_eq!(op.scope(), "community:member", "{}", op.tool());
        }
        assert_eq!(
            c["hands_args_forbidden"],
            serde_json::json!(["actor_id", "actor_role"])
        );
    }
}
