"""Unit coverage for `services/bundle_data_schema.py`.

Every rejection path is asserted by its `reason` code (never message
text), mirroring `flask_core.app_manifest`'s own test convention. Includes
the spec's injection-attempt cases (identifiers and default literals) --
`bundle_data_schema` never builds SQL itself, so "injection" here means
"a payload designed to smuggle a function call/expression/extra statement
past validation and into `bundle_data_ddl`'s input", not a live SQL
execution -- the assertion is that `TableDeclarationError`/rejection
happens, so the payload never reaches the DDL layer at all.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from services.bundle_data_schema import (
    APP_COMMUNITY_SCHEMA,
    APP_CORE_SCHEMA,
    ERASURE_ANONYMIZE,
    ERASURE_DELETE_ROW,
    MAX_COLUMNS,
    MAX_INDEX_WIDTH,
    MAX_INDEXES,
    REASON_COLUMN_NAME_SUGGESTS_PII,
    REASON_DEFAULT_TOO_LONG,
    REASON_DEFAULT_TYPE_MISMATCH,
    REASON_DUPLICATE_COLUMN,
    REASON_INDEX_TOO_WIDE,
    REASON_INDEX_UNKNOWN_COLUMN,
    REASON_INVALID_COLUMN_NAME,
    REASON_INVALID_COLUMN_TYPE,
    REASON_INVALID_DEFAULT_LITERAL,
    REASON_INVALID_TABLE_IDENTITY,
    REASON_JSONB_LENGTH,
    REASON_NO_COLUMNS,
    REASON_NUMERIC_PRECISION,
    REASON_PII_ADJACENT_NOT_NULLABLE,
    REASON_RESERVED_COLUMN_NAME,
    REASON_TEXT_LENGTH,
    REASON_TOO_MANY_COLUMNS,
    REASON_TOO_MANY_INDEXES,
    REASON_USER_REF_NULLABILITY,
    ColumnType,
    LiteralKind,
    TableDeclarationError,
    column_name_suggests_pii,
    derive_table_identity,
    parse_default_literal,
    sanitize_app_id,
    validate_table_declaration,
)


def _minimal_column(name: str = "score", type_: str = "int4", **kwargs: object) -> dict:
    return {"name": name, "type": type_, **kwargs}


class TestParseDefaultLiteral:
    """The strict, closed literal grammar (§3.2 C2.1) -- exactly four shapes."""

    @pytest.mark.parametrize(
        ("raw", "kind", "value"),
        [
            ("NULL", LiteralKind.NULL, None),
            ("null", LiteralKind.NULL, None),
            ("true", LiteralKind.BOOL, True),
            ("FALSE", LiteralKind.BOOL, False),
            ("42", LiteralKind.NUMBER, 42),
            ("-3.5", LiteralKind.NUMBER, -3.5),
            ("'hello'", LiteralKind.STRING, "hello"),
            ("'it''s'", LiteralKind.STRING, "it's"),
            ("''", LiteralKind.STRING, ""),
        ],
    )
    def test_accepts_exactly_four_shapes(self, raw: str, kind: LiteralKind, value: object) -> None:
        parsed = parse_default_literal(raw)
        assert parsed.kind is kind
        assert parsed.value == value

    @pytest.mark.parametrize(
        "raw",
        [
            "now()",
            "gen_random_uuid()",
            "1+1",
            "'abc'::text",
            "CURRENT_TIMESTAMP",
            "unquoted",
            "'unterminated",
            "'; DROP TABLE app_core.foo; --",  # inert once quoted-string-checked
            "1; DROP TABLE app_core.foo",  # bareword/expression, no quotes -- rejected outright
            "'a' || 'b'",
            "(SELECT 1)",
        ],
    )
    def test_rejects_functions_casts_and_expressions(self, raw: str) -> None:
        with pytest.raises(TableDeclarationError) as exc_info:
            parse_default_literal(raw)
        assert exc_info.value.reason == REASON_INVALID_DEFAULT_LITERAL

    def test_sql_injection_payload_inside_a_valid_string_literal_is_inert(self) -> None:
        # A well-formed quoted string containing SQL-looking text is a legal
        # STRING literal -- rejecting it isn't the job of this parser; it's
        # bundle_data_ddl's job to only ever pass it to sql.Literal(), never
        # interpolate it as SQL text. Confirm the parser treats it as inert data.
        parsed = parse_default_literal("'x''; DROP TABLE app_core.foo; --'")
        assert parsed.kind is LiteralKind.STRING
        assert parsed.value == "x'; DROP TABLE app_core.foo; --"


class TestTrailingNewlineRegexAnchorBypass:
    r"""regression: `$` matches before a trailing `\n`, `^` matches after a leading one.

    Every validation regex in this module must be `\A...\Z`-anchored (or use
    `re.fullmatch`), never `^...$` -- otherwise a payload like `"'hello'\n"`
    passes `_STRING_RE` and `raw[1:-1]` silently slices off the closing quote
    instead of the trailing newline, and `"scorex\n"` passes
    `_COLUMN_NAME_RE`. Covers every literal shape,
    column names, app ids, and index-column references.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "NULL\n",
            "\nNULL",
            "NULL\r\n",
            "true\n",
            "\ntrue",
            "true\r\n",
            "42\n",
            "\n42",
            "42\r\n",
            "'hello'\n",
            "\n'hello'",
            "'hello'\r\n",
            "''\n",
        ],
    )
    def test_default_literal_with_embedded_newline_rejected(self, raw: str) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            parse_default_literal(raw)
        assert exc.value.reason == REASON_INVALID_DEFAULT_LITERAL

    @pytest.mark.parametrize(
        "name",
        ["score\n", "\nscore", "score\r\n", "score\r"],
    )
    def test_column_name_with_embedded_newline_rejected(self, name: str) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(name=name)]}, provider="builtin"
            )
        assert exc.value.reason == REASON_INVALID_COLUMN_NAME

    @pytest.mark.parametrize(
        "app_id",
        [
            "penguintechinc.waddles.fishing_core\n",
            "\npenguintechinc.waddles.fishing_core",
            "penguintechinc.waddles.fishing_core\r\n",
        ],
    )
    def test_app_id_with_embedded_newline_never_yields_an_unsafe_identifier(
        self, app_id: str
    ) -> None:
        # Mirrors Rust `sanitized_app_id`: a newline maps to `_`, so the result is either a
        # safe identifier or a reserved-shape rejection -- never a name carrying the newline.
        try:
            identity = derive_table_identity(app_id, provider="builtin")
        except TableDeclarationError as exc:
            assert exc.reason == REASON_INVALID_TABLE_IDENTITY
        else:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", identity.table)

    @pytest.mark.parametrize(
        "col_ref",
        ["kind\n", "\nkind", "kind\r\n"],
    )
    def test_index_column_reference_with_embedded_newline_rejected(self, col_ref: str) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {
                    "columns": [_minimal_column(name="kind", type_="text(8)")],
                    "indexes": [{"columns": [col_ref]}],
                },
                provider="builtin",
            )
        # The newline-suffixed reference never equals the declared "kind"
        # column, so it is rejected as unknown rather than silently matched.
        assert exc.value.reason == REASON_INDEX_UNKNOWN_COLUMN


