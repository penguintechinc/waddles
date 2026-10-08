namespace WaddleSdk.Db;

/// <summary>Discriminates which field of a <see cref="DbValue"/> is populated.</summary>
public enum DbValueKind
{
    Null,
    Bool,
    Int,
    Float,
    Text,
    Bytes,
}

/// <summary>
/// Idiomatic, WIT-independent mirror of `waddle:bundle/db`'s `value` variant
/// (`wit/waddle-bundle/stage.wit`). Immutable; construct via the static
/// factory methods, read via <see cref="Kind"/> and the matching property.
/// </summary>
public readonly struct DbValue
{
    public DbValueKind Kind { get; }
    public bool BoolValue { get; }
    public long IntValue { get; }
    public double FloatValue { get; }
    public string? TextValue { get; }
    public byte[]? BytesValue { get; }

    private DbValue(DbValueKind kind, bool b = false, long i = 0, double f = 0, string? t = null, byte[]? by = null)
    {
        Kind = kind;
        BoolValue = b;
        IntValue = i;
        FloatValue = f;
        TextValue = t;
        BytesValue = by;
    }

    public static DbValue Null() => new(DbValueKind.Null);
    public static DbValue Of(bool value) => new(DbValueKind.Bool, b: value);
    public static DbValue Of(long value) => new(DbValueKind.Int, i: value);
    public static DbValue Of(double value) => new(DbValueKind.Float, f: value);
    public static DbValue Of(string value) => new(DbValueKind.Text, t: value);
    public static DbValue Of(byte[] value) => new(DbValueKind.Bytes, by: value);
}

/// <summary>One `(column, value)` pair -- mirrors `waddle:bundle/db`'s `column-value` record.</summary>
public sealed record DbColumnValue(string Column, DbValue Value);

/// <summary>
/// One row as returned by the host: mirrors `waddle:bundle/db`'s `row` record.
/// `RowId`/`Version` are the platform-owned identity/optimistic-concurrency
/// columns; `Columns` echoes back exactly the declared-column values.
/// </summary>
public sealed record DbRow(string RowId, ulong Version, IReadOnlyList<DbColumnValue> Columns);

/// <summary>A declared-column (or fixed platform-column) sort key for <see cref="IDbClient.Query"/>.</summary>
public sealed record DbOrderColumn(string Name, bool Descending);

/// <summary>Discriminates which case of <see cref="DbOrderBy"/> is populated.</summary>
public enum DbOrderByKind
{
    Column,
    Random,
}

/// <summary>
/// Idiomatic mirror of `waddle:bundle/db`'s `order-by` variant: either a declared
/// (or fixed platform) column, or `ORDER BY random()`. Omit entirely (pass `null`
/// to <see cref="IDbClient.Query"/>) for the host's own default, stable `row-id`
/// ascending.
/// </summary>
public readonly struct DbOrderBy
{
    public DbOrderByKind Kind { get; }
    public DbOrderColumn? Column { get; }

    private DbOrderBy(DbOrderByKind kind, DbOrderColumn? column)
    {
        Kind = kind;
        Column = column;
    }

    public static DbOrderBy ByColumn(string name, bool descending = false) =>
        new(DbOrderByKind.Column, new DbOrderColumn(name, descending));

    public static DbOrderBy ByRandom() => new(DbOrderByKind.Random, null);
}

/// <summary>
/// Mirrors `waddle:bundle/db`'s `error` variant (structured ops -- the earlier
/// raw-SQL `execute`'s `syntax` case is retired along with it; see
/// `wit/waddle-bundle/stage.wit`'s `db` interface doc).
/// </summary>
public enum DbErrorKind
{
    Denied,
    InvalidColumn,
    InvalidValue,
    NotFound,
    Conflict,
    QuotaExceeded,
    Timeout,
    Backend,
}

/// <summary>
/// Thrown by <see cref="IDbClient"/> implementations for a `db` host-call failure.
/// A bundle's per-project adapter catches the generated `WitException&lt;IDbImports.Error&gt;`
/// and rethrows this instead -- see the SDK README.
/// </summary>
public sealed class WaddleDbException(DbErrorKind kind, string message) : Exception(message)
{
    public DbErrorKind Kind { get; } = kind;
}
