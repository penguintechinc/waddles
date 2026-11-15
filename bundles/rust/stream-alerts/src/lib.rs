//! Stream Alerts Presentation Bundle.
//!
//! Renders stream events (subscriptions, follows, cheers, raids) as custom overlay
//! components or surface events for the composite overlay builder.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum AlertType {
    Subscription,
    Follow,
    Cheer,
    Raid,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AlertEvent {
    pub alert_type: AlertType,
    pub title: String,
    pub message: String,
    pub image_url: Option<String>,
    pub duration_ms: u32,
}

impl AlertEvent {
    pub fn new_sub(tier: &str, months: u32) -> Self {
        Self {
            alert_type: AlertType::Subscription,
            title: "New Subscriber!".to_string(),
            message: format!("Subscribed at {tier} for {months} months!"),
            image_url: Some("https://assets.penguintech.cloud/alerts/sub_star.svg".to_string()),
            duration_ms: 6000,
        }
    }

    pub fn new_follow() -> Self {
        Self {
            alert_type: AlertType::Follow,
            title: "New Follower!".to_string(),
            message: "Thanks for following the stream!".to_string(),
            image_url: Some("https://assets.penguintech.cloud/alerts/heart.svg".to_string()),
            duration_ms: 5000,
        }
    }

    pub fn to_sanitized_html(&self) -> String {
        let (icon, bg) = match self.alert_type {
            AlertType::Subscription => ("⭐", "#ffd700"),
            AlertType::Follow => ("💖", "#ff69b4"),
            AlertType::Cheer => ("💎", "#00ffff"),
            AlertType::Raid => ("⚔️", "#ff4500"),
        };

        format!(
            "<div class=\"alert-box\" style=\"display:flex;align-items:center;background:rgba(20,20,30,0.85);border:2px solid {bg};border-radius:12px;padding:16px;color:#fff;box-shadow:0 8px 32px rgba(0,0,0,0.5);\">\
<div style=\"font-size:32px;margin-right:16px;\">{icon}</div>\
<div><div style=\"font-size:20px;font-weight:bold;color:{bg};\">{}</div><div style=\"font-size:15px;color:#e0e0e0;\">{}</div></div>\
</div>",
            self.title, self.message
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sub_alert_renders_html() {
        let alert = AlertEvent::new_sub("Tier 1", 3);
        let html = alert.to_sanitized_html();
        assert!(html.contains("New Subscriber!"));
        assert!(html.contains("Subscribed at Tier 1 for 3 months!"));
        assert!(html.contains("#ffd700"));
    }

    #[test]
    fn follow_alert_renders_html() {
        let alert = AlertEvent::new_follow();
        let html = alert.to_sanitized_html();
        assert!(html.contains("New Follower!"));
        assert!(html.contains("#ff69b4"));
    }
}
