"""Supply-chain denylist scan of a bundle's declared dependencies (Phase 1, Task 3).

general.md forbids PRC-origin / sanctioned-entity dependencies. This extracts declared package
names from requirements*.txt, pyproject.toml, Cargo.toml/lock, package.json/lock and *.csproj
inside one bundle directory and matches them against `supply_chain_denylist.yaml`. The list is
data, not code: extend the YAML. Fail-loud if the list cannot be loaded (never silently empty).
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_DENYLIST_PATH = Path(__file__).with_name("supply_chain_denylist.yaml")
_MAX_FILE_BYTES = 5 * 1024 * 1024
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.ASCII)

_MAX_INCLUDE_DEPTH = 8
_SAFE_REQ_OPTS = frozenset(
    {
        "--hash", "--no-binary", "--only-binary", "--prefer-binary", "--pre",
        "--require-hashes", "--no-deps", "--no-index", "--use-feature",
    }
)  # fmt: skip
_REQ_INCLUDE_PREFIXES = ("--requirement", "--constraint", "-r", "-c")
_NUGET_SUFFIXES = (".csproj", ".fsproj", ".vbproj", ".props", ".targets")
_NUGET_TAG_RE = re.compile(
    r"<(?:PackageReference|PackageVersion|GlobalPackageReference|DotNetCliToolReference)\b([^>]*)>",
    re.I,
)
_NUGET_ATTR_RE = re.compile(r"\b(?:Include|Update)\s*=\s*([\"'])(.*?)\1", re.I | re.S)
_PKGCONFIG_RE = re.compile(r"<package\b[^>]*?\bid\s*=\s*([\"'])(.*?)\1", re.I | re.S)
_TOML_FILES = {"pyproject.toml", "cargo.toml", "cargo.lock", "poetry.lock", "uv.lock"}
_JSON_FILES = {"package.json", "package-lock.json", "npm-shrinkwrap.json"}
# Dependency manifests/lockfiles we cannot parse: their presence rejects the bundle (fail closed).
_UNSUPPORTED = {
    "yarn.lock", "pnpm-lock.yaml", "pipfile", "pipfile.lock", "setup.py", "setup.cfg", "go.mod",
    "go.sum", "gemfile", "gemfile.lock", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "composer.lock", "bun.lockb", "bun.lock", "pdm.lock", "environment.yml",
}  # fmt: skip
_NPM_BAD_SPEC_RE = re.compile(r"[:/\\]|^\s*[.~]")
Recorder = Callable[[str, list[str], str], None]


@dataclass(slots=True, frozen=True)
class DepFinding:
    """One denylisted dependency found in a bundle."""

    ecosystem: str
    package: str
    file: str
    pattern: str


def _load_denylist() -> dict[str, tuple[str, ...]]:
    """Load the curated denylist YAML; raises if missing/empty (no silent empty list)."""
    raw = yaml.safe_load(_DENYLIST_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise RuntimeError("supply_chain_denylist.yaml is empty or malformed")
    return {eco: tuple(str(p).lower() for p in pats) for eco, pats in raw.items()}


_DENYLIST = _load_denylist()


def _norm(ecosystem: str, name: str) -> str:
    """Normalise a package name for matching (PEP 503 for pypi, lowercase elsewhere)."""
    lowered = name.strip().lower()
    return re.sub(r"[-_.]+", "-", lowered) if ecosystem == "pypi" else lowered


def match_denylist(ecosystem: str, name: str) -> str | None:
    """Return the matching denylist pattern for `name`, or None."""
    candidate = _norm(ecosystem, name)
    for pattern in _DENYLIST.get(ecosystem, ()):
        pat = _norm(ecosystem, pattern.rstrip("*")) + ("*" if pattern.endswith("*") else "")
        if pat.endswith("*"):
            if candidate.startswith(pat[:-1]):
                return pattern
        elif candidate == pat:
            return pattern
    return None


def _is_manifest_name(lower: str) -> bool:
    """True for any file name we treat as a dependency manifest/lockfile (supported or not)."""
    return (
        (lower.startswith(("requirements", "constraints")) and lower.endswith(".txt"))
        or lower in _TOML_FILES
        or lower in _JSON_FILES
        or lower in _UNSUPPORTED
        or lower == "packages.config"
        or lower.endswith(_NUGET_SUFFIXES)
    )


def _read(path: Path, rel: str) -> str:
    """Read a regular, bounded-size manifest; anything else raises (fail closed)."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError(f"unreadable or oversized dependency manifest: {rel}")
    return path.read_text(encoding="utf-8", errors="replace")


