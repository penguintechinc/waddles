#!/usr/bin/env python3
"""Codemod: make Python COMMAND (app) bundles activation-gated only, not feature-flag gated.

Agreed model: app bundles are gated by the platform's Global:installed -> Tenant:enabled ->
Community:activated chain (DB), NOT by a per-command PostHog flag. Feature flags stay only for
SYSTEM/health bundles. Before this codemod, every `bundles/python/<name>/src/app.py` self-gated
inside `transform()` on `waddles.command-<name>`; this removes that gate.

Per bundle it (1) classifies, (2) removes the `FLAG_KEY` const + `feature_enabled` import + the
hard guard in `transform()`, (3) optionally drops the `flags.read` permission from
`bundle.yaml`/`hub-manifest.yaml` (`--drop-flags-read`), (4) patch-bumps the version in every file
that carries it (digests are immutable per (app_id, version)), (5) updates the bundle README.
`--prune-tests` then runs each edited bundle's pytest suite and deletes the obsolete
"flag OFF -> no-op" tests (only tests that now FAIL and mention the flag; anything else is
reported for manual handling, never touched).

A bundle that does not match the expected shape is SKIPPED and reported -- never mangled.
The enumeration (guarded / system / skipped, with counts) is always printed first: a zero
denominator is a failure, not a pass.

Usage (via `make codemod-app-bundles-activation-only`, dry-run by default):
    python3 scripts/codemod_app_bundles_activation_only.py            # enumerate only
    python3 scripts/codemod_app_bundles_activation_only.py --apply [--drop-flags-read]
    python3 scripts/codemod_app_bundles_activation_only.py --prune-tests
    python3 scripts/codemod_app_bundles_activation_only.py --only about,eightball ...
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLES_DIR = REPO_ROOT / "bundles" / "python"
CATALOG = REPO_ROOT / "bundles" / "core-bundles.yaml"

#: SYSTEM/health bundles that stay flag/kill-switch managed -- never touched even if they
#: someday grow a guard-looking shape.
SYSTEM_BUNDLES = frozenset({"pyping"})

FLAG_KEY_RE = re.compile(r'^FLAG_KEY = "waddles\.command-[\w-]+"[ \t]*\n', re.M)
GUARD_RE = re.compile(
    r"^[ \t]+if not await feature_enabled\(FLAG_KEY, default=False\):[ \t]*\n[ \t]+return None[ \t]*\n",
    re.M,
)
IMPORT_LINE = "from waddle_sdk.flask_core.feature_flags import feature_enabled\n"
IMPORT_MULTI_RE = re.compile(
    r"^from waddle_sdk\.flask_core\.feature_flags import feature_enabled, (tier_at_least)[ \t]*\n", re.M
)
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

ACTIVATION_SENTENCE = (
    "Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated "
    "check decides whether this bundle runs."
)

#: (pattern, replacement) pairs applied to app.py prose (docstrings/comments) after the code edit.
#: Order matters: the multi-name import-list rules run before the generic sentence rules.
PROSE_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"`(\w+)`, `(\w+)`, `(\w+)`, `(\w+)` and\s+`feature_enabled`"),
        r"`\1`, `\2`, `\3` and `\4`",
    ),
    (re.compile(r"`(\w+)`, `(\w+)`, `(\w+)` and\s+`feature_enabled`"), r"`\1`, `\2` and `\3`"),
    (
        re.compile(
            r"Gated (?:behind|by) (?:the )?PostHog flag\s+``waddles\.command-[\w-]+``"
            r"(?:[^.]|\.(?=[\w`/]))*?\.(?=\s|$)"
        ),
        ACTIVATION_SENTENCE,
    ),
    (re.compile(r",?\s+or\s+while\s+the\s+(?:`waddles\.command-[\w-]+`\s+)?flag\s+is\s+(?:disabled|off)"), ""),
    (re.compile(r"\s+while\s+the\s+`waddles\.command-[\w-]+`\s+flag\s+is\s+disabled"), ""),
    (re.compile(r"flag\s+check\s+second,\s+"), ""),
    (re.compile(r"(?<=[ ,(])flag\s+second,\s+"), ""),
    (re.compile(r",\s+then\s+the\s+flag,\s+then"), ", then"),
    (re.compile(r",\s+then\s+flag,\s+then"), ", then"),
    (re.compile(r"A\s+flag-disabled\s+`[^`]+`\s+is\s+indistinguishable\s+from\s+an\s+unrecognized\s+command\."), ""),
    (re.compile(r"comes\s+before\s+the\s+flag\s+check"), "is the only precondition"),
    (re.compile(r",\s+a\s+disabled\s+flag,"), ","),
    (re.compile(r",\s+the\s+flag\s+being\s+OFF,"), ","),
    (
        re.compile(
            r"then\s+the\s+PostHog\s+flag\s+``waddles\.command-[\w-]+``\s+\(default\s+OFF\),\s+then"
        ),
        "then",
    ),
    (re.compile(r"\(cheap-skip\s+before\s+the\s+flag\s+check\)"), "(cheap-skip)"),
    (re.compile(r",?\s+and\s+while\s+`waddles\.command-[\w-]+`\s+is\s+disabled"), ""),
]

#: Prose rules that are only true once `flags.read` is dropped from the manifests.
FLAGS_READ_PROSE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\(only\s+`flags\.read`\s+is\s+declared\)"), "(no permission is declared)"),
    (re.compile(r"`storage\.kv`\s+\+\s+`flags\.read`"), "`storage.kv`"),
]


@dataclass(slots=True)
class BundleResult:
    """Per-bundle classification + outcome, for the final summary table."""

    name: str
    state: str  # guarded | system | skipped | converted | edited
    reason: str = ""
    notes: list[str] = field(default_factory=list)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_mentions(src: str, names: set[str]) -> bool:
    """True if any ast.Name / ast.Attribute attr in `src` is in `names` (docstrings ignored)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in names:
            return True
        if isinstance(node, ast.Attribute) and node.attr in names:
            return True
        if isinstance(node, ast.ImportFrom) and any(a.name in names for a in node.names):
            return True
    return False


