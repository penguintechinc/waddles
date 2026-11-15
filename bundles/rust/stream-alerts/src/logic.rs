//! Host-independent stream-alert logic.
//!
//! The process stage maps a platform event to a text-only alert (never a name or id); the
//! action stage RE-VALIDATES that alert against the surface contract's bounds and publishes it
//! to this bundle's own namespaced surface. The action stage never trusts a surface id or a
//! payload shape from its envelope: the surface is the constant [`SURFACE`].

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// The only surface this bundle publishes to (namespaced under its app id).
pub const SURFACE: &str = "waddles.core.app.stream-alerts.alerts";

/// Contract bounds (`bundles/contracts/surfaces.v1.json`).
pub const MAX_TITLE: usize = 100;
pub const MAX_MESSAGE: usize = 300;
pub const MIN_DURATION_MS: u32 = 1_000;
pub const MAX_DURATION_MS: u32 = 30_000;
/// Cap on any numeric amount echoed to viewers.
pub const MAX_AMOUNT: u64 = 1_000_000_000;

const ALERT_TYPES: [&str; 5] = ["follow", "sub", "raid", "cheer", "donation"];

/// One alert frame; mirrors the contract schema exactly.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Alert {
    pub alert_type: String,
    pub title: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub amount: Option<u64>,
    pub duration_ms: u32,
}

impl Alert {
    /// Checks the contract bounds; the action stage calls this before every publish.
    pub fn validate(&self) -> Result<(), String> {
        if !ALERT_TYPES.contains(&self.alert_type.as_str()) {
            return Err("unsupported alert_type".into());
        }
        if self.title.chars().count() > MAX_TITLE || self.message.chars().count() > MAX_MESSAGE {
            return Err("title or message too long".into());
        }
        if self
            .title
            .chars()
            .chain(self.message.chars())
            .any(char::is_control)
        {
            return Err("control character in text".into());
        }
        if self.title.contains("{user:") || self.message.contains("{user:") {
            return Err("user placeholder not allowed".into());
        }
        if !(MIN_DURATION_MS..=MAX_DURATION_MS).contains(&self.duration_ms) {
            return Err("duration_ms out of range".into());
        }
        if self.amount.is_some_and(|a| a > MAX_AMOUNT) {
            return Err("amount out of range".into());
        }
        Ok(())
    }
}

/// Reads a JSON number or numeric string as a non-negative finite value.
fn num(v: &Value) -> Option<f64> {
    let f = match v {
        Value::Number(n) => n.as_f64()?,
        Value::String(s) => s.trim().parse::<f64>().ok()?,
        _ => return None,
    };
    (f.is_finite() && f >= 0.0).then_some(f)
}

/// Looks a field up in `metadata` first, then at the payload top level.
fn field<'a>(p: &'a Value, key: &str) -> Option<&'a Value> {
    p.get("metadata")
        .and_then(|m| m.get(key))
        .or_else(|| p.get(key))
}

/// Clamps a float amount into `0..=MAX_AMOUNT` (floors it).
fn amount(f: f64) -> u64 {
    if f >= MAX_AMOUNT as f64 {
        MAX_AMOUNT
    } else {
        f as u64
    }
}

/// Maps an event to an alert, or `None` when it is not an alert event. No name or id from the
/// payload is ever copied into the alert.
pub fn build_alert(event_type: &str, p: &Value) -> Option<Alert> {
    let (alert_type, title, message, amt, duration_ms) = match event_type {
        "channel.follow" | "channel.followed" => (
            "follow",
            "New follower!",
            "Thanks for following!".to_string(),
            None,
            5_000,
        ),
        "channel.subscribe" => {
            let tier = match field(p, "tier").and_then(num) {
                Some(t) if t == 2.0 || t == 2000.0 => 2,
                Some(t) if t == 3.0 || t == 3000.0 => 3,
                _ => 1,
            };
            (
                "sub",
                "New subscriber!",
                format!("Subscribed at Tier {tier}!"),
                None,
                6_000,
            )
        }
        "channel.subscription.gift" | "channel.subscription.gifts" => {
            let total = amount(field(p, "total").and_then(num).unwrap_or(1.0)).max(1);
            (
                "sub",
                "Gift subs!",
                format!("{total} gift sub(s)!"),
                Some(total),
                6_000,
            )
        }
        "channel.cheer" => {
            let bits = amount(field(p, "bits").and_then(num)?);
            (
                "cheer",
                "Cheer!",
                format!("{bits} bits!"),
                Some(bits),
                6_000,
            )
        }
        "channel.raid" => {
            let viewers = amount(field(p, "viewers").and_then(num).unwrap_or(0.0));
            (
                "raid",
                "Incoming raid!",
                format!("Raid of {viewers}!"),
                Some(viewers),
                8_000,
            )
        }
        "donation" | "tip" | "channel.donation" | "channel.tip" => {
            let amt = amount(field(p, "amount").and_then(num)?);
            (
                "donation",
                "Donation!",
                "Thank you for the support!".to_string(),
                Some(amt),
                8_000,
            )
        }
        _ => return None,
    };
    let alert = Alert {
        alert_type: alert_type.to_string(),
        title: title.to_string(),
        message,
        amount: amt,
        duration_ms,
    };
    alert.validate().ok()?;
    Some(alert)
}

