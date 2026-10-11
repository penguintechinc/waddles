"""Every `AAALogger.audit()` / `AAALogger.auth()` call site in the repo must execute.

# regression: 91 `logger.audit(...)` call sites (analytics-core, reputation, security-core,
# labels, the interactive action modules, the consolidated `services/*` apps and the trigger
# receivers) -- plus two `logger.auth(...)` calls on the Kick webhook invalid-signature
# path -- did not match `AAALogger.audit(action, user, community, result, **extras)`:
#
# * a message passed as the first positional AND `action=...` -> `TypeError: got multiple
#   values for argument 'action'`;
# * `user` / `community` / `result` omitted -> `TypeError: missing required arguments`;
# * an extra named `event_type` -> collides with `AAALogger._build_extra(event_type, ...)`.
#
# Each raised on the SUCCESS path, after the work was already done (e.g.
# `HealthService.calculate_health_score` stored its snapshot, then 500'd;
# `PolicyEnforcer.disable_auto_ban` committed the UPDATE, then returned False).
#
# Handler-level tests cannot reasonably cover ~150 call sites spread over ~35 modules with
# their own DAL / HTTP / platform-SDK fixtures, so this sweep does the part that matters:
# it finds every `logger.audit(` / `self.logger.audit(` / `.auth(` call in the repo by AST,
# rebuilds the *exact same call expression* (same positional args, same keyword names,
# same value expressions) and executes it against the REAL `AAALogger` -- no signature
# re-implementation, so `_build_extra` collisions and formatter errors are caught too.
# Free variables in the value expressions resolve to a permissive stand-in object.

Mutation check built in: `BROKEN_SHAPES` are the pre-fix shapes and MUST raise (so a sweep
that silently stopped catching anything fails here); `VALID_SHAPES` MUST pass.
"""

from __future__ import annotations

import ast
import builtins
import logging
import os
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "flask_core"
if "flask_core" not in sys.modules:
    _stub = types.ModuleType("flask_core")
    _stub.__path__ = [str(_PKG_DIR)]
    sys.modules["flask_core"] = _stub

from flask_core.logging_config import AAALogger, StructuredFormatter  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
_PRUNE_DIRS = frozenset(
    {
        ".git", ".venv", "venv", "node_modules", ".worktrees", ".claude", "worktrees",
        "site-packages", "target", "dist", "build", "__pycache__", "tests", "test",
    }
)  # fmt: skip
_AUDITED_METHODS = frozenset({"audit", "auth"})
_LOGGER_RECEIVERS = frozenset({"logger", "self.logger"})

#: The two call sites the original bug report named -- the sweep must reach both.
NAMED_SITES = (
    "core/analytics_core_module/services/health_service.py",
    "core/analytics_core_module/jobs/bot_score_calculator.py",
)


@dataclass(slots=True, frozen=True)
class CallSite:
    """One `<logger>.audit(...)` / `<logger>.auth(...)` call found in source."""

    path: str
    line: int
    method: str
    expression: ast.Expression

    @property
    def ident(self) -> str:
        """Return ``path:line`` (stable pytest id)."""
        return f"{self.path}:{self.line}"


class _Anything:
    """Permissive stand-in for any runtime value a call-site argument expression touches."""

    def __getattr__(self, name: str) -> _Anything:
        return _Anything()

    def __call__(self, *args: Any, **kwargs: Any) -> _Anything:
        return _Anything()

    def __getitem__(self, key: Any) -> _Anything:
        return _Anything()

    def __iter__(self) -> Iterator[Any]:
        return iter(())

    def __len__(self) -> int:
        return 1

    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        return "anything"

    def __format__(self, spec: str) -> str:
        return "anything"

    def __lt__(self, other: Any) -> bool:
        return True

    __le__ = __gt__ = __ge__ = __lt__

    def __add__(self, other: Any) -> _Anything:
        return self

    __radd__ = __sub__ = __mul__ = __truediv__ = __add__