def uses_flags_capability(src: str) -> bool:
    """True if the (already guard-stripped) source still reaches the WIT `flags` import.

    Covers the license-tier readers (`tier_at_least`, a direct `wit_world.imports.flags` lookup);
    such a bundle keeps `flags.read` -- the host gates BOTH `flags.enabled` and `flags.tier` on it.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "flags":
            return True
        if isinstance(node, ast.Name) and node.id in {"tier_at_least", "flags_mod"}:
            return True
        if isinstance(node, ast.Attribute) and node.attr == "flags":
            return True
        if isinstance(node, ast.ImportFrom) and any(a.name == "tier_at_least" for a in node.names):
            return True
    return False


def classify(name: str) -> BundleResult:
    """Classify one bundle dir as guarded / system / converted / skipped (with a reason)."""
    app = BUNDLES_DIR / name / "src" / "app.py"
    if name in SYSTEM_BUNDLES:
        return BundleResult(name, "system", "system/health bundle (no flag guard; stays as-is)")
    if not app.is_file():
        return BundleResult(name, "skipped", "no src/app.py")
    src = _read(app)
    has_key = bool(FLAG_KEY_RE.search(src))
    has_guard = bool(GUARD_RE.search(src))
    if not has_key and not has_guard and not _code_mentions(src, {"feature_enabled", "FLAG_KEY"}):
        return BundleResult(name, "converted", "no flag guard present (already activation-only)")
    if not (has_key and has_guard and (IMPORT_LINE in src or IMPORT_MULTI_RE.search(src))):
        return BundleResult(name, "skipped", "flag shape does not match expected pattern")
    new = _strip_code(src)
    if _code_mentions(new, {"feature_enabled", "FLAG_KEY", "FEATURE_FLAG_KEY"}):
        return BundleResult(
            name, "skipped", "additional flag use remains after guard removal (manual handling)"
        )
    return BundleResult(name, "guarded")


def _strip_code(src: str) -> str:
    """Remove the FLAG_KEY const (+ its `#:` comment block), the import, and the guard."""
    # The const and the `#:` comment lines directly above it that talk about the flag.
    lines = src.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        out.append(line)
    text = "".join(out)

    m = FLAG_KEY_RE.search(text)
    if m:
        start = m.start()
        before = text[:start].splitlines(keepends=True)
        while before and before[-1].lstrip().startswith("#:") and re.search(
            r"flag|PostHog", before[-1], re.I
        ):
            start -= len(before.pop())
        end = m.end()
        # Drop one blank line after the const if the line before it is also blank.
        if text[end : end + 1] == "\n" and (start == 0 or text[start - 1 : start] == "\n") and (
            text[max(0, start - 2) : start] == "\n\n" or start == 0
        ):
            end += 1
        text = text[:start] + text[end:]

    text = text.replace(IMPORT_LINE, "", 1)
    text = IMPORT_MULTI_RE.sub("from waddle_sdk.flask_core.feature_flags import tier_at_least\n", text)

    g = GUARD_RE.search(text)
    if g:
        start, end = g.start(), g.end()
        if text[end : end + 1] == "\n" and text[start - 1 : start] == "\n" and text[start - 2 : start] == "\n\n":
            end += 1  # collapse the blank line the guard sat next to
        text = text[:start] + text[end:]
    return text


def _docstring_lines(src: str) -> set[int]:
    """1-based line numbers covered by a docstring (module/class/function) expression."""
    spans: set[int] = set()
    for node in ast.walk(ast.parse(src)):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            val = body[0].value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                spans.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    return spans


_LISTISH_RE = re.compile(r"^\s*(?:[-*+|>]|\d+[.)]|```|:)")


def _tidy_prose(new: str, old: str) -> str:
    """Reflow prose paragraphs the rules touched; wrap any other line pushed past 100 columns.

    A paragraph is reflowed only when it sits in a docstring, shares one indent, and holds no
    list/table/fence line -- so markdown structure is never mangled.
    """
    old_lines = set(old.splitlines())
    in_doc = _docstring_lines(new)
    lines = [ln.rstrip() if ln not in old_lines else ln for ln in new.splitlines()]
    out: list[str] = []
    i = 0
    while i < len(lines):
        if i + 1 not in in_doc or not lines[i].strip():
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].strip() and j + 1 in in_doc:
            j += 1
        para = lines[i:j]
        touched = any(ln not in old_lines for ln in para)
        indents = {len(ln) - len(ln.lstrip()) for ln in para}
        plain = len(indents) == 1 and not any(_LISTISH_RE.match(ln) for ln in para)
        plain = plain and '"""' not in "".join(para)
        if touched and plain:
            indent = " " * indents.pop()
            text = " ".join(ln.strip() for ln in para)
            out.extend(
                textwrap.wrap(
                    text, width=100, initial_indent=indent, subsequent_indent=indent,
                    break_long_words=False, break_on_hyphens=False,
                )
            )
        else:
            for ln in para:
                out.extend(_wrap_long(ln) if ln not in old_lines else [ln])
        i = j
    final: list[str] = []
    for no, ln in enumerate(out):
        final.extend(_wrap_long(ln) if ln not in old_lines and ln.lstrip().startswith("#") else [ln])
    return "\n".join(final) + "\n"


