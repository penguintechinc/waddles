//! P2: per-[`Surface`] renderers (contract C3's "push/render wire").
//!
//! Turns a validated [`OverlayPush`] (+ the community's stored
//! `presentation_config` theme, via [`RenderTheme`]) into the themed,
//! validated payload P3's live SSE/websocket hub and P4's push route
//! forward to the browser overlay client -- replacing today's Python
//! `services/presentation_hub.py`'s raw push pass-through with real
//! server-side validation, since `overlay_schema::OverlayPush`'s own doc
//! notes a bundle-origin push is no longer a trusted first-party caller
//! once this service owns rendering.
//!
//! One [`Renderer`] impl per [`Surface`] variant (one submodule each),
//! reached only through [`render`]/[`render_with_metrics`] -- route code
//! (P3/P4) never constructs a per-surface renderer directly. `image` is a
//! deliberate fail-loud stub ([`RenderError::NotYetImplemented`]) pending
//! the P9 image-surface slice; see `image`'s own module doc for why that
//! is not a silent default.
//!
//! `full_screen`/`media` share [`shared::TextImageContent`]/
//! [`shared::validate_text_image`]; `crawler`/`ticker` share
//! [`shared::TextContent`]/[`shared::validate_text`] -- both pairs mirror
//! an existing identical-shape pair in `render.py`/`overlay_schema`
//! respectively, so the shared validation lives once, not duplicated per
//! surface.

mod alert_box;
mod chat;
mod crawler;
mod error;
mod full_screen;
mod goals;
mod image;
mod media;
mod metrics;
mod music;
mod shared;
mod theme;
mod ticker;

pub use error::RenderError;
pub use metrics::{register_render_metrics, RenderMetrics};
pub use theme::{RenderTheme, ResolvedTheme};

pub use alert_box::AlertContent;
pub use chat::ChatContent;
pub use goals::GoalsContent;
pub use music::MusicContent;
pub use shared::{TextContent, TextImageContent};

use std::time::Instant;

use overlay_schema::{OverlayPush, Surface};
use serde::Serialize;

/// One surface's renderer: validates `push` against that surface's shape
/// and builds its rendered content. Implemented once per [`Surface`]
/// variant in this module's submodules; never called directly outside
/// this module -- use [`render`]/[`render_with_metrics`].
pub trait Renderer {
    /// The surface this renderer handles -- used for error/metric labels.
    fn surface(&self) -> Surface;
    /// Validates `push` and builds the rendered content for this surface.
    /// `theme` is the community's already-[`RenderTheme::resolve`]d theme
    /// -- most renderers don't need it today (only the eventual static
    /// page shell would), but every [`Renderer`] receives it so a future
    /// surface that *does* theme its content (e.g. a themed alert
    /// animation) doesn't need a trait-wide signature change.
    fn render(
        &self,
        push: &OverlayPush,
        theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError>;
}

/// The validated, themed output of rendering one [`OverlayPush`] against a
/// given [`Surface`] -- what P3's SSE/websocket hub and P4's push route
/// serialize and forward to the browser overlay client.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct RenderedFrame {
    pub surface: Surface,
    pub theme: ResolvedTheme,
    #[serde(flatten)]
    pub content: RenderedContent,
}

/// Per-surface rendered content, internally tagged by `"content_type"` so
/// a [`RenderedFrame`] stays one flat JSON object regardless of which
/// surface produced it -- the same "no extra wrapper" shape
/// `overlay_schema::OverlayEnvelope`'s own doc establishes for
/// [`OverlayPush`] itself. `Image` has no variant: [`image::ImageRenderer`]
/// never returns `Ok`.
#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "content_type", rename_all = "snake_case")]
pub enum RenderedContent {
    FullScreen(TextImageContent),
    Media(TextImageContent),
    Crawler(TextContent),
    Ticker(TextContent),
    Music(MusicContent),
    AlertBox(AlertContent),
    Chat(ChatContent),
    Goals(GoalsContent),
}

/// Returns the single [`Renderer`] for `surface`. Total over every
/// [`Surface::ALL`] member -- a `Surface` added to the enum without a
/// matching arm here is a compile error, not a silent gap.
fn renderer_for(surface: Surface) -> &'static dyn Renderer {
    match surface {
        Surface::FullScreen => &full_screen::FullScreenRenderer,
        Surface::Media => &media::MediaRenderer,
        Surface::Crawler => &crawler::CrawlerRenderer,
        Surface::Ticker => &ticker::TickerRenderer,
        Surface::Music => &music::MusicRenderer,
        Surface::AlertBox => &alert_box::AlertBoxRenderer,
        Surface::Chat => &chat::ChatRenderer,
        Surface::Goals => &goals::GoalsRenderer,
        Surface::Image => &image::ImageRenderer,
    }
}

