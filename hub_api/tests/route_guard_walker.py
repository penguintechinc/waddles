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
import importlib
import inspect
import linecache
import textwrap
from collections.abc import Callable, Iterable, Iterator
from types import FunctionType, ModuleType
from typing import Any

from services.route_guards import GUARD_ATTR, INLINE_ATTR

#: Authorization primitives, compared BY IDENTITY after resolving the called name through the
#: caller's globals/closure -- a local no-op that merely shares a primitive's name never matches.
#: Local helpers (`_authorize`, `_gate`, `_member_gate`, ...) are not listed; the walker follows
#: them to the primitive they call.
_RAISING = (
    ("services.community_authz", "authorize_community"),
    ("services.community_authz", "require_community_scope"),
    ("services.community_authz", "require_community_admin"),
    ("services.community_authz", "require_community_admin_scoped"),
    ("services.community_authz", "require_community_member_scoped"),
    ("services.community_access", "require_community_admin"),
    ("services.community_access", "require_community_member"),
    ("services.route_guards", "community_admin_guard"),
    ("services.route_guards", "community_member_guard"),
)
#: Primitives that RETURN the caller's standing instead of raising: the result must be consumed
#: by a deny decision (`if not standing.is_admin: return ...`), never ignored.
_VALUE = (
    ("services.community_authz", "resolve_caller_standing_sync"),
    ("services.community_authz", "resolve_community_membership_scoped"),
    ("services.community_authz", "resolve_editor_scopes"),
    ("services.community_authz", "resolve_editor_scopes_async"),
    ("services.community_authz", "member_scopes_sync"),
    ("services.marketplace_billing_service", "is_community_admin"),
)

#: How many helper hops the walker follows from a handler (handler -> helper -> service -> ...).
_MAX_DEPTH = 4

_PRIMS: dict[int, tuple[str, bool]] | None = None


def _primitives() -> dict[int, tuple[str, bool]]:
    """`id(object) -> (name, returns_value)` for every real authorization primitive."""
    global _PRIMS
    if _PRIMS is None:
        table: dict[int, tuple[str, bool]] = {}
        for entries, returns_value in ((_RAISING, False), (_VALUE, True)):
            for module, name in entries:
                obj = getattr(importlib.import_module(module), name)
                table[id(obj)] = (name, returns_value)
        _PRIMS = table
    return _PRIMS


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


_TRUSTED_GUARD_CODES: frozenset[Any] | None = None


def _trusted_guard_codes() -> frozenset[Any]:
    """Code objects of the REAL guard wrappers (`services.route_guards`).

    Built by applying each guard to a dummy handler. `__authz_guard__` is just an attribute anyone
    can `setattr`, so a label only counts when it sits on a wrapper that is genuinely one of these.
    """
    global _TRUSTED_GUARD_CODES
    if _TRUSTED_GUARD_CODES is None:
        route_guards = importlib.import_module("services.route_guards")

        async def _dummy() -> None:
            return None

        wrappers = (
            route_guards.community_admin_guard(_dummy),
            route_guards.community_member_guard(_dummy),
            route_guards.tenant_admin_guard(_dummy),
            route_guards.platform_analytics_guard(_dummy),
        )
        _TRUSTED_GUARD_CODES = frozenset(w.__code__ for w in wrappers)
    return _TRUSTED_GUARD_CODES


def guard_kind(view: Any) -> str | None:
    """The guard label of a REAL guard wrapper (or a `mark_guarded` stamp) in the view's chain.

    A hand-stamped `__authz_guard__` attribute with no real wrapper behind it is ignored; a
    `mark_guarded` stamp is returned but is still only a label (`inline_guard_gaps` demands an
    enforcement call plus a keyed deny test for it).
    """
    trusted = _trusted_guard_codes()
    for fn in chain(view):
        kind = getattr(fn, GUARD_ATTR, None)
        if kind and (getattr(fn, "__code__", None) in trusted or getattr(fn, INLINE_ATTR, False)):
            return str(kind)
    return None


