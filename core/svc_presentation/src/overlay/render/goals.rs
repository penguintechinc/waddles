//! `goals` renderer -- #458's new goal-bar widget. No Python precedent;
//! validation requires `overlay_schema::GoalPayload` to be present with a
//! non-empty `label`, a non-negative `current`, and a strictly positive
//! `target` (a zero/negative target makes the progress fraction undefined
//! for the browser client to render).

use overlay_schema::OverlayPush;
use overlay_schema::Surface;

use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

/// `goals`' rendered content -- mirrors `overlay_schema::GoalPayload`
/// field-for-field.
#[derive(Debug, Clone, serde::Serialize, PartialEq)]
pub struct GoalsContent {
    pub label: String,
    pub current: f64,
    pub target: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
}

pub struct GoalsRenderer;

impl Renderer for GoalsRenderer {
    fn surface(&self) -> Surface {
        Surface::Goals
    }

    fn render(
        &self,
        push: &OverlayPush,
        _theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError> {
        let surface = self.surface();
        let goal = push.goal.as_ref().ok_or(RenderError::MissingField {
            surface,
            field: "goal",
        })?;
        if goal.label.trim().is_empty() {
            return Err(RenderError::InvalidField {
                surface,
                field: "label",
                reason: "must not be empty",
            });
        }
        if goal.target <= 0.0 {
            return Err(RenderError::InvalidField {
                surface,
                field: "target",
                reason: "must be greater than 0",
            });
        }
        if goal.current < 0.0 {
            return Err(RenderError::InvalidField {
                surface,
                field: "current",
                reason: "must not be negative",
            });
        }
        Ok(RenderedContent::Goals(GoalsContent {
            label: goal.label.clone(),
            current: goal.current,
            target: goal.target,
            unit: goal.unit.clone(),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use overlay_schema::GoalPayload;

    fn theme() -> ResolvedTheme {
        ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        }
    }

    fn valid_goal() -> GoalPayload {
        GoalPayload {
            label: "Followers".to_string(),
            current: 10.0,
            target: 100.0,
            unit: Some("followers".to_string()),
        }
    }

    #[test]
    fn renders_a_goal_bar() {
        let push = OverlayPush {
            goal: Some(valid_goal()),
            ..Default::default()
        };
        let frame = GoalsRenderer.render(&push, &theme()).unwrap();
        let RenderedContent::Goals(content) = frame else {
            panic!("expected Goals content");
        };
        assert_eq!(content.label, "Followers");
        assert_eq!(content.current, 10.0);
        assert_eq!(content.target, 100.0);
    }

    #[test]
    fn rejects_a_missing_goal_loudly() {
        let err = GoalsRenderer
            .render(&OverlayPush::default(), &theme())
            .unwrap_err();
        assert_eq!(
            err,
            RenderError::MissingField {
                surface: Surface::Goals,
                field: "goal",
            }
        );
    }

    #[test]
    fn rejects_a_zero_target_loudly() {
        let push = OverlayPush {
            goal: Some(GoalPayload {
                target: 0.0,
                ..valid_goal()
            }),
            ..Default::default()
        };
        let err = GoalsRenderer.render(&push, &theme()).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Goals,
                field: "target",
                reason: "must be greater than 0",
            }
        );
    }

    #[test]
    fn rejects_a_negative_current_loudly() {
        let push = OverlayPush {
            goal: Some(GoalPayload {
                current: -5.0,
                ..valid_goal()
            }),
            ..Default::default()
        };
        let err = GoalsRenderer.render(&push, &theme()).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Goals,
                field: "current",
                reason: "must not be negative",
            }
        );
    }

    #[test]
    fn rejects_an_empty_label_loudly() {
        let push = OverlayPush {
            goal: Some(GoalPayload {
                label: "".to_string(),
                ..valid_goal()
            }),
            ..Default::default()
        };
        let err = GoalsRenderer.render(&push, &theme()).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Goals,
                field: "label",
                reason: "must not be empty",
            }
        );
    }

    #[test]
    fn surface_reports_goals() {
        assert_eq!(GoalsRenderer.surface(), Surface::Goals);
    }
}
