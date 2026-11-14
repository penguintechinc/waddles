"""Security guards for the shared BuildKit cache mounts (build-cache poisoning findings).

* a cache-mounted install step must be integrity-verified (check-dockerfile-cache-mounts.py),
* the cached build path only runs for release-branch pushes / dispatches (alpha_plan.py),
* only deploy-dal2alpha.yml may reach the persistent shared BuildKit pod, and it has no
  pull_request-style trigger.
"""

import importlib.util
import pathlib
import re
import sys
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

CI = pathlib.Path(__file__).resolve().parents[1]
REPO = CI.parents[1]
sys.path.insert(0, str(CI))
import alpha_plan as ap

_spec = importlib.util.spec_from_file_location(
    "cm", CI / "check-dockerfile-cache-mounts.py"
)
assert _spec is not None and _spec.loader is not None
cm = importlib.util.module_from_spec(_spec)
sys.modules["cm"] = cm
_spec.loader.exec_module(cm)

MOUNT = "RUN --mount=type=cache,id=x,target=/root/.cache/pip,sharing=shared \\\n    "


def findings(run_line: str) -> list[Any]:
    """Findings for a one-RUN Dockerfile."""
    return list(cm.check_text("Dockerfile", f"FROM x\n{run_line}\n")[1])


def test_original_unhashed_flask_core_install_is_flagged() -> None:
    assert findings(MOUNT + "pip install /app/libs/flask_core")


@pytest.mark.parametrize(
    "run",
    [
        MOUNT + "pip install --require-hashes -r requirements.txt",
        MOUNT + "pip install --require-hashes -r /tmp/r.txt && rm -f /tmp/r.txt",
        MOUNT + "pip install --no-deps --no-build-isolation /app/libs/flask_core",
        MOUNT + "python3 -m pip install --require-hashes -r r.txt",
        "RUN --mount=type=cache,id=c,target=/c cargo build --release --locked && install -D a b",
        "RUN --mount=type=cache,id=n,target=/root/.npm npm ci --ignore-scripts",
    ],
)
def test_verified_installs_pass(run: str) -> None:
    assert findings(run) == []


@pytest.mark.parametrize(
    "run",
    [
        MOUNT + "pip install requests",
        MOUNT
        + "pip install --no-deps requests",  # --no-deps on a *named* package is not enough
        MOUNT + "pip install -r requirements.txt",
        MOUNT + "pip install --require-hashes -r r.txt && pip install /app/x",
        "RUN --mount=type=cache,id=c,target=/c cargo build --release",
        "RUN --mount=type=cache,id=n,target=/root/.npm npm install",
        "RUN --mount=type=cache,id=n,target=/root/.npm yarn install",
    ],
)
def test_unverified_installs_fail(run: str) -> None:
    assert findings(run), run


def test_uncached_steps_are_ignored() -> None:
    assert cm.check_text("D", "FROM x\nRUN pip install requests\n") == (0, [])


def test_whole_repo_passes_and_examines_the_mounted_steps() -> None:
    files = cm.dockerfiles()
    total = 0
    bad: list[Any] = []
    for p in files:
        n, fs = cm.check_text(p, (REPO / p).read_text())
        total += n
        bad += fs
    assert total >= 11, f"expected the 11 cache-mounted steps, examined {total}"
    assert bad == [], bad


def test_cache_ids_carry_the_epoch_salt() -> None:
    for p in cm.dockerfiles():
        text = (REPO / p).read_text()
        ids = re.findall(r"--mount=type=cache,id=([^,\s]+)", text)
        for i in ids:
            assert "${CACHE_EPOCH}" in i, (
                f"{p}: cache id {i!r} lacks the CACHE_EPOCH salt"
            )
        if ids:
            assert "ARG CACHE_EPOCH=" in text, (
                f"{p}: mounts use CACHE_EPOCH but never declare it"
            )


@pytest.mark.parametrize(
    "event,ref,ok",
    [
        ("push", "refs/heads/release/v3.0.X", True),
        ("workflow_dispatch", "refs/heads/release/v3.0.X", True),
        ("pull_request", "refs/pull/12/merge", False),
        ("pull_request_target", "refs/heads/release/v3.0.X", False),
        ("push", "refs/heads/main", False),
        ("push", "refs/heads/feature/x", False),
        ("workflow_dispatch", "refs/heads/chore/x", False),
        ("push", "refs/heads/release/v3.0.X/../x", False),
        ("", "", False),
    ],
)
def test_trusted_context(event: str, ref: str, ok: bool) -> None:
    assert (ap.check_trusted_context(event, ref) is None) is ok


def test_plan_refuses_untrusted_context(tmp_path: pathlib.Path) -> None:
    rc = ap.main(
        [
            "plan", "--tag", "alpha-1", "--registry", "r", "--out", str(tmp_path / "p.json"),
            "--event", "pull_request", "--ref", "refs/pull/1/merge",
        ]
    )  # fmt: skip
    assert rc == 2
    assert not (tmp_path / "p.json").exists()


def _workflows() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in sorted((REPO / ".github/workflows").glob("*.y*ml")):
        out[f.name] = yaml.safe_load(f.read_text())
    return out


def test_only_the_deploy_workflow_reaches_the_shared_buildkit() -> None:
    users = []
    for name, text in (
        (f.name, f.read_text()) for f in (REPO / ".github/workflows").glob("*.y*ml")
    ):
        if re.search(r"driver:\s*remote|BUILDKIT_HOST|buildkitd\.buildkit", text):
            users.append(name)
    assert users == ["deploy-dal2alpha.yml"], users


def test_deploy_workflow_has_no_pr_style_trigger_and_every_job_pins_the_release_ref() -> (
    None
):
    wf = _workflows()["deploy-dal2alpha.yml"]
    # PyYAML parses a bare `on:` key as the boolean True
    raw: dict[Any, Any] = wf
    triggers = raw.get("on") or raw[True]
    assert set(triggers) == {"push", "workflow_dispatch"}, set(triggers)
    assert triggers["push"]["branches"] == ["release/v3.0.X"]
    for name, job in wf["jobs"].items():
        cond = str(job.get("if", ""))
        assert "github.ref == 'refs/heads/release/v3.0.X'" in cond, (
            f"job {name} lacks the release-ref guard"
        )
        assert "penguintechinc/waddles" in cond, (
            f"job {name} lacks the repository guard"
        )


def test_deploy_workflow_passes_the_cache_epoch_and_the_trusted_context() -> None:
    text = (REPO / ".github/workflows/deploy-dal2alpha.yml").read_text()
    assert "CACHE_EPOCH=${{ vars.ALPHA_CACHE_EPOCH" in text
    assert "GITHUB_EVENT_NAME" in text or "--event" in text
