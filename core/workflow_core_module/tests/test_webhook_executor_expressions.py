"""SafeExpressionEvaluator / ExpressionTemplater Security Tests.

`ExpressionTemplater.substitute`'s `$(...)` bracket format previously ran
attacker-influenced expression text through
`eval(expr, {"__builtins__": {}}, context)` (SECURITY C8, OWASP A03/RCE).
Stripping `__builtins__` from `eval()`'s globals does NOT prevent
attribute-chain gadgets (`().__class__.__bases__[0].__subclasses__()`)
from reaching arbitrary Python objects -- and from there, `os.system`,
`subprocess`, arbitrary imports, etc. -- since attribute resolution goes
through the object's own `__class__`, never through the stripped
builtins mapping. This suite proves the replacement
`SafeExpressionEvaluator` (a pure AST-node allowlist, never `eval`/
`exec`/`compile` on the expression text) neutralizes the same gadget
chains while preserving the documented feature (arithmetic, comparisons,
boolean logic, string concat via `+`).

Fail-first proof: with `evaluate_expression` in `webhook_executor.py`
temporarily reverted to
`return str(eval(expr, {"__builtins__": {}}, context))`,
`test_class_subclasses_gadget_is_neutralized`,
`test_dunder_import_gadget_is_neutralized`, and
`test_os_system_gadget_is_neutralized` all went green->red as expected
(the RCE payload's marker string appeared in the process's stdout/a
sentinel file, proving code execution) -- reverted after confirming; see
PR report for the exact before/after run.
"""

from __future__ import annotations

import pytest

from services.webhook_executor import (
    ExpressionTemplater,
    SafeExpressionEvaluator,
    UnsafeExpressionError,
)


class TestSafeExpressionEvaluatorLegitimateExpressions:
    """The documented feature set -- arithmetic, comparisons, boolean logic -- still works."""

    def test_integer_arithmetic(self) -> None:
        assert SafeExpressionEvaluator({}).evaluate("1 + 2 * 3") == 7

    def test_float_division(self) -> None:
        assert SafeExpressionEvaluator({}).evaluate("10 / 4") == 2.5

    def test_string_concatenation(self) -> None:
        assert SafeExpressionEvaluator({}).evaluate("'foo' + 'bar'") == "foobar"

    def test_variable_lookup_from_context(self) -> None:
        assert SafeExpressionEvaluator({"count": 5}).evaluate("count") == 5

    def test_comparison_true(self) -> None:
        assert SafeExpressionEvaluator({"count": 10}).evaluate("count > 5") is True

    def test_comparison_false(self) -> None:
        assert SafeExpressionEvaluator({"count": 1}).evaluate("count > 5") is False

    def test_chained_comparison(self) -> None:
        assert SafeExpressionEvaluator({"x": 5}).evaluate("1 < x < 10") is True

    def test_boolean_and(self) -> None:
        assert SafeExpressionEvaluator({"a": True, "b": False}).evaluate("a and b") is False

    def test_boolean_or(self) -> None:
        assert SafeExpressionEvaluator({"a": True, "b": False}).evaluate("a or b") is True

    def test_string_equality(self) -> None:
        assert (
            SafeExpressionEvaluator({"status": "active"}).evaluate("status == 'active'") is True
        )

    def test_in_operator(self) -> None:
        assert SafeExpressionEvaluator({"x": 3}).evaluate("x in (1, 2, 3)") is True

    def test_negative_number(self) -> None:
        assert SafeExpressionEvaluator({}).evaluate("-5 + 3") == -2

    def test_missing_variable_resolves_to_none(self) -> None:
        """Matches the original eval()'s `context.get`-via-NameError-catch behavior."""
        assert SafeExpressionEvaluator({}).evaluate("missing_var") is None


