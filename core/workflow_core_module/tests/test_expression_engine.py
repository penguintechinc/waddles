"""`services/expression_engine.py` -- safe AST-based `{{...}}` expression evaluation."""

from __future__ import annotations

import pytest

from services.expression_engine import (
    BuiltInFunction,
    DictAccessor,
    ExpressionContext,
    ExpressionEngine,
    ExpressionEvaluationError,
    ExpressionEvaluator,
    ExpressionParser,
    ExpressionSecurityError,
    ExpressionSyntaxError,
    StringMethods,
    create_engine,
)


class TestBuiltInFunction:
    def test_random_within_bounds(self) -> None:
        value = BuiltInFunction.random(1, 5)
        assert 1 <= value <= 5

    def test_now_variants(self) -> None:
        assert "T" in BuiltInFunction.now()
        assert isinstance(BuiltInFunction.now_unix(), float)
        assert len(BuiltInFunction.now_date().split("-")) == 3
        assert len(BuiltInFunction.now_time().split(":")) == 3

    def test_case_conversion(self) -> None:
        assert BuiltInFunction.uppercase("abc") == "ABC"
        assert BuiltInFunction.lowercase("ABC") == "abc"

    def test_length(self) -> None:
        assert BuiltInFunction.length("hello") == 5
        assert BuiltInFunction.length([1, 2, 3]) == 3

    def test_math_helpers(self) -> None:
        assert BuiltInFunction.round(1.2345, 2) == 1.23
        assert BuiltInFunction.floor(1.9) == 1
        assert BuiltInFunction.ceil(1.1) == 2
        assert BuiltInFunction.abs(-5) == 5.0
        assert BuiltInFunction.sqrt(9) == 3.0
        assert BuiltInFunction.min(3, 1, 2) == 1.0
        assert BuiltInFunction.max(3, 1, 2) == 3.0

    def test_join_and_split(self) -> None:
        assert BuiltInFunction.join(",", "a", "b", "c") == "a,b,c"
        assert BuiltInFunction.split("a,b,c", ",") == ["a", "b", "c"]

    def test_string_predicates(self) -> None:
        assert BuiltInFunction.contains("hello world", "world") is True
        assert BuiltInFunction.startswith("hello", "he") is True
        assert BuiltInFunction.endswith("hello", "lo") is True
        assert BuiltInFunction.replace("hello", "l", "L") == "heLLo"
        assert BuiltInFunction.strip("  hi  ") == "hi"
        assert BuiltInFunction.trim("  hi  ") == "hi"

    @pytest.mark.parametrize(
        "value,expected",
        [(True, True), ("false", False), ("0", False), ("", False),
         ("null", False), ("none", False), ("yes", True), (1, True), (0, False)],
    )
    def test_bool_conversion(self, value, expected) -> None:
        assert BuiltInFunction.bool(value) is expected


class TestStringMethods:
    @pytest.mark.parametrize(
        "method,args,expected",
        [
            ("upper", (), "HELLO"),
            ("lower", (), "hello"),
            ("capitalize", (), "Hello"),
            ("title", (), "Hello"),
            ("strip", (), "hello"),
            ("lstrip", (), "hello"),
            ("rstrip", (), "hello"),
        ],
    )
    def test_no_arg_methods(self, method, args, expected) -> None:
        assert StringMethods.apply_method("hello", method, *args) == expected

    def test_replace(self) -> None:
        assert StringMethods.apply_method("hello", "replace", "l", "L") == "heLLo"

    def test_replace_missing_args_raises(self) -> None:
        with pytest.raises(ExpressionEvaluationError, match="replace"):
            StringMethods.apply_method("hello", "replace", "l")

    def test_split_with_and_without_sep(self) -> None:
        assert StringMethods.apply_method("a,b", "split", ",") == ["a", "b"]
        assert StringMethods.apply_method("a b", "split") == ["a", "b"]

    def test_join_with_list(self) -> None:
        assert StringMethods.apply_method(",", "join", ["a", "b"]) == "a,b"

    def test_join_without_iterable_raises(self) -> None:
        with pytest.raises(ExpressionEvaluationError, match="join"):
            StringMethods.apply_method(",", "join")

    def test_startswith_endswith_find_count(self) -> None:
        assert StringMethods.apply_method("hello", "startswith", "he") is True
        assert StringMethods.apply_method("hello", "endswith", "lo") is True
        assert StringMethods.apply_method("hello", "find", "l") == 2
        assert StringMethods.apply_method("hello", "count", "l") == 2

    @pytest.mark.parametrize("method", ["startswith", "endswith", "find", "count"])
    def test_missing_required_arg_raises(self, method) -> None:
        with pytest.raises(ExpressionEvaluationError):
            StringMethods.apply_method("hello", method)

    def test_unknown_method_raises(self) -> None:
        with pytest.raises(ExpressionEvaluationError, match="Unknown string method"):
            StringMethods.apply_method("hello", "reverse")


