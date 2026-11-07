"""Valid/invalid matrix for `flask_core.bundle_author` and the `collection` helpers."""

from __future__ import annotations

from typing import Any

import pytest
from flask_core.bundle_attribution import (
    collection_attribution_problem,
    valid_collection_slug,
)
from flask_core.bundle_author import (
    AuthorError,
    AuthorKind,
    ManifestAuthor,
    author_view,
    canonical_author,
    parse_author,
)

_VALID: list[tuple[str, AuthorKind, str | None, str, str]] = [
    (
        "github:penguintechinc",
        AuthorKind.GITHUB,
        "penguintechinc",
        "penguintechinc",
        "github:penguintechinc",
    ),
    (
        "github:Psychoboy",
        AuthorKind.GITHUB,
        "Psychoboy",
        "Psychoboy",
        "github:Psychoboy",
    ),
    ("@octo-cat", AuthorKind.GITHUB, "octo-cat", "octo-cat", "github:octo-cat"),
    ("@a", AuthorKind.GITHUB, "a", "a", "github:a"),
    (
        "https://github.com/octo-cat",
        AuthorKind.GITHUB,
        "octo-cat",
        "octo-cat",
        "github:octo-cat",
    ),
    (
        "https://github.com/octo-cat/",
        AuthorKind.GITHUB,
        "octo-cat",
        "octo-cat",
        "github:octo-cat",
    ),
    ("github:" + "a" * 39, AuthorKind.GITHUB, "a" * 39, "a" * 39, "github:" + "a" * 39),
    ("Penguin Tech", AuthorKind.NAME, None, "Penguin Tech", "Penguin Tech"),
    ("jane_doe.v2-x", AuthorKind.NAME, None, "jane_doe.v2-x", "jane_doe.v2-x"),
    ("José Núñez", AuthorKind.NAME, None, "José Núñez", "José Núñez"),
    ("x", AuthorKind.NAME, None, "x", "x"),
    ("n" * 64, AuthorKind.NAME, None, "n" * 64, "n" * 64),
]

_INVALID: list[tuple[Any, str]] = [
    (None, "author_required"),
    ("", "author_required"),
    (42, "author_not_string"),
    (["github:a"], "author_not_string"),
    ("a\nb", "author_control_chars"),
    ("a\x00b", "author_control_chars"),
    ("a b", "author_control_chars"),
    ("<b>bold</b>", "author_markup_rejected"),
    ("[x](http://e.com)", "author_invalid_url"),
    ("[x](y)", "author_markup_rejected"),
    ("Penguin (Psychoboy)", "author_markup_rejected"),
    ("a`b", "author_markup_rejected"),
    ("https://gitlab.com/foo", "author_invalid_url"),
    ("http://github.com/foo", "author_invalid_url"),
    ("https://github.com/foo/bar", "author_invalid_url"),
    ("https://github.com/foo?tab=repos", "author_invalid_url"),
    ("https://github.com/foo#readme", "author_invalid_url"),
    ("github:", "author_invalid_github_handle"),
    ("@", "author_invalid_github_handle"),
    ("github:-lead", "author_invalid_github_handle"),
    ("github:trail-", "author_invalid_github_handle"),
    ("github:dou--ble", "author_invalid_github_handle"),
    ("github:" + "a" * 40, "author_invalid_github_handle"),
    ("@under_score", "author_invalid_github_handle"),
    ("github:foo bar", "author_invalid_github_handle"),
    ("-leading", "author_invalid_name"),
    ("trailing.", "author_invalid_name"),
    ("two  spaces", "author_invalid_name"),
    ("a - b", "author_invalid_name"),
    (" padded", "author_invalid_name"),
    ("emoji \U0001f600", "author_invalid_name"),
    ("_x", "author_invalid_name"),
    ("n" * 65, "author_too_long"),
    ("github:" + "a" * 70, "author_too_long"),
]


@pytest.mark.parametrize(("raw", "kind", "handle", "display", "canonical"), _VALID)
def test_valid_authors_normalize(
    raw: str, kind: AuthorKind, handle: str | None, display: str, canonical: str
) -> None:
    author = parse_author(raw)
    assert (author.kind, author.handle, author.display, author.canonical) == (
        kind,
        handle,
        display,
        canonical,
    )
    assert canonical_author(raw) == canonical
    # Canonical form is a fixed point.
    assert canonical_author(canonical) == canonical


@pytest.mark.parametrize(("raw", "code"), _INVALID)
def test_invalid_authors_carry_a_stable_code(raw: Any, code: str) -> None:
    with pytest.raises(AuthorError) as exc:
        parse_author(raw)
    assert exc.value.code == code
    assert str(exc.value)


def test_trailing_newline_is_not_swallowed_by_fullmatch() -> None:
    """Regression for `$`-before-trailing-newline: fullmatch must reject it."""
    with pytest.raises(AuthorError):
        parse_author("github:octo\n")
    with pytest.raises(AuthorError):
        parse_author("name\n")


def test_profile_url_only_for_github() -> None:
    assert parse_author("@octo").profile_url == "https://github.com/octo"
    assert parse_author("Plain Name").profile_url is None


def test_model_validate_accepts_dict_roundtrip() -> None:
    author = parse_author("@octo")
    assert ManifestAuthor.model_validate(author.model_dump()) == author


def test_author_view_github_name_none_and_legacy() -> None:
    assert author_view("github:octo") == {
        "display": "octo",
        "github_handle": "octo",
        "profile_url": "https://github.com/octo",
    }
    assert author_view("Plain Name") == {
        "display": "Plain Name",
        "github_handle": None,
        "profile_url": None,
    }
    assert author_view(None) is None
    assert author_view("") is None
    # Legacy unvalidated value renders as a plain, never-linked name.
    assert author_view("superpenguintv (Psychoboy)") == {
        "display": "superpenguintv (Psychoboy)",
        "github_handle": None,
        "profile_url": None,
    }


@pytest.mark.parametrize("slug", ["superpenguintv", "a", "a-b-c", "x1", "9"])
def test_valid_collection_slugs(slug: str) -> None:
    assert valid_collection_slug(slug)


@pytest.mark.parametrize(
    "slug", ["", "Upper", "a_b", "-a", "a-", "a--b", "a b", "a\n", "x" * 51]
)
def test_invalid_collection_slugs(slug: str) -> None:
    assert not valid_collection_slug(slug)


@pytest.mark.parametrize(
    ("collection", "license_id", "notice", "problem"),
    [
        (None, None, None, False),
        ("other", None, None, False),
        ("superpenguintv", "MIT", "Ported from X. MIT License", False),
        ("superpenguintv", "Apache-2.0", "notice", True),
        ("superpenguintv", None, "notice", True),
        ("superpenguintv", "MIT", None, True),
        ("superpenguintv", "MIT", "   ", True),
    ],
)
def test_attributed_collection_requires_mit_and_notice(
    collection: str | None, license_id: str | None, notice: str | None, problem: bool
) -> None:
    assert (
        collection_attribution_problem(collection, license_id, notice) is not None
    ) is problem
