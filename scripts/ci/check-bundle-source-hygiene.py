#!/usr/bin/env python3
"""Static hygiene checks over shipped App Bundle Python source (`bundles/**/src/**/*.py`).

Three independent AST checks, each catching a defect class this project has
hit repeatedly in bundle code, selectable with `--check`:

``log-pii``
    A bundle log call (`log.info(...)`, `logger.error(...)`, `print(...)`)
    whose message or arguments carry RAW USER INPUT or a raw identity --
    `rest`, `message`, `text`, `username`, `target`, `@mention`, `event.actor`,
    a bare `event`/`payload` object, ... The PII boundary is the hub-api
    `users` table; a bundle runs on the hot path of every chat message and
    must log only opaque/structural fields (`community`, `op`, `context`,
    `app_id`, counts, the exception TYPE). The recurring leak is the
    "add a log" fix for a no-stubs/silent-fallback finding that writes
    `log.error("...", rest=rest)`. Judgment calls the AST cannot make
    (a log arg that merely LOOKS like input) are escaped with an inline
    `# pii-ok: <reason>` comment -- the reason is mandatory and every
    suppression is printed in the summary.

``silent-except``
    A BROAD handler (`except:` / `except Exception:` / `except BaseException:`
    / a tuple containing one, or `contextlib.suppress(<broad>)`) whose body is
    nothing but `pass` / `...` / `continue` / `break` / a bare `return` /
    `return <literal>` / `x = <literal>` -- i.e. it swallows an arbitrary
    failure and substitutes a default with no log call and no re-raise.
    Complements the semgrep layer of `scripts/ci/check-no-stubs.sh`, which
    only matches handlers whose ENTIRE body is `pass` or `return <expr>` and
    misses bare `return`, `continue`, `...`, literal-assign defaults and
    tuple-typed handlers. NARROW handlers (`except CommandUsageError:`,
    `except db.ConflictError: continue` retry loops, `except ImportError:`
    fail-closed shims) are expected control flow and are never flagged.
    Escape hatch: `# silent-ok: <reason>` on any line of the handler.

``badge-truthiness``
    A role/badge field (`is_mod`, `is_broadcaster`, ...) read through Python TRUTHINESS instead
    of an identity check: `bool(payload.get("is_mod"))`, `bool(is_mod)`, or a bare badge read as
    an operand of `or`/`and`/`not`/`if`/`while`/a ternary. A string badge such as `"false"` is
    truthy, so a non-moderator whose normalizer emitted a string passed every mod gate that used
    one -- and `transform()` laundered it into a real `True` for `dispatch` (fix/bundle-defects-wave:
    40 bundles). Only `is True` (or `isinstance(x, bool)` guards, `"is_mod" in payload` tests and
    comparisons) are accepted. Escape hatch: `# badge-ok: <reason>`.

Exit code is the gate (critical-rules.md Verification Integrity): non-zero on
any finding, on an unparseable source file, and on a ZERO denominator (no
files scanned / no log calls or handlers examined -- a scanner pointed at the
wrong root reports clean). Counts examined are always printed.

Usage: check-bundle-source-hygiene.py [--check log-pii|silent-except|badge-truthiness|all] [--root DIR]
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

LOG_LEVELS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical"})
LOG_RECEIVER_NAMES = frozenset({"log", "logger", "_log", "_logger", "logging", "LOG"})

# Identifiers (lowercased exact match) that hold raw user-typed input or a raw
# platform identity inside a bundle. Deliberately an exact-name list, not a
# substring match: `target_ms` (a duration) and `error_message` (exception
# text) are legitimate and must not trip it.
RAW_INPUT_NAMES = frozenset({
    "rest", "remainder", "arg", "args", "argstr", "arg_str", "argument", "arguments",
    "message", "text", "raw", "raw_text", "raw_input", "raw_message", "user_input",
    "utterance", "reply_text",
})
IDENTITY_NAMES = frozenset({
    "username", "user_name", "handle", "nick", "nickname", "display_name", "displayname",
    "login", "screen_name", "actor", "author", "sender", "chatter", "email", "phone",
    "mention", "mentions", "mentioned", "target", "target_user", "target_name",
    "target_handle", "target_token", "target_str", "target_text", "target_arg", "raw_target",
    "at_target", "recipient", "opponent", "challenger", "victim",
    "ip", "ip_address",
})
TAINTED_NAMES = RAW_INPUT_NAMES | IDENTITY_NAMES
# Whole-object values: logging the bare object serializes the user text inside it.
CONTAINER_NAMES = frozenset({"event", "payload", "envelope", "parsed", "request"})
# `event.payload` / `ctx.payload` as an attribute is the same whole-object leak.
TAINTED_ATTRS = TAINTED_NAMES | {"payload"}
# kwarg KEYS that announce raw input regardless of how the value is spelled.
STRICT_KEYS = frozenset({
    "rest", "username", "user_name", "handle", "mention", "mentions", "target",
    "text", "raw", "raw_text", "email", "args", "arg", "argument",
})
# Calls whose result cannot carry the input's content (a size, a type, a bool).
SAFE_CALLS = frozenset({
    "len", "type", "bool", "isinstance", "int", "float", "id", "callable", "hasattr",
})
BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException"})
PII_PRAGMA = re.compile(r"#\s*pii-ok\s*:\s*(\S.*)$")
SILENT_PRAGMA = re.compile(r"#\s*silent-ok\s*:\s*(\S.*)$")
BADGE_PRAGMA = re.compile(r"#\s*badge-ok\s*:\s*(\S.*)$")
# Normalized role/badge field names a platform normalizer emits (core/svc_ingest/src/normalize.rs).
BADGE_NAMES = frozenset({
    "is_mod", "is_moderator", "is_broadcaster", "is_vip", "is_subscriber", "is_sub",
})
SKIP_DIR_NAMES = frozenset({"tests", "test", "__pycache__", "node_modules", "target", "dist", "build"})


@dataclass(slots=True)
class Finding:
    """One gate hit: where it is, which rule fired, and why."""

    path: str
    line: int
    rule: str
    detail: str


@dataclass(slots=True)
class Result:
    """Aggregate counters for one check run -- the denominator proof plus findings."""

    files_scanned: int = 0
    examined: int = 0
    suppressed: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def discover_sources(root: Path) -> list[Path]:
    """Return every shipped bundle source file: `.py` under a `src/` dir inside `<root>/bundles`.

    Test directories, `test_*.py`, `conftest.py` and build output are excluded so only
    code that ships in a bundle is judged.
    """
    bundles = root / "bundles"
    found: list[Path] = []
    if not bundles.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(bundles):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        rel_parts = Path(dirpath).relative_to(bundles).parts
        if "src" not in rel_parts:
            continue
        for name in sorted(filenames):
            if name.endswith(".py") and not name.startswith("test_") and name != "conftest.py":
                found.append(Path(dirpath) / name)
    return found


def parse_source(path: Path) -> tuple[ast.Module, list[str]]:
    """Parse one source file into an AST plus its lines (for pragma lookup); raises on bad syntax."""
    text = path.read_text(encoding="utf-8")
    return ast.parse(text, filename=str(path)), text.splitlines()


def pragma_reason(lines: list[str], first: int, last: int, pattern: re.Pattern[str]) -> str | None:
    """Return the justification text of a suppression pragma on lines `first..last` (1-based), if any."""
    for lineno in range(first, last + 1):
        match = pattern.search(lines[lineno - 1])
        if match:
            return match.group(1).strip()
    return None


def is_log_call(call: ast.Call) -> bool:
    """True for `log.<level>(...)`, `self.log.<level>(...)`, `logging.<level>(...)` and `print(...)`."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    if not isinstance(func, ast.Attribute) or func.attr not in LOG_LEVELS:
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return receiver.id in LOG_RECEIVER_NAMES
    return isinstance(receiver, ast.Attribute) and receiver.attr in LOG_RECEIVER_NAMES


