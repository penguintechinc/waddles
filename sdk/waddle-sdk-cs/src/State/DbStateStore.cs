using WaddleSdk.Clock;
using WaddleSdk.Db;

namespace WaddleSdk.State;

/// <summary>
/// <see cref="IStateStore"/> over the bundle's single `db` table, which must declare columns
/// <c>k</c> (text, the key), <c>v</c> (bytes, the value) and <c>exp</c> (int, expiry in epoch ms; 0 = none).
///
/// The `db` interface has no column filter, so a lookup scans the table in pages of 200 rows up to
/// <see cref="MaxScanRows"/>: use this for SMALL state (pots, join windows, a handful of live games), not a per-user table.
/// Expired rows met during a scan are deleted (lazy expiry). <see cref="Mutate"/> is a real compare-and-swap on the row
/// version; <see cref="TryClaim"/> arbitrates concurrent inserts by earliest row id.
/// </summary>
public sealed class DbStateStore(IDbClient db, IClockClient clock, int maxScanRows = 1000) : IStateStore
{
    private const uint PageSize = 200;
    private const string KeyColumn = "k";
    private const string ValueColumn = "v";
    private const string ExpiryColumn = "exp";

    /// <summary>Upper bound on rows examined per lookup.</summary>
    public int MaxScanRows { get; } = maxScanRows;

    public bool AtomicMutate => true;

    public byte[]? Get(string key) => Find(key)?.Value;

    public void Set(string key, byte[] value, uint ttlSeconds)
    {
        var found = Find(key);
        var columns = Columns(key, value, ttlSeconds);
        if (found is null)
        {
            db.Insert(columns);
        }
        else
        {
            db.Update(found.RowId, found.Version, columns);
        }
    }

    public void Delete(string key)
    {
        var found = Find(key);
        if (found is not null)
        {
            TryDelete(found.RowId, found.Version);
        }
    }

    public bool TryClaim(string key, uint ttlSeconds)
    {
        if (Find(key) is not null)
        {
            return false;
        }

        var mine = db.Insert(Columns(key, [1], ttlSeconds));
        var winner = Scan(key, out _);
        if (winner is not null && winner.RowId == mine.RowId)
        {
            return true;
        }

        TryDelete(mine.RowId, mine.Version);
        return false;
    }

    public bool Mutate(string key, Func<byte[]?, byte[]?> mutate, uint ttlSeconds, int maxAttempts = 5)
    {
        for (var attempt = 0; attempt < maxAttempts; attempt++)
        {
            var found = Find(key);
            var next = mutate(found?.Value);
            try
            {
                if (found is null)
                {
                    if (next is not null)
                    {
                        db.Insert(Columns(key, next, ttlSeconds));
                    }
                }
                else if (next is null)
                {
                    db.Delete(found.RowId, found.Version);
                }
                else
                {
                    db.Update(found.RowId, found.Version, Columns(key, next, ttlSeconds));
                }

                return true;
            }
            catch (WaddleDbException ex) when (ex.Kind is DbErrorKind.Conflict or DbErrorKind.NotFound)
            {
                // Another event won the row between our read and write; re-read and re-apply.
            }
        }

        return false;
    }

    private sealed record Found(string RowId, ulong Version, byte[] Value);

    private Found? Find(string key)
    {
        var found = Scan(key, out var expired);
        if (expired is not null)
        {
            TryDelete(expired.RowId, expired.Version);
        }

        return found;
    }

    private Found? Scan(string key, out Found? expiredRow)
    {
        expiredRow = null;
        var now = (long)clock.NowMillis();
        for (uint offset = 0; offset < MaxScanRows; offset += PageSize)
        {
            var rows = db.Query(PageSize, offset, null);
            foreach (var row in rows)
            {
                var (k, v, exp) = Read(row);
                if (k != key)
                {
                    continue;
                }

                if (exp != 0 && exp <= now)
                {
                    expiredRow = new Found(row.RowId, row.Version, v);
                    continue;
                }

                return new Found(row.RowId, row.Version, v);
            }

            if (rows.Count < PageSize)
            {
                break;
            }
        }

        return null;
    }

    private static (string Key, byte[] Value, long Exp) Read(DbRow row)
    {
        var key = "";
        byte[] value = [];
        long exp = 0;
        foreach (var column in row.Columns)
        {
            switch (column.Column)
            {
                case KeyColumn when column.Value.Kind == DbValueKind.Text:
                    key = column.Value.TextValue!;
                    break;
                case ValueColumn when column.Value.Kind == DbValueKind.Bytes:
                    value = column.Value.BytesValue!;
                    break;
                case ExpiryColumn when column.Value.Kind == DbValueKind.Int:
                    exp = column.Value.IntValue;
                    break;
            }
        }

        return (key, value, exp);
    }

    private List<DbColumnValue> Columns(string key, byte[] value, uint ttlSeconds) =>
    [
        new(KeyColumn, DbValue.Of(key)),
        new(ValueColumn, DbValue.Of(value)),
        new(ExpiryColumn, DbValue.Of(ttlSeconds == 0 ? 0L : (long)clock.NowMillis() + (long)ttlSeconds * 1000)),
    ];

    private void TryDelete(string rowId, ulong version)
    {
        try
        {
            db.Delete(rowId, version);
        }
        catch (WaddleDbException ex) when (ex.Kind is DbErrorKind.Conflict or DbErrorKind.NotFound)
        {
            // Already gone or rewritten by a concurrent event: nothing to clean up.
        }
    }
}
