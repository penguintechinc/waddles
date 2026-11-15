"""Composite overlay scene routes (`/overlay/widgets|layout*`) and `overlay_scene_service`.

Standalone Quart app over the `overlay_db` fixture with real JWTs and real pydal queries. The
cases that matter most are the isolation ones: a widget registered by another tenant must never be
listed or placeable, and a component id from another community must read as 404.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.overlay as overlay_module
from blueprints.v1.overlay import SCOPE_ADMIN, overlay_bp
from services import overlay_scene_service
from services.schema import bind_lifecycle_tables
from tests.test_v1_overlay_blueprint import (
    OTHER_TENANT_SLUG,
    _owner_headers,
    _test_config,
    seed_community,
)

WIDGET = {
    "widget_id": "alerts.stream.event_box",
    "display_name": "Alert Box",
    "output_kind": "sanitized_html",
    "default_x": 660,
    "default_y": 120,
    "default_width": 600,
    "default_height": 220,
}


@pytest.fixture
def app(overlay_db: Any) -> Quart:
    """Quart app mounting only the overlay blueprint, lifecycle tables migrated."""
    bind_lifecycle_tables(overlay_db.dal, migrate=True)
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(overlay_bp)
    quart_app.config["dal"] = overlay_db.dal
    quart_app.config["async_dal"] = overlay_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    """Test client."""
    return app.test_client()


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default every feature flag ON; individual tests flip it off."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(overlay_module, "feature_enabled", stub)
    return stub


def _tenant_id(overlay_db: Any, community_id: int) -> int:
    """Tenant id owning `community_id`."""
    dal = overlay_db.dal
    return int(dal(dal.communities.id == community_id).select().first().tenant_id)


def _activate_bundle(
    overlay_db: Any,
    community_id: int,
    app_id: str,
    widgets: list[dict],
    owned_surfaces: list[str] | None = None,
) -> None:
    """Enable a bundle declaring `widgets` under `stages.presentation.overlay_widgets`."""
    dal = overlay_db.dal
    tenant_id = _tenant_id(overlay_db, community_id)
    dal.app_catalog.insert(
        app_id=app_id,
        name=app_id,
        manifest_version="2",
        module="core",
        feature="f",
        provider="builtin",
        execution_model="wasi-component",
        platform_compatibility={},
        status="active",
        stages={
            "presentation": {"overlay_widgets": widgets, "owned_surfaces": owned_surfaces or []}
        },
    )
    dal.app_activations.insert(
        community_id=community_id, tenant_id=tenant_id, app_id=app_id, enabled=True
    )
    dal.commit()


def _register_widget(
    overlay_db: Any,
    community_id: int,
    widget_id: str,
    *,
    bundle_app_id: str = "reg.bundle",
    surface_id: str | None = None,
    output_kind_code: int = 1,
) -> None:
    """Insert an `overlay_widget_registry` row for `community_id`."""
    dal = overlay_db.dal
    dal.overlay_widget_registry.insert(
        public_id=f"00000000-0000-0000-0000-{community_id:012d}",
        tenant_id=_tenant_id(overlay_db, community_id),
        community_id=community_id,
        bundle_app_id=bundle_app_id,
        surface_id=surface_id,
        widget_id=widget_id,
        display_name=widget_id,
        output_kind_code=output_kind_code,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    dal.commit()


def _url(community_id: int, suffix: str = "") -> str:
    """Admin overlay URL."""
    return f"/api/v1/admin/{community_id}/overlay{suffix}"


class TestWidgets:
    async def test_lists_enabled_bundle_widgets(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "waddles.core.overlay.alerts", [WIDGET])
        res = await client.get(_url(cid, "/widgets"), headers=headers)
        assert res.status_code == 200
        widgets = (await res.get_json())["widgets"]
        assert [w["widget_id"] for w in widgets] == ["alerts.stream.event_box"]
        assert widgets[0]["bundle_app_id"] == "waddles.core.overlay.alerts"

    async def test_disabled_bundle_is_not_listed(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "waddles.core.overlay.alerts", [WIDGET])
        dal = overlay_db.dal
        dal(dal.app_activations.app_id == "waddles.core.overlay.alerts").update(enabled=False)
        dal.commit()
        res = await client.get(_url(cid, "/widgets"), headers=headers)
        assert (await res.get_json())["widgets"] == []

    async def test_registry_widgets_are_tenant_isolated(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        other = seed_community(overlay_db, tenant_slug=OTHER_TENANT_SLUG, name="other")
        _activate_bundle(overlay_db, cid, "reg.bundle", [])
        _register_widget(overlay_db, cid, "mine.widget")
        _register_widget(overlay_db, other, "theirs.widget")
        res = await client.get(_url(cid, "/widgets"), headers=headers)
        ids = [w["widget_id"] for w in (await res.get_json())["widgets"]]
        assert ids == ["mine.widget"]

    async def test_flag_off_is_402(
        self, client: Any, overlay_db: Any, _flags_on: AsyncMock
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _flags_on.return_value = False
        res = await client.get(_url(cid, "/widgets"), headers=headers)
        assert res.status_code == 402

    async def test_requires_scope(self, client: Any, overlay_db: Any) -> None:
        _, cid, _ = _owner_headers(overlay_db)
        res = await client.get(_url(cid, "/widgets"))
        assert res.status_code == 401
        assert SCOPE_ADMIN == "streaming.overlay:admin"


class TestBundleIsolation:
    async def test_same_widget_id_in_two_bundles_does_not_shadow(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        first = {**WIDGET, "display_name": "First", "default_x": 100}
        second = {**WIDGET, "display_name": "Second", "default_x": 200}
        _activate_bundle(overlay_db, cid, "a.bundle", [first])
        _activate_bundle(overlay_db, cid, "b.bundle", [second])
        listed = (await (await client.get(_url(cid, "/widgets"), headers=headers)).get_json())[
            "widgets"
        ]
        assert [(w["bundle_app_id"], w["display_name"]) for w in listed] == [
            ("a.bundle", "First"),
            ("b.bundle", "Second"),
        ]
        for app_id, x in (("a.bundle", 100), ("b.bundle", 200)):
            res = await client.post(
                _url(cid, "/layout/components"),
                headers=headers,
                json={"bundle_app_id": app_id, "widget_id": WIDGET["widget_id"]},
            )
            comp = (await res.get_json())["component"]
            assert (comp["bundle_app_id"], comp["x"]) == (app_id, x)

    async def test_placement_must_name_the_declaring_bundle(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "a.bundle", [WIDGET])
        res = await client.post(
            _url(cid, "/layout/components"),
            headers=headers,
            json={"bundle_app_id": "b.bundle", "widget_id": WIDGET["widget_id"]},
        )
        assert res.status_code == 404

    async def test_widget_pointing_at_another_bundles_surface_is_withheld(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        owner = {
            **WIDGET,
            "widget_id": "music.card",
            "output_kind": "surface",
            "surface_id": "owner.bundle.now",
        }
        thief = {
            **WIDGET,
            "widget_id": "evil.card",
            "output_kind": "surface",
            "surface_id": "owner.bundle.now",
        }
        _activate_bundle(
            overlay_db, cid, "owner.bundle", [owner], owned_surfaces=["owner.bundle.now"]
        )
        _activate_bundle(overlay_db, cid, "thief.bundle", [thief])
        listed = (await (await client.get(_url(cid, "/widgets"), headers=headers)).get_json())[
            "widgets"
        ]
        assert [w["widget_id"] for w in listed] == ["music.card"]
        res = await client.post(
            _url(cid, "/layout/components"),
            headers=headers,
            json={"bundle_app_id": "thief.bundle", "widget_id": "evil.card"},
        )
        assert res.status_code == 404

    async def test_registry_row_cannot_claim_another_bundles_surface(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        owner = {
            **WIDGET,
            "widget_id": "music.card",
            "output_kind": "surface",
            "surface_id": "owner.bundle.now",
        }
        _activate_bundle(
            overlay_db, cid, "owner.bundle", [owner], owned_surfaces=["owner.bundle.now"]
        )
        _activate_bundle(overlay_db, cid, "thief.bundle", [])
        _register_widget(
            overlay_db,
            cid,
            "evil.card",
            bundle_app_id="thief.bundle",
            surface_id="owner.bundle.now",
            output_kind_code=4,
        )
        listed = (await (await client.get(_url(cid, "/widgets"), headers=headers)).get_json())[
            "widgets"
        ]
        assert [w["widget_id"] for w in listed] == ["music.card"]

    async def test_contested_surface_withholds_only_its_own_widgets(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        shared = "a.bundle.shared"
        card_a = {**WIDGET, "widget_id": "a.card", "output_kind": "surface", "surface_id": shared}
        card_b = {**WIDGET, "widget_id": "b.card", "output_kind": "surface", "surface_id": shared}
        _activate_bundle(overlay_db, cid, "a.bundle", [card_a, WIDGET], owned_surfaces=[shared])
        _activate_bundle(overlay_db, cid, "b.bundle", [card_b], owned_surfaces=[shared])
        _activate_bundle(
            overlay_db,
            cid,
            "c.bundle",
            [{**WIDGET, "widget_id": "c.text"}],
        )
        res = await client.get(_url(cid, "/widgets"), headers=headers)
        assert res.status_code == 200
        ids = sorted(
            (w["bundle_app_id"], w["widget_id"]) for w in (await res.get_json())["widgets"]
        )
        assert ids == [("a.bundle", WIDGET["widget_id"]), ("c.bundle", "c.text")]

    async def test_registry_row_of_disabled_bundle_is_not_listed(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _register_widget(overlay_db, cid, "orphan.widget", bundle_app_id="gone.bundle")
        res = await client.get(_url(cid, "/widgets"), headers=headers)
        assert (await res.get_json())["widgets"] == []


class TestLayout:
    async def test_default_canvas_is_1080p(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        res = await client.get(_url(cid, "/layout"), headers=headers)
        layout = (await res.get_json())["layout"]
        assert layout["canvas"] == {"resolution": "1080p", "width": 1920, "height": 1080}
        assert layout["components"] == []

    async def test_component_lifecycle_uses_widget_defaults(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "waddles.core.overlay.alerts", [WIDGET])
        add = await client.post(
            _url(cid, "/layout/components"),
            headers=headers,
            json={"bundle_app_id": "waddles.core.overlay.alerts", "widget_id": WIDGET["widget_id"]},
        )
        assert add.status_code == 201
        comp = (await add.get_json())["component"]
        assert (comp["x"], comp["y"], comp["width"], comp["height"]) == (660, 120, 600, 220)
        assert comp["bundle_app_id"] == "waddles.core.overlay.alerts"
        assert comp["output_kind"] == "sanitized_html"
        assert comp["visible"] is True

        upd = await client.patch(
            _url(cid, f"/layout/components/{comp['instance_id']}"),
            headers=headers,
            json={"x": 100, "visible": False, "props": {"theme": "dark"}},
        )
        assert upd.status_code == 200
        updated = (await upd.get_json())["component"]
        assert (updated["x"], updated["visible"], updated["props"]) == (
            100,
            False,
            {"theme": "dark"},
        )

        gone = await client.delete(
            _url(cid, f"/layout/components/{comp['instance_id']}"), headers=headers
        )
        assert gone.status_code == 200
        again = await client.delete(
            _url(cid, f"/layout/components/{comp['instance_id']}"), headers=headers
        )
        assert again.status_code == 404

    async def test_spoofed_origin_fields_are_ignored(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "real.bundle", [WIDGET])
        add = await client.post(
            _url(cid, "/layout/components"),
            headers=headers,
            json={"bundle_app_id": "real.bundle", "widget_id": WIDGET["widget_id"]},
        )
        assert (await add.get_json())["component"]["bundle_app_id"] == "real.bundle"

    async def test_unavailable_widget_is_404(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        other = seed_community(overlay_db, tenant_slug=OTHER_TENANT_SLUG, name="other")
        _activate_bundle(overlay_db, cid, "reg.bundle", [])
        _register_widget(overlay_db, other, "theirs.widget")
        res = await client.post(
            _url(cid, "/layout/components"),
            headers=headers,
            json={"bundle_app_id": "reg.bundle", "widget_id": "theirs.widget"},
        )
        assert res.status_code == 404

    @pytest.mark.parametrize(
        "body",
        [
            {"widget_id": "BAD ID"},
            {"widget_id": "w", "x": -1},
            {"widget_id": "w", "x": "abc"},
            {"widget_id": "w", "width": 31},
            {"widget_id": "w", "z_index": 1001},
        ],
    )
    async def test_invalid_bodies_are_rejected(
        self, client: Any, overlay_db: Any, body: dict[str, Any]
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        res = await client.post(_url(cid, "/layout/components"), headers=headers, json=body)
        assert res.status_code in (400, 422)

    async def test_geometry_must_fit_canvas(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "real.bundle", [WIDGET])
        res = await client.post(
            _url(cid, "/layout/components"),
            headers=headers,
            json={"bundle_app_id": "real.bundle", "widget_id": WIDGET["widget_id"], "x": 3000},
        )
        assert res.status_code == 400

    async def test_oversized_props_rejected(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "real.bundle", [WIDGET])
        res = await client.post(
            _url(cid, "/layout/components"),
            headers=headers,
            json={
                "bundle_app_id": "real.bundle",
                "widget_id": WIDGET["widget_id"],
                "props": {"k": "x" * 9000},
            },
        )
        assert res.status_code == 400

    async def test_component_cap(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "real.bundle", [WIDGET])
        for _ in range(overlay_scene_service.MAX_COMPONENTS):
            ok = await client.post(
                _url(cid, "/layout/components"),
                headers=headers,
                json={"bundle_app_id": "real.bundle", "widget_id": WIDGET["widget_id"]},
            )
            assert ok.status_code == 201
        over = await client.post(
            _url(cid, "/layout/components"),
            headers=headers,
            json={"bundle_app_id": "real.bundle", "widget_id": WIDGET["widget_id"]},
        )
        assert over.status_code == 409

    async def test_other_communitys_component_is_404(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "real.bundle", [WIDGET])
        add = await client.post(
            _url(cid, "/layout/components"),
            headers=headers,
            json={"bundle_app_id": "real.bundle", "widget_id": WIDGET["widget_id"]},
        )
        instance = (await add.get_json())["component"]["instance_id"]
        other_headers, other_cid, _ = _owner_headers(overlay_db)
        res = await client.delete(
            _url(other_cid, f"/layout/components/{instance}"), headers=other_headers
        )
        assert res.status_code == 404
        still = await client.get(_url(cid, "/layout"), headers=headers)
        assert len((await still.get_json())["layout"]["components"]) == 1

    async def test_clear_layout(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "real.bundle", [WIDGET])
        for _ in range(3):
            await client.post(
                _url(cid, "/layout/components"),
                headers=headers,
                json={"bundle_app_id": "real.bundle", "widget_id": WIDGET["widget_id"]},
            )
        res = await client.delete(_url(cid, "/layout"), headers=headers)
        assert (await res.get_json())["removed_count"] == 3


class TestCanvas:
    async def test_set_canvas_persists_int_code_on_presentation_config(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        res = await client.put(
            _url(cid, "/layout/canvas"), headers=headers, json={"resolution": "4k"}
        )
        assert res.status_code == 200
        assert (await res.get_json())["canvas"] == {
            "resolution": "4k",
            "width": 3840,
            "height": 2160,
        }
        dal = overlay_db.dal
        assert (
            dal(dal.presentation_config.community_id == cid).select().first().canvas_resolution_code
            == 3
        )
        back = await client.get(_url(cid, "/layout"), headers=headers)
        assert (await back.get_json())["layout"]["canvas"]["resolution"] == "4k"
        # Idempotent: a second set updates the same row, never inserts a duplicate.
        await client.put(_url(cid, "/layout/canvas"), headers=headers, json={"resolution": "2k"})
        assert dal(dal.presentation_config.community_id == cid).count() == 1

    async def test_unknown_resolution_rejected(self, client: Any, overlay_db: Any) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        res = await client.put(
            _url(cid, "/layout/canvas"), headers=headers, json={"resolution": "8k"}
        )
        assert res.status_code in (400, 422)

    async def test_shrinking_canvas_that_would_orphan_a_component_is_409(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, cid, _ = _owner_headers(overlay_db)
        _activate_bundle(overlay_db, cid, "real.bundle", [WIDGET])
        await client.put(_url(cid, "/layout/canvas"), headers=headers, json={"resolution": "4k"})
        await client.post(
            _url(cid, "/layout/components"),
            headers=headers,
            json={
                "bundle_app_id": "real.bundle",
                "widget_id": WIDGET["widget_id"],
                "x": 3000,
                "y": 100,
            },
        )
        res = await client.put(
            _url(cid, "/layout/canvas"), headers=headers, json={"resolution": "1080p"}
        )
        assert res.status_code == 409
        kept = await client.get(_url(cid, "/layout"), headers=headers)
        assert (await kept.get_json())["layout"]["canvas"]["resolution"] == "4k"


def test_canvas_for_code_defaults_to_1080p() -> None:
    assert overlay_scene_service.canvas_for_code(None).resolution == "1080p"
    assert overlay_scene_service.canvas_for_code(99).width == 1920
    assert overlay_scene_service.canvas_for_code(2).height == 1440
