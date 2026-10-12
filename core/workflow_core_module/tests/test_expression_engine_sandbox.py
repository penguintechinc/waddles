"""Regression tests: the workflow expression engine must not be sandbox-escapable.

Release-audit finding: `ExpressionEvaluator.evaluate_ast` ran validated ASTs
through `eval(..., {"__builtins__": {}})`, a known-bypassable sandbox
(`().__class__.__bases__[0].__subclasses__()`), and the validator permitted
the `Attribute`/`Subscript`/`Call` chain needed for it. The evaluator is now
an allowlist AST walker. These payload strings are only ever parsed/walked,
never executed by Python's own eval.
"""

from __future__ import annotations

import pytest

from services.expression_engine import (
    ExpressionContext,
    ExpressionEngine,
    ExpressionEngineException,
    ExpressionEvaluationError,
    ExpressionEvaluator,
    ExpressionParser,
    ExpressionSecurityError,
)
from services.webhook_executor import SafeExpressionEvaluator, UnsafeExpressionError

ESCAPE_PAYLOADS = [
    "().__class__.__bases__[0].__subclasses__()",
    "''.__class__.__mro__[1].__subclasses__()",
    "x.__class__",
    "x.__class__.__init__.__globals__",
    "[c for c in ().__class__.__bases__[0].__subclasses__()]",
    "__import__('os').system('id')",
    "__class__('x')",
    "open('/etc/passwd')",
    "getattr(x, 'a')",
    "(lambda: 1)()",
    "x._private",
    "x.keys()",
    "upper.__call__('a')",
    "uppercase.__globals__",
    "'a'.format.__self__",
    "f(**{'a': 1})",
    "uppercase(s='a')",
    "[1,2][1:]",
]


@pytest.mark.parametrize("payload", ESCAPE_PAYLOADS)
def test_parser_rejects_sandbox_escape_payloads(payload: str) -> None:
    with pytest.raises((ExpressionSecurityError, ExpressionEngineException)):
        ExpressionParser.parse_expression(payload)


@pytest.mark.parametrize("payload", ESCAPE_PAYLOADS)
def test_engine_never_evaluates_sandbox_escape_payloads(payload: str) -> None:
    engine = ExpressionEngine(ExpressionContext({"x": {"a": 1}, "s": "a"}))
    result = engine.evaluate(payload)
    assert result.success is False
    assert result.value is None


def test_evaluator_rejects_escape_even_when_parser_is_bypassed() -> None:
    """Defence in depth: a hand-built AST that skipped the parser is refused."""
    import ast

    tree = ast.parse("().__class__.__bases__[0].__subclasses__()", mode="eval")
    with pytest.raises(ExpressionSecurityError):
        ExpressionEvaluator.evaluate_ast(tree, ExpressionContext())


def test_context_dunder_variable_is_not_reachable() -> None:
    ctx = ExpressionContext({"__secret": 1, "ok": 2})
    with pytest.raises(ExpressionSecurityError):
        ExpressionEvaluator.evaluate_expression("__secret", ctx)
    assert ExpressionEvaluator.evaluate_expression("ok", ctx) == 2


def test_attribute_access_is_dict_key_lookup_not_getattr() -> None:
    ctx = ExpressionContext({"user": {"name": "penguin"}})
    assert ExpressionEvaluator.evaluate_expression("user.name", ctx) == "penguin"
    with pytest.raises(ExpressionEvaluationError):
        ExpressionEvaluator.evaluate_expression("user.title", ctx)  # no such key
    # `.keys` is a dict METHOD, not a key -- must not resolve via getattr
    with pytest.raises(ExpressionEvaluationError):
        ExpressionEvaluator.evaluate_expression("user.keys", ctx)


@pytest.mark.parametrize(
    ("expression", "variables", "expected"),
    [
        ("1 + 2 * 3", {}, 7),
        ("(10 - 4) / 2", {}, 3.0),
        ("7 // 2 + 7 % 4", {}, 6),
        ("2 ** 10", {}, 1024),
        ("-x + +y", {"x": 3, "y": 1}, -2),
        ("not flag", {"flag": False}, True),
        ("x > 5 and y < 3", {"x": 10, "y": 1}, True),
        ("x > 5 or y > 3", {"x": 1, "y": 1}, False),
        ("1 < x <= 5", {"x": 5}, True),
        ("user.level >= 5", {"user": {"level": 5}}, True),
        ("items[1].name", {"items": [{"name": "a"}, {"name": "b"}]}, "b"),
        ("m['k']", {"m": {"k": "v"}}, "v"),
        ("[1, 2, x]", {"x": 3}, [1, 2, 3]),
        ("{'a': x}", {"x": 1}, {"a": 1}),
        ("uppercase(name)", {"name": "hi"}, "HI"),
        ("name.upper()", {"name": "hi"}, "HI"),
        ("name.replace('i', 'ey')", {"name": "hi"}, "hey"),
        ("max(1, 5, 3) + length('abc')", {}, 8),
        ("'a' + 'b'", {}, "ab"),
        ("None == None", {}, True),
    ],
)
def test_legitimate_expressions_still_evaluate(
    expression: str, variables: dict, expected: object
) -> None:
    engine = ExpressionEngine(ExpressionContext(variables))
    result = engine.evaluate(expression)
    assert result.success is True, result.error
    assert result.value == expected


def test_template_substitution_still_works() -> None:
    engine = ExpressionEngine(ExpressionContext({"user": {"name": "penguin"}}))
    result = engine.substitute("Hello {{user.name}} / {{1 + 1}}")
    assert result.success is True
    assert result.value == "Hello penguin / 2"


def test_resource_exhaustion_is_refused() -> None:
    engine = ExpressionEngine()
    assert engine.evaluate("9 ** 9 ** 9").success is False
    assert engine.evaluate("'a' * 1000000000").success is False
    assert engine.evaluate("'a' * 10").success is True


def test_overlong_expression_rejected() -> None:
    with pytest.raises(ExpressionSecurityError):
        ExpressionParser.parse_expression("1 + " * 3000 + "1")


def test_no_eval_in_evaluator_source() -> None:
    import ast
    import inspect

    import services.expression_engine as mod

    tree = ast.parse(inspect.getsource(mod))
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert called.isdisjoint({"eval", "exec", "compile", "__import__"})


@pytest.mark.parametrize(
    "payload",
    [
        "().__class__.__bases__[0].__subclasses__()",
        "x.__class__",
        "__import__('os')",
        "9 ** 9 ** 9",
    ],
)
def test_webhook_safe_evaluator_rejects_escapes_and_bombs(payload: str) -> None:
    with pytest.raises(UnsafeExpressionError):
        SafeExpressionEvaluator({"x": 1}).evaluate(payload)


def test_webhook_safe_evaluator_still_evaluates() -> None:
    assert SafeExpressionEvaluator({"x": 2}).evaluate("x * 3 + 1") == 7
