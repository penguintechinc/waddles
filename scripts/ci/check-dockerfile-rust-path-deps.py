#!/usr/bin/env python3
"""Static check that every same-repo Cargo `path = "../x"` dependency a Rust
Dockerfile builds is actually `COPY`-ed into that Dockerfile's build context.

Regression gate for the recurring failure class hit on gh-425, gh-498,
gh-465, gh-468, gh-469: `core/svc_action/Dockerfile.rust` (and siblings)
declare same-repo path dependencies (`bundle-active-set`, `bundle-host-http`,
which itself path-depends on `egress-assertion`) that must be copied into
the Docker build context or `cargo build` fails closed with "unable to
update ../egress_assertion" (no such file) INSIDE the build -- a failure
that costs a full CI round trip to discover, one Dockerfile at a time. This
script proves the fix statically, before Docker ever runs, by walking each
crate's `Cargo.toml` `path = ...` dependencies TRANSITIVELY and asserting
every resolved directory is reachable from a `COPY` instruction in the
Dockerfile that builds it.

Build context resolution: read the *building* workflow (`.github/workflows/
build-*.yml`) that references each Dockerfile.rust's `file:` and pull its
`context:` value (resolving `${{ env.X }}` from that same workflow's own
`env:` block). No matching workflow reference -- e.g. a Dockerfile with no
build workflow yet -- falls back to the Dockerfile's own directory (the
narrowest possible context, so a missing COPY is never accidentally masked
by an overly generous default).

Exit code is the gate: zero Dockerfile.rust files examined is a hard
failure, never a silent pass (critical-rules.md Verification Integrity).
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DOCKERFILE_GLOB = "core/**/Dockerfile.rust"
COPY_RE = re.compile(r"^\s*COPY\s+(.*)$", re.IGNORECASE)
ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\S.*?)\s*$")


@dataclass
class DockerfileFinding:
    """One examined Dockerfile.rust's resolved context, deps, and gaps."""

    dockerfile: Path
    context_dir: Path
    context_source: str
    path_deps: list[Path] = field(default_factory=list)
    missing: list[Path] = field(default_factory=list)


def parse_copy_sources(dockerfile_text: str) -> list[str]:
    """Returns every non-`--from=` COPY source token across the Dockerfile.

    Multi-stage `COPY --from=builder ...` lines copy build *output*, not
    source tree, and are never relevant to path-dependency coverage.
    """
    sources: list[str] = []
    for line in dockerfile_text.splitlines():
        match = COPY_RE.match(line)
        if not match:
            continue
        rest = match.group(1).strip()
        if "--from=" in rest:
            continue
        tokens = rest.split()
        if len(tokens) < 2:
            continue
        # Last token is the destination; everything before is source(s).
        sources.extend(tokens[:-1])
    return sources


def _repo_relative(path: Path, repo_root: Path) -> Path:
    return path.resolve().relative_to(repo_root.resolve())


def resolve_build_context(
    dockerfile_rel: Path, repo_root: Path, workflows_dir: Path
) -> tuple[Path, str]:
    """Finds the `context:` a `build-push-action` step uses for this Dockerfile.

    Scans every workflow YAML for a `file:` value referencing this
    Dockerfile (literal, or via a `${{ env.X }}/Dockerfile.rust` pattern
    whose `env.X` resolves to this Dockerfile's parent dir), then returns the
    nearest preceding `context:` value in the same step block, resolving one
    level of `${{ env.NAME }}` substitution from that workflow's top-level
    `env:` block. Falls back to the Dockerfile's own directory.
    """
    dockerfile_dir_name = dockerfile_rel.parent.name
    for workflow in sorted(workflows_dir.glob("*.yml")):
        text = workflow.read_text()
        if "Dockerfile.rust" not in text:
            continue
        env_vars: dict[str, str] = {}
        lines = text.splitlines()
        in_env_block = False
        env_indent = None
        for line in lines:
            stripped = line.strip()
            if stripped == "env:":
                in_env_block = True
                env_indent = len(line) - len(line.lstrip())
                continue
            if in_env_block:
                indent = len(line) - len(line.lstrip())
                if stripped and indent <= env_indent:
                    in_env_block = False
                    continue
                m = ENV_LINE_RE.match(line)
                if m:
                    env_vars[m.group(1)] = m.group(2)

        def substitute(value: str) -> str:
            def repl(m: re.Match[str]) -> str:
                name = m.group(1)
                return env_vars.get(name, m.group(0))

            return re.sub(r"\$\{\{\s*env\.([A-Za-z0-9_]+)\s*\}\}", repl, value)

        file_val = None
        context_val = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("file:"):
                file_val = substitute(stripped[len("file:") :].strip())
            elif stripped.startswith("context:"):
                context_val = substitute(stripped[len("context:") :].strip())
            if file_val and file_val.endswith("Dockerfile.rust"):
                file_dir = file_val.rsplit("/Dockerfile.rust", 1)[0].strip("./")
                if file_dir.split("/")[-1] == dockerfile_dir_name and context_val:
                    return repo_root / context_val, f"{workflow.name}:context={context_val}"
    return dockerfile_rel.parent, "fallback=dockerfile-directory"


