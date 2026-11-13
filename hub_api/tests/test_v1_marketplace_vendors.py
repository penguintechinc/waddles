"""Vendor marketplaces (one per GitHub org) in `/marketplace/catalog` + `/marketplace/vendors`.

The sqlite fixture binds `marketplace_catalog` as a plain table, so rows carry the view's derived
`vendor_key` / `source_repo` columns directly; the view's SQL derivation itself is proven against
real Postgres in `alembic/tests/test_0091_marketplace_vendor_view.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.marketplace_catalog import catalog_bp, vendors_bp
from services import marketplace_catalog_service as catalog_service

_NOW = "2026-01-01 00:00:00"


@pytest.fixture
def dal(marketplace_catalog_db: Any) -> Any:
    db, _ids = marketplace_catalog_db
    return db


@pytest.fixture
def client(dal: Any) -> Any:
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(catalog_bp)
    app.register_blueprint(vendors_bp)
    app.config["dal"] = dal
    return app.test_client()


def _bundle(
    dal: Any, app_id: str, vendor: str | None, repo: str | None, author: str | None
) -> None:
    """Insert one `bundle` row the way the 0091 view would project it."""
    dal.marketplace_catalog.insert(
        source="bundle",
        source_id=None,
        source_ref=app_id,
        name=app_id,
        display_name=app_id,
        version="1.0.0",
        author=author,
        vendor_key=vendor,
        source_repo=repo,
        source_url=f"https://github.com/{repo}" if repo else None,
        is_core=False,
        pricing_type="free",
        price_cents=0,
        pricing_model="flat",
        avg_rating=0,
        review_count=0,
        install_count=0,
        created_at=_NOW,
        updated_at=_NOW,
        tenant_id=None,
    )
    dal.commit()


@pytest.fixture
def seeded(dal: Any) -> dict[str, int]:
    """Two vendors; `acme-co` has TWO repos; plus the fixture's 2 unaffiliated legacy rows."""
    for i in range(3):
        _bundle(dal, f"acme.social.b{i}", "acme-co", "acme-co/social", "github:Acme-Co")
    for i in range(2):
        _bundle(dal, f"acme.business.b{i}", "acme-co", "acme-co/business", "github:Acme-Co")
    _bundle(dal, "other.tool.b0", "other-org", "other-org/tools", "github:other-org")
    return {"acme-co": 5, "other-org": 1, "_unaffiliated": 2}


async def _json(client: Any, url: str) -> tuple[int, dict[str, Any]]:
    response = await client.get(url)
    return response.status_code, await response.get_json()


async def test_every_bundle_appears_with_vendor_attribution(client: Any, seeded: Any) -> None:
    status, body = await _json(client, "/api/v1/marketplace/catalog?limit=100")
    assert status == 200
    bundles = [m for m in body["modules"] if m["source"] == "bundle"]
    assert len(bundles) == 6
    by_ref = {m["sourceRef"]: m for m in bundles}
    assert by_ref["acme.social.b0"]["vendorKey"] == "acme-co"
    assert by_ref["acme.social.b0"]["vendorName"] == "Acme-Co"  # handle casing preserved
    assert by_ref["acme.social.b0"]["sourceRepo"] == "acme-co/social"
    assert by_ref["acme.social.b0"]["sourceId"] is None
    assert by_ref["other.tool.b0"]["vendorKey"] == "other-org"


async def test_demo_rows_still_present_and_unaffiliated(client: Any, seeded: Any) -> None:
    _, body = await _json(client, "/api/v1/marketplace/catalog?limit=100")
    legacy = {m["name"]: m for m in body["modules"] if m["source"] != "bundle"}
    assert set(legacy) == {"core-widget", "global-vendor-widget"}
    assert all(m["vendorKey"] is None and m["sourceRepo"] is None for m in legacy.values())


async def test_vendor_counts_sum_to_total(client: Any, seeded: Any) -> None:
    _, vendors = await _json(client, "/api/v1/marketplace/vendors")
    _, catalog = await _json(client, "/api/v1/marketplace/catalog?limit=100")
    assert vendors["total"] == catalog["pagination"]["total"] == 8
    assert sum(v["count"] for v in vendors["vendors"]) == vendors["total"]
    assert {v["key"]: v["count"] for v in vendors["vendors"]} == seeded
    assert vendors["vendors"][-1]["key"] == "_unaffiliated"  # sorted last
    for v in vendors["vendors"]:
        assert sum(r["count"] for r in v["repos"]) == v["count"]


async def test_vendor_with_two_repos_lists_both(client: Any, seeded: Any) -> None:
    _, vendors = await _json(client, "/api/v1/marketplace/vendors")
    acme = next(v for v in vendors["vendors"] if v["key"] == "acme-co")
    assert acme["name"] == "Acme-Co"
    assert acme["repos"] == [
        {"repo": "acme-co/social", "count": 3},
        {"repo": "acme-co/business", "count": 2},
    ]