class TestExpressionParser:
    def test_parse_valid_expression(self) -> None:
        tree = ExpressionParser.parse_expression("1 + 2")
        assert tree is not None

    def test_parse_invalid_syntax_raises(self) -> None:
        with pytest.raises(ExpressionSyntaxError):
            ExpressionParser.parse_expression("1 +")

    def test_parse_unsafe_expression_raises(self) -> None:
        with pytest.raises(ExpressionSecurityError):
            ExpressionParser.parse_expression("__import__('os')")

    def test_parse_disallowed_call_raises(self) -> None:
        # Asserts the engine's AST allowlist REJECTS calling `eval` -- this
        # string is never executed by Python's own `eval`/`exec`, only
        # parsed to AST and checked against `ExpressionParser.validate_ast`.
        with pytest.raises(ExpressionSecurityError):
            ExpressionParser.parse_expression("eval('1')")

    def test_extract_expressions(self) -> None:
        result = ExpressionParser.extract_expressions("Hello {{name}}, you are {{age}}")
        assert result == ["name", "age"]

    def test_extract_expressions_none(self) -> None:
        assert ExpressionParser.extract_expressions("no expressions here") == []

    def test_extract_variables(self) -> None:
        result = ExpressionParser.extract_variables("user.level > 5 and active")
        assert "user.level" in result
        assert "active" in result
        assert "and" not in result

    def test_validate_ast_allows_boolop(self) -> None:
        tree = ExpressionParser.parse_expression("1 == 1 and 2 == 2")
        assert tree is not None

    def test_validate_ast_allows_whitelisted_function(self) -> None:
        tree = ExpressionParser.parse_expression("uppercase('hi')")
        assert tree is not None

    def test_validate_ast_allows_attribute_method_call(self) -> None:
        tree = ExpressionParser.parse_expression("name.upper()")
        assert tree is not None


class TestExpressionContext:
    def test_set_and_get_variable(self) -> None:
        ctx = ExpressionContext()
        ctx.set_variable("x", 5)
        assert ctx.get_variable("x") == 5
        assert ctx.has_variable("x") is True
        assert ctx.has_variable("y") is False

    def test_set_variables_bulk(self) -> None:
        ctx = ExpressionContext({"a": 1})
        ctx.set_variables({"b": 2})
        assert ctx.get_all_variables() == {"a": 1, "b": 2}

    def test_get_evaluation_namespace_includes_functions(self) -> None:
        ctx = ExpressionContext({"x": 1})
        namespace = ctx.get_evaluation_namespace()
        assert namespace["x"] == 1
        assert "uppercase" in namespace


class TestDictAccessor:
    def test_attribute_access(self) -> None:
        accessor = DictAccessor({"name": "penguin"})
        assert accessor.name == "penguin"

    def test_attribute_access_missing_raises(self) -> None:
        accessor = DictAccessor({})
        with pytest.raises(AttributeError):
            _ = accessor.missing

    def test_setattr_and_delattr(self) -> None:
        accessor = DictAccessor({})
        accessor.name = "value"
        assert accessor["name"] == "value"
        del accessor.name
        assert "name" not in accessor

    def test_delattr_missing_raises(self) -> None:
        accessor = DictAccessor({})
        with pytest.raises(AttributeError):
            del accessor.missing


class TestExpressionEvaluator:
    def test_evaluate_simple_arithmetic(self) -> None:
        ctx = ExpressionContext()
        result = ExpressionEvaluator.evaluate_expression("1 + 2 * 3", ctx)
        assert result == 7

    def test_evaluate_with_variable(self) -> None:
        ctx = ExpressionContext({"x": 10})
        result = ExpressionEvaluator.evaluate_expression("x > 5", ctx)
        assert result is True

    def test_evaluate_nested_dict_attribute_access(self) -> None:
        ctx = ExpressionContext({"user": {"name": "penguin", "level": 5}})
        result = ExpressionEvaluator.evaluate_expression("user.level", ctx)
        assert result == 5

    def test_evaluate_list_access(self) -> None:
        ctx = ExpressionContext({"items": [{"name": "a"}, {"name": "b"}]})
        result = ExpressionEvaluator.evaluate_expression("items[0].name", ctx)
        assert result == "a"

    def test_evaluate_undefined_variable_raises(self) -> None:
        ctx = ExpressionContext()
        with pytest.raises(ExpressionEvaluationError):
            ExpressionEvaluator.evaluate_expression("undefined_var + 1", ctx)

    def test_evaluate_attribute_error(self) -> None:
        ctx = ExpressionContext({"x": {"a": 1}})
        with pytest.raises(ExpressionEvaluationError):
            ExpressionEvaluator.evaluate_expression("x.missing_attr", ctx)

    def test_evaluate_type_error(self) -> None:
        ctx = ExpressionContext({"x": "text"})
        with pytest.raises(ExpressionEvaluationError):
            ExpressionEvaluator.evaluate_expression("x + 5", ctx)

    def test_evaluate_zero_division(self) -> None:
        ctx = ExpressionContext()
        with pytest.raises(ExpressionEvaluationError, match="Division by zero"):
            ExpressionEvaluator.evaluate_expression("1 / 0", ctx)

    def test_evaluate_value_error(self) -> None:
        ctx = ExpressionContext()
        with pytest.raises(ExpressionEvaluationError):
            ExpressionEvaluator.evaluate_expression("sqrt(-1)", ctx)

    def test_evaluate_builtin_function_call(self) -> None:
        ctx = ExpressionContext()
        result = ExpressionEvaluator.evaluate_expression("uppercase('hi')", ctx)
        assert result == "HI"


