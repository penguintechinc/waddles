namespace WaddleSdk.Db;

/// <summary>
/// Typed wrapper over `waddle:bundle/db` (`wit/waddle-bundle/stage.wit`) --
/// parameterized SQL executed by the stage under the bundle's own Postgres
/// role, restricted to the manifest's `data.tables` and row-level-security
/// scoped to the envelope's tenant/community. Granted only when
/// `data.tables` is non-empty. A bundle's per-project adapter implements
/// this by delegating to the generated `IDbImports.Execute` static method --
/// see the SDK README.
/// </summary>
public interface IDbClient
{
    /// <summary>
    /// Executes <paramref name="statement"/> (with `$1..$n` placeholders) against
    /// <paramref name="parameters"/>. String interpolation of parameters into the
    /// statement text is never valid -- always use placeholders, never concatenation.
    /// </summary>
    DbRows Execute(string statement, IReadOnlyList<DbValue> parameters);
}
