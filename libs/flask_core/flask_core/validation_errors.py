"""Redaction-safe description of Pydantic validation failures.

A Pydantic v2 ``ValidationError.errors()`` entry carries ``input`` -- the exact
value the client sent -- and a ``msg`` / ``ctx`` that can echo it too (a custom
``ValueError(f"bad {v}")``, a UUID parse error naming the offending character,
an ``extra_forbidden`` error whose ``loc`` is the attacker-chosen key name).
Logging ``{errors}`` therefore writes user-supplied values (PII, tokens,
handles) into the log stream of every service using ``flask_core.validation``.

This module is the single place flask_core turns a validation failure into log
text. It is allowlist-based and fails closed: only the error *count*, the
*location* of each failing field (a segment is emitted only if it is a field
name or alias declared on the model; list indexes and dict keys are replaced by
placeholders) and the Pydantic error *type* (regex-validated) are ever emitted.
``input``, ``msg``, ``ctx`` and ``url`` are never read into the output.
"""

from __future__ import annotations

import re
import typing
from functools import lru_cache
from typing import Final

from pydantic import BaseModel, ValidationError

_ERROR_TYPE_RE: Final = re.compile(r"[a-z0-9_.\-]{1,64}")
_UNKNOWN_TYPE: Final = "unknown"
_REDACTED_KEY: Final = "<key>"
_ROOT_LOC: Final = "<root>"
_INDEX_MARK: Final = "[]"
_MAX_DETAILS: Final = 20
_MAX_ANNOTATION_DEPTH: Final = 8


def _nested_models(annotation: object, depth: int = 0) -> list[type[BaseModel]]:
    """Collect every ``BaseModel`` subclass reachable through a type annotation's arguments."""
    if depth > _MAX_ANNOTATION_DEPTH or annotation is None:
        return []
    if isinstance(annotation, type) and typing.get_origin(annotation) is None:
        return [annotation] if issubclass(annotation, BaseModel) else []
    found: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        found.extend(_nested_models(arg, depth + 1))
    return found


@lru_cache(maxsize=256)
def _declared_names(model: type[BaseModel]) -> frozenset[str]:
    """Return every developer-declared field name/alias on `model` and its nested models.

    These are the only ``loc`` segments safe to log: anything else in a ``loc``
    (extra keys, dict keys, union branch labels) can be client-controlled.
    """
    names: set[str] = set()
    seen: set[type[BaseModel]] = set()
    stack: list[type[BaseModel]] = [model]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for name, info in current.model_fields.items():
            names.add(name)
            for alias in (info.alias, info.validation_alias):
                if isinstance(alias, str):
                    names.add(alias)
            stack.extend(_nested_models(info.annotation))
    return frozenset(names)


def _render_loc(loc: tuple[int | str, ...], declared: frozenset[str]) -> str:
    """Render an error ``loc`` with only declared field names; indexes/keys are masked."""
    parts: list[str] = []
    for segment in loc:
        if isinstance(segment, int):
            # A list index or an int dict key -- the number itself is never emitted.
            if parts:
                parts[-1] += _INDEX_MARK
            else:
                parts.append(_INDEX_MARK)
        elif segment in declared:
            parts.append(segment)
        else:
            parts.append(_REDACTED_KEY)
    return ".".join(parts) or _ROOT_LOC


def _render_type(error_type: object) -> str:
    """Return the Pydantic error type if it is a plain identifier-like token, else a fixed label."""
    if isinstance(error_type, str) and _ERROR_TYPE_RE.fullmatch(error_type):
        return error_type
    return _UNKNOWN_TYPE


def describe_validation_errors(
    exc: ValidationError, model: type[BaseModel] | None = None
) -> str:
    """Return a loggable, value-free one-line description of a validation failure.

    Format: ``errors=<count> fields=<loc>:<type>,<loc>:<type>`` (capped at 20
    entries, then ``+N more``). Pass `model` so ``loc`` segments can be checked
    against its declared fields; without it every named segment is masked.
    """
    declared = _declared_names(model) if model is not None else frozenset()
    errors = exc.errors(include_url=False, include_context=False)
    details = [
        f"{_render_loc(tuple(err['loc']), declared)}:{_render_type(err.get('type'))}"
        for err in errors[:_MAX_DETAILS]
    ]
    overflow = len(errors) - len(details)
    suffix = f" +{overflow} more" if overflow > 0 else ""
    return f"errors={len(errors)} fields={','.join(details)}{suffix}"