def _const_str_key(node: ast.expr | None) -> str | None:
    """The lowercase string value of a constant-string node (a dict key / `.get()` key), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.lower()
    return None


def taint(node: ast.AST | None) -> list[str]:
    """Names of raw-input/identity values an expression would put into a log line.

    Field selection (`event.platform`, `payload['command']`, `.get('channel_id')`)
    is judged by the selected field, never the container; method calls on a
    tainted receiver stay tainted; size/type/bool wrappers sanitize.
    """
    if node is None or isinstance(node, ast.Constant):
        return []
    if isinstance(node, ast.Name):
        lowered = node.id.lower()
        if lowered in TAINTED_NAMES or lowered in CONTAINER_NAMES:
            return [node.id]
        return []
    if isinstance(node, ast.Attribute):
        if node.attr.lower() in TAINTED_ATTRS:
            return [node.attr]
        if isinstance(node.value, ast.Name):
            return []
        return taint(node.value)
    if isinstance(node, ast.Subscript):
        key = _const_str_key(node.slice)
        if key is not None:
            return [key] if key in TAINTED_NAMES else []
        return taint(node.value) + taint(node.slice)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in SAFE_CALLS:
            return []
        if isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
            key = _const_str_key(node.args[0])
            if key is not None:
                hits = [key] if key in TAINTED_NAMES else []
                return hits + [h for arg in node.args[1:] for h in taint(arg)]
        hits = taint(func.value) if isinstance(func, ast.Attribute) else taint(func)
        for arg in node.args:
            hits += taint(arg)
        for kw in node.keywords:
            hits += taint(kw.value)
        return hits
    if isinstance(node, ast.Compare):
        return []
    hits: list[str] = []
    for child in ast.iter_child_nodes(node):
        hits += taint(child)
    return hits


def _is_safe_value(node: ast.expr) -> bool:
    """True when a kwarg value is a literal, a size/type/bool wrapper, or a comparison."""
    if isinstance(node, (ast.Constant, ast.Compare)):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in SAFE_CALLS


def log_call_leaks(call: ast.Call) -> list[str]:
    """Every raw-input/identity leak in one log call's message, positional args and kwargs."""
    leaks: list[str] = []
    for arg in call.args:
        leaks += taint(arg)
    for kw in call.keywords:
        if kw.arg is not None and kw.arg.lower() in STRICT_KEYS and not _is_safe_value(kw.value):
            leaks.append(f"{kw.arg}=")
        leaks += taint(kw.value)
    return sorted(set(leaks))


