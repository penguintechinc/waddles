"""Community public-UUID routing: converter, id-translation hook, codemod, real-app families."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from quart import Blueprint, Quart, jsonify
from quart.routing import QuartMap
from werkzeug.exceptions import NotFound
from werkzeug.routing import Rule

from app import create_app
from services import community_public_id as cpid
from services.community_authz import community_belongs_to_tenant
from tests.community_ids import UNKNOWN_PUBLIC_ID, public_id
from tests.conftest import TENANT_SLUG
from tests.test_app_factory import _test_config

REPO_ROOT = Path(__file__).resolve().parents[2]
UUID_A = "5B0E2F0A-3C1D-4A55-9D0E-0F6A5C3D7E11"


class TestConverter:
    """`cuuid` matches only dashed UUIDs and normalises case."""

    @staticmethod
    def _adapter() -> Any:
        url_map = QuartMap()
        url_map.converters["cuuid"] = cpid.CommunityUuidConverter
        url_map.add(Rule("/c/<cuuid:community_id>/x", endpoint="x", methods=["GET"]))
        return url_map.bind("localhost")

    def test_valid_uuid_routes_and_is_lowercased(self) -> None:
        _endpoint, args = self._adapter().match("/c/" + UUID_A + "/x", method="GET")
        assert args["community_id"] == UUID_A.lower()
        assert isinstance(args["community_id"], cpid.CommunityPublicId)

    @pytest.mark.parametrize(
        "bad",
        [
            "7",
            "0",
            "-1",
            "abc",
            "5b0e2f0a3c1d4a559d0e0f6a5c3d7e11",
            UUID_A + "0",
            "{" + UUID_A + "}",
        ],
    )
    def test_numeric_and_malformed_do_not_route(self, bad: str) -> None:
        with pytest.raises(NotFound):
            self._adapter().match(f"/c/{bad}/x", method="GET")


def _echo_app(db: Any, tenant_id: int) -> Quart:
    """Bare app with the converter + hook and one tenant-fenced echo route."""
    app = Quart(__name__)
    app.config["dal"] = db.dal
    app.config["async_dal"] = db
    cpid.install_community_public_ids(app)
    bp = Blueprint("echo", __name__)

    @bp.route("/c/<cuuid:community_id>/echo")
    async def echo(community_id: int) -> Any:
        """Echo the translated id; the real tenant helper decides ownership."""
        if not await community_belongs_to_tenant(
            db, db.dal, community_id=community_id, tenant_id=tenant_id
        ):
            return jsonify({"error": "Community not found"}), 404
        return jsonify({"community_id": community_id, "type": type(community_id).__name__})

    app.register_blueprint(bp)
    return app


def _seed(db: Any) -> tuple[int, int, int]:
    """Return `(own_tenant_id, own_community_id, foreign_community_id)`."""
    dal = db.dal
    own = dal(dal.tenants.slug == TENANT_SLUG).select().first()
    foreign_tenant = dal.tenants.insert(slug="other", display_name="Other", is_active=True)
    mine = dal.communities.insert(
        name="mine", tenant_id=own.id, is_active=True, public_id=str(uuid.uuid4())
    )
    theirs = dal.communities.insert(
        name="theirs", tenant_id=foreign_tenant, is_active=True, public_id=str(uuid.uuid4())
    )
    dal.commit()
    return int(own.id), int(mine), int(theirs)


class TestPreprocessor:
    async def test_uuid_resolves_to_numeric_int_for_the_handler(self, auth_db: Any) -> None:
        cpid.ID_CACHE.clear()
        tenant_id, mine, _ = _seed(auth_db)
        app = _echo_app(auth_db, tenant_id)
        response = await app.test_client().get(f"/c/{public_id(auth_db, mine)}/echo")
        assert response.status_code == 200
        assert await response.get_json() == {"community_id": mine, "type": "int"}

    async def test_uppercase_uuid_resolves(self, auth_db: Any) -> None:
        cpid.ID_CACHE.clear()
        tenant_id, mine, _ = _seed(auth_db)
        app = _echo_app(auth_db, tenant_id)
        response = await app.test_client().get(f"/c/{public_id(auth_db, mine).upper()}/echo")
        assert (await response.get_json())["community_id"] == mine

    async def test_numeric_id_is_a_404_and_never_reaches_the_handler(self, auth_db: Any) -> None:
        tenant_id, mine, _ = _seed(auth_db)
        app = _echo_app(auth_db, tenant_id)
        response = await app.test_client().get(f"/c/{mine}/echo")
        assert response.status_code == 404

    async def test_cache_hit_skips_the_lookup(self, auth_db: Any) -> None:
        cpid.ID_CACHE.clear()
        tenant_id, mine, _ = _seed(auth_db)
        app = _echo_app(auth_db, tenant_id)
        pid = public_id(auth_db, mine)
        client = app.test_client()
        await client.get(f"/c/{pid}/echo")
        assert len(cpid.ID_CACHE) == 1
        lookups: list[str] = []
        original = cpid.lookup_community_id

        async def counting(value: str) -> int:
            """Count only lookups that miss the cache (the real function consults it first)."""
            if cpid.ID_CACHE.get(value) is None:
                lookups.append(value)
            return await original(value)

        cpid.lookup_community_id = counting  # type: ignore[assignment]
        try:
            response = await client.get(f"/c/{pid}/echo")
        finally:
            cpid.lookup_community_id = original  # type: ignore[assignment]
        assert (await response.get_json())["community_id"] == mine
        assert lookups == []

    async def test_cache_hit_issues_no_communities_id_query(self, auth_db: Any) -> None:
        cpid.ID_CACHE.clear()
        tenant_id, mine, _ = _seed(auth_db)
        app = _echo_app(auth_db, tenant_id)
        pid = public_id(auth_db, mine)
        client = app.test_client()
        await client.get(f"/c/{pid}/echo")  # warm
        original = auth_db.select_async
        queries: list[int] = []

        async def spy(query: Any, *fields: Any, **kw: Any) -> Any:
            queries.append(len(fields))
            return await original(query, *fields, **kw)

        auth_db.select_async = spy
        try:
            await client.get(f"/c/{pid}/echo")
        finally:
            auth_db.select_async = original
        assert queries == [0]  # only the tenant-fence helper's own (field-less) query ran

    async def test_unknown_and_other_tenant_are_indistinguishable(self, auth_db: Any) -> None:
        cpid.ID_CACHE.clear()
        tenant_id, _mine, theirs = _seed(auth_db)
        app = _echo_app(auth_db, tenant_id)
        client = app.test_client()
        unknown = await client.get(f"/c/{UNKNOWN_PUBLIC_ID}/echo")
        foreign = await client.get(f"/c/{public_id(auth_db, theirs)}/echo")
        assert unknown.status_code == foreign.status_code == 404
        assert await unknown.get_json() == await foreign.get_json()

    async def test_unknown_uuid_is_not_cached(self, auth_db: Any) -> None:
        cpid.ID_CACHE.clear()
        tenant_id, _mine, _ = _seed(auth_db)
        app = _echo_app(auth_db, tenant_id)
        await app.test_client().get(f"/c/{UNKNOWN_PUBLIC_ID}/echo")
        assert len(cpid.ID_CACHE) == 0

    def test_cache_is_bounded_lru(self) -> None:
        cache = cpid._IdCache(max_entries=2)
        cache.put("a", 1)
        cache.put("b", 2)
        assert cache.get("a") == 1
        cache.put("c", 3)  # evicts b, the least recently used
        assert cache.get("b") is None
        assert cache.get("a") == 1
        assert cache.get("c") == 3
        assert len(cache) == 2


#: One representative route per blueprint family.
FAMILIES = {
    "admin": ("GET", "/api/v1/admin/{c}/settings"),
    "bot": ("GET", "/api/v1/admin/{c}/shoutout/config"),
    "calls": ("GET", "/api/v1/admin/{c}/calls/rooms"),
    "overlay": ("GET", "/api/v1/admin/{c}/browser-sources"),
    "event_proxy": ("GET", "/api/v1/admin/{c}/calendar/events"),
    "workflow": ("GET", "/api/v1/admin/{c}/workflows"),
}


class TestRealAppRouteFamilies:
    """Rule-level checks through the real app's url_map (no DB, no auth needed)."""

    @pytest.fixture
    def app(self) -> Quart:
        return create_app(_test_config())

    @pytest.mark.parametrize("family", sorted(FAMILIES))
    def test_uuid_matches_and_numeric_404s(self, app: Quart, family: str) -> None:
        method, template = FAMILIES[family]
        adapter = app.url_map.bind("localhost")
        adapter.match(template.format(c=str(uuid.uuid4())), method=method)
        with pytest.raises(NotFound):
            adapter.match(template.format(c="42"), method=method)

    def test_only_internal_service_key_routes_keep_a_numeric_community_param(
        self, app: Quart
    ) -> None:
        offenders = [
            r.rule
            for r in app.url_map.iter_rules()
            if "<int:community_id>" in r.rule or "<community_id>" in r.rule
        ]
        assert all("/internal" in o for o in offenders), offenders

    def test_event_proxy_rules_use_the_converter_and_downstream_stays_numeric(
        self, app: Quart
    ) -> None:
        from blueprints.v1 import event

        route = next(r for r in event.ALL_ROUTES if r.name == "adm_list_events")
        assert "{community_id}" in route.downstream_template
        rules = [r.rule for r in app.url_map.iter_rules() if r.endpoint.endswith("adm_list_events")]
        assert rules
        assert all("<cuuid:community_id>" in r for r in rules)


