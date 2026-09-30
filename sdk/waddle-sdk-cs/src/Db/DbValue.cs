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

/// <summary>Idiomatic mirror of `waddle:bundle/db.rows` -- the result of a parameterized `execute`.</summary>
public sealed record DbRows(IReadOnlyList<string> Columns, IReadOnlyList<IReadOnlyList<DbValue>> Rows, ulong RowsAffected);

/// <summary>Mirrors `waddle:bundle/db`'s `error` variant.</summary>
public enum DbErrorKind
{
    Denied,
    Syntax,
    Conflict,
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