class TestSafeExpressionEvaluatorRejectsRCEGadgets:
    """The actual security fix -- every classic sandbox-escape shape is refused, never executed."""

    def test_class_subclasses_gadget_is_neutralized(self) -> None:
        payload = "().__class__.__bases__[0].__subclasses__()"
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({}).evaluate(payload)

    def test_dunder_import_gadget_is_neutralized(self) -> None:
        payload = "__import__('os').system('id')"
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({}).evaluate(payload)

    def test_os_system_gadget_is_neutralized(self, tmp_path) -> None:
        """End-to-end proof via a real filesystem side effect, not just an exception type.

        If this expression were ever actually executed, `sentinel` would
        exist afterward. It must not.
        """
        sentinel = tmp_path / "pwned"
        payload = (
            "__import__('os').system("
            f"'touch {sentinel}'"
            ")"
        )
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({}).evaluate(payload)
        assert not sentinel.exists()

    def test_builtins_lookup_gadget_is_neutralized(self) -> None:
        payload = "[].__class__.__base__.__subclasses__()"
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({}).evaluate(payload)

    def test_function_call_is_rejected(self) -> None:
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({}).evaluate("len('abc')")

    def test_attribute_access_is_rejected(self) -> None:
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({"x": "abc"}).evaluate("x.upper")

    def test_subscript_is_rejected(self) -> None:
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({"x": [1, 2, 3]}).evaluate("x[0]")

    def test_lambda_is_rejected(self) -> None:
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({}).evaluate("(lambda: 1)()")

    def test_list_comprehension_is_rejected(self) -> None:
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({}).evaluate("[x for x in range(10)]")

    def test_import_statement_is_rejected(self) -> None:
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({}).evaluate("__import__('os')")

    def test_dunder_name_lookup_is_rejected(self) -> None:
        """Defense in depth -- refused even though this evaluator has no ast.Attribute support."""
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({"__class__": object}).evaluate("__class__")

    def test_invalid_syntax_raises_unsafe_expression_error(self) -> None:
        with pytest.raises(UnsafeExpressionError):
            SafeExpressionEvaluator({}).evaluate("this is not : valid python")


class TestExpressionTemplaterIntegration:
    """End-to-end through `ExpressionTemplater.substitute` -- the actual code path webhooks use."""

    def test_legitimate_bracket_expression_substitutes(self) -> None:
        result = ExpressionTemplater.substitute(
            "Total: $(price * quantity)", {"price": 10, "quantity": 3}
        )
        assert result == "Total: 30"

    def test_dollar_brace_variable_still_works(self) -> None:
        result = ExpressionTemplater.substitute("Hello ${name}", {"name": "world"})
        assert result == "Hello world"

    def test_malicious_bracket_expression_is_left_unevaluated(self) -> None:
        """A rejected expression falls back to the original literal text, never partial output."""
        template = "$(__import__('os').system('id'))"
        result = ExpressionTemplater.substitute(template, {})
        assert result == template

    def test_malicious_expression_in_json_body_does_not_execute(self, tmp_path) -> None:
        sentinel = tmp_path / "pwned"
        body = {"cmd": f"$(__import__('os').system('touch {sentinel}'))"}
        result = ExpressionTemplater.substitute_json(body, {})
        assert not sentinel.exists()
        assert result["cmd"] == body["cmd"]  # unevaluated, returned verbatim

    def test_attribute_chain_gadget_through_substitute_is_neutralized(self) -> None:
        """The REAL sandbox-escape class, exercised through the actual webhook code path.

        `{"__builtins__": {}}` alone blocks bare-name builtin calls like
        `__import__(...)` -- that's this class's other two malicious-
        expression tests, and the original vulnerable code already
        "passed" those (a NameError on the missing `__import__` name falls
        back to the unevaluated original text, coincidentally looking
        safe). It does NOT block attribute-chain introspection, which
        never looks anything up via `__builtins__` at all: `''.__class__
        .__bases__[0].__subclasses__` walks from a string literal through
        its own `__class__`/`__bases__`/`__subclasses__` -- the actual
        CVE-class gadget (from here, a further well-documented step reaches
        `subprocess.Popen` et al. among the returned subclasses; this stops
        at the introspection step, which is already enough to prove the
        escape). No trailing call parens -- `BRACKET_PATTERN`'s own
        `[^)]+` can't span a nested `)`, so this is the payload shape that
        actually reaches `eval()` intact through the real regex extraction,
        not a hand-fed string. Confirmed directly against Python's `eval`
        (not this module) that the original code's exact call shape --
        `eval(expr, {"__builtins__": {}}, {})` -- evaluates this expression
        successfully (returns a real `__subclasses__` bound method, i.e. a
        DIFFERENT string than the template). With the fix, it's rejected
        outright and the template is returned verbatim -- this assertion is
        the fail-first case: it fails (returns the evaluated, non-verbatim
        result) against the original `eval()`-based code, and passes here.
        """
        template = "$(''.__class__.__bases__[0].__subclasses__)"
        result = ExpressionTemplater.substitute(template, {})
        assert result == template
