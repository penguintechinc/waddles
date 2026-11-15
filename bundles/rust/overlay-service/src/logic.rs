//! Host-independent Overlay Service tool logic.
//!
//! Tools are pure functions over two seams:
//! 1. [`OverlayHostStore`]: host-persisted scene layout, component instances, and widget registry.
//! 2. [`ConfigStore`]: bundle's own community-scoped kv store for themes and presets.
//!
//! Security invariants:
//! - Layout mutation tools (`add_component`, `update_component`, `remove_component`,
//!   `clear_layout`, `set_canvas_theme`) require `community:admin` scope (`ToolContext::has_scope`).
//! - Coordinates are strictly validated against canvas bounds (`0..=3840`, `0..=2160`).
//! - Custom HTML payloads are bounded to 16 KiB.
//! - IDs exposed across boundaries are UUID strings (`public_id`), never numeric PKs.

use std::fmt;
use std::sync::Arc;

use overlay_schema::{
    CanvasResolution, CanvasThemeDto, ComponentStatusCode, MAX_WIDGET_HTML_BYTES,
    SceneComponentDto, SceneLayoutDto, WidgetDescriptorDto, WidgetOutputKind, validate_coordinates,
};
use serde::Deserialize;
use serde_json::{Value, json};
use waddle_sdk::tool::{ToolContext, ToolError, ToolRegistry};

/// Admin scope required for layout-mutating operations.
pub const ADMIN_SCOPE: &str = "community:admin";

/// Errors returned by the host storage seam.
#[derive(Debug, PartialEq, Eq)]
pub enum HostStoreError {
    NotFound(String),
    AlreadyExists(String),
    CapacityReached(String),
    InvalidArgument(String),
    Backend(String),
}

impl fmt::Display for HostStoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotFound(s) => write!(f, "not found: {s}"),
            Self::AlreadyExists(s) => write!(f, "already exists: {s}"),
            Self::CapacityReached(s) => write!(f, "capacity reached: {s}"),
            Self::InvalidArgument(s) => write!(f, "invalid argument: {s}"),
            Self::Backend(s) => write!(f, "backend failure: {s}"),
        }
    }
}

impl std::error::Error for HostStoreError {}

/// Host capability seam for layout, component placement, and widget registration.
pub trait OverlayHostStore: Send + Sync {
    fn register_widget(&self, descriptor: &WidgetDescriptorDto) -> Result<(), HostStoreError>;
    fn get_widget(&self, widget_id: &str) -> Result<Option<WidgetDescriptorDto>, HostStoreError>;
    fn list_widgets(&self) -> Result<Vec<WidgetDescriptorDto>, HostStoreError>;

    fn push_widget(
        &self,
        widget_id: &str,
        surface: &str,
        output_kind: WidgetOutputKind,
        frame_json: &str,
    ) -> Result<(), HostStoreError>;

    fn get_layout(&self) -> Result<SceneLayoutDto, HostStoreError>;
    fn save_theme(&self, theme: &CanvasThemeDto) -> Result<(), HostStoreError>;

    fn add_component(&self, component: &SceneComponentDto) -> Result<(), HostStoreError>;
    fn update_component(
        &self,
        instance_id: &str,
        updater: Box<dyn FnOnce(&mut SceneComponentDto) + Send>,
    ) -> Result<SceneComponentDto, HostStoreError>;
    fn remove_component(&self, instance_id: &str) -> Result<bool, HostStoreError>;
    fn clear_components(&self) -> Result<u32, HostStoreError>;
}

/// Key-value storage seam. In the component this is the host `kv` import, which the host
/// scopes to the invocation's verified (tenant, community, app); a bundle never names either.
pub trait ConfigStore: Send + Sync {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    fn set(&self, key: &str, value: &[u8]) -> Result<(), String>;
}

/// Live-push seam (the host `overlay` import in the component). Must fail loudly when the
/// transport is unavailable; a push is never silently dropped.
pub trait PushSink: Send + Sync {
    fn push(
        &self,
        widget_id: &str,
        surface: &str,
        output_kind: WidgetOutputKind,
        frame_json: &str,
    ) -> Result<(), HostStoreError>;
}

