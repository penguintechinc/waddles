using System.Text.Json.Serialization;
using WaddleSdk.Json;
using WaddleSdk.Tool;

namespace WaddleSdk.Economy;

/// <summary>Arguments of the economy service's read tools (`points_view`/`get_balance`).</summary>
public sealed record EconomyViewArgs([property: JsonPropertyName("user")] string User);

/// <summary>Result of `points_view`/`get_balance`: the balance plus the community's configured currency alias (e.g. `waddlebucks`).</summary>
public sealed record EconomyViewResult(
    [property: JsonPropertyName("user")] string User,
    [property: JsonPropertyName("balance")] long Balance,
    [property: JsonPropertyName("alias")] string Alias);

/// <summary>
/// Typed client for the platform economy SERVICE bundle (`waddles.core.service.economy`, `bundles/rust/economy`). The WIT
/// `economy` import moves money atomically but cannot tell a bundle what the community CALLS its currency; the service's
/// read tools return that alias. Requires `tool.call:waddles.core.service.economy:points_view` (read) in the manifest and a
/// `stage-next-tools` build.
/// </summary>
public static class EconomyServiceTools
{
    /// <summary>The economy service bundle's app id.</summary>
    public const string Target = "waddles.core.service.economy";

    /// <summary>The economy service's documented default currency alias, used only when the tool call cannot answer.</summary>
    public const string DefaultAlias = "waddlebucks";

    /// <summary>`points_view`: <paramref name="user"/>'s balance and the community currency alias.</summary>
    public static EconomyViewResult PointsView(IToolClient tools, string user) =>
        tools.Invoke(Target, "points_view", new EconomyViewArgs(user), WaddleSdkJsonContext.Default.EconomyViewArgs, WaddleSdkJsonContext.Default.EconomyViewResult);
}