def _py_req(line: str) -> str | None:
    """Package name from one requirement spec; direct URL/path references raise (fail closed)."""
    spec = line.split(" #", 1)[0].strip()
    if not spec or spec.startswith("#"):
        return None
    if "://" in spec or " @ " in spec or "@" in spec.split(";", 1)[0] or spec[0] in "./~\\":
        raise ValueError("non-registry python requirement")
    m = _REQ_NAME_RE.match(spec)
    if not m:
        raise ValueError("unparseable python requirement")
    return m.group(1)


def _py_names(text: str) -> list[str]:
    """Package names from requirements-style text (no include handling; see `_scan_req_file`)."""
    return [n for n in (_py_req(ln) for ln in text.splitlines()) if n]


def _within(root: Path, candidate: Path) -> Path:
    """Return `candidate` resolved iff it is a symlink-free path inside `root`; else raise."""
    resolved = candidate.resolve()
    if os.path.normpath(candidate) != str(resolved) or not resolved.is_relative_to(root):
        raise ValueError("dependency reference escapes bundle or traverses a symlink")
    return resolved


def _scan_req_file(
    path: Path, root: Path, record: Recorder, seen: set[Path], depth: int = 0
) -> None:
    """Scan a requirements file, recursively resolving -r/-c includes inside the bundle."""
    if depth > _MAX_INCLUDE_DEPTH:
        raise ValueError("requirements include depth exceeded")
    if path in seen:
        return
    seen.add(path)
    rel = str(path.relative_to(root))
    text = _read(path, rel).replace("\\\r\n", "").replace("\\\n", "")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            target = None
            for prefix in _REQ_INCLUDE_PREFIXES:
                rest = line[len(prefix) :]
                if line.startswith(prefix) and not (
                    prefix.startswith("--") and rest and rest[0] not in " \t="
                ):
                    target = rest.lstrip(" \t=").split(" #", 1)[0].strip()
                    break
            if target is None:
                if line.split("=", 1)[0].split()[0] in _SAFE_REQ_OPTS:
                    continue
                raise ValueError(f"unsupported requirements option in {rel}")
            if not target or "://" in target:
                raise ValueError(f"unresolvable requirements include in {rel}")
            inc = _within(root, path.parent / target)
            _scan_req_file(inc, root, record, seen, depth + 1)
            continue
        try:
            name = _py_req(line)
        except ValueError as exc:
            raise ValueError(f"{exc}: {rel}") from exc
        if name:
            record("pypi", [name], rel)


def _cargo_spec(dep_name: str, spec: Any, manifest_dir: Path, root: Path, names: list[str]) -> None:
    """Collect the real crate name of one Cargo dependency; reject out-of-bundle path deps."""
    if isinstance(spec, dict):
        names.append(str(spec.get("package", dep_name)))
        if "path" in spec:
            _within(root, manifest_dir / str(spec["path"]))
    else:
        names.append(dep_name)


def _toml_deps(doc: dict[str, Any], ecosystem: str, manifest_dir: Path, root: Path) -> list[str]:
    """Names from pyproject.toml / Cargo.toml dependency tables."""
    names: list[str] = []
    if ecosystem == "pypi":
        project = doc.get("project", {})
        for dep in project.get("dependencies", []):
            names += _py_names(str(dep))
        for group in project.get("optional-dependencies", {}).values():
            for dep in group:
                names += _py_names(str(dep))
        for group in doc.get("dependency-groups", {}).values():
            for dep in group:
                if isinstance(dep, str):
                    names += _py_names(dep)
        for dep in doc.get("build-system", {}).get("requires", []):
            names += _py_names(str(dep))
        tool = doc.get("tool", {})
        for dep in tool.get("uv", {}).get("dev-dependencies", []):
            names += _py_names(str(dep))
        poetry = tool.get("poetry", {})
        tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
        tables += [g.get("dependencies", {}) for g in poetry.get("group", {}).values()]
        for table in tables:
            names += [k for k in table if k != "python"]
        return names

    def walk(table: dict[str, Any]) -> None:
        for key in ("dependencies", "dev-dependencies", "build-dependencies"):
            for dep_name, spec in table.get(key, {}).items():
                _cargo_spec(dep_name, spec, manifest_dir, root, names)
        for tgt in table.get("target", {}).values():
            if isinstance(tgt, dict):
                walk(tgt)

    walk(doc)
    walk(doc.get("workspace", {}))
    for registry_table in doc.get("patch", {}).values():
        for dep_name, spec in registry_table.items():
            names.append(dep_name)
            _cargo_spec(dep_name, spec, manifest_dir, root, names)
    for key, spec in doc.get("replace", {}).items():
        _cargo_spec(key.split(":", 1)[0].split("@", 1)[0], spec, manifest_dir, root, names)
    return names


def _npm_spec(name: str, spec: str, names: list[str]) -> None:
    """Record a dependency name, resolving `npm:` aliases and flagging non-registry specs."""
    names.append(name)
    spec = spec.strip()
    if spec.startswith("npm:"):
        real = spec[4:]
        at = real.rfind("@")
        names.append(real[:at] if at > 0 else real)
    elif _NPM_BAD_SPEC_RE.search(spec):
        raise ValueError("non-registry npm dependency spec (git/url/file/link)")


