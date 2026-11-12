//! The community reputation scale and reason classes (decisions D1/D2 of the
//! reputation unification plan).
//!
//! The legacy FICO-style scale is 300..=850 with a default of 600. The
//! persisted bounds are owned by the shared SQL function `reputation_apply()`
//! (created by the hub-api alembic migration); this module is the host-side
//! mirror used to validate a per-community policy and to bound a delta BEFORE
//! the store call, so a bundle can never request an out-of-class magnitude.

use crate::ReputationError;

/// Lowest legacy score.
pub const SCALE_MIN: i64 = 300;
/// Highest legacy score.
pub const SCALE_MAX: i64 = 850;
/// Score a member holds before any adjustment.
pub const SCALE_DEFAULT: i64 = 600;
/// Default per-call bound of an activity (accrual) delta.
pub const ACTIVITY_CAP: i32 = 5;
/// Hard ceiling of a moderation delta (the policy may only lower it).
pub const MODERATION_CAP: i32 = 200;

/// Why a delta is applied. The class selects the per-call magnitude bound and
/// is carried to `reputation_apply()` as its `p_reason_code` smallint.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum ReasonClass {
    /// Routine engagement (follow, sub, cheer, raid, ...): small deltas.
    Activity,
    /// Moderation outcome (ban, timeout): a larger, separately capped delta.
    Moderation,
}

impl ReasonClass {
    /// The smallint wire code (`reputation_apply`'s `p_reason_code`).
    pub fn code(self) -> i16 {
        match self {
            Self::Activity => 1,
            Self::Moderation => 2,
        }
    }

    /// Parses a wire code; unknown codes are rejected, never defaulted.
    pub fn from_code(code: i16) -> Result<Self, ReputationError> {
        match code {
            1 => Ok(Self::Activity),
            2 => Ok(Self::Moderation),
            _ => Err(ReputationError::Invalid(
                "unknown reason class code".to_string(),
            )),
        }
    }

    /// The stable name used on the guest-facing wire and in metrics.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Activity => "activity",
            Self::Moderation => "moderation",
        }
    }

    /// Parses a wire name (`activity` | `moderation`).
    pub fn parse(s: &str) -> Result<Self, ReputationError> {
        match s {
            "activity" => Ok(Self::Activity),
            "moderation" => Ok(Self::Moderation),
            _ => Err(ReputationError::Invalid(
                "reason class must be activity or moderation".to_string(),
            )),
        }
    }
}

/// A community's score scale plus its per-class delta bounds. Always lies
/// within the legacy 300..=850 envelope, so a policy can narrow the scale but
/// never widen it.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct ScoreScale {
    pub min: i64,
    pub max: i64,
    pub default: i64,
    pub activity_cap: i32,
    pub moderation_cap: i32,
}

impl ScoreScale {
    /// The legacy scale: 300..=850, starting at 600, activity +/-5,
    /// moderation +/-200.
    pub const LEGACY: ScoreScale = ScoreScale {
        min: SCALE_MIN,
        max: SCALE_MAX,
        default: SCALE_DEFAULT,
        activity_cap: ACTIVITY_CAP,
        moderation_cap: MODERATION_CAP,
    };

    /// Validates a per-policy scale: `300 <= min < default < max <= 850`,
    /// `1 <= activity_cap <= moderation_cap <= 200`.
    pub fn new(
        min: i64,
        max: i64,
        default: i64,
        activity_cap: i32,
        moderation_cap: i32,
    ) -> Result<Self, ReputationError> {
        let bad = |m: &str| Err(ReputationError::Invalid(m.to_string()));
        if min < SCALE_MIN || max > SCALE_MAX {
            return bad("scale must lie within 300..=850");
        }
        if !(min < default && default < max) {
            return bad("scale must satisfy min < default < max");
        }
        if activity_cap < 1 || activity_cap > moderation_cap || moderation_cap > MODERATION_CAP {
            return bad("caps must satisfy 1 <= activity <= moderation <= 200");
        }
        Ok(Self {
            min,
            max,
            default,
            activity_cap,
            moderation_cap,
        })
    }

    /// Clamps a raw score into the scale.
    pub fn clamp(&self, raw: i64) -> i64 {
        raw.clamp(self.min, self.max)
    }

    /// The per-call magnitude bound of `class`.
    pub fn cap_of(&self, class: ReasonClass) -> i32 {
        match class {
            ReasonClass::Activity => self.activity_cap,
            ReasonClass::Moderation => self.moderation_cap,
        }
    }

    /// Rejects a zero delta or one whose magnitude exceeds the class bound.
    pub fn check_delta(&self, class: ReasonClass, delta: i32) -> Result<(), ReputationError> {
        crate::validate_delta(delta)?;
        if i64::from(delta).abs() > i64::from(self.cap_of(class)) {
            return Err(ReputationError::Invalid(format!(
                "{} delta must be within +/-{}",
                class.as_str(),
                self.cap_of(class)
            )));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_scale_is_valid_and_matches_constants() {
        assert_eq!(
            ScoreScale::new(300, 850, 600, 5, 200).unwrap(),
            ScoreScale::LEGACY,
            "LEGACY must pass its own validation"
        );
    }

    #[test]
    fn clamp_pins_to_bounds() {
        let s = ScoreScale::LEGACY;
        assert_eq!(s.clamp(i64::MIN), 300);
        assert_eq!(s.clamp(299), 300);
        assert_eq!(s.clamp(600), 600);
        assert_eq!(s.clamp(851), 850);
        assert_eq!(s.clamp(i64::MAX), 850);
    }

    #[test]
    fn policy_may_narrow_but_never_widen() {
        assert!(ScoreScale::new(400, 800, 600, 3, 100).is_ok());
        assert!(ScoreScale::new(299, 850, 600, 5, 200).is_err());
        assert!(ScoreScale::new(300, 851, 600, 5, 200).is_err());
        assert!(ScoreScale::new(300, 850, 300, 5, 200).is_err());
        assert!(ScoreScale::new(300, 850, 850, 5, 200).is_err());
        assert!(ScoreScale::new(300, 850, 600, 0, 200).is_err());
        assert!(ScoreScale::new(300, 850, 600, 50, 10).is_err());
        assert!(ScoreScale::new(300, 850, 600, 5, 201).is_err());
    }

    #[test]
    fn class_caps_are_separate() {
        let s = ScoreScale::LEGACY;
        assert!(s.check_delta(ReasonClass::Activity, 5).is_ok());
        assert!(s.check_delta(ReasonClass::Activity, -5).is_ok());
        assert!(s.check_delta(ReasonClass::Activity, 6).is_err());
        assert!(s.check_delta(ReasonClass::Moderation, -200).is_ok());
        assert!(s.check_delta(ReasonClass::Moderation, 200).is_ok());
        assert!(s.check_delta(ReasonClass::Moderation, -201).is_err());
        assert!(s.check_delta(ReasonClass::Moderation, 0).is_err());
        assert!(s.check_delta(ReasonClass::Activity, i32::MIN).is_err());
    }

    #[test]
    fn reason_class_codes_round_trip_and_reject_unknown() {
        for c in [ReasonClass::Activity, ReasonClass::Moderation] {
            assert_eq!(ReasonClass::from_code(c.code()).unwrap(), c);
            assert_eq!(ReasonClass::parse(c.as_str()).unwrap(), c);
        }
        assert!(ReasonClass::from_code(0).is_err());
        assert!(ReasonClass::from_code(3).is_err());
        assert!(ReasonClass::parse("admin").is_err());
    }
}
