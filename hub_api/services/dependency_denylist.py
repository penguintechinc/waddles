"""Supply-chain denylist scan of a bundle's declared dependencies (Phase 1, Task 3).

general.md forbids PRC-origin / sanctioned-entity dependencies. This extracts declared package
names from requirements*.txt, pyproject.toml, Cargo.toml/lock, package.json/lock and *.csproj
inside one bundle directory and matches them against `supply_chain_denylist.yaml`. The list is
data, not code: extend the YAML. Fail-loud if the list cannot be loaded (never silently empty).
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_DENYLIST_PATH = Path(__file__).with_name("supply_chain_denylist.yaml")
_MAX_FILE_BYTES = 5 * 1024 * 1024
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.ASCII)
_CSPROJ_RE = re.compile(r"<PackageReference\s+[^>]*?Include\s*=\s*\"([^\"]+)\"", re.I)


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


def _read(path: Path) -> str | None:
    """Read a manifest file if it is a regular, bounded-size file (never follows symlinks)."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _py_names(text: str) -> list[str]:
    """Package names from a requirements-style text."""
    names = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        if not stripped.strip() or stripped.lstrip().startswith("-"):
            continue
        m = _REQ_NAME_RE.match(stripped)
        if m:
            names.append(m.group(1))
    return names


def _toml_deps(doc: dict[str, Any], ecosystem: str) -> list[str]:
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
        poetry = doc.get("tool", {}).get("poetry", {})
        names += [k for k in poetry.get("dependencies", {}) if k != "python"]
        return names

    def walk(table: dict[str, Any]) -> None:
        for key in ("dependencies", "dev-dependencies", "build-dependencies"):
            for dep_name, spec in table.get(key, {}).items():
                names.append(
                    spec["package"] if isinstance(spec, dict) and "package" in spec else dep_name
                )
        for tgt in table.get("target", {}).values():
            if isinstance(tgt, dict):
                walk(tgt)

    walk(doc)
    walk(doc.get("workspace", {}))
    return names


def _npm_names(doc: dict[str, Any]) -> list[str]:
    """Names from package.json / package-lock.json."""
    names: list[str] = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        names += list(doc.get(key, {}))
    for pkg_path in doc.get("packages", {}):
        if "node_modules/" in pkg_path:
            names.append(pkg_path.rsplit("node_modules/", 1)[1])
    return names


def scan_bundle_dependencies(bundle_dir: Path) -> tuple[DepFinding, ...]:
    """Scan every dependency manifest under `bundle_dir`; returns all denylist findings.

    Unparseable manifests raise `ValueError` (fail closed: a manifest we cannot read could be
    hiding a banned dependency).
    """
    findings: list[DepFinding] = []

    def record(eco: str, names: list[str], rel: str) -> None:
        for name in names:
            pattern = match_denylist(eco, name)
            if pattern is not None:
                findings.append(DepFinding(eco, name, rel, pattern))

    for path in sorted(bundle_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        name = path.name
        rel = str(path.relative_to(bundle_dir))
        lower = name.lower()
        is_req = lower.startswith("requirements") and lower.endswith(".txt")
        if not (
            is_req
            or lower
            in {"pyproject.toml", "cargo.toml", "cargo.lock", "package.json", "package-lock.json"}
            or lower.endswith(".csproj")
        ):
            continue
        text = _read(path)
        if text is None:
            raise ValueError(f"unreadable or oversized dependency manifest: {rel}")
        try:
            if is_req:
                record("pypi", _py_names(text), rel)
            elif lower == "pyproject.toml":
                record("pypi", _toml_deps(tomllib.loads(text), "pypi"), rel)
            elif lower == "cargo.toml":
                record("cargo", _toml_deps(tomllib.loads(text), "cargo"), rel)
            elif lower == "cargo.lock":
                pkgs = tomllib.loads(text).get("package", [])
                record("cargo", [p["name"] for p in pkgs if "name" in p], rel)
            elif lower in {"package.json", "package-lock.json"}:
                record("npm", _npm_names(json.loads(text)), rel)
            else:
                record("nuget", _CSPROJ_RE.findall(text), rel)
        except (tomllib.TOMLDecodeError, json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise ValueError(f"malformed dependency manifest: {rel}") from exc
    return tuple(findings)