class TestColumnNameSuggestsPii:
    @pytest.mark.parametrize(
        "name",
        [
            "email",
            "e_mail",
            "user_name",
            "username",
            "display_name",
            "first_name",
            "handle",
            "login",
            "phone_number",
            "home_address",
            "ssn",
            "dob",
            "birth_date",
        ],
    )
    def test_flags_known_pii_shaped_names(self, name: str) -> None:
        assert column_name_suggests_pii(name)

    @pytest.mark.parametrize("name", ["score", "kind", "fish_caught", "fish_type_ref", "payload"])
    def test_does_not_flag_clean_names(self, name: str) -> None:
        assert not column_name_suggests_pii(name)

    def test_documented_false_positive_tradeoff(self) -> None:
        # "ip" substring-matches "skip_count" -- a deliberate, spec-acknowledged
        # tradeoff (§3.7), not a bug. Documented here so a future change to the
        # matching strategy notices it broke this contract either way.
        assert column_name_suggests_pii("skip_count")


class TestValidateTableDeclarationColumns:
    def test_minimal_valid_declaration(self) -> None:
        decl = validate_table_declaration({"columns": [_minimal_column()]}, provider="builtin")
        assert len(decl.columns) == 1
        assert decl.columns[0].type_kind is ColumnType.INT4

    def test_column_lookup_by_name(self) -> None:
        decl = validate_table_declaration(
            {"columns": [_minimal_column(name="score")]}, provider="builtin"
        )
        assert decl.column("score") is not None
        assert decl.column("score").name == "score"
        assert decl.column("missing") is None

    def test_invalid_provider_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration({"columns": [_minimal_column()]}, provider="evil")
        assert exc.value.reason == "invalid_provider"

    def test_no_columns_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration({"columns": []}, provider="builtin")
        assert exc.value.reason == REASON_NO_COLUMNS

    def test_too_many_columns_rejected(self) -> None:
        columns = [_minimal_column(name=f"c{i}") for i in range(MAX_COLUMNS + 1)]
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration({"columns": columns}, provider="builtin")
        assert exc.value.reason == REASON_TOO_MANY_COLUMNS

    def test_exactly_max_columns_accepted(self) -> None:
        columns = [_minimal_column(name=f"c{i}") for i in range(MAX_COLUMNS)]
        decl = validate_table_declaration({"columns": columns}, provider="builtin")
        assert len(decl.columns) == MAX_COLUMNS

    def test_duplicate_column_rejected(self) -> None:
        columns = [_minimal_column(name="dup"), _minimal_column(name="dup")]
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration({"columns": columns}, provider="builtin")
        assert exc.value.reason == REASON_DUPLICATE_COLUMN

    @pytest.mark.parametrize(
        "name",
        ["row_id", "tenant_id", "community_id", "version", "created_at", "updated_at"],
    )
    def test_reserved_platform_column_name_rejected(self, name: str) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(name=name)]}, provider="builtin"
            )
        assert exc.value.reason == REASON_RESERVED_COLUMN_NAME

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "Score",  # uppercase
            "1score",  # leading digit
            "score-name",  # hyphen
            "score name",  # space
            "score;drop",  # injection-shaped identifier attempt
            "score--",
            "score/*x*/",
            "a" * 64,  # too long
        ],
    )
    def test_invalid_column_name_rejected(self, name: str) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(name=name)]}, provider="builtin"
            )
        assert exc.value.reason == REASON_INVALID_COLUMN_NAME

    def test_pii_shaped_column_name_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(name="display_name", type_="text(32)")]},
                provider="builtin",
            )
        assert exc.value.reason == REASON_COLUMN_NAME_SUGGESTS_PII

    @pytest.mark.parametrize(
        "raw_type",
        [
            "float",
            "varchar",
            "text",  # bare, no length
            "numeric",  # bare, no precision/scale
            "jsonb()",
            "int",
            "double precision",
            "text(abc)",
        ],
    )
    def test_invalid_column_type_rejected(self, raw_type: str) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_=raw_type)]}, provider="builtin"
            )
        assert exc.value.reason == REASON_INVALID_COLUMN_TYPE

    def test_missing_column_type_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration({"columns": [{"name": "score"}]}, provider="builtin")
        assert exc.value.reason == REASON_INVALID_COLUMN_TYPE

    def test_numeric_precision_over_cap_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_="numeric(39,2)")]}, provider="builtin"
            )
        assert exc.value.reason == REASON_NUMERIC_PRECISION

    def test_numeric_scale_over_cap_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_="numeric(20,13)")]}, provider="builtin"
            )
        assert exc.value.reason == REASON_NUMERIC_PRECISION

    def test_numeric_scale_exceeding_precision_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_="numeric(4,5)")]}, provider="builtin"
            )
        assert exc.value.reason == REASON_NUMERIC_PRECISION

    def test_text_length_over_cap_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_="text(8193)")]}, provider="builtin"
            )
        assert exc.value.reason == REASON_TEXT_LENGTH

    def test_text_length_zero_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_="text(0)")]}, provider="builtin"
            )
        assert exc.value.reason == REASON_TEXT_LENGTH

    def test_jsonb_bare_defaults_to_16kib_cap(self) -> None:
        decl = validate_table_declaration(
            {"columns": [_minimal_column(name="payload", type_="jsonb")]}, provider="builtin"
        )
        assert decl.columns[0].parsed_type.max_len == 16384

    def test_jsonb_narrower_cap_respected(self) -> None:
        decl = validate_table_declaration(
            {"columns": [_minimal_column(name="metadata", type_="jsonb(4096)")]},
            provider="builtin",
        )
        assert decl.columns[0].parsed_type.max_len == 4096
        assert decl.jsonb_columns[0].name == "metadata"

    def test_jsonb_columns_flagged_for_secondary_review(self) -> None:
        decl = validate_table_declaration(
            {
                "columns": [
                    _minimal_column(name="payload", type_="jsonb"),
                    _minimal_column(name="score"),
                ]
            },
            provider="thirdparty",
        )
        assert [c.name for c in decl.jsonb_columns] == ["payload"]


