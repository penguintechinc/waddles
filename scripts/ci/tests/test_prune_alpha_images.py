"""Tests for scripts/ci/prune_alpha_images.py: dry-run default, keep-N, deployed protection."""

import json
import pathlib
import sys
import urllib.error
from email.message import Message
from typing import Any

import pytest

CI = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))
import changed_images as ci
import prune_alpha_images as pr

IMAP = ci.load_map(CI / "image-inputs.yaml")


def ver(i: int, *tags: str) -> dict[str, Any]:
    return {"id": i, "metadata": {"container": {"tags": list(tags)}}}


class FakeApi:
    """Dict-backed GitHub packages API; records deletes."""

    def __init__(self, packages: dict[str, list[dict[str, Any]]]) -> None:
        self.packages = packages  # name -> versions
        self.deleted: list[str] = []

    def get_pages(self, path: str) -> list[dict[str, Any]]:
        if path.endswith("/versions"):
            name = (
                path.split("/container/")[1]
                .rsplit("/versions", 1)[0]
                .replace("%2F", "/")
            )
            if name not in self.packages:
                raise urllib.error.HTTPError("u", 404, "Not Found", Message(), None)
            return self.packages[name]
        return [{"name": n} for n in self.packages]

    def delete(self, path: str) -> None:
        self.deleted.append(path)


def epochs(n: int) -> list[dict[str, Any]]:
    return [ver(i, f"alpha-{1_000_000_000 + i}") for i in range(n)]


def test_keeps_newest_n_and_deletes_rest() -> None:
    vs = epochs(15)
    r = pr.plan_image_package("waddles/hub-api", vs, 10, set())
    assert len(r.delete) == 5 and len(r.keep) == 10
    assert {d[1] for d in r.delete} == {0, 1, 2, 3, 4}


def test_deployed_tag_is_never_deleted_even_if_old() -> None:
    vs = epochs(15)
    r = pr.plan_image_package("waddles/hub-api", vs, 10, {"alpha-1000000000"})
    assert 0 not in {d[1] for d in r.delete} and len(r.delete) == 4


def test_untagged_and_foreign_tags_are_never_touched() -> None:
    vs = epochs(12) + [
        ver(100),
        ver(101, "latest"),
        ver(102, "beta-1700000000"),
        ver(103, "alpha-1000000000", "latest"),
    ]
    r = pr.plan_image_package("p", vs, 10, set())
    ids = {d[1] for d in r.delete}
    assert not ids & {100, 101, 102, 103}
    assert r.skipped_untagged == 1


def test_variants_share_an_epoch() -> None:
    vs = [
        ver(1, "alpha-1000000001-cpu"),
        ver(2, "alpha-1000000001"),
        ver(3, "alpha-1000000000"),
    ]
    r = pr.plan_image_package("p", vs, 1, set())
    assert {d[1] for d in r.delete} == {3}


def test_cache_package_keeps_only_alpha_ref() -> None:
    r = pr.plan_cache_package(
        "waddles/buildcache/hub-api",
        "hub-api",
        set(IMAP.images),
        [ver(1, "alpha"), ver(2, "run-123"), ver(3, "alpha-9")],
    )
    assert {d[1] for d in r.delete} == {2, 3} and r.stale_packages == []


def test_cache_package_for_unknown_image_is_stale_entirely() -> None:
    r = pr.plan_cache_package(
        "waddles/buildcache/gone", "gone", set(IMAP.images), [ver(1, "alpha")]
    )
    assert r.stale_packages == ["waddles/buildcache/gone"] and len(r.delete) == 1


def _api() -> FakeApi:
    pk = {f"waddles/{n}": epochs(12) for n in IMAP.images}
    pk["waddles/buildcache/hub-api"] = [ver(900, "alpha"), ver(901, "old-run")]
    pk["waddles/buildcache/removed-svc"] = [ver(950, "alpha")]
    return FakeApi(pk)


def test_dry_run_never_deletes() -> None:
    api = _api()
    out: list[str] = []
    assert (
        pr.run(api, "o", "waddles", IMAP, 10, set(), apply=False, out=out.append) == 0
    )
    assert api.deleted == []
    assert any("mode=DRY-RUN" in line for line in out)
    assert any(
        "would delete waddles/buildcache/hub-api version=901" in line for line in out
    )


def test_apply_deletes_exactly_the_planned_versions() -> None:
    api = _api()
    pr.run(api, "o", "waddles", IMAP, 10, set(), apply=True, out=lambda _s: None)
    assert len(api.deleted) == 11 * 2 + 2  # 2 old epochs/image + 901 + removed-svc 950
    assert all("/versions/" in d for d in api.deleted)


def test_zero_examined_is_a_failure() -> None:
    assert (
        pr.run(FakeApi({}), "o", "waddles", IMAP, 10, set(), False, out=lambda _s: None)
        == 1
    )


def test_apply_without_deployed_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")
    assert pr.main(["--apply"]) == 2
    assert "requires --deployed" in capsys.readouterr().err


def test_main_requires_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert pr.main([]) == 2


def test_deployed_tags_from_plan_and_values(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"helm_tags": {"a": "alpha-1", "b": "alpha-2"}}))
    assert pr.deployed_tags_from(str(f)) == {"alpha-1", "alpha-2"}
    f.write_text(
        json.dumps({"global": {"imageTag": "alpha-3", "imageTags": {"x": "alpha-4"}}})
    )
    assert pr.deployed_tags_from(str(f)) == {"alpha-3", "alpha-4"}
    assert pr.deployed_tags_from(None) is None
    assert "alpha-4-cpu" in pr.expand_variants({"alpha-4"}, IMAP)
