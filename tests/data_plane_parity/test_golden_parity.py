"""Golden-vector parity: the legacy Python ingest path vs the Rust oracle (gh #840 PR4).

Every corpus vector's `golden` is the Rust `normalize.rs` output (asserted by
`core/svc_ingest/tests/parity_golden.rs`), so a Python-vs-golden diff here IS a
Python-vs-Rust diff. Differences must be declared on the vector
(`python_divergences`) -- an undeclared difference or a stale declaration both
fail. Python-only platforms freeze the Python output as their golden until a
Rust/bundle path lands.

Run: `make test-data-plane-parity` (Rust oracle first, then this suite).
Re-bless Python-only goldens: `PARITY_BLESS=1 python3 -m pytest tests/data_plane_parity`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from data_plane_parity.harness import (
    DUAL_PATHS,
    GLOBAL_DIVERGENCE_SOURCE,
    REQUIRED_PLATFORMS,
    Vector,
    declared_divergences,
    load_corpus,
    observed_divergences,
    run_python_path,
    write_vector,
)

VECTORS = load_corpus()
BLESS = os.environ.get("PARITY_BLESS") == "1"


def _is_dual(vector: Vector) -> bool:
    return vector.data["path"] in DUAL_PATHS


class TestCorpusShape:
    """The corpus itself is well-formed and covers every platform slot."""

    def test_corpus_is_not_empty_and_ids_are_unique(self) -> None:
        ids = [v.id for v in VECTORS]
        assert len(ids) >= 30, f"only {len(ids)} vectors -- corpus shrank"
        assert len(ids) == len(set(ids))

    def test_every_platform_slot_has_a_directory_and_vector(self) -> None:
        assert {v.data["platform"] for v in VECTORS} >= REQUIRED_PLATFORMS

    def test_dual_path_platforms_cover_all_three_paths(self) -> None:
        assert {v.data["path"] for v in VECTORS if _is_dual(v)} == set(DUAL_PATHS)

    @pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v.id)
    def test_vector_is_well_formed(self, vector: Vector) -> None:
        data = vector.data
        for key in (
            "id",
            "platform",
            "path",
            "seed",
            "frame",
            "context",
            "outcome",
            "golden_origin",
        ):
            assert key in data, f"{vector.id}: missing {key!r}"
        assert vector.id == f"{vector.path.parent.name}/{vector.path.stem}"
        assert data["outcome"] in {"event", "dropped"}
        if _is_dual(vector):
            assert data["golden_origin"] == "rust"
            assert "rust_path" not in data
        else:
            # Stub slot: Python-only today -- must say so explicitly.
            assert data["golden_origin"] == "python"
            assert data.get("rust_path", "<missing>") is None
        if data["outcome"] == "event":
            assert data["golden"] is not None, f"{vector.id}: golden missing (bless it)"
            assert "occurred_at" not in data["golden"]
        else:
            assert data["golden"] is None
        pii = data["pii_classifier"]
        assert pii == "pass" or pii.startswith("reject:"), (
            f"{vector.id}: bad pii_classifier {pii!r}"
        )
        for div in data.get("python_divergences", []):
            assert {"path", "python", "reason", "tracking"} <= set(div)


class TestPythonMatchesGolden:
    """Python path vs golden, modulo exactly the declared divergences."""

    @pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v.id)
    async def test_python_path_matches_golden_modulo_declared_divergences(
        self, vector: Vector
    ) -> None:
        result = await run_python_path(vector)

        if BLESS and vector.data["golden_origin"] == "python" and result.outcome == "event":
            vector.data["golden"] = result.event
            write_vector(vector)

        if result.outcome == "event":
            assert result.occurred_at is not None
            parsed = datetime.fromisoformat(result.occurred_at.replace("Z", "+00:00"))
            assert parsed.tzinfo is not None, "occurred_at must be timezone-aware"
            if _is_dual(vector):
                # Neither path gets a platform timestamp for these frames: both stamp arrival time.
                assert abs(datetime.now(UTC) - parsed) < timedelta(minutes=5), (
                    "occurred_at is not 'now'"
                )

        observed = {d.path: d.python for d in observed_divergences(vector, result)}
        declared = declared_divergences(vector)

        undeclared = {p: v for p, v in observed.items() if p not in declared}
        wrong_value = {
            p: (observed[p], declared[p])
            for p in observed
            if p in declared and observed[p] != declared[p]
        }
        stale = sorted(p for p in declared if p not in observed)
        assert not undeclared, f"{vector.id}: UNDECLARED Python-vs-Rust divergence {undeclared}"
        assert not wrong_value, (
            f"{vector.id}: divergence value drifted (observed, declared) {wrong_value}"
        )
        assert not stale, (
            f"{vector.id}: stale declared divergence {stale} no longer occurs -- remove it"
        )

    @pytest.mark.parametrize(
        "vector",
        [v for v in VECTORS if _is_dual(v) and v.data["outcome"] == "event"],
        ids=lambda v: v.id,
    )
    def test_rust_event_always_carries_source_python_never_does(self, vector: Vector) -> None:
        """`source` is the one global divergence: Rust always sets it, Python has no such field."""
        assert vector.data["golden"]["source"] is not None, GLOBAL_DIVERGENCE_SOURCE
        assert vector.data["golden"]["source"]["platform"] == vector.data["platform"]


def test_divergence_report(capsys: pytest.CaptureFixture[str]) -> None:
    """Print the denominators + every declared divergence (the real findings)."""
    dual = [v for v in VECTORS if _is_dual(v)]
    python_only = [v for v in VECTORS if not _is_dual(v)]
    declared = [(v.id, d) for v in dual for d in v.data.get("python_divergences", [])]
    assert dual and python_only, "corpus must contain both dual-path vectors and python-only slots"
    grouped: dict[tuple[str, str], list[str]] = {}
    for vid, d in declared:
        grouped.setdefault((d["path"], d["reason"]), []).append(vid)
    with capsys.disabled():
        print(
            f"\n[parity] vectors={len(VECTORS)} dual-path={len(dual)} python-only-slots={len(python_only)}"
        )
        print(f"[parity] platforms={sorted({v.data['platform'] for v in VECTORS})}")
        print(
            f"[parity] Python-vs-Rust divergences: {len(grouped)} distinct ({len(declared)} vector instances) + global `source`"
        )
        for (path, reason), vids in sorted(grouped.items()):
            print(f"[parity]   {path} x{len(vids)} -- {reason}")