class TestExpressionEngine:
    def test_evaluate_success(self) -> None:
        engine = ExpressionEngine()
        engine.set_context({"x": 5})
        result = engine.evaluate("x + 1")
        assert result.success is True
        assert result.value == 6
        assert bool(result) is True

    def test_evaluate_expression_engine_exception_captured(self) -> None:
        engine = ExpressionEngine()
        result = engine.evaluate("1 / 0")
        assert result.success is False
        assert "Division by zero" in result.error

    def test_evaluate_unexpected_exception_captured(self) -> None:
        engine = ExpressionEngine()
        result = engine.evaluate("1 +")
        assert result.success is False

    def test_substitute_no_expressions(self) -> None:
        engine = ExpressionEngine()
        result = engine.substitute("plain text")
        assert result.success is True
        assert result.value == "plain text"

    def test_substitute_with_expression(self) -> None:
        engine = ExpressionEngine()
        engine.set_context({"name": "penguin"})
        result = engine.substitute("Hello {{name}}!")
        assert result.success is True
        assert result.value == "Hello penguin!"

    def test_substitute_failure_propagates(self) -> None:
        engine = ExpressionEngine()
        result = engine.substitute("Value: {{1 / 0}}")
        assert result.success is False
        assert "Failed to evaluate" in result.error

    def test_substitute_dict_simple_strings(self) -> None:
        engine = ExpressionEngine()
        engine.set_context({"name": "penguin"})
        result = engine.substitute_dict({"greeting": "Hi {{name}}"})
        assert result.success is True
        assert result.value == {"greeting": "Hi penguin"}

    def test_substitute_dict_nested_dict(self) -> None:
        engine = ExpressionEngine()
        engine.set_context({"x": 1})
        result = engine.substitute_dict({"outer": {"inner": "{{x}}"}})
        assert result.value == {"outer": {"inner": "1"}}

    def test_substitute_dict_list_values(self) -> None:
        engine = ExpressionEngine()
        engine.set_context({"x": 1})
        result = engine.substitute_dict({"items": ["{{x}}", "static", 5]})
        assert result.value == {"items": ["1", "static", 5]}

    def test_substitute_dict_tuple_values(self) -> None:
        engine = ExpressionEngine()
        engine.set_context({"x": 1})
        result = engine.substitute_dict({"items": ("{{x}}", "static")})
        assert result.value == {"items": ("1", "static")}

    def test_substitute_dict_passthrough_other_types(self) -> None:
        engine = ExpressionEngine()
        result = engine.substitute_dict({"count": 5, "flag": True})
        assert result.value == {"count": 5, "flag": True}

    def test_substitute_dict_propagates_nested_failure(self) -> None:
        engine = ExpressionEngine()
        result = engine.substitute_dict({"outer": {"inner": "{{1 / 0}}"}})
        assert result.success is False

    def test_substitute_dict_propagates_list_item_failure(self) -> None:
        engine = ExpressionEngine()
        result = engine.substitute_dict({"items": ["{{1 / 0}}"]})
        assert result.success is False

    def test_extract_variables(self) -> None:
        engine = ExpressionEngine()
        result = engine.extract_variables("user.name and active")
        assert "user.name" in result

    def test_validate_expression_success(self) -> None:
        engine = ExpressionEngine()
        result = engine.validate_expression("1 + 1")
        assert result.success is True
        assert result.value is True

    def test_validate_expression_failure(self) -> None:
        engine = ExpressionEngine()
        result = engine.validate_expression("1 +")
        assert result.success is False

    def test_get_available_functions(self) -> None:
        engine = ExpressionEngine()
        functions = engine.get_available_functions()
        assert "uppercase" in functions

    def test_get_available_variables(self) -> None:
        engine = ExpressionEngine()
        engine.set_context({"a": 1, "b": 2})
        assert set(engine.get_available_variables()) == {"a", "b"}


class TestCreateEngine:
    def test_create_engine_with_context(self) -> None:
        engine = create_engine({"x": 1})
        assert engine.get_available_variables() == ["x"]

    def test_create_engine_without_context(self) -> None:
        engine = create_engine()
        assert engine.get_available_variables() == []