/// Surfaces a non-contract widget may target (`overlay_schema::Surface` wire forms).
const PUSH_SURFACES: [&str; 10] = [
    "full_screen",
    "media",
    "crawler",
    "music",
    "alert_box",
    "chat",
    "goals",
    "ticker",
    "image",
    "caption",
];

/// Surface a widget publishes to: its own `surface_id`, defaulting to `full_screen`; an
/// unknown name is refused (fail closed) rather than redirected elsewhere.
fn widget_surface(widget: &WidgetDescriptorDto) -> Result<String, ToolError> {
    let s = widget.surface_id.as_deref().unwrap_or("full_screen");
    if PUSH_SURFACES.contains(&s) {
        Ok(s.to_string())
    } else {
        Err(ToolError::InvalidArgs(format!(
            "widget surface '{s}' is not a pushable overlay surface"
        )))
    }
}

/// Sanitizes bundle-supplied HTML with an allowlist: no scripts, no event handlers, no
/// `style`/`class` attributes, only `https` URLs. Never trust bundle markup.
fn sanitize_html(raw: &str) -> String {
    let mut b = ammonia::Builder::default();
    // Text-formatting tags only: no `a`/`img`/media, so bundle markup carries no URLs at all;
    // relative URLs (`//host`, `/path`, `../x`) are denied as well, as defence in depth.
    b.tags(std::collections::HashSet::from([
        "b", "i", "u", "em", "strong", "small", "span", "div", "p", "br", "ul", "ol", "li", "h1",
        "h2", "h3", "h4", "h5", "h6",
    ]));
    b.url_schemes(std::collections::HashSet::from(["https"]));
    b.url_relative(ammonia::UrlRelative::Deny);
    b.clean(raw).to_string()
}

/// Accepts only an `https://` URL with a host and no credentials.
fn validate_image_url(raw: &str) -> Result<(), ToolError> {
    let bad = || ToolError::InvalidArgs("image_url must be an https URL with a host".into());
    let u = url::Url::parse(raw).map_err(|_| bad())?;
    if u.scheme() != "https" || u.host_str().is_none() || !u.username().is_empty() {
        return Err(bad());
    }
    Ok(())
}

/// Shared push path for `push_widget` and the preset renderers: the actor must hold the
/// CURRENT `community:admin` scope (re-evaluated by the host on every call, so a demoted
/// admin loses push rights immediately), the widget must be registered, and its output kind
/// must match. `frame` carries the already-validated content fields.
fn push_checked(
    ctx: &ToolContext,
    host: &dyn OverlayHostStore,
    widget_id: &str,
    output_kind: WidgetOutputKind,
    frame: Value,
) -> Result<Value, ToolError> {
    if !ctx.has_scope(ADMIN_SCOPE) {
        return Err(ToolError::Forbidden);
    }
    let widget = host
        .get_widget(widget_id)
        .map_err(store_err)?
        .ok_or(ToolError::Forbidden)?;
    if widget.output_kind != output_kind {
        return Err(ToolError::InvalidArgs(
            "output_kind does not match the registered widget".into(),
        ));
    }
    let surface = widget_surface(&widget)?;
    let mut frame = frame;
    frame["widget_id"] = json!(widget_id);
    frame["output_kind"] = json!(output_kind.as_str());
    let frame_json =
        serde_json::to_string(&frame).map_err(|e| ToolError::InvalidArgs(e.to_string()))?;
    host.push_widget(widget_id, &surface, output_kind, &frame_json)
        .map_err(store_err)?;
    Ok(json!({ "status": "ok", "widget_id": widget_id }))
}

/// Maps a storage failure to a tool error without leaking backend detail; a backend failure
/// is `TargetUnavailable` (fail closed), never treated as "allowed".
fn store_err(e: HostStoreError) -> ToolError {
    match e {
        HostStoreError::Backend(_) => ToolError::TargetUnavailable,
        other => ToolError::InvalidArgs(other.to_string()),
    }
}

/// `true` when `id` is a valid widget id (`[a-z0-9][a-z0-9_.-]{0,127}`); also keeps ids safe
/// for use inside kv documents.
fn valid_widget_id(id: &str) -> bool {
    let mut chars = id.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase() || c.is_ascii_digit())
        && id.len() <= 128
        && id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '_' | '.' | '-'))
}

// ---------------------------------------------------------------------------
// Tool Request Payloads
// ---------------------------------------------------------------------------

