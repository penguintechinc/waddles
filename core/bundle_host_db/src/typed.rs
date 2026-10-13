//! Typed-column handling for [`crate::backend`]: how a declared
//! `uuid`/`timestamptz`/`jsonb` (and NULL-in-any-type) column value is
//! **bound** on a write and **projected** on a read.
//!
//! **Why this module exists.** SeaORM/sqlx send every guest value as the
//! Rust type it was converted to -- `DbValue::Text` becomes a `text`
//! parameter. Postgres has no *assignment* cast from `text` to `uuid`/
//! `timestamptz`/`jsonb`, so a bare `INSERT ... VALUES ($1)` into such a
//! column fails with "column x is of type uuid but expression is of type
//! text" -- and a NULL (`Value::String(None)` is a `text` NULL) fails the
//! same way for `int`/`bool` columns. On the way out, sqlx refuses to decode
//! a `timestamptz`/`jsonb` column into a `String`. The `waddles_bundle_runtime`
//! integration tests used to touch only bool/int/text columns, so none of
//! this surfaced until a bundle declared a typed column.
//!
//! **The fix, in one place:**
//! * **Write** -- [`bind_value`] picks the bind type from the *declared*
//!   column type (never from the guest value) and [`param_cast`] emits the
//!   matching explicit SQL cast (`$n::uuid`, `$n::timestamptz`,
//!   `$n::jsonb`, `$n::numeric`), so the statement is explicit about types
//!   regardless of how the parameter happens to be typed on the wire. Only
//!   typed columns are cast -- a `text`/`int`/`bool` parameter is never
//!   wrapped.
//! * **Read** -- [`select_expr`] projects `timestamptz` as an RFC 3339 UTC
//!   string and `jsonb`/`numeric` as text *in SQL*, so the host decodes a
//!   plain `String` and the form is independent of the connection's
//!   `TimeZone`/`DateStyle`. `uuid` decodes natively (it always could).
//!
//! **`numeric(p,s)`** is an exact decimal, so it is written as a decimal
//! string ([`validate_numeric_text`]) or an integer
//! ([`validate_numeric_int`]) -- never a float, whose binary rounding would
//! silently corrupt a decimal -- and read back as its exact decimal text.
//!
//! Malformed values fail loud as [`DbError::InvalidValue`] -- validated
//! host-side first ([`validate_timestamptz`], plus the existing uuid/jsonb
//! checks in `backend`), and, for anything Postgres itself still refuses
//! (SQLSTATE class 22), mapped by [`map_write_error`] -- never silently
//! coerced to NULL and never misreported as a `backend` outage.

use sea_orm::error::RuntimeErr;
use sea_orm::{DbErr, Value};
use uuid::Uuid;

use crate::backend::{db_value_to_sea_value, DbError, DbValue};
use crate::limits::MAX_NUMERIC_TEXT_BYTES;
use crate::schema::{ColumnDef, ColumnType};
use crate::scope::quote_ident;

/// `to_char` pattern rendering a UTC `timestamp` as RFC 3339 with
/// microsecond precision (`2026-01-02T01:04:05.123456Z`) -- `timestamptz`
/// stores microseconds, so this is lossless, and fixed-width so it sorts
/// chronologically as text too.
const TIMESTAMPTZ_OUT_FORMAT: &str = r#"YYYY-MM-DD"T"HH24:MI:SS.US"Z""#;

/// The Postgres type a column actually has: a `user_ref` column is always a
/// `uuid` in the provisioned DDL (`hub_api/services/bundle_data_ddl.py`
/// `_column_sql_type`), whatever `sql_type` the cached schema carries for
/// it, so the flag wins over the declared type.
pub(crate) fn effective_type(col: &ColumnDef) -> ColumnType {
    if col.is_user_ref {
        ColumnType::Uuid
    } else {
        col.sql_type
    }
}

