//! Galactic Avatars Presentation Bundle.
//!
//! Star Wars-inspired stream avatars running along the bottom stage strip of the stream overlay
//! with animated lightsabers, blasters, and droids.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Faction {
    Jedi,
    Sith,
    Mandalorian,
    Droid,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GalacticAvatar {
    pub name: String,
    pub faction: Faction,
    pub level: u32,
    pub saber_color: Option<String>,
}

impl GalacticAvatar {
    pub fn new_jedi(name: &str, level: u32) -> Self {
        Self {
            name: name.to_string(),
            faction: Faction::Jedi,
            level,
            saber_color: Some("#00e5ff".to_string()),
        }
    }

    pub fn new_sith(name: &str, level: u32) -> Self {
        Self {
            name: name.to_string(),
            faction: Faction::Sith,
            level,
            saber_color: Some("#ff1744".to_string()),
        }
    }

    pub fn to_html_avatar(&self) -> String {
        let (color, icon) = match self.faction {
            Faction::Jedi => ("#00e5ff", "⚡"),
            Faction::Sith => ("#ff1744", "🔥"),
            Faction::Mandalorian => ("#ffd700", "🛡️"),
            Faction::Droid => ("#76ff03", "🤖"),
        };

        format!(
            "<div class=\"galactic-avatar\" style=\"display:inline-flex;flex-direction:column;align-items:center;margin:0 12px;\">\
<div style=\"font-size:28px;filter:drop-shadow(0 0 8px {color});\">{icon}</div>\
<div style=\"font-size:12px;font-weight:bold;color:#fff;background:rgba(0,0,0,0.6);border-radius:4px;padding:2px 6px;margin-top:4px;\">{}</div>\
</div>",
            self.name
        )
    }
}

pub fn render_stage_strip(avatars: &[GalacticAvatar]) -> String {
    let mut inner = String::new();
    for a in avatars {
        inner.push_str(&a.to_html_avatar());
    }

    format!(
        "<div class=\"galactic-stage-strip\" style=\"display:flex;align-items:flex-end;height:100%;width:100%;background:rgba(10,12,24,0.4);border-bottom:3px solid #00e5ff;padding:8px 16px;box-sizing:border-box;\">\
{inner}\
</div>"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_jedi_and_sith_avatars() {
        let jedi = GalacticAvatar::new_jedi("Obi-Penguin", 50);
        let sith = GalacticAvatar::new_sith("Darth-Waddles", 99);
        let html = render_stage_strip(&[jedi, sith]);

        assert!(html.contains("Obi-Penguin"));
        assert!(html.contains("Darth-Waddles"));
        assert!(html.contains("#00e5ff"));
        assert!(html.contains("#ff1744"));
    }
}