async def test_vendor_filter(client: Any, seeded: Any) -> None:
    _, body = await _json(client, "/api/v1/marketplace/catalog?vendor=acme-co&limit=100")
    assert body["pagination"]["total"] == 5
    assert {m["vendorKey"] for m in body["modules"]} == {"acme-co"}
    _, upper = await _json(client, "/api/v1/marketplace/catalog?vendor=ACME-CO&limit=100")
    assert upper["pagination"]["total"] == 5  # keys are case-insensitive


async def test_repo_filter_within_vendor(client: Any, seeded: Any) -> None:
    url = "/api/v1/marketplace/catalog?vendor=acme-co&repo=acme-co/business&limit=100"
    _, body = await _json(client, url)
    assert body["pagination"]["total"] == 2
    assert {m["sourceRepo"] for m in body["modules"]} == {"acme-co/business"}


async def test_unaffiliated_filter(client: Any, seeded: Any) -> None:
    _, body = await _json(client, "/api/v1/marketplace/catalog?vendor=_unaffiliated&limit=100")
    assert {m["name"] for m in body["modules"]} == {"core-widget", "global-vendor-widget"}


async def test_empty_vendor_returns_empty_page(client: Any, seeded: Any) -> None:
    status, body = await _json(client, "/api/v1/marketplace/catalog?vendor=nobody-here")
    assert status == 200
    assert body["modules"] == []
    assert body["pagination"]["total"] == 0


@pytest.mark.parametrize("query", ["vendor=bad%20key", "repo=no-slash", "vendor=-x-"])
async def test_malformed_filter_is_400_not_ignored(client: Any, seeded: Any, query: str) -> None:
    status, body = await _json(client, f"/api/v1/marketplace/catalog?{query}")
    assert status == 400
    assert body["success"] is False


async def test_vendor_pagination_is_stable_and_complete(client: Any, seeded: Any) -> None:
    seen: list[str] = []
    for page in (1, 2, 3):
        _, body = await _json(
            client, f"/api/v1/marketplace/catalog?vendor=acme-co&limit=2&page={page}"
        )
        assert body["pagination"]["totalPages"] == 3
        seen += [m["sourceRef"] for m in body["modules"]]
    assert len(seen) == len(set(seen)) == 5


async def test_bundle_entry_by_app_id(client: Any, seeded: Any) -> None:
    status, body = await _json(client, "/api/v1/marketplace/catalog/bundle/acme.social.b1")
    assert status == 200
    assert body["module"]["vendorKey"] == "acme-co"
    status, _ = await _json(client, "/api/v1/marketplace/catalog/bundle/nope.nothing")
    assert status == 404


async def test_categories_count_includes_bundles(client: Any, dal: Any, seeded: Any) -> None:
    dal(dal.marketplace_catalog.source_ref == "acme.social.b0").update(category="alternatives")
    dal.commit()
    _, body = await _json(client, "/api/v1/marketplace/catalog/categories")
    assert {"category": "alternatives", "count": 1} in body["categories"]


@pytest.fixture(autouse=True)
def _reset_vendorless_log() -> None:
    """The once-per-app_id ERROR dedupe is process-global; isolate each test."""
    catalog_service._VENDORLESS_LOGGED.clear()


async def test_vendorless_bundles_are_listed_unaffiliated_not_500(
    client: Any, dal: Any, seeded: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """(a) no attribution and (b) plain-name author: 200, bucketed, one ERROR per bundle."""
    _bundle(dal, "legacy.no.versions", None, None, None)
    _bundle(dal, "plain.name.author", None, None, "Plain Name")
    caplog.set_level("ERROR", logger=catalog_service.logger.name)

    status, page = await _json(client, "/api/v1/marketplace/catalog?limit=100")
    assert status == 200
    by_ref = {m["sourceRef"]: m for m in page["modules"] if m["source"] == "bundle"}
    for ref in ("legacy.no.versions", "plain.name.author"):
        assert by_ref[ref]["vendorKey"] is None and by_ref[ref]["vendorName"] is None
    assert page["pagination"]["total"] == 10

    status, unaff = await _json(
        client, "/api/v1/marketplace/catalog?vendor=_unaffiliated&limit=100"
    )
    assert status == 200
    assert {m["name"] for m in unaff["modules"]} == {
        "core-widget",
        "global-vendor-widget",
        "legacy.no.versions",
        "plain.name.author",
    }

    status, one = await _json(client, "/api/v1/marketplace/catalog/bundle/legacy.no.versions")
    assert status == 200
    assert one["module"]["vendorKey"] is None

    status, vendors = await _json(client, "/api/v1/marketplace/vendors")
    assert status == 200
    assert vendors["total"] == page["pagination"]["total"] == 10
    assert sum(v["count"] for v in vendors["vendors"]) == vendors["total"]
    assert {v["key"]: v["count"] for v in vendors["vendors"]}["_unaffiliated"] == 4

    # Four requests touched both rows (list, filter, entry), yet exactly one ERROR per app_id.
    errors = [r for r in caplog.records if "no derivable vendor" in r.getMessage()]
    assert sorted(r.app_id for r in errors) == ["legacy.no.versions", "plain.name.author"]
    assert all(not hasattr(r, "author") for r in errors)  # PII-free: app_id + flag only
