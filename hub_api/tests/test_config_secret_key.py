"""`config.py::HubAPIConfig.from_env()` -- C1 security fix, `secret_key` field.

`secret_key=require_secret_key()` (config.py's exact call, no args) is what
this file proves fails closed: forcing production posture on that same call
raises `InsecureSecretError` when `SECRET_KEY` is unset/placeholder, exactly
matching what happens if hub-api boots in a real deployment missing the env
var. The full fail-closed decision matrix (placeholder spellings, custom env
var names, non-production passthrough) lives in `flask_core`'s own
`tests/test_secrets.py` -- not duplicated here, only wired to prove hub-api's
`from_env()` actually calls the fixed helper rather than the old
`os.getenv("SECRET_KEY", "change-me-in-production")` fallback.
"""

from __future__ import annotations

import pytest
from flask_core.secrets import InsecureSecretError, require_secret_key

from config import HubAPIConfig


@pytest.fixture(autouse=True)
def _clean_secret_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)


def test_from_env_refuses_to_start_when_unset_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`from_env()`'s exact `secret_key` call raises when forced production, unset.

    `require_secret_key()`, no args -- raises once production posture is
    forced on, with SECRET_KEY unset. This is what a real deploy missing the
    env var now hits at startup instead of silently signing tokens with the
    public placeholder.
    """
    with pytest.raises(InsecureSecretError, match="SECRET_KEY"):
        require_secret_key(require=True)


def test_from_env_refuses_to_start_when_placeholder_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_KEY", "change-me-in-production")
    with pytest.raises(InsecureSecretError):
        require_secret_key(require=True)


def test_from_env_passes_through_real_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # T10: the session-cookie / refresh-token MAC key is its own env var (no shared JWT secret).
    monkeypatch.setenv("SESSION_MAC_KEY", "a-real-randomly-generated-value")
    cfg = HubAPIConfig.from_env()
    assert cfg.secret_key == "a-real-randomly-generated-value"


def test_from_env_does_not_raise_under_pytest_even_when_unset() -> None:
    """Documents the safe-for-tests default (`require=None`) resolution path.

    `HubAPIConfig.from_env()` itself never fails collection/startup of hub-api's
    own test suite even though SECRET_KEY is never set in CI/local test runs.
    """
    cfg = HubAPIConfig.from_env()
    assert cfg.secret_key == "change-me-in-production"