def _wrap_long(line: str) -> list[str]:
    """Wrap one over-long comment/docstring line to 100 columns, keeping its indent + marker."""
    if len(line) <= 100:
        return [line]
    indent = line[: len(line) - len(line.lstrip())]
    stripped = line.lstrip()
    marker = "#: " if stripped.startswith("#:") else "# " if stripped.startswith("#") else ""
    body = stripped[len(marker.strip()) :].lstrip() if marker else stripped
    lead = indent + marker
    return textwrap.wrap(
        body, width=100, initial_indent=lead, subsequent_indent=lead,
        break_long_words=False, break_on_hyphens=False,
    )


def _prose(src: str, drop_flags_read: bool) -> str:
    rules = PROSE_RULES + (FLAGS_READ_PROSE_RULES if drop_flags_read else [])
    new = src
    for pat, repl in rules:
        new = pat.sub(repl, new)
    return _tidy_prose(new, src)


def _bump(version: str) -> str:
    m = VERSION_RE.match(version)
    if not m:
        raise ValueError(f"non-semver version {version!r}")
    return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"


FLAGS_READ_YAML_RE = re.compile(
    r"^[ \t]*- id: flags\.read[ \t]*\n(?:[ \t]+justification:.*\n)?", re.M
)


def _drop_flags_read(text: str) -> str:
    """Remove the `flags.read` permission entry; an emptied list becomes `permissions: []`."""
    new = FLAGS_READ_YAML_RE.sub("", text)
    # `permissions:` immediately followed by a non-list line => list is now empty.
    new = re.sub(r"^permissions:[ \t]*\n(?![ \t]+- )", "permissions: []\n", new, flags=re.M)
    return new


