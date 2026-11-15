"""Regression gate: every `bundles/core-bundles.yaml` row must survive the core-bundle-seeder.

The `waddlebot-core-bundle-seeder` Helm post-upgrade hook exits non-zero (marking the release
failed) when ANY catalog row fails. These tests run the seeder's own per-entry validation
(`seed_one()`'s guard -> bundle.yaml/hub-manifest agreement -> `parse_bundle_manifest_v2`, with
the exact flags the Job passes) over EVERY row, and the WIT conformance gate over every world a
catalog row is built against, so a bad `author`, a malformed `app_id`, a seed-order dependency or
a validator/world mismatch fails CI instead of the alpha deploy.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- fixed argv, trusted CLI
from pathlib import Path
from typing import Any

import pytest
import yaml
from flask_core.bundle_author import canonical_author

from cli import seed_core_bundles as seeder
from services import bundle_component_validator as validator
from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2

_REPO = Path(__file__).resolve().parents[2]
_BUNDLES = _REPO / "bundles"
_CATALOG = _BUNDLES / "core-bundles.yaml"
_WIT_DIRS = (_REPO / "wit" / "waddle-bundle", _REPO / "wit" / "waddle-connector")


def _source_manifest(entry: seeder.CatalogEntry) -> dict[str, Any]:
    """The hub manifest the built image flattens to `entry.manifest_path` (from `source_dir`)."""
    raw = yaml.safe_load((_BUNDLES / entry.source_dir / "hub-manifest.yaml").read_text())
    assert isinstance(raw, dict), entry.app_id
    return raw


def test_every_core_catalog_row_passes_the_seeders_validation() -> None:
    """Run the seeder's per-entry validation over all rows; zero failures, denominator pinned."""
    raw_rows = yaml.safe_load(_CATALOG.read_text())["bundles"]
    entries = seeder.load_catalog(_CATALOG)
    assert len(entries) == len(raw_rows) >= 100, f"only {len(entries)} catalog rows examined"

    failures: dict[str, str] = {}
    for entry in entries:
        try:
            seeder._guard_core_namespace(entry.app_id)
            assert entry.source_dir, "catalog row has no source_dir"
            hub_raw = _source_manifest(entry)
            seeder._assert_manifest_files_agree(entry, _BUNDLES, hub_raw)
            manifest = parse_bundle_manifest_v2(
                hub_raw,
                known_custom_platforms=frozenset(),
                allow_wildcard_consumes=False,
                allow_prebuilt=True,
                trusted_source=True,
            )
            assert manifest.app_id == entry.app_id, f"manifest app_id {manifest.app_id!r}"
            assert manifest.version == entry.version, f"manifest version {manifest.version!r}"
            canonical_author(hub_raw.get("author"))
        except (seeder.CoreBundleSeederError, ManifestV2Error, AssertionError, ValueError) as exc:
            failures[entry.app_id] = f"{type(exc).__name__}: {exc}"
    print(f"examined={len(entries)} failures={len(failures)}")
    assert failures == {}


def test_every_core_bundle_source_manifest_has_a_valid_author() -> None:
    """`author` is present and valid in every catalog row's bundle.yaml AND hub-manifest."""
    examined = 0
    bad: dict[str, str] = {}
    for entry in seeder.load_catalog(_CATALOG):
        for name in ("hub-manifest.yaml", "bundle.yaml"):
            path = _BUNDLES / entry.source_dir / name
            if not path.is_file():
                continue
            examined += 1
            try:
                canonical_author((yaml.safe_load(path.read_text()) or {}).get("author"))
            except ValueError as exc:
                bad[f"{entry.app_id}/{name}"] = str(exc)
    assert examined >= 200, f"only {examined} manifests examined"
    assert bad == {}


def _flatten(entries: tuple[seeder.CatalogEntry, ...], dest: Path) -> Path:
    """Mimic `bundles/Dockerfile.core-bundles`: each hub manifest at its `manifest_path`."""
    for entry in entries:
        shutil.copyfile(
            _BUNDLES / entry.source_dir / "hub-manifest.yaml", dest / entry.manifest_path
        )
    return dest