/// The explicit SQL cast suffix for a write placeholder (`$n` + this):
/// non-empty only for the types Postgres will not assign from the bound
/// parameter type (`text`, or `bigint` for an integer written to a
/// `numeric`). A `numeric` cast is the unconstrained `::numeric`: the
/// column's own `numeric(p,s)` typmod applies on assignment, and
/// [`validate_numeric_text`]/[`validate_numeric_int`] already guarantee the
/// value fits it exactly (no silent rounding, no overflow).
pub(crate) fn param_cast(col: &ColumnDef) -> &'static str {
    match effective_type(col) {
        ColumnType::Uuid => "::uuid",
        ColumnType::Timestamptz => "::timestamptz",
        ColumnType::Jsonb => "::jsonb",
        ColumnType::Numeric { .. } => "::numeric",
        ColumnType::Int4 | ColumnType::Int8 | ColumnType::Bool | ColumnType::Text => "",
    }
}

/// Converts one already-validated guest value into the SeaORM [`Value`]
/// bound for `col`. A `uuid` is parsed and bound natively (so any spelling
/// the host accepts -- hyphenless, braced, `urn:uuid:` -- reaches Postgres
/// in canonical form); a NULL is typed by the *column* so it assigns into
/// `uuid`/`int`/`bool` columns instead of arriving as an untyped-`text`
/// NULL. Everything else keeps its plain conversion, with the SQL cast
/// ([`param_cast`]) doing the rest.
pub(crate) fn bind_value(col: &ColumnDef, value: &DbValue) -> Result<Value, DbError> {
    Ok(match (effective_type(col), value) {
        (ColumnType::Uuid, DbValue::Null) => Value::Uuid(None),
        (ColumnType::Int4 | ColumnType::Int8, DbValue::Null) => Value::BigInt(None),
        (ColumnType::Bool, DbValue::Null) => Value::Bool(None),
        (ColumnType::Uuid, DbValue::Text(s)) => {
            let parsed = Uuid::parse_str(s).map_err(|_| {
                DbError::InvalidValue(format!("{:?} is not a valid UUID", col.name))
            })?;
            Value::Uuid(Some(parsed))
        }
        _ => db_value_to_sea_value(value),
    })
}

/// The `SELECT`-list expression that reads declared column `col` back in
/// the form [`crate::backend`] decodes (always aliased to the column's own
/// name): `timestamptz` as an RFC 3339 UTC string, `jsonb` as JSON text,
/// everything else as the bare column.
///
/// A non-finite `timestamptz` (`infinity`) has no `to_char` rendering --
/// it would silently read as NULL -- so it is passed through as its text
/// form instead (loud, not lossy).
///
/// Because the alias shadows the column name, `ORDER BY` must reference the
/// qualified `"table"."column"` (see `backend::order_by_sql`) to sort by the
/// stored value rather than this projection.
pub(crate) fn select_expr(col: &ColumnDef) -> String {
    let ident = quote_ident(&col.name);
    match effective_type(col) {
        ColumnType::Timestamptz => format!(
            "CASE WHEN isfinite({ident}) \
             THEN to_char({ident} AT TIME ZONE 'UTC', '{TIMESTAMPTZ_OUT_FORMAT}') \
             ELSE {ident}::text END AS {ident}"
        ),
        // `numeric` reads as its exact decimal text (scale digits kept,
        // e.g. `1.50`), never a float: nothing is lost on the way out.
        ColumnType::Jsonb | ColumnType::Numeric { .. } => format!("{ident}::text AS {ident}"),
        ColumnType::Uuid
        | ColumnType::Int4
        | ColumnType::Int8
        | ColumnType::Bool
        | ColumnType::Text => ident,
    }
}

/// Reads exactly `N` ASCII digits at `start` as a number, `None` on any
/// short read or non-digit.
fn digits(bytes: &[u8], start: usize, n: usize) -> Option<u32> {
    let slice = bytes.get(start..start.checked_add(n)?)?;
    slice.iter().try_fold(0u32, |acc, &b| {
        b.is_ascii_digit().then(|| acc * 10 + u32::from(b - b'0'))
    })
}

