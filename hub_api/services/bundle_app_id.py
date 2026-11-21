"""The ONE Python implementation of the bundle app_id grammar and storage-key derivation.

Every Python site that validates an app_id, detects the first-party core namespace, or
derives a per-bundle table / schema / kv / object key from an app_id goes through this
module. The Rust twin is `core/bundle_capability_gate/src/resource.rs`
(`is_valid_app_id`, `is_core_namespace`, `bundle_storage_key`); both are proven
byte-identical by the golden vectors in `testdata/bundle_app_id/vectors.json`, which
a Rust test and `hub_api/tests/test_bundle_app_id.py` both read.

Grammar: `<github-org>.<repo>.<bundle-name>` -- exactly three `.`-separated segments, each
`[a-z0-9][a-z0-9-]*` (no `_`), at most 127 bytes. GitHub org/repo names map `_` -> `-` when
lowercased (docs/BUNDLE-APP-IDS.md).
"""

from __future__ import annotations

import hashlib
import re

#: Max app_id length in bytes.
APP_ID_MAX_LEN = 127

#: One segment: `[a-z0-9][a-z0-9-]*` -- no underscore (matches Rust `is_valid_segment`).
SEGMENT_PATTERN = r"[a-z0-9][a-z0-9-]*"

#: Full-id pattern (exactly three segments); length is checked separately.
APP_ID_PATTERN = rf"{SEGMENT_PATTERN}\.{SEGMENT_PATTERN}\.{SEGMENT_PATTERN}"

#: Anchored, compiled form for `fullmatch`/servicer use (no trailing-newline `$` hole).
APP_ID_RE = re.compile(APP_ID_PATTERN)
_SEGMENT_RE = re.compile(SEGMENT_PATTERN)

#: Reserved first-party namespace; an id is core only via `is_core_namespace`.
CORE_NAMESPACE_PREFIX = "penguintechinc.waddles."

#: Postgres `NAMEDATALEN` - 1: the identifier ceiling every storage key must fit in.
POSTGRES_IDENTIFIER_MAX_LEN = 63
#: Hex chars of `sha256(app_id)` appended to every storage key.
STORAGE_KEY_HASH_LEN = 16


def is_valid_app_id(app_id: object) -> bool:
    """True only for a well-formed `<org>.<repo>.<name>` id (<=127 bytes, no `_`).

    Never raises: a non-string, an empty string, or a string with a trailing newline is simply
    invalid (`fullmatch`, never `$`).
    """
    return (
        isinstance(app_id, str)
        and len(app_id.encode("utf-8")) <= APP_ID_MAX_LEN
        and APP_ID_RE.fullmatch(app_id) is not None
    )


def is_core_namespace(app_id: object) -> bool:
    """True only for a first-party core id: `penguintechinc.waddles.` + exactly one valid segment.

    Mirrors Rust `is_core_namespace` exactly. A bare `startswith` would also trust
    `penguintechinc.waddles.x.y` or `penguintechinc.waddles.X`.
    """
    if not isinstance(app_id, str) or not app_id.startswith(CORE_NAMESPACE_PREFIX):
        return False
    if len(app_id.encode("utf-8")) > APP_ID_MAX_LEN:
        return False
    return _SEGMENT_RE.fullmatch(app_id[len(CORE_NAMESPACE_PREFIX) :]) is not None


def bundle_storage_key(app_id: str) -> str:
    """The injective per-bundle storage key: `<readable>_<sha256(raw app_id)[:16 hex]>`.

    Used for the data table name, kv namespace and object prefix alike. The readable part
    (lower-cased ASCII alphanumerics, every other char -> `_`, runs collapsed, ends trimmed,
    `b_`-prefixed when the id does not start with an ASCII letter, `b` if empty, truncated to
    leave room for the suffix) is for humans only; injectivity comes from the suffix hashing the
    EXACT app_id, so `acme.bot.kv-store` and `acme.bot_kv.store` never share a key. Result is
    <= 63 bytes, `[a-z][a-z0-9_]*`, never contains `__` or `:`. Byte-identical to Rust
    `bundle_storage_key`.
    """
    readable: list[str] = []
    if app_id and not (app_id[0].isascii() and app_id[0].isalpha()):
        readable.append("b_")
    for char in app_id:
        mapped = char.lower() if (char.isascii() and char.isalnum()) else "_"
        if mapped == "_" and readable and readable[-1].endswith("_"):
            continue
        readable.append(mapped)
    keep = POSTGRES_IDENTIFIER_MAX_LEN - STORAGE_KEY_HASH_LEN - 1
    text = "".join(readable)[:keep].rstrip("_") or "b"
    digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:STORAGE_KEY_HASH_LEN]
    return f"{text}_{digest}"
