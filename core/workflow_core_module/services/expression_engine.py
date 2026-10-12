"""
Expression Engine for Waddles Workflow System

This module provides safe evaluation of workflow expressions with:
- Variable substitution from workflow context
- Built-in functions for common operations
- Comparison operations
- String methods
- Safe AST-based evaluation (no arbitrary code execution)
- Comprehensive error handling

Security: expressions are NEVER passed to ``eval``/``exec``/``compile``.
``ExpressionParser.validate_ast`` rejects disallowed constructs up front and
``ExpressionEvaluator`` interprets the validated AST node-by-node with an
explicit allowlist: literals, arithmetic/boolean/comparison operators,
variable names, dict-key attribute access, list/dict/tuple subscripts, calls
to the public ``BuiltInFunction`` set, and a fixed set of string methods.
Any name or attribute beginning with ``_`` (dunders such as ``__class__``)
is refused, attribute access never uses ``getattr`` (so
``().__class__.__bases__[0].__subclasses__()`` style sandbox escapes have no
surface), and ``**``/``*`` results are size-capped against resource
exhaustion. The previous ``eval(..., {"__builtins__": {}})`` sandbox was
bypassable and has been removed.
"""

import ast
import operator
import re
import random as random_module
import datetime
import math
from typing import Any, Dict, Optional, List, Union
from dataclasses import dataclass
from enum import Enum


class ExpressionEngineException(Exception):
    """Base exception for expression engine errors"""
    pass


class ExpressionSyntaxError(ExpressionEngineException):
    """Raised when expression syntax is invalid"""
    pass


class ExpressionEvaluationError(ExpressionEngineException):
    """Raised when expression evaluation fails"""
    pass


class ExpressionSecurityError(ExpressionEngineException):
    """Raised when expression contains potentially unsafe operations"""
    pass


@dataclass
class ExpressionResult:
    """Result of expression evaluation"""
    success: bool
    value: Any = None
    error: Optional[str] = None
    expression: Optional[str] = None

    def __bool__(self) -> bool:
        return self.success