def _edit_manifests(name: str, do_drop: bool, result: BundleResult) -> None:
    bdir = BUNDLES_DIR / name
    bundle_yaml = bdir / "bundle.yaml"
    hub = bdir / "hub-manifest.yaml"
    pyproj = bdir / "pyproject.toml"
    vers: dict[str, str] = {}
    pat = {
        bundle_yaml: re.compile(r"^version: (\S+)[ \t]*$", re.M),
        hub: re.compile(r"^version: (\S+)[ \t]*$", re.M),
        pyproj: re.compile(r'^version = "([^"]+)"[ \t]*$', re.M),
    }
    for path, rx in pat.items():
        found = rx.findall(_read(path))
        if len(found) != 1:
            raise ValueError(f"{path.name}: expected exactly one version, found {found}")
        vers[path.name] = found[0]
    if len(set(vers.values())) != 1:
        raise ValueError(f"version drift across files: {vers}")
    old = next(iter(vers.values()))
    new = _bump(old)

    note = (
        f"# {new}: activation-gated only -- removed the in-bundle waddles.command-{name} "
        "feature-flag guard (activation is the sole gate; see docs/reference/command-bundles.md).\n"
    )
    writes: list[tuple[Path, str]] = []
    by = _read(bundle_yaml)
    by = re.sub(r"^version: " + re.escape(old) + r"[ \t]*$", note + f"version: {new}", by, flags=re.M)
    by = re.sub(r'^(  tested_with: )"' + re.escape(old) + '"', rf'\1"{new}"', by, flags=re.M)
    by = re.sub(r'^(  min_version: )"' + re.escape(old) + '"', rf'\1"{new}"', by, flags=re.M)
    if do_drop:
        by = _drop_flags_read(by)
    writes.append((bundle_yaml, by))

    hb = _read(hub)
    hb = re.sub(r"^version: " + re.escape(old) + r"[ \t]*$", f"version: {new}", hb, flags=re.M)
    if do_drop:
        hb = _drop_flags_read(hb)
    writes.append((hub, hb))

    pp = _read(pyproj)
    pp = pp.replace(f'version = "{old}"', f'version = "{new}"', 1)
    writes.append((pyproj, pp))

    cat = _read(CATALOG)
    row = re.compile(
        r"(- app_id: waddles\.core\.example\." + re.escape(name) + r"\n    version: )\"([^\"]+)\""
    )
    found = row.findall(cat)
    if len(found) == 1 and found[0][1] != old:
        raise ValueError(f"core-bundles.yaml row for {name} is @ {found[0][1]}, files say {old}")
    if len(found) > 1:
        raise ValueError(f"core-bundles.yaml has {len(found)} rows for {name}")
    if found:
        writes.append((CATALOG, row.sub(rf'\1"{new}"', cat)))
    else:
        result.notes.append("not in core-bundles.yaml (build-only/inert)")
    for wpath, wtext in writes:
        wpath.write_text(wtext, encoding="utf-8")
    result.notes.append(f"{old}->{new}{' (flags.read dropped)' if do_drop else ''}")


README_FLAG_ROW_RE = re.compile(r"^\| Flag \| `waddles\.command-[\w-]+` \(default OFF\) \|[ \t]*\n", re.M)
README_FLAGS_READ_ROW_RE = re.compile(r"^\| `flags\.read` \|.*\n", re.M)
README_SECTION_RE = re.compile(r"^## Feature flag[ \t]*\n.*?(?=^## |\Z)", re.M | re.S)
README_SECTION_NEW = (
    "## Gating\n\n"
    "Activation-gated only (installed -> enabled -> activated), **not** feature-flag gated: "
    "`transform()` carries no flag check. Feature flags are reserved for system/health bundles. "
    "See `docs/reference/command-bundles.md`.\n\n"
)