/// Days in `month` of `year` (Gregorian), `month` already known to be 1..=12.
fn days_in_month(year: u32, month: u32) -> u32 {
    match month {
        2 if year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400)) => {
            29
        }
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// Strict RFC 3339 check for a `timestamptz` value: `YYYY-MM-DD`, `T` (or a
/// space), `HH:MM:SS`, an optional `.fraction` of 1-9 digits, and a
/// **mandatory** `Z` or `+HH:MM`/`-HH:MM` offset, every field range-checked
/// (real month lengths and leap years, no leap second, no year 0). Returns
/// a static reason on failure so the caller can name the column.
///
/// Deliberately narrower than what Postgres will parse: `now`, `today`,
/// `infinity`, a bare date, and an offset-less timestamp are all rejected --
/// the latter two would otherwise be silently interpreted in whatever
/// `TimeZone` the pooled connection happens to carry. Anything this accepts,
/// Postgres's `::timestamptz` cast accepts too.
pub(crate) fn validate_timestamptz(s: &str) -> Result<(), &'static str> {
    const EXPECTED: &str = "must be an RFC 3339 timestamp with an explicit UTC offset \
                            (e.g. 2026-01-02T03:04:05Z)";
    let b = s.as_bytes();
    let fields = (
        digits(b, 0, 4),
        digits(b, 5, 2),
        digits(b, 8, 2),
        digits(b, 11, 2),
        digits(b, 14, 2),
        digits(b, 17, 2),
    );
    let (Some(year), Some(month), Some(day), Some(hour), Some(minute), Some(second)) = fields
    else {
        return Err(EXPECTED);
    };
    let separators_ok = b.get(4) == Some(&b'-')
        && b.get(7) == Some(&b'-')
        && matches!(b.get(10), Some(b'T' | b' '))
        && b.get(13) == Some(&b':')
        && b.get(16) == Some(&b':');
    if !separators_ok {
        return Err(EXPECTED);
    }
    if year == 0
        || !(1..=12).contains(&month)
        || day == 0
        || day > days_in_month(year, month)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return Err("has a date or time field out of range");
    }

    let mut i = 19;
    if b.get(i) == Some(&b'.') {
        let start = i + 1;
        i = start;
        while b.get(i).is_some_and(u8::is_ascii_digit) {
            i += 1;
        }
        if !(1..=9).contains(&(i - start)) {
            return Err(EXPECTED);
        }
    }
    match b.get(i) {
        Some(b'Z') => i += 1,
        Some(b'+' | b'-') => {
            let (Some(off_hour), Some(off_minute)) = (digits(b, i + 1, 2), digits(b, i + 4, 2))
            else {
                return Err(EXPECTED);
            };
            if b.get(i + 3) != Some(&b':') {
                return Err(EXPECTED);
            }
            if off_hour > 23 || off_minute > 59 {
                return Err("has a UTC offset out of range");
            }
            i += 6;
        }
        _ => return Err(EXPECTED),
    }
    if i == b.len() {
        Ok(())
    } else {
        Err(EXPECTED)
    }
}