/// Renders `push` against `surface`, using `theme`'s resolved (defaulted)
/// values. The single entry point P3's live SSE/websocket hub and P4's
/// push route call -- never a per-surface renderer directly.
pub fn render(
    surface: Surface,
    push: &OverlayPush,
    theme: &RenderTheme,
) -> Result<RenderedFrame, RenderError> {
    let resolved = theme.resolve();
    let content = renderer_for(surface).render(push, &resolved)?;
    Ok(RenderedFrame {
        surface,
        theme: resolved,
        content,
    })
}

/// Same as [`render`], plus: records [`RenderMetrics`] (a render-duration
/// histogram and an ok/error-labeled counter, both labeled by `surface`)
/// and logs the outcome at `DEBUG`/`WARN` -- surface and error shape only,
/// never push content (see `error`'s own module doc on why every
/// [`RenderError`] variant is PII-free by construction).
pub fn render_with_metrics(
    surface: Surface,
    push: &OverlayPush,
    theme: &RenderTheme,
    metrics: &RenderMetrics,
) -> Result<RenderedFrame, RenderError> {
    let start = Instant::now();
    let result = render(surface, push, theme);
    let elapsed = start.elapsed().as_secs_f64();

    let outcome = if result.is_ok() { "ok" } else { "error" };
    metrics
        .renders_total
        .with_label_values(&[surface.as_str(), outcome])
        .inc();
    metrics
        .render_duration_seconds
        .with_label_values(&[surface.as_str()])
        .observe(elapsed);

    match &result {
        Ok(_) => {
            tracing::debug!(surface = %surface, elapsed_ms = elapsed * 1000.0, "overlay push rendered")
        }
        Err(err) => {
            tracing::warn!(surface = %surface, error = %err, "overlay push rejected by renderer")
        }
    }

    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use overlay_schema::{AlertPayload, PushKind};

    fn theme() -> RenderTheme {
        RenderTheme::default()
    }

    #[test]
    fn render_dispatches_to_the_matching_surfaces_renderer() {
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            ..Default::default()
        };
        for surface in [Surface::FullScreen, Surface::Media] {
            let frame = render(surface, &push, &theme()).unwrap();
            assert_eq!(frame.surface, surface);
        }
    }

    #[test]
    fn render_dispatches_every_surface_without_panicking() {
        // A total smoke pass over Surface::ALL -- Image is expected to
        // error (fail-loud stub), every other surface either renders or
        // rejects the deliberately-empty default push, never panics.
        let push = OverlayPush::default();
        for surface in Surface::ALL {
            let _ = render(*surface, &push, &theme());
        }
    }

    #[test]
    fn render_resolves_the_theme_into_the_output_frame() {
        let theme = RenderTheme {
            primary_color: Some("#abcdef".to_string()),
            ..Default::default()
        };
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            ..Default::default()
        };
        let frame = render(Surface::FullScreen, &push, &theme).unwrap();
        assert_eq!(frame.theme.primary_color, "#abcdef");
        assert_eq!(frame.theme.secondary_color, theme::DEFAULT_SECONDARY_COLOR);
    }

    #[test]
    fn rendered_frame_serializes_as_one_flat_tagged_object() {
        let push = OverlayPush {
            alert: Some(AlertPayload {
                alert_type: "sub".to_string(),
                user: None,
                display_name: Some("Name".to_string()),
                amount: None,
                message: None,
            }),
            ..Default::default()
        };
        let frame = render(Surface::AlertBox, &push, &theme()).unwrap();
        let value = serde_json::to_value(&frame).unwrap();
        assert_eq!(value["surface"], "alert_box");
        assert_eq!(value["content_type"], "alert_box");
        assert_eq!(value["alert_type"], "sub");
        assert!(value["theme"]["primary_color"].is_string());
    }

    #[test]
    fn render_with_metrics_records_an_ok_outcome() {
        let registry = prometheus::Registry::new();
        let metrics = register_render_metrics(&registry);
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            ..Default::default()
        };
        let frame = render_with_metrics(Surface::Media, &push, &theme(), &metrics).unwrap();
        assert_eq!(frame.surface, Surface::Media);

        let rendered = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(rendered.contains("svc_presentation_overlay_renders_total"));
    }

    #[test]
    fn render_with_metrics_records_an_error_outcome_without_panicking() {
        let registry = prometheus::Registry::new();
        let metrics = register_render_metrics(&registry);
        let err = render_with_metrics(Surface::Image, &OverlayPush::default(), &theme(), &metrics)
            .unwrap_err();
        assert_eq!(
            err,
            RenderError::NotYetImplemented {
                surface: Surface::Image
            }
        );
    }

    #[test]
    fn image_surface_is_the_only_fail_loud_stub() {
        let push = OverlayPush::default();
        for surface in Surface::ALL {
            let result = render(*surface, &push, &theme());
            if *surface == Surface::Image {
                assert!(matches!(result, Err(RenderError::NotYetImplemented { .. })));
            }
        }
    }
}
