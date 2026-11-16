"""Static tests for 0110_merge_precompile_vault: the schema-neutral merge that restores one head."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = REPO_ROOT / "alembic" / "versions" / "0110_merge_precompile_vault.py"


def _load():
    """Import the revision module by path (versions/ is not a package)."""
    spec = importlib.util.spec_from_file_location("rev0110", _PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_merges_both_heads_and_id_fits() -> None:
    """The merge revises exactly the two diverged heads and its id fits VARCHAR(32)."""
    m = _load()
    assert m.revision == "0110_merge_precompile_vault"
    assert len(m.revision) <= 32
    assert set(m.down_revision) == {"0108_bundle_precompile_requests", "0109_platform_vault"}


def test_graph_has_exactly_one_head() -> None:
    """The full graph resolves to a single head, and the merge is still an ancestor of it.

    The merge was the head when it landed; later revisions chain on top, so the head moves on
    while the invariant stays: exactly one head, descended from the merge.
    """
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, heads
    ancestors = {rev.revision for rev in script.walk_revisions(base="base", head=heads[0])}
    assert "0110_merge_precompile_vault" in ancestors