/// `deny_unknown_fields`: a caller-supplied `bundle_app_id` (or any identity field) is
/// rejected, never honoured -- ownership comes only from the host-verified actor.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegisterWidgetArgs {
    pub widget_id: String,
    pub display_name: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub category: Option<String>,
    pub output_kind: String,
    #[serde(default)]
    pub surface_id: Option<String>,
    #[serde(default)]
    pub allow_standalone_output: Option<bool>,
    #[serde(default)]
    pub default_x: Option<i32>,
    #[serde(default)]
    pub default_y: Option<i32>,
    #[serde(default)]
    pub default_width: Option<i32>,
    #[serde(default)]
    pub default_height: Option<i32>,
    #[serde(default)]
    pub default_z_index: Option<i32>,
    #[serde(default)]
    pub props_schema: Option<Value>,
}

#[derive(Debug, Deserialize)]
pub struct PushWidgetArgs {
    pub widget_id: String,
    pub output_kind: String,
    #[serde(default)]
    pub html: Option<String>,
    #[serde(default)]
    pub image_url: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub surface_id: Option<String>,
    #[serde(default)]
    pub payload: Option<Value>,
}

/// Args for `push_alert_preset`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AlertPresetArgs {
    pub widget_id: String,
    pub alert: String,
    #[serde(default)]
    pub tier: Option<String>,
    #[serde(default)]
    pub months: Option<u32>,
}

/// Args for `push_stage_strip`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StageStripArgs {
    pub widget_id: String,
    pub avatars: Vec<galactic_avatars::GalacticAvatar>,
}

#[derive(Debug, Deserialize)]
pub struct AddComponentArgs {
    pub widget_id: String,
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
    #[serde(default)]
    pub z_index: Option<i32>,
    #[serde(default)]
    pub props: Option<Value>,
}

#[derive(Debug, Deserialize)]
pub struct UpdateComponentArgs {
    pub instance_id: String,
    #[serde(default)]
    pub x: Option<i32>,
    #[serde(default)]
    pub y: Option<i32>,
    #[serde(default)]
    pub width: Option<i32>,
    #[serde(default)]
    pub height: Option<i32>,
    #[serde(default)]
    pub z_index: Option<i32>,
    #[serde(default)]
    pub visible: Option<bool>,
    #[serde(default)]
    pub props: Option<Value>,
}

#[derive(Debug, Deserialize)]
pub struct RemoveComponentArgs {
    pub instance_id: String,
}

#[derive(Debug, Deserialize, Default)]
pub struct ListComponentsArgs {
    #[serde(default)]
    pub include_hidden: Option<bool>,
}

#[derive(Debug, Deserialize, Default)]
pub struct SetCanvasThemeArgs {
    #[serde(default)]
    pub theme: Option<String>,
    #[serde(default)]
    pub resolution: Option<String>,
    #[serde(default)]
    pub primary_color: Option<String>,
    #[serde(default)]
    pub secondary_color: Option<String>,
    #[serde(default)]
    pub font_family: Option<String>,
}

// ---------------------------------------------------------------------------
// Registry Builder
// ---------------------------------------------------------------------------