class TestDefaultLiteralTypeCompatibility:
    def test_null_default_requires_nullable(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(default="NULL", nullable=False)]},
                provider="builtin",
            )
        assert exc.value.reason == REASON_DEFAULT_TYPE_MISMATCH

    def test_null_default_with_nullable_column_accepted(self) -> None:
        decl = validate_table_declaration(
            {"columns": [_minimal_column(default="NULL", nullable=True)]}, provider="builtin"
        )
        assert decl.columns[0].default is not None
        assert decl.columns[0].default.kind is LiteralKind.NULL

    def test_bool_default_on_non_bool_column_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_="int4", default="true")]}, provider="builtin"
            )
        assert exc.value.reason == REASON_DEFAULT_TYPE_MISMATCH

    def test_number_default_on_bool_column_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_="bool", default="1")]}, provider="builtin"
            )
        assert exc.value.reason == REASON_DEFAULT_TYPE_MISMATCH

    def test_string_default_too_long_for_text_column_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_="text(4)", default="'toolong'")]},
                provider="builtin",
            )
        assert exc.value.reason == REASON_DEFAULT_TOO_LONG

    def test_string_default_within_text_length_accepted(self) -> None:
        decl = validate_table_declaration(
            {"columns": [_minimal_column(type_="text(8)", default="'ok'")]}, provider="builtin"
        )
        assert decl.columns[0].default is not None
        assert decl.columns[0].default.value == "ok"

    def test_numeric_default_on_numeric_column_accepted(self) -> None:
        decl = validate_table_declaration(
            {"columns": [_minimal_column(type_="numeric(10,2)", default="1.50")]},
            provider="builtin",
        )
        assert decl.columns[0].default is not None
        assert decl.columns[0].default.value == 1.5

    def test_string_default_on_jsonb_within_cap_accepted(self) -> None:
        decl = validate_table_declaration(
            {"columns": [_minimal_column(name="payload", type_="jsonb(16)", default="'{}'")]},
            provider="builtin",
        )
        assert decl.columns[0].default is not None

    def test_string_default_on_jsonb_exceeding_cap_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(name="payload", type_="jsonb(4)", default="'12345'")]},
                provider="builtin",
            )
        assert exc.value.reason == REASON_DEFAULT_TOO_LONG

    def test_bool_default_on_bool_column_accepted(self) -> None:
        decl = validate_table_declaration(
            {"columns": [_minimal_column(type_="bool", default="true")]}, provider="builtin"
        )
        assert decl.columns[0].default is not None
        assert decl.columns[0].default.value is True

    def test_string_default_on_uuid_column_accepted(self) -> None:
        decl = validate_table_declaration(
            {
                "columns": [
                    _minimal_column(
                        name="ref",
                        type_="uuid",
                        default="'123e4567-e89b-12d3-a456-426614174000'",
                    )
                ]
            },
            provider="builtin",
        )
        assert decl.columns[0].default is not None

    def test_string_default_on_timestamptz_column_accepted(self) -> None:
        decl = validate_table_declaration(
            {
                "columns": [
                    _minimal_column(
                        name="seen_at", type_="timestamptz", default="'2026-01-01T00:00:00Z'"
                    )
                ]
            },
            provider="builtin",
        )
        assert decl.columns[0].default is not None

    def test_string_default_on_int4_column_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_="int4", default="'5'")]}, provider="builtin"
            )
        assert exc.value.reason == REASON_DEFAULT_TYPE_MISMATCH

    def test_jsonb_size_over_cap_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(name="payload", type_="jsonb(20000)")]},
                provider="builtin",
            )
        assert exc.value.reason == REASON_JSONB_LENGTH

    def test_jsonb_size_zero_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(name="payload", type_="jsonb(0)")]},
                provider="builtin",
            )
        assert exc.value.reason == REASON_JSONB_LENGTH


