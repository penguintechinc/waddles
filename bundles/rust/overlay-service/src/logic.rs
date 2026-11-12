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
    validate_coordinates, CanvasResolution, CanvasThemeDto, ComponentStatusCode, SceneComponentDto,
    SceneLayoutDto, WidgetDescriptorDto, WidgetOutputKind, MAX_SCENE_COMPONENTS,
    MAX_WIDGET_HTML_BYTES,
};
use serde::Deserialize;
use serde_json::{json, Value};
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

/// Key-value storage seam.
pub trait ConfigStore: Send + Sync {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    fn set(&self, key: &str, value: &[u8]) -> Result<(), String>;
}

// ---------------------------------------------------------------------------
// Tool Request Payloads
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
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
    #[serde(default)]
    pub bundle_app_id: Option<String>,
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

pub fn build_registry(
    host: Arc<dyn OverlayHostStore>,
    _kv: Arc<dyn ConfigStore>,
) -> ToolRegistry {
    let mut reg = ToolRegistry::new();

    // 1. register_widget
    {
        let host = Arc::clone(&host);
        reg.register("register_widget", move |ctx: &ToolContext, val: Value| {
            let args: RegisterWidgetArgs = serde_json::from_value(val)
                .map_err(|e| ToolError::InvalidArgs(format!("malformed args: {e}")))?;

            if args.widget_id.trim().is_empty() || args.widget_id.len() > 128 {
                return Err(ToolError::InvalidArgs("widget_id must be 1-128 chars".into()));
            }
            if args.display_name.trim().is_empty() || args.display_name.len() > 120 {
                return Err(ToolError::InvalidArgs("display_name must be 1-120 chars".into()));
            }

            let output_kind = WidgetOutputKind::from_str_name(&args.output_kind).ok_or_else(
                || ToolError::InvalidArgs(format!("unsupported output_kind '{}'", args.output_kind)),
            )?;

            let def_x = args.default_x.unwrap_or(48);
            let def_y = args.default_y.unwrap_or(48);
            let def_w = args.default_width.unwrap_or(400);
            let def_h = args.default_height.unwrap_or(200);
            let def_z = args.default_z_index.unwrap_or(10);

            validate_coordinates(def_x, def_y, def_w, def_h, def_z)
                .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

            let bundle_app_id = args
                .bundle_app_id
                .or_else(|| ctx.actor.clone())
                .unwrap_or_else(|| "system".to_string());

            let descriptor = WidgetDescriptorDto {
                widget_id: args.widget_id.clone(),
                bundle_app_id,
                display_name: args.display_name,
                description: args.description.unwrap_or_default(),
                category: args.category.unwrap_or_else(|| "custom".to_string()),
                output_kind,
                surface_id: args.surface_id,
                allow_standalone_output: args.allow_standalone_output.unwrap_or(true),
                default_x: def_x,
                default_y: def_y,
                default_width: def_w,
                default_height: def_h,
                default_z_index: def_z,
                props_schema: args.props_schema.unwrap_or_else(|| json!({})),
            };

            host.register_widget(&descriptor)
                .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

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

            let output_kind = WidgetOutputKind::from_str_name(&args.output_kind).ok_or_else(
                || ToolError::InvalidArgs(format!("unsupported output_kind '{}'", args.output_kind)),
            )?;

            if output_kind == WidgetOutputKind::SanitizedHtml {
                if let Some(ref h) = args.html {
                    if h.len() > MAX_WIDGET_HTML_BYTES {
                        return Err(ToolError::InvalidArgs(format!(
                            "html payload exceeds {} bytes",
                            MAX_WIDGET_HTML_BYTES
                        )));
                    }
                }
            }

            if let Ok(Some(w)) = host.get_widget(&args.widget_id) {
                if let Some(ref actor) = ctx.actor {
                    if actor != "system" && !ctx.has_scope(ADMIN_SCOPE) && actor != &w.bundle_app_id {
                        return Err(ToolError::Forbidden);
                    }
                }
            }

            let frame_json = serde_json::to_string(&json!({
                "widget_id": args.widget_id,
                "output_kind": output_kind.as_str(),
                "html": args.html,
                "image_url": args.image_url,
                "title": args.title,
                "text": args.text,
                "surface_id": args.surface_id,
                "payload": args.payload,
            }))
            .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

            host.push_widget(&args.widget_id, output_kind, &frame_json)
                .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

            Ok(json!({ "status": "ok", "widget_id": args.widget_id }))
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
                .map_err(|e| ToolError::InvalidArgs(e.to_string()))?
                .ok_or_else(|| {
                    ToolError::InvalidArgs(format!("widget '{}' is not registered", args.widget_id))
                })?;

            let props = args.props.unwrap_or_else(|| json!({}));
            let props_str = props.to_string();
            if props_str.len() > 4096 {
                return Err(ToolError::InvalidArgs("props payload exceeds 4096 bytes".into()));
            }

            let seed = rand_seed(args.widget_id.as_bytes()) ^ (args.x as u64).wrapping_shl(16) ^ (args.y as u64);
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
                other => ToolError::InvalidArgs(other.to_string()),
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
                .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

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
                .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

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

            let layout = host.get_layout().map_err(|e| ToolError::InvalidArgs(e.to_string()))?;
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
        reg.register("list_registered_widgets", move |_ctx: &ToolContext, _val: Value| {
            let widgets = host.list_widgets().map_err(|e| ToolError::InvalidArgs(e.to_string()))?;
            Ok(json!({ "widgets": widgets }))
        });
    }

    // 8. clear_layout (admin-only)
    {
        let host = Arc::clone(&host);
        reg.register("clear_layout", move |ctx: &ToolContext, _val: Value| {
            if !ctx.has_scope(ADMIN_SCOPE) {
                return Err(ToolError::Forbidden);
            }

            let count = host.clear_components().map_err(|e| ToolError::InvalidArgs(e.to_string()))?;
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

            let mut current = host.get_layout().map_err(|e| ToolError::InvalidArgs(e.to_string()))?.canvas;

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

            host.save_theme(&current).map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

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
mod tests {
    use super::*;
    use std::sync::Mutex;

    struct FakeHost {
        widgets: Mutex<Vec<WidgetDescriptorDto>>,
        layout: Mutex<SceneLayoutDto>,
        pushes: Mutex<Vec<(String, WidgetOutputKind, String)>>,
    }

    impl FakeHost {
        fn new() -> Self {
            Self {
                widgets: Mutex::new(Vec::new()),
                layout: Mutex::new(SceneLayoutDto::default()),
                pushes: Mutex::new(Vec::new()),
            }
        }
    }

    impl OverlayHostStore for FakeHost {
        fn register_widget(&self, descriptor: &WidgetDescriptorDto) -> Result<(), HostStoreError> {
            let mut list = self.widgets.lock().unwrap();
            list.retain(|w| w.widget_id != descriptor.widget_id);
            list.push(descriptor.clone());
            Ok(())
        }

        fn get_widget(&self, widget_id: &str) -> Result<Option<WidgetDescriptorDto>, HostStoreError> {
            let list = self.widgets.lock().unwrap();
            Ok(list.iter().find(|w| w.widget_id == widget_id).cloned())
        }

        fn list_widgets(&self) -> Result<Vec<WidgetDescriptorDto>, HostStoreError> {
            Ok(self.widgets.lock().unwrap().clone())
        }

        fn push_widget(
            &self,
            widget_id: &str,
            output_kind: WidgetOutputKind,
            frame_json: &str,
        ) -> Result<(), HostStoreError> {
            self.pushes
                .lock()
                .unwrap()
                .push((widget_id.to_string(), output_kind, frame_json.to_string()));
            Ok(())
        }

        fn get_layout(&self) -> Result<SceneLayoutDto, HostStoreError> {
            Ok(self.layout.lock().unwrap().clone())
        }

        fn save_theme(&self, theme: &CanvasThemeDto) -> Result<(), HostStoreError> {
            self.layout.lock().unwrap().canvas = theme.clone();
            Ok(())
        }

        fn add_component(&self, component: &SceneComponentDto) -> Result<(), HostStoreError> {
            let mut l = self.layout.lock().unwrap();
            if l.components.len() >= MAX_SCENE_COMPONENTS {
                return Err(HostStoreError::CapacityReached("max components reached".into()));
            }
            l.components.push(component.clone());
            Ok(())
        }

        fn update_component(
            &self,
            instance_id: &str,
            updater: Box<dyn FnOnce(&mut SceneComponentDto) + Send>,
        ) -> Result<SceneComponentDto, HostStoreError> {
            let mut l = self.layout.lock().unwrap();
            let c = l
                .components
                .iter_mut()
                .find(|c| c.instance_id == instance_id)
                .ok_or_else(|| HostStoreError::NotFound(instance_id.to_string()))?;
            updater(c);
            Ok(c.clone())
        }

        fn remove_component(&self, instance_id: &str) -> Result<bool, HostStoreError> {
            let mut l = self.layout.lock().unwrap();
            let len_before = l.components.len();
            l.components.retain(|c| c.instance_id != instance_id);
            Ok(l.components.len() < len_before)
        }

        fn clear_components(&self) -> Result<u32, HostStoreError> {
            let mut l = self.layout.lock().unwrap();
            let count = l.components.len() as u32;
            l.components.clear();
            Ok(count)
        }
    }

    struct FakeKv;
    impl ConfigStore for FakeKv {
        fn get(&self, _key: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(None)
        }
        fn set(&self, _key: &str, _value: &[u8]) -> Result<(), String> {
            Ok(())
        }
    }

    fn call_tool(
        reg: &ToolRegistry,
        name: &str,
        mut args: Value,
        admin: bool,
    ) -> Result<Vec<u8>, ToolError> {
        if !args.is_object() {
            args = json!({});
        }
        if let Some(obj) = args.as_object_mut() {
            obj.insert(
                waddle_sdk::tool::CTX_ARG.to_string(),
                json!({
                    "actor": "0195f3a2-8c11-7000-8000-000000000001",
                    "scopes": if admin { vec![ADMIN_SCOPE] } else { vec![] }
                }),
            );
        }
        let bytes = serde_json::to_vec(&args).unwrap();
        reg.call(name, &bytes)
    }

    #[test]
    fn register_and_list_widgets() {
        let host = Arc::new(FakeHost::new());
        let reg = build_registry(host, Arc::new(FakeKv));

        let args = json!({
            "widget_id": "dj.music.album_now_playing",
            "display_name": "Now Playing (Album Cover)",
            "output_kind": "surface",
            "surface_id": "music.now_playing",
            "default_x": 48,
            "default_y": 860,
            "default_width": 460,
            "default_height": 160,
        });

        let res = call_tool(&reg, "register_widget", args, false).unwrap();
        let parsed: Value = serde_json::from_slice(&res).unwrap();
        assert_eq!(parsed["registered"], true);
        assert_eq!(parsed["output_kind"], "surface");

        let list_res = call_tool(&reg, "list_registered_widgets", json!({}), false).unwrap();
        let list_json: Value = serde_json::from_slice(&list_res).unwrap();
        assert_eq!(list_json["widgets"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn push_widget_validates_size_and_records_frame() {
        let host = Arc::new(FakeHost::new());
        let reg = build_registry(Arc::clone(&host) as Arc<dyn OverlayHostStore>, Arc::new(FakeKv));

        let push_args = json!({
            "widget_id": "custom.avatars",
            "output_kind": "sanitized_html",
            "html": "<div class=\"avatar\">Hero</div>",
        });

        let res = call_tool(&reg, "push_widget", push_args, false).unwrap();
        let parsed: Value = serde_json::from_slice(&res).unwrap();
        assert_eq!(parsed["status"], "ok");

        assert_eq!(host.pushes.lock().unwrap().len(), 1);
    }

    #[test]
    fn add_component_requires_admin_scope() {
        let host = Arc::new(FakeHost::new());
        let reg = build_registry(Arc::clone(&host) as Arc<dyn OverlayHostStore>, Arc::new(FakeKv));

        // First register the widget
        let reg_args = json!({
            "widget_id": "dj.music.album_now_playing",
            "display_name": "Now Playing",
            "output_kind": "surface",
        });
        call_tool(&reg, "register_widget", reg_args, false).unwrap();

        let add_args = json!({
            "widget_id": "dj.music.album_now_playing",
            "x": 48,
            "y": 860,
            "width": 460,
            "height": 160,
        });

        // Non-admin call fails Forbidden
        let err = call_tool(&reg, "add_component", add_args.clone(), false).unwrap_err();
        assert_eq!(err, ToolError::Forbidden);

        // Admin call succeeds
        let ok_res = call_tool(&reg, "add_component", add_args, true).unwrap();
        let comp: Value = serde_json::from_slice(&ok_res).unwrap();
        assert_eq!(comp["widget_id"], "dj.music.album_now_playing");
        assert_eq!(comp["x"], 48);
        assert_eq!(comp["status"], "active");
    }

    #[test]
    fn component_lifecycle_update_and_remove() {
        let host = Arc::new(FakeHost::new());
        let reg = build_registry(Arc::clone(&host) as Arc<dyn OverlayHostStore>, Arc::new(FakeKv));

        // Register widget
        let reg_args = json!({
            "widget_id": "alerts.stream.event_box",
            "display_name": "Alert Box",
            "output_kind": "surface",
        });
        call_tool(&reg, "register_widget", reg_args, false).unwrap();

        // Add
        let add_res = call_tool(
            &reg,
            "add_component",
            json!({
                "widget_id": "alerts.stream.event_box",
                "x": 660,
                "y": 120,
                "width": 600,
                "height": 220,
            }),
            true,
        )
        .unwrap();
        let comp: Value = serde_json::from_slice(&add_res).unwrap();
        let instance_id = comp["instance_id"].as_str().unwrap();

        // Update
        let update_res = call_tool(
            &reg,
            "update_component",
            json!({
                "instance_id": instance_id,
                "x": 700,
                "visible": false,
            }),
            true,
        )
        .unwrap();
        let updated: Value = serde_json::from_slice(&update_res).unwrap();
        assert_eq!(updated["updated"], true);
        assert_eq!(updated["component"]["x"], 700);
        assert_eq!(updated["component"]["status"], "hidden");

        // List components (include_hidden: false should filter it)
        let list_visible = call_tool(
            &reg,
            "list_components",
            json!({"include_hidden": false}),
            true,
        )
        .unwrap();
        let list_vis_json: Value = serde_json::from_slice(&list_visible).unwrap();
        assert_eq!(list_vis_json["components"].as_array().unwrap().len(), 0);

        // Remove
        let remove_res = call_tool(
            &reg,
            "remove_component",
            json!({ "instance_id": instance_id }),
            true,
        )
        .unwrap();
        let removed: Value = serde_json::from_slice(&remove_res).unwrap();
        assert_eq!(removed["removed"], true);

        // List should be empty
        let list_all = call_tool(&reg, "list_components", json!({}), true).unwrap();
        let list_all_json: Value = serde_json::from_slice(&list_all).unwrap();
        assert_eq!(list_all_json["components"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn set_canvas_theme_resolution_configuration() {
        let host = Arc::new(FakeHost::new());
        let reg = build_registry(Arc::clone(&host) as Arc<dyn OverlayHostStore>, Arc::new(FakeKv));

        // Configure 2k resolution
        let res_2k = call_tool(
            &reg,
            "set_canvas_theme",
            json!({
                "resolution": "2k",
                "theme": "cyberpunk"
            }),
            true,
        )
        .unwrap();
        let val_2k: Value = serde_json::from_slice(&res_2k).unwrap();
        assert_eq!(val_2k["canvas"]["resolution"], "2k");
        assert_eq!(val_2k["canvas"]["width"], 2560);
        assert_eq!(val_2k["canvas"]["height"], 1440);

        // Configure 4k resolution
        let res_4k = call_tool(
            &reg,
            "set_canvas_theme",
            json!({
                "resolution": "4k"
            }),
            true,
        )
        .unwrap();
        let val_4k: Value = serde_json::from_slice(&res_4k).unwrap();
        assert_eq!(val_4k["canvas"]["resolution"], "4k");
        assert_eq!(val_4k["canvas"]["width"], 3840);
        assert_eq!(val_4k["canvas"]["height"], 2160);

        // Invalid resolution rejected
        let res_bad = call_tool(
            &reg,
            "set_canvas_theme",
            json!({
                "resolution": "8k"
            }),
            true,
        )
        .unwrap_err();
        assert!(matches!(res_bad, ToolError::InvalidArgs(_)));
    }
}
