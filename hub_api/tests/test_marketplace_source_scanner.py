"""Malicious-source suite for the marketplace scanner/guards/denylist (Phase 1, Task 3)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from services import marketplace_source_scanner as sc
from services.dependency_denylist import match_denylist, scan_bundle_dependencies
from services.marketplace_source_guards import (
    GuardLimits,
    GuardViolationError,
    check_listing,
    check_tree,
)
from services.vendor_bundle_authz import source_namespace_prefix, source_short
from tests.malicious_source_fixtures import (
    add_bundle,
    commit_bare,
    make_source,
    make_zip_bomb,
    manifest,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


@pytest.fixture(autouse=True)
def _transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Enable the test-only local transport and neutralise DNS in the URL guard."""
    monkeypatch.setenv(sc.TEST_TRANSPORT_ENV, "1")

    async def _ok(url: str, *, allowed_schemes: tuple[str, ...]) -> str:
        return url

    monkeypatch.setattr(sc, "validate_outbound_url", _ok)
    yield
    sc._test_remote = None


async def _scan(
    tmp_path: Path, work: Path, *, limits: GuardLimits | None = None, pin: str | None = None
) -> Any:
    bare = tmp_path / "remote.git"
    sha = commit_bare(work, bare)
    sc.enable_test_transport(f"file://{bare}")
    src = make_source(pin or sha)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    try:
        return await sc.scan_source(src, workdir=scratch, limits=limits), src, scratch
    finally:
        assert list(scratch.iterdir()) == [], "temp clone must be cleaned up"


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


async def test_clean_repo_discovers_all_bundles(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    src_probe = make_source("a" * 40)
    prefix = source_namespace_prefix(source_short(src_probe))
    add_bundle(work, "python", "alpha")
    add_bundle(work, "rust", "beta", manifest(language="rust"))
    # app_id rewritten to the assigned src- space regardless of claim
    (result, src, _) = await _scan(tmp_path, work)
    assert result.examined == 2 and len(result.bundles) == 2 and not result.rejected
    ids = {b.assigned_app_id for b in result.bundles}
    assert ids == {f"{source_namespace_prefix(source_short(src))}{n}" for n in ("alpha", "beta")}
    assert prefix.startswith("waddles.integrations.src-")


async def test_sha_mismatch_aborts(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "alpha")
    with pytest.raises(sc.ScanRejectedError) as ei:
        await _scan(tmp_path, work, pin="0" * 40)
    assert ei.value.reason in {"git_failed", "sha_mismatch"}


async def test_no_pinned_sha_rejected(tmp_path: Path) -> None:
    src = make_source(None)
    with pytest.raises(sc.ScanRejectedError) as ei:
        await sc.scan_source(src, workdir=tmp_path)
    assert ei.value.reason == "no_pinned_sha"


async def test_no_proxy_in_prod_mode_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sc._test_remote = None
    monkeypatch.delenv(sc.PROXY_ENV, raising=False)
    with pytest.raises(sc.ScanRejectedError) as ei:
        await sc.scan_source(make_source("a" * 40), workdir=tmp_path)
    assert ei.value.reason == "no_egress_proxy"


def test_test_transport_disabled_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sc.TEST_TRANSPORT_ENV)
    with pytest.raises(RuntimeError):
        sc.enable_test_transport("file:///x")