def check_log_pii(root: Path, sources: list[Path]) -> Result:
    """Run the PII-in-bundle-logs check over `sources`; counts every log call examined."""
    result = Result(files_scanned=len(sources))
    for path in sources:
        rel = path.relative_to(root).as_posix()
        tree, lines = parse_source(path)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and is_log_call(node)):
                continue
            result.examined += 1
            leaks = log_call_leaks(node)
            if not leaks:
                continue
            end = node.end_lineno or node.lineno
            reason = pragma_reason(lines, node.lineno, end, PII_PRAGMA)
            if reason:
                result.suppressed.append(f"{rel}:{node.lineno} pii-ok: {reason}")
                continue
            result.findings.append(Finding(
                rel, node.lineno, "log-pii",
                f"log call carries raw user input/identity ({', '.join(leaks)}) -- log only "
                "community/op/context/counts/exception type, or tokenize before logging",
            ))
    return result


def is_broad_exception(node: ast.expr | None) -> bool:
    """True for a bare handler, `Exception`/`BaseException` (also `builtins.X`), or a tuple containing one."""
    if node is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in BROAD_EXCEPTIONS
    if isinstance(node, ast.Attribute):
        return node.attr in BROAD_EXCEPTIONS
    if isinstance(node, ast.Tuple):
        return any(is_broad_exception(elt) for elt in node.elts)
    return False


def _is_literal(node: ast.expr) -> bool:
    """True when `node` is a pure literal (constant, empty/constant container) -- no names, no calls."""
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return False
    return True


def swallow_kind(handler: ast.ExceptHandler) -> str | None:
    """Describe how a handler silently swallows its error, or None if it does anything real.

    "Anything real" = a re-raise or ANY call (log, reply builder, cleanup) anywhere in the body,
    or any statement beyond pass/.../continue/break/return-literal/assign-literal.
    """
    kinds: list[str] = []
    for stmt in handler.body:
        for sub in ast.walk(stmt):
            if isinstance(sub, (ast.Raise, ast.Call)):
                return None
        if isinstance(stmt, ast.Pass):
            kinds.append("pass")
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            kinds.append("...")
        elif isinstance(stmt, (ast.Continue, ast.Break)):
            kinds.append(type(stmt).__name__.lower())
        elif isinstance(stmt, ast.Return) and (stmt.value is None or _is_literal(stmt.value)):
            kinds.append("return" if stmt.value is None else "return <default>")
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None and _is_literal(stmt.value):
            kinds.append("assign <default>")
        else:
            return None
    return " + ".join(kinds) if kinds else None


def _suppress_is_broad(item: ast.withitem) -> bool:
    """True for `with suppress(<broad>)` / `with contextlib.suppress(<broad>)` context items."""
    expr = item.context_expr
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    return name == "suppress" and any(is_broad_exception(arg) for arg in expr.args)


