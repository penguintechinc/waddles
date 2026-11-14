"""Tests for scripts/ci/alpha_plan.py (plan resolution, lanes, retag rows).

The registry is a dict-backed fake here (logic coverage); the real-registry path
(`docker buildx imagetools` retag + digest verification) is exercised by
scripts/ci/tests/alpha_registry_e2e.sh against a throwaway local registry.
"""

import io
import json
import pathlib
import sys
from collections.abc import Callable, Collection
from typing import Any

import pytest

CI = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))
import alpha_plan as ap
import changed_images as ci

IMAP = ci.load_map(CI / "image-inputs.yaml")
NAMES = sorted(IMAP.images)
REG = "ghcr.io/penguintechinc/waddles"
OLD_SHA = "1" * 40
HEAD = "2" * 40
NEW_TAG = "alpha-2000000000"
OLD_TAG = "alpha-1000000000"


def values(
    tags: dict[str, str] | None = None, default: str = OLD_TAG
) -> dict[str, Any]:
    g: dict[str, Any] = {"imageTag": default}
    if tags is not None:
        g["imageTags"] = tags
    return {"global": g}


def fake_registry(
    missing: Collection[str] = (), unlabeled: Collection[str] = ()
) -> Callable[[str], ap.ImageInfo | None]:
    """ref -> ImageInfo for every image at OLD_TAG, minus `missing`."""

    def inspect(ref: str) -> ap.ImageInfo | None:
        for n in NAMES:
            sfx = IMAP.images[n].tag_suffix
            if ref == f"{REG}/{n}:{OLD_TAG}{sfx}" and n not in missing:
                return ap.ImageInfo(
                    f"sha256:{n:0<64}"[:71], None if n in unlabeled else OLD_SHA
                )
        return None

    return inspect


def run(
    paths: list[str],
    vals: dict[str, Any] | None = None,
    force: bool = False,
    lanes: dict[str, str] | None = None,
    probe: Callable[[str], bool] = lambda _u: True,
    **reg: Collection[str],
) -> dict[str, Any]:
    return ap.build_plan(
        IMAP,
        HEAD,
        NEW_TAG,
        REG,
        values() if vals is None else vals,
        force,
        lanes or {"a": "tcp://a:1234", "b": ""},
        lambda _b, _h: list(paths),
        inspect=fake_registry(**reg),
        probe=probe,
    )


def test_docs_only_reuses_everything_and_pins_old_tags() -> None:
    p = run(["docs/x.md"])
    assert p["build"] == [] and p["examined"] == 11 and len(p["reuse"]) == 11
    assert set(p["helm_tags"].values()) == {OLD_TAG}
    assert len(p["retag"]) == 11
    assert p["matrix"] == {"include": []}


def test_one_changed_image_gets_new_tag_rest_keep_old() -> None:
    p = run(["core/svc_streaming/src/main.rs"])
    assert p["build"] == ["svc-streaming"]
    assert p["helm_tags"]["svc-streaming"] == NEW_TAG
    assert all(v == OLD_TAG for k, v in p["helm_tags"].items() if k != "svc-streaming")
    assert "svc-streaming" not in {r["service"] for r in p["retag"]}
    row = p["matrix"]["include"][0]
    assert row["suffix"] == "-cpu" and row["target"] == "cpu"


def test_retag_rows_use_suffix_digest_and_new_tag() -> None:
    p = run(["docs/x.md"])
    r = {x["service"]: x for x in p["retag"]}
    s = r["svc-streaming"]
    assert s["src"] == f"{REG}/svc-streaming:{OLD_TAG}-cpu"
    assert s["dst"] == f"{REG}/svc-streaming:{NEW_TAG}-cpu"
    assert s["digest"].startswith("sha256:")
    assert r["hub-api"]["dst"] == f"{REG}/hub-api:{NEW_TAG}"


def test_shared_crate_rebuilds_all_dependents_only() -> None:
    p = run(["core/service_auth/src/lib.rs"])
    assert set(p["build"]) == {
        "svc-ingest",
        "svc-process",
        "svc-action",
        "svc-presentation",
        "egress-proxy",
    }


def test_missing_deployed_image_in_registry_is_rebuilt() -> None:
    p = run(["docs/x.md"], missing={"seeder"})
    assert p["build"] == ["seeder"]
    assert "not found in registry" in p["decisions"]["seeder"]["reasons"][0]


def test_missing_revision_label_is_rebuilt() -> None:
    p = run(["docs/x.md"], unlabeled={"hub-webui"})
    assert p["build"] == ["hub-webui"]
    assert "label" in p["decisions"]["hub-webui"]["reasons"][0]


def test_no_helm_values_builds_everything() -> None:
    p = ap.build_plan(
        IMAP,
        HEAD,
        NEW_TAG,
        REG,
        None,
        False,
        {"a": "tcp://a:1"},
        lambda _b, _h: [],
        inspect=fake_registry(),
        probe=lambda _u: True,
    )
    assert p["build"] == NAMES and p["retag"] == []
    assert set(p["helm_tags"].values()) == {NEW_TAG}


def test_force_all_builds_everything_even_when_nothing_changed() -> None:
    p = run(["docs/x.md"], force=True)
    assert p["build"] == NAMES and p["force_all"] is True


def test_per_image_pins_in_release_values_win_over_global() -> None:
    vals = values(tags={n: OLD_TAG for n in NAMES}, default="alpha-999")
    p = run(["docs/x.md"], vals=vals)
    assert p["build"] == []
    assert set(p["helm_tags"].values()) == {OLD_TAG}


