"""Tests for scripts/ci/check_app_ids.py: real-repo denominator, parsers, and a proven failure."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "check_app_ids", ROOT / "scripts/ci/check_app_ids.py"
)
guard = importlib.util.module_from_spec(_spec)
sys.modules["check_app_ids"] = guard
_spec.loader.exec_module(guard)  # type: ignore[union-attr]

GOOD = "penguintechinc.waddles.ping"
# Legacy ids are assembled at runtime so the repo-wide old-id sweep never matches this file.
OLD = "waddles" + ".core."
OLD_EXAMPLE = OLD + "example.ping"
OLD_SERVICE = OLD + "service.discord"
OLD_ECONOMY = OLD + "service.economy"


def _write(root: Path, rel: str, body: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(body)


def _seed(root: Path) -> None:
    """Minimal tree with one legal id for every source family."""
    _write(
        root,
        "bundles/python/ping/bundle.yaml",
        f"app_id: {GOOD}\npermissions:\n  - tool.call:{GOOD}:echo\n",
    )
    _write(root, "bundles/core-bundles.yaml", f"bundles:\n  - app_id: {GOOD}\n")
    _write(root, "core/x/src/lib.rs", f'pub const PING_APP_ID: &str = "{GOOD}";\n')
    _write(root, "alembic/versions/0001_x.py", f'APP_ID = "{GOOD}"\n')
    _write(
        root,
        "config/postgres/migrations/001.sql",
        f"INSERT INTO app_catalog (app_id, feature) VALUES ('{GOOD}', 'penguintechinc.waddles');\n",
    )


def test_real_repo_has_denominator_and_no_violations():
    bad, denom = guard.scan(ROOT)
    assert all(v > 0 for v in denom.values()), denom
    assert sum(denom.values()) > 0
    assert bad == [], bad[:5]


def test_seeded_tree_passes_and_posthog_flag_is_not_an_app_id(tmp_path):
    _seed(tmp_path)
    bad, denom = guard.scan(tmp_path)
    assert bad == []
    assert denom == {
        s: 1 for s in guard.SOURCES
    }  # PostHog flag keys are not examined; only the seeded feature column is


def test_each_source_family_fails_on_one_injected_bad_id(tmp_path):
    cases = {
        "bundles/python/ping/bundle.yaml": (
            "app_id: " + GOOD,
            "app_id: " + OLD_EXAMPLE,
        ),
        "bundles/core-bundles.yaml": ("app_id: " + GOOD, "app_id: " + OLD_SERVICE),
        "core/x/src/lib.rs": (GOOD, "penguintechinc.waddles.fixture-bot-commands-default"),
        "alembic/versions/0001_x.py": (GOOD, "penguintechinc.waddles.echo-demo"),
        "config/postgres/migrations/001.sql": (
            f"VALUES ('{GOOD}'",
            "VALUES ('penguintechinc.waddles.discord-example'",
        ),
    }
    for rel, (old, new) in cases.items():
        _seed(tmp_path)
        p = tmp_path / rel
        p.write_text(p.read_text().replace(old, new, 1))
        bad, _ = guard.scan(tmp_path)
        assert len(bad) == 1 and bad[0][0] == rel, (rel, bad)


def test_tool_call_grant_with_old_id_fails(tmp_path):
    _seed(tmp_path)
    p = tmp_path / "bundles/python/ping/bundle.yaml"
    p.write_text(p.read_text() + f"  - tool.call:{OLD_ECONOMY}:adjust\n")
    bad, _ = guard.scan(tmp_path)
    assert [b[2] for b in bad] == [OLD_ECONOMY]


def test_validate_rules():
    assert guard.validate("psychoboy.penguintwitchbot.wheel") is None
    assert guard.validate("penguintechinc.waddles.echo-pipeline") is None
    for bad in (
        "penguintechinc.waddles.default",
        "penguintechinc.waddles.discord-demo",
        "penguintechinc.waddles.Upper",
        "penguintechinc.waddles.a.b",
        "acme.widgets.thing",
    ):
        assert guard.validate(bad), bad


def test_empty_tree_is_a_failure_not_a_pass(tmp_path, capsys):
    (tmp_path / "README.md").write_text("nothing here\n")
    assert guard.main(["check_app_ids.py", str(tmp_path)]) == 1
    assert "zero ids examined" in capsys.readouterr().out


def _seeded_baseline(root: Path) -> dict[str, int]:
    """Seed a legal tree, write its sweep baseline (the seed's PostHog flag key) and return it."""
    _seed(root)
    base = {k: len(v) for k, v in guard.sweep(root).items()}
    _write(root, guard.BASELINE_REL, json.dumps(base))
    return base


def test_sweep_fails_on_old_id_planted_in_alembic_test_fixture(tmp_path, capsys):
    """An old id in a test fixture (outside every structural parser) must fail the guard."""
    base = _seeded_baseline(tmp_path)
    _write(tmp_path, "alembic/tests/test_planted.py", f'APP = "{OLD}planted"\n')
    assert guard.main(["x", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "OLD-ID alembic/tests/test_planted.py:1" in out
    assert "old-id mention(s) beyond baseline" in out


def test_sweep_fails_on_old_id_planted_in_hub_api_test(tmp_path):
    """Same guarantee for hub_api/tests fixtures and arbitrary text files."""
    base = _seeded_baseline(tmp_path)
    _write(tmp_path, "hub_api/tests/test_x.py", f"id = '{OLD}ping'\n")
    _write(tmp_path, "docs/notes.md", f"see {OLD}service.discord\n")
    over = guard.sweep_violations(guard.sweep(tmp_path), base)
    assert {r for r, _, _ in over} == {"hub_api/tests/test_x.py", "docs/notes.md"}


def test_sweep_ignores_new_ids_and_platform_flag_keys(tmp_path):
    """New-shape ids and the two platform flag families are not old ids."""
    base = _seeded_baseline(tmp_path)
    flag = "waddles" + ".core."
    _write(
        tmp_path,
        "alembic/tests/test_ok.py",
        f'A = "{GOOD}"\nF1 = "{flag}rust-data-plane"\nF2 = "{flag}disable-db-bundle-config"\n',
    )
    assert guard.sweep_violations(guard.sweep(tmp_path), base) == []
    assert guard.main(["x", str(tmp_path)]) == 0


def test_sweep_baseline_budget_blocks_growth_but_not_existing_debt(tmp_path):
    """A baselined file passes at its count and fails when one more old id is added."""
    _write(tmp_path, "docs/a.md", f"{OLD}one\n")
    hits = guard.sweep(tmp_path)
    assert guard.sweep_violations(hits, {"docs/a.md": 1}) == []
    _write(tmp_path, "docs/a.md", f"{OLD}one\n{OLD}two\n")
    grown = guard.sweep_violations(guard.sweep(tmp_path), {"docs/a.md": 1})
    assert [(r, ln) for r, ln, _ in grown] == [("docs/a.md", 2)]


def test_real_repo_sweep_is_within_baseline():
    """The checked-in tree has no old-id mention beyond scripts/ci/old_id_baseline.json."""
    assert guard.sweep_violations(guard.sweep(ROOT), guard.load_baseline(ROOT)) == []
    assert not any(r.startswith("alembic/tests/test_0033") for r in guard.sweep(ROOT))

def test_planted_old_style_seed_feature_fails(tmp_path):
    """A seeded app_catalog.feature of the old waddles.<category>.<name> shape must fail the guard."""
    _seed(tmp_path)
    old_feature = OLD + "demo"
    _write(
        tmp_path,
        "config/postgres/migrations/002.sql",
        f"INSERT INTO app_catalog (app_id, feature) VALUES ('{GOOD}', '{old_feature}');\n",
    )
    bad, _ = guard.scan(tmp_path)
    assert any(v[2] == old_feature and "feature" in v[3] for v in bad), bad
    assert guard.main(["x", str(tmp_path)]) == 1