/// Checks a decimal string written to a `numeric(precision, scale)` column:
/// an optional sign, digits with an optional single `.` (`12`, `-1.50`,
/// `.5`, `5.`), and nothing else -- no exponent, whitespace, `NaN` or
/// `Infinity`. It must also fit the column **exactly**: more significant
/// fractional digits than `scale`, or more integer digits than
/// `precision - scale`, is rejected rather than silently rounded (Postgres
/// would round the former and only error on the latter). Zero padding is
/// harmless and ignored (`1.50` fits `numeric(5,1)`).
///
/// Anything this accepts, Postgres's `::numeric` cast accepts too.
pub(crate) fn validate_numeric_text(s: &str, precision: u8, scale: u8) -> Result<(), &'static str> {
    const EXPECTED: &str = "must be a plain decimal number (e.g. -12.50)";
    let bytes = s.as_bytes();
    if bytes.len() > MAX_NUMERIC_TEXT_BYTES {
        return Err("is too long to be a decimal number");
    }
    let unsigned = match bytes.first() {
        Some(b'+' | b'-') => &bytes[1..],
        _ => bytes,
    };
    let (int_part, frac_part) = match unsigned.iter().position(|&b| b == b'.') {
        Some(dot) => (&unsigned[..dot], &unsigned[dot + 1..]),
        None => (unsigned, &[][..]),
    };
    if int_part.is_empty() && frac_part.is_empty() {
        return Err(EXPECTED);
    }
    if !int_part.iter().chain(frac_part).all(u8::is_ascii_digit) {
        return Err(EXPECTED);
    }
    let int_digits = int_part.len() - int_part.iter().take_while(|&&b| b == b'0').count();
    let frac_digits = frac_part.len() - frac_part.iter().rev().take_while(|&&b| b == b'0').count();
    if frac_digits > usize::from(scale) {
        return Err("has more fractional digits than the column's scale allows");
    }
    if int_digits > usize::from(precision.saturating_sub(scale)) {
        return Err("has more integer digits than the column's precision allows");
    }
    Ok(())
}

/// Checks an integer written to a `numeric(precision, scale)` column: its
/// digit count must fit the `precision - scale` integer digits the column
/// has (`0` has none, so it always fits).
pub(crate) fn validate_numeric_int(
    value: i64,
    precision: u8,
    scale: u8,
) -> Result<(), &'static str> {
    let digits = if value == 0 {
        0
    } else {
        value.unsigned_abs().ilog10() + 1
    };
    if digits > u32::from(precision.saturating_sub(scale)) {
        return Err("has more integer digits than the column's precision allows");
    }
    Ok(())
}

