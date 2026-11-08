"""`author` / `collection` rules in `parse_bundle_manifest_v2` + every repo manifest parses."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml
from flask_core.bundle_author import canonical_author

from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2

_REPO = pathlib.Path(__file__).resolve().parents[2]

_BASE: dict[str, Any] = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music",
    "version": "1.0.0",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "author": "github:penguintechinc",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "bundles.social_music_process:transform",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        }
    },
}


def _parse(**overrides: Any) -> Any:
    raw = {**_BASE, **overrides}
    raw = {k: v for k, v in raw.items() if v is not _MISSING}
    return parse_bundle_manifest_v2(
        raw,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=False,
    )


_MISSING = object()


@pytest.mark.parametrize(
    ("author", "stored"),
    [
        ("github:penguintechinc", "github:penguintechinc"),
        ("@penguintechinc", "github:penguintechinc"),
        ("https://github.com/penguintechinc/", "github:penguintechinc"),
        ("Penguin Tech", "Penguin Tech"),
    ],
)
def test_author_is_normalized_to_canonical_form(author: str, stored: str) -> None:
    assert _parse(author=author).author == stored


@pytest.mark.parametrize(
    ("author", "reason"),
    [
        (_MISSING, "author_required"),
        (None, "author_required"),
        ("", "author_required"),
        ("<script>", "author_markup_rejected"),
        ("https://evil.example/x", "author_invalid_url"),
        ("github:-bad", "author_invalid_github_handle"),
        ("emoji \U0001f600", "author_invalid_name"),
        ("a\tb", "author_control_chars"),
    ],
)
@pytest.mark.parametrize("provider", ["builtin", "thirdparty"])
def test_author_required_and_validated_for_every_bundle(
    author: Any, reason: str, provider: str
) -> None:
    extra = {"license": "MIT"} if provider == "thirdparty" else {}
    with pytest.raises(ManifestV2Error) as exc:
        _parse(author=author, provider=provider, **extra)
    assert exc.value.reason == reason


def test_collection_slug_validated() -> None:
    assert _parse(collection="community-ports").collection == "community-ports"
    for bad in ("Upper", "a_b", "", 5):
        with pytest.raises(ManifestV2Error) as exc:
            _parse(collection=bad)
        assert exc.value.reason == "invalid_collection"


def test_superpenguintv_collection_requires_mit_and_notice() -> None:
    ok = _parse(collection="superpenguintv", license="MIT", notice="Ported; MIT License")
    assert ok.collection == "superpenguintv"
    for kw in (
        {"license": "Apache-2.0", "notice": "n"},
        {"notice": "n"},
        {"license": "MIT"},
        {"license": "MIT", "notice": "  "},
    ):
        with pytest.raises(ManifestV2Error) as exc:
            _parse(collection="superpenguintv", **kw)
        assert exc.value.reason == "collection_attribution_required"


def _repo_manifests() -> list[pathlib.Path]:
    return sorted((_REPO / "bundles").glob("*/*/hub-manifest.yaml"))


#: Pre-existing defect on the release base (NOT introduced by the author backfill):
#: bundles/rust/spectrum/hub-manifest.yaml is `kind: service` with no `stages:` block, which
#: `parse_bundle_manifest_v2` rejects (`missing_field`). Pinned here so the test stays honest --
#: it fails if this entry starts parsing (remove it) or any OTHER manifest stops parsing.
_KNOWN_UNPARSEABLE = {"rust/spectrum": "missing_field"}


def test_every_repo_hub_manifest_parses_with_a_valid_author() -> None:
    paths = _repo_manifests()
    assert len(paths) >= 80, f"only {len(paths)} manifests examined"
    failures: dict[str, str] = {}
    for path in paths:
        rel = f"{path.parent.parent.name}/{path.parent.name}"
        raw = yaml.safe_load(path.read_text())
        try:
            parse_bundle_manifest_v2(
                raw,
                known_custom_platforms=frozenset(),
                allow_wildcard_consumes=True,
                allow_prebuilt=True,
                trusted_source=True,
            )
        except ManifestV2Error as exc:
            failures[rel] = exc.reason
        assert canonical_author(raw["author"]) == raw["author"], f"{rel}: author not canonical"
    assert failures == _KNOWN_UNPARSEABLE


def test_first_party_authors_and_superpenguintv_designation() -> None:
    examined = 0
    for name in ("bundle.yaml", "hub-manifest.yaml"):
        for path in sorted((_REPO / "bundles").glob(f"*/*/{name}")):
            raw = yaml.safe_load(path.read_text())
            rel = f"{path.parent.parent.name}/{path.parent.name}"
            if rel == "csharp/superpenguin-roll":
                assert raw["author"] == "github:Psychoboy", path
                assert raw["collection"] == "superpenguintv", path
                assert raw["category"] == "alternatives", path
                assert raw["license"] == "MIT" and raw["notice"].strip(), path
            else:
                assert raw["author"] == "github:penguintechinc", path
                assert "collection" not in raw, path
            examined += 1
    assert examined >= 200, f"only {examined} manifests examined"


def test_bundle_yaml_and_hub_manifest_agree_on_author_collection_version() -> None:
    examined = 0
    for hub in _repo_manifests():
        bundle = hub.parent / "bundle.yaml"
        if not bundle.exists():
            continue
        h, b = yaml.safe_load(hub.read_text()), yaml.safe_load(bundle.read_text())
        for key in ("author", "collection", "version"):
            assert h.get(key) == b.get(key), f"{hub.parent}: {key} differs"
        examined += 1
    assert examined >= 80, f"only {examined} pairs examined"
