//! kv-backed [`OverlayHostStore`]: all overlay state (widget registry, scene layout) lives in
//! the bundle's host `kv`, which the host scopes to the invocation's verified (tenant,
//! community, app).
//!
//! There is deliberately NO process-wide state here: a guest instance may be reused across
//! communities, so anything held in guest memory would leak between tenants. Every operation
//! reads and writes the kv document; a kv failure is `HostStoreError::Backend` (fail closed),
//! and when the host has no community context the kv import itself errors.
//!
//! Keys use `.` separators (never `:`). Documents are read-modify-write; the kv has no
//! compare-and-swap, so two concurrent writers in one community can lose an update.

use std::sync::Arc;

use overlay_schema::{
    CanvasThemeDto, MAX_SCENE_COMPONENTS, SceneComponentDto, SceneLayoutDto, WidgetDescriptorDto,
    WidgetOutputKind,
};

use crate::logic::{ConfigStore, HostStoreError, OverlayHostStore, PushSink};

const WIDGETS_KEY: &str = "overlay.widgets";
const LAYOUT_KEY: &str = "overlay.layout";

/// Overlay state persisted in community-scoped kv, plus the live-push sink.
pub struct KvHostStore {
    kv: Arc<dyn ConfigStore>,
    sink: Arc<dyn PushSink>,
}

impl KvHostStore {
    /// Builds a store over the host kv and push transport.
    pub fn new(kv: Arc<dyn ConfigStore>, sink: Arc<dyn PushSink>) -> Self {
        Self { kv, sink }
    }

    /// Reads and decodes a JSON document; absent -> `None`, any kv/decode failure -> Backend.
    fn load<T: serde::de::DeserializeOwned>(&self, key: &str) -> Result<Option<T>, HostStoreError> {
        match self.kv.get(key) {
            Ok(None) => Ok(None),
            Ok(Some(bytes)) => serde_json::from_slice(&bytes)
                .map(Some)
                .map_err(|_| HostStoreError::Backend("corrupt overlay state".into())),
            Err(_) => Err(HostStoreError::Backend("kv unavailable".into())),
        }
    }

    /// Encodes and writes a JSON document.
    fn save<T: serde::Serialize>(&self, key: &str, value: &T) -> Result<(), HostStoreError> {
        let bytes = serde_json::to_vec(value)
            .map_err(|_| HostStoreError::Backend("encode overlay state".into()))?;
        self.kv
            .set(key, &bytes)
            .map_err(|_| HostStoreError::Backend("kv unavailable".into()))
    }

    fn widgets(&self) -> Result<Vec<WidgetDescriptorDto>, HostStoreError> {
        Ok(self.load(WIDGETS_KEY)?.unwrap_or_default())
    }

    fn layout(&self) -> Result<SceneLayoutDto, HostStoreError> {
        Ok(self.load(LAYOUT_KEY)?.unwrap_or_default())
    }
}

impl OverlayHostStore for KvHostStore {
    fn register_widget(&self, descriptor: &WidgetDescriptorDto) -> Result<(), HostStoreError> {
        let mut widgets = self.widgets()?;
        widgets.retain(|w| w.widget_id != descriptor.widget_id);
        widgets.push(descriptor.clone());
        self.save(WIDGETS_KEY, &widgets)
    }

    fn get_widget(&self, widget_id: &str) -> Result<Option<WidgetDescriptorDto>, HostStoreError> {
        Ok(self
            .widgets()?
            .into_iter()
            .find(|w| w.widget_id == widget_id))
    }

    fn list_widgets(&self) -> Result<Vec<WidgetDescriptorDto>, HostStoreError> {
        self.widgets()
    }

    fn push_widget(
        &self,
        widget_id: &str,
        surface: &str,
        output_kind: WidgetOutputKind,
        frame_json: &str,
    ) -> Result<(), HostStoreError> {
        self.sink.push(widget_id, surface, output_kind, frame_json)
    }

    fn get_layout(&self) -> Result<SceneLayoutDto, HostStoreError> {
        self.layout()
    }

    fn save_theme(&self, theme: &CanvasThemeDto) -> Result<(), HostStoreError> {
        let mut layout = self.layout()?;
        layout.canvas = theme.clone();
        self.save(LAYOUT_KEY, &layout)
    }

    fn add_component(&self, component: &SceneComponentDto) -> Result<(), HostStoreError> {
        let mut layout = self.layout()?;
        if layout.components.len() >= MAX_SCENE_COMPONENTS {
            return Err(HostStoreError::CapacityReached(
                "scene component capacity reached".into(),
            ));
        }
        layout.components.push(component.clone());
        self.save(LAYOUT_KEY, &layout)
    }

    fn update_component(
        &self,
        instance_id: &str,
        updater: Box<dyn FnOnce(&mut SceneComponentDto) + Send>,
    ) -> Result<SceneComponentDto, HostStoreError> {
        let mut layout = self.layout()?;
        let comp = layout
            .components
            .iter_mut()
            .find(|c| c.instance_id == instance_id)
            .ok_or_else(|| HostStoreError::NotFound(instance_id.to_string()))?;
        updater(comp);
        let updated = comp.clone();
        self.save(LAYOUT_KEY, &layout)?;
        Ok(updated)
    }

    fn remove_component(&self, instance_id: &str) -> Result<bool, HostStoreError> {
        let mut layout = self.layout()?;
        let before = layout.components.len();
        layout.components.retain(|c| c.instance_id != instance_id);
        let removed = layout.components.len() < before;
        if removed {
            self.save(LAYOUT_KEY, &layout)?;
        }
        Ok(removed)
    }

    fn clear_components(&self) -> Result<u32, HostStoreError> {
        let mut layout = self.layout()?;
        let count = layout.components.len() as u32;
        layout.components.clear();
        self.save(LAYOUT_KEY, &layout)?;
        Ok(count)
    }
}
