"""Bundle-declared `data.table` manifest parsing & validation.

Phase 0 of `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-
schemas.md` (Rev 5) §3: a bundle manifest may declare **one** physical
table (`data.table.columns[]`/`indexes[]`) drawn from a strict typed-
column allowlist, no bundle-influenced FK/CHECK grammar, no raw SQL ever.
This module is the "declared -> validated" half of that pipeline --
:mod:`bundle_data_ddl` is the "validated -> DDL" half. **Not wired into
onboarding yet** (`bundle_manifest_v2.py`/`bundle_approval_service.py`
integration is a later phase per the spec's §14 phased plan) -- this is a
pure library, exercised only by its own tests today.

Every rejection raises a typed :class:`TableDeclarationError` carrying a
stable machine-checkable ``reason`` code (mirrors
`libs/flask_core/flask_core/app_manifest.py`'s ``ManifestError`` pattern
exactly, including the `column_name_suggests_pii` code name the spec's
§3.2/§1 review-history table calls out by that literal string) so callers
and tests can assert on *why* a declaration was rejected, not just that it
was.

Security note: this module never builds or executes SQL -- it only
produces validated, immutable dataclasses. Nothing here is a defense
against SQL injection by itself; :mod:`bundle_data_ddl` is what enforces
"identifiers only via `psycopg2.sql.Identifier`, values only via
`psycopg2.sql.Literal`" using the *outputs* of this module's validation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TableDeclarationError(Exception):
    """Raised when a bundle's `data.table` declaration fails validation.

    ``reason`` is a stable machine-checkable code (see the ``REASON_*``
    constants below) -- callers/tests assert on it, not on message text.
    """

    def __init__(self, reason: str, detail: str) -> None:
        """Store the machine-checkable `reason` code alongside the human-readable `detail`."""
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


REASON_NO_COLUMNS = "no_columns"
REASON_TOO_MANY_COLUMNS = "too_many_columns"
REASON_INVALID_COLUMN_NAME = "invalid_column_name"
REASON_DUPLICATE_COLUMN = "duplicate_column"
REASON_RESERVED_COLUMN_NAME = "reserved_column_name"
REASON_INVALID_COLUMN_TYPE = "invalid_column_type"
REASON_NUMERIC_PRECISION = "invalid_numeric_precision"
REASON_TEXT_LENGTH = "invalid_text_length"
REASON_JSONB_LENGTH = "invalid_jsonb_length"
REASON_COLUMN_NAME_SUGGESTS_PII = "column_name_suggests_pii"
REASON_INVALID_DEFAULT_LITERAL = "invalid_default_literal"
REASON_DEFAULT_TYPE_MISMATCH = "default_type_mismatch"
REASON_DEFAULT_TOO_LONG = "default_too_long"
REASON_USER_REF_NULLABILITY = "user_ref_nullability_violation"
REASON_PII_ADJACENT_NOT_NULLABLE = "pii_adjacent_not_nullable"
REASON_NO_INDEXES = "no_indexes"  # reserved; empty indexes list is legal
REASON_TOO_MANY_INDEXES = "too_many_indexes"
REASON_INDEX_NO_COLUMNS = "index_no_columns"
REASON_INDEX_TOO_WIDE = "index_too_wide"
REASON_INDEX_UNKNOWN_COLUMN = "index_unknown_column"
REASON_INVALID_TABLE_IDENTITY = "invalid_table_identity"
REASON_INVALID_PROVIDER = "invalid_provider"

# ---------------------------------------------------------------------------
# Limits (spec §3.1/§3.2/§3.4/§3.5)
# ---------------------------------------------------------------------------

MAX_COLUMNS = 32
MAX_INDEXES = 8
MAX_INDEX_WIDTH = 4  # bundle-declared columns per composite index (tenant_id
# is auto-prefixed by bundle_data_ddl, on top of this cap -- see §3.5).
MAX_NUMERIC_PRECISION = 38
MAX_NUMERIC_SCALE = 12
MAX_TEXT_LEN = 8192
MAX_JSONB_BYTES = 16384  # 16 KiB, §3.1
POSTGRES_IDENTIFIER_MAX_LEN = 63  # NAMEDATALEN - 1, §3.4
TABLE_NAME_HASH_SUFFIX_LEN = 8

_COLUMN_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]{0,62}\Z")

# Platform-owned columns (§3.3) -- a bundle may never declare a column with
# one of these names; bundle_data_ddl attaches them unconditionally.
RESERVED_COLUMN_NAMES = frozenset(
    {"row_id", "tenant_id", "community_id", "version", "created_at", "updated_at"}
)

# §3.2 C5.1: normalized (case- and separator-insensitive) denylist, expanded
# from Rev 3's small exact-match list per Gemini review round 3. Matching is
# substring containment on the *normalized* (lower-cased, `-`/`_`/` `
# stripped) column name against each *normalized* denylist token -- this is
# what the spec's own wording ("normalized ... match") describes, and what
# makes compounds like `first-name`/`display_name` both collapse onto a
# `name` hit without a bespoke compound list. Substring matching is a
# deliberate, spec-acknowledged tradeoff (§3.7: "catches name-shaped risk
# only ... stated honestly, not solved") -- e.g. `ip` also flags an unrelated
# column like `skip_count` (normalizes to `skipcount`, contains `ip`). That
# false-positive is accepted by design: a reviewer renames or confirms, they
# never silently pass (§3.2: "rejects the manifest ... not a silent
# warning").
_PII_DENYLIST = (
    "email",
    "e-mail",
    "mail",
    "name",
    "username",
    "handle",
    "login",
    "phone",
    "address",
    "ip",
    "ssn",
    "dob",
    "birth",
)


def _normalize_for_pii_check(value: str) -> str:
    lowered = value.lower()
    for sep in ("-", "_", " "):
        lowered = lowered.replace(sep, "")
    return lowered


_PII_DENYLIST_NORMALIZED = tuple(_normalize_for_pii_check(token) for token in _PII_DENYLIST)


def column_name_suggests_pii(name: str) -> bool:
    """Return True if `name` normalizes to a substring match against the PII denylist."""
    normalized = _normalize_for_pii_check(name)
    return any(token in normalized for token in _PII_DENYLIST_NORMALIZED)


# ---------------------------------------------------------------------------
# Column types (§3.1 allowlist)
# ---------------------------------------------------------------------------


class ColumnType(Enum):
    """The full, closed type allowlist -- no other type is ever accepted."""

    USER_REF = "user_ref"
    UUID = "uuid"
    INT4 = "int4"
    INT8 = "int8"
    NUMERIC = "numeric"
    BOOL = "bool"
    TEXT = "text"
    TIMESTAMPTZ = "timestamptz"
    JSONB = "jsonb"


_SIMPLE_TYPES = {
    "user_ref": ColumnType.USER_REF,
    "uuid": ColumnType.UUID,
    "int4": ColumnType.INT4,
    "int8": ColumnType.INT8,
    "bool": ColumnType.BOOL,
    "timestamptz": ColumnType.TIMESTAMPTZ,
}
_NUMERIC_RE = re.compile(r"\Anumeric\((\d{1,3}),(\d{1,3})\)\Z")
_TEXT_RE = re.compile(r"\Atext\((\d{1,6})\)\Z")
_JSONB_BARE_RE = re.compile(r"\Ajsonb\Z")
_JSONB_SIZED_RE = re.compile(r"\Ajsonb\((\d{1,6})\)\Z")

# Erasure actions valid on a `user_ref` column's declaration (§4).
ERASURE_DELETE_ROW = "delete_row"
ERASURE_ANONYMIZE = "anonymize"
_KNOWN_ERASURE_ACTIONS = frozenset({ERASURE_DELETE_ROW, ERASURE_ANONYMIZE})


@dataclass(slots=True, frozen=True)
class ParsedColumnType:
    """A validated `ColumnType` plus its type-specific parameters, if any."""

    kind: ColumnType
    numeric_precision: int | None = None
    numeric_scale: int | None = None
    max_len: int | None = None  # text (chars) or jsonb (bytes)


def _parse_column_type(raw: str) -> ParsedColumnType:
    if raw in _SIMPLE_TYPES:
        return ParsedColumnType(kind=_SIMPLE_TYPES[raw])

    match = _NUMERIC_RE.fullmatch(raw)
    if match:
        precision, scale = int(match.group(1)), int(match.group(2))
        if not (1 <= precision <= MAX_NUMERIC_PRECISION):
            raise TableDeclarationError(
                REASON_NUMERIC_PRECISION,
                f"numeric precision {precision} exceeds {MAX_NUMERIC_PRECISION}",
            )
        if not (0 <= scale <= MAX_NUMERIC_SCALE):
            raise TableDeclarationError(
                REASON_NUMERIC_PRECISION, f"numeric scale {scale} exceeds {MAX_NUMERIC_SCALE}"
            )
        if scale > precision:
            raise TableDeclarationError(
                REASON_NUMERIC_PRECISION, f"numeric scale {scale} exceeds precision {precision}"
            )
        return ParsedColumnType(
            kind=ColumnType.NUMERIC, numeric_precision=precision, numeric_scale=scale
        )

    match = _TEXT_RE.fullmatch(raw)
    if match:
        max_len = int(match.group(1))
        if not (1 <= max_len <= MAX_TEXT_LEN):
            raise TableDeclarationError(
                REASON_TEXT_LENGTH, f"text length {max_len} exceeds {MAX_TEXT_LEN}"
            )
        return ParsedColumnType(kind=ColumnType.TEXT, max_len=max_len)

    if _JSONB_BARE_RE.fullmatch(raw):
        return ParsedColumnType(kind=ColumnType.JSONB, max_len=MAX_JSONB_BYTES)

    match = _JSONB_SIZED_RE.fullmatch(raw)
    if match:
        max_len = int(match.group(1))
        if not (1 <= max_len <= MAX_JSONB_BYTES):
            raise TableDeclarationError(
                REASON_JSONB_LENGTH, f"jsonb size {max_len} exceeds {MAX_JSONB_BYTES}"
            )
        return ParsedColumnType(kind=ColumnType.JSONB, max_len=max_len)

    raise TableDeclarationError(REASON_INVALID_COLUMN_TYPE, f"{raw!r} is not an allowlisted type")


# ---------------------------------------------------------------------------
# Column default literals (§3.2 C2.1 -- literal-only, strict parser)
# ---------------------------------------------------------------------------


class LiteralKind(Enum):
    """The exactly-four shapes a bundle-declared default may take."""

    NULL = "null"
    NUMBER = "number"
    BOOL = "bool"
    STRING = "string"


@dataclass(slots=True, frozen=True)
class LiteralDefault:
    """A parsed, type-checked column default -- never a function/cast/expression."""

    kind: LiteralKind
    value: Any  # None | int | float | bool | str
    raw: str  # original manifest text, retained for error messages/snapshots


_NULL_RE = re.compile(r"\ANULL\Z", re.IGNORECASE)
_BOOL_RE = re.compile(r"\A(true|false)\Z", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\A-?\d+(\.\d+)?\Z")
# Single-quoted SQL string literal: doubled `''` is the only escape accepted
# (standard SQL string-literal syntax); a literal backslash is rejected
# outright -- there is no escape grammar here at all, only "is this exactly
# one quoted string or not". Anchored with `\A`/`\Z`, not `^`/`$` -- `$`
# matches immediately before a trailing `\n`, which would let a payload
# like `"'hello'\n"` pass this check and then have `raw[1:-1]` silently
# slice off the closing quote instead of the trailing newline.
_STRING_RE = re.compile(r"\A'(?:[^'\\]|'')*'\Z")


def parse_default_literal(raw: str) -> LiteralDefault:
    """Parse a bundle-declared column default with a strict, closed grammar.

    Accepts exactly one of: `NULL` (any case), `true`/`false` (any case), a
    bare integer/decimal number, or a single-quoted string (`''` doubled-
    quote escape only). Anything else -- a function call, a cast (`::`), an
    expression, an unquoted bareword -- is rejected. This is the whole of
    §3.2 C2.1's requirement: bundle-declared defaults are scalar literals,
    never function calls (`now()`, `gen_random_uuid()`), full stop.
    """
    if _NULL_RE.fullmatch(raw):
        return LiteralDefault(kind=LiteralKind.NULL, value=None, raw=raw)
    if _BOOL_RE.fullmatch(raw):
        return LiteralDefault(kind=LiteralKind.BOOL, value=raw.lower() == "true", raw=raw)
    if _NUMBER_RE.fullmatch(raw):
        value: Any = float(raw) if "." in raw else int(raw)
        return LiteralDefault(kind=LiteralKind.NUMBER, value=value, raw=raw)
    if _STRING_RE.fullmatch(raw):
        # `_STRING_RE` is `\A...\Z`-anchored, so a fullmatch guarantees `raw`
        # is exactly a leading quote, doubled-quote-escaped body, and a
        # trailing quote with nothing else -- but slicing is only trusted
        # once that shape is re-asserted explicitly here, not inferred from
        # the match alone, so a future regex edit that loosens the anchors
        # fails closed (raises) instead of silently mis-slicing.
        if len(raw) < 2 or raw[0] != "'" or raw[-1] != "'":  # pragma: no cover - defense in depth
            raise TableDeclarationError(
                REASON_INVALID_DEFAULT_LITERAL, f"{raw!r} is not a well-formed quoted string"
            )
        inner = raw[1:-1].replace("''", "'")
        return LiteralDefault(kind=LiteralKind.STRING, value=inner, raw=raw)
    raise TableDeclarationError(
        REASON_INVALID_DEFAULT_LITERAL,
        f"{raw!r} is not a literal NULL/bool/number/quoted-string -- "
        "functions, casts, and expressions are never accepted",
    )


def _validate_default_for_type(
    default: LiteralDefault, parsed_type: ParsedColumnType, *, nullable: bool
) -> None:
    if default.kind == LiteralKind.NULL:
        if not nullable:
            raise TableDeclarationError(
                REASON_DEFAULT_TYPE_MISMATCH, "default NULL requires the column to be nullable"
            )
        return

    if default.kind == LiteralKind.BOOL:
        if parsed_type.kind is not ColumnType.BOOL:
            raise TableDeclarationError(
                REASON_DEFAULT_TYPE_MISMATCH,
                f"bool default is not valid for {parsed_type.kind.value}",
            )
        return

    if default.kind == LiteralKind.NUMBER:
        if parsed_type.kind not in (ColumnType.INT4, ColumnType.INT8, ColumnType.NUMERIC):
            raise TableDeclarationError(
                REASON_DEFAULT_TYPE_MISMATCH,
                f"numeric default is not valid for {parsed_type.kind.value}",
            )
        return

    # STRING -- valid for text (length-checked below), and as a literal
    # source value for uuid/timestamptz/jsonb (Postgres applies the implicit
    # assignment cast at DDL-apply time; this module only checks the shapes
    # this codebase's own type allowlist permits a string default for).
    if parsed_type.kind == ColumnType.TEXT:
        if parsed_type.max_len is None:  # pragma: no cover - text always has a max_len
            raise TableDeclarationError(REASON_DEFAULT_TYPE_MISMATCH, "text column missing max_len")
        if len(default.value) > parsed_type.max_len:
            raise TableDeclarationError(
                REASON_DEFAULT_TOO_LONG,
                f"default string length {len(default.value)} "
                f"exceeds column max_len {parsed_type.max_len}",
            )
        return
    if parsed_type.kind == ColumnType.JSONB:
        if parsed_type.max_len is None:  # pragma: no cover - jsonb always has a max_len
            raise TableDeclarationError(
                REASON_DEFAULT_TYPE_MISMATCH, "jsonb column missing max_len"
            )
        if len(default.value.encode("utf-8")) > parsed_type.max_len:
            raise TableDeclarationError(
                REASON_DEFAULT_TOO_LONG,
                f"default jsonb payload exceeds column max_len {parsed_type.max_len} bytes",
            )
        return
    if parsed_type.kind in (ColumnType.UUID, ColumnType.TIMESTAMPTZ):
        return
    raise TableDeclarationError(
        REASON_DEFAULT_TYPE_MISMATCH, f"string default is not valid for {parsed_type.kind.value}"
    )


# ---------------------------------------------------------------------------
# Column & index declarations
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ColumnDecl:
    """One validated bundle-declared column."""

    name: str
    parsed_type: ParsedColumnType
    nullable: bool
    default: LiteralDefault | None = None
    pii_adjacent: bool = False
    on_erasure: str | None = None  # only meaningful when parsed_type.kind is USER_REF

    @property
    def type_kind(self) -> ColumnType:
        """Shorthand for `self.parsed_type.kind`."""
        return self.parsed_type.kind


@dataclass(slots=True, frozen=True)
class IndexColumnRef:
    """One column reference inside a declared index, with optional sort direction."""

    name: str
    descending: bool = False


@dataclass(slots=True, frozen=True)
class IndexDecl:
    """A bundle-declared index -- `bundle_data_ddl` auto-prefixes `tenant_id`."""

    columns: tuple[IndexColumnRef, ...]


@dataclass(slots=True, frozen=True)
class TableDeclaration:
    """A fully validated `data.table` declaration -- the input to `bundle_data_ddl`."""

    columns: tuple[ColumnDecl, ...]
    indexes: tuple[IndexDecl, ...] = ()

    @property
    def jsonb_columns(self) -> tuple[ColumnDecl, ...]:
        """Columns requiring §2.1/§3.1 mandatory secondary human review at vendor approval."""
        return tuple(c for c in self.columns if c.type_kind is ColumnType.JSONB)

    def column(self, name: str) -> ColumnDecl | None:
        """Return the declared column named `name`, or `None`."""
        for c in self.columns:
            if c.name == name:
                return c
        return None


def _validate_column_name(name: Any) -> str:
    if not isinstance(name, str) or not _COLUMN_NAME_RE.fullmatch(name):
        raise TableDeclarationError(
            REASON_INVALID_COLUMN_NAME, f"{name!r} must match [a-z][a-z0-9_]{{0,62}} exactly"
        )
    return name


def _parse_column(raw: Mapping[str, Any]) -> ColumnDecl:
    name = _validate_column_name(raw.get("name"))

    if name in RESERVED_COLUMN_NAMES:
        raise TableDeclarationError(
            REASON_RESERVED_COLUMN_NAME,
            f"{name!r} is a platform-owned column, bundles may not declare it",
        )

    raw_type = raw.get("type")
    if not isinstance(raw_type, str) or not raw_type:
        raise TableDeclarationError(REASON_INVALID_COLUMN_TYPE, f"column {name!r} has no type")
    parsed_type = _parse_column_type(raw_type)

    if column_name_suggests_pii(name):
        raise TableDeclarationError(
            REASON_COLUMN_NAME_SUGGESTS_PII,
            f"column name {name!r} normalizes to a PII-denylist match -- rename and "
            "confirm no PII, or drop the column",
        )

    nullable = bool(raw.get("nullable", False))
    pii_adjacent = bool(raw.get("pii_adjacent", False))

    on_erasure: str | None = None
    if parsed_type.kind is ColumnType.USER_REF:
        on_erasure = raw.get("on_erasure", ERASURE_DELETE_ROW)
        if on_erasure not in _KNOWN_ERASURE_ACTIONS:
            raise TableDeclarationError(
                REASON_USER_REF_NULLABILITY,
                f"user_ref column {name!r} on_erasure {on_erasure!r} must be one of "
                f"{sorted(_KNOWN_ERASURE_ACTIONS)}",
            )
        # §3.2 C4.2: NOT NULL + anonymize is unsatisfiable (anonymize nulls
        # the column) -- rejected at validation, not left to fail at
        # erasure-sweep time.
        if on_erasure == ERASURE_ANONYMIZE and not nullable:
            raise TableDeclarationError(
                REASON_USER_REF_NULLABILITY,
                f"user_ref column {name!r} must be nullable when on_erasure is "
                "'anonymize' (a NOT NULL column cannot be nulled)",
            )
    elif "on_erasure" in raw:
        raise TableDeclarationError(
            REASON_USER_REF_NULLABILITY,
            f"column {name!r}: on_erasure is only meaningful on a user_ref column",
        )

    # §3.2: a pii_adjacent column (nulled alongside an anonymized user_ref
    # column at erasure sweep time) must itself be nullable, for the same
    # reason a NOT NULL user_ref + anonymize combination is unsatisfiable.
    if pii_adjacent and not nullable:
        raise TableDeclarationError(
            REASON_PII_ADJACENT_NOT_NULLABLE,
            f"column {name!r} is pii_adjacent=true and must be nullable "
            "(anonymize nulls it at erasure sweep time)",
        )

    default: LiteralDefault | None = None
    raw_default = raw.get("default")
    if raw_default is not None:
        default = parse_default_literal(str(raw_default))
        _validate_default_for_type(default, parsed_type, nullable=nullable)

    return ColumnDecl(
        name=name,
        parsed_type=parsed_type,
        nullable=nullable,
        default=default,
        pii_adjacent=pii_adjacent,
        on_erasure=on_erasure,
    )


def _parse_index(raw: Mapping[str, Any], declared_column_names: frozenset[str]) -> IndexDecl:
    raw_columns = raw.get("columns")
    if not isinstance(raw_columns, list | tuple) or not raw_columns:
        raise TableDeclarationError(
            REASON_INDEX_NO_COLUMNS, "an index must declare at least one column"
        )
    if len(raw_columns) > MAX_INDEX_WIDTH:
        raise TableDeclarationError(
            REASON_INDEX_TOO_WIDE,
            f"index has {len(raw_columns)} bundle-declared columns, exceeds {MAX_INDEX_WIDTH}",
        )

    refs = []
    for entry in raw_columns:
        col_name: Any
        if isinstance(entry, str):
            col_name, descending = entry, False
        elif isinstance(entry, Mapping):
            col_name = entry.get("column")
            descending = str(entry.get("dir", "asc")).lower() == "desc"
        else:
            raise TableDeclarationError(
                REASON_INDEX_UNKNOWN_COLUMN,
                f"index column entry {entry!r} is not a string or mapping",
            )
        if not isinstance(col_name, str) or col_name not in declared_column_names:
            raise TableDeclarationError(
                REASON_INDEX_UNKNOWN_COLUMN,
                f"index references column {col_name!r}, which is not declared on this table",
            )
        refs.append(IndexColumnRef(name=col_name, descending=descending))

    return IndexDecl(columns=tuple(refs))


def validate_table_declaration(raw: Mapping[str, Any], *, provider: str) -> TableDeclaration:
    """Validate a bundle manifest's `data.table` dict into a `TableDeclaration`.

    ``provider`` (`'builtin' | 'thirdparty'`, matching
    `flask_core.app_manifest.KNOWN_PROVIDERS`) is accepted for symmetry with
    `bundle_data_ddl.derive_table_identity` (schema selection is provider-
    driven, never manifest-claimed) but this function does not itself
    branch on it -- validation is identical for core and community bundles
    (§2.1: "validation doesn't distinguish provenance").

    Phase 1 integration note: ``provider`` MUST be resolved by the caller
    from the platform's own app/provider registry (the same source
    `bundle_data_ddl.derive_table_identity`'s call site uses), never read
    off the bundle manifest or an inbound request payload -- a bundle or
    caller supplying its own `provider` value is exactly the "manifest-
    claimed schema placement" attack `derive_table_identity`'s docstring
    already rejects (see `test_schema_is_never_taken_from_a_manifest_
    looking_claim`). When onboarding wires this module in, add an
    assertion/lookup at the call site that fetches `provider` from the
    registry keyed by the already-authenticated app identity, not from
    `raw`.

    Raises `TableDeclarationError` on the first violation found, in this
    order: column count cap, per-column name/type/PII-gate/default/
    user_ref-nullability checks (in declaration order), duplicate column
    names, then index count/width/unknown-column checks.
    """
    if provider not in ("builtin", "thirdparty"):
        raise TableDeclarationError(
            REASON_INVALID_PROVIDER, f"provider {provider!r} is not builtin/thirdparty"
        )

    raw_columns = raw.get("columns")
    if not isinstance(raw_columns, list | tuple) or not raw_columns:
        raise TableDeclarationError(
            REASON_NO_COLUMNS, "data.table.columns must declare at least one column"
        )
    if len(raw_columns) > MAX_COLUMNS:
        raise TableDeclarationError(
            REASON_TOO_MANY_COLUMNS, f"{len(raw_columns)} columns exceeds the cap of {MAX_COLUMNS}"
        )

    columns = []
    seen_names: set[str] = set()
    for raw_column in raw_columns:
        column = _parse_column(raw_column)
        if column.name in seen_names:
            raise TableDeclarationError(
                REASON_DUPLICATE_COLUMN, f"column {column.name!r} declared twice"
            )
        seen_names.add(column.name)
        columns.append(column)

    raw_indexes = raw.get("indexes", ())
    if len(raw_indexes) > MAX_INDEXES:
        raise TableDeclarationError(
            REASON_TOO_MANY_INDEXES, f"{len(raw_indexes)} indexes exceeds the cap of {MAX_INDEXES}"
        )
    declared_names = frozenset(seen_names)
    indexes = tuple(_parse_index(raw_index, declared_names) for raw_index in raw_indexes)

    return TableDeclaration(columns=tuple(columns), indexes=indexes)


# ---------------------------------------------------------------------------
# Table identity (§3.4 -- server-side schema selection + identifier safety)
# ---------------------------------------------------------------------------


APP_CORE_SCHEMA = "app_core"
APP_COMMUNITY_SCHEMA = "app_community"

_SANITIZED_CHARSET_RE = re.compile(r"\A[a-z0-9_]+\Z")


@dataclass(slots=True, frozen=True)
class TableIdentity:
    """The resolved, authoritative `(schema, table)` pair for one bundle app.

    Computed once at approval time (§3.4: "computed once at approval,
    stored ... authoritative -- the data plane looks it up, never
    re-derives it") -- callers persist this, they never recompute it from
    `app_id` at request time.
    """

    schema: str
    table: str


def derive_table_identity(app_id: str, *, provider: str) -> TableIdentity:
    """Derive the authoritative schema+table name for a bundle's data table.

    Schema is chosen **server-side from `provider`**, never from anything
    the manifest itself claims (§3.4) -- `builtin` (first-party
    `waddles.core.*`) -> `app_core`, everything else -> `app_community`.

    Table name is `app_id`, lower-cased with `.` mapped to `_`; any other
    character outside `[a-z0-9_]` is **rejected**, not silently dropped.
    If the result would exceed Postgres's 63-byte `NAMEDATALEN`, it is
    truncated and given an 8-hex-char `sha256(app_id)` suffix so two
    truncated names can never collide on their shared prefix alone.
    """
    if provider not in ("builtin", "thirdparty"):
        raise TableDeclarationError(
            REASON_INVALID_PROVIDER, f"provider {provider!r} is not builtin/thirdparty"
        )
    if not isinstance(app_id, str) or not app_id:
        raise TableDeclarationError(
            REASON_INVALID_TABLE_IDENTITY, "app_id must be a non-empty string"
        )

    schema = APP_CORE_SCHEMA if provider == "builtin" else APP_COMMUNITY_SCHEMA

    sanitized = app_id.lower().replace(".", "_")
    if not _SANITIZED_CHARSET_RE.fullmatch(sanitized):
        raise TableDeclarationError(
            REASON_INVALID_TABLE_IDENTITY,
            f"app_id {app_id!r} contains characters outside [a-z0-9_.] after lower-casing",
        )
    if not sanitized[0].isalpha():
        raise TableDeclarationError(
            REASON_INVALID_TABLE_IDENTITY, f"app_id {app_id!r} must start with a letter"
        )

    if len(sanitized) <= POSTGRES_IDENTIFIER_MAX_LEN:
        table = sanitized
    else:
        digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:TABLE_NAME_HASH_SUFFIX_LEN]
        keep = POSTGRES_IDENTIFIER_MAX_LEN - TABLE_NAME_HASH_SUFFIX_LEN - 1  # 1 for the separator
        table = f"{sanitized[:keep]}_{digest}"

    return TableIdentity(schema=schema, table=table)
