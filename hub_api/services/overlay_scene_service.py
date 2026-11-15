"""Composite overlay scene: widget discovery, placed components, canvas resolution.

The Overlay Designer lays widgets out on one transparent canvas that svc-presentation serves at
`/{overlay_code}/scene`. This module owns the control-plane side of that: which widgets a community
may place, where each placed instance sits, and which canvas resolution the scene renders at.
Callers (`blueprints/v1/overlay.py`) have already run `community_access.require_community_admin()`,
same division of responsibility as `overlay_service.py`.

Invariants worth knowing before changing anything:

* **Tenant scoping is derived, never supplied.** Every query filters on BOTH the community id
  and the tenant id read from the `communities` row, so a widget registered by another tenant
  can never be listed, placed, moved or removed here.
* **A component's origin is resolved, not trusted.** `bundle_app_id`, output kind and surface
  come from the widget the community is actually allowed to use (an enabled bundle's declared
  widget or a registry row), never from the request body.
* **Int codes at rest, labels at the edge.** `output_kind_code` (1 text, 2 image, 3 sanitized_html,
  4 surface), `status_code` (1 visible, 2 hidden) and `canvas_resolution_code` (1 1080p, 2 2k, 3 4k)
  are stored as integers; the API speaks the string labels. Canvas width/height are derived from the
  code, never stored twice.
* **The canvas lives on `presentation_config`**, the row svc-presentation already reads per
  community, so a resolution change reaches the data plane without a hub-api hop.
* Nothing here logs widget props or HTML (bundle-authored content); only ids, counts and ops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.errors import ApiError, bad_request, conflict, not_found

logger = logging.getLogger(__name__)

#: canvas_resolution_code -> (label, width_px, height_px).
CANVASES: dict[int, tuple[str, int, int]] = {
    1: ("1080p", 1920, 1080),
    2: ("2k", 2560, 1440),
    3: ("4k", 3840, 2160),
}
_CANVAS_CODE_BY_LABEL = {label: code for code, (label, _, _) in CANVASES.items()}
DEFAULT_CANVAS_CODE = 1

#: output_kind_code <-> label.
OUTPUT_KINDS: dict[int, str] = {1: "text", 2: "image", 3: "sanitized_html", 4: "surface"}
_OUTPUT_KIND_CODE_BY_LABEL = {label: code for code, label in OUTPUT_KINDS.items()}

STATUS_VISIBLE = 1
STATUS_HIDDEN = 2

#: Bounds shared with the 0113 CHECK constraints.
MIN_SIZE_PX = 32
MAX_X_PX = 3840
MAX_Y_PX = 2160
MIN_Z, MAX_Z = -1000, 1000
MAX_COMPONENTS = 128
MAX_PROPS_BYTES = 8192


@dataclass(slots=True, frozen=True)
class WidgetRecord:
    """One widget the community may place on its scene."""

    widget_id: str
    bundle_app_id: str
    display_name: str
    description: str
    category: str
    output_kind: str
    surface_id: str | None
    allow_standalone_output: bool
    default_x: int
    default_y: int
    default_width: int
    default_height: int
    default_z_index: int
    props_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CanvasRecord:
    """Canvas size the scene renders at."""

    resolution: str
    width: int
    height: int


@dataclass(slots=True, frozen=True)
class ComponentRecord:
    """One placed widget instance; `instance_id` is the row's public UUID."""

    instance_id: str
    bundle_app_id: str
    widget_id: str
    surface_id: str
    output_kind: str
    display_name: str
    x: int
    y: int
    width: int
    height: int
    z_index: int
    visible: bool
    props: dict[str, Any]


@dataclass(slots=True, frozen=True)
class SceneLayout:
    """Canvas plus every placed component, back to front."""

    canvas: CanvasRecord
    components: list[ComponentRecord]


@dataclass(slots=True, frozen=True)
class ComponentInput:
    """Validated placement request; `None` fields fall back to the widget's defaults."""

    widget_id: str
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None
    z_index: int | None = None
    props: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class ComponentPatch:
    """Validated partial update; only non-`None` fields change."""

    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None
    z_index: int | None = None
    visible: bool | None = None
    props: dict[str, Any] | None = None


def canvas_for_code(code: int | None) -> CanvasRecord:
    """Map a stored `canvas_resolution_code` to its label and pixel size (default 1080p)."""
    label, width, height = CANVASES.get(code or DEFAULT_CANVAS_CODE, CANVASES[DEFAULT_CANVAS_CODE])
    return CanvasRecord(resolution=label, width=width, height=height)


