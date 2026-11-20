"""Unit tests for scripts/rename/apply_mapping.py (pure functions + a temp git repo)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("apply_mapping", HERE.parent / "apply_mapping.py")
am = importlib.util.module_from_spec(SPEC)
sys.modules["apply_mapping"] = am
SPEC.loader.exec_module(am)

MAP = {
    "waddles.core.example.count": "penguintechinc.waddles.count",
    "waddles.core.example.counter": "penguintechinc.waddles.counter",
    "waddles.core.example.game-servers": "penguintechinc.waddles.game-servers",
    "waddles.core.service.discord": "penguintechinc.waddles.discord",
    "waddles.core.example.discord": "penguintechinc.waddles.discord-invite",
    "waddles.integrations.waddleai.default": "penguintechinc.waddles.waddleai",
}
RULES = am.build_rules(MAP)


def rw(text: str) -> str:
    return am.rewrite_text(text, RULES)


def test_longest_first_boundary_safety():
    assert rw("waddles.core.example.counter") == "penguintechinc.waddles.counter"
    assert rw("waddles.core.example.count") == "penguintechinc.waddles.count"
    # superstrings that are not mapped ids are left alone
    assert rw("waddles.core.example.counters") == "waddles.core.example.counters"
    assert rw("waddles.core.example.count-x") == "waddles.core.example.count-x"
    assert rw("xwaddles.core.example.count") == "xwaddles.core.example.count"
    assert rw("app_id: waddles.core.example.count.") == "app_id: penguintechinc.waddles.count."


def test_collision_pair_maps_independently():
    out = rw("a=waddles.core.service.discord b=waddles.core.example.discord")
    assert out == "a=penguintechinc.waddles.discord b=penguintechinc.waddles.discord-invite"


def test_idempotent():
    src = (
        'x: "tool.call:waddles.core.service.discord:send"\n'
        "waddles.core.example.count waddles_core_example_game_servers_t\n"
    )
    once = rw(src)
    assert rw(once) == once
    assert "waddles.core" not in once and "waddles_core" not in once


def test_all_real_mapping_rows_idempotent_and_no_example_or_demo():
    mapping = am.load_mapping(am.DEFAULT_MAPPING)
    rules = am.build_rules(mapping)
    assert len(mapping) == 274  # 200 bundles + 19 extra pipeline/media + 55 test-fixture ids
    for old, new in mapping.items():
        assert "example" not in new and "demo" not in new
        once = am.rewrite_text(f"{old} {am.sanitize_rust(old)}", rules)
        assert once == f"{new} {am.sanitize_rust(new)}"
        assert am.rewrite_text(once, rules) == once
    assert mapping["waddles.core.example.discord"] == "penguintechinc.waddles.discord-invite"
    assert mapping["waddles.core.service.discord"] == "penguintechinc.waddles.discord"


def test_exclusions_untouched():
    src = (
        "waddles.integrations.waddleai waddles.integrations.provider_connections "
        "waddles.integrations.static_connections waddles.core.rust-data-plane "
        "waddles.core.disable-pii-tokenization waddles:app:1:2:x:state "
        "waddles.integrations.waddleai.default"
    )
    out = rw(src)
    assert out == src.replace(
        "waddles.integrations.waddleai.default", "penguintechinc.waddles.waddleai"
    )


def test_tool_call_grant_form():
    src = 'id: "tool.call:waddles.core.service.discord:discord.scheduled_event.create"'
    assert rw(src) == 'id: "tool.call:penguintechinc.waddles.discord:discord.scheduled_event.create"'


def test_kv_key_form():
    assert rw("waddles:app:7:3:waddles.core.example.count:state") == (
        "waddles:app:7:3:penguintechinc.waddles.count:state"
    )


def test_sanitised_forms_match_code_sanitisers():
    assert am.sanitize_rust("waddles.core.example.game-servers") == "waddles_core_example_game_servers"
    assert am.sanitize_py("waddles.core.example.game-servers") == "waddles_core_example_game-servers"
    # rust form + table suffix
    assert rw("app_core.waddles_core_example_game_servers_items") == (
        "app_core.penguintechinc_waddles_game_servers_items"
    )
    # python form keeps the hyphen
    assert rw("waddles_core_example_game-servers") == "penguintechinc_waddles_game-servers"
    # sanitised boundary safety
    assert rw("waddles_core_example_counter_t") == "penguintechinc_waddles_counter_t"
    assert rw("waddles_core_example_counters") == "waddles_core_example_counters"


def test_mapping_ids_not_prefix_of_each_other_and_new_do_not_contain_old():
    mapping = am.load_mapping(am.DEFAULT_MAPPING)
    for a in mapping:
        for b in mapping:
            if a != b:
                assert not b.startswith(a + "."), (a, b)
    for new in mapping.values():
        assert not am.build_rules(mapping).dotted.search(new)


def test_glob_matching():
    rx = am.glob_to_regex("hub_api/services/**/*.py")
    assert rx.match("hub_api/services/a/b.py") and rx.match("hub_api/services/b.py")
    assert not rx.match("hub_api/x/b.py")
    assert am.glob_to_regex("bundles/python/**").match("bundles/python/a/b/c.yaml")
    assert not am.glob_to_regex("*.md").match("a/b.md")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a/x.yaml").write_text("app_id: waddles.core.example.count\r\n")
    (tmp_path / "b/y.yaml").write_text("app_id: waddles.core.example.counter\n")
    (tmp_path / "a/bin.dat").write_bytes(b"\xff\xfe waddles.core.example.count")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def _mapping_file(tmp_path: Path) -> Path:
    p = tmp_path.parent / (tmp_path.name + "-map.tsv")
    p.write_text(
        "old\tnew\tkind\trationale\n"
        + "".join(f"{o}\t{n}\tapp\tr\n" for o, n in MAP.items())
    )
    return p


def test_cli_paths_limit_check_and_idempotent(repo: Path, capsys):
    mp = _mapping_file(repo)
    base = ["--root", str(repo), "--mapping", str(mp)]
    assert am.main(base + ["--check"]) == 1  # 2 text hits remain (binary skipped)
    assert "remaining: total=2" in capsys.readouterr().out
    assert am.main(base + ["--paths", "a/**"]) == 0
    assert (repo / "a/x.yaml").read_bytes() == b"app_id: penguintechinc.waddles.count\r\n"
    assert "counter" in (repo / "b/y.yaml").read_text()  # outside --paths, untouched
    assert (repo / "b/y.yaml").read_text() == "app_id: waddles.core.example.counter\n"
    assert (repo / "a/bin.dat").read_bytes() == b"\xff\xfe waddles.core.example.count"
    snap = (repo / "a/x.yaml").read_bytes()
    assert am.main(base + ["--paths", "a/**"]) == 0
    assert (repo / "a/x.yaml").read_bytes() == snap  # idempotent: no diff
    assert am.main(base + ["--paths", "b/**"]) == 0
    assert am.main(base + ["--check"]) == 0


def test_cli_zero_files_fails_loud_and_write_requires_paths(repo: Path):
    mp = _mapping_file(repo)
    base = ["--root", str(repo), "--mapping", str(mp)]
    assert am.main(base + ["--check", "--paths", "nomatch/**"]) == 2
    with pytest.raises(SystemExit):
        am.main(base)