def test_git_argv_hardened_and_env_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    argv = sc._git_argv("fetch", proxy="http://proxy:3128", local=False)
    assert (
        argv[0] == "git"
        and "protocol.allow=never" in argv
        and "http.proxy=http://proxy:3128" in argv
    )
    assert "http.followRedirects=false" in argv and "protocol.file.allow=always" not in argv
    env = sc._git_env(Path("/nonexistent"), proxy="http://proxy:3128")
    assert "AWS_SECRET_ACCESS_KEY" not in env and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/r.git",
        "https://169.254.169.254/r.git",
        "https://10.0.0.5/r.git",
        "http://git.example.org/r.git",
        "file:///etc/passwd",
        "ssh://git@git.example.org/r.git",
    ],
)
async def test_ssrf_and_scheme_rejected(
    url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from services.url_guard import validate_outbound_url

    monkeypatch.setattr(sc, "validate_outbound_url", validate_outbound_url)
    sc._test_remote = None
    monkeypatch.setenv(sc.PROXY_ENV, "http://proxy:3128")
    src = replace(make_source("a" * 40), git_url=url)
    with pytest.raises(sc.ScanRejectedError) as ei:
        await sc.scan_source(src, workdir=tmp_path)
    assert ei.value.reason == "url_rejected"


async def test_symlink_escape_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "alpha")
    os.symlink("../../../../etc", work / "bundles" / "python" / "alpha" / "esc")
    with pytest.raises(sc.ScanRejectedError) as ei:
        await _scan(tmp_path, work)
    assert ei.value.reason == "symlink_escape"


async def test_absolute_symlink_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "alpha")
    os.symlink("/etc/passwd", work / "bundles" / "python" / "alpha" / "abs")
    with pytest.raises(sc.ScanRejectedError) as ei:
        await _scan(tmp_path, work)
    assert ei.value.reason == "symlink_absolute"


async def test_zip_bomb_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    d = add_bundle(work, "python", "alpha")
    make_zip_bomb(d / "payload.zip", mb=60)
    with pytest.raises(sc.ScanRejectedError) as ei:
        await _scan(tmp_path, work)
    assert ei.value.reason == "archive_bomb"


async def test_gzip_bomb_rejected(tmp_path: Path) -> None:
    import gzip

    work = _repo(tmp_path)
    d = add_bundle(work, "python", "alpha")
    with gzip.open(d / "x.gz", "wb") as fh:
        fh.write(b"\0" * (30 * 1024 * 1024))
    with pytest.raises(sc.ScanRejectedError) as ei:
        await _scan(tmp_path, work)
    assert ei.value.reason == "archive_bomb"


async def test_oversized_clone_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "alpha", files={"big.bin": "x" * 200_000})
    add_bundle(work, "python", "beta", files={"big.bin": "y" * 200_000})
    with pytest.raises(sc.ScanRejectedError) as ei:
        await _scan(tmp_path, work, limits=GuardLimits(max_total_bytes=300_000))
    assert ei.value.reason == "clone_too_large"


async def test_per_file_too_large_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "alpha", files={"big.bin": "x" * 100_000})
    with pytest.raises(sc.ScanRejectedError) as ei:
        await _scan(tmp_path, work, limits=GuardLimits(max_file_bytes=50_000))
    assert ei.value.reason == "file_too_large"


async def test_too_many_files_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "alpha", files={f"f{i}.txt": "x" for i in range(30)})
    with pytest.raises(sc.ScanRejectedError) as ei:
        await _scan(tmp_path, work, limits=GuardLimits(max_files=20))
    assert ei.value.reason == "too_many_files"


async def test_too_deep_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(
        work, "python", "alpha", files={"/".join("d" * 1 for _ in range(15)) + "/f.txt": "x"}
    )
    with pytest.raises(sc.ScanRejectedError) as ei:
        await _scan(tmp_path, work, limits=GuardLimits(max_depth=8))
    assert ei.value.reason == "too_deep"


async def test_submodule_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "alpha")
    bare = tmp_path / "r.git"
    sha = commit_bare(work, bare, extra_index=f"160000,{'a' * 40},sub")
    sc.enable_test_transport(f"file://{bare}")
    (tmp_path / "s").mkdir()
    with pytest.raises(sc.ScanRejectedError) as ei:
        await sc.scan_source(make_source(sha), workdir=tmp_path / "s")
    assert ei.value.reason == "submodule"


