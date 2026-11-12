//! General Overlay SERVICE bundle (WASM entrypoint).
//!
//! An always-on `kind: service` bundle on `world stage-tools` exporting `tool-provider`.
//! Manages full-screen composite overlay layouts, placed components, themes (1080p, 2K, 4K),
//! and presentation bundle widget registrations.

pub mod logic;

use std::sync::{Arc, OnceLock};

use logic::{ConfigStore, HostStoreError, OverlayHostStore};
use overlay_schema::{
    CanvasThemeDto, SceneComponentDto, SceneLayoutDto, WidgetDescriptorDto, WidgetOutputKind,
};
use waddle_sdk::tool::ToolRegistry;

/// In-memory host implementation for service execution within host process.
pub struct MemoryHostStore {
    inner: std::sync::Mutex<MemoryState>,
}

struct MemoryState {
    widgets: Vec<WidgetDescriptorDto>,
    layout: SceneLayoutDto,
    pushes: Vec<(String, WidgetOutputKind, String)>,
}

impl Default for MemoryHostStore {
    fn default() -> Self {
        Self::new()
    }
}

impl MemoryHostStore {
    pub fn new() -> Self {
        Self {
            inner: std::sync::Mutex::new(MemoryState {
                widgets: Vec::new(),
                layout: SceneLayoutDto::default(),
                pushes: Vec::new(),
            }),
        }
    }
}

impl OverlayHostStore for MemoryHostStore {
    fn register_widget(&self, descriptor: &WidgetDescriptorDto) -> Result<(), HostStoreError> {
        let mut state = self.inner.lock().unwrap();
        state.widgets.retain(|w| w.widget_id != descriptor.widget_id);
        state.widgets.push(descriptor.clone());
        Ok(())
    }

    fn get_widget(&self, widget_id: &str) -> Result<Option<WidgetDescriptorDto>, HostStoreError> {
        let state = self.inner.lock().unwrap();
        Ok(state.widgets.iter().find(|w| w.widget_id == widget_id).cloned())
    }

    fn list_widgets(&self) -> Result<Vec<WidgetDescriptorDto>, HostStoreError> {
        Ok(self.inner.lock().unwrap().widgets.clone())
    }

    fn push_widget(
        &self,
        widget_id: &str,
        output_kind: WidgetOutputKind,
        frame_json: &str,
    ) -> Result<(), HostStoreError> {
        self.inner
            .lock()
            .unwrap()
            .pushes
            .push((widget_id.to_string(), output_kind, frame_json.to_string()));
        Ok(())
    }

    fn get_layout(&self) -> Result<SceneLayoutDto, HostStoreError> {
        Ok(self.inner.lock().unwrap().layout.clone())
    }

    fn save_theme(&self, theme: &CanvasThemeDto) -> Result<(), HostStoreError> {
        self.inner.lock().unwrap().layout.canvas = theme.clone();
        Ok(())
    }

    fn add_component(&self, component: &SceneComponentDto) -> Result<(), HostStoreError> {
        let mut state = self.inner.lock().unwrap();
        if state.layout.components.len() >= overlay_schema::MAX_SCENE_COMPONENTS {
            return Err(HostStoreError::CapacityReached(
                "scene component capacity reached".into(),
            ));
        }
        state.layout.components.push(component.clone());
        Ok(())
    }

    fn update_component(
        &self,
        instance_id: &str,
        updater: Box<dyn FnOnce(&mut SceneComponentDto) + Send>,
    ) -> Result<SceneComponentDto, HostStoreError> {
        let mut state = self.inner.lock().unwrap();
        let comp = state
            .layout
            .components
            .iter_mut()
            .find(|c| c.instance_id == instance_id)
            .ok_or_else(|| HostStoreError::NotFound(instance_id.to_string()))?;
        updater(comp);
        Ok(comp.clone())
    }

    fn remove_component(&self, instance_id: &str) -> Result<bool, HostStoreError> {
        let mut state = self.inner.lock().unwrap();
        let len_before = state.layout.components.len();
        state.layout.components.retain(|c| c.instance_id != instance_id);
        Ok(state.layout.components.len() < len_before)
    }

    fn clear_components(&self) -> Result<u32, HostStoreError> {
        let mut state = self.inner.lock().unwrap();
        let count = state.layout.components.len() as u32;
        state.layout.components.clear();
        Ok(count)
    }
}

pub struct MemoryKv;
impl ConfigStore for MemoryKv {
    fn get(&self, _key: &str) -> Result<Option<Vec<u8>>, String> {
        Ok(None)
    }
    fn set(&self, _key: &str, _value: &[u8]) -> Result<(), String> {
        Ok(())
    }
}

static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

pub fn default_registry() -> &'static ToolRegistry {
    REGISTRY.get_or_init(|| {
        logic::build_registry(Arc::new(MemoryHostStore::new()), Arc::new(MemoryKv))
    })
}
