"""Pydantic-validated `author` for bundle manifests and marketplace listings.

A bundle's `author` is either a GitHub profile (user or org) or a plain display
name. Accepted spellings are normalized to a structured value so the marketplace
can render a verified profile link instead of untrusted free text:

* GitHub: `github:<handle>`, `github/<handle>`, `@<handle>`, `https://github.com/<handle>[/]`
  (all normalize to kind=github; canonical storage is `github:<handle>`, and manifest
  text such as `github/<handle>` is never rewritten -- only the stored/display form is
  normalized, handle case preserved as given)
* Name: letters/digits joined by single ` `, `-`, `_` or `.` (1-64 chars)

Everything else (HTML/markdown, other URLs, emoji, control characters) is rejected
with a stable machine-readable code carried on `AuthorError`.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

#: GitHub login rule (users AND orgs): 1-39 chars, alnum or single inner hyphens.
_GITHUB_HANDLE_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
#: Letters/digits runs joined by exactly one separator; no leading/trailing separator.
_NAME_RE = re.compile(r"[^\W_]+(?:[ _.\-][^\W_]+)*")
#: Any control character (C0, DEL, C1, line/paragraph separators) -- checked first.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
#: Characters that signal markup rather than a name; gives a clearer error code.
_MARKUP_RE = re.compile(r"[<>&`*\[\]()\\|{}~#]")
_GITHUB_URL_PREFIX = "https://github.com/"
_GITHUB_PREFIX = "github:"
_GITHUB_SLASH_PREFIX = "github/"

MAX_NAME_LEN = 64
#: Raw input ceiling: longest accepted spelling (a 64-char name, or the GitHub URL form).
MAX_AUTHOR_LEN = max(MAX_NAME_LEN, len(_GITHUB_URL_PREFIX) + 39 + 1)

CODE_REQUIRED = "author_required"
CODE_NOT_STRING = "author_not_string"
CODE_CONTROL_CHARS = "author_control_chars"
CODE_MARKUP = "author_markup_rejected"
CODE_INVALID_URL = "author_invalid_url"
CODE_INVALID_HANDLE = "author_invalid_github_handle"
CODE_INVALID_NAME = "author_invalid_name"
CODE_TOO_LONG = "author_too_long"


class AuthorError(ValueError):
    """Author failed validation; `code` is the stable reason code, `str()` the message."""

    def __init__(self, code: str, message: str) -> None:
        """Carry the machine-readable `code` alongside the human message."""
        super().__init__(message)
        self.code = code


class AuthorKind(str, Enum):
    """Whether the author resolves to a GitHub profile or is a plain name."""

    GITHUB = "github"
    NAME = "name"


def _parse(raw: Any) -> tuple[AuthorKind, str | None, str]:
    """Classify and validate `raw`, returning (kind, handle, display) or raising AuthorError."""
    if raw is None or (isinstance(raw, str) and raw == ""):
        raise AuthorError(CODE_REQUIRED, "author is required")
    if not isinstance(raw, str):
        raise AuthorError(CODE_NOT_STRING, "author must be a string")
    if len(raw) > MAX_AUTHOR_LEN:
        raise AuthorError(CODE_TOO_LONG, f"author exceeds {MAX_AUTHOR_LEN} characters")
    if _CONTROL_RE.search(raw):
        raise AuthorError(
            CODE_CONTROL_CHARS, "author must not contain control characters"
        )

    handle: str | None = None
    if raw.startswith(_GITHUB_URL_PREFIX):
        handle = raw[len(_GITHUB_URL_PREFIX) :].removesuffix("/")
        if "/" in handle or "?" in handle or "#" in handle:
            raise AuthorError(
                CODE_INVALID_URL,
                "GitHub author URL must be https://github.com/<handle> only",
            )
    elif "://" in raw:
        raise AuthorError(
            CODE_INVALID_URL, "author URL must be a https://github.com/<handle> URL"
        )
    elif raw.startswith(_GITHUB_PREFIX):
        handle = raw[len(_GITHUB_PREFIX) :]
    elif raw.startswith(_GITHUB_SLASH_PREFIX):
        handle = raw[len(_GITHUB_SLASH_PREFIX) :]
    elif raw.startswith("@"):
        handle = raw[1:]

    if handle is not None:
        if _GITHUB_HANDLE_RE.fullmatch(handle) is None:
            raise AuthorError(CODE_INVALID_HANDLE, "invalid GitHub handle")
        return AuthorKind.GITHUB, handle, handle

    if _MARKUP_RE.search(raw):
        raise AuthorError(CODE_MARKUP, "author must not contain HTML or markdown")
    if len(raw) > MAX_NAME_LEN or _NAME_RE.fullmatch(raw) is None:
        raise AuthorError(
            CODE_INVALID_NAME,
            "author name must be 1-64 letters/digits, with a single space, '-', '_' or '.' "
            "between words",
        )
    return AuthorKind.NAME, None, raw


class ManifestAuthor(BaseModel):
    """Normalized, validated author. Validate a raw string with `ManifestAuthor.model_validate`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AuthorKind
    handle: str | None = None
    display: str

    @model_validator(mode="before")
    @classmethod
    def _from_raw(cls, value: Any) -> Any:
        """Accept the raw manifest string (any non-dict) and normalize it."""
        if isinstance(value, dict):
            return value
        try:
            kind, handle, display = _parse(value)
        except AuthorError as exc:
            raise ValueError(f"{exc.code}: {exc}") from exc
        return {"kind": kind, "handle": handle, "display": display}

    @property
    def canonical(self) -> str:
        """Storage form: `github:<handle>` or the plain name."""
        return f"{_GITHUB_PREFIX}{self.handle}" if self.handle else self.display

    @property
    def profile_url(self) -> str | None:
        """GitHub profile URL for github authors, else None."""
        return f"{_GITHUB_URL_PREFIX}{self.handle}" if self.handle else None


def parse_author(raw: Any) -> ManifestAuthor:
    """Validate `raw` and return the normalized author; raises `AuthorError` with a code."""
    try:
        return ManifestAuthor.model_validate(raw)
    except ValidationError as exc:
        message = str(exc.errors()[0].get("msg", "")).removeprefix("Value error, ")
        code, _, detail = message.partition(": ")
        raise AuthorError(code, detail or message) from exc


def canonical_author(raw: Any) -> str:
    """Validate `raw` and return its canonical storage string."""
    return parse_author(raw).canonical


def author_view(stored: str | None) -> dict[str, str | None] | None:
    """Read-path projection of a stored author: `{display, github_handle, profile_url}`.

    Lenient by design: a legacy row that predates validation still renders (as a plain
    name, never linked) instead of failing the listing. Writes always go through
    `parse_author`.
    """
    if not stored:
        return None
    try:
        author = parse_author(stored)
    except AuthorError:
        return {"display": stored[:255], "github_handle": None, "profile_url": None}
    return {
        "display": author.display,
        "github_handle": author.handle,
        "profile_url": author.profile_url,
    }
