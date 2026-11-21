"""Shared `?limit=` page-size parsing -- server-side upper bound (security.md Input Validation).

Before this module existed, 16 of hub-api's 18 list endpoints parsed
`?limit=` with a bare `int(request.args.get("limit", "<default>"))` --
no upper bound, so `?limit=999999` reached the DB/proxy call unclamped
(an unbounded-response-size DoS surface, gh security-review HIGH
alongside C5's rate-limiting gap). Two endpoints (`cookie_consent.py`,
`analytics.py`) had already independently invented their own clamp
helper (`_parse_int`/`_clamp_limit`) -- this module is the one shared
implementation every list endpoint now routes through, so a future
endpoint gets bounds validation for free instead of needing to
reinvent it a third time.
"""

from __future__ import annotations

import re

from services.errors import bad_request

#: Hard ceiling when a call site doesn't pass its own `maximum=` --
#: matches `HubAPIConfig.api_max_page_size`'s own default (`API_MAX_PAGE_SIZE`
#: env var), kept as a plain constant here (not read from config) so this
#: module has no import-time dependency on `config.HubAPIConfig.from_env()`
#: having already run -- callers that DO want the configured value pass
#: `maximum=cfg.api_max_page_size` explicitly.
DEFAULT_MAX_PAGE_SIZE = 100

#: A page number: 1-9 ASCII digits (bounded so `int()` can never be handed a huge value).
_PAGE_RE = re.compile(r"[0-9]{1,9}")


def parse_limit(
    raw: str | None,
    *,
    default: int,
    maximum: int = DEFAULT_MAX_PAGE_SIZE,
    minimum: int = 1,
) -> int:
    """Parse a query-string `?limit=` value, clamped to `[minimum, maximum]`.

    Garbage/missing input falls back to `default` rather than raising --
    matches `cookie_consent.py::_parse_int`'s existing precedent (a bad
    client value degrades to a safe default, it never 500s the request).
    """
    try:
        parsed = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def parse_page(raw: str | None) -> int:
    """Parse a query-string `?page=`: missing/empty -> 1, otherwise a positive integer.

    Validated explicitly (no exception-driven default): anything that is not 1-9 ASCII digits
    raises a typed 400 `ApiError`, so a bad client value is a client error and never a 500.
    Zero clamps to 1.
    """
    if raw is None or raw == "":
        return 1
    if not _PAGE_RE.fullmatch(raw):
        raise bad_request("Invalid page")
    return max(1, int(raw))