class TestUserRefNullabilityRule:
    """§3.2 C4.2 -- NOT NULL user_ref + anonymize is unsatisfiable, rejected at validation."""

    def test_user_ref_defaults_to_delete_row_and_may_be_not_null(self) -> None:
        decl = validate_table_declaration(
            {"columns": [_minimal_column(name="user_ref", type_="user_ref", nullable=False)]},
            provider="builtin",
        )
        assert decl.columns[0].on_erasure == ERASURE_DELETE_ROW

    def test_user_ref_anonymize_requires_nullable(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {
                    "columns": [
                        _minimal_column(
                            name="user_ref",
                            type_="user_ref",
                            nullable=False,
                            on_erasure=ERASURE_ANONYMIZE,
                        )
                    ]
                },
                provider="builtin",
            )
        assert exc.value.reason == REASON_USER_REF_NULLABILITY

    def test_user_ref_anonymize_with_nullable_accepted(self) -> None:
        decl = validate_table_declaration(
            {
                "columns": [
                    _minimal_column(
                        name="user_ref",
                        type_="user_ref",
                        nullable=True,
                        on_erasure=ERASURE_ANONYMIZE,
                    )
                ]
            },
            provider="builtin",
        )
        assert decl.columns[0].on_erasure == ERASURE_ANONYMIZE

    def test_unknown_erasure_action_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {
                    "columns": [
                        _minimal_column(
                            name="user_ref", type_="user_ref", nullable=True, on_erasure="wipe"
                        )
                    ]
                },
                provider="builtin",
            )
        assert exc.value.reason == REASON_USER_REF_NULLABILITY

    def test_on_erasure_on_non_user_ref_column_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(on_erasure=ERASURE_DELETE_ROW)]}, provider="builtin"
            )
        assert exc.value.reason == REASON_USER_REF_NULLABILITY

    def test_pii_adjacent_not_nullable_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {"columns": [_minimal_column(type_="text(8)", pii_adjacent=True, nullable=False)]},
                provider="builtin",
            )
        assert exc.value.reason == REASON_PII_ADJACENT_NOT_NULLABLE

    def test_pii_adjacent_nullable_accepted(self) -> None:
        decl = validate_table_declaration(
            {"columns": [_minimal_column(type_="text(8)", pii_adjacent=True, nullable=True)]},
            provider="builtin",
        )
        assert decl.columns[0].pii_adjacent is True