class _Namespace(dict[str, Any]):
    """Name lookup that yields builtins (`str`, `len`, ...) and `_Anything` for the rest."""

    def __missing__(self, key: str) -> Any:
        if hasattr(builtins, key):
            return getattr(builtins, key)
        return _Anything()


class _Collector(logging.Handler):
    """Keeps every emitted record so the sweep can assert on the AAA fields."""

    def __init__(self) -> None:
        """Accept every level."""
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Store the record."""
        self.records.append(record)


def _iter_source_files(root: Path) -> Iterator[Path]:
    """Yield production `.py` files under `root` (no tests, venvs, worktrees, build output)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _PRUNE_DIRS)
        for name in sorted(filenames):
            if name.endswith(".py") and not name.startswith("test_") and name != "conftest.py":
                yield Path(dirpath) / name


def _receiver(node: ast.Call) -> str | None:
    """Return the dotted receiver of `<receiver>.<method>(...)`, or None for other shapes."""
    func = node.func
    if isinstance(func, ast.Attribute):
        try:
            return ast.unparse(func.value)
        except ValueError:  # pragma: no cover - unparse failure on exotic AST
            return None
    return None


def find_call_sites(source: str, path: str) -> list[CallSite]:
    """Return every AAALogger audit/auth call in `source`."""
    tree = ast.parse(source, filename=path)
    sites: list[CallSite] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _AUDITED_METHODS
            and _receiver(node) in _LOGGER_RECEIVERS
        ):
            call = ast.Call(
                func=ast.Name(id="__aaa_method__", ctx=ast.Load()),
                args=node.args,
                keywords=node.keywords,
            )
            expression = ast.fix_missing_locations(ast.Expression(body=call))
            ast.copy_location(call, node)
            sites.append(CallSite(path, node.lineno, node.func.attr, expression))
    return sorted(sites, key=lambda s: (s.path, s.line))


def _repo_call_sites() -> list[CallSite]:
    """Collect every audit/auth call site under the repo root."""
    sites: list[CallSite] = []
    for file in _iter_source_files(REPO_ROOT):
        try:
            source = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - non-UTF8 vendored file
            continue
        if ".audit(" not in source and ".auth(" not in source:
            continue
        try:
            sites.extend(find_call_sites(source, file.relative_to(REPO_ROOT).as_posix()))
        except SyntaxError:  # pragma: no cover - py2-era / templated file; not a call site
            continue
    return sites


def execute_call(site: CallSite, logger: AAALogger, collector: _Collector) -> logging.LogRecord:
    """Run the call expression against the real logger; return the single record it emitted."""
    namespace = _Namespace(__aaa_method__=getattr(logger, site.method))
    collector.records.clear()
    code = compile(site.expression, filename=site.path, mode="eval")
    eval(code, {"__builtins__": builtins.__dict__}, namespace)  # noqa: S307 - own AST, test only
    assert len(collector.records) == 1, f"{site.ident}: expected exactly 1 log record"
    return collector.records[0]


@pytest.fixture
def real_logger(tmp_path: Path) -> Iterator[tuple[AAALogger, _Collector]]:
    """Provide a real `AAALogger` (console + file handlers) with a record collector attached."""
    aaa = AAALogger("aaa-call-site-sweep", "test", log_level="DEBUG", log_dir=str(tmp_path))
    collector = _Collector()
    aaa.logger.addHandler(collector)
    try:
        yield aaa, collector
    finally:
        aaa.logger.removeHandler(collector)
        for handler in list(aaa.logger.handlers):
            handler.close()


CALL_SITES = _repo_call_sites()


def test_sweep_denominator_reaches_the_named_sites() -> None:
    """The sweep must actually examine call sites -- incl. the two the bug report named."""
    paths = {s.path for s in CALL_SITES}
    assert len(CALL_SITES) > 0, "no audit()/auth() call sites found -- scanner mis-pointed"
    for named in NAMED_SITES:
        assert named in paths, f"sweep did not reach {named}"