async def test_core_namespace_claim_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "evil", manifest("waddles.core.x.evil"))
    add_bundle(work, "python", "good")
    (result, _, _) = await _scan(tmp_path, work)
    assert [r.reason for r in result.rejected] == ["namespace_violation"]
    assert [b.dir_name for b in result.bundles] == ["good"]


async def test_cross_source_claim_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    other = source_namespace_prefix("abcdef012345")
    add_bundle(work, "python", "evil", manifest(f"{other}evil"))
    add_bundle(work, "python", "good")
    (result, _, _) = await _scan(tmp_path, work)
    assert [r.reason for r in result.rejected] == ["namespace_violation"]


async def test_builtin_provider_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "evil", manifest(provider="builtin"))
    add_bundle(work, "python", "good")
    (result, _, _) = await _scan(tmp_path, work)
    assert [r.reason for r in result.rejected] == ["provider_not_thirdparty"]


async def test_malformed_and_nonsemver_manifests_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "bad1", raw_manifest="a: [unclosed")
    add_bundle(work, "python", "bad2", raw_manifest="- a\n- b\n")
    add_bundle(work, "python", "bad3", manifest(version="latest"))
    add_bundle(work, "python", "good")
    (result, _, _) = await _scan(tmp_path, work)
    reasons = {r.dir_name: r.reason for r in result.rejected}
    assert reasons["bad1"] == "manifest_unreadable" and reasons["bad2"] == "manifest_malformed"
    assert reasons["bad3"] == "bad_semver"
    assert result.examined == 4 and len(result.bundles) == 1