class TestIndexes:
    def test_index_with_no_columns_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {
                    "columns": [_minimal_column(name="kind", type_="text(8)")],
                    "indexes": [{"columns": []}],
                },
                provider="builtin",
            )
        assert exc.value.reason == "index_no_columns"

    def test_index_referencing_undeclared_column_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {
                    "columns": [_minimal_column(name="kind", type_="text(8)")],
                    "indexes": [{"columns": ["nonexistent"]}],
                },
                provider="builtin",
            )
        assert exc.value.reason == REASON_INDEX_UNKNOWN_COLUMN

    def test_index_too_wide_rejected(self) -> None:
        columns = [_minimal_column(name=f"c{i}", type_="int4") for i in range(MAX_INDEX_WIDTH + 1)]
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {
                    "columns": columns,
                    "indexes": [{"columns": [c["name"] for c in columns]}],
                },
                provider="builtin",
            )
        assert exc.value.reason == REASON_INDEX_TOO_WIDE

    def test_too_many_indexes_rejected(self) -> None:
        columns = [_minimal_column(name=f"c{i}", type_="int4") for i in range(MAX_INDEXES + 1)]
        indexes = [{"columns": [c["name"]]} for c in columns]
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration({"columns": columns, "indexes": indexes}, provider="builtin")
        assert exc.value.reason == REASON_TOO_MANY_INDEXES

    def test_composite_index_with_direction_accepted(self) -> None:
        decl = validate_table_declaration(
            {
                "columns": [
                    _minimal_column(name="kind", type_="text(32)"),
                    _minimal_column(name="score", type_="int8"),
                ],
                "indexes": [{"columns": ["kind", {"column": "score", "dir": "desc"}]}],
            },
            provider="builtin",
        )
        assert len(decl.indexes) == 1
        assert decl.indexes[0].columns[1].descending is True

    def test_index_column_entry_wrong_shape_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            validate_table_declaration(
                {
                    "columns": [_minimal_column(name="kind", type_="text(8)")],
                    "indexes": [{"columns": [123]}],
                },
                provider="builtin",
            )
        assert exc.value.reason == REASON_INDEX_UNKNOWN_COLUMN


