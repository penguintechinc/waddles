"""0075: plaintext ai_knowledge_sources tokens are sealed; idempotent; fails loud without key.

Runs the real `upgrade()` against a real (sqlite) database through alembic's own
`Operations` context -- no mocked `op`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_PATH = Path(__file__).resolve().parent.parent / "versions" / "0075_ai_knowledge_token_enc.py"
KEY = "ab" * 32
PLAIN = "ghp_PLAINTEXT_SENTINEL_0123456789"


def _mod():
    spec = importlib.util.spec_from_file_location("migration_0075", _PATH)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def engine():
    eng = sa.create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE ai_knowledge_sources (id INTEGER PRIMARY KEY, encrypted_token TEXT)"
            )
        )
    return eng


def _run(eng, fn: str) -> None:
    m = _mod()
    with eng.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            getattr(m, fn)()


def _tokens(eng) -> dict[int, str | None]:
    with eng.connect() as c:
        return dict(
            c.execute(sa.text("SELECT id, encrypted_token FROM ai_knowledge_sources")).fetchall()
        )


def test_metadata_single_head_chain() -> None:
    m = _mod()
    assert (m.revision, m.down_revision) == (
        "0075_ai_knowledge_token_enc",
        "0074_connection_secret_version",
    )
    assert len(m.revision) <= 32


def test_seals_plaintext_and_roundtrips(engine, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_SYNC_ENCRYPTION_KEY", KEY)
    with engine.begin() as c:
        c.execute(
            sa.text("INSERT INTO ai_knowledge_sources VALUES (1, :t), (2, NULL), (3, '')"),
            {"t": PLAIN},
        )
    _run(engine, "upgrade")
    t = _tokens(engine)
    assert t[1] != PLAIN and PLAIN not in t[1]
    assert t[2] is None and t[3] == ""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "hub_api"))
    from services.github_sync_service import decrypt_token

    assert decrypt_token(t[1]) == PLAIN


def test_idempotent_no_double_encrypt(engine, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_SYNC_ENCRYPTION_KEY", KEY)
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO ai_knowledge_sources VALUES (1, :t)"), {"t": PLAIN})
    _run(engine, "upgrade")
    first = _tokens(engine)[1]
    _run(engine, "upgrade")
    assert _tokens(engine)[1] == first


def test_missing_key_fails_loud_and_leaves_row(engine, monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_SYNC_ENCRYPTION_KEY", raising=False)
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO ai_knowledge_sources VALUES (1, :t)"), {"t": PLAIN})
    with pytest.raises(RuntimeError, match="GITHUB_SYNC_ENCRYPTION_KEY"):
        _run(engine, "upgrade")


def test_no_rows_or_no_table_needs_no_key(engine, monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_SYNC_ENCRYPTION_KEY", raising=False)
    _run(engine, "upgrade")  # empty table
    bare = sa.create_engine("sqlite://")
    _run(bare, "upgrade")  # table absent


def test_downgrade_is_noop(engine, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_SYNC_ENCRYPTION_KEY", KEY)
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO ai_knowledge_sources VALUES (1, 'x')"))
    _run(engine, "downgrade")
    assert _tokens(engine)[1] == "x"


def test_single_head_and_upgrade_chain_reaches_0075() -> None:
    """The real revision graph has exactly one head and 0075 descends from 0074."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    ancestors = {r.revision for r in script.walk_revisions("base", "heads")}
    assert {"0074_connection_secret_version", "0075_ai_knowledge_token_enc"} <= ancestors
    assert script.get_revision("0075_ai_knowledge_token_enc").down_revision == (
        "0074_connection_secret_version"
    )
