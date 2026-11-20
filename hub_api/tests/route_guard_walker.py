"""AST helpers for the route-guard CI walkers (`test_read_route_guards.py`).

A route only counts as enforced when its call chain really CALLS an authorization primitive.
Comments, docstrings, string literals and constant-dead branches never count, so a handler that
merely mentions `_require_admin` in a comment cannot satisfy the walker. `mark_guarded` is only a
label (`services.route_guards`); `inline_guard_gaps` makes every stamped route additionally prove
a keyed deny test exists.
"""

from __future__ import annotations

# ruff: noqa: N802, N815
import ast
import inspect
import textwrap
from collections.abc import Callable, Iterable, Iterator
from types import FunctionType, ModuleType
from typing import Any

from services.route_guards import GUARD_ATTR, INLINE_ATTR

#: Authorization primitives: calling any of these in a route's call chain is real enforcement.
#: Local helpers (`_authorize`, `_gate`, `_member_gate`, ...) are not listed; the walker follows
#: them to the primitive they call.
ENFORCEMENT_CALLS: frozenset[str] = frozenset(
    {
        "authorize_community",
        "authorize_reputation_admin",
        "require_community_scope",
        "require_community_admin",
        "require_community_member",
        "require_community_admin_scoped",
        "require_community_member_scoped",
        "resolve_community_membership_scoped",
        "resolve_editor_scopes",
        "resolve_editor_scopes_async",
        "resolve_caller_standing_sync",
        "member_scopes_sync",
        "is_community_admin",
        "community_admin_guard",
        "community_member_guard",
    }
)

#: How many helper hops the walker follows from a handler (handler -> helper -> service -> ...).
_MAX_DEPTH = 4


def chain(view: Any) -> list[Any]:
    """The view plus every function it wraps (`functools.wraps` `__wrapped__` chain)."""
    out = []
    while view is not None:
        out.append(view)
        view = getattr(view, "__wrapped__", None)
    return out


def is_inline_stamped(view: Any) -> bool:
    """True when the view carries a `mark_guarded` stamp (a label, not enforcement)."""
    return any(getattr(fn, INLINE_ATTR, False) for fn in chain(view))


def guard_kind(view: Any) -> str | None:
    """The `__authz_guard__` label anywhere in the view's wrapper chain."""
    for fn in chain(view):
        kind = getattr(fn, GUARD_ATTR, None)
        if kind:
            return str(kind)
    return None


def _constant_truth(test: ast.expr) -> bool | None:
    """Truthiness of a literal `if` test (`if False:` / `if 0:`), else None (unknown)."""
    if isinstance(test, ast.Constant):
        return bool(test.value)
    return None


class _CallCollector(ast.NodeVisitor):
    """Collects Call nodes that can execute: skips constant-dead branches and code after return."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        truth = _constant_truth(node.test)
        self.visit(node.test)
        if truth is not False:
            self._block(node.body)
        if truth is not True:
            self._block(node.orelse)

    def visit_While(self, node: ast.While) -> None:
        if _constant_truth(node.test) is False:
            self._block(node.orelse)
            return
        self.generic_visit(node)

    def _block(self, stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            self.visit(stmt)
            if isinstance(stmt, ast.Return | ast.Raise):
                return  # everything after is unreachable

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for deco in node.decorator_list:
            self.visit(deco)
        self._block(node.body)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]


def _parse(fn: Any) -> ast.AST | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return None


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _resolve(fn: Any, call: ast.Call) -> FunctionType | None:
    """The repo function a call refers to (module global, closure cell, or `module.attr`)."""
    func = call.func
    scope = getattr(fn, "__globals__", {})
    closure: dict[str, Any] = {}
    code = getattr(fn, "__code__", None)
    cells = getattr(fn, "__closure__", None)
    if code is not None and cells:
        for name, cell in zip(code.co_freevars, cells, strict=False):
            try:
                closure[name] = cell.cell_contents
            except ValueError:
                continue
    target: Any = None
    if isinstance(func, ast.Name):
        target = closure.get(func.id, scope.get(func.id))
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        base = closure.get(func.value.id, scope.get(func.value.id))
        if isinstance(base, ModuleType):
            target = getattr(base, func.attr, None)
    if callable(target):
        target = inspect.unwrap(target)
    if not isinstance(target, FunctionType):
        return None
    filename = getattr(target.__code__, "co_filename", "")
    if "site-packages" in filename or "/lib/python" in filename:
        return None
    return target


def enforcement_calls(view: Any) -> set[str]:
    """Enforcement primitives actually CALLED in the view's chain (helpers followed)."""
    found: set[str] = set()
    seen: set[Any] = set()

    def walk(fn: Any, depth: int) -> None:
        code = getattr(fn, "__code__", None)
        if code is None or code in seen:
            return
        seen.add(code)
        tree = _parse(fn)
        if tree is None:
            return
        collector = _CallCollector()
        collector.visit(tree)
        for call in collector.calls:
            name = _call_name(call)
            if name in ENFORCEMENT_CALLS:
                found.add(name)
            elif depth < _MAX_DEPTH:
                callee = _resolve(fn, call)
                if callee is not None:
                    walk(callee, depth + 1)

    for fn in chain(view):
        walk(fn, 0)
    return found


def has_enforcement_call(view: Any) -> bool:
    """True when the view's call chain really calls an authorization primitive."""
    return bool(enforcement_calls(view))


def iter_routes(app: Any) -> Iterator[tuple[str, str, Any]]:
    """`(METHOD, rule, view)` for every non-HEAD/OPTIONS route of a Quart app."""
    for rule in app.url_map.iter_rules():
        view = app.view_functions[rule.endpoint]
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            yield method, rule.rule, view


def inline_guard_gaps(
    routes: Iterable[tuple[str, str, Any]],
    registry: dict[tuple[str, str], Callable[..., Any]],
) -> list[str]:
    """Problems with `mark_guarded` routes: no real enforcement call, or no keyed deny test.

    `registry` maps `(METHOD, rule)` to the deny test function that proves a plain member is
    refused with the real guards active.
    """
    problems: list[str] = []
    for method, rule, view in routes:
        if not is_inline_stamped(view):
            continue
        if not has_enforcement_call(view):
            problems.append(f"{method} {rule}: stamped mark_guarded but no enforcement call")
        if (method, rule) not in registry:
            problems.append(f"{method} {rule}: stamped mark_guarded but no keyed deny test")
    return problems