async def test_denylisted_dependency_rejected(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    add_bundle(work, "python", "bad", files={"requirements.txt": "requests\ndashscope==1.0\n"})
    add_bundle(work, "python", "good", files={"requirements.txt": "requests\n"})
    (result, _, _) = await _scan(tmp_path, work)
    assert len(result.rejected) == 1 and "dashscope" in result.rejected[0].detail
    assert [b.dir_name for b in result.bundles] == ["good"]


async def test_no_bundles_is_an_error(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    (work / "README.md").write_text("hi")
    with pytest.raises(sc.ScanRejectedError) as ei:
        await _scan(tmp_path, work)
    assert ei.value.reason == "no_bundles"


async def test_scan_updates_status_via_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []

    async def fake_update(dal: Any, pid: Any, status: Any, **kw: Any) -> None:
        calls.append((status, kw))

    monkeypatch.setattr(sc, "update_status", fake_update)
    work = _repo(tmp_path)
    add_bundle(work, "python", "alpha")
    bare = tmp_path / "r.git"
    sha = commit_bare(work, bare)
    sc.enable_test_transport(f"file://{bare}")
    scratch = tmp_path / "s"
    scratch.mkdir()
    await sc.scan_source(make_source(sha), workdir=scratch, dal=object())  # type: ignore[arg-type]
    assert calls[0][0] == sc.SourceStatus.ACTIVE and calls[0][1]["scanned"] is True
    with pytest.raises(sc.ScanRejectedError):
        await sc.scan_source(make_source("0" * 40), workdir=scratch, dal=object())  # type: ignore[arg-type]
    assert calls[1][0] == sc.SourceStatus.ERROR and calls[1][1]["last_scan_error"]


def test_listing_guard_unit() -> None:
    lim = GuardLimits(max_files=2, max_depth=3)
    with pytest.raises(GuardViolationError):
        check_listing([("100644", 1, "a")] * 3, lim)
    with pytest.raises(GuardViolationError):
        check_listing([("100644", 1, "a/b/c/d")], lim)
    with pytest.raises(GuardViolationError):
        check_listing([("100644", 1, "../x")], lim)


def test_check_tree_clean(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_text("x")
    assert check_tree(tmp_path, GuardLimits()) == 1


def test_tar_with_symlink_member_rejected(tmp_path: Path) -> None:
    import io
    import tarfile

    with tarfile.open(tmp_path / "x.tar", "w") as tf:
        info = tarfile.TarInfo("l")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info, io.BytesIO())
    with pytest.raises(GuardViolationError):
        check_tree(tmp_path, GuardLimits())


@pytest.mark.parametrize(
    ("eco", "fname", "content", "pkg"),
    [
        (
            "pypi",
            "pyproject.toml",
            '[project]\nname="x"\ndependencies=["Tencent-Cloud-SDK>=1"]\n',
            None,
        ),
        (
            "pypi",
            "pyproject.toml",
            '[project]\nname="x"\ndependencies=["tencentcloud-sdk-python"]\n',
            "tencentcloud-sdk-python",
        ),
        ("cargo", "Cargo.toml", '[dependencies]\nrsmpeg = "0.1"\n', "rsmpeg"),
        ("cargo", "Cargo.lock", '[[package]]\nname = "baidu-thing"\n', "baidu-thing"),
        (
            "npm",
            "package.json",
            '{"dependencies": {"@alicloud/pop-core": "1"}}',
            "@alicloud/pop-core",
        ),
        (
            "nuget",
            "x.csproj",
            "<Project><ItemGroup>"
            '<PackageReference Include="AlibabaCloud.SDK.Core" Version="1"/>'
            "</ItemGroup></Project>",
            "AlibabaCloud.SDK.Core",
        ),
    ],
)
def test_denylist_each_ecosystem(
    tmp_path: Path, eco: str, fname: str, content: str, pkg: str | None
) -> None:
    (tmp_path / fname).write_text(content)
    found = scan_bundle_dependencies(tmp_path)
    if pkg is None:
        assert found == ()
    else:
        assert [(f.ecosystem, f.package) for f in found] == [(eco, pkg)]


def test_denylist_clean_and_malformed(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"dependencies": {"left-pad": "1"}}')
    assert scan_bundle_dependencies(tmp_path) == ()
    (tmp_path / "Cargo.toml").write_text("not = [toml")
    with pytest.raises(ValueError):
        scan_bundle_dependencies(tmp_path)
    assert match_denylist("pypi", "Qwen_VL") == "qwen*"


def _scan_err(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        scan_bundle_dependencies(tmp_path)


def _names(tmp_path: Path) -> list[str]:
    return [f.package for f in scan_bundle_dependencies(tmp_path)]


def test_denylist_npm_alias_and_lock_name(tmp_path: Path) -> None:
    """regression: npm alias / lockfile `name` bypass."""
    from services.dependency_denylist import _DENYLIST

    bad = next(p for p in _DENYLIST["npm"] if not p.endswith("*"))
    (tmp_path / "package.json").write_text(f'{{"dependencies": {{"innocent": "npm:{bad}@1"}}}}')
    assert bad in _names(tmp_path)
    (tmp_path / "package.json").write_text('{"dependencies": {"ok": "^1.0.0"}}')
    assert _names(tmp_path) == []
    (tmp_path / "package-lock.json").write_text(
        f'{{"packages": {{"node_modules/innocent": {{"name": "{bad}", "version": "1.0.0"}}}}}}'
    )
    assert bad in _names(tmp_path)


@pytest.mark.parametrize(
    "spec", ["git+https://x/y.git", "user/repo", "file:../x", "https://x/t.tgz"]
)
def test_denylist_npm_non_registry_spec_rejected(tmp_path: Path, spec: str) -> None:
    (tmp_path / "package.json").write_text(f'{{"dependencies": {{"foo": "{spec}"}}}}')
    _scan_err(tmp_path)


def test_denylist_requirements_include_resolved(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("-r other.txt\nclean==1\n")
    (tmp_path / "other.txt").write_text("qwen-vl==1\n")
    assert _names(tmp_path) == ["qwen-vl"]
    (tmp_path / "requirements.txt").write_text("-r ../escape.txt\n")
    _scan_err(tmp_path)
    (tmp_path / "requirements.txt").write_text("-e .\n")
    _scan_err(tmp_path)
    (tmp_path / "requirements.txt").write_text("pkg @ https://evil/x.whl\n")
    _scan_err(tmp_path)


def test_denylist_csproj_quotes_and_update(tmp_path: Path) -> None:
    (tmp_path / "x.csproj").write_text(
        "<Project><ItemGroup><PackageReference Update='AlibabaCloud.SDK.Core' Version='1'/>"
        "</ItemGroup></Project>"
    )
    assert _names(tmp_path) == ["AlibabaCloud.SDK.Core"]
    (tmp_path / "x.csproj").write_text('<PackageReference Include="$(Pkg)"/>')
    _scan_err(tmp_path)


def test_denylist_unsupported_manifest_rejected(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").write_text("# yarn\n")
    _scan_err(tmp_path)


def test_denylist_symlinked_manifest_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("clean==1\n")
    (tmp_path / "requirements.txt").symlink_to(real)
    _scan_err(tmp_path)


def test_denylist_symlinked_dir_rejected(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "d", target_is_directory=True)
    _scan_err(tmp_path)


def test_denylist_cargo_patch_and_path_dep(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[patch.crates-io]\nrsmpeg = { path = "vendor/r" }\n')
    assert "rsmpeg" in _names(tmp_path)
    (tmp_path / "Cargo.toml").write_text('[dependencies]\nfoo = { path = "../../outside" }\n')
    _scan_err(tmp_path)
    (tmp_path / "Cargo.toml").write_text('[dependencies]\nfoo = { path = "sub" }\n')
    assert _names(tmp_path) == []


def test_denylist_pip_mixed_options_rejected_and_include_scanned(tmp_path: Path) -> None:
    """regression: safe -r first token must not smuggle --extra-index-url on the same line."""
    (tmp_path / "safe.txt").write_text("dashscope==1\n")
    for bad in (
        "-r safe.txt --extra-index-url http://evil",
        "-r safe.txt -i http://evil",
        "-r safe.txt --pre",
        "-r safe.txt -f http://evil",
        "-e .",
        "--trusted-host evil.example",
        "-r safe.txt --find-links=x",
    ):
        (tmp_path / "requirements.txt").write_text(bad + "\n")
        _scan_err(tmp_path)
    (tmp_path / "requirements.txt").write_text("-r safe.txt -c safe.txt\n--require-hashes\nok==1\n")
    assert "dashscope" in _names(tmp_path)


@pytest.mark.parametrize(
    ("spec", "ok"),
    [
        ("^1.2.3", True), (">=1.0.0 <2.0.0", True), ("1.x", True), ("latest", True),
        ("1.0.0 - 2.0.0", True), ("^1 || ^2", True), ("npm:left-pad@^1.0.0", True),
        ("evil.tgz", False), ("1.0.0.tgz", False), ("x.tar.gz", False), ("a.tar", False),
        ("git+https://x/y.git", False), ("github:o/r", False), ("o/r", False),
        ("file:../x", False), ("https://x/y", False), ("./x", False), ("npm:a/b@1", False),
    ],
)  # fmt: skip
def test_denylist_npm_spec_allowlist(tmp_path: Path, spec: str, ok: bool) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"pkg": spec}}))
    if ok:
        assert _names(tmp_path) == []
    else:
        _scan_err(tmp_path)


def test_denylist_npm_nested_overrides_resolutions(tmp_path: Path) -> None:
    from services.dependency_denylist import _DENYLIST

    bad = next(p for p in _DENYLIST["npm"] if not p.endswith("*"))
    for key in ("overrides", "resolutions"):
        doc = {key: {"a": {"b": {"c": {bad: "1.0.0"}}}}}
        (tmp_path / "package.json").write_text(json.dumps(doc))
        assert bad in _names(tmp_path)
        doc = {key: {"a": {".": "evil.tgz"}}}
        (tmp_path / "package.json").write_text(json.dumps(doc))
        _scan_err(tmp_path)
        doc = {key: {"a": {"b": {"c": "git+https://x/y"}}}}
        (tmp_path / "package.json").write_text(json.dumps(doc))
        _scan_err(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({key: {"a": {".": "^1.0.0"}}}))
        assert _names(tmp_path) == []


def test_denylist_nuget_xml_parser(tmp_path: Path) -> None:
    pkg = "AlibabaCloud.SDK.Core"
    ent = "AlibabaCloud&#x2E;SDK.Core"  # XML character reference regex scanners miss
    (tmp_path / "a.csproj").write_text(
        f'<Project><ItemGroup><PackageReference Include="{ent}" Version="1"/>'
        '<PackageVersion\n Update="Ok.Pkg" /></ItemGroup></Project>'
    )
    assert _names(tmp_path) == [pkg]
    (tmp_path / "a.csproj").write_text('<Project><Import Project="../../evil.props"/></Project>')
    _scan_err(tmp_path)
    (tmp_path / "a.csproj").write_text('<Project><Import Project="$(X)/e.props"/></Project>')
    _scan_err(tmp_path)
    (tmp_path / "a.csproj").write_text('<Project><Import Project="b.props"/></Project>')
    (tmp_path / "b.props").write_text(
        f'<Project><ItemGroup><GlobalPackageReference Include="{pkg}"/></ItemGroup></Project>'
    )
    assert _names(tmp_path) == [pkg]
    (tmp_path / "b.props").write_text("<!DOCTYPE x [<!ENTITY e 'v'>]><Project>&e;</Project>")
    _scan_err(tmp_path)
    (tmp_path / "b.props").write_text("<Project><Unclosed></Project>")
    _scan_err(tmp_path)


@pytest.mark.parametrize(
    ("fname", "content"),
    [
        ("pyproject.toml", '[tool.poetry.dependencies]\nfoo = {git = "https://x/y"}\n'),
        ("pyproject.toml", '[tool.poetry.dependencies]\nfoo = {url = "https://x/y.whl"}\n'),
        ("pyproject.toml", '[tool.poetry.dependencies]\nfoo = {path = "../../out"}\n'),
        ("pyproject.toml", '[tool.poetry.dependencies]\nfoo = [{git = "https://x/y"}]\n'),
        ("pyproject.toml", '[tool.uv.sources]\nfoo = {git = "https://x/y"}\n'),
        ("pyproject.toml", '[[tool.uv.index]]\nurl = "https://evil/simple"\n'),
        ("pyproject.toml", '[[tool.poetry.source]]\nname = "e"\nurl = "https://evil"\n'),
        ("Cargo.toml", '[dependencies]\nfoo = {git = "https://x/y"}\n'),
        ("Cargo.toml", '[dependencies]\nfoo = {version = "1", registry = "evil"}\n'),
        ("Cargo.toml", '[dependencies]\nfoo = {version = "1", registry-index = "https://e"}\n'),
        ("Cargo.toml", '[patch."https://evil"]\nfoo = {version = "1"}\n'),
    ],
)  # fmt: skip
def test_denylist_non_registry_sources_rejected(tmp_path: Path, fname: str, content: str) -> None:
    (tmp_path / fname).write_text(content)
    _scan_err(tmp_path)


def test_denylist_clean_manifests_still_pass(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.13"\nfoo = "^1"\nbar = {path = "sub"}\n'
    )
    (tmp_path / "Cargo.toml").write_text('[dependencies]\nserde = {version = "1"}\n')
    (tmp_path / "package.json").write_text('{"dependencies": {"a": "^1.2.3"}}')
    (tmp_path / "x.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Newtonsoft.Json" Version="13.0.1"/></ItemGroup></Project>'
    )
    (tmp_path / "requirements.txt").write_text("ok==1 --hash=sha256:ab\n--require-hashes\n")
    assert _names(tmp_path) == []