def _bind(dal: Any) -> None:
    """Make sure the overlay + lifecycle tables are defined on `dal` (idempotent)."""
    from services.schema import bind_lifecycle_tables, bind_overlay_tables

    bind_overlay_tables(dal)
    bind_lifecycle_tables(dal)


async def _run(async_dal: Any, fn: Callable[[], Any], *, write: bool) -> Any:
    """Run `fn` as ONE executor job; commit on success / roll back on failure when `write`."""

    def _job() -> Any:
        try:
            result = fn()
            if write:
                async_dal.dal.commit()
            return result
        except Exception:
            if write:
                async_dal.dal.rollback()
            raise

    return await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)


def _community_tenant_id(dal: Any, community_id: int) -> int:
    """Return the tenant owning `community_id` or raise 404 (never a caller-supplied tenant)."""
    row = dal(dal.communities.id == community_id).select(dal.communities.tenant_id).first()
    if row is None:
        raise not_found("Community not found")
    return int(row.tenant_id)


def _check_geometry(
    *, x: int, y: int, width: int, height: int, canvas: CanvasRecord, what: str = "component"
) -> None:
    """Reject a rectangle that does not fit the community's canvas (400)."""
    if width < MIN_SIZE_PX or height < MIN_SIZE_PX:
        raise bad_request(f"{what} must be at least {MIN_SIZE_PX}x{MIN_SIZE_PX} px")
    if width > canvas.width or height > canvas.height:
        raise bad_request(f"{what} is larger than the {canvas.resolution} canvas")
    if not (0 <= x <= canvas.width - MIN_SIZE_PX and 0 <= y <= canvas.height - MIN_SIZE_PX):
        raise bad_request(f"{what} position is outside the {canvas.resolution} canvas")


def _check_props(props: dict[str, Any] | None) -> dict[str, Any]:
    """Bound bundle-authored props (they are stored and echoed to the scene page)."""
    value = props or {}
    if len(json.dumps(value, separators=(",", ":"))) > MAX_PROPS_BYTES:
        raise bad_request(f"props exceed {MAX_PROPS_BYTES} bytes")
    return value


def _component_record(row: Any) -> ComponentRecord:
    """Build the API record from an `overlay_scene_components` row."""
    return ComponentRecord(
        instance_id=str(row.public_id),
        bundle_app_id=row.bundle_app_id,
        widget_id=row.widget_id,
        surface_id=row.surface_id,
        output_kind=OUTPUT_KINDS.get(row.output_kind_code, "text"),
        display_name=row.display_name,
        x=row.x_px,
        y=row.y_px,
        width=row.width_px,
        height=row.height_px,
        z_index=row.z_index,
        visible=row.status_code == STATUS_VISIBLE,
        props=row.props or {},
    )


def _canvas_row(dal: Any, community_id: int) -> Any | None:
    """The community's `presentation_config` row, if one exists."""
    return dal(dal.presentation_config.community_id == community_id).select().first()


def _widgets_sync(dal: Any, tenant_id: int, community_id: int) -> dict[str, WidgetRecord]:
    """Union of widgets declared by enabled bundles and rows in the community's registry.

    A registry row (a bundle's explicit runtime registration) overrides a manifest declaration with
    the same `widget_id`. Both sources are filtered to this tenant + community.
    """
    found: dict[str, WidgetRecord] = {}

    query = (
        (dal.app_activations.tenant_id == tenant_id)
        & (dal.app_activations.community_id == community_id)
        & (dal.app_activations.enabled == True)  # noqa: E712 - pydal query operator
        & (dal.app_activations.app_id == dal.app_catalog.app_id)
        & (dal.app_catalog.status == "active")
    )
    for row in dal(query).select(dal.app_activations.app_id, dal.app_catalog.stages):
        stages = row.app_catalog.stages
        presentation = stages.get("presentation") if isinstance(stages, dict) else None
        declared = presentation.get("overlay_widgets") if isinstance(presentation, dict) else None
        for entry in declared or []:
            if isinstance(entry, dict) and isinstance(entry.get("widget_id"), str):
                widget = _widget_from_declaration(row.app_activations.app_id, entry)
                found[widget.widget_id] = widget

    registry = dal.overlay_widget_registry
    for reg in dal(
        (registry.tenant_id == tenant_id) & (registry.community_id == community_id)
    ).select():
        found[reg.widget_id] = WidgetRecord(
            widget_id=reg.widget_id,
            bundle_app_id=reg.bundle_app_id,
            display_name=reg.display_name,
            description=reg.description or "",
            category=reg.category or "custom",
            output_kind=OUTPUT_KINDS.get(reg.output_kind_code, "text"),
            surface_id=reg.surface_id,
            allow_standalone_output=bool(reg.allow_standalone_output),
            default_x=reg.default_x_px,
            default_y=reg.default_y_px,
            default_width=reg.default_width_px,
            default_height=reg.default_height_px,
            default_z_index=reg.default_z_index,
            props_schema=reg.props_schema or {},
        )
    return found


