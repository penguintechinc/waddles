"""Tests for `scripts/marketplace_fixture_git_server.py` (the flag-on E2E fixture git remote).

Real `git` against the real server over loopback HTTP (TLS is exercised separately): clone every
fixture kind, drive the admin API (auth, commit, force-push) and the CLI fail-loud paths.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.malicious_source_fixtures import GIT_ENV

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "marketplace_fixture_git_server.py"
TOKEN = "t" * 24

spec = importlib.util.spec_from_file_location("mkt_fixture_git", SCRIPT)
assert spec is not None and spec.loader is not None
fx = importlib.util.module_from_spec(spec)
sys.modules["mkt_fixture_git"] = fx
spec.loader.exec_module(fx)


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Any, str]]:
    root = tmp_path_factory.mktemp("fixture-root")
    store = fx.FixtureStore(root, base_url="https://fixture.test", wasm=fx.PLACEHOLDER_WASM)
    store.build_all()
    server = fx.make_server(store, "127.0.0.1", 0, admin_token=TOKEN, ssl_context=None)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield store, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _call(
    base: str, method: str, path: str, body: object | None = None, *, token: str | None = TOKEN
) -> tuple[int, Any]:
    req = urllib.request.Request(  # noqa: S310
        base + path, method=method, data=None if body is None else json.dumps(body).encode()
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(  # noqa: S603
        [shutil.which("git") or "git", *args],
        cwd=cwd,
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_every_fixture_repo_is_cloneable_over_http(served: tuple[Any, str], tmp_path: Path) -> None:
    store, base = served
    names = set(fx.PREBUILT_REPOS + fx.SANDBOX_REPOS + fx.HOSTILE_REPOS)
    assert set(store.repos) == names and len(names) == 12
    for name in sorted(names):
        dest = tmp_path / name
        _git("clone", "-q", f"{base}/{name}.git", str(dest))
        assert _git("rev-parse", "HEAD", cwd=dest) == store.head(name)
        assert (dest / "bundles" / "python" / "hello" / "hub-manifest.yaml").is_file()


def test_hostile_repos_carry_their_payloads(served: tuple[Any, str], tmp_path: Path) -> None:
    _, base = served

    def tree(name: str) -> Path:
        _git("clone", "-q", f"{base}/{name}.git", str(tmp_path / name))
        return tmp_path / name / "bundles" / "python" / "hello"

    assert (tree("hostile-symlink-escape") / "esc").is_symlink()
    assert (tree("hostile-symlink-abs") / "abs").is_symlink()
    assert (tree("hostile-zip-bomb") / "payload.zip").stat().st_size < 1_000_000
    assert "dashscope" in (tree("hostile-denylisted-dep") / "requirements.txt").read_text()
    assert "waddles.core.hello" in (tree("hostile-core-shadow") / "hub-manifest.yaml").read_text()
    assert "src-" + "a" * 12 in (tree("hostile-other-prefix") / "hub-manifest.yaml").read_text()
    assert any(p.is_file() for p in tree("hostile-deep-path").rglob("f.txt"))
    d = tree("sandbox-egress")
    assert "socket" in (d / "egress_probe.py").read_text()


def test_admin_requires_bearer_token(served: tuple[Any, str]) -> None:
    _, base = served
    assert _call(base, "GET", "/_admin/repos", token=None)[0] == 401
    assert _call(base, "GET", "/_admin/repos", token="wrong")[0] == 401
    code, body = _call(base, "GET", "/_admin/repos")
    assert code == 200 and len(body) == 12
    code, body = _call(base, "GET", "/_admin/publisher-key")
    assert code == 200 and len(body["public_key"]) == 44


def test_commit_signs_for_source_and_bumps_head(served: tuple[Any, str], tmp_path: Path) -> None:
    store, base = served
    before = store.head("autoupdate")
    sid = str(uuid.uuid4())
    code, body = _call(
        base, "POST", "/_admin/repos/autoupdate/commit", {"version": "1.1.0", "sign_for": sid}
    )
    assert code == 200 and body["head"] != before == _git(
        "rev-parse", "HEAD~1", cwd=_clone(base, tmp_path)
    )
    d = _clone(base, tmp_path / "again", "autoupdate") / "bundles" / "python" / "hello"
    assert (d / "component.sig").is_file() and "1.1.0" in (d / "hub-manifest.yaml").read_text()
    # unsigned commit removes the attestation
    _call(base, "POST", "/_admin/repos/autoupdate/commit", {"version": "1.2.0"})
    assert not (store.repos["autoupdate"].work / "bundles/python/hello/component.sig").exists()


def _clone(base: str, dest: Path, name: str = "autoupdate") -> Path:
    _git("clone", "-q", f"{base}/{name}.git", str(dest))
    return dest


def test_force_push_makes_old_head_unreachable_from_main(
    served: tuple[Any, str], tmp_path: Path
) -> None:
    store, base = served
    old = store.head("rewrite")
    code, body = _call(base, "POST", "/_admin/repos/rewrite/force-push", {})
    assert code == 200 and body["head"] != old
    dest = _clone(base, tmp_path / "rw", "rewrite")
    ancestry = _git("rev-list", "main", cwd=dest).split()
    assert old not in ancestry


@pytest.mark.parametrize(
    ("method", "path", "body", "code"),
    [
        ("POST", "/_admin/repos/nope/commit", {"version": "1.0.1"}, 404),
        ("POST", "/_admin/repos/prebuilt/commit", {}, 400),
        (
            "POST",
            "/_admin/repos/prebuilt/commit",
            {"version": "1.0.1", "sign_for": "not-a-uuid"},
            400,
        ),
        ("POST", "/_admin/repos/sandbox/commit", {"version": "1.0.1"}, 404),
        ("POST", "/_admin/repos/prebuilt/explode", {}, 404),
        ("GET", "/_admin/nothing", None, 404),
    ],
)
def test_admin_errors_are_loud(
    served: tuple[Any, str], method: str, path: str, body: object, code: int
) -> None:
    assert _call(served[1], "POST" if method == "POST" else "GET", path, body)[0] == code


def test_admin_invalid_json_is_400(served: tuple[Any, str]) -> None:
    req = urllib.request.Request(  # noqa: S310
        served[1] + "/_admin/repos/prebuilt/commit", method="POST", data=b"{nope"
    )
    req.add_header("Authorization", f"Bearer {TOKEN}")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=20)  # noqa: S310
    assert ei.value.code == 400


def test_git_receive_pack_is_refused(served: tuple[Any, str]) -> None:
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(  # noqa: S310
            served[1] + "/prebuilt.git/info/refs?service=git-receive-pack", timeout=20
        )
    assert ei.value.code in (403, 404)


def test_unknown_repo_path_is_a_git_backend_error(served: tuple[Any, str]) -> None:
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(served[1] + "/missing.git/info/refs?service=git-upload-pack")  # noqa: S310
    assert ei.value.code in (404, 502)


def test_store_preconditions(tmp_path: Path) -> None:
    with pytest.raises(fx.FixtureError):
        fx.FixtureStore(tmp_path, base_url="https://h", wasm=None)
    store = fx.FixtureStore(tmp_path, base_url="https://h/", wasm=b"w")
    assert store.url("x") == "https://h/x.git"
    with pytest.raises(fx.FixtureError):
        store.head("none")
    with pytest.raises(fx.FixtureError):
        fx.make_handler(store, "")
    # the publisher key persists across store instances (stable registration)
    assert fx.FixtureStore(tmp_path, base_url="https://h", wasm=b"w").publisher_public_key == (
        store.publisher_public_key
    )


def test_sandbox_bundle_dir_is_copied(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "m.py").write_text("x = 1\n")
    (src / "hub-manifest.yaml").write_text("ignored: true\n")
    store = fx.FixtureStore(tmp_path / "r", base_url="https://h", wasm=b"w", sandbox_bundle_dir=src)
    store._build_sandbox("sandbox", egress_probe=False)
    assert (store.repos["sandbox"].work / "bundles/python/hello/sub/m.py").is_file()


def test_tls_context_and_self_signed(tmp_path: Path) -> None:
    cert, key = fx.self_signed(tmp_path, "fixture.test")
    ctx = fx.tls_context(cert, key)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_self_signed_requires_openssl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fx.shutil, "which", lambda _n: None)
    with pytest.raises(fx.FixtureError):
        fx.self_signed(tmp_path, "h")


def test_http_backend_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class R:
        stdout = str(tmp_path)

    monkeypatch.setattr(fx.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(fx.FixtureError):
        fx._http_backend()


def test_make_server_wraps_tls(tmp_path: Path) -> None:
    store = fx.FixtureStore(tmp_path, base_url="https://h", wasm=b"w")
    cert, key = fx.self_signed(tmp_path, "h")
    server = fx.make_server(
        store, "127.0.0.1", 0, admin_token=TOKEN, ssl_context=fx.tls_context(cert, key)
    )
    try:
        assert isinstance(server.socket, ssl.SSLSocket)
    finally:
        server.server_close()


def test_main_print_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = fx.main(
        ["--hostname", "h.test", "--allow-placeholder-wasm", "--root", str(tmp_path),
         "--print-index", "--port", "8443"]
    )  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["prebuilt"]["url"] == "https://h.test:8443/prebuilt.git"


def test_main_fails_loudly_without_prereqs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = ["--hostname", "h.test", "--root", str(tmp_path)]
    monkeypatch.delenv("FIXTURE_ADMIN_TOKEN", raising=False)
    assert fx.main(base) == 2  # no wasm
    assert fx.main([*base, "--allow-placeholder-wasm"]) == 2  # no cert/tls choice
    assert fx.main([*base, "--allow-placeholder-wasm", "--self-signed"]) == 2  # no admin token
    assert fx.main([*base, "--wasm", str(tmp_path / "missing.wasm")]) == 2


def test_main_serves_until_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIXTURE_ADMIN_TOKEN", TOKEN)

    def boom(self: Any, *a: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(fx.ThreadingHTTPServer, "serve_forever", boom)
    rc = fx.main(
        ["--hostname", "h.test", "--allow-placeholder-wasm", "--self-signed",
         "--root", str(tmp_path), "--host", "127.0.0.1", "--port", "0"]
    )  # fmt: skip
    assert rc == 0