class TestDeriveTableIdentity:
    def test_invalid_provider_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            derive_table_identity("penguintechinc.waddles.quotes", provider="evil")
        assert exc.value.reason == "invalid_provider"

    def test_app_id_not_starting_with_a_letter_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            derive_table_identity("9bad.app", provider="builtin")
        assert exc.value.reason == REASON_INVALID_TABLE_IDENTITY

    def test_builtin_provider_maps_to_app_core_schema(self) -> None:
        identity = derive_table_identity("penguintechinc.waddles.fishing_core", provider="builtin")
        assert identity.schema == APP_CORE_SCHEMA
        assert identity.table == "penguintechinc_waddles_fishing_core"

    def test_thirdparty_provider_maps_to_app_community_schema(self) -> None:
        identity = derive_table_identity(
            "penguintechinc.waddles.superpenguin_fishing_core", provider="thirdparty"
        )
        assert identity.schema == APP_COMMUNITY_SCHEMA

    def test_schema_is_never_taken_from_a_manifest_looking_claim(self) -> None:
        # Even an app_id that *looks* like it's claiming app_core placement
        # doesn't get to choose -- only `provider` decides the schema.
        identity = derive_table_identity("penguintechinc.waddles.app_core", provider="thirdparty")
        assert identity.schema == APP_COMMUNITY_SCHEMA

    def test_long_app_id_is_truncated_with_hash_suffix(self) -> None:
        long_app_id = "penguintechinc.waddles." + ("segment-" * 20) + "app"
        identity = derive_table_identity(long_app_id, provider="builtin")
        assert len(identity.table) <= 63
        assert "_" in identity.table
        digest_suffix = identity.table.rsplit("_", 1)[1]
        assert len(digest_suffix) == 8

    def test_truncated_names_for_different_app_ids_do_not_collide(self) -> None:
        base = "penguintechinc.waddles." + ("segment-" * 20)
        id_a = derive_table_identity(base + "app_a", provider="builtin")
        id_b = derive_table_identity(base + "app_b", provider="builtin")
        assert id_a.table != id_b.table

    @pytest.mark.parametrize(
        "app_id",
        [
            "penguintechinc.waddles.fish'; DROP TABLE app_core.x; --",
            'penguintechinc.waddles.fish"quote',
            "penguintechinc.waddles.fish$$tag$$",
            "penguintechinc.waddles.fish;drop",
            "penguintechinc.waddles.fish/*comment*/",
            "penguintechinc.waddles.fish\x00null",
            "WADDLES.CORE.FISHING.MixedCase!",
        ],
    )
    def test_injection_shaped_app_id_never_yields_an_unsafe_identifier(self, app_id: str) -> None:
        # Every non-[a-z0-9] char becomes `_` (Rust parity), so no injection payload can survive
        # into the identifier; shapes that collapse to the reserved `__` are rejected outright.
        try:
            identity = derive_table_identity(app_id, provider="builtin")
        except TableDeclarationError as exc:
            assert exc.reason == REASON_INVALID_TABLE_IDENTITY
        else:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", identity.table)
            assert len(identity.table) <= 63

    @pytest.mark.parametrize(
        ("app_id", "expected"),
        [
            ("penguintechinc.waddles.discord-invite", "penguintechinc_waddles_discord_invite"),
            ("penguintechinc.waddles.live-now", "penguintechinc_waddles_live_now"),
            ("psychoboy.penguintwitchbot.roll", "psychoboy_penguintwitchbot_roll"),
            ("Acme-Corp.Widgets.X1", "acme_corp_widgets_x1"),
        ],
    )
    def test_hyphens_map_to_underscore_like_rust(self, app_id: str, expected: str) -> None:
        """Parity with Rust `sanitized_app_id`: hyphens become underscores."""
        assert sanitize_app_id(app_id) == expected
        identity = derive_table_identity(app_id, provider="builtin")
        assert identity.table == expected
        assert re.fullmatch(r"[a-z][a-z0-9_]*", identity.table)

    def test_non_ascii_and_every_non_alnum_char_map_to_underscore(self) -> None:
        # Rust maps per `char`: one `_` per non-ASCII-alphanumeric char (here U+00FF and '-').
        assert sanitize_app_id("a.b-c\u00ffd") == "a_b_c_d"
        assert sanitize_app_id("A.Z0") == "a_z0"

    def test_exactly_63_chars_is_not_truncated(self) -> None:
        app_id = "a" + "b" * 62
        assert len(app_id) == 63
        assert derive_table_identity(app_id, provider="builtin").table == app_id

    def test_64_chars_is_truncated_to_63_with_underscore_plus_8_hex_suffix(self) -> None:
        app_id = "a" + "b" * 63
        table = derive_table_identity(app_id, provider="builtin").table
        digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:8]
        assert len(table) == 63
        assert table == "a" + "b" * 53 + "_" + digest

    def test_hyphenated_long_id_truncation_matches_rust_layout(self) -> None:
        app_id = "penguintechinc.waddles." + "very-long-bundle-name-" * 4 + "x"
        table = derive_table_identity(app_id, provider="builtin").table
        sanitized = sanitize_app_id(app_id)
        digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:8]
        assert len(sanitized) > 63
        assert table == sanitized[:54] + "_" + digest
        assert len(table) == 63
        assert "-" not in table

    def test_empty_app_id_rejected(self) -> None:
        with pytest.raises(TableDeclarationError) as exc:
            derive_table_identity("", provider="builtin")
        assert exc.value.reason == REASON_INVALID_TABLE_IDENTITY
