"""Marketplace author DTO: the structured, linkable form of a stored bundle/module author.

Listings expose `{display, githubHandle, profileUrl}` instead of free text so the UI can
render a GitHub profile link without ever trusting a URL from the manifest -- the link is
derived server-side from a validated handle.
"""

# ruff: noqa: N815  (camelCase fields are the hub-api DTO wire convention)
from __future__ import annotations

from dataclasses import dataclass

from flask_core.bundle_author import author_view


@dataclass(slots=True, frozen=True)
class AuthorDTO:
    """Author of a marketplace entry; `profileUrl`/`githubHandle` are set for GitHub authors."""

    display: str
    githubHandle: str | None
    profileUrl: str | None


def to_author_dto(stored: str | None) -> AuthorDTO | None:
    """Project a stored author string into an `AuthorDTO` (None when no author is stored)."""
    view = author_view(stored)
    if view is None:
        return None
    display = view["display"] or ""
    return AuthorDTO(
        display=display,
        githubHandle=view["github_handle"],
        profileUrl=view["profile_url"],
    )
