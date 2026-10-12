"""
Regression: community_access must fail CLOSED on a missing/placeholder SECRET_KEY.

Before the fix, all three JWT-verifying call sites in ``community_access``
read ``os.getenv("SECRET_KEY", "change-me-in-production")`` directly, so an
unconfigured production deploy verified bearer tokens against a publicly known
key (forgeable tokens, including ``super_admin``). They now go through
``flask_core.secrets.require_secret_key``, which raises ``InsecureSecretError``
in a production-like environment instead of returning a default key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask_core import community_access
from flask_core.auth import create_jwt_token
from flask_core.community_access import (
    _is_super_admin,
    decode_caller_user_id,
    install_community_scoped_auth,
)
from flask_core.secrets import InsecureSecretError
from quart import Blueprint, Quart

PLACEHOLDER = "change-me-in-production"
REAL_SECRET = "a-real-randomly-generated-secret-value-0123456789"


class _Req:
    """Minimal request stand-in carrying only an Authorization header."""

    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force production posture (pytest detection off, RELEASE_MODE=true)."""
    monkeypatch.setattr("flask_core.secrets._running_under_pytest", lambda: False)
    monkeypatch.setenv("RELEASE_MODE", "true")


def _token(secret: str) -> str:
    return create_jwt_token(
        user_id="1",
        username="a",
        email="a@example.com",
        roles=["super_admin"],
        secret_key=secret,
        tenant="acme-corp",
    )


@pytest.mark.usefixtures("production")
class TestFailsClosedWhenUnset:
    def test_decode_caller_user_id_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SECRET_KEY", raising=False)
        with pytest.raises(InsecureSecretError, match="SECRET_KEY"):
            decode_caller_user_id(_Req(_token(PLACEHOLDER)))  # type: ignore[arg-type]

    def test_is_super_admin_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SECRET_KEY", raising=False)
        with pytest.raises(InsecureSecretError):
            _is_super_admin(_Req(_token(PLACEHOLDER)))  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["", PLACEHOLDER])
    def test_empty_or_placeholder_raises(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("SECRET_KEY", value)
        with pytest.raises(InsecureSecretError):
            decode_caller_user_id(_Req(_token(PLACEHOLDER)))  # type: ignore[arg-type]

    async def test_blueprint_before_request_never_authenticates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token forged with the public placeholder key is NOT accepted (never a 200)."""
        monkeypatch.delenv("SECRET_KEY", raising=False)
        bp = Blueprint("c", __name__, url_prefix="/api/v1/c")
        install_community_scoped_auth(bp)

        @bp.route("/whoami")
        async def whoami() -> tuple[dict[str, Any], int]:
            return {"ok": True}, 200

        app = Quart(__name__)
        app.config["PROPAGATE_EXCEPTIONS"] = False
        app.register_blueprint(bp)
        response = await app.test_client().get(
            "/api/v1/c/whoami",
            headers={"Authorization": f"Bearer {_token(PLACEHOLDER)}"},
        )
        assert response.status_code != 200


@pytest.mark.usefixtures("production")
def test_real_secret_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", REAL_SECRET)
    assert decode_caller_user_id(_Req(_token(REAL_SECRET))) == 1  # type: ignore[arg-type]
    assert _is_super_admin(_Req(_token(REAL_SECRET))) is True  # type: ignore[arg-type]


def test_default_placeholder_never_hardcoded_in_module() -> None:
    """The module source must not carry a `change-me` fallback literal at all."""
    source = Path(community_access.__file__).read_text()
    assert "change-me" not in source
    assert 'os.getenv("SECRET_KEY"' not in source
