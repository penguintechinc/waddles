"""Tests for scripts/ci/gen-connector-parity-expected.py (connector parity harness)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "ci" / "gen-connector-parity-expected.py"

_spec = importlib.util.spec_from_file_location("gen_expected", SCRIPT)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, check=False)


def _corpus_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "corpus"
    for p in ("twitch", "discord", "youtube"):
        shutil.copytree(gen.CORPUS / p, dst / p)
    return dst


def test_committed_expected_files_are_current() -> None:
    r = _run("--check")
    assert r.returncode == 0, r.stderr
    assert "examined=34" in r.stdout


def test_pseudonym_matches_rust_known_value() -> None:
    # Cross-checked by core/bundle_executor/tests/connector_parity.rs (same input -> same uuid).
    exp = json.loads((gen.OUT / "twitch" / "irc_full_tag_set.json").read_text())
    assert exp["events"][0]["actor"]["uuid"] == gen.pseudonym_uuid("twitch", "999")
    assert exp["events"][0]["actor"]["raw_handle"] == "someuser"


def test_check_fails_on_stale_missing_and_orphaned(tmp_path: Path) -> None:
    corpus, out = _corpus_copy(tmp_path), tmp_path / "expected"
    assert _run("--corpus", str(corpus), "--out", str(out)).returncode == 0
    assert _run("--check", "--corpus", str(corpus), "--out", str(out)).returncode == 0

    f = out / "twitch" / "irc_full_tag_set.json"
    f.write_text(f.read_text().replace("hello chat", "tampered"))
    r = _run("--check", "--corpus", str(corpus), "--out", str(out))
    assert r.returncode == 1 and "stale    twitch/irc_full_tag_set.json" in r.stderr

    f.unlink()
    r = _run("--check", "--corpus", str(corpus), "--out", str(out))
    assert r.returncode == 1 and "missing  twitch/irc_full_tag_set.json" in r.stderr

    (out / "twitch" / "zz_orphan.json").write_text("{}")
    r = _run("--check", "--corpus", str(corpus), "--out", str(out))
    assert "orphaned twitch/zz_orphan.json" in r.stderr


def test_golden_change_makes_expected_stale(tmp_path: Path) -> None:
    corpus, out = _corpus_copy(tmp_path), tmp_path / "expected"
    _run("--corpus", str(corpus), "--out", str(out))
    vec = corpus / "discord" / "guild_text_message.json"
    d = json.loads(vec.read_text())
    d["golden"]["payload"]["is_mod"] = True
    vec.write_text(json.dumps(d))
    r = _run("--check", "--corpus", str(corpus), "--out", str(out))
    assert r.returncode == 1 and "stale    discord/guild_text_message.json" in r.stderr


def test_zero_corpus_vectors_is_a_failure(tmp_path: Path) -> None:
    empty = tmp_path / "corpus"
    empty.mkdir()
    r = _run("--check", "--corpus", str(empty), "--out", str(tmp_path / "o"))
    assert r.returncode == 1 and "zero corpus vectors" in r.stderr