README_PROSE_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^- Gated behind PostHog flag `waddles\.command-[\w-]+` \(default OFF\); needs only the `flags\.read` permission\.$", re.M),
        "- Activation-gated only (no feature flag); declares no permissions.",
    ),
    (
        re.compile(r"^- Gated behind PostHog flag `waddles\.command-[\w-]+` \(default OFF\); permissions: `flags\.read`, `storage\.kv`\.$", re.M),
        "- Activation-gated only (no feature flag); permissions: `storage.kv`.",
    ),
    (re.compile(r", `feature_enabled`\)"), ")"),
    (re.compile(r"^- \*\*Flag fails closed[^\n]*\n(?:  [^\n]*\n)*", re.M), ""),
    (
        re.compile(r"Gated by PostHog flag\s+`waddles\.command-[\w-]+`(?: \(default OFF\))?\."),
        "Activation-gated only.",
    ),
]

#: README prose that is only true once `flags.read` is dropped from the manifests.
README_FLAGS_READ_PROSE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"`storage\.kv` \+ `flags\.read`"), "`storage.kv`"),
]


def _edit_readme(name: str, do_drop: bool, result: BundleResult) -> None:
    path = BUNDLES_DIR / name / "README.md"
    if not path.is_file():
        result.notes.append("no README")
        return
    text = _read(path)
    text = README_FLAG_ROW_RE.sub(
        "| Gating | Activation only (installed -> enabled -> activated); no feature flag |\n", text
    )
    text = README_SECTION_RE.sub(README_SECTION_NEW, text)
    for pat, repl in README_PROSE_RULES:
        text = pat.sub(repl, text)
    if do_drop:
        text = README_FLAGS_READ_ROW_RE.sub("", text)
        for pat, repl in README_FLAGS_READ_PROSE_RULES:
            text = pat.sub(repl, text)
    path.write_text(text, encoding="utf-8")


def apply_bundle(name: str, drop_flags: bool) -> BundleResult:
    """Edit one guarded bundle in place; any failure leaves it classified `skipped`."""
    result = classify(name)
    if result.state != "guarded":
        return result
    app = BUNDLES_DIR / name / "src" / "app.py"
    try:
        src = _read(app)
        stripped = _strip_code(src)
        keeps_flags = uses_flags_capability(stripped)  # also proves it still parses
        do_drop = drop_flags and not keeps_flags
        if drop_flags and keeps_flags:
            result.notes.append("kept flags.read (still reads license tier)")
        new = _prose(stripped, do_drop)
        ast.parse(new)  # still valid Python
        _edit_manifests(name, do_drop, result)
    except (ValueError, SyntaxError) as exc:
        return BundleResult(name, "skipped", f"apply failed ({exc}); manual handling")
    app.write_text(new, encoding="utf-8")
    _edit_readme(name, do_drop, result)
    # tests: drop the eager-import expectation of `feature_enabled`.
    for tpath in (BUNDLES_DIR / name / "tests").glob("*.py"):
        t = _read(tpath)
        t2 = re.sub(r',\s*"feature_enabled"(?=[,)])', "", t)
        t2 = re.sub(r'"feature_enabled",\s*', "", t2)
        if t2 != t:
            tpath.write_text(t2, encoding="utf-8")
    result.state = "edited"
    return result


FAILED_RE = re.compile(r"^FAILED (\S+?)::(?:(\w+)::)?(\w+)(?:\[.*?\])?(?: |$)", re.M)


def _pytest(name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"],
        cwd=BUNDLES_DIR / name,
        capture_output=True,
        text=True,
        check=False,
    )


def _remove_functions(path: Path, names: set[str]) -> int:
    src = _read(path)
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            spans.append((first - 1, node.end_lineno or node.lineno))
    for first, last in sorted(spans, reverse=True):
        # swallow trailing blank lines so spacing stays ruff-clean.
        while last < len(lines) and lines[last].strip() == "":
            last += 1
        del lines[first:last]
    path.write_text("".join(lines), encoding="utf-8")
    return len(spans)


