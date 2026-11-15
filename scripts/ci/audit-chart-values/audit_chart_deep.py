#!/usr/bin/env python3
"""Deep chart audit: rendered-but-unread env vars + a delete-and-render cross-check of the static audit.

1. Env readers: renders every values-<env>.yaml, collects every env var our containers get (container
   `env:` entries and the data keys of ConfigMaps they `envFrom`), and fails on any name that no
   service code reads (grep -w over the code dirs), that the container's own command/args do not
   read, and that the chart's files/ scripts do not read -- unless allowlisted with a reason.
2. Delete-and-render: deletes every values key (in every file defining it), re-renders every env, and
   fails on (a) a key the static audit calls dead whose deletion changes a render (static audit
   wrong). Keys with no direct template reference (reached only through a wholesale/aliased parent)
   whose deletion changes no render in any env are counted as "suspects" for review
   (`--list-suspects`); most are children of features switched off in every env, so they fail the
   run only with `--strict-suspects`.

Zero environments rendered, zero env vars, or zero code files scanned is a failure.
Run via `make audit-chart-values-deep`.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "audit_chart_values", os.path.join(_HERE, "audit_chart_values.py")
)
assert _SPEC and _SPEC.loader
static_audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(static_audit)

# Our images (repo basename): their env must have a reader in this repo. Third-party images
# (postgres, valkey, seaweedfs, kubectl, aws-cli, ...) read their own env and are skipped.
OURS = re.compile(r"^(hub-[a-z-]+|svc-[a-z-]+|migrations|seeder|egress-proxy)$")
# Images whose only "code" is the inline command/args rendered by the chart.
INLINE = re.compile(r"^(python)$")
CODE_EXT = (
    ".py",
    ".rs",
    ".go",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".sh",
    ".cs",
    ".toml",
)
SKIP_DIRS = {
    "node_modules",
    "target",
    ".git",
    ".venv",
    "tests",
    "test",
    "__pycache__",
    "docs",
    "k8s",
}


def env_args(env: str) -> list[str]:
    """Extra helm args matching scripts/ci/helm_matrix.sh (offline renders of secret-requiring tiers)."""
    return (
        ["--set", "global.deploymentTier=alpha"]
        if env in ("beta", "gamma", "production", "ci")
        else []
    )


def render(helm: str, chart: str, env: str | None) -> str:
    """Render the chart for one values-<env>.yaml (None = base values only); fail loud on error."""
    args = [helm, "template", "waddlebot", chart, "--kube-version", "1.30.0"]
    args += (
        ["-f", os.path.join(chart, f"values-{env}.yaml")] + env_args(env)
        if env
        else ["--set", "global.deploymentTier=alpha"]
    )
    r = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "HELM_PLUGINS": os.devnull},
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"helm template ({env or 'base'}) failed: {r.stderr.strip()[:400]}"
        )
    return r.stdout


def read_yaml(path: str) -> Any:
    """Parse one YAML file."""
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def environments(chart: str) -> list[str]:
    """The env names of every values-<env>.yaml in the chart."""
    return sorted(
        f[len("values-") : -len(".yaml")]
        for f in os.listdir(chart)
        if f.startswith("values-") and f.endswith(".yaml")
    )


def image_repo(image: str) -> str:
    """`reg/org/repo/name:tag@sha` -> `name`."""
    return image.split("@")[0].rsplit("/", 1)[-1].split(":")[0]


def walk_pods(doc: Any) -> list[dict[str, Any]]:
    """Every pod spec (containers + initContainers holder) inside one manifest."""
    out = []
    stack = [doc]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if "containers" in x and isinstance(x["containers"], list):
                out.append(x)
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return out


def collect_env(manifests: str) -> dict[str, dict[str, Any]]:
    """name -> {"where": set, "inline": str of command/args text of the consuming containers, "ours": bool}."""
    docs = [d for d in yaml.safe_load_all(manifests) if isinstance(d, dict)]
    cms = {
        d["metadata"]["name"]: d.get("data") or {}
        for d in docs
        if d.get("kind") == "ConfigMap"
    }
    out: dict[str, dict[str, Any]] = {}

    def add(name: str, where: str, inline: str, ours: bool) -> None:
        e = out.setdefault(name, {"where": set(), "inline": "", "ours": False})
        e["where"].add(where)
        e["inline"] += "\n" + inline
        e["ours"] = e["ours"] or ours

    consumed_by_third_party: set[str] = set()
    for d in docs:
        for pod in walk_pods(d):
            for c in (pod.get("containers") or []) + (pod.get("initContainers") or []):
                repo = image_repo(str(c.get("image", "")))
                ours, inline_img = bool(OURS.match(repo)), bool(INLINE.match(repo))
                if not (ours or inline_img):
                    # a third-party image (postgres, valkey, ...) that pulls a ConfigMap key reads it itself
                    for ev in c.get("env") or []:
                        key = (
                            (ev.get("valueFrom") or {}).get("configMapKeyRef") or {}
                        ).get("key")
                        if key:
                            consumed_by_third_party.add(key)
                    continue
                # readers inside the pod spec itself: the inline command/args script, and kubelet
                # `$(NAME)` expansion in another env value of the same container
                inline = "\n".join(
                    str(x) for x in (c.get("command") or []) + (c.get("args") or [])
                )
                inline += "\n" + "\n".join(
                    str(ev.get("value", "")) for ev in c.get("env") or []
                )
                where = f"{d.get('kind')}/{d['metadata']['name']}/{c.get('name')}"
                for ev in c.get("env") or []:
                    add(ev["name"], where, inline, ours)
                for ef in c.get("envFrom") or []:
                    ref = (ef.get("configMapRef") or {}).get("name")
                    for key in cms.get(ref, {}):
                        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
                            add(key, f"ConfigMap/{ref}", inline, ours)
    for key in consumed_by_third_party & set(out):
        out[key]["inline"] += f"\n{key}"
    return out


def code_files(repo: str, dirs: list[str], extra: list[str]) -> list[str]:
    """Every non-test source file under the code dirs (plus extra files/dirs)."""
    files = []
    for top in [os.path.join(repo, d) for d in dirs] + extra:
        if os.path.isfile(top):
            files.append(top)
            continue
        for root, sub, names in os.walk(top):
            sub[:] = [s for s in sub if s not in SKIP_DIRS]
            files += [
                os.path.join(root, n)
                for n in names
                if n.endswith(CODE_EXT)
                and not n.startswith("test_")
                and "_test." not in n
            ]
    return files


def load_allowlist(path: str) -> list[tuple[re.Pattern[str], str]]:
    """`NAME_or_glob*  # reason` per line; reason mandatory."""
    out = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                name, _, reason = line.partition("#")
                if not reason.strip():
                    raise SystemExit(f"{path}:{n}: {name.strip()!r} has no '# reason'")
                out.append(
                    (
                        re.compile(
                            "^" + re.escape(name.strip()).replace(r"\*", ".*") + "$"
                        ),
                        reason.strip(),
                    )
                )
    return out


def env_reader_check(
    renders: dict[str, str], files: list[str], allow: list[tuple[re.Pattern[str], str]]
) -> tuple[int, list[str]]:
    """Return (env names examined, sorted unread names with where they are rendered)."""
    merged: dict[str, dict[str, Any]] = {}
    for text in renders.values():
        for name, e in collect_env(text).items():
            m = merged.setdefault(name, {"where": set(), "inline": "", "ours": False})
            m["where"] |= e["where"]
            m["inline"] += e["inline"]
            m["ours"] = m["ours"] or e["ours"]
    corpus = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                corpus.append(fh.read())
        except OSError:
            continue
    blob = "\n".join(corpus)
    words = set(re.findall(r"[A-Z_][A-Z0-9_]{2,}", blob))
    unread = []
    for name, e in sorted(merged.items()):
        if any(p.match(name) for p, _ in allow):
            continue
        if name in words or re.search(r"\b" + re.escape(name) + r"\b", e["inline"]):
            continue
        unread.append(f"{name}  ({', '.join(sorted(e['where'])[:3])})")
    return len(merged), unread


def delete_path(d: dict[str, Any], path: tuple[str, ...]) -> bool:
    """Delete one leaf path from a parsed values doc; True when it existed."""
    cur: Any = d
    for p in path[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    if isinstance(cur, dict) and path[-1] in cur:
        del cur[path[-1]]
        return True
    return False


def delete_and_render(
    helm: str, chart: str, keys: list[tuple[str, ...]], jobs: int
) -> dict[str, list[str]]:
    """For each key: delete it in every values file, re-render all envs, return {key: [changed envs]}."""
    envs = environments(chart)
    files = ["values.yaml"] + [f"values-{e}.yaml" for e in envs]
    with tempfile.TemporaryDirectory() as tmp:

        def work(chunk: list[tuple[str, ...]]) -> dict[str, list[str]]:
            wd = tempfile.mkdtemp(dir=tmp)
            copy_dir = os.path.join(wd, "chart")
            shutil.copytree(
                chart, copy_dir, ignore=shutil.ignore_patterns("tests", "docs")
            )
            orig = {f: read_yaml(os.path.join(copy_dir, f)) or {} for f in files}

            def write(data: dict[str, Any]) -> None:
                for f, d in data.items():
                    with open(os.path.join(copy_dir, f), "w", encoding="utf-8") as fh:
                        yaml.safe_dump(d, fh, sort_keys=False)

            def digest(env: str) -> str:
                try:
                    out = render(helm, copy_dir, env)
                except RuntimeError as exc:
                    out = "ERR " + str(exc)
                return hashlib.sha256(out.encode()).hexdigest()

            write(orig)
            base = {e: digest(e) for e in envs}
            res = {}
            for path in chunk:
                data = copy.deepcopy(orig)
                touched = {f: data[f] for f in files if delete_path(data[f], path)}
                write(touched)
                check = envs if "values.yaml" in touched else [f[7:-5] for f in touched]
                res[".".join(path)] = [e for e in check if digest(e) != base[e]]
                write({f: orig[f] for f in touched})
            return res

        chunks = [keys[i::jobs] for i in range(jobs)] if keys else []
        out: dict[str, list[str]] = {}
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for r in pool.map(work, [c for c in chunks if c]):
                out.update(r)
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the exit code."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("chart")
    ap.add_argument(
        "--repo", default=os.getcwd(), help="repo root to search for env readers"
    )
    ap.add_argument(
        "--code-dirs",
        default="core,hub_api,admin,bundles,libs,sdk,config,scripts,migrations,alembic",
        help="comma-separated code dirs (relative to --repo) searched for readers",
    )
    ap.add_argument("--env-allowlist", default=os.path.join(_HERE, "env-allowlist.txt"))
    ap.add_argument(
        "--allowlist",
        default=os.path.join(_HERE, "allowlist.txt"),
        help="static audit allowlist",
    )
    ap.add_argument("--helm", default=os.environ.get("HELM", "helm"))
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument(
        "--render-allowlist",
        default=os.path.join(_HERE, "render-allowlist.txt"),
        help="keys (globs ok) kept although render-neutral and only parent-referenced",
    )
    ap.add_argument(
        "--list-suspects",
        action="store_true",
        help="print every suspect key (review aid)",
    )
    ap.add_argument(
        "--strict-suspects",
        action="store_true",
        help="fail on suspects too (most are children of features off in every env)",
    )
    ap.add_argument(
        "--skip-render-check", action="store_true", help="env-reader check only"
    )
    args = ap.parse_args(argv)

    envs = environments(args.chart)
    renders = {e: render(args.helm, args.chart, e) for e in envs}
    dirs = [d for d in args.code_dirs.split(",") if d]
    files = code_files(args.repo, dirs, [os.path.join(args.chart, "files")])
    n_env, unread = env_reader_check(renders, files, load_allowlist(args.env_allowlist))
    for u in unread:
        print(f"UNREAD ENV   {u}")
    print(
        f"env readers: {len(renders)} envs rendered, {n_env} env names examined, {len(files)} code files scanned, "
        f"unread: {len(unread)}"
    )
    failed = bool(unread)
    if not renders or n_env == 0 or not files:
        print("FAIL: zero items examined", file=sys.stderr)
        return 1

    if not args.skip_render_check:
        raw = static_audit.load_templates(os.path.join(args.chart, "templates"))
        tpls = {f: static_audit.actions_only(t) for f, t in raw.items()}
        _, dead = static_audit.dead_values(
            args.chart, tpls, static_audit.load_subcharts(args.chart)
        )
        dead_keys = {k for k, _ in dead}
        refs = static_audit.template_refs(tpls)
        direct = {r[:i] for r in refs for i in range(1, len(r) + 1)}
        keys = sorted(
            {
                p
                for f in ["values.yaml"] + [f"values-{e}.yaml" for e in envs]
                for p in static_audit.leaves(
                    read_yaml(os.path.join(args.chart, f)) or {}
                )
            }
        )
        allow = load_allowlist(args.render_allowlist)
        res = delete_and_render(args.helm, args.chart, keys, args.jobs)
        disagree, suspect, neutral_direct = [], [], 0
        for path in keys:
            k = ".".join(path)
            changed = res.get(k, [])
            if k in dead_keys and changed:
                disagree.append(f"{k} (static: dead; deleting it changes {changed})")
            elif not changed and path[0] != "spire" and path not in direct:
                # render-neutral AND only reachable through a wholesale/aliased parent: likely dead
                if not any(p.match(k) for p, _ in allow):
                    suspect.append(k)
            elif not changed:
                neutral_direct += 1
        for k in disagree:
            print(f"STATIC/RENDER DISAGREE  {k}")
        if args.strict_suspects or args.list_suspects:
            for k in suspect:
                print(
                    f"SUSPECT DEAD  {k}  (no direct template reference; deleting it changes no render)"
                )
        print(
            f"delete-and-render: {len(keys)} keys tested across {len(envs)} envs; render-neutral but directly "
            f"referenced (feature off in every env): {neutral_direct}; suspects: {len(suspect)}; "
            f"static/render disagreements: {len(disagree)}"
        )
        if not keys:
            print("FAIL: zero values keys tested", file=sys.stderr)
            return 1
        failed = failed or bool(disagree) or (args.strict_suspects and bool(suspect))

    if failed:
        print(
            "FAIL: remove the unread env (or allowlist it with a reason)",
            file=sys.stderr,
        )
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