/// Maps a failed `INSERT`/`UPDATE` to the right [`DbError`]: a Postgres
/// **data exception** (SQLSTATE class `22` -- malformed text for the column
/// type, numeric/length overflow such as a value longer than a
/// `varchar(n)`, a jsonb-refused NUL escape, ...) is bad guest input and
/// becomes [`DbError::InvalidValue`]; every other failure stays
/// [`DbError::Backend`] (an actionable ERROR -- the database itself is
/// unhealthy). The raw Postgres message is deliberately *not* surfaced: it
/// can echo the offending value, which may be user data.
pub(crate) fn map_write_error(err: DbErr) -> DbError {
    let sqlstate = match &err {
        DbErr::Exec(RuntimeErr::SqlxError(e)) | DbErr::Query(RuntimeErr::SqlxError(e)) => {
            match e.as_ref() {
                sea_orm::sqlx::Error::Database(db) => db.code().map(|c| c.into_owned()),
                _ => None,
            }
        }
        _ => None,
    };
    match sqlstate {
        Some(code) if code.starts_with("22") => {
            tracing::debug!(sqlstate = %code, "db capability: write rejected as a data exception");
            DbError::InvalidValue(format!(
                "value rejected by the database as malformed or out of range for its column \
                 (sqlstate {code})"
            ))
        }
        _ => DbError::Backend(err.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn col(name: &str, sql_type: ColumnType, is_user_ref: bool) -> ColumnDef {
        ColumnDef {
            name: name.to_string(),
            sql_type,
            nullable: true,
            is_user_ref,
        }
    }

    const ALL_TYPES: [ColumnType; 6] = [
        ColumnType::Uuid,
        ColumnType::Int4,
        ColumnType::Int8,
        ColumnType::Bool,
        ColumnType::Text,
        ColumnType::Timestamptz,
    ];

    #[test]
    fn a_user_ref_column_is_always_a_uuid_whatever_type_the_cache_carries() {
        for t in ALL_TYPES.into_iter().chain([ColumnType::Jsonb]) {
            assert_eq!(effective_type(&col("u", t, true)), ColumnType::Uuid);
            assert_eq!(effective_type(&col("u", t, false)), t);
        }
    }

    #[test]
    fn only_typed_columns_get_a_cast_and_it_matches_the_column_type() {
        let cast = |t, user_ref| param_cast(&col("c", t, user_ref));
        assert_eq!(cast(ColumnType::Uuid, false), "::uuid");
        assert_eq!(cast(ColumnType::Uuid, true), "::uuid");
        assert_eq!(cast(ColumnType::Text, true), "::uuid", "user_ref is a uuid");
        assert_eq!(cast(ColumnType::Timestamptz, false), "::timestamptz");
        assert_eq!(cast(ColumnType::Jsonb, false), "::jsonb");
        for t in [
            ColumnType::Int4,
            ColumnType::Int8,
            ColumnType::Bool,
            ColumnType::Text,
        ] {
            assert_eq!(cast(t, false), "", "{t:?} must not be cast");
        }
    }

    #[test]
    fn a_uuid_is_bound_natively_in_canonical_form_for_every_spelling() {
        let canonical = Uuid::parse_str("9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d").unwrap();
        for spelling in [
            "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d",
            "9A1B2C3D-4E5F-4A6B-8C7D-0E1F2A3B4C5D",
            "9a1b2c3d4e5f4a6b8c7d0e1f2a3b4c5d",
            "{9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d}",
            "urn:uuid:9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d",
        ] {
            assert_eq!(
                bind_value(
                    &col("c", ColumnType::Uuid, false),
                    &DbValue::Text(spelling.into())
                )
                .unwrap(),
                Value::Uuid(Some(canonical)),
                "{spelling}"
            );
        }
        // user_ref binds the same way even if its cached sql_type is wrong.
        assert_eq!(
            bind_value(
                &col("c", ColumnType::Text, true),
                &DbValue::Text(canonical.to_string())
            )
            .unwrap(),
            Value::Uuid(Some(canonical))
        );
    }

    #[test]
    fn a_malformed_uuid_fails_loud_instead_of_binding_text() {
        let err = bind_value(
            &col("c", ColumnType::Uuid, false),
            &DbValue::Text("nope".into()),
        )
        .unwrap_err();
        assert_eq!(err.code(), "invalid_value");
    }

    #[test]
    fn null_is_typed_by_the_column_not_left_as_a_text_null() {
        let null = |t, user_ref| bind_value(&col("c", t, user_ref), &DbValue::Null).unwrap();
        assert_eq!(null(ColumnType::Uuid, false), Value::Uuid(None));
        assert_eq!(null(ColumnType::Text, true), Value::Uuid(None));
        assert_eq!(null(ColumnType::Int4, false), Value::BigInt(None));
        assert_eq!(null(ColumnType::Int8, false), Value::BigInt(None));
        assert_eq!(null(ColumnType::Bool, false), Value::Bool(None));
        for t in [ColumnType::Text, ColumnType::Timestamptz, ColumnType::Jsonb] {
            assert_eq!(null(t, false), Value::String(None), "{t:?}");
        }
    }

    #[test]
    fn non_uuid_values_keep_their_plain_conversion() {
        let v = |t, val: DbValue| bind_value(&col("c", t, false), &val).unwrap();
        assert_eq!(v(ColumnType::Int8, DbValue::Int(7)), Value::BigInt(Some(7)));
        assert_eq!(
            v(ColumnType::Bool, DbValue::Bool(true)),
            Value::Bool(Some(true))
        );
        assert_eq!(
            v(ColumnType::Text, DbValue::Text("t".into())),
            Value::String(Some("t".into()))
        );
        assert_eq!(
            v(
                ColumnType::Timestamptz,
                DbValue::Text("2026-01-02T03:04:05Z".into())
            ),
            Value::String(Some("2026-01-02T03:04:05Z".into()))
        );
        assert_eq!(
            v(ColumnType::Jsonb, DbValue::Text("{}".into())),
            Value::String(Some("{}".into()))
        );
    }

    #[test]
    fn select_expr_projects_timestamptz_and_jsonb_as_text_and_leaves_the_rest_bare() {
        for t in ALL_TYPES {
            if t == ColumnType::Timestamptz {
                continue;
            }
            assert_eq!(select_expr(&col("c", t, false)), "\"c\"", "{t:?}");
        }
        assert_eq!(select_expr(&col("c", ColumnType::Text, true)), "\"c\"");
        assert_eq!(
            select_expr(&col("doc", ColumnType::Jsonb, false)),
            "\"doc\"::text AS \"doc\""
        );
        let ts = select_expr(&col("seen_at", ColumnType::Timestamptz, false));
        assert!(ts.ends_with("AS \"seen_at\""), "{ts}");
        assert!(
            ts.contains("AT TIME ZONE 'UTC'"),
            "UTC, not session TimeZone: {ts}"
        );
        assert!(
            ts.contains("isfinite(\"seen_at\")"),
            "infinity must not read as NULL: {ts}"
        );
        assert!(
            ts.contains(r#"'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'"#),
            "RFC 3339 pattern: {ts}"
        );
    }

    const NUMERIC_10_2: ColumnType = ColumnType::Numeric {
        precision: 10,
        scale: 2,
    };

    #[test]
    fn numeric_is_cast_bound_plainly_and_read_back_as_exact_text() {
        let c = col("amt", NUMERIC_10_2, false);
        assert_eq!(param_cast(&c), "::numeric");
        assert_eq!(select_expr(&c), "\"amt\"::text AS \"amt\"");
        // A decimal keeps its exact text, an integer binds as bigint, and
        // NULL is a text NULL the `::numeric` cast then types.
        assert_eq!(
            bind_value(&c, &DbValue::Text("-12.50".into())).unwrap(),
            Value::String(Some("-12.50".into()))
        );
        assert_eq!(
            bind_value(&c, &DbValue::Int(7)).unwrap(),
            Value::BigInt(Some(7))
        );
        assert_eq!(bind_value(&c, &DbValue::Null).unwrap(), Value::String(None));
        assert_eq!(effective_type(&c), NUMERIC_10_2);
        // user_ref still wins over a numeric cached type.
        assert_eq!(
            effective_type(&col("amt", NUMERIC_10_2, true)),
            ColumnType::Uuid
        );
    }

    #[test]
    fn numeric_text_that_fits_the_column_exactly_is_accepted() {
        for ok in [
            "0",
            "12",
            "-12",
            "+12",
            "12.5",
            "12.50",
            "12.500", // trailing zeros are not significant
            ".5",
            "5.",
            "-0.01",
            "00012.34", // leading zeros are not significant
            "99999999.99",
            "-99999999.9900",
        ] {
            assert_eq!(validate_numeric_text(ok, 10, 2), Ok(()), "{ok:?}");
        }
        // Edge shapes of the (precision, scale) pair itself.
        assert_eq!(validate_numeric_text("0.12345", 5, 5), Ok(()));
        assert_eq!(validate_numeric_text("0", 5, 5), Ok(()));
        assert_eq!(validate_numeric_text("999", 3, 0), Ok(()));
        assert_eq!(validate_numeric_text("1.0", 3, 0), Ok(()));
    }

    #[test]
    fn numeric_text_that_is_not_a_plain_decimal_is_rejected() {
        for bad in [
            "",
            "-",
            "+",
            ".",
            "-.",
            "1.2.3",
            "1e5",
            "1E5",
            "NaN",
            "Infinity",
            "-Infinity",
            " 1",
            "1 ",
            "1,5",
            "--1",
            "+-1",
            "0x10",
            "1_000",
            "\u{0661}\u{0662}",
            "1.5f",
        ] {
            let err = validate_numeric_text(bad, 10, 2).unwrap_err();
            assert!(err.contains("plain decimal"), "{bad:?} -> {err}");
        }
    }

    #[test]
    fn numeric_text_is_rejected_rather_than_rounded_or_overflowed() {
        for (value, precision, scale, reason) in [
            ("1.234", 10, 2, "fractional"),
            ("0.001", 10, 2, "fractional"),
            ("-12.505", 10, 2, "fractional"),
            ("1.5", 3, 0, "fractional"),
            ("100000000", 10, 2, "integer"),
            ("-100000000.00", 10, 2, "integer"),
            ("123456789.1", 10, 2, "integer"),
            ("1000", 3, 0, "integer"),
            ("1", 5, 5, "integer"),
            ("1.00000", 5, 5, "integer"),
        ] {
            let err = validate_numeric_text(value, precision, scale).unwrap_err();
            assert!(
                err.contains(reason),
                "{value:?} in numeric({precision},{scale}) -> {err}"
            );
        }
        let too_long = "0".repeat(MAX_NUMERIC_TEXT_BYTES + 1);
        assert!(validate_numeric_text(&too_long, 10, 2)
            .unwrap_err()
            .contains("too long"));
        // Zero padding up to the cap is still just zero.
        assert_eq!(
            validate_numeric_text(&"0".repeat(MAX_NUMERIC_TEXT_BYTES), 10, 2),
            Ok(())
        );
    }

    #[test]
    fn numeric_int_must_fit_the_integer_digits_of_the_column() {
        for ok in [0, 1, -1, 99_999_999, -99_999_999] {
            assert_eq!(validate_numeric_int(ok, 10, 2), Ok(()), "{ok}");
        }
        for bad in [100_000_000, -100_000_000, i64::MAX, i64::MIN] {
            assert!(validate_numeric_int(bad, 10, 2).is_err(), "{bad}");
        }
        assert_eq!(
            validate_numeric_int(0, 5, 5),
            Ok(()),
            "zero has no integer digits"
        );
        assert!(validate_numeric_int(1, 5, 5).is_err());
        assert_eq!(validate_numeric_int(i64::MAX, 38, 0), Ok(()));
        assert_eq!(validate_numeric_int(i64::MIN, 38, 0), Ok(()));
    }

    #[test]
    fn rfc3339_timestamps_with_an_explicit_offset_are_accepted() {
        for ok in [
            "2026-01-02T03:04:05Z",
            "2026-01-02T03:04:05.1Z",
            "2026-01-02T03:04:05.123456789Z",
            "2026-01-02T03:04:05+00:00",
            "2026-01-02T03:04:05.5-05:30",
            "2026-01-02T03:04:05+23:59",
            "2026-01-02 03:04:05Z",
            "2024-02-29T00:00:00Z",
            "2000-02-29T23:59:59Z",
            "0001-01-01T00:00:00Z",
            "9999-12-31T23:59:59Z",
        ] {
            assert_eq!(validate_timestamptz(ok), Ok(()), "{ok}");
        }
    }

    #[test]
    fn timestamps_that_postgres_would_guess_at_are_rejected() {
        for bad in [
            "",
            "now",
            "today",
            "infinity",
            "-infinity",
            "epoch",
            "2026-01-02",
            "2026-01-02T03:04:05",
            "2026-01-02T03:04",
            "2026-01-02T03:04:05.Z",
            "2026-01-02T03:04:05.1234567890Z",
            "2026-01-02T03:04:05z",
            "2026-01-02t03:04:05Z",
            "2026-01-02T03:04:05+0000",
            "2026-01-02T03:04:05+00",
            "2026-01-02T03:04:05 Z",
            "2026-01-02T03:04:05Zjunk",
            "2026-01-02T03:04:05+00:00:00",
            "26-01-02T03:04:05Z",
            "2026/01/02T03:04:05Z",
            "２０２６-01-02T03:04:05Z",
            "2026-01-02T03:04:05Z\n",
        ] {
            assert!(
                validate_timestamptz(bad).is_err(),
                "{bad:?} must be rejected"
            );
        }
    }

    #[test]
    fn timestamp_fields_are_range_checked() {
        for bad in [
            "0000-01-01T00:00:00Z",
            "2026-00-10T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-01-00T00:00:00Z",
            "2026-01-32T00:00:00Z",
            "2026-04-31T00:00:00Z",
            "2026-02-29T00:00:00Z",
            "1900-02-29T00:00:00Z",
            "2026-01-01T24:00:00Z",
            "2026-01-01T00:60:00Z",
            "2026-01-01T00:00:60Z",
        ] {
            assert_eq!(
                validate_timestamptz(bad),
                Err("has a date or time field out of range"),
                "{bad}"
            );
        }
        for bad in ["2026-01-01T00:00:00+24:00", "2026-01-01T00:00:00-00:60"] {
            assert_eq!(
                validate_timestamptz(bad),
                Err("has a UTC offset out of range"),
                "{bad}"
            );
        }
    }

    /// A stand-in for the Postgres driver error carrying a SQLSTATE, so the
    /// class-22 mapping is covered without a database.
    #[derive(Debug)]
    struct FakePgError {
        sqlstate: Option<&'static str>,
    }

    impl std::fmt::Display for FakePgError {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            write!(f, "fake pg error echoing the secret value")
        }
    }

    impl std::error::Error for FakePgError {}

    impl sea_orm::sqlx::error::DatabaseError for FakePgError {
        fn message(&self) -> &str {
            "fake pg error echoing the secret value"
        }
        fn code(&self) -> Option<std::borrow::Cow<'_, str>> {
            self.sqlstate.map(std::borrow::Cow::Borrowed)
        }
        fn as_error(&self) -> &(dyn std::error::Error + Send + Sync + 'static) {
            self
        }
        fn as_error_mut(&mut self) -> &mut (dyn std::error::Error + Send + Sync + 'static) {
            self
        }
        fn into_error(self: Box<Self>) -> Box<dyn std::error::Error + Send + Sync + 'static> {
            self
        }
        fn kind(&self) -> sea_orm::sqlx::error::ErrorKind {
            sea_orm::sqlx::error::ErrorKind::Other
        }
    }

    fn pg_error(sqlstate: Option<&'static str>) -> sea_orm::sqlx::Error {
        sea_orm::sqlx::Error::Database(Box::new(FakePgError { sqlstate }))
    }

    #[test]
    fn a_class_22_data_exception_is_invalid_value_on_both_exec_and_query_errors() {
        for code in ["22P02", "22007", "22001", "22003", "22P05", "22021"] {
            for wrap in [DbErr::Query, DbErr::Exec] {
                let err = map_write_error(wrap(RuntimeErr::SqlxError(std::sync::Arc::new(
                    pg_error(Some(code)),
                ))));
                assert_eq!(err.code(), "invalid_value", "{code}");
                let message = err.to_string();
                assert!(message.contains(code), "names the sqlstate: {message}");
                assert!(
                    !message.contains("secret value"),
                    "the raw driver message may echo user data and must not be surfaced: {message}"
                );
            }
        }
    }

    #[test]
    fn every_other_database_failure_stays_a_backend_error() {
        // A uniqueness violation, a permission error, a missing table, an
        // admin shutdown, and a driver error carrying no SQLSTATE at all:
        // none is bad guest input, so none may be downgraded to a quiet
        // `invalid_value`.
        for code in [
            Some("23505"),
            Some("42501"),
            Some("42P01"),
            Some("57P01"),
            None,
        ] {
            let err = map_write_error(DbErr::Query(RuntimeErr::SqlxError(std::sync::Arc::new(
                pg_error(code),
            ))));
            assert_eq!(err.code(), "backend", "{code:?}");
        }
    }

    #[test]
    fn non_database_errors_stay_backend_errors() {
        let err = map_write_error(DbErr::Custom("boom".to_string()));
        assert_eq!(err.code(), "backend");
        assert!(err.to_string().contains("boom"));

        let io = sea_orm::sqlx::Error::PoolTimedOut;
        let err = map_write_error(DbErr::Exec(RuntimeErr::SqlxError(std::sync::Arc::new(io))));
        assert_eq!(err.code(), "backend");
    }
}