def _widget_from_declaration(app_id: str, entry: dict[str, Any]) -> WidgetRecord:
    """Build a `WidgetRecord` from a manifest `overlay_widgets` entry (validated at upload)."""
    return WidgetRecord(
        widget_id=entry["widget_id"],
        bundle_app_id=app_id,
        display_name=str(entry.get("display_name", entry["widget_id"])),
        description=str(entry.get("description", "")),
        category=str(entry.get("category", "custom")),
        output_kind=str(entry.get("output_kind", "text")),
        surface_id=entry.get("surface_id"),
        allow_standalone_output=bool(entry.get("allow_standalone_output", True)),
        default_x=int(entry.get("default_x", 48)),
        default_y=int(entry.get("default_y", 48)),
        default_width=int(entry.get("default_width", 400)),
        default_height=int(entry.get("default_height", 200)),
        default_z_index=int(entry.get("default_z_index", 10)),
        props_schema=dict(entry.get("props_schema") or {}),
    )


async def list_available_widgets(
    async_dal: Any, dal: Any, *, community_id: int
) -> list[WidgetRecord]:
    """Widgets this community may place: enabled-bundle declarations plus registry rows."""
    _bind(dal)

    def _fn() -> list[WidgetRecord]:
        tenant_id = _community_tenant_id(dal, community_id)
        return sorted(
            _widgets_sync(dal, tenant_id, community_id).values(), key=lambda w: w.widget_id
        )

    return list(await _run(async_dal, _fn, write=False))


async def get_scene_layout(async_dal: Any, dal: Any, *, community_id: int) -> SceneLayout:
    """The community's canvas and placed components, ordered back to front."""
    _bind(dal)

    def _fn() -> SceneLayout:
        tenant_id = _community_tenant_id(dal, community_id)
        cfg = _canvas_row(dal, community_id)
        comps = dal.overlay_scene_components
        rows = dal((comps.tenant_id == tenant_id) & (comps.community_id == community_id)).select(
            orderby=comps.z_index | comps.id
        )
        return SceneLayout(
            canvas=canvas_for_code(cfg.canvas_resolution_code if cfg else None),
            components=[_component_record(r) for r in rows],
        )

    return await _run(async_dal, _fn, write=False)  # type: ignore[no-any-return]


async def add_component(
    async_dal: Any, dal: Any, *, community_id: int, data: ComponentInput
) -> ComponentRecord:
    """Place `data.widget_id` on the scene; origin metadata comes from the resolved widget."""
    _bind(dal)

    def _fn() -> ComponentRecord:
        tenant_id = _community_tenant_id(dal, community_id)
        widget = _widgets_sync(dal, tenant_id, community_id).get(data.widget_id)
        if widget is None:
            raise not_found("Widget is not available to this community")
        comps = dal.overlay_scene_components
        scope = (comps.tenant_id == tenant_id) & (comps.community_id == community_id)
        if dal(scope).count() >= MAX_COMPONENTS:
            raise conflict(f"A scene holds at most {MAX_COMPONENTS} components")
        cfg = _canvas_row(dal, community_id)
        canvas = canvas_for_code(cfg.canvas_resolution_code if cfg else None)
        x = widget.default_x if data.x is None else data.x
        y = widget.default_y if data.y is None else data.y
        width = widget.default_width if data.width is None else data.width
        height = widget.default_height if data.height is None else data.height
        z_index = widget.default_z_index if data.z_index is None else data.z_index
        _check_geometry(x=x, y=y, width=width, height=height, canvas=canvas)
        now = datetime.now(UTC)
        public_id = uuid.uuid4()
        comps.insert(
            public_id=str(public_id),
            tenant_id=tenant_id,
            community_id=community_id,
            bundle_app_id=widget.bundle_app_id,
            widget_id=widget.widget_id,
            surface_id=widget.surface_id or "custom",
            output_kind_code=_OUTPUT_KIND_CODE_BY_LABEL.get(widget.output_kind, 1),
            display_name=widget.display_name,
            x_px=x,
            y_px=y,
            width_px=width,
            height_px=height,
            z_index=z_index,
            status_code=STATUS_VISIBLE,
            props=_check_props(data.props),
            created_at=now,
            updated_at=now,
        )
        row = dal(comps.public_id == str(public_id)).select().first()
        return _component_record(row)

    record: ComponentRecord = await _run(async_dal, _fn, write=True)
    logger.info(
        "overlay_component_added",
        extra={"community_id": community_id, "widget_id": record.widget_id},
    )
    return record