def check_silent_except(root: Path, sources: list[Path]) -> Result:
    """Run the silent-except-to-default check over `sources`; counts every handler/suppress examined."""
    result = Result(files_scanned=len(sources))
    for path in sources:
        rel = path.relative_to(root).as_posix()
        tree, lines = parse_source(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                result.examined += 1
                kind = swallow_kind(node) if is_broad_exception(node.type) else None
                caught = ast.unparse(node.type) if node.type is not None else "(bare except)"
                detail = f"broad `except {caught}` swallows the error ({kind}) with no log call and no re-raise"
            elif isinstance(node, (ast.With, ast.AsyncWith)) and any(_suppress_is_broad(i) for i in node.items):
                result.examined += 1
                kind = "suppress"
                detail = "`contextlib.suppress(<broad exception>)` silently swallows every failure in its block"
            else:
                continue
            if kind is None:
                continue
            end = node.end_lineno or node.lineno
            reason = pragma_reason(lines, node.lineno, end, SILENT_PRAGMA)
            if reason:
                result.suppressed.append(f"{rel}:{node.lineno} silent-ok: {reason}")
                continue
            result.findings.append(Finding(rel, node.lineno, "silent-except", detail + " -- log the exception (type + message) or let it propagate"))
    return result


def _is_badge_read(node: ast.AST | None) -> bool:
    """True when `node` directly reads a role/badge field: `is_mod`, `x.get("is_mod")`, `x["is_mod"]`."""
    if isinstance(node, ast.Name):
        return node.id in BADGE_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in BADGE_NAMES
    if isinstance(node, ast.Subscript):
        return _const_str_key(node.slice) in BADGE_NAMES
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        return bool(node.args) and _const_str_key(node.args[0]) in BADGE_NAMES
    return False


def _contains_badge_read(node: ast.AST) -> bool:
    """True when any sub-expression of `node` is a badge read."""
    return any(_is_badge_read(sub) for sub in ast.walk(node))


def _truthiness_operands(node: ast.AST) -> list[ast.expr]:
    """The sub-expressions Python coerces to bool when `node` is a boolean context, else []."""
    if isinstance(node, ast.BoolOp):
        return list(node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return [node.operand]
    if isinstance(node, (ast.If, ast.While, ast.IfExp)):
        return [node.test]
    if isinstance(node, ast.comprehension):
        return list(node.ifs)
    return []


def check_badge_truthiness(root: Path, sources: list[Path]) -> Result:
    """Run the badge-truthiness check over `sources`; counts every badge read examined."""
    result = Result(files_scanned=len(sources))
    for path in sources:
        rel = path.relative_to(root).as_posix()
        tree, lines = parse_source(path)
        result.examined += sum(1 for node in ast.walk(tree) if _is_badge_read(node))
        flagged: list[tuple[ast.AST, str]] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "bool"
                and any(_contains_badge_read(arg) for arg in node.args)
            ):
                flagged.append((node, f"`{ast.unparse(node)}` coerces a badge through truthiness"))
            for operand in _truthiness_operands(node):
                if _is_badge_read(operand):
                    flagged.append((operand, f"`{ast.unparse(operand)}` is used as a bare truthiness test"))
        for node, why in flagged:
            end = getattr(node, "end_lineno", None) or node.lineno
            reason = pragma_reason(lines, node.lineno, end, BADGE_PRAGMA)
            if reason:
                result.suppressed.append(f"{rel}:{node.lineno} badge-ok: {reason}")
                continue
            result.findings.append(Finding(
                rel, node.lineno, "badge-truthiness",
                f"{why} -- a string badge (\"false\") is truthy, so a non-moderator would pass; "
                "use `x is True` (fail closed on anything that is not a real boolean)",
            ))
    return result


CHECKS = {
    "log-pii": ("log calls", check_log_pii),
    "silent-except": ("except handlers", check_silent_except),
    "badge-truthiness": ("badge reads", check_badge_truthiness),
}


def emit(check: str, unit: str, result: Result) -> bool:
    """Print findings, suppressions and the denominator summary for one check; True when it passed."""
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for finding in result.findings:
        print(f"{finding.path}:{finding.line}: [{finding.rule}] {finding.detail}", file=sys.stderr)
        if in_actions:
            print(f"::error file={finding.path},line={finding.line},title=bundle-hygiene {finding.rule}::{finding.detail}")
    for note in result.suppressed:
        print(f"  suppressed: {note}")
    zero = result.files_scanned == 0 or result.examined == 0
    if zero:
        print(
            f"check-bundle-source-hygiene[{check}]: FAIL -- zero denominator "
            f"(files_scanned={result.files_scanned}, {unit.replace(' ', '_')}_examined={result.examined}); "
            "scan root moved/missing?",
            file=sys.stderr,
        )
    passed = not zero and not result.findings
    print(
        f"check-bundle-source-hygiene[{check}]: files_scanned={result.files_scanned} "
        f"{unit.replace(' ', '_')}_examined={result.examined} suppressed={len(result.suppressed)} "
        f"findings={len(result.findings)} -> {'PASS' if passed else 'FAIL'}"
    )
    return passed


def main(argv: list[str]) -> int:
    """CLI entry: run the selected check(s) against `--root`; exit 1 on any failure."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", choices=[*CHECKS, "all"], default="all")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2],
                        help="repo root containing bundles/ (default: this repo)")
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    sources = discover_sources(root)
    selected = list(CHECKS) if args.check == "all" else [args.check]
    ok = True
    for name in selected:
        unit, runner = CHECKS[name]
        try:
            result = runner(root, sources)
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"check-bundle-source-hygiene[{name}]: FAIL -- cannot parse a bundle source file: {exc}", file=sys.stderr)
            return 1
        ok = emit(name, unit, result) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
