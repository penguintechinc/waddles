namespace WaddleSdk.Db;

/// <summary>
/// Typed wrapper over `waddle:bundle/db` (`wit/waddle-bundle/stage.wit`) --
/// structured ops against the bundle's own single table in the shared
/// Postgres instance, enforced host-side under the least-privilege
/// `waddles_bundle_runtime` role; every row is scoped server-side to the
/// invocation's authenticated (tenant, community, app_id). Granted only when
/// `data.tables` is non-empty. **No raw SQL crosses this boundary, ever**
/// (design doc SS1 round-1 CRITICAL finding) -- there is no `table`/
/// `statement` parameter anywhere below, by design: a bundle owns exactly
/// one table, and every op implicitly targets it. A bundle's per-project
/// adapter implements this by delegating to the generated `IDbImports`
/// static methods -- see the SDK README.
/// </summary>
public interface IDbClient
{
    /// <summary>Inserts one row; returns the platform-assigned `RowId`/`Version` alongside the stored columns.</summary>
    DbRow Insert(IReadOnlyList<DbColumnValue> columnValues);

    /// <summary>Fetches one row by its platform `RowId`; throws <see cref="WaddleDbException"/> (<see cref="DbErrorKind.NotFound"/>) if absent.</summary>
    DbRow Get(string rowId);

    /// <summary>
    /// Bounded, orderable list of this bundle's own rows. `limit` is clamped
    /// host-side regardless of what is requested; <paramref name="orderBy"/>
    /// omitted (`null`) defaults to stable `RowId` ascending.
    /// </summary>
    IReadOnlyList<DbRow> Query(uint limit, uint offset, DbOrderBy? orderBy = null);

    /// <summary>
    /// Updates one row, gated on <paramref name="expectedVersion"/> (optimistic
    /// concurrency) -- a mismatch or missing row throws <see cref="WaddleDbException"/>
    /// (<see cref="DbErrorKind.Conflict"/>/<see cref="DbErrorKind.NotFound"/>).
    /// </summary>
    DbRow Update(string rowId, ulong expectedVersion, IReadOnlyList<DbColumnValue> columnValues);

    /// <summary>Deletes one row, gated on <paramref name="expectedVersion"/> the same way as <see cref="Update"/>.</summary>
    void Delete(string rowId, ulong expectedVersion);
}
