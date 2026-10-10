"""Static audit for log calls that write exception text (and so bound DB values) to logs.

A DB driver exception's message routinely embeds the *bound values* of the failed
statement (message content, usernames, platform ids -- see ``flask_core.db_errors``).
Any log call that interpolates the exception, passes ``exc_info``, or uses
``logger.exception`` therefore writes PII into the log stream. ``describe_db_error``
is the sanctioned replacement; this module is the regression guard that keeps the
unsafe shapes from coming back.

It flags, for every logging call (receiver whose dotted name contains ``log``):

* ``exc_info=...`` (anything but a literal ``False``/``None``) -- renders the exception text;
* ``logger.exception(...)`` -- same, implicitly;
* ``traceback.format_exc()``/``print_exc()``/... evaluated inside the call;
* a bound exception name -- ``except ... as e`` or an ``@errorhandler`` parameter -- used
  in the call other than via ``describe_db_error(e)``, ``type(e)`` or a value-free
  attribute (``e.code``, ``e.status_code``, ``e.response.status_code``, ...).

Usable as a library (`audit_paths`) and as a CI check
(``python3 -m flask_core.exc_log_audit PATH...``); the CLI fails when anything is
flagged *or* when nothing was examined, so a mis-pointed scan cannot report clean.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

LOG_METHODS: Final[frozenset[str]] = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}
)
#: Calls that render a traceback / exception text when evaluated inside a log call.
_TRACEBACK_FUNCS: Final[frozenset[str]] = frozenset(
    {"format_exc", "print_exc", "format_exception", "print_exception", "format_exception_only"}
)
#: Wrappers that make a bound exception safe to pass to a log call.
_SAFE_WRAPPERS: Final[frozenset[str]] = frozenset({"describe_db_error", "type"})
#: Attributes of an exception that carry no driver text: status/codes, the numeric
#: ``community_id`` of this codebase's own exceptions, and their application-authored
#: ``message``.
_SAFE_ATTRS: Final[frozenset[str]] = frozenset(
    {"code", "status_code", "status", "errno", "pgcode", "sqlstate", "community_id", "message"}
)


@dataclass(slots=True, frozen=True)
class Finding:
    """One unsafe log call: where it is and why it is unsafe."""

    path: str
    line: int
    reason: str

    def render(self) -> str:
        """Format as ``path:line: reason``."""
        return f"{self.path}:{self.line}: {self.reason}"


@dataclass(slots=True, frozen=True)
class AuditReport:
    """Result of an audit run, including the denominator (what was actually examined)."""

    files_examined: int
    log_calls_examined: int
    findings: tuple[Finding, ...]


def _receiver_name(func: ast.Attribute) -> str:
    """Return the dotted name of a method call's receiver (``self.logger`` -> ``self.logger``)."""
    parts: list[str] = []
    node: ast.expr = func.value
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_log_call(node: ast.Call) -> bool:
    """Return True if `node` looks like ``<something-logger>.<level>(...)``."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in LOG_METHODS
        and "log" in _receiver_name(func).lower()
    )


def _is_literal_off(value: ast.expr) -> bool:
    """Return True for ``exc_info=False`` / ``exc_info=None`` (explicitly no traceback)."""
    return isinstance(value, ast.Constant) and value.value in (False, None)


def _bound_exception_names(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> set[str]:
    """Return the exception names in scope at `call` (``except ... as N``, errorhandler params)."""
    names: set[str] = set()
    node: ast.AST | None = parents.get(call)
    while node is not None:
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(target, ast.Attribute) and target.attr == "errorhandler":
                    names.update(a.arg for a in node.args.args)
        node = parents.get(node)
    return names


def _use_is_safe(name_node: ast.Name, call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """Return True if this use of an exception name cannot render driver text."""
    # Value-free attribute chain: e.code, e.status_code, e.response.status_code, ...
    top: ast.AST = name_node
    last_attr: str | None = None
    parent = parents.get(top)
    while isinstance(parent, ast.Attribute) and parent.value is top:
        last_attr = parent.attr
        top = parent
        parent = parents.get(top)
    if last_attr is not None and last_attr in _SAFE_ATTRS:
        return True
    # Sanctioned wrappers: describe_db_error(e), type(e)
    node: ast.AST | None = parents.get(name_node)
    while node is not None and node is not call:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _SAFE_WRAPPERS
        ):
            return True
        node = parents.get(node)
    return False


def _call_name(node: ast.Call) -> str | None:
    """Return the bare name of a call's target (``a.b.c()`` -> ``c``, ``c()`` -> ``c``)."""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _audit_call(call: ast.Call, parents: dict[ast.AST, ast.AST], path: str) -> Iterator[Finding]:
    """Yield a `Finding` for each unsafe aspect of one logging call."""
    if isinstance(call.func, ast.Attribute) and call.func.attr == "exception":
        yield Finding(path, call.lineno, "logger.exception() renders the exception text")
    for kw in call.keywords:
        if kw.arg == "exc_info" and not _is_literal_off(kw.value):
            yield Finding(path, kw.value.lineno, "exc_info renders the exception text")
    bound = _bound_exception_names(call, parents)
    for part in [*call.args, *(kw.value for kw in call.keywords)]:
        for node in ast.walk(part):
            if isinstance(node, ast.Call) and _call_name(node) in _TRACEBACK_FUNCS:
                yield Finding(path, node.lineno, "traceback text interpolated into a log call")
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in bound
                and not _use_is_safe(node, call, parents)
            ):
                yield Finding(
                    path,
                    node.lineno,
                    f"exception {node.id!r} interpolated into a log call (use describe_db_error)",
                )


def audit_source(source: str, path: str = "<string>") -> tuple[int, list[Finding]]:
    """Audit one module's source; return ``(log_calls_examined, findings)``."""
    tree = ast.parse(source, filename=path)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    findings: list[Finding] = []
    examined = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_log_call(node):
            examined += 1
            findings.extend(_audit_call(node, parents, path))
    return examined, sorted(set(findings), key=lambda f: (f.line, f.reason))


def _iter_python_files(paths: Iterable[Path], *, include_tests: bool) -> Iterator[Path]:
    """Yield .py files under `paths`, skipping test files/dirs unless `include_tests`."""
    for root in paths:
        candidates = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in candidates:
            if path.suffix != ".py":
                continue
            if not include_tests and (
                path.name.startswith("test_") or "tests" in path.parts or path.name == "conftest.py"
            ):
                continue
            yield path


def audit_paths(paths: Iterable[Path], *, include_tests: bool = False) -> AuditReport:
    """Audit every Python file under `paths` and return the findings with the denominator."""
    files = 0
    calls = 0
    findings: list[Finding] = []
    for path in _iter_python_files(paths, include_tests=include_tests):
        files += 1
        examined, found = audit_source(path.read_text(encoding="utf-8"), str(path))
        calls += examined
        findings.extend(found)
    return AuditReport(files, calls, tuple(findings))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: exit 1 on any finding or when nothing was examined."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python3 -m flask_core.exc_log_audit PATH...", file=sys.stderr)
        return 2
    report = audit_paths(Path(a) for a in args)
    for finding in report.findings:
        print(finding.render())
    print(
        f"exc_log_audit: files={report.files_examined} log_calls={report.log_calls_examined} "
        f"findings={len(report.findings)}"
    )
    if report.files_examined == 0 or report.log_calls_examined == 0:
        print("exc_log_audit: FAIL -- nothing examined", file=sys.stderr)
        return 1
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
