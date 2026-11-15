//! General Overlay SERVICE bundle (WASM entrypoint).
//!
//! An always-on `kind: service` bundle on `world stage-tools` exporting `tool-provider`.
//! Manages full-screen composite overlay layouts, placed components, themes (1080p, 2K, 4K),
//! and presentation bundle widget registrations.
//!
//! All state lives in the host `kv` (scoped by the host to the verified tenant/community/app);
//! the component keeps NO process-wide registry or store, so nothing can leak between
//! communities sharing a guest instance. Identity comes only from the host-verified
//! `ToolContext`, never from tool arguments.

#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;
pub mod logic;
pub mod store;

#[cfg(target_arch = "wasm32")]
mod component {
    use std::sync::Arc;

    use super::bindings::exports::waddle::bundle::tool_provider::Guest;
    use super::bindings::waddle::bundle::tool::ToolError as WitError;
    use super::bindings::waddle::bundle::{kv, log, overlay};
    use super::logic::{self, ConfigStore, HostStoreError, PushSink};
    use super::store::KvHostStore;
    use overlay_schema::WidgetOutputKind;
    use serde_json::{json, Value};
    use waddle_sdk::tool::{ToolError, ToolRegistry};

    pub struct Component;

    /// The bundle's own kv (host-scoped to this community + app); errors never carry detail.
    struct WasmKv;

    impl ConfigStore for WasmKv {
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            kv::get(key).map_err(|_| "kv unavailable".to_string())
        }
        fn set(&self, key: &str, value: &[u8]) -> Result<(), String> {
            kv::set(key, value, 0).map_err(|_| "kv unavailable".to_string())
        }
    }

    /// Live push through the host `overlay` import. Contract `surface` widgets need the
    /// `publish-surface` import, which this world does not link: that is refused loudly
    /// rather than dropped.
    struct WasmPush;

    impl PushSink for WasmPush {
        fn push(
            &self,
            widget_id: &str,
            surface: &str,
            output_kind: WidgetOutputKind,
            frame_json: &str,
        ) -> Result<(), HostStoreError> {
            let frame: Value = serde_json::from_str(frame_json)
                .map_err(|_| HostStoreError::InvalidArgument("malformed frame".into()))?;
            let mut body = serde_json::Map::new();
            body.insert("widget_id".into(), json!(widget_id));
            match output_kind {
                WidgetOutputKind::SanitizedHtml => {
                    body.insert("sanitized_html".into(), frame["html"].clone());
                }
                WidgetOutputKind::Text => {
                    body.insert("text".into(), frame["text"].clone());
                    body.insert("title".into(), frame["title"].clone());
                }
                WidgetOutputKind::Image => {
                    body.insert("image_url".into(), frame["image_url"].clone());
                    body.insert("title".into(), frame["title"].clone());
                }
                WidgetOutputKind::Surface => {
                    return Err(HostStoreError::Backend(
                        "surface widgets require publish-surface".into(),
                    ));
                }
            }
            body.retain(|_, v| !v.is_null());
            overlay::push(
                widget_id,
                wit_surface(surface)?,
                &Value::Object(body).to_string(),
            )
            .map(|_| ())
            .map_err(|_| HostStoreError::Backend("overlay push failed".into()))
        }
    }

    /// Maps the widget's own surface name onto the WIT enum; unknown names fail closed.
    fn wit_surface(name: &str) -> Result<overlay::Surface, HostStoreError> {
        Ok(match name {
            "full_screen" => overlay::Surface::FullScreen,
            "media" => overlay::Surface::Media,
            "crawler" => overlay::Surface::Crawler,
            "music" => overlay::Surface::Music,
            "alert_box" => overlay::Surface::AlertBox,
            "chat" => overlay::Surface::Chat,
            "goals" => overlay::Surface::Goals,
            "ticker" => overlay::Surface::Ticker,
            "image" => overlay::Surface::Image,
            "caption" => overlay::Surface::Caption,
            _ => return Err(HostStoreError::InvalidArgument("unknown surface".into())),
        })
    }

    /// Maps the SDK tool error onto the WIT one.
    fn to_wit(e: ToolError) -> WitError {
        match e {
            ToolError::NotGranted => WitError::NotGranted,
            ToolError::Forbidden => WitError::Forbidden,
            ToolError::Timeout => WitError::Timeout,
            ToolError::DepthExceeded => WitError::DepthExceeded,
            ToolError::PoolExhausted => WitError::PoolExhausted,
            ToolError::TargetUnavailable => WitError::TargetUnavailable,
            ToolError::InvalidArgs(m) => WitError::InvalidArgs(m),
            ToolError::NoTransport => {
                WitError::InvalidArgs("no tool transport installed".to_string())
            }
        }
    }

    /// Builds a fresh registry per call: stateless, so no cross-community guest state exists.
    fn registry() -> ToolRegistry {
        let host = KvHostStore::new(Arc::new(WasmKv), Arc::new(WasmPush));
        logic::build_registry(Arc::new(host))
    }

    impl Guest for Component {
        /// Routes `name` through the tool table; logs tool name and outcome code only.
        fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
            let result = registry().call(&name, &args);
            let outcome = match &result {
                Ok(_) => "ok",
                Err(ToolError::InvalidArgs(_)) => "invalid_args",
                Err(ToolError::Forbidden) => "forbidden",
                Err(_) => "error",
            };
            let fields = json!({ "tool": name, "outcome": outcome }).to_string();
            log::write(log::Level::Debug, "overlay tool call", &fields);
            result.map_err(to_wit)
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);