class BuiltInFunction:
    """Handler for built-in functions"""

    @staticmethod
    def random(min_val: int = 0, max_val: int = 100) -> int:
        """Generate random integer between min_val and max_val (inclusive)"""
        return random_module.randint(int(min_val), int(max_val))

    @staticmethod
    def now() -> str:
        """Get current timestamp in ISO format"""
        return datetime.datetime.now().isoformat()

    @staticmethod
    def now_unix() -> float:
        """Get current Unix timestamp"""
        return datetime.datetime.now().timestamp()

    @staticmethod
    def now_date() -> str:
        """Get current date in YYYY-MM-DD format"""
        return datetime.datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def now_time() -> str:
        """Get current time in HH:MM:SS format"""
        return datetime.datetime.now().strftime("%H:%M:%S")

    @staticmethod
    def uppercase(text: str) -> str:
        """Convert text to uppercase"""
        return str(text).upper()

    @staticmethod
    def lowercase(text: str) -> str:
        """Convert text to lowercase"""
        return str(text).lower()

    @staticmethod
    def length(obj: Any) -> int:
        """Get length of string, list, or dict"""
        return len(obj)

    @staticmethod
    def round(value: float, decimals: int = 0) -> float:
        """Round number to specified decimal places"""
        return round(float(value), int(decimals))

    @staticmethod
    def floor(value: float) -> int:
        """Floor function"""
        return math.floor(float(value))

    @staticmethod
    def ceil(value: float) -> int:
        """Ceiling function"""
        return math.ceil(float(value))

    @staticmethod
    def abs(value: float) -> float:
        """Absolute value"""
        return abs(float(value))

    @staticmethod
    def sqrt(value: float) -> float:
        """Square root"""
        return math.sqrt(float(value))

    @staticmethod
    def min(*values) -> float:
        """Return minimum value"""
        return min(float(v) for v in values)

    @staticmethod
    def max(*values) -> float:
        """Return maximum value"""
        return max(float(v) for v in values)

    @staticmethod
    def join(delimiter: str, *values) -> str:
        """Join values with delimiter"""
        return str(delimiter).join(str(v) for v in values)

    @staticmethod
    def split(text: str, delimiter: str = ",") -> List[str]:
        """Split string by delimiter"""
        return str(text).split(str(delimiter))

    @staticmethod
    def contains(text: str, substring: str) -> bool:
        """Check if text contains substring"""
        return str(substring) in str(text)

    @staticmethod
    def startswith(text: str, prefix: str) -> bool:
        """Check if text starts with prefix"""
        return str(text).startswith(str(prefix))

    @staticmethod
    def endswith(text: str, suffix: str) -> bool:
        """Check if text ends with suffix"""
        return str(text).endswith(str(suffix))

    @staticmethod
    def replace(text: str, old: str, new: str) -> str:
        """Replace all occurrences of old with new"""
        return str(text).replace(str(old), str(new))

    @staticmethod
    def strip(text: str) -> str:
        """Remove leading/trailing whitespace"""
        return str(text).strip()

    @staticmethod
    def trim(text: str) -> str:
        """Alias for strip"""
        return str(text).strip()

    @staticmethod
    def bool(value: Any) -> bool:
        """Convert value to boolean"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() not in ("false", "0", "", "null", "none")
        return bool(value)


class StringMethods:
    """String method extensions for expressions"""

    @staticmethod
    def apply_method(text: str, method: str, *args) -> Any:
        """Apply string method to text"""
        text = str(text)

        if method == "upper":
            return text.upper()
        elif method == "lower":
            return text.lower()
        elif method == "capitalize":
            return text.capitalize()
        elif method == "title":
            return text.title()
        elif method == "strip":
            return text.strip()
        elif method == "lstrip":
            return text.lstrip()
        elif method == "rstrip":
            return text.rstrip()
        elif method == "replace":
            if len(args) >= 2:
                return text.replace(str(args[0]), str(args[1]))
            raise ExpressionEvaluationError(f"replace() requires 2 arguments, got {len(args)}")
        elif method == "split":
            sep = str(args[0]) if args else None
            return text.split(sep)
        elif method == "join":
            if args and isinstance(args[0], (list, tuple)):
                return text.join(str(v) for v in args[0])
            raise ExpressionEvaluationError(f"join() requires an iterable argument")
        elif method == "startswith":
            if args:
                return text.startswith(str(args[0]))
            raise ExpressionEvaluationError("startswith() requires at least 1 argument")
        elif method == "endswith":
            if args:
                return text.endswith(str(args[0]))
            raise ExpressionEvaluationError("endswith() requires at least 1 argument")
        elif method == "find":
            if args:
                return text.find(str(args[0]))
            raise ExpressionEvaluationError("find() requires at least 1 argument")
        elif method == "count":
            if args:
                return text.count(str(args[0]))
            raise ExpressionEvaluationError("count() requires at least 1 argument")
        else:
            raise ExpressionEvaluationError(f"Unknown string method: {method}")


PUBLIC_FUNCTION_NAMES = frozenset(
    name for name in dir(BuiltInFunction) if not name.startswith("_")
)

ALLOWED_STRING_METHODS = frozenset({
    "upper", "lower", "capitalize", "title", "strip", "lstrip", "rstrip",
    "replace", "split", "join", "startswith", "endswith", "find", "count",
})

ALLOWED_CONSTANT_TYPES = (str, int, float, bool, type(None))

MAX_EXPRESSION_LENGTH = 4096
MAX_POW_EXPONENT = 1000
MAX_REPEAT_LENGTH = 100_000


class ExpressionParser:
    """Parses workflow expressions"""

    # Pattern to match template expressions: {{...}}
    EXPRESSION_PATTERN = re.compile(r'\{\{([^}]+)\}\}')

    # Pattern to match variable access: user.name, message.content, etc.
    VARIABLE_PATTERN = re.compile(r'[a-zA-Z_][a-zA-Z0-9_.]*')

    # Allowed node types for safe evaluation
    ALLOWED_NODE_TYPES = {
        ast.Expression,
        ast.Compare,
        ast.BinOp,
        ast.UnaryOp,
        ast.Call,
        ast.Name,
        ast.Constant,
        ast.List,
        ast.Tuple,
        ast.Dict,
        ast.Attribute,
        ast.Subscript,
        ast.BoolOp,
    }

    # Allowed operators
    ALLOWED_OPERATORS = {
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.And,
        ast.Or,
        ast.Not,
        ast.UAdd,
        ast.USub,
    }

    @staticmethod
    def validate_ast(node: ast.AST) -> bool:
        """Recursively validate AST to ensure it's safe to evaluate"""
        # Allow context-related and operator nodes that don't have dangerous side effects
        allowed_context_nodes = {
            ast.Load, ast.Store, ast.Del,
            ast.keyword,
        }

        # All operator types
        all_operators = ExpressionParser.ALLOWED_OPERATORS

        for child in ast.walk(node):
            node_type = type(child)

            # Check if node type is allowed
            if node_type not in ExpressionParser.ALLOWED_NODE_TYPES:
                # Allow context nodes
                if node_type not in allowed_context_nodes:
                    # Allow operator nodes
                    if not any(isinstance(child, op_type) for op_type in all_operators):
                        # Also allow And/Or/Not operators
                        if node_type not in {ast.And, ast.Or, ast.Not}:
                            return False

            # Never allow private/dunder names or attributes (``__class__``,
            # ``__subclasses__``, ``_x`` ...) -- the sandbox-escape gadget surface.
            if isinstance(child, ast.Name) and child.id.startswith("_"):
                return False
            if isinstance(child, ast.Attribute) and child.attr.startswith("_"):
                return False
            if isinstance(child, ast.Constant) and not isinstance(
                child.value, ALLOWED_CONSTANT_TYPES
            ):
                return False

            # Prevent dangerous function calls
            if isinstance(child, ast.Call):
                if child.keywords or any(isinstance(a, ast.Starred) for a in child.args):
                    return False
                if isinstance(child.func, ast.Name):
                    func_name = child.func.id
                    # Only allow whitelisted built-in functions
                    if func_name not in PUBLIC_FUNCTION_NAMES:
                        return False
                elif isinstance(child.func, ast.Attribute):
                    # Only a fixed set of string methods may be called
                    if child.func.attr not in ALLOWED_STRING_METHODS:
                        return False
                else:
                    return False

            # Prevent dangerous operators
            if isinstance(child, (ast.BinOp, ast.UnaryOp)):
                if type(child.op) not in ExpressionParser.ALLOWED_OPERATORS:
                    return False

        return True

    @staticmethod
    def parse_expression(expression: str) -> ast.Expression:
        """Parse expression string into AST"""
        expression = expression.strip()

        if len(expression) > MAX_EXPRESSION_LENGTH:
            raise ExpressionSecurityError(
                f"Expression exceeds maximum length of {MAX_EXPRESSION_LENGTH}"
            )

        try:
            tree = ast.parse(expression, mode='eval')
        except SyntaxError as e:
            raise ExpressionSyntaxError(f"Invalid expression syntax: {e}")

        if not ExpressionParser.validate_ast(tree):
            raise ExpressionSecurityError("Expression contains unsafe operations")

        return tree

    @staticmethod
    def extract_expressions(text: str) -> List[str]:
        """Extract all {{...}} expressions from text"""
        matches = ExpressionParser.EXPRESSION_PATTERN.findall(text)
        return matches

    @staticmethod
    def extract_variables(expression: str) -> List[str]:
        """Extract variable names from expression"""
        # Remove function calls and operators
        cleaned = re.sub(r'\([^)]*\)', '', expression)
        cleaned = re.sub(r'[<>=!+\-*/%()]', ' ', cleaned)

        matches = ExpressionParser.VARIABLE_PATTERN.findall(cleaned)
        return [m for m in matches if m not in {'and', 'or', 'not', 'True', 'False', 'None'}]


