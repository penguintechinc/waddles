"""Tests for scripts/ci/check-dockerfile-pip-hashes.py (every Dockerfile pip install is hash-locked)."""

import importlib.util
import pathlib
import sys
from typing import Any

import pytest

CI = pathlib.Path(__file__).resolve().parents[1]
REPO = CI.parents[1]
_spec = importlib.util.spec_from_file_location("ph", CI / "check-dockerfile-pip-hashes.py")
assert _spec is not None and _spec.loader is not None
ph = importlib.util.module_from_spec(_spec)
sys.modules["ph"] = ph
_spec.loader.exec_module(ph)

LOCKED = "RUN pip install --no-cache-dir --require-hashes -r requirements.txt\n"
FLASK_CORE_OLD = "RUN pip install --no-cache-dir /app/libs/flask_core\n"
FLASK_CORE_NEW = (
    "RUN pip install --no-cache-dir --no-deps --no-build-isolation /app/libs/flask_core\n"
)


def check(body: str) -> tuple[int, list[Any]]:
    """Run the checker over a Dockerfile body."""
    return ph.check_text("Dockerfile", body)


def test_old_unhashed_flask_core_install_is_a_failing_fixture() -> None:
    n, fs = check("FROM x\n" + LOCKED + FLASK_CORE_OLD)
    assert n == 2
    assert fs, "the pre-fix `pip install /app/libs/flask_core` line must fail"
    assert all(f.line == 3 for f in fs)


def test_old_flask_core_install_with_cache_mount_also_fails() -> None:
    old = "RUN --mount=type=cache,id=c,target=/root/.cache/pip pip install /app/libs/flask_core\n"
    assert check("FROM x\n" + LOCKED + old)[1]


def test_hash_locked_then_nodeps_local_install_passes() -> None:
    n, fs = check("FROM x\n" + LOCKED + FLASK_CORE_NEW)
    assert (n, fs) == (2, [])


def test_chained_in_one_run_passes() -> None:
    chain = (
        "RUN pip install --require-hashes -r /tmp/b.txt && \\\n"
        "    # a comment inside a continued RUN\n"
        "    pip install --no-deps --no-build-isolation /app/libs/flask_core && \\\n"
        "    pip uninstall -y setuptools\n"
    )
    assert check("FROM x\n" + chain) == (2, [])


@pytest.mark.parametrize(
    "run",
    [
        "RUN pip install requests",
        "RUN pip install --no-cache-dir 'alembic>=1.13' sqlalchemy",
        "RUN pip3 install -r requirements.txt",
        "RUN python3 -m pip install componentize-py==0.25.1 pyyaml==6.0.3",
        "RUN pip install --no-deps requests",  # --no-deps on a named package is not enough
        "RUN pip install --require-hashes -r r.txt && pip install --no-cache-dir /app/x",
        "RUN pip install --upgrade pip",
        "RUN uv pip install --system requests",
    ],
)
def test_unhashed_installs_fail(run: str) -> None:
    assert check("FROM x\n" + run + "\n")[1], run


def test_nodeps_local_install_without_prior_hashed_install_fails() -> None:
    fs = check("FROM x\n" + FLASK_CORE_NEW)[1]
    assert any("earlier --require-hashes" in f.why for f in fs)


def test_local_install_without_no_build_isolation_fails() -> None:
    body = "FROM x\n" + LOCKED + "RUN pip install --no-deps /app/libs/flask_core\n"
    assert any("--no-build-isolation" in f.why for f in check(body)[1])


def test_hashed_state_is_per_stage_and_inherited_by_name() -> None:
    other_stage = (
        "FROM a AS builder\n" + LOCKED + "FROM b\n" + FLASK_CORE_NEW  # fresh stage: not hashed
    )
    assert check(other_stage)[1]
    inherited = "FROM a AS builder\n" + LOCKED + "FROM builder AS next\n" + FLASK_CORE_NEW
    assert check(inherited)[1] == []


def test_dockerfile_without_pip_examines_zero() -> None:
    assert check("FROM x\nRUN echo hi\n") == (0, [])


def test_main_fails_closed_on_zero_examined(tmp_path: pathlib.Path) -> None:
    df = tmp_path / "Dockerfile"
    df.write_text("FROM x\nRUN echo hi\n")
    assert ph.main([str(df)]) == 1


def test_main_fails_on_violation_and_passes_when_clean(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "Dockerfile.bad"
    bad.write_text("FROM x\n" + FLASK_CORE_OLD)
    good = tmp_path / "Dockerfile.good"
    good.write_text("FROM x\n" + LOCKED + FLASK_CORE_NEW)
    assert ph.main([str(bad)]) == 1
    assert ph.main([str(good)]) == 0


def test_whole_repo_passes_and_examines_the_installs() -> None:
    files = ph.dockerfiles()
    total = 0
    bad: list[Any] = []
    for p in files:
        n, fs = ph.check_text(p, (REPO / p).read_text())
        total += n
        bad += fs
    assert len(files) >= 40
    assert total >= 8, f"expected the repo's pip install steps, examined {total}"
    assert bad == [], bad


def test_flask_core_images_install_with_no_deps_and_hashed_lock() -> None:
    for p in ("hub_api/Dockerfile", "bundles/Dockerfile.core-bundles"):
        text = (REPO / p).read_text()
        assert "--no-deps --no-build-isolation /app/libs/flask_core" in text, p