def _fix_test_imports(name: str) -> None:
    """Drop the removed `FLAG_KEY`/`feature_enabled` names from `from app import (...)` lists."""
    for tpath in (BUNDLES_DIR / name / "tests").glob("*.py"):
        t = _read(tpath)
        t2 = re.sub(r"^[ \t]+FLAG_KEY,[ \t]*\n", "", t, flags=re.M)
        t2 = re.sub(r"^from app import FLAG_KEY[ \t]*\n", "", t2, flags=re.M)
        t2 = re.sub(r"(from app import [^\n(]*?), FLAG_KEY\b", r"\1", t2)
        if t2 != t:
            tpath.write_text(t2, encoding="utf-8")


def prune_tests(name: str) -> BundleResult:
    """Run the bundle's tests; delete failing flag-mentioning tests; report any other failure."""
    _fix_test_imports(name)
    proc = _pytest(name)
    result = BundleResult(name, "edited")
    fails = FAILED_RE.findall(proc.stdout)
    if proc.returncode not in (0, 1) and not fails:
        return BundleResult(name, "skipped", f"pytest error rc={proc.returncode}: {proc.stdout[-200:]}")
    by_file: dict[str, set[str]] = {}
    manual: list[str] = []
    for fname, _cls, test in fails:
        fpath = BUNDLES_DIR / name / fname
        tree_src = _read(fpath)
        seg = ""
        for node in ast.walk(ast.parse(tree_src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == test:
                seg = ast.get_source_segment(tree_src, node) or ""
        if re.search(r"flag|FLAG|feature_enabled|command_off", test + seg):
            by_file.setdefault(fname, set()).add(test)
        else:
            manual.append(f"{fname}::{test}")
    removed = sum(_remove_functions(BUNDLES_DIR / name / f, ns) for f, ns in by_file.items())
    if removed:
        tests = [str(p) for p in (BUNDLES_DIR / name / "tests").glob("*.py")]
        subprocess.run(["ruff", "check", "--select", "F401", "--fix", "-q", *tests], check=False)
    result.notes.append(f"pruned {removed} flag test(s)")
    if manual:
        result.state = "skipped"
        result.reason = "non-flag test failures need manual handling: " + ", ".join(manual)
    return result


def enumerate_bundles(only: set[str] | None) -> list[BundleResult]:
    names = sorted(p.name for p in BUNDLES_DIR.iterdir() if p.is_dir())
    if only:
        names = [n for n in names if n in only]
    return [classify(n) for n in names]


def print_enumeration(results: list[BundleResult]) -> None:
    groups: dict[str, list[BundleResult]] = {}
    for r in results:
        groups.setdefault(r.state, []).append(r)
    print(f"bundles examined: {len(results)}")
    for state in ("guarded", "converted", "system", "skipped"):
        members = groups.get(state, [])
        print(f"  {state}: {len(members)}")
        if state != "guarded" or len(members) <= 200:
            print("    " + ", ".join(m.name for m in members))
        for m in members:
            if state in ("skipped",):
                print(f"      - {m.name}: {m.reason}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="edit the guarded bundles in place")
    ap.add_argument("--drop-flags-read", action="store_true", help="also drop the flags.read permission")
    ap.add_argument("--prune-tests", action="store_true", help="delete obsolete flag-off tests (needs --apply first)")
    ap.add_argument("--only", default="", help="comma-separated bundle names")
    args = ap.parse_args()
    only = {s for s in args.only.split(",") if s} or None

    results = enumerate_bundles(only)
    print_enumeration(results)
    if not results:
        print("FAIL: zero bundles examined", file=sys.stderr)
        return 1
    if not (args.apply or args.prune_tests):
        return 0

    out: list[BundleResult] = []
    if args.apply:
        out = [apply_bundle(r.name, args.drop_flags_read) for r in results if r.state == "guarded"]
        print(f"\napplied: {sum(r.state == 'edited' for r in out)} edited, "
              f"{sum(r.state == 'skipped' for r in out)} skipped (of {len(out)} guarded)")
    if args.prune_tests:
        targets = [r.name for r in results if r.state in ("guarded", "converted")] if not args.apply else [
            r.name for r in out if r.state == "edited"
        ]
        out = [prune_tests(n) for n in targets]
        print(f"\npruned tests across {len(out)} bundles")
    bad = [r for r in out if r.state == "skipped"]
    for r in bad:
        print(f"MANUAL: {r.name}: {r.reason}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
