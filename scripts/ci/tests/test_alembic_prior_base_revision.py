"""Unit tests for scripts/ci/alembic_prior_base_revision.py.

Regression coverage for the pr-validation.yml `alembic-migration-chain` job
(gh-526): a PR that touches only `alembic/tests/**` test helpers -- and so
adds no new `alembic/versions/*.py` revision -- must resolve to exit code 2
("no new revisions", a benign signal the CI step uses to skip the
downgrade/reapply round-trip), never conflated with exit code 1 (a real
error: new revisions that don't form a single linear stack). Before this
fix both cases returned exit 1, so the whole job failed for a change with
no migration content.

Exercises `resolve_prior_base_revision` directly against constructed
`revision -> down_revision` maps -- no git/subprocess/Postgres needed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).parent.parent / "alembic_prior_base_revision.py"


def _load_module() -> Any:
    """Loads the CI script as an importable module for unit tests."""
    spec = importlib.util.spec_from_file_location("alembic_prior_base_revision", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def test_no_new_revisions_is_exit_code_2_not_1() -> None:
    """An unchanged revision set (e.g. a test-helper-only PR) is exit 2 --
    the CI step must be able to tell "nothing new" apart from a real error."""
    revisions = {"0025_app_source_bindings": "0024_x"}
    exit_code, output = mod.resolve_prior_base_revision(dict(revisions), dict(revisions))
    assert exit_code == 2
    assert "no new" in output


def test_empty_base_and_head_is_also_exit_code_2() -> None:
    """No revisions at all on either side is still the benign "nothing new"
    case, not an error -- e.g. a PR before any alembic/versions/ exists."""
    exit_code, output = mod.resolve_prior_base_revision({}, {})
    assert exit_code == 2
    assert "no new" in output


def test_single_new_revision_resolves_to_its_down_revision() -> None:
    """One new revision stacked on existing history resolves to that
    revision's down_revision as the prior-base entry point."""
    base = {"0024_x": "0023_y"}
    head = {"0024_x": "0023_y", "0025_new": "0024_x"}
    exit_code, output = mod.resolve_prior_base_revision(base, head)
    assert exit_code == 0
    assert output == "0024_x"


def test_linear_stack_of_new_revisions_resolves_to_one_entry_point() -> None:
    """Multiple new revisions stacked linearly (a rebased stacked PR) still
    resolve to the single entry point back into pre-existing history."""
    base = {"0024_x": "0023_y"}
    head = {
        "0024_x": "0023_y",
        "0025_a": "0024_x",
        "0026_b": "0025_a",
    }
    exit_code, output = mod.resolve_prior_base_revision(base, head)
    assert exit_code == 0
    assert output == "0024_x"


def test_non_linear_new_revisions_is_a_real_error_exit_1() -> None:
    """Two new revisions branching off different existing bases is
    ambiguous -- a real error (exit 1), never the benign "no new
    revisions" case."""
    base = {"0024_x": "0023_y"}
    head = {
        "0024_x": "0023_y",
        "0025_a": "0024_x",
        "0025_b": "0023_y",
    }
    exit_code, output = mod.resolve_prior_base_revision(base, head)
    assert exit_code == 1
    assert "do not form a single linear stack" in output


def test_new_root_revision_with_no_down_revision_is_a_real_error() -> None:
    """A new revision with down_revision=None can't resolve to a prior-base
    entry point into pre-existing history -- a real error, not benign."""
    base: dict[str, str | None] = {}
    head: dict[str, str | None] = {"0001_root": None}
    exit_code, _output = mod.resolve_prior_base_revision(base, head)
    assert exit_code == 1


def test_parse_revision_extracts_revision_and_down_revision() -> None:
    """Sanity check on the regex-based source parser the CLI path relies on."""
    source = '\nrevision = "0026_new"\ndown_revision = "0025_app_source_bindings"\n'
    revision, down_revision = mod.parse_revision(source)
    assert revision == "0026_new"
    assert down_revision == "0025_app_source_bindings"


def test_parse_revision_handles_root_revision_with_no_down_revision() -> None:
    """A root migration file has no `down_revision` assignment at all."""
    source = '\nrevision = "0001_root"\n'
    revision, down_revision = mod.parse_revision(source)
    assert revision == "0001_root"
    assert down_revision is None


def test_parse_revision_raises_without_revision_assignment() -> None:
    """A malformed migration file missing `revision = ...` must fail loudly,
    not silently resolve to a false revision id."""
    with pytest.raises(ValueError):
        mod.parse_revision("# no revision here\n")


def test_parse_revision_handles_merge_tuple_down_revision() -> None:
    """A merge revision's `down_revision = (a, b)` parses to both parents."""
    source = 'revision = "0110_m"\ndown_revision = ("0108_a", "0109_b")\n'
    assert mod.parse_revision(source) == ("0110_m", ("0108_a", "0109_b"))


def test_multi_head_base_joined_by_new_merge_revision_resolves_all_parents() -> None:
    """Base has two heads; the PR's merge revision joins them. Both parents
    are the entry points (the old code saw `down_revision` as None -> exit 1)."""
    base = {"0107_x": None, "0108_a": "0107_x", "0109_b": "0107_x"}
    head = {**base, "0110_m": ("0108_a", "0109_b")}
    exit_code, output = mod.resolve_prior_base_revision(base, head)
    assert exit_code == 0
    assert output == "0108_a,0109_b"


def test_new_revision_stacked_on_a_new_merge_still_resolves() -> None:
    """Merge + a follow-up revision on top: one new head, two entry points."""
    base = {"0108_a": "0107_x", "0109_b": "0107_x"}
    head = {**base, "0110_m": ("0108_a", "0109_b"), "0111_n": "0110_m"}
    exit_code, output = mod.resolve_prior_base_revision(base, head)
    assert exit_code == 0
    assert output == "0108_a,0109_b"


def test_two_new_heads_without_merge_remain_a_real_error() -> None:
    """Fail-loud guard: independent new branches (no merge) are still fatal."""
    base = {"0108_a": "0107_x", "0109_b": "0107_x"}
    head = {**base, "0110_p": "0108_a", "0110_q": "0109_b"}
    exit_code, _output = mod.resolve_prior_base_revision(base, head)
    assert exit_code == 1


def test_merge_leaving_two_new_heads_is_a_real_error() -> None:
    """A merge plus another stray new head is not a single converged stack."""
    base = {"0108_a": "0107_x", "0109_b": "0107_x"}
    head = {**base, "0110_m": ("0108_a", "0109_b"), "0111_stray": "0108_a"}
    exit_code, _output = mod.resolve_prior_base_revision(base, head)
    assert exit_code == 1