def _npm_map(table: Any, names: list[str]) -> None:
    """Walk a name -> spec/entry mapping (package.json deps, lockfile deps, overrides)."""
    if not isinstance(table, dict):
        return
    for name, val in table.items():
        at = name.find("@", 1 if name.startswith("@") else 0)
        key = name[:at] if at > 0 else name
        if isinstance(val, str):
            _npm_spec(key, val, names)
        elif isinstance(val, dict):
            names.append(key)
            if isinstance(val.get("version"), str):
                _npm_spec(key, val["version"], names)
            if isinstance(val.get("name"), str):
                names.append(val["name"])
            for sub in ("dependencies", "requires", "optionalDependencies", "peerDependencies"):
                _npm_map(val.get(sub), names)


def _npm_names(doc: dict[str, Any]) -> list[str]:
    """Names from package.json / package-lock.json, incl. aliases, `name` fields, overrides."""
    names: list[str] = []
    for key in (
        "dependencies", "devDependencies", "peerDependencies", "optionalDependencies",
        "overrides", "resolutions",
    ):  # fmt: skip
        _npm_map(doc.get(key), names)
    names += [str(n) for n in doc.get("bundledDependencies", []) if isinstance(n, str)]
    for pkg_path, entry in doc.get("packages", {}).items():
        if "node_modules/" in pkg_path:
            names.append(pkg_path.rsplit("node_modules/", 1)[1])
        if isinstance(entry, dict):
            _npm_map({pkg_path.rsplit("node_modules/", 1)[-1] or "_root": entry}, names)
    return names


def _nuget_names(text: str, lower: str) -> list[str]:
    """Package ids from csproj-style files (any quotes; Include=/Update=) or packages.config."""
    if lower == "packages.config":
        found = [m.group(2) for m in _PKGCONFIG_RE.finditer(text)]
    else:
        found = [
            part.strip()
            for tag in _NUGET_TAG_RE.finditer(text)
            for attr in _NUGET_ATTR_RE.finditer(tag.group(1))
            for part in attr.group(2).split(";")
            if part.strip()
        ]
    if any("$(" in n for n in found):
        raise ValueError("unresolvable MSBuild property in package reference")
    return found


def scan_bundle_dependencies(bundle_dir: Path) -> tuple[DepFinding, ...]:
    """Scan every dependency manifest under `bundle_dir`; returns all denylist findings.

    Fail closed: unparseable/unsupported manifests, symlinked manifests (or directories),
    out-of-bundle path deps and out-of-bundle requirement includes raise `ValueError`.
    """
    root = bundle_dir.resolve()
    findings: list[DepFinding] = []
    seen_findings: set[DepFinding] = set()
    seen_reqs: set[Path] = set()

    def record(eco: str, names: list[str], rel: str) -> None:
        for name in names:
            pattern = match_denylist(eco, name)
            if pattern is not None:
                f = DepFinding(eco, name, rel, pattern)
                if f not in seen_findings:
                    seen_findings.add(f)
                    findings.append(f)

    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        lower = path.name.lower()
        if path.is_symlink():
            if _is_manifest_name(lower) or path.is_dir():
                raise ValueError(f"symlinked dependency manifest or directory: {rel}")
            continue
        if not path.is_file() or not _is_manifest_name(lower):
            continue
        if lower in _UNSUPPORTED:
            raise ValueError(f"unsupported dependency manifest: {rel}")
        try:
            if lower.startswith(("requirements", "constraints")) and lower.endswith(".txt"):
                _scan_req_file(path, root, record, seen_reqs)
                continue
            text = _read(path, rel)
            if lower in _TOML_FILES:
                doc = tomllib.loads(text)
                if lower in {"cargo.lock", "poetry.lock", "uv.lock"}:
                    eco = "cargo" if lower == "cargo.lock" else "pypi"
                    pkgs = doc.get("package", [])
                    record(eco, [p["name"] for p in pkgs if "name" in p], rel)
                else:
                    eco = "pypi" if lower == "pyproject.toml" else "cargo"
                    record(eco, _toml_deps(doc, eco, path.parent, root), rel)
            elif lower in _JSON_FILES:
                record("npm", _npm_names(json.loads(text)), rel)
            else:
                record("nuget", _nuget_names(text, lower), rel)
        except (tomllib.TOMLDecodeError, json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise ValueError(f"malformed dependency manifest: {rel}") from exc
        except (KeyError, ValueError) as exc:
            if isinstance(exc, ValueError) and rel in str(exc):
                raise
            raise ValueError(f"unsafe or malformed dependency manifest: {rel}") from exc
    return tuple(findings)
