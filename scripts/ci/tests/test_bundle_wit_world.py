"""Gate: every Python bundle is registered in the seeder catalog and built on the right WIT world.

Two failure modes this closes, both silent until a chatter hits the command:

1. **Unregistered bundle.** `bundles/core-bundles.yaml` is the seed data `hub_api/cli/
   seed_core_bundles.py` reads to publish/install/activate a bundle into the DB registry. A
   `bundles/python/<name>/` directory with no catalog row is built by nobody and activatable by
   nobody -- it just never exists at runtime.
2. **Wrong WIT world.** `bundles/build_python_bundles.py` compiles each row against `world stage`
   unless the row says `wit_world: stage-next`. `economy`, `ai`, `identity`, `reputation`,
   `overlay` and `streaming-lifecycle` exist only in `stage-next` (`wit/waddle-bundle/stage.wit`),
   so an economy/AI bundle built on `stage` compiles and publishes cleanly and then has no host
   import to call.

The build script already FAILS a row whose manifest needs stage-next but resolves to `stage`
(`resolve_wit_world`); this gate proves that on the REAL catalog + manifests (so the failure is in
CI, not in a Docker build on deploy day), pins the batch's 12 economy/AI rows, and proves the
resolved world is what actually reaches the `componentize-py` command line.

Denominators are asserted: a scan that examined too few bundles FAILS instead of passing quietly.

Run: python3 -m pytest scripts/ci/tests/test_bundle_wit_world.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLES_DIR = REPO_ROOT / "bundles" / "python"
CATALOG_PATH = REPO_ROOT / "bundles" / "core-bundles.yaml"
BUILD_SCRIPT = REPO_ROOT / "bundles" / "build_python_bundles.py"

#: Python bundle directories that intentionally have NO catalog row. An entry that gains a row (or
#: loses its directory) fails the test, so this list can only shrink.
UNREGISTERED_EXEMPT = {
    "chat": (
        "older BUILD-ONLY / INERT port of the community_chat_process monolith; its bundle.yaml "
        "states cut-over (activation) is a separate, later change"
    ),
}

#: The economy bundles (economy.* / identity.resolve) and AI bundles (ai.generate) of the hardened
#: batch -- the 12 that MUST be built against `stage-next`.
STAGE_NEXT_BATCH = frozenset(
    {
        "steal",
        "defuse",
        "jackpot",
        "top",
        "ask",
        "define",
        "eli5",
        "rephrase",
        "sentiment",
        "summarize",
        "tldr",
        "translate",
    }
)

#: The other 25 bundles of the batch: no stage-next capability, so they stay on the default world.
STAGE_BATCH = frozenset(
    {
        "affirmation",
        "clap",
        "commands",
        "dance",
        "emojify",
        "fact",
        "horoscope",
        "insult",
        "kiss",
        "mock",
        "motivate",
        "numberfact",
        "pat",
        "pickup",
        "poke",
        "rate",
        "reverse",
        "riddle",
        "say",
        "truthordare",
        "uwu",
        "vibecheck",
        "wordcount",
        "wouldyourather",
        "yomama",
    }
)

#: One permission id per stage-next-only host family (stage.wit `economy`/`ai`/`identity`/
#: `reputation`/`overlay`/`streaming-lifecycle`).
STAGE_NEXT_ONLY_PERMISSIONS = [
    "ai.generate",
    "economy.read",
    "economy.wager",
    "economy.transfer",
    "identity.resolve",
    "reputation.read",
    "reputation.community.write",
    "overlay.media",
    "streaming.lifecycle.subscribe",
]


def _load_build_script() -> ModuleType:
    """Import `bundles/build_python_bundles.py` by path (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("build_python_bundles_under_test", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bpb = _load_build_script()


def _python_rows() -> list[dict[str, Any]]:
    """Every `language: python` catalog row, in file order."""
    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    return [row for row in raw["bundles"] if row.get("language") == "python"]


def _name(row: dict[str, Any]) -> str:
    """The bundle directory name a row builds (`python/<name>` -> `<name>`)."""
    return str(row["source_dir"]).split("/", 1)[1]


def _manifest(row: dict[str, Any]) -> dict[str, Any]:
    """The parsed `hub-manifest.yaml` of a catalog row's bundle."""
    path = REPO_ROOT / "bundles" / row["source_dir"] / "hub-manifest.yaml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} did not parse to a mapping"
    return loaded


def test_scan_denominators_are_non_trivial() -> None:
    """A scan that found almost nothing must FAIL, not pass (zero examined is a failure)."""
    rows = _python_rows()
    dirs = [p for p in BUNDLES_DIR.iterdir() if (p / "hub-manifest.yaml").is_file()]
    assert len(rows) >= 100, f"only {len(rows)} python catalog rows scanned"
    assert len(dirs) >= 100, f"only {len(dirs)} bundle directories scanned"
    assert len(STAGE_NEXT_BATCH) == 12 and len(STAGE_BATCH) == 25
    assert not STAGE_NEXT_BATCH & STAGE_BATCH


def test_every_bundle_directory_is_registered_in_the_catalog() -> None:
    """No `bundles/python/<name>` (with a hub-manifest) may be missing from the seeder catalog."""
    registered = {_name(row) for row in _python_rows()}
    on_disk = {p.name for p in BUNDLES_DIR.iterdir() if (p / "hub-manifest.yaml").is_file()}
    unregistered = on_disk - registered
    assert unregistered == set(UNREGISTERED_EXEMPT), (
        f"unregistered bundle dirs {sorted(unregistered)} != exempt "
        f"{sorted(UNREGISTERED_EXEMPT)} -- add a bundles/core-bundles.yaml row (or fix the exempt "
        "list if an exemption is stale)"
    )
    dangling = registered - on_disk
    assert not dangling, f"catalog rows whose bundle directory/manifest is missing: {dangling}"


def test_batch_of_37_is_registered() -> None:
    """The hardened batch (12 stage-next + 25 stage) is fully present in the catalog."""
    registered = {_name(row) for row in _python_rows()}
    missing = (STAGE_NEXT_BATCH | STAGE_BATCH) - registered
    assert not missing, f"batch bundles missing from bundles/core-bundles.yaml: {sorted(missing)}"
    assert len(STAGE_NEXT_BATCH | STAGE_BATCH) == 37


def test_catalog_rows_agree_with_their_manifests_and_are_unique() -> None:
    """The seeder raises at deploy on a version/app_id mismatch; fail here instead."""
    rows = _python_rows()
    for row in rows:
        manifest = _manifest(row)
        assert manifest["app_id"] == row["app_id"], f"{row['app_id']}: manifest app_id differs"
        assert str(manifest["version"]) == str(row["version"]), (
            f"{row['app_id']}: catalog version {row['version']!r} != "
            f"hub-manifest version {manifest['version']!r} (seeder raises on this)"
        )
    for field in ("app_id", "manifest_path", "artifact_path", "source_dir"):
        values = [row[field] for row in rows]
        dupes = sorted({v for v in values if values.count(v) > 1})
        assert not dupes, f"duplicate catalog {field}: {dupes} (two rows would overwrite)"


def test_every_row_resolves_to_exactly_the_world_its_permissions_need() -> None:
    """Real catalog x real manifests: the declared world equals the required world, both ways."""
    rows = _python_rows()
    stage_next_rows = 0
    for row in rows:
        perms = bpb._permission_ids(_manifest(row))
        resolved = bpb.resolve_wit_world(row["app_id"], row.get("wit_world"), perms)
        assert resolved == bpb.required_wit_world(perms), (
            f"{row['app_id']}: resolves to {resolved!r} but its permissions need "
            f"{bpb.required_wit_world(perms)!r}"
        )
        stage_next_rows += resolved == "stage-next"
    assert stage_next_rows >= len(STAGE_NEXT_BATCH), "fewer stage-next rows than the batch alone"


def test_the_twelve_economy_and_ai_bundles_build_on_stage_next() -> None:
    """steal/defuse/jackpot/top + the 8 AI bundles say `stage-next`; the other 25 stay default."""
    by_name = {_name(row): row for row in _python_rows()}
    for name in sorted(STAGE_NEXT_BATCH):
        assert by_name[name].get("wit_world") == "stage-next", f"{name}: missing wit_world"
    for name in sorted(STAGE_BATCH):
        assert by_name[name].get("wit_world") in (None, "stage"), f"{name}: must stay on stage"


@pytest.mark.parametrize("permission_id", STAGE_NEXT_ONLY_PERMISSIONS)
@pytest.mark.parametrize("declared", [None, "stage"])
def test_stage_next_permission_on_stage_world_fails_the_build(
    permission_id: str, declared: str | None
) -> None:
    """The silent-degrade shape: a stage-next-only permission with a `stage` world must raise."""
    with pytest.raises(bpb.BuildError, match="stage-next"):
        bpb.resolve_wit_world("waddles.core.example.x", declared, ["storage.kv", permission_id])
    assert (
        bpb.resolve_wit_world("waddles.core.example.x", "stage-next", [permission_id])
        == "stage-next"
    )


@pytest.mark.parametrize(
    "permissions",
    [
        [],
        ["storage.kv"],
        ["chat.send:discord", "chat.send:twitch"],
        ["flags.read", "platform.clock"],
    ],
)
def test_plain_permissions_resolve_to_the_default_world(permissions: list[str]) -> None:
    """A bundle with no stage-next capability stays on `stage` (declared or by default)."""
    assert bpb.resolve_wit_world("waddles.core.example.x", None, permissions) == "stage"
    assert bpb.resolve_wit_world("waddles.core.example.x", "stage", permissions) == "stage"


@pytest.mark.parametrize("declared", ["stage-v2", "connector", "", "Stage", 7, ["stage-next"]])
def test_unknown_world_is_rejected(declared: object) -> None:
    """A typo or an unwired world name fails loud rather than reaching componentize-py."""
    with pytest.raises(bpb.BuildError, match="wit_world must be one of"):
        bpb.resolve_wit_world("waddles.core.example.x", declared, [])


def _tmp_repo(tmp_path: Path, permissions: list[dict[str, str]]) -> None:
    """Lay out a minimal fake repo for `_build_one` and point the build module at it."""
    bundle = tmp_path / "bundles" / "python" / "x"
    (bundle / "src").mkdir(parents=True)
    (bundle / "hub-manifest.yaml").write_text(
        yaml.safe_dump({"app_id": "waddles.core.example.x", "permissions": permissions}),
        encoding="utf-8",
    )
    for sub in ("wit/waddle-bundle", "sdk/waddle-sdk/src"):
        (tmp_path / sub).mkdir(parents=True)


@pytest.mark.parametrize(
    ("wit_world", "permissions", "expected"),
    [
        (None, [{"id": "storage.kv"}], "stage"),
        ("stage-next", [{"id": "economy.read"}], "stage-next"),
        ("stage-next", [{"id": "ai.generate"}], "stage-next"),
    ],
)
def test_resolved_world_reaches_the_componentize_py_command_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wit_world: str | None,
    permissions: list[dict[str, str]],
    expected: str,
) -> None:
    """The `-w` argument is the resolved world -- not a hard-coded constant."""
    _tmp_repo(tmp_path, permissions)
    monkeypatch.setattr(bpb, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bpb, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setattr(bpb, "WIT_WORLD_DIR", tmp_path / "wit" / "waddle-bundle")
    monkeypatch.setattr(bpb, "SDK_SRC", tmp_path / "sdk" / "waddle-sdk" / "src")
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bpb.subprocess, "run", fake_run)
    row: dict[str, Any] = {
        "app_id": "waddles.core.example.x",
        "source_dir": "python/x",
        "manifest_path": "x.manifest.yaml",
        "artifact_path": "x.wasm",
    }
    if wit_world is not None:
        row["wit_world"] = wit_world

    assert bpb._build_one(row) == "waddles.core.example.x"
    assert len(seen) == 1
    cmd = seen[0]
    assert cmd[cmd.index("-w") + 1] == expected
    assert (tmp_path / "dist" / "x.manifest.yaml").is_file()


def test_build_one_refuses_stage_next_permission_without_the_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through `_build_one`: no componentize-py call is ever made for the bad row."""
    _tmp_repo(tmp_path, [{"id": "economy.wager"}])
    monkeypatch.setattr(bpb, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bpb, "DIST_DIR", tmp_path / "dist")

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("componentize-py must not run for a mis-declared world")

    monkeypatch.setattr(bpb.subprocess, "run", boom)
    row = {
        "app_id": "waddles.core.example.x",
        "source_dir": "python/x",
        "manifest_path": "x.manifest.yaml",
        "artifact_path": "x.wasm",
    }
    with pytest.raises(bpb.BuildError, match="economy.wager"):
        bpb._build_one(row)
