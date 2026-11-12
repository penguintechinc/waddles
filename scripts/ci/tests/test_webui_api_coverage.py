"""Unit tests for scripts/ci/webui_api_coverage.py matcher, parsers and exclusions."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "webui_api_coverage", Path(__file__).resolve().parents[1] / "webui_api_coverage.py"
)
assert _SPEC and _SPEC.loader
cov = importlib.util.module_from_spec(_SPEC)
sys.modules["webui_api_coverage"] = cov
_SPEC.loader.exec_module(cov)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("/api/v1/communities/{community_id}/x", "/api/v1/communities/{}/x"),
        ("/api/v1/communities/<int:community_id>/x", "/api/v1/communities/{}/x"),
        ("/api/v1/communities/:id/x/", "/api/v1/communities/{}/x"),
        ("/api/v1/communities/${c.id}/x", "/api/v1/communities/{}/x"),
        ("/api/v1/items${qs}", "/api/v1/items"),
        ("/api/v1/items?page=1", "/api/v1/items"),
    ],
)
def test_normalise(raw: str, want: str) -> None:
    """Every param style collapses to the same key; query strings drop."""
    assert cov.normalise(raw) == want


def _ep(m: str, p: str) -> object:
    return cov.Endpoint(m, p, "f:1")


def test_match_and_method_mismatch() -> None:
    """Same path + method matches; same path other method is a method mismatch."""
    routes = [_ep("GET", "/api/v1/a/<int:id>")]
    assert cov.find_match(_ep("GET", "/api/v1/a/${x}"), routes) == (True, True)
    assert cov.find_match(_ep("DELETE", "/api/v1/a/${x}"), routes) == (True, False)
    assert cov.find_match(_ep("GET", "/api/v1/b"), routes) == (False, False)


def test_webui_param_matches_literal_segment() -> None:
    """A webui `${platform}` segment matches a literal route segment."""
    assert cov.find_match(_ep("GET", "/api/v1/auth/oauth/${p}"), [_ep("GET", "/api/v1/auth/oauth/discord")])[1]


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("/api/v1/internal/x", True),
        ("/health", True),
        ("/metrics", True),
        ("/api/v1/webhooks/twitch", True),
        ("/api/v1/auth/oauth/discord/callback", True),
        ("/api/v1/communities/{id}", False),
        ("/api/v1/internalish", False),
    ],
)
def test_exclusions(path: str, want: bool) -> None:
    """Internal/health/webhook/callback routes are excluded; lookalikes are not."""
    assert cov.excluded(path) is want


def test_feature_area_and_demo() -> None:
    """Area skips generic segments; demo flag uses the normalised path."""
    assert cov.feature_area("/api/v1/admin/${communityId}/calls/rooms") == "calls"
    assert not cov.is_demo("/api/v1/admin/${communityId}/music")
    assert cov.is_demo("/api/v1/communities/${id}/guild-pairings")


def test_parsers(tmp_path: Path) -> None:
    """Quart, webui, openapi and axum parsers each extract the expected pair."""
    (tmp_path / "bp.py").write_text(
        'bp = Blueprint("x", __name__, url_prefix="/api/v1/x")\n'
        '@bp.route("/<int:id>", methods=["GET", "DELETE"])\nasync def f(): ...\n'
        '@bp.post("/y")\nasync def g(): ...\n'
    )
    got = {e.key for e in cov.parse_quart(tmp_path, tmp_path)}
    assert got == {("GET", "/api/v1/x/{}"), ("DELETE", "/api/v1/x/{}"), ("POST", "/api/v1/x/y")}
    (tmp_path / "s.ts").write_text("apiClient\n  .get<Foo<Bar>>(\n  `/api/v1/x/${id}`)\nfetchy.get('/nope')\n")
    calls, files = cov.parse_webui(tmp_path, tmp_path)
    assert files == 1 and [c.key for c in calls] == [("GET", "/api/v1/x/{}")]
    (tmp_path / "v1.yaml").write_text("info: {}\npaths:\n  /api/v1/x/{id}:\n    get:\n      x: 1\n    put:\n      x: 1\n")
    assert {e.key for e in cov.parse_openapi(tmp_path / "v1.yaml", Path("v1.yaml"))} == {
        ("GET", "/api/v1/x/{}"),
        ("PUT", "/api/v1/x/{}"),
    }
    src = tmp_path / "src" / "api"
    src.mkdir(parents=True)
    (src / "mod.rs").write_text('Router::new().route("/rooms/{id}", get(a::get).delete(a::del))')
    assert {e.key for e in cov.parse_axum(tmp_path / "src", tmp_path)} == {
        ("GET", "/api/v1/rooms/{}"),
        ("DELETE", "/api/v1/rooms/{}"),
    }