def _load_codemod() -> Any:
    spec = importlib.util.spec_from_file_location(
        "community_uuid_routes", REPO_ROOT / "scripts/codemods/community_uuid_routes.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["community_uuid_routes"] = module
    spec.loader.exec_module(module)
    return module


class TestCodemod:
    @pytest.fixture
    def tree(self, tmp_path: Path) -> Path:
        bp = tmp_path / "hub_api/blueprints/v1"
        bp.mkdir(parents=True)
        (tmp_path / "hub_api/services").mkdir(parents=True)
        (tmp_path / "hub_api/services/audit_events.py").write_text(
            '_ADM = "/api/v1/admin/<int:community_id>"\n'
        )
        (bp / "a.py").write_text(
            '"""Doc mentions /x/<community_id> only."""\n'
            "from quart import Blueprint\n"
            'a_bp = Blueprint("a", __name__, url_prefix="/api/v1")\n'
            'internal_bp = Blueprint("i", __name__, url_prefix="/api/v1/internal")\n'
            '@a_bp.route("/communities/<int:community_id>/things/<int:thing_id>")\n'
            "async def one(): ...\n"
            '@a_bp.get("/<community_id>/untyped")\n'
            "async def two(): ...\n"
            '@a_bp.get("/other/<int:user_id>")\n'
            "async def three(): ...\n"
            '@a_bp.get("/x/<community_public_id>/already")\n'
            "async def four(): ...\n"
            '@internal_bp.get("/c/<int:community_id>/svc")\n'
            "async def five(): ...\n"
        )
        return tmp_path

    def test_rewrites_only_community_params_and_is_idempotent(self, tree: Path) -> None:
        mod = _load_codemod()
        first = mod.run(tree, dry_run=False)
        assert sum(r.changed for r in first) == 3  # 2 routes + 1 audit key
        text = (tree / "hub_api/blueprints/v1/a.py").read_text()
        assert '"/communities/<cuuid:community_id>/things/<int:thing_id>"' in text
        assert '"/<cuuid:community_id>/untyped"' in text
        assert '"/other/<int:user_id>"' in text
        assert '"/x/<community_public_id>/already"' in text
        assert '"/c/<int:community_id>/svc"' in text  # internal blueprint stays numeric
        assert "/x/<community_id> only" in text  # docstring untouched
        before = {p: p.read_text() for p in tree.rglob("*.py")}
        second = mod.run(tree, dry_run=False)
        assert sum(r.changed for r in second) == 0
        assert before == {p: p.read_text() for p in tree.rglob("*.py")}

    def test_dry_run_reports_without_writing(self, tree: Path) -> None:
        mod = _load_codemod()
        original = (tree / "hub_api/blueprints/v1/a.py").read_text()
        results = mod.run(tree, dry_run=True)
        assert sum(r.changed for r in results) == 3
        assert (tree / "hub_api/blueprints/v1/a.py").read_text() == original
        assert any("internal" in s for r in results for s in r.skipped)
