"""0128_dsar_pending_draft: chain and emitted DDL."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest import mock

from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0128_dsar_pending_draft.py"
_REV = "0128_dsar_pending_draft"
_PREV = "0127_tenant_license_key"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0128", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert module.revision == _REV and len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_history_has_one_head_and_0128_is_in_its_ancestry() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert _REV in {
        r.revision for r in script.walk_revisions(base="base", head=heads[0])
    }


def test_upgrade_and_downgrade_emit_expected_ddl() -> None:
    module = _load()
    with mock.patch.object(module.op, "execute") as execute:
        module.upgrade()
    up = "\n".join(c.args[0] for c in execute.call_args_list)
    assert "pending_message_enc TEXT" in up and "pending_regime SMALLINT" in up
    assert "pending_token_hash TEXT" in up
    assert "ck_dsar_requests_pending_regime" in up
    with mock.patch.object(module.op, "execute") as execute:
        module.downgrade()
    down = "\n".join(c.args[0] for c in execute.call_args_list)
    assert "DROP COLUMN IF EXISTS pending_regime" in down
    assert "DROP COLUMN IF EXISTS pending_message_enc" in down
    assert "DROP COLUMN IF EXISTS pending_token_hash" in down