def parse_path_deps(cargo_toml: Path) -> list[Path]:
    """Returns absolute directories declared via `path = "..."` in one Cargo.toml.

    Scans `[dependencies]` and `[build-dependencies]` only -- `cargo build
    --release` (what every Dockerfile.rust actually runs) never resolves
    `[dev-dependencies]`, and no Dockerfile in this repo COPYs a dev-only
    path dependency, so including them would produce false positives.
    """
    data = tomllib.loads(cargo_toml.read_text())
    deps: list[Path] = []
    for section in ("dependencies", "build-dependencies"):
        for spec in data.get(section, {}).values():
            if isinstance(spec, dict) and "path" in spec:
                deps.append((cargo_toml.parent / spec["path"]).resolve())
    return deps


def transitive_path_deps(crate_dir: Path) -> list[Path]:
    """BFS over `path = ...` deps starting at `crate_dir/Cargo.toml`.

    Returns the deduplicated set of dependency crate directories reached,
    NOT including `crate_dir` itself (the primary crate is always assumed
    present -- it's the thing the Dockerfile's own `COPY .../src` line
    copies, never in question here).
    """
    seen: set[Path] = set()
    queue: list[Path] = [crate_dir.resolve()]
    result: list[Path] = []
    while queue:
        current = queue.pop(0)
        cargo_toml = current / "Cargo.toml"
        if not cargo_toml.is_file():
            continue
        for dep_dir in parse_path_deps(cargo_toml):
            if dep_dir in seen:
                continue
            seen.add(dep_dir)
            result.append(dep_dir)
            queue.append(dep_dir)
    return result


def is_copied(dep_dir: Path, context_dir: Path, copy_sources: list[str]) -> bool:
    """True if `dep_dir` is reachable via one of the Dockerfile's COPY sources.

    A directory is "covered" if some COPY source, resolved relative to the
    build context, is exactly `dep_dir`'s context-relative path (whole-tree
    copy of the dependency's own directory).
    """
    try:
        rel = dep_dir.resolve().relative_to(context_dir.resolve())
    except ValueError:
        return False  # dep dir isn't even reachable from this context
    rel_str = str(rel)
    for source in copy_sources:
        normalized = source.strip().lstrip("./")
        if normalized.rstrip("/") == rel_str:
            return True
    return False


def main() -> int:
    """CLI entry point. Prints per-Dockerfile findings; exit 1 on any gap."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", type=Path)
    args = parser.parse_args()
    repo_root: Path = args.repo_root.resolve()
    workflows_dir = repo_root / ".github" / "workflows"

    dockerfiles = sorted(repo_root.glob(DOCKERFILE_GLOB))
    if not dockerfiles:
        print(
            f"::error::0 Dockerfile.rust files found under {DOCKERFILE_GLOB} -- "
            "scanner is pointed at the wrong path or the pattern is stale.",
            file=sys.stderr,
        )
        return 1

    findings: list[DockerfileFinding] = []
    for dockerfile in dockerfiles:
        crate_dir = dockerfile.parent
        context_dir, context_source = resolve_build_context(
            _repo_relative(dockerfile, repo_root), repo_root, workflows_dir
        )
        copy_sources = parse_copy_sources(dockerfile.read_text())
        path_deps = transitive_path_deps(crate_dir)
        missing = [d for d in path_deps if not is_copied(d, context_dir, copy_sources)]
        findings.append(
            DockerfileFinding(
                dockerfile=dockerfile,
                context_dir=context_dir,
                context_source=context_source,
                path_deps=path_deps,
                missing=missing,
            )
        )

    total_deps_examined = sum(len(f.path_deps) for f in findings)
    print(
        f"Examined {len(dockerfiles)} Dockerfile.rust file(s), "
        f"{total_deps_examined} transitive path-dependency edge(s) total."
    )

    failed = False
    for finding in findings:
        rel_dockerfile = _repo_relative(finding.dockerfile, repo_root)
        if not finding.path_deps:
            print(f"  OK   {rel_dockerfile}: no same-repo path dependencies")
            continue
        dep_names = ", ".join(sorted(d.name for d in finding.path_deps))
        if finding.missing:
            failed = True
            missing_names = ", ".join(sorted(d.name for d in finding.missing))
            print(
                f"  FAIL {rel_dockerfile} (context={finding.context_source}): "
                f"path deps [{dep_names}] -- MISSING from build context: {missing_names}"
            )
        else:
            print(
                f"  OK   {rel_dockerfile} (context={finding.context_source}): "
                f"path deps [{dep_names}] all COPYed"
            )

    if failed:
        print(
            "::error::One or more Dockerfile.rust builds are missing a COPY for a "
            "transitive same-repo path dependency -- see FAIL lines above.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
