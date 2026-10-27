"""Tests for flask_core.validation -- models, scalar validators and the request decorators.

Decorators run inside a real Quart app so JSON/query/form parsing, status codes and the
strict / non-strict branches are exercised end to end.
"""

import logging
from datetime import datetime

import pytest
from pydantic import BaseModel
from quart import Quart

from flask_core import validation as v
from flask_core.validation import (
    CommunityIdRequired,
    DateRange,
    PaginationParams,
    PlatformRequired,
    UsernameRequired,
    validate_data,
    validate_email,
    validate_form,
    validate_json,
    validate_non_negative_integer,
    validate_positive_integer,
    validate_query,
    validate_url,
    validate_username_format,
)


class Item(BaseModel):
    name: str
    qty: int = 1

    model_config = {"extra": "forbid"}


class Flags(BaseModel):
    limit: int = 10
    ratio: float = 0.5
    active: bool = False
    tag: str = ""
    delta: int = 0


class TestModels:
    def test_pagination_defaults_and_bounds(self):
        p = PaginationParams()
        assert (p.limit, p.offset) == (50, 0)
        for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"extra": 1}):
            with pytest.raises(Exception):
                PaginationParams(**bad)

    def test_community_id_must_be_positive(self):
        assert CommunityIdRequired(community_id=3).community_id == 3
        with pytest.raises(Exception):
            CommunityIdRequired(community_id=0)

    def test_username_stripped_and_blank_rejected(self):
        assert UsernameRequired(username="  bob ").username == "bob"
        with pytest.raises(Exception):
            UsernameRequired(username="   ")
        with pytest.raises(Exception):
            UsernameRequired(username="")

    def test_date_range(self):
        ok = DateRange(start_date=datetime(2026, 1, 1), end_date=datetime(2026, 1, 2))
        assert ok.end_date > ok.start_date
        assert DateRange().start_date is None
        assert DateRange(end_date=datetime(2026, 1, 1)).end_date is not None  # no start -> no check
        with pytest.raises(Exception, match="end_date must be after start_date"):
            DateRange(start_date=datetime(2026, 1, 2), end_date=datetime(2026, 1, 1))
        with pytest.raises(Exception):
            DateRange(start_date=datetime(2026, 1, 1), end_date=datetime(2026, 1, 1))

    @pytest.mark.parametrize("platform,ok", [("twitch", True), ("discord", True), ("slack", True),
                                             ("kick", True), ("myspace", False), ("Twitch", False)])
    def test_platform_pattern(self, platform, ok):
        if ok:
            assert PlatformRequired(platform=platform).platform == platform
        else:
            with pytest.raises(Exception):
                PlatformRequired(platform=platform)


class TestScalarValidators:
    def test_email(self):
        assert validate_email("  A.B+tag@Example.COM ".strip()) == "a.b+tag@example.com"
        for bad in ("", "no-at-sign", "a@b", "a@@b.com", "a b@c.com"):
            with pytest.raises(ValueError):
                validate_email(bad)

    def test_url(self):
        assert validate_url("https://example.com/path?q=1") == "https://example.com/path?q=1"
        assert validate_url("http://a.io") == "http://a.io"
        for bad in ("", "ftp://example.com", "example.com", "http://localhost", "javascript:alert(1)"):
            with pytest.raises(ValueError):
                validate_url(bad)

    def test_username_format(self):
        assert validate_username_format("good_name-1") == "good_name-1"
        for bad in ("", "ab", "x" * 51, "has space", "bad!char"):
            with pytest.raises(ValueError):
                validate_username_format(bad)

    def test_integers(self):
        assert validate_positive_integer(1) == 1
        assert validate_non_negative_integer(0) == 0
        with pytest.raises(ValueError, match="qty must be positive"):
            validate_positive_integer(0, "qty")
        with pytest.raises(ValueError, match="value must be non-negative"):
            validate_non_negative_integer(-1)


class TestValidateData:
    def test_valid(self):
        ok, obj, errors = validate_data(Item, {"name": "x"})
        assert ok and obj.name == "x" and errors == []

    def test_invalid_reports_field_message_type(self):
        ok, obj, errors = validate_data(Item, {"qty": "abc", "bogus": 1})
        assert not ok and obj is None
        fields = {e["field"] for e in errors}
        assert {"name", "qty", "bogus"} <= fields
        assert all(set(e) == {"field", "message", "type"} for e in errors)


def make_app(strict=True) -> Quart:
    app = Quart("validation-test")

    @app.route("/json", methods=["POST"])
    @validate_json(Item, strict=strict)
    async def json_route(data):
        return {"got": data.model_dump() if isinstance(data, Item) else {"raw": data}}

    @app.route("/json-boom", methods=["POST"])
    @validate_json(Item)
    async def json_boom(data):
        raise RuntimeError("handler exploded")

    @app.route("/q")
    @validate_query(Flags, strict=strict)
    async def query_route(q):
        return {"got": q.model_dump() if isinstance(q, Flags) else {"raw": q}}

    @app.route("/q-boom")
    @validate_query(Flags)
    async def query_boom(q):
        raise RuntimeError("handler exploded")

    @app.route("/form", methods=["POST"])
    @validate_form(Item, strict=strict)
    async def form_route(data):
        return {"got": data.model_dump() if isinstance(data, Item) else {"raw": data}}

    @app.route("/form-boom", methods=["POST"])
    @validate_form(Item)
    async def form_boom(data):
        raise RuntimeError("handler exploded")

    return app