class ExpressionContext:
    """Manages context variables for expression evaluation"""

    def __init__(self, variables: Optional[Dict[str, Any]] = None):
        """Initialize context with variables"""
        self.variables = variables or {}
        self.functions = self._setup_functions()

    def _setup_functions(self) -> Dict[str, Any]:
        """Setup available functions"""
        functions = {}
        for name in dir(BuiltInFunction):
            if not name.startswith('_'):
                functions[name] = getattr(BuiltInFunction, name)
        return functions

    def set_variable(self, name: str, value: Any) -> None:
        """Set a variable in context"""
        self.variables[name] = value

    def get_variable(self, name: str) -> Any:
        """Get a variable from context"""
        return self.variables.get(name)

    def set_variables(self, variables: Dict[str, Any]) -> None:
        """Set multiple variables"""
        self.variables.update(variables)

    def get_all_variables(self) -> Dict[str, Any]:
        """Get all variables"""
        return self.variables.copy()

    def has_variable(self, name: str) -> bool:
        """Check if variable exists"""
        return name in self.variables

    def get_evaluation_namespace(self) -> Dict[str, Any]:
        """Get namespace for evaluation (variables + functions)"""
        namespace = self.functions.copy()
        namespace.update(self.variables)
        return namespace


class DictAccessor(dict):
    """Dictionary that allows attribute-style access"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError:
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


class ExpressionEvaluator:
    """Evaluates parsed expressions safely"""

    @staticmethod
    def _convert_to_accessor(obj: Any) -> Any:
        """Recursively convert dicts to DictAccessor for attribute access"""
        if isinstance(obj, dict) and not isinstance(obj, DictAccessor):
            accessor = DictAccessor(obj)
            for key, value in obj.items():
                accessor[key] = ExpressionEvaluator._convert_to_accessor(value)
            return accessor
        elif isinstance(obj, (list, tuple)):
            converted = [ExpressionEvaluator._convert_to_accessor(item) for item in obj]
            return type(obj)(converted) if isinstance(obj, tuple) else converted
        return obj

    _BINOPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    _UNARYOPS = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
        ast.Not: operator.not_,
    }
    _COMPARES = {
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.Lt: operator.lt,
        ast.LtE: operator.le,
        ast.Gt: operator.gt,
        ast.GtE: operator.ge,
    }

    @staticmethod
    def evaluate_ast(tree: ast.Expression, context: ExpressionContext) -> Any:
        """Evaluate a validated AST against the context via an allowlist walker.

        Never calls ``eval``/``exec``/``compile``. Re-validates the tree so a
        caller handing in an AST that skipped ``ExpressionParser`` is still
        refused (fail loud with ``ExpressionSecurityError``).
        """
        if not isinstance(tree, ast.Expression) or not ExpressionParser.validate_ast(tree):
            raise ExpressionSecurityError("Expression contains unsafe operations")

        namespace = context.get_evaluation_namespace()

        # Convert dictionaries to DictAccessor for attribute-style access
        converted_namespace = {}
        for key, value in namespace.items():
            converted_namespace[key] = ExpressionEvaluator._convert_to_accessor(value)

        try:
            return ExpressionEvaluator._eval_node(tree.body, converted_namespace)
        except ExpressionEngineException:
            raise
        except KeyError as e:
            raise ExpressionEvaluationError(f"Undefined variable: {e}") from e
        except AttributeError as e:
            raise ExpressionEvaluationError(f"Attribute error: {e}") from e
        except TypeError as e:
            raise ExpressionEvaluationError(f"Type error: {e}") from e
        except ZeroDivisionError:
            raise ExpressionEvaluationError("Division by zero") from None
        except ValueError as e:
            raise ExpressionEvaluationError(f"Value error: {e}") from e
        except Exception as e:
            raise ExpressionEvaluationError(f"Evaluation error: {e}") from e

    @staticmethod
    def _check_sizes(op: ast.operator, left: Any, right: Any) -> None:
        """Refuse ``**``/``*`` operands that would exhaust CPU or memory."""
        if isinstance(op, ast.Pow):
            if (
                isinstance(right, (int, float))
                and not isinstance(right, bool)
                and abs(right) > MAX_POW_EXPONENT
            ):
                raise ExpressionEvaluationError(
                    f"Exponent exceeds maximum of {MAX_POW_EXPONENT}"
                )
        elif isinstance(op, ast.Mult):
            for seq, count in ((left, right), (right, left)):
                if isinstance(seq, (str, list, tuple)) and isinstance(count, int):
                    if len(seq) * max(count, 0) > MAX_REPEAT_LENGTH:
                        raise ExpressionEvaluationError(
                            f"Repeated sequence exceeds maximum length of {MAX_REPEAT_LENGTH}"
                        )

    @staticmethod
    def _eval_node(node: ast.AST, ns: Dict[str, Any]) -> Any:
        """Interpret one allowlisted AST node; any other node type is refused."""
        ev = ExpressionEvaluator._eval_node

        if isinstance(node, ast.Constant):
            if not isinstance(node.value, ALLOWED_CONSTANT_TYPES):
                raise ExpressionSecurityError("Disallowed constant type")
            return node.value

        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise ExpressionSecurityError(f"Disallowed name: {node.id}")
            if node.id not in ns:
                raise ExpressionEvaluationError(f"Undefined variable: '{node.id}'")
            return ns[node.id]

        if isinstance(node, ast.BinOp):
            op = ExpressionEvaluator._BINOPS.get(type(node.op))
            if op is None:
                raise ExpressionSecurityError(
                    f"Disallowed operator: {type(node.op).__name__}"
                )
            left, right = ev(node.left, ns), ev(node.right, ns)
            ExpressionEvaluator._check_sizes(node.op, left, right)
            return op(left, right)

        if isinstance(node, ast.UnaryOp):
            op = ExpressionEvaluator._UNARYOPS.get(type(node.op))
            if op is None:
                raise ExpressionSecurityError(
                    f"Disallowed operator: {type(node.op).__name__}"
                )
            return op(ev(node.operand, ns))

        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result: Any = True
                for value_node in node.values:
                    result = ev(value_node, ns)
                    if not result:
                        return result
                return result
            if isinstance(node.op, ast.Or):
                result = False
                for value_node in node.values:
                    result = ev(value_node, ns)
                    if result:
                        return result
                return result
            raise ExpressionSecurityError("Disallowed boolean operator")

        if isinstance(node, ast.Compare):
            left = ev(node.left, ns)
            for cmp_op, comparator in zip(node.ops, node.comparators):
                fn = ExpressionEvaluator._COMPARES.get(type(cmp_op))
                if fn is None:
                    raise ExpressionSecurityError(
                        f"Disallowed comparison: {type(cmp_op).__name__}"
                    )
                right = ev(comparator, ns)
                if not fn(left, right):
                    return False
                left = right
            return True

        if isinstance(node, ast.List):
            return [ev(elt, ns) for elt in node.elts]

        if isinstance(node, ast.Tuple):
            return tuple(ev(elt, ns) for elt in node.elts)

        if isinstance(node, ast.Dict):
            if any(k is None for k in node.keys):
                raise ExpressionSecurityError("Dict unpacking is not allowed")
            return {ev(k, ns): ev(v, ns) for k, v in zip(node.keys, node.values)}

        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ExpressionSecurityError(f"Disallowed attribute: {node.attr}")
            target = ev(node.value, ns)
            # Dict-key lookup ONLY -- never getattr(), so no object-graph walking.
            if isinstance(target, dict):
                if node.attr in target:
                    return target[node.attr]
                raise AttributeError(f"object has no attribute '{node.attr}'")
            raise ExpressionSecurityError(
                f"Attribute access not allowed on {type(target).__name__}"
            )

        if isinstance(node, ast.Subscript):
            target = ev(node.value, ns)
            if not isinstance(target, (dict, list, tuple, str)):
                raise ExpressionSecurityError(
                    f"Subscript not allowed on {type(target).__name__}"
                )
            if isinstance(node.slice, ast.Slice):
                raise ExpressionSecurityError("Slices are not allowed")
            return target[ev(node.slice, ns)]

        if isinstance(node, ast.Call):
            if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
                raise ExpressionSecurityError("Keyword/starred call arguments are not allowed")
            args = [ev(a, ns) for a in node.args]
            func = node.func
            if isinstance(func, ast.Name):
                if func.id not in PUBLIC_FUNCTION_NAMES:
                    raise ExpressionSecurityError(f"Disallowed function: {func.id}")
                return getattr(BuiltInFunction, func.id)(*args)
            if isinstance(func, ast.Attribute):
                if func.attr not in ALLOWED_STRING_METHODS:
                    raise ExpressionSecurityError(f"Disallowed method: {func.attr}")
                target = ev(func.value, ns)
                if not isinstance(target, str):
                    raise ExpressionSecurityError(
                        f"Method calls only allowed on strings, not {type(target).__name__}"
                    )
                return StringMethods.apply_method(target, func.attr, *args)
            raise ExpressionSecurityError("Disallowed call target")

        raise ExpressionSecurityError(f"Disallowed expression node: {type(node).__name__}")

    @staticmethod
    def evaluate_expression(expression: str, context: ExpressionContext) -> Any:
        """Parse and evaluate expression"""
        tree = ExpressionParser.parse_expression(expression)
        return ExpressionEvaluator.evaluate_ast(tree, context)


class ExpressionEngine:
    """Main expression engine for workflow system"""

    def __init__(self, context: Optional[ExpressionContext] = None):
        """Initialize engine with optional context"""
        self.context = context or ExpressionContext()
        self.parser = ExpressionParser()
        self.evaluator = ExpressionEvaluator()

    def set_context(self, variables: Dict[str, Any]) -> None:
        """Set workflow context variables"""
        self.context.set_variables(variables)

    def evaluate(self, expression: str) -> ExpressionResult:
        """
        Evaluate a single expression

        Args:
            expression: The expression to evaluate (e.g., "user.level > 5")

        Returns:
            ExpressionResult with success status and value
        """
        try:
            result_value = self.evaluator.evaluate_expression(expression, self.context)
            return ExpressionResult(
                success=True,
                value=result_value,
                expression=expression
            )
        except ExpressionEngineException as e:
            return ExpressionResult(
                success=False,
                error=str(e),
                expression=expression
            )
        except Exception as e:
            return ExpressionResult(
                success=False,
                error=f"Unexpected error: {str(e)}",
                expression=expression
            )

    def substitute(self, text: str) -> ExpressionResult:
        """
        Substitute all {{...}} expressions in text

        Args:
            text: Text with embedded expressions

        Returns:
            ExpressionResult with substituted text
        """
        try:
            expressions = self.parser.extract_expressions(text)

            if not expressions:
                return ExpressionResult(success=True, value=text)

            result_text = text
            for expr in expressions:
                expr_result = self.evaluate(expr)
                if not expr_result.success:
                    return ExpressionResult(
                        success=False,
                        error=f"Failed to evaluate {{{{{expr}}}}}: {expr_result.error}",
                        expression=text
                    )

                replacement = str(expr_result.value)
                result_text = result_text.replace(f"{{{{{expr}}}}}", replacement)

            return ExpressionResult(success=True, value=result_text, expression=text)

        except Exception as e:
            return ExpressionResult(
                success=False,
                error=f"Substitution error: {str(e)}",
                expression=text
            )

    def substitute_dict(self, data: Dict[str, Any]) -> ExpressionResult:
        """
        Recursively substitute expressions in dictionary values

        Args:
            data: Dictionary with potential expressions in string values

        Returns:
            ExpressionResult with substituted dictionary
        """
        try:
            result = {}

            for key, value in data.items():
                if isinstance(value, str):
                    sub_result = self.substitute(value)
                    if not sub_result.success:
                        return sub_result
                    result[key] = sub_result.value
                elif isinstance(value, dict):
                    sub_result = self.substitute_dict(value)
                    if not sub_result.success:
                        return sub_result
                    result[key] = sub_result.value
                elif isinstance(value, (list, tuple)):
                    sub_list = []
                    for item in value:
                        if isinstance(item, str):
                            sub_result = self.substitute(item)
                            if not sub_result.success:
                                return sub_result
                            sub_list.append(sub_result.value)
                        else:
                            sub_list.append(item)
                    result[key] = sub_list if isinstance(value, list) else tuple(sub_list)
                else:
                    result[key] = value

            return ExpressionResult(success=True, value=result)

        except Exception as e:
            return ExpressionResult(
                success=False,
                error=f"Dictionary substitution error: {str(e)}"
            )

    def extract_variables(self, expression: str) -> List[str]:
        """Extract variable names used in expression"""
        try:
            return self.parser.extract_variables(expression)
        except Exception as e:
            return []

    def validate_expression(self, expression: str) -> ExpressionResult:
        """
        Validate expression without evaluating it

        Args:
            expression: Expression to validate

        Returns:
            ExpressionResult with validation result
        """
        try:
            self.parser.parse_expression(expression)
            return ExpressionResult(
                success=True,
                value=True,
                expression=expression
            )
        except ExpressionEngineException as e:
            return ExpressionResult(
                success=False,
                error=str(e),
                expression=expression
            )

    def get_available_functions(self) -> List[str]:
        """Get list of available built-in functions"""
        return [name for name in dir(BuiltInFunction) if not name.startswith('_')]

    def get_available_variables(self) -> List[str]:
        """Get list of available variables in context"""
        return list(self.context.get_all_variables().keys())


# Convenience function for quick usage
def create_engine(context: Optional[Dict[str, Any]] = None) -> ExpressionEngine:
    """Create a new expression engine with optional initial context"""
    expr_context = ExpressionContext(context) if context else ExpressionContext()
    return ExpressionEngine(expr_context)
