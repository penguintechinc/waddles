using WaddleSdk.Clock;
using WaddleSdk.Context;
using WaddleSdk.Db;
using WaddleSdk.Flags;
using WaddleSdk.Http;
using WaddleSdk.Kv;
using WaddleSdk.Log;
using WaddleSdk.Relay;

namespace WaddleSdk.Stage;

/// <summary>
/// Aggregates every `stage` world capability (`context`/`http`/`kv`/`db`/`relay`/
/// `%flags`/`log`/`clock`) behind this SDK's WIT-independent interfaces. A bundle
/// supplies exactly one small per-project adapter implementing this (typically
/// named `WaddleHostAdapter`) that delegates each member to the generated
/// `I*Imports` static methods -- see the SDK README and
/// `bundles/csharp/superpenguin-roll/WaddleHostAdapter.cs` for a real one.
/// </summary>
public interface IWaddleHost
{
    BundleContextInfo Context { get; }
    IKvClient Kv { get; }
    IDbClient Db { get; }
    IRelayClient Relay { get; }
    IHttpClient Http { get; }
    IFlagsClient Flags { get; }
    ILogClient Log { get; }
    IClockClient Clock { get; }
}