class TestValidateJson:
    async def test_valid_body_reaches_handler_as_model(self):
        resp = await make_app().test_client().post("/json", json={"name": "a", "qty": 3})
        assert resp.status_code == 200
        assert (await resp.get_json())["got"] == {"name": "a", "qty": 3}

    async def test_invalid_body_strict_400_with_field_errors(self):
        resp = await make_app().test_client().post("/json", json={"qty": "x"})
        body = await resp.get_json()
        assert resp.status_code == 400 and body["message"] == "Validation failed"
        assert {e["field"] for e in body["errors"]} == {"name", "qty"}

    async def test_invalid_body_non_strict_passes_raw_data(self):
        resp = await make_app(strict=False).test_client().post("/json", json={"qty": "x"})
        assert resp.status_code == 200
        assert (await resp.get_json())["got"] == {"raw": {"qty": "x"}}

    async def test_null_body_rejected(self):
        resp = await make_app().test_client().post("/json", data="null", headers={"Content-Type": "application/json"})
        body = await resp.get_json()
        assert resp.status_code == 400 and body["message"] == "Request body must be valid JSON"

    async def test_malformed_json_is_generic_400(self):
        resp = await make_app().test_client().post("/json", data="{oops", headers={"Content-Type": "application/json"})
        body = await resp.get_json()
        assert resp.status_code == 400 and body["message"] == "Invalid request data"

    async def test_handler_exception_does_not_leak_message(self):
        resp = await make_app().test_client().post("/json-boom", json={"name": "a"})
        assert resp.status_code == 400
        assert "exploded" not in (await resp.get_data()).decode()

    async def test_validation_failure_log_has_no_client_values(self, caplog):
        secret = "s3cret-" + "value"
        with caplog.at_level(logging.DEBUG, logger=v.logger.name):
            await make_app().test_client().post("/json", json={"name": "a", "qty": secret})
        assert secret not in caplog.text
        assert "validation_failed" in caplog.text


class TestValidateQuery:
    async def test_type_conversion(self):
        resp = await make_app().test_client().get("/q?limit=25&ratio=1.5&active=TRUE&tag=hello&delta=-4")
        assert resp.status_code == 200
        assert (await resp.get_json())["got"] == {
            "limit": 25, "ratio": 1.5, "active": True, "tag": "hello", "delta": -4,
        }

    async def test_non_numeric_dotted_value_stays_string(self):
        resp = await make_app().test_client().get("/q?tag=v1.2.3")
        assert (await resp.get_json())["got"]["tag"] == "v1.2.3"

    async def test_false_boolean(self):
        resp = await make_app().test_client().get("/q?active=false")
        assert (await resp.get_json())["got"]["active"] is False

    async def test_defaults_when_empty(self):
        resp = await make_app().test_client().get("/q")
        assert (await resp.get_json())["got"]["limit"] == 10

    async def test_invalid_strict_400(self):
        resp = await make_app().test_client().get("/q?limit=notint")
        body = await resp.get_json()
        assert resp.status_code == 400 and body["message"] == "Invalid query parameters"
        assert body["errors"][0]["field"] == "limit"

    async def test_invalid_non_strict_passes_raw(self):
        resp = await make_app(strict=False).test_client().get("/q?limit=notint")
        assert resp.status_code == 200
        assert (await resp.get_json())["got"] == {"raw": {"limit": "notint"}}

    async def test_handler_exception_is_generic_400(self):
        resp = await make_app().test_client().get("/q-boom")
        body = await resp.get_json()
        assert resp.status_code == 400 and body["errors"] == []
        assert "exploded" not in (await resp.get_data()).decode()


class TestValidateForm:
    async def test_valid_form(self):
        resp = await make_app().test_client().post("/form", form={"name": "a", "qty": "4"})
        assert resp.status_code == 200
        assert (await resp.get_json())["got"] == {"name": "a", "qty": 4}

    async def test_invalid_strict(self):
        resp = await make_app().test_client().post("/form", form={"qty": "x"})
        body = await resp.get_json()
        assert resp.status_code == 400 and body["message"] == "Invalid form data"
        assert {e["field"] for e in body["errors"]} == {"name", "qty"}

    async def test_invalid_non_strict_passes_raw(self):
        resp = await make_app(strict=False).test_client().post("/form", form={"qty": "x"})
        assert (await resp.get_json())["got"] == {"raw": {"qty": "x"}}

    async def test_handler_exception_is_generic_400(self):
        resp = await make_app().test_client().post("/form-boom", form={"name": "a"})
        body = await resp.get_json()
        assert resp.status_code == 400 and body["message"] == "Invalid form data" and body["errors"] == []


def test_all_exports_resolve():
    # regression: __all__ listed shared-library names that only existed when py_libs was
    # importable, so `from flask_core.validation import *` raised AttributeError without it.
    missing = [name for name in v.__all__ if not hasattr(v, name)]
    assert missing == []
    namespace: dict = {}
    exec("from flask_core.validation import *", namespace)  # noqa: S102 - star-import contract
    assert "validate_json" in namespace


def test_shared_models_reexported_when_py_libs_present(monkeypatch):
    import importlib.util
    import sys
    import types

    models = types.ModuleType("py_libs.models")
    for name in ("WaddleBaseModel", "WaddleRequestModel", "Platform",
                 "PaginationParams", "CommunityIdRequired", "DateRange"):
        setattr(models, name, type(name, (), {}))
    monkeypatch.setitem(sys.modules, "py_libs", types.ModuleType("py_libs"))
    monkeypatch.setitem(sys.modules, "py_libs.models", models)
    spec = importlib.util.spec_from_file_location("validation_with_shared", v.__file__,
                                                  submodule_search_locations=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "flask_core"
    spec.loader.exec_module(mod)
    assert mod.HAS_SHARED_MODELS is True
    assert mod.WaddleBaseModel is models.WaddleBaseModel
    # the real module is untouched, and its flag always agrees with what it exported
    assert v.HAS_SHARED_MODELS == (v.WaddleBaseModel is not None)
