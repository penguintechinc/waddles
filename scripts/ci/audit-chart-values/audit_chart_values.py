#!/usr/bin/env python3
"""Audit a Helm chart for dead values keys and unused named templates (helpers).

Fails (exit 1) when any values leaf in values.yaml / values-*.yaml is referenced by no template,
or any `define` is never included, unless listed in the allowlist. Zero keys or zero templates
examined is also a failure. Run via `make audit-chart-values`.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from collections.abc import Iterable

import yaml

ID = r"[A-Za-z_]\w*"
CHAIN = re.compile(r"\.Values((?:\." + ID + r")*)")
ASSIGN = re.compile(r"(\$" + ID + r")\s*:?=\s*\(?\s*\$?\.Values((?:\." + ID + r")*)")
ASSIGN_INDEX = re.compile(
    r"(\$"
    + ID
    + r")\s*:?=\s*\(?\s*index\s+\(?\s*\$?\.Values((?:\."
    + ID
    + r")*)\s*\)?((?:\s+\"[^\"]+\")+)"
)
VAR_FROM_VAR = re.compile(
    r"(\$"
    + ID
    + r")\s*:?=\s*\(?\s*(?:index\s+)?(\$"
    + ID
    + r")((?:\."
    + ID
    + r")*)((?:\s+\"[^\"]+\")*)"
)
VARUSE = re.compile(r"(\$" + ID + r")((?:\." + ID + r")+)")
INDEX = re.compile(
    r"index\s+\(?\s*\$?\.Values((?:\." + ID + r")*)\s*\)?((?:\s+\"[^\"]+\")+)"
)
VAR_INDEX = re.compile(
    r"index\s+(\$" + ID + r")((?:\." + ID + r")*)((?:\s+\"[^\"]+\")+)"
)
DEFINE = re.compile(r'define\s+"([^"]+)"')


def segs(s: str) -> tuple[str, ...]:
    """Split a `.a.b.c` chain into a path tuple."""
    return tuple(x for x in s.split(".") if x)


def quoted(s: str) -> tuple[str, ...]:
    """Return the quoted string arguments of an `index` call as a path tuple."""
    return tuple(re.findall(r'"([^"]+)"', s))


def leaves(node: object, path: tuple[str, ...] = ()) -> Iterable[tuple[str, ...]]:
    """Yield every leaf path of a parsed values document (empty maps count as leaves)."""
    if isinstance(node, dict) and node:
        for k, v in node.items():
            yield from leaves(v, path + (str(k),))
    elif path:
        yield path


def actions_only(text: str) -> str:
    """Keep only template actions, minus `{{/* */}}` comments, so plain YAML comments never count."""
    text = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", " ", text, flags=re.DOTALL)
    return "\n".join(re.findall(r"\{\{.*?\}\}", text, flags=re.DOTALL))


def template_refs(templates: dict[str, str]) -> set[tuple[str, ...]]:
    """All values paths referenced by template actions, resolving `$var := .Values.x` aliases."""
    refs: set[tuple[str, ...]] = set()
    for t in templates.values():
        vars_: dict[str, set[tuple[str, ...]]] = {}
        for m in ASSIGN.finditer(t):
            vars_.setdefault(m.group(1), set()).add(segs(m.group(2)))
        for m in ASSIGN_INDEX.finditer(t):
            vars_.setdefault(m.group(1), set()).add(
                segs(m.group(2)) + quoted(m.group(3))
            )
        for _ in range(4):
            for m in VAR_FROM_VAR.finditer(t):
                for base in list(vars_.get(m.group(2), ())):
                    vars_.setdefault(m.group(1), set()).add(
                        base + segs(m.group(3)) + quoted(m.group(4))
                    )
        for m in CHAIN.finditer(t):
            refs.add(segs(m.group(1)))
        for m in INDEX.finditer(t):
            refs.add(segs(m.group(1)) + quoted(m.group(2)))
        for m in VARUSE.finditer(t):
            for base in vars_.get(m.group(1), ()):
                refs.add(base + segs(m.group(2)))
        for m in VAR_INDEX.finditer(t):
            for base in vars_.get(m.group(1), ()):
                refs.add(base + segs(m.group(2)) + quoted(m.group(3)))
        for var, bases in vars_.items():
            if re.search(re.escape(var) + r"(?![\w.])", t):
                refs.update(bases)
    return refs


def dead_values(
    chart: str, templates: dict[str, str], subcharts: dict[str, dict[str, str]]
) -> tuple[int, list[tuple[str, str]]]:
    """Return (examined_count, sorted dead dotted paths with the files defining them)."""
    found: dict[tuple[str, ...], set[str]] = {}
    files = [os.path.join(chart, "values.yaml")] + sorted(
        glob.glob(os.path.join(chart, "values-*.yaml"))
    )
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for p in leaves(yaml.safe_load(fh) or {}):
                found.setdefault(p, set()).add(os.path.basename(f))
    refs = template_refs(templates)
    prefixes = {r[:i] for r in refs for i in range(1, len(r) + 1)}
    sub_refs = {name: template_refs(t) for name, t in subcharts.items()}
    sub_prefixes = {
        name: {r[:i] for r in rs for i in range(1, len(r) + 1)} | {()}
        for name, rs in sub_refs.items()
    }
    dead = []
    for p, where in found.items():
        if p[0] in subcharts:
            sp = p[1:]
            if any(
                sp[:i] in sub_prefixes[p[0]] or sp[:i] in sub_refs[p[0]]
                for i in range(1, len(sp) + 1)
            ):
                continue
            if p[:1] in refs:  # parent chart passes the whole subchart map somewhere
                continue
        elif p in prefixes or any(p[:i] in refs for i in range(1, len(p))):
            # referenced directly, or an ancestor is consumed as a whole (toYaml/range/include/alias)
            continue
        if p[0] == "global" and any(
            p[1:] in sub_prefixes[s]
            or any(p[1:i] in sub_refs[s] for i in range(2, len(p)))
            for s in sub_prefixes
        ):
            continue
        dead.append((".".join(p), ",".join(sorted(where))))
    return len(found), sorted(dead)


def unused_helpers(
    templates: dict[str, str], raw: dict[str, str]
) -> tuple[int, list[str]]:
    """Return (defines_examined, sorted names never reached from a rendered template)."""
    bodies: dict[str, str] = {}
    for text in raw.values():
        ms = list(DEFINE.finditer(text))
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            bodies[m.group(1)] = actions_only(text[m.start() : end])
    roots = "\n".join(
        actions_only(t)
        for f, t in raw.items()
        if not os.path.basename(f).startswith("_") and not f.endswith(".tpl")
    )
    used: set[str] = set()
    frontier = [roots]
    while frontier:
        txt = frontier.pop()
        for name, body in bodies.items():
            if name not in used and re.search(
                r'(?:include|template)\s+\(?\s*"' + re.escape(name) + '"', txt
            ):
                used.add(name)
                frontier.append(body)
    return len(bodies), sorted(set(bodies) - used)


def load_templates(directory: str) -> dict[str, str]:
    """Read every file under a templates/ directory."""
    out = {}
    for f in glob.glob(os.path.join(directory, "**", "*"), recursive=True):
        if os.path.isfile(f):
            with open(f, encoding="utf-8") as fh:
                out[os.path.relpath(f, directory)] = fh.read()
    return out


def load_allowlist(path: str | None) -> dict[str, str]:
    """Allowlist: `key-or-helper  # reason` per line; a reason is mandatory."""
    allow: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return allow
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, reason = line.partition("#")
            if not reason.strip():
                raise SystemExit(
                    f"{path}:{n}: allowlist entry {name.strip()!r} has no '# reason'"
                )
            allow[name.strip()] = reason.strip()
    return allow


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "chart", help="chart directory (contains values.yaml and templates/)"
    )
    ap.add_argument(
        "--allowlist",
        help="file of intentionally kept keys/helpers, one per line with '# reason'",
    )
    args = ap.parse_args(argv)

    raw = load_templates(os.path.join(args.chart, "templates"))
    templates = {f: actions_only(t) for f, t in raw.items()}
    subcharts = {}
    for d in sorted(glob.glob(os.path.join(args.chart, "charts", "*", "templates"))):
        chart_yaml = os.path.join(os.path.dirname(d), "Chart.yaml")
        with open(chart_yaml, encoding="utf-8") as fh:
            name = (yaml.safe_load(fh) or {}).get("name", "")
        with open(os.path.join(args.chart, "Chart.yaml"), encoding="utf-8") as fh:
            for dep in (yaml.safe_load(fh) or {}).get("dependencies", []) or []:
                if dep.get("name") == name:
                    name = dep.get("alias") or name
        subcharts[name] = {f: actions_only(t) for f, t in load_templates(d).items()}

    allow = load_allowlist(args.allowlist)
    n_keys, dead = dead_values(args.chart, templates, subcharts)
    n_defs, unused = unused_helpers(templates, raw)
    dead = [(k, f) for k, f in dead if k not in allow]
    unused = [h for h in unused if h not in allow]

    for k, f in dead:
        print(f"DEAD VALUE   {k}  ({f})")
    for h in unused:
        print(f"UNUSED HELPER {h}")
    print(
        f"examined: {n_keys} values keys, {len(templates)} templates, {n_defs} helpers; "
        f"dead values: {len(dead)}, unused helpers: {len(unused)}, allowlisted: {len(allow)}"
    )
    if n_keys == 0 or not templates or n_defs == 0:
        print("FAIL: zero items examined", file=sys.stderr)
        return 1
    if dead or unused:
        print(
            "FAIL: remove the dead entries (or allowlist them with a reason)",
            file=sys.stderr,
        )
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
