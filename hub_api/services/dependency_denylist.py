"""Supply-chain denylist scan of a bundle's declared dependencies (Phase 1, Task 3).

general.md forbids PRC-origin / sanctioned-entity dependencies. This extracts declared package
names from requirements*.txt, pyproject.toml, Cargo.toml/lock, package.json/lock and *.csproj
inside one bundle directory and matches them against `supply_chain_denylist.yaml`. The list is
data, not code: extend the YAML. Fail-loud if the list cannot be loaded (never silently empty).

Scope. DOES cover: the declared-dependency forms of the manifests above (incl. npm aliases,
overrides/resolutions, pip -r/-c includes, uv/poetry/cargo/MSBuild specifics such as
PackageDownload and Sdk ids); registry pinning (git/url/path/custom-index/registry sources,
off-registry lockfile `resolved`/`source` entries and out-of-bundle `link:` entries are
rejected); and fail-closed behaviour on anything unsupported or unparseable (unknown manifests
like yarn.lock/go.mod, and package-manager config files that can redirect sources such as
.npmrc, .cargo/config.toml, pip.conf, uv.toml, nuget.config reject the whole bundle).

Does NOT cover: transitive dependencies absent from a lockfile, binary/vendored code, or
dependencies fetched at runtime. This is DEFENSE-IN-DEPTH, not the sole supply-chain gate: it
sits behind human platform:admin approval, the WASM sandbox, publisher attestation and
namespace isolation.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

_DENYLIST_PATH = Path(__file__).with_name("supply_chain_denylist.yaml")
_MAX_FILE_BYTES = 5 * 1024 * 1024
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.ASCII)

_MAX_INCLUDE_DEPTH = 8
_SAFE_REQ_OPTS = frozenset(
    {
        "--hash", "--no-binary", "--only-binary", "--prefer-binary",
        "--require-hashes", "--no-deps", "--no-index", "--use-feature",
    }
)  # fmt: skip
_REQ_ARG_OPTS = frozenset({"--hash", "--no-binary", "--only-binary", "--use-feature"})
_REQ_INCLUDE_SHORT = {"-r", "-c"}
_REQ_INCLUDE_LONG = {"--requirement", "--constraint"}
_NUGET_SUFFIXES = (".csproj", ".fsproj", ".vbproj", ".props", ".targets")
_NUGET_ELEMENTS = frozenset(
    {
        "packagereference", "packageversion", "globalpackagereference", "dotnetclitoolreference",
        "packagedownload", "frameworkreference",
    }
)  # fmt: skip
_NUGET_BAD_PROPS = frozenset(
    {"restoresources", "restoreadditionalprojectsources", "restorefallbackfolders"}
)
# Package-manager config files that can redirect sources: presence rejects the bundle.
_CONFIG_FILES = frozenset(
    {"uv.toml", "pip.conf", "pip.ini", ".npmrc", ".yarnrc", ".yarnrc.yml", "poetry.toml",
     "nuget.config"}
)  # fmt: skip
_CARGO_CONFIG_NAMES = frozenset({"config", "config.toml"})
_NPM_REGISTRY = "https://registry.npmjs.org/"
_CARGO_SOURCES = frozenset(
    {"registry+https://github.com/rust-lang/crates.io-index", "sparse+https://index.crates.io/"}
)
_PYPI_REGISTRIES = frozenset({"https://pypi.org/simple", "https://pypi.org/simple/"})
_NUGET_ATTRS = frozenset({"include", "update"})
_TOML_FILES = {"pyproject.toml", "cargo.toml", "cargo.lock", "poetry.lock", "uv.lock"}
_JSON_FILES = {"package.json", "package-lock.json", "npm-shrinkwrap.json"}
# Dependency manifests/lockfiles we cannot parse: their presence rejects the bundle (fail closed).
_UNSUPPORTED = {
    "yarn.lock", "pnpm-lock.yaml", "pipfile", "pipfile.lock", "setup.py", "setup.cfg", "go.mod",
    "go.sum", "gemfile", "gemfile.lock", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "composer.lock", "bun.lockb", "bun.lock", "pdm.lock", "environment.yml",
}  # fmt: skip
_SEMVER_CMP_RE = re.compile(
    r"^(?:[<>]=?|=|~>?|\^)?v?(?:\d+|[xX*])(?:\.(?:\d+|[xX*])){0,2}"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$",
    re.ASCII,
)
_NPM_TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$", re.ASCII)
_NPM_NAME_RE = re.compile(r"^(?:@[a-z0-9._~-]+/)?[a-z0-9][a-z0-9._~-]*$", re.I | re.ASCII)
_CARGO_BAD_KEYS = frozenset({"git", "registry", "registry-index"})
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


def _is_config_file(path: Path) -> bool:
    """True for a source-redirecting package-manager config (path-aware for `.cargo/config*`)."""
    lower = path.name.lower()
    if lower in _CONFIG_FILES:
        return True
    return lower in _CARGO_CONFIG_NAMES and path.parent.name.lower() == ".cargo"


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


def _req_option_targets(line: str, rel: str) -> list[str]:
    """Tokenise one pip option line; return every -r/-c target, reject any unsafe option.

    Every option token must be a safe flag (include or in `_SAFE_REQ_OPTS`); anything else
    (-e, -i, --index-url, --extra-index-url, -f, --find-links, --trusted-host, --pre ...)
    rejects the bundle, so a safe option cannot smuggle an unsafe one onto the same line.
    """
    try:
        tokens = shlex.split(line.split(" #", 1)[0], comments=False, posix=True)
    except ValueError as exc:
        raise ValueError(f"unparseable requirements option line in {rel}") from exc
    targets: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        flag, eq, val = tok.partition("=")
        if flag in _REQ_INCLUDE_LONG or flag in _REQ_INCLUDE_SHORT:
            if not eq:
                if i >= len(tokens):
                    raise ValueError(f"unresolvable requirements include in {rel}")
                val = tokens[i]
                i += 1
        elif tok[:2] in _REQ_INCLUDE_SHORT and not tok.startswith("--"):
            val = tok[2:]  # -rfile.txt / -cfile.txt
        elif flag in _SAFE_REQ_OPTS:
            if flag in _REQ_ARG_OPTS and not eq:
                i += 1  # value token belongs to the option
            continue
        else:
            raise ValueError(f"unsupported requirements option in {rel}")
        if not val or "://" in val:
            raise ValueError(f"unresolvable requirements include in {rel}")
        targets.append(val)
    return targets


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
            for target in _req_option_targets(line, rel):
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
        if _CARGO_BAD_KEYS & spec.keys():
            raise ValueError("non-registry cargo dependency (git/registry source)")
        names.append(str(spec.get("package", dep_name)))
        if "path" in spec:
            _within(root, manifest_dir / str(spec["path"]))
    else:
        names.append(dep_name)


_PY_BAD_SPEC_KEYS = frozenset({"git", "url", "source", "rev", "branch", "tag"})
_UV_BAD_KEYS = frozenset({"sources", "index", "index-url", "extra-index-url", "find-links"})


def _poetry_spec(spec: Any, manifest_dir: Path, root: Path) -> None:
    """Reject poetry dependency specs that leave the registry (git/url/source; path in-bundle)."""
    if not isinstance(spec, dict):
        return
    if _PY_BAD_SPEC_KEYS & spec.keys():
        raise ValueError("non-registry poetry dependency (git/url/custom source)")
    if "path" in spec:
        _within(root, manifest_dir / str(spec["path"]))


def _has_bad_key(obj: Any) -> bool:
    """True if any (recursively nested) mapping key is a custom index/source key."""
    if isinstance(obj, dict):
        return bool(_UV_BAD_KEYS & obj.keys()) or any(_has_bad_key(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_bad_key(v) for v in obj)
    return False


def _reject_py_sources(tool: dict[str, Any]) -> None:
    """Fail closed on custom indexes/sources in `[tool.uv]` and `[tool.poetry.source]`."""
    uv = tool.get("uv", {})
    if isinstance(uv, dict) and (_UV_BAD_KEYS & uv.keys() or _has_bad_key(uv.get("pip", {}))):
        raise ValueError("custom uv index/source configuration")
    if tool.get("poetry", {}).get("source"):
        raise ValueError("custom poetry package source")


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
        uv_tbl = tool.get("uv", {})
        for key in ("dev-dependencies", "override-dependencies", "constraint-dependencies"):
            for dep in [*uv_tbl.get(key, []), *uv_tbl.get("pip", {}).get(key, [])]:
                names += _py_names(str(dep))
        _reject_py_sources(tool)
        poetry = tool.get("poetry", {})
        tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
        tables += [g.get("dependencies", {}) for g in poetry.get("group", {}).values()]
        for table in tables:
            for key, spec in table.items():
                if key == "python":
                    continue
                for variant in spec if isinstance(spec, list) else [spec]:
                    _poetry_spec(variant, manifest_dir, root)
                names.append(key)
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
    for registry, registry_table in doc.get("patch", {}).items():
        if registry != "crates-io":
            raise ValueError("cargo patch from a non-crates.io registry")
        for dep_name, spec in registry_table.items():
            names.append(dep_name)
            _cargo_spec(dep_name, spec, manifest_dir, root, names)
    for key, spec in doc.get("replace", {}).items():
        _cargo_spec(key.split(":", 1)[0].split("@", 1)[0], spec, manifest_dir, root, names)
    return names


def _check_lock_sources(kind: str, pkgs: list[Any], base: Path, root: Path) -> None:
    """Reject Cargo/uv/poetry lock packages sourced from anything but the default registry."""
    for pkg in pkgs:
        src = pkg.get("source")
        if src is None:
            continue
        if kind == "cargo.lock":
            ok = src in _CARGO_SOURCES
        elif kind == "uv.lock":
            ok = _uv_source_ok(src, base, root)
        else:  # poetry.lock: [package.source] table; only in-bundle directory/file allowed
            ok = isinstance(src, dict) and src.get("type") in {"directory", "file"}
            if ok:
                _within(root, base / str(src.get("url", "")))
        if not ok:
            raise ValueError("lockfile package from a non-default registry/source")


def _uv_source_ok(src: Any, base: Path, root: Path) -> bool:
    """True for the default PyPI registry or an in-bundle virtual/editable/directory source."""
    if not isinstance(src, dict):
        return False
    if set(src) == {"registry"}:
        return src["registry"] in _PYPI_REGISTRIES
    if len(src) == 1 and next(iter(src)) in {"virtual", "editable", "directory"}:
        _within(root, base / str(next(iter(src.values()))))
        return True
    return False


def _npm_range_ok(rng: str) -> bool:
    """True iff `rng` is a strict semver range (comparators, hyphen ranges, `||`) or empty."""
    rng = rng.strip()
    if not rng:
        return True
    for alt in rng.split("||"):
        toks = alt.split()
        if not toks:
            return False
        for idx, tok in enumerate(toks):
            hyphen = tok == "-" and 0 < idx < len(toks) - 1
            if not hyphen and not _SEMVER_CMP_RE.match(tok):
                return False
    return True


def _npm_spec_ok(spec: str) -> bool:
    """Allowlist: semver range, dist-tag, or `npm:<name>@<range>` alias; nothing else."""
    spec = spec.strip()
    if spec.startswith("npm:"):
        real = spec[4:]
        at = real.rfind("@")
        name, rng = (real[:at], real[at + 1 :]) if at > 0 else (real, "")
        return bool(_NPM_NAME_RE.match(name)) and _npm_range_ok(rng)
    return _npm_range_ok(spec) or bool(_NPM_TAG_RE.match(spec))


def _npm_spec(name: str, spec: str, names: list[str]) -> None:
    """Record a dependency name, resolving `npm:` aliases; reject any non-allowlisted spec."""
    names.append(name)
    if not _npm_spec_ok(spec):
        raise ValueError("non-registry or unrecognised npm dependency spec")
    spec = spec.strip()
    if spec.startswith("npm:"):
        real = spec[4:]
        at = real.rfind("@")
        names.append(real[:at] if at > 0 else real)


def _npm_overrides(table: Any, parent: str, names: list[str]) -> None:
    """Recursively walk `overrides`/`resolutions`: every string leaf is a spec (key `.` too)."""
    if isinstance(table, str):
        if table.startswith("$") and _NPM_NAME_RE.match(table[1:]):
            return  # reference to a direct dependency's own (already checked) spec
        _npm_spec(parent, table, names)
        return
    if not isinstance(table, dict):
        raise ValueError("malformed npm overrides entry")
    for key, val in table.items():
        at = key.find("@", 1 if key.startswith("@") else 0)
        name = key[:at] if at > 0 else key
        # resolutions keys may be glob paths ("a/**/b"): every segment is a package name
        segs = re.split(r"/?\*\*/?", name)
        if name != ".":
            names += [x for x in segs if x]
        _npm_overrides(val, parent if name == "." else name, names)


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
            resolved = val.get("resolved")
            if resolved is not None and not (
                isinstance(resolved, str)
                and (val.get("link") is True or resolved.startswith(_NPM_REGISTRY))
            ):
                raise ValueError("npm lockfile entry resolved outside the npm registry")
            if isinstance(val.get("version"), str):
                _npm_spec(key, val["version"], names)
            if isinstance(val.get("name"), str):
                names.append(val["name"])
            for sub in ("dependencies", "requires", "optionalDependencies", "peerDependencies"):
                _npm_map(val.get(sub), names)


def _npm_names(doc: dict[str, Any], base: Path, root: Path) -> list[str]:
    """Names from package.json / package-lock.json, incl. aliases, `name` fields, overrides."""
    names: list[str] = []
    for key in (
        "dependencies", "devDependencies", "peerDependencies", "optionalDependencies",
    ):  # fmt: skip
        _npm_map(doc.get(key), names)
    for key in ("overrides", "resolutions"):
        if key in doc:
            _npm_overrides(doc[key], "_root", names)
    names += [str(n) for n in doc.get("bundledDependencies", []) if isinstance(n, str)]
    for pkg_path, entry in doc.get("packages", {}).items():
        if "node_modules/" in pkg_path:
            names.append(pkg_path.rsplit("node_modules/", 1)[1])
        if isinstance(entry, dict):
            if entry.get("link") is True:
                target = entry.get("resolved")
                if not isinstance(target, str) or not target:
                    raise ValueError("npm lockfile link entry without a target")
                _within(root, base / target)
            _npm_map({pkg_path.rsplit("node_modules/", 1)[-1] or "_root": entry}, names)
    return names


def _local(tag: str) -> str:
    """Lower-cased local name of an XML tag, namespace stripped."""
    return tag.rsplit("}", 1)[-1].lower()


def _nuget_names(text: str, lower: str, path: Path, root: Path) -> list[str]:
    """Package ids from csproj-style XML (defusedxml) or packages.config.

    `<Import>` is rejected unless it targets a plain in-bundle .props/.targets file (which the
    caller scans as its own manifest); anything with a property, `Sdk=` or outside the bundle fails.
    """
    try:
        tree = DefusedET.fromstring(text)
    except (DefusedXmlException, SyntaxError) as exc:
        raise ValueError("malformed or unsafe XML dependency manifest") from exc
    found: list[str] = []
    for el in tree.iter():
        tag = _local(el.tag) if isinstance(el.tag, str) else ""
        attrs = {_local(k): v for k, v in el.attrib.items()}
        if lower == "packages.config":
            if tag == "package" and "id" in attrs:
                found.append(attrs["id"].strip())
        elif tag in _NUGET_BAD_PROPS:
            raise ValueError("custom NuGet restore source configuration")
        elif tag == "project" and "sdk" in attrs:
            found += [x.strip().split("/", 1)[0] for x in attrs["sdk"].split(";") if x.strip()]
        elif tag == "sdk" and "name" in attrs:
            found.append(attrs["name"].strip().split("/", 1)[0])
        elif tag in _NUGET_ELEMENTS:
            for key in _NUGET_ATTRS & attrs.keys():
                found += [x.strip() for x in attrs[key].split(";") if x.strip()]
        elif tag == "import":
            target = attrs.get("project", "").strip().replace("\\", "/")
            if (
                "sdk" in attrs
                or not target
                or "$(" in target
                or not target.lower().endswith((".props", ".targets"))
            ):
                raise ValueError("unsupported MSBuild <Import>")
            _within(root, path.parent / target)
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
        if _is_config_file(path):
            raise ValueError(f"unsupported package-manager config file: {rel}")
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
                    _check_lock_sources(lower, pkgs, path.parent, root)
                    record(eco, [p["name"] for p in pkgs if "name" in p], rel)
                else:
                    eco = "pypi" if lower == "pyproject.toml" else "cargo"
                    record(eco, _toml_deps(doc, eco, path.parent, root), rel)
            elif lower in _JSON_FILES:
                record("npm", _npm_names(json.loads(text), path.parent, root), rel)
            else:
                record("nuget", _nuget_names(text, lower, path, root), rel)
        except (tomllib.TOMLDecodeError, json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise ValueError(f"malformed dependency manifest: {rel}") from exc
        except (KeyError, ValueError) as exc:
            if isinstance(exc, ValueError) and rel in str(exc):
                raise
            raise ValueError(f"unsafe or malformed dependency manifest: {rel}") from exc
    return tuple(findings)
