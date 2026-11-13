"""Guards that keep scripts/ci/image-inputs.yaml honest as the repo changes.

* every tracked file is classified (a new unmapped path fails this test),
* every top-level directory/file is mentioned by the map (a new top-level dir fails),
* every Dockerfile COPY source is covered by that image's inputs (the map cannot drift
  from the Dockerfiles it describes),
* lanes/images are structurally sane.
"""

import os
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterator

import pytest

CI = pathlib.Path(__file__).resolve().parents[1]
REPO = CI.parents[1]
sys.path.insert(0, str(CI))
import changed_images as ci

IMAP = ci.load_map(CI / "image-inputs.yaml")


def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"], check=True, capture_output=True
    ).stdout.decode()
    return [p for p in out.split("\0") if p]


def test_every_tracked_file_is_classified() -> None:
    res = ci.audit(IMAP, tracked())
    assert res["examined"] > 1000, "audit examined suspiciously few files"
    assert res["unmapped"] == [], (
        "unmapped paths -- add them to an image's inputs or to `ignore` in "
        f"scripts/ci/image-inputs.yaml: {res['unmapped'][:20]}"
    )


def _first_segments() -> set[str]:
    pats = list(IMAP.shared_all) + list(IMAP.ignore)
    for img in IMAP.images.values():
        pats.extend(img.inputs)
    segs = set()
    for p in pats:
        first = p.split("/")[0]
        if "**" not in first:
            segs.add(first)
    return segs


def test_every_top_level_entry_is_mapped() -> None:
    tops = {p.split("/")[0] for p in tracked()}
    segs = _first_segments()
    missing = sorted(
        t
        for t in tops
        if IMAP.classify(t)[0] == "unmapped"
        and not any(ci.glob_to_regex(s).match(t) or s == t for s in segs)
    )
    assert missing == [], (
        f"top-level entries not mentioned by image-inputs.yaml: {missing}; "
        "map each to an image or add it to `ignore`"
    )


def test_detector_fires_for_new_top_level_dir() -> None:
    """Prove the two guards above can fail: a synthetic new top dir is unmapped."""
    probe = "brand-new-service/src/main.rs"
    assert IMAP.classify(probe)[0] == "unmapped"
    assert "brand-new-service" not in _first_segments()


def _copy_sources(dockerfile: pathlib.Path) -> Iterator[tuple[list[str], str | None]]:
    """Yield (sources, from_stage_or_None) for each COPY/ADD instruction (line-continued)."""
    text = dockerfile.read_text()
    text = re.sub(r"\\\n", " ", text)
    for line in text.splitlines():
        m = re.match(r"\s*(?:COPY|ADD)\s+(.*)", line)
        if not m:
            continue
        toks = m.group(1).split()
        frm = None
        args = []
        for t in toks:
            if t.startswith("--from="):
                frm = t.split("=", 1)[1]
            elif t.startswith("--"):
                continue
            else:
                args.append(t)
        yield args[:-1], frm


@pytest.mark.parametrize("name", sorted(IMAP.images))
def test_dockerfile_copy_sources_are_inputs(name: str) -> None:
    img = IMAP.images[name]
    df = REPO / img.dockerfile
    assert df.is_file(), df
    named = {c.split("=", 1)[0] for c in img.build_contexts}
    examined = 0
    for sources, frm in _copy_sources(df):
        if frm is not None:
            if frm in named:
                # named build context (e.g. proto=./proto): its directory is an input
                src_dir = dict(c.split("=", 1) for c in img.build_contexts)[frm]
                probe = os.path.normpath(src_dir) + "/probe.file"
                _, hit = IMAP.classify(probe)
                assert name in hit, (
                    f"{name}: named context {frm} ({probe}) not an input"
                )
                examined += 1
            continue
        for s in sources:
            assert s not in (".", "./", ".."), (
                f"{name}: COPY of whole context ({s}) is unmappable"
            )
            rel = os.path.normpath(os.path.join(img.context, s))
            if "*" in rel:
                probe = rel.replace("*", "x")
            elif (REPO / rel).is_dir():
                probe = rel + "/probe.file"
            else:
                probe = rel
            _, hit = IMAP.classify(probe)
            assert name in hit, (
                f"{name}: Dockerfile COPY source {rel!r} (probe {probe!r}) is not covered by "
                "its inputs in scripts/ci/image-inputs.yaml"
            )
            examined += 1
    assert examined > 0, f"{name}: examined zero COPY sources"


def test_lanes_and_images_are_sane() -> None:
    assert set(IMAP.lanes) == {"a", "b"}
    lanes = {i.lane for i in IMAP.images.values()}
    assert lanes == {"a", "b"}, "both BuildKit lanes should carry images"
    heavy_rust = [
        "svc-ingest",
        "svc-process",
        "svc-action",
        "svc-bundles-executor",
        "svc-streaming",
        "svc-presentation",
        "egress-proxy",
    ]
    on_a = [n for n in heavy_rust if IMAP.images[n].lane == "a"]
    on_b = [n for n in heavy_rust if IMAP.images[n].lane == "b"]
    assert on_a and on_b, "heavy Rust compiles must be split across both lanes"
    assert abs(len(on_a) - len(on_b)) <= 1
    assert IMAP.images["seeder"].lane != IMAP.images["svc-ingest"].lane