def _parse(fn: Any) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The function's OWN source as an AST node.

    `inspect.getsource` unwraps `functools.wraps` chains and would hand back the innermost
    handler for every wrapper, so the wrapper bodies (where decorator guards live) would never be
    analysed. Read the block at the code object's own first line instead.
    """
    code = getattr(fn, "__code__", None)
    if code is None:
        return None
    lines = linecache.getlines(code.co_filename)
    if not lines:
        return None
    try:
        block = inspect.getblock(lines[code.co_firstlineno - 1 :])
        tree = ast.parse(textwrap.dedent("".join(block)))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return None
    node = tree.body[0] if tree.body else None
    return node if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) else None


def _scope_of(fn: Any) -> dict[str, Any]:
    """Names visible to `fn`: module globals overlaid with its closure cells."""
    scope = dict(getattr(fn, "__globals__", {}))
    code = getattr(fn, "__code__", None)
    cells = getattr(fn, "__closure__", None)
    if code is not None and cells:
        for name, cell in zip(code.co_freevars, cells, strict=False):
            try:
                scope[name] = cell.cell_contents
            except ValueError:
                continue
    return scope


def _resolve(scope: dict[str, Any], node: ast.expr) -> Any:
    """The object a Name / `module.attr` expression refers to, else None."""
    if isinstance(node, ast.Name):
        return scope.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        base = scope.get(node.value.id)
        if isinstance(base, ModuleType):
            return getattr(base, node.attr, None)
    return None


def _repo_function(obj: Any) -> FunctionType | None:
    """The repo-owned function behind `obj` (wrappers unwrapped), or None for libs/builtins."""
    if not callable(obj):
        return None
    target = inspect.unwrap(obj)
    if not isinstance(target, FunctionType):
        return None
    filename = target.__code__.co_filename
    if "site-packages" in filename or "/lib/python" in filename:
        return None
    return target


def _is_broad_try(node: ast.Try) -> bool:
    """A `try` whose handler swallows everything (bare/Exception/BaseException, no re-raise)."""
    for handler in node.handlers:
        broad = handler.type is None or (
            isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException")
        )
        if broad and not any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
            return True
    return False


class _Facts:
    """Function-wide name facts: which names feed a deny decision, which are read at all."""

    def __init__(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        consumed: set[str] = set()
        assigns: list[tuple[set[str], set[str]]] = []
        self.loaded: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                self.loaded.add(sub.id)
            if isinstance(sub, ast.If):
                if any(
                    isinstance(n, ast.Return | ast.Raise)
                    for part in (sub.body, sub.orelse)
                    for x in part
                    for n in ast.walk(x)
                ):
                    consumed |= _names(sub.test)
            elif isinstance(sub, ast.IfExp | ast.Assert):
                consumed |= _names(sub.test)
            elif isinstance(sub, ast.Return) and sub.value is not None:
                consumed |= _names(sub.value)
            elif isinstance(sub, ast.Raise):
                for part in (sub.exc, sub.cause):
                    if part is not None:
                        consumed |= _names(part)
            elif isinstance(sub, ast.Assign | ast.AnnAssign | ast.AugAssign) and sub.value:
                targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                assigns.append(
                    (
                        {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)},
                        _names(sub.value),
                    )
                )
        changed = True
        while changed:
            changed = False
            for tgt_names, read_names in assigns:
                if tgt_names & consumed and not read_names <= consumed:
                    consumed |= read_names
                    changed = True
        self.consumed = consumed


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _terminates(stmts: list[ast.stmt]) -> bool:
    """True when the block always ends in return/raise (a deny/early exit path)."""
    return any(isinstance(s, ast.Return | ast.Raise) for s in stmts)


class _Analyzer:
    """All-paths enforcement analysis of one function (and the helpers it calls)."""

    def __init__(self, depth: int, seen: set[Any], *, decorators: bool = True) -> None:
        self.depth, self.seen, self.decorators = depth, seen, decorators

    def function(self, fn: Any) -> set[str]:
        code = getattr(fn, "__code__", None)
        if code is None or code in self.seen:
            return set()  # recursion guard: `seen` is the CURRENT call stack, not a global memo
        self.seen.add(code)
        try:
            return self._function(fn)
        finally:
            self.seen.discard(code)

    def _function(self, fn: Any) -> set[str]:
        node = _parse(fn)
        if node is None:
            return set()
        self.scope, self.facts = _scope_of(fn), _Facts(node)
        hits: set[str] = set()
        for deco in node.decorator_list if self.decorators else []:
            obj = _resolve(self.scope, deco.func if isinstance(deco, ast.Call) else deco)
            if obj is not None and id(obj) in _primitives():
                hits.add(_primitives()[id(obj)][0])
        return hits | self.block(node.body)

    def block(self, stmts: list[ast.stmt]) -> set[str]:
        """Primitives enforced on EVERY path through `stmts` (early return/raise ends the walk)."""
        hits: set[str] = set()
        for st in stmts:
            hits |= self.stmt(st)
            if isinstance(st, ast.Return | ast.Raise):
                break
        return hits

    def stmt(self, st: ast.stmt) -> set[str]:
        if isinstance(st, ast.If):
            hits = self.expr(st.test, assigned=None, bare=False)
            if isinstance(st.test, ast.Constant):
                return hits | self.block(st.body if st.test.value else st.orelse)
            body, other = self.block(st.body), self.block(st.orelse)
            if st.orelse and body and other:
                hits |= body | other  # both branches enforce
            return hits
        if isinstance(st, ast.Try):
            if _is_broad_try(st):
                return set()  # a swallowing handler turns a denial into a pass
            return self.block(st.body) | self.block(st.finalbody)
        if isinstance(st, ast.With | ast.AsyncWith):
            hits = set()
            for item in st.items:
                hits |= self.expr(item.context_expr, assigned=None, bare=False)
            return hits | self.block(st.body)
        if isinstance(st, ast.Return | ast.Raise | ast.Assert):
            parts = (
                [st.value]
                if isinstance(st, ast.Return)
                else [getattr(st, "exc", None), getattr(st, "test", None)]
            )
            hits = set()
            for part in parts:
                if part is not None:
                    hits |= self.expr(part, assigned=None, bare=False)
            return hits
        if isinstance(st, ast.Assign | ast.AnnAssign | ast.AugAssign):
            if st.value is None:
                return set()
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            names = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            return self.expr(st.value, assigned=names, bare=False)
        if isinstance(st, ast.Expr):
            return self.expr(st.value, assigned=None, bare=True)
        return set()  # loops, nested defs, match: not guaranteed to run

    def expr(self, node: ast.expr, *, assigned: set[str] | None, bare: bool) -> set[str]:
        direct = node.value if isinstance(node, ast.Await) else node
        conditional: set[int] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.IfExp):
                conditional |= {id(n) for part in (sub.body, sub.orelse) for n in ast.walk(part)}
            elif isinstance(sub, ast.BoolOp) and bare:
                conditional |= {id(n) for part in sub.values[1:] for n in ast.walk(part)}
            elif isinstance(sub, ast.Lambda):
                conditional |= {id(n) for n in ast.walk(sub)}
        hits: set[str] = set()
        for sub in ast.walk(node):
            if id(sub) in conditional:
                continue
            if isinstance(sub, ast.Call):
                hits |= self.call(
                    sub, sub.func, is_call=True, top=sub is direct, assigned=assigned, bare=bare
                )
                for arg in [*sub.args, *(k.value for k in sub.keywords)]:
                    if isinstance(arg, ast.Name | ast.Attribute):
                        hits |= self.call(
                            arg, arg, is_call=False, top=False, assigned=assigned, bare=False
                        )
        return hits

    def call(
        self,
        node: ast.expr,
        target: ast.expr,
        *,
        is_call: bool,
        top: bool,
        assigned: set[str] | None,
        bare: bool,
    ) -> set[str]:
        obj = _resolve(self.scope, target)
        if obj is None:
            return set()
        raw = inspect.unwrap(obj) if callable(obj) else obj
        prim = _primitives().get(id(obj)) or _primitives().get(id(raw))
        names = assigned if (top or assigned is not None) else None
        if prim is not None:
            name, returns_value = prim
            if not is_call:
                return set()
            if returns_value and (
                (top and bare) or (names is not None and not names & self.facts.consumed)
            ):
                return set()  # standing fetched but never used in a deny decision
            return {name}
        helper = _repo_function(obj)
        if helper is None or self.depth >= _MAX_DEPTH:
            return set()
        if names is not None and not names & self.facts.loaded:
            return set()  # helper's verdict ignored
        inner = _Analyzer(self.depth + 1, self.seen)
        return inner.function(helper)


def enforcement_calls(view: Any, *, handler_only: bool = False) -> set[str]:
    """Authorization primitives the view's chain enforces on every path (helpers followed).

    A primitive counts only when it is resolved by identity, runs on every path (not under a
    one-armed `if`/loop/`except`, not in a `try` that swallows everything) and -- for
    value-returning primitives -- its result feeds a deny decision. Decorator guards count by
    identity too. `handler_only` analyses just the innermost handler body (no wrappers, no
    decorators), so an outer guard cannot vouch for a stamp on the handler underneath it.
    """
    seen: set[Any] = set()
    found: set[str] = set()
    if handler_only:
        return _Analyzer(0, seen, decorators=False).function(chain(view)[-1])
    for fn in chain(view):
        found |= _Analyzer(0, seen).function(fn)
    return found


def has_enforcement_call(view: Any, *, handler_only: bool = False) -> bool:
    """True when the view's call chain really enforces through an authorization primitive."""
    return bool(enforcement_calls(view, handler_only=handler_only))


def required_scopes(view: Any) -> list[str]:
    """Scopes passed to `require_scope(...)` anywhere in the view's wrapper chain."""
    scopes: list[str] = []
    for fn in chain(view):
        code = getattr(fn, "__code__", None)
        cells = getattr(fn, "__closure__", None)
        if code is not None and cells and "required_scopes" in code.co_freevars:
            scopes.extend(cells[code.co_freevars.index("required_scopes")].cell_contents)
    return scopes


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
        if not has_enforcement_call(view, handler_only=True):
            problems.append(f"{method} {rule}: stamped mark_guarded but no enforcement call")
        if (method, rule) not in registry:
            problems.append(f"{method} {rule}: stamped mark_guarded but no keyed deny test")
    return problems
