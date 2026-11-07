"""Unit tests for 0082's pure author normalization (no database needed)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parents[1]
    / "versions"
    / "0082_bundle_author_collection.py"
)
_spec = importlib.util.spec_from_file_location("mig0082", _PATH)
assert _spec is not None and _spec.loader is not None
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        (None, None),
        ("github:octo", "github:octo"),
        ("@octo-cat", "github:octo-cat"),
        ("https://github.com/octo/", "github:octo"),
        ("Acme Corp", "Acme Corp"),
        ("superpenguintv (Psychoboy)", "superpenguintv Psychoboy"),
        ("PenguinTech/waddles", "PenguinTech waddles"),
        ("<b>Evil</b>", "b Evil b"),
        ("@-bad", "bad"),
        ("\U0001f600\U0001f600", None),
        ("", None),
        ("x" * 80, "x" * 64),
    ],
)
def test_normalize_author(legacy: str | None, expected: str | None) -> None:
    assert _mig.normalize_author(legacy) == expected


def test_migration_chains_off_the_prior_head() -> None:
    assert _mig.down_revision == "0081_spectrum_conn_resource"
    assert len(_mig.revision) <= 32