async def update_component(
    async_dal: Any, dal: Any, *, community_id: int, instance_id: str, data: ComponentPatch
) -> ComponentRecord:
    """Move / resize / show / hide / re-prop one placed component (scoped to tenant + community)."""
    _bind(dal)

    def _fn() -> ComponentRecord:
        tenant_id = _community_tenant_id(dal, community_id)
        comps = dal.overlay_scene_components
        row = (
            dal(
                (comps.public_id == instance_id)
                & (comps.tenant_id == tenant_id)
                & (comps.community_id == community_id)
            )
            .select()
            .first()
        )
        if row is None:
            raise not_found("Component not found")
        cfg = _canvas_row(dal, community_id)
        canvas = canvas_for_code(cfg.canvas_resolution_code if cfg else None)
        x = row.x_px if data.x is None else data.x
        y = row.y_px if data.y is None else data.y
        width = row.width_px if data.width is None else data.width
        height = row.height_px if data.height is None else data.height
        _check_geometry(x=x, y=y, width=width, height=height, canvas=canvas)
        updates: dict[str, Any] = {
            "x_px": x,
            "y_px": y,
            "width_px": width,
            "height_px": height,
            "updated_at": datetime.now(UTC),
        }
        if data.z_index is not None:
            updates["z_index"] = data.z_index
        if data.visible is not None:
            updates["status_code"] = STATUS_VISIBLE if data.visible else STATUS_HIDDEN
        if data.props is not None:
            updates["props"] = _check_props(data.props)
        row.update_record(**updates)
        return _component_record(dal(comps.id == row.id).select().first())

    return await _run(async_dal, _fn, write=True)  # type: ignore[no-any-return]


async def remove_component(
    async_dal: Any, dal: Any, *, community_id: int, instance_id: str
) -> bool:
    """Delete one placed component; returns whether a row was removed."""
    _bind(dal)

    def _fn() -> bool:
        tenant_id = _community_tenant_id(dal, community_id)
        comps = dal.overlay_scene_components
        deleted = dal(
            (comps.public_id == instance_id)
            & (comps.tenant_id == tenant_id)
            & (comps.community_id == community_id)
        ).delete()
        return bool(deleted)

    return bool(await _run(async_dal, _fn, write=True))


async def clear_layout(async_dal: Any, dal: Any, *, community_id: int) -> int:
    """Delete every placed component for the community; returns the count removed."""
    _bind(dal)

    def _fn() -> int:
        tenant_id = _community_tenant_id(dal, community_id)
        comps = dal.overlay_scene_components
        return int(
            dal((comps.tenant_id == tenant_id) & (comps.community_id == community_id)).delete()
        )

    removed = int(await _run(async_dal, _fn, write=True))
    logger.info("overlay_layout_cleared", extra={"community_id": community_id, "removed": removed})
    return removed


async def set_canvas(
    async_dal: Any, dal: Any, *, community_id: int, resolution: str
) -> CanvasRecord:
    """Set the scene resolution; 409 when a placed component would no longer fit the new canvas.

    Upserts the community's `presentation_config` row (the one svc-presentation reads).
    """
    code = _CANVAS_CODE_BY_LABEL.get(resolution)
    if code is None:
        raise bad_request(
            f"Unsupported resolution {resolution!r}; use one of {sorted(_CANVAS_CODE_BY_LABEL)}"
        )
    _bind(dal)
    canvas = canvas_for_code(code)

    def _fn() -> CanvasRecord:
        tenant_id = _community_tenant_id(dal, community_id)
        comps = dal.overlay_scene_components
        for row in dal(
            (comps.tenant_id == tenant_id) & (comps.community_id == community_id)
        ).select():
            try:
                _check_geometry(
                    x=row.x_px,
                    y=row.y_px,
                    width=row.width_px,
                    height=row.height_px,
                    canvas=canvas,
                )
            except ApiError as exc:
                raise conflict(
                    f"Component {row.widget_id!r} does not fit {canvas.resolution}; "
                    "move or remove it first"
                ) from exc
        now = datetime.now(UTC)
        cfg = _canvas_row(dal, community_id)
        if cfg is None:
            dal.presentation_config.insert(
                community_id=community_id,
                canvas_resolution_code=code,
                created_at=now,
                updated_at=now,
            )
        else:
            cfg.update_record(canvas_resolution_code=code, updated_at=now)
        return canvas

    result: CanvasRecord = await _run(async_dal, _fn, write=True)
    logger.info(
        "overlay_canvas_set", extra={"community_id": community_id, "resolution": result.resolution}
    )
    return result