def test_services_are_seeded_before_any_bundle_that_requests_their_tools(tmp_path: Path) -> None:
    """`order_services_first` guarantees every requested tool target is seeded first."""
    entries = seeder.load_catalog(_CATALOG)
    ordered = seeder.order_services_first(entries, _flatten(entries, tmp_path))
    assert sorted(e.app_id for e in ordered) == sorted(e.app_id for e in entries)
    position = {e.app_id: i for i, e in enumerate(ordered)}
    kinds = {e.app_id: _source_manifest(e).get("kind") for e in ordered}
    first_app = min(i for a, i in position.items() if kinds[a] != "service")
    assert all(position[a] < first_app for a, k in kinds.items() if k == "service")

    consumers = 0
    for entry in ordered:
        for req in _source_manifest(entry).get("requested_tools") or []:
            target = req["target_app_id"] if isinstance(req, dict) else req
            consumers += 1
            assert target in position, f"{entry.app_id} requests unknown tool target {target}"
            assert (
                position[target] < position[entry.app_id]
            ), f"{entry.app_id} would be seeded before its tool target {target}"
    assert consumers > 0, "no requested_tools examined -- the ordering check proved nothing"


def test_order_services_first_tolerates_an_unreadable_manifest(tmp_path: Path) -> None:
    """A missing manifest stays in place (seed_one fails that entry loudly) -- no abort."""
    entry = seeder.CatalogEntry(
        app_id="waddles.core.example.nope",
        version="1.0.0",
        language="rust",
        manifest_path="missing.yaml",
        artifact_path="x.wasm",
    )
    assert seeder.order_services_first((entry,), tmp_path) == (entry,)