@pytest.mark.parametrize("site", CALL_SITES, ids=[s.ident for s in CALL_SITES])
def test_call_site_executes_against_real_aaa_logger(
    site: CallSite, real_logger: tuple[AAALogger, _Collector]
) -> None:
    """The call binds to the real signature, does not raise, and logs actor + action + result.

    # regression: TypeError on the success path (see module docstring).
    """
    aaa, collector = real_logger

    record = execute_call(site, aaa, collector)

    assert record.event_type == site.method.upper()  # type: ignore[attr-defined]
    assert record.action, f"{site.ident}: empty action"  # type: ignore[attr-defined]
    assert record.result, f"{site.ident}: empty result"  # type: ignore[attr-defined]
    if site.method == "audit":
        assert record.user, f"{site.ident}: audit event without an actor"  # type: ignore[attr-defined]
    # the structured line renders (every extra is formattable) and names the actor
    rendered = StructuredFormatter("aaa-call-site-sweep", "test").format(record)
    assert f"action={record.action}" in rendered  # type: ignore[attr-defined]


#: (id, source) -- the pre-fix shapes; each MUST raise `TypeError` when executed.
BROKEN_SHAPES: list[tuple[str, str]] = [
    (
        "message-positional-plus-action-kw (health_service / bot_score_calculator)",
        'self.logger.audit("Community health calculated", community_id=1,'
        ' action="calculate_health_score", result="SUCCESS")',
    ),
    ("missing-user", 'logger.audit(action="x", community=1, result="SUCCESS")'),
    ("missing-community", 'logger.audit(action="x", user="u", result="SUCCESS")'),
    ("missing-result", 'logger.audit(action="x", user="u", community=1)'),
    (
        "reserved-event_type-extra",
        'logger.audit(action="x", user="u", community=1, result="SUCCESS", event_type="e")',
    ),
    ("auth-message-positional", 'logger.auth("bad sig", action="verify", result="FAILURE")'),
]

#: (id, source) -- the post-fix shapes; each MUST pass.
VALID_SHAPES: list[tuple[str, str]] = [
    (
        "keyword-complete",
        'self.logger.audit(action="calculate_health_score", user="system",'
        ' community=str(community_id), result="SUCCESS", detail="Community health calculated")',
    ),
    ("positional-complete", 'logger.audit("x", "u", "1", "SUCCESS", extra=1)'),
    (
        "auth-keyword-complete",
        'logger.auth(action="verify", user="anonymous", result="FAILURE", detail="bad sig")',
    ),
]


@pytest.mark.parametrize(("name", "source"), BROKEN_SHAPES, ids=[n for n, _ in BROKEN_SHAPES])
def test_pre_fix_shape_is_caught(
    name: str, source: str, real_logger: tuple[AAALogger, _Collector]
) -> None:
    """Mutation check: the sweep's executor raises on every shape the bug produced."""
    aaa, collector = real_logger
    (site,) = find_call_sites(source, f"<{name}>")

    with pytest.raises(TypeError):
        execute_call(site, aaa, collector)


@pytest.mark.parametrize(("name", "source"), VALID_SHAPES, ids=[n for n, _ in VALID_SHAPES])
def test_post_fix_shape_passes(
    name: str, source: str, real_logger: tuple[AAALogger, _Collector]
) -> None:
    """The sweep must not cry wolf on correctly-shaped calls."""
    aaa, collector = real_logger
    (site,) = find_call_sites(source, f"<{name}>")

    record = execute_call(site, aaa, collector)

    assert record.action and record.result  # type: ignore[attr-defined]


def test_find_call_sites_ignores_other_receivers_and_methods() -> None:
    """Only `logger`/`self.logger` `.audit`/`.auth` are examined (no false denominators)."""
    source = (
        "def f(db, logger):\n"
        "    db.audit('x')\n"
        "    logger.info('x')\n"
        "    other.logger.audit('x')\n"
        "    logger.audit(action='a', user='u', community='c', result='r')\n"
    )

    sites = find_call_sites(source, "<mem>")

    assert [(s.line, s.method) for s in sites] == [(5, "audit")]