def test_deployed_tags_resolution() -> None:
    assert ap.deployed_tags(None, ["a"]) == {"a": None}
    assert ap.deployed_tags({"global": {"imageTag": "t"}}, ["a"]) == {"a": "t"}
    assert ap.deployed_tags(
        {"global": {"imageTag": "t", "imageTags": {"a": "u"}}}, ["a", "b"]
    ) == {"a": "u", "b": "t"}


def test_single_endpoint_default_is_identical_to_today() -> None:
    p = run(["core/svc_ingest/src/x.rs", "hub_api/blueprints/x.py"])
    hosts = {r["buildkit_host"] for r in p["matrix"]["include"]}
    assert hosts == {"tcp://a:1234"} and p["warnings"] == []


def test_two_endpoints_split_lanes() -> None:
    p = run(
        ["core/svc_ingest/src/x.rs", "core/svc_streaming/src/x.rs"],
        lanes={"a": "tcp://a:1234", "b": "tcp://b:1234"},
    )
    by = {r["service"]: r["buildkit_host"] for r in p["matrix"]["include"]}
    assert by == {"svc-ingest": "tcp://a:1234", "svc-streaming": "tcp://b:1234"}


def test_unreachable_secondary_falls_back_to_primary_with_warning() -> None:
    p = run(
        ["core/svc_streaming/src/x.rs"],
        lanes={"a": "tcp://a:1234", "b": "tcp://b:1234"},
        probe=lambda u: u.startswith("tcp://a"),
    )
    assert p["matrix"]["include"][0]["buildkit_host"] == "tcp://a:1234"
    assert any("unreachable" in w and "lane b" in w for w in p["warnings"])


def test_missing_primary_endpoint_fails_loudly() -> None:
    with pytest.raises(SystemExit):
        run(["docs/x.md"], lanes={"a": "", "b": ""})


def test_probe_endpoint_rejects_non_tcp_and_closed_port() -> None:
    assert ap.probe_endpoint("http://x:1") is False
    assert ap.probe_endpoint("tcp://127.0.0.1:1", timeout=0.5) is False


def test_config_labels_handles_index_and_manifest_shapes() -> None:
    cfg = {"config": {"Labels": {ap.REVISION_LABEL: "abc"}}}
    assert ap._config_labels({"linux/amd64": cfg}) == {ap.REVISION_LABEL: "abc"}
    assert ap._config_labels(cfg) == {ap.REVISION_LABEL: "abc"}
    assert ap._config_labels(None) == {}
    assert ap._config_labels({"linux/arm64": cfg}) == {ap.REVISION_LABEL: "abc"}


def test_retag_cmd_fails_on_empty_plan(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "plan.json"
    f.write_text(json.dumps({"retag": []}))
    assert ap.main(["retag", "--plan", str(f)]) == 1


def test_retag_cmd_verifies_digest(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = {"service": "x", "src": "r/x:old", "dst": "r/x:new", "digest": "sha256:aaa"}
    f = tmp_path / "plan.json"
    f.write_text(json.dumps({"retag": [row]}))
    monkeypatch.setattr(ap, "registry_retag", lambda _s, _d, _t: None)
    monkeypatch.setattr(
        ap, "registry_inspect", lambda _ref: ap.ImageInfo("sha256:bbb", None)
    )
    assert ap.main(["retag", "--plan", str(f)]) == 1  # digest mismatch fails loudly
    monkeypatch.setattr(
        ap, "registry_inspect", lambda _ref: ap.ImageInfo("sha256:aaa", None)
    )
    assert ap.main(["retag", "--plan", str(f)]) == 0
    monkeypatch.setattr(ap, "registry_inspect", lambda _ref: None)
    assert ap.main(["retag", "--plan", str(f)]) == 1  # destination missing after retag


def _plan_file(
    tmp_path: pathlib.Path, paths: list[str]
) -> tuple[pathlib.Path, dict[str, Any]]:
    p = run(paths)
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(p))
    return f, p


def test_helm_values_file_pins_every_image(tmp_path: pathlib.Path) -> None:
    f, _ = _plan_file(tmp_path, ["core/svc_streaming/src/x.rs"])
    out = tmp_path / "v.json"
    assert ap.main(["helm-values", "--plan", str(f), "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["global"]["imageTag"] == NEW_TAG
    assert doc["global"]["imageTags"]["svc-streaming"] == NEW_TAG
    assert doc["global"]["imageTags"]["hub-api"] == OLD_TAG
    assert set(doc["global"]["imageTags"]) == set(NAMES)


def test_verify_checks_only_deployment_images(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    f, p = _plan_file(tmp_path, ["core/svc_streaming/src/x.rs"])
    want = ap.expected_deployment_images(p, REG, IMAP)
    assert "seeder" not in want and "migrations" not in want and len(want) == 9
    monkeypatch.setattr(sys, "stdin", io.StringIO(" ".join(want.values())))
    assert ap.main(["verify", "--plan", str(f), "--registry", REG]) == 0
    assert want["svc-streaming"].endswith(f"{NEW_TAG}-cpu")
    monkeypatch.setattr(sys, "stdin", io.StringIO(" ".join(list(want.values())[1:])))
    assert (
        ap.main(["verify", "--plan", str(f), "--registry", REG]) == 1
    )  # one missing -> fail
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert ap.main(["verify", "--plan", str(f), "--registry", REG]) == 1