def _wit_world_docs() -> dict[str, dict[str, Any]]:
    """`world name -> single-world `wasm-tools component wit --json` doc`, from the real WIT."""
    docs: dict[str, dict[str, Any]] = {}
    for wit_dir in _WIT_DIRS:
        out = subprocess.run(  # noqa: S603 # nosec B603
            [
                shutil.which("wasm-tools") or "wasm-tools",
                "component",
                "wit",
                "--json",
                str(wit_dir),
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        doc = json.loads(out.stdout)
        for world in doc["worlds"]:
            key = f"{wit_dir.name}:{world['name']}"
            docs[key] = {**doc, "worlds": [world]}
    return docs


@pytest.mark.skipif(shutil.which("wasm-tools") is None, reason="wasm-tools CLI not installed")
def test_conformance_gate_accepts_every_world_a_catalog_row_is_built_against() -> None:
    """The gate must accept the real export surface of each catalog `wit_world`.

    Regression: stage-tools / connector / connector-tools service bundles were all rejected
    (`published with no app_version_id`) because only `process-stage`+`action-stage` exports
    were allowed, which failed the core-bundle-seeder on dal2alpha.
    """
    worlds = _wit_world_docs()
    pairs: set[tuple[str, str]] = set()
    for entry, row in zip(
        seeder.load_catalog(_CATALOG), yaml.safe_load(_CATALOG.read_text())["bundles"], strict=True
    ):
        pairs.add((row.get("wit_world") or "stage", _source_manifest(entry).get("kind") or "app"))
    used = {w for w, _ in pairs}
    assert {"stage", "stage-tools", "connector", "connector-tools"} <= used
    rejected: dict[str, str | None] = {}
    for world_name, kind in sorted(pairs):
        key = (
            f"waddle-connector:{world_name}"
            if world_name.startswith("connector")
            else f"waddle-bundle:{world_name}"
        )
        assert key in worlds, f"catalog wit_world {world_name!r} has no WIT definition"
        result = validator._validate_document(worlds[key], kind)
        if not result.ok:
            rejected[f"{world_name}/{kind}"] = result.reason
    assert rejected == {}


def test_conformance_gate_still_rejects_a_mixed_or_unknown_export_surface() -> None:
    """Export sets are matched exactly: a service that also exports a stage is not conformant."""
    packages = [{"name": "waddle:bundle@1.0.0"}]
    interfaces = [
        {"name": "tool-provider", "package": 0},
        {"name": "process-stage", "package": 0},
        {"name": "action-stage", "package": 0},
    ]

    def doc(exports: dict[str, Any]) -> dict[str, Any]:
        return {
            "worlds": [{"name": "root", "imports": {}, "exports": exports, "package": 0}],
            "interfaces": interfaces,
            "types": [],
            "packages": packages,
        }

    ref = lambda i: {"interface": {"id": i}}  # noqa: E731
    assert validator._validate_document(doc({"a": ref(0)}), "service").ok is True
    mixed = validator._validate_document(doc({"a": ref(0), "b": ref(1), "c": ref(2)}))
    assert mixed.ok is False and mixed.reason and "stage_export_mismatch" in mixed.reason
    only_one_stage = validator._validate_document(doc({"a": ref(1)}))
    assert only_one_stage.ok is False


_PKGS = [
    {"name": "waddle:bundle@1.0.0"},
    {"name": "waddle:connector@1.0.0"},
    {"name": "waddle:connector-evil@1.0.0"},
    {"name": "waddle:bundlex@1.0.0"},
]
_IFACES = [
    {"name": "process-stage", "package": 0},  # 0
    {"name": "action-stage", "package": 0},  # 1
    {"name": "tool-provider", "package": 0},  # 2
    {"name": "receiver", "package": 1},  # 3
    {"name": "sender", "package": 1},  # 4
    {"name": "receiver", "package": 2},  # 5 lookalike namespace
    {"name": "process-stage", "package": 3},  # 6 lookalike namespace
    {"name": "streaming-lifecycle", "package": 0},  # 7
]


def _export_doc(*ids: int) -> dict[str, Any]:
    return {
        "worlds": [
            {
                "name": "root",
                "imports": {},
                "exports": {f"e{i}": {"interface": {"id": i}} for i in ids},
                "package": 0,
            }
        ],
        "interfaces": _IFACES,
        "types": [],
        "packages": _PKGS,
    }


@pytest.mark.parametrize(
    ("ids", "kind"),
    [
        ((0, 1), "app"),
        ((0, 1, 7), "app"),
        ((2,), "service"),
        ((3, 4), "service"),
        ((3, 4, 2), "service"),
    ],
)
def test_each_valid_kind_and_export_pairing_is_accepted(ids: tuple[int, ...], kind: str) -> None:
    assert validator._validate_document(_export_doc(*ids), kind).ok is True


@pytest.mark.parametrize(
    ("ids", "kind"),
    [
        ((2,), "app"),  # service (tool-provider) exports under an app manifest
        ((3, 4), "app"),  # connector exports under an app manifest
        ((3, 4, 2), "app"),
        ((0, 1), "service"),  # an app's stage pair under a service manifest
        ((0, 1, 7), "service"),
        ((0, 1), "bogus"),
    ],
)
def test_export_surface_must_match_the_manifest_declared_kind(
    ids: tuple[int, ...], kind: str
) -> None:
    result = validator._validate_document(_export_doc(*ids), kind)
    assert result.ok is False
    assert result.reason and (
        f"kind={kind}" in result.reason or "unknown_manifest_kind" in result.reason
    )


@pytest.mark.parametrize(
    ("ids", "kind"),
    [
        ((5, 4), "service"),  # waddle:connector-evil/receiver + real sender
        ((5, 3, 4), "service"),
        ((6, 1), "app"),  # waddle:bundlex/process-stage + real action-stage
    ],
)
def test_lookalike_namespaces_are_rejected_not_prefix_matched(
    ids: tuple[int, ...], kind: str
) -> None:
    """A namespace that merely STARTS WITH an allowed one is a different, disallowed package."""
    result = validator._validate_document(_export_doc(*ids), kind)
    assert result.ok is False
    assert result.reason and result.reason.startswith("disallowed_export")


def test_mismatch_reason_is_the_declared_kinds_nearest_set_and_deterministic() -> None:
    first = validator._validate_document(_export_doc(2), "app")
    again = validator._validate_document(_export_doc(2), "app")
    assert first.reason == again.reason
    assert first.reason == (
        "stage_export_mismatch: kind=app "
        "missing=['waddle:bundle/action-stage', 'waddle:bundle/process-stage'] "
        "extra=['waddle:bundle/tool-provider']"
    )