/// Publishing seam (the host `overlay.publish-surface` import in the component).
pub trait Publisher {
    /// Publishes `payload_json` to `surface`; `Err((retryable, code))` on refusal.
    fn publish(&self, surface: &str, payload_json: &str) -> Result<(), (bool, String)>;
}

/// Action stage: parses and re-validates the alert, then publishes it to [`SURFACE`] only.
pub fn dispatch(publisher: &dyn Publisher, payload_json: &str) -> Result<(), (bool, String)> {
    let alert: Alert =
        serde_json::from_str(payload_json).map_err(|_| (false, "BAD_PAYLOAD".to_string()))?;
    alert
        .validate()
        .map_err(|_| (false, "BAD_PAYLOAD".to_string()))?;
    let body = serde_json::to_string(&alert).map_err(|_| (false, "BAD_PAYLOAD".to_string()))?;
    publisher.publish(SURFACE, &body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::cell::RefCell;

    #[derive(Default)]
    struct Fake {
        calls: RefCell<Vec<(String, String)>>,
    }

    impl Publisher for Fake {
        fn publish(&self, surface: &str, payload: &str) -> Result<(), (bool, String)> {
            self.calls
                .borrow_mut()
                .push((surface.to_string(), payload.to_string()));
            Ok(())
        }
    }

    #[test]
    fn maps_each_event_type_without_names() {
        let p = json!({"user_name": "someone", "metadata": {"tier": "3000", "bits": 250, "viewers": 42, "amount": 12.7}});
        for (ev, ty) in [
            ("channel.follow", "follow"),
            ("channel.subscribe", "sub"),
            ("channel.cheer", "cheer"),
            ("channel.raid", "raid"),
            ("donation", "donation"),
        ] {
            let a = build_alert(ev, &p).unwrap();
            assert_eq!(a.alert_type, ty);
            assert!(!serde_json::to_string(&a).unwrap().contains("someone"));
        }
        assert_eq!(
            build_alert("channel.subscribe", &p).unwrap().message,
            "Subscribed at Tier 3!"
        );
        assert_eq!(build_alert("donation", &p).unwrap().amount, Some(12));
        assert!(build_alert("chat.message", &p).is_none());
        assert!(build_alert("channel.cheer", &json!({})).is_none());
    }

    #[test]
    fn amounts_are_clamped_and_negative_rejected() {
        let a = build_alert("channel.cheer", &json!({"bits": 1e30})).unwrap();
        assert_eq!(a.amount, Some(MAX_AMOUNT));
        assert!(build_alert("channel.cheer", &json!({"bits": -5})).is_none());
    }

    #[test]
    fn dispatch_publishes_only_to_own_surface() {
        let fake = Fake::default();
        let a = build_alert("channel.follow", &json!({})).unwrap();
        dispatch(&fake, &serde_json::to_string(&a).unwrap()).unwrap();
        assert_eq!(fake.calls.borrow()[0].0, SURFACE);
    }

    /// Negative: a forged envelope can neither pick another surface nor smuggle fields/names.
    #[test]
    fn dispatch_rejects_forged_payloads() {
        let fake = Fake::default();
        let bad = [
            json!({"alert_type": "follow", "title": "t", "message": "m", "duration_ms": 5000, "surface": "alert"}),
            json!({"alert_type": "nope", "title": "t", "message": "m", "duration_ms": 5000}),
            json!({"alert_type": "follow", "title": "t", "message": "{user:abc}", "duration_ms": 5000}),
            json!({"alert_type": "follow", "title": "t\u{7}", "message": "m", "duration_ms": 5000}),
            json!({"alert_type": "follow", "title": "t", "message": "m", "duration_ms": 10}),
            json!({"alert_type": "follow", "title": "x".repeat(101), "message": "m", "duration_ms": 5000}),
            json!({"alert_type": "cheer", "title": "t", "message": "m", "amount": 2_000_000_000u64, "duration_ms": 5000}),
        ];
        for b in bad {
            let e = dispatch(&fake, &b.to_string()).unwrap_err();
            assert_eq!(e, (false, "BAD_PAYLOAD".to_string()), "{b}");
        }
        assert!(dispatch(&fake, "not json").is_err());
        assert!(fake.calls.borrow().is_empty());
    }
}