pub fn build_registry(host: Arc<dyn OverlayHostStore>) -> ToolRegistry {
    let mut reg = ToolRegistry::new();

    // 1. register_widget
    {
        let host = Arc::clone(&host);
        reg.register("register_widget", move |ctx: &ToolContext, val: Value| {
            // Deny by default: registering is an admin act by a host-verified actor.
            if !ctx.has_scope(ADMIN_SCOPE) {
                return Err(ToolError::Forbidden);
            }
            let owner = ctx.actor.clone().ok_or(ToolError::Forbidden)?;

            let args: RegisterWidgetArgs = serde_json::from_value(val)
                .map_err(|e| ToolError::InvalidArgs(format!("malformed args: {e}")))?;

            if !valid_widget_id(&args.widget_id) {
                return Err(ToolError::InvalidArgs(
                    "widget_id must match [a-z0-9][a-z0-9_.-]{0,127}".into(),
                ));
            }
            if args.display_name.trim().is_empty() || args.display_name.len() > 120 {
                return Err(ToolError::InvalidArgs(
                    "display_name must be 1-120 chars".into(),
                ));
            }

            let output_kind =
                WidgetOutputKind::from_str_name(&args.output_kind).ok_or_else(|| {
                    ToolError::InvalidArgs(format!(
                        "unsupported output_kind '{}'",
                        args.output_kind
                    ))
                })?;

            let def_x = args.default_x.unwrap_or(48);
            let def_y = args.default_y.unwrap_or(48);
            let def_w = args.default_width.unwrap_or(400);
            let def_h = args.default_height.unwrap_or(200);
            let def_z = args.default_z_index.unwrap_or(10);

            validate_coordinates(def_x, def_y, def_w, def_h, def_z)
                .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

            let surface_id = if output_kind == WidgetOutputKind::Surface {
                args.surface_id
            } else {
                let s = args.surface_id.unwrap_or_else(|| "full_screen".to_string());
                if !PUSH_SURFACES.contains(&s.as_str()) {
                    return Err(ToolError::InvalidArgs(format!(
                        "surface_id '{s}' is not a pushable overlay surface"
                    )));
                }
                Some(s)
            };
            // Never overwrite a widget another principal registered (hijack); a lookup
            // failure fails closed.
            match host.get_widget(&args.widget_id) {
                Ok(Some(existing)) if existing.bundle_app_id != owner => {
                    return Err(ToolError::Forbidden);
                }
                Ok(_) => {}
                Err(e) => return Err(store_err(e)),
            }
            let bundle_app_id = owner;

            let descriptor = WidgetDescriptorDto {
                widget_id: args.widget_id.clone(),
                bundle_app_id,
                display_name: args.display_name,
                description: args.description.unwrap_or_default(),
                category: args.category.unwrap_or_else(|| "custom".to_string()),
                output_kind,
                surface_id,
                allow_standalone_output: args.allow_standalone_output.unwrap_or(true),
                default_x: def_x,
                default_y: def_y,
                default_width: def_w,
                default_height: def_h,
                default_z_index: def_z,
                props_schema: args.props_schema.unwrap_or_else(|| json!({})),
            };

            host.register_widget(&descriptor).map_err(store_err)?;

            Ok(json!({
                "registered": true,
                "widget_id": args.widget_id,
                "output_kind": output_kind.as_str(),
                "output_kind_code": output_kind.to_code(),
            }))
        });
    }

    // 2. push_widget
    {
        let host = Arc::clone(&host);
        reg.register("push_widget", move |ctx: &ToolContext, val: Value| {
            let args: PushWidgetArgs = serde_json::from_value(val)
                .map_err(|e| ToolError::InvalidArgs(format!("malformed args: {e}")))?;
            if args.widget_id.is_empty() {
                return Err(ToolError::InvalidArgs("widget_id is required".into()));
            }
            let output_kind =
                WidgetOutputKind::from_str_name(&args.output_kind).ok_or_else(|| {
                    ToolError::InvalidArgs(format!(
                        "unsupported output_kind '{}'",
                        args.output_kind
                    ))
                })?;
            if let Some(h) = args.html.as_ref()
                && h.len() > MAX_WIDGET_HTML_BYTES
            {
                return Err(ToolError::InvalidArgs(format!(
                    "html payload exceeds {MAX_WIDGET_HTML_BYTES} bytes"
                )));
            }
            if let Some(u) = args.image_url.as_deref() {
                validate_image_url(u)?;
            }
            let html = args.html.as_deref().map(sanitize_html);
            let frame = json!({
                "html": html,
                "image_url": args.image_url,
                "title": args.title,
                "text": args.text,
                "surface_id": args.surface_id,
                "payload": args.payload,
            });
            push_checked(ctx, host.as_ref(), &args.widget_id, output_kind, frame)
        });
    }

    // 2b. push_alert_preset: renders a stock stream alert (escaped text only) and pushes it.
    {
        let host = Arc::clone(&host);
        reg.register("push_alert_preset", move |ctx: &ToolContext, val: Value| {
            let args: AlertPresetArgs = serde_json::from_value(val)
                .map_err(|e| ToolError::InvalidArgs(format!("malformed args: {e}")))?;
            let alert = match args.alert.as_str() {
                "follow" => stream_alerts::AlertEvent::new_follow(),
                "subscription" => {
                    let tier = args.tier.unwrap_or_else(|| "Tier 1".to_string());
                    if tier.len() > 32 || args.months.unwrap_or(1) > 1200 {
                        return Err(ToolError::InvalidArgs("tier/months out of range".into()));
                    }
                    stream_alerts::AlertEvent::new_sub(&tier, args.months.unwrap_or(1))
                }
                other => {
                    return Err(ToolError::InvalidArgs(format!(
                        "unsupported alert '{other}'"
                    )));
                }
            };
            let frame = json!({ "html": alert.to_sanitized_html() });
            push_checked(
                ctx,
                host.as_ref(),
                &args.widget_id,
                WidgetOutputKind::SanitizedHtml,
                frame,
            )
        });
    }

    // 2c. push_stage_strip: renders the galactic avatar strip (escaped names) and pushes it.
    {
        let host = Arc::clone(&host);
        reg.register("push_stage_strip", move |ctx: &ToolContext, val: Value| {
            let args: StageStripArgs = serde_json::from_value(val)
                .map_err(|e| ToolError::InvalidArgs(format!("malformed args: {e}")))?;
            if args.avatars.len() > 50 || args.avatars.iter().any(|a| a.name.len() > 64) {
                return Err(ToolError::InvalidArgs(
                    "too many avatars or name too long".into(),
                ));
            }
            let frame = json!({ "html": galactic_avatars::render_stage_strip(&args.avatars) });
            push_checked(
                ctx,
                host.as_ref(),
                &args.widget_id,
                WidgetOutputKind::SanitizedHtml,
                frame,
            )
        });
    }

    // 3. add_component (admin-only)
    {
        let host = Arc::clone(&host);
        reg.register("add_component", move |ctx: &ToolContext, val: Value| {
            if !ctx.has_scope(ADMIN_SCOPE) {
                return Err(ToolError::Forbidden);
            }

            let args: AddComponentArgs = serde_json::from_value(val)
                .map_err(|e| ToolError::InvalidArgs(format!("malformed args: {e}")))?;

            let z_index = args.z_index.unwrap_or(10);
            validate_coordinates(args.x, args.y, args.width, args.height, z_index)
                .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

            let widget = host
                .get_widget(&args.widget_id)
                .map_err(store_err)?
                .ok_or_else(|| {
                    ToolError::InvalidArgs(format!("widget '{}' is not registered", args.widget_id))
                })?;

            let props = args.props.unwrap_or_else(|| json!({}));
            let props_str = props.to_string();
            if props_str.len() > 4096 {
                return Err(ToolError::InvalidArgs(
                    "props payload exceeds 4096 bytes".into(),
                ));
            }

            let seed = rand_seed(args.widget_id.as_bytes())
                ^ (args.x as u64).wrapping_shl(16)
                ^ (args.y as u64);
            let instance_id = format!("0195f3a2-8c11-7000-8000-{:012x}", seed);

            let component = SceneComponentDto {
                instance_id,
                bundle_app_id: widget.bundle_app_id,
                widget_id: widget.widget_id,
                surface_id: widget.surface_id.unwrap_or_else(|| "custom".to_string()),
                output_kind: widget.output_kind,
                display_name: widget.display_name,
                x: args.x,
                y: args.y,
                width: args.width,
                height: args.height,
                z_index,
                status: ComponentStatusCode::Active,
                props,
            };

            host.add_component(&component).map_err(|e| match e {
                HostStoreError::CapacityReached(m) => ToolError::InvalidArgs(m),
                other => store_err(other),
            })?;

            Ok(json!(component))
        });
    }

    // 4. update_component (admin-only)
    {
        let host = Arc::clone(&host);
        reg.register("update_component", move |ctx: &ToolContext, val: Value| {
            if !ctx.has_scope(ADMIN_SCOPE) {
                return Err(ToolError::Forbidden);
            }

            let args: UpdateComponentArgs = serde_json::from_value(val)
                .map_err(|e| ToolError::InvalidArgs(format!("malformed args: {e}")))?;

            let updated = host
                .update_component(
                    &args.instance_id,
                    Box::new(move |c| {
                        if let Some(x) = args.x {
                            c.x = x;
                        }
                        if let Some(y) = args.y {
                            c.y = y;
                        }
                        if let Some(w) = args.width {
                            c.width = w;
                        }
                        if let Some(h) = args.height {
                            c.height = h;
                        }
                        if let Some(z) = args.z_index {
                            c.z_index = z;
                        }
                        if let Some(vis) = args.visible {
                            c.status = if vis {
                                ComponentStatusCode::Active
                            } else {
                                ComponentStatusCode::Hidden
                            };
                        }
                        if let Some(p) = args.props {
                            c.props = p;
                        }
                    }),
                )
                .map_err(store_err)?;

            Ok(json!({
                "instance_id": args.instance_id,
                "updated": true,
                "component": updated,
            }))
        });
    }

    // 5. remove_component (admin-only)
    {
        let host = Arc::clone(&host);
        reg.register("remove_component", move |ctx: &ToolContext, val: Value| {
            if !ctx.has_scope(ADMIN_SCOPE) {
                return Err(ToolError::Forbidden);
            }

            let args: RemoveComponentArgs = serde_json::from_value(val)
                .map_err(|e| ToolError::InvalidArgs(format!("malformed args: {e}")))?;

            let removed = host
                .remove_component(&args.instance_id)
                .map_err(store_err)?;

            Ok(json!({
                "instance_id": args.instance_id,
                "removed": removed,
            }))
        });
    }

    // 6. list_components
    {
        let host = Arc::clone(&host);
        reg.register("list_components", move |_ctx: &ToolContext, val: Value| {
            let args: ListComponentsArgs = serde_json::from_value(val).unwrap_or_default();

            let layout = host.get_layout().map_err(store_err)?;
            let include_hidden = args.include_hidden.unwrap_or(true);

            let filtered_components = if include_hidden {
                layout.components
            } else {
                layout
                    .components
                    .into_iter()
                    .filter(|c| c.status == ComponentStatusCode::Active)
                    .collect()
            };

            Ok(json!({
                "canvas": layout.canvas,
                "components": filtered_components,
            }))
        });
    }

    // 7. list_registered_widgets
    {
        let host = Arc::clone(&host);
        reg.register(
            "list_registered_widgets",
            move |_ctx: &ToolContext, _val: Value| {
                let widgets = host.list_widgets().map_err(store_err)?;
                Ok(json!({ "widgets": widgets }))
            },
        );
    }

    // 8. clear_layout (admin-only)
    {
        let host = Arc::clone(&host);
        reg.register("clear_layout", move |ctx: &ToolContext, _val: Value| {
            if !ctx.has_scope(ADMIN_SCOPE) {
                return Err(ToolError::Forbidden);
            }

            let count = host.clear_components().map_err(store_err)?;
            Ok(json!({ "removed_count": count }))
        });
    }

    // 9. set_canvas_theme (admin-only)
    {
        let host = Arc::clone(&host);
        reg.register("set_canvas_theme", move |ctx: &ToolContext, val: Value| {
            if !ctx.has_scope(ADMIN_SCOPE) {
                return Err(ToolError::Forbidden);
            }

            let args: SetCanvasThemeArgs = serde_json::from_value(val)
                .map_err(|e| ToolError::InvalidArgs(format!("malformed args: {e}")))?;

            let mut current = host.get_layout().map_err(store_err)?.canvas;

            if let Some(t) = args.theme {
                current.theme = t;
            }
            if let Some(r) = args.resolution {
                if let Some(res) = CanvasResolution::from_str_name(&r) {
                    current.resolution = res.as_str().to_string();
                    let (w, h) = res.dimensions();
                    current.width = w;
                    current.height = h;
                } else {
                    return Err(ToolError::InvalidArgs(format!(
                        "unsupported resolution '{}'; must be '1080p', '2k', or '4k'",
                        r
                    )));
                }
            }
            if let Some(p) = args.primary_color {
                current.primary_color = p;
            }
            if let Some(s) = args.secondary_color {
                current.secondary_color = s;
            }
            if let Some(f) = args.font_family {
                current.font_family = f;
            }

            host.save_theme(&current).map_err(store_err)?;

            Ok(json!({ "canvas": current }))
        });
    }

    reg
}

fn rand_seed(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in bytes {
        h ^= *b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h & 0xffffffffffff
}

#[cfg(test)]
#[path = "logic_tests.rs"]
mod tests;
