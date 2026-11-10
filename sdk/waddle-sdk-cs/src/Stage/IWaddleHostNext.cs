using WaddleSdk.Ai;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Media;
using WaddleSdk.Overlay;
using WaddleSdk.Tool;

namespace WaddleSdk.Stage;

/// <summary>
/// <see cref="IWaddleHost"/> plus the capabilities only the newer WIT worlds import: `economy`, `identity`, `overlay`, `ai`,
/// `media` (all of `stage-next`/`stage-v2`) and `tool` (`stage-next-tools`/`stage-tools`). A bundle built on
/// `stage-next-tools` (the world catalog rows may name: see `bundles/build_python_bundles.py` `VALID_WIT_WORLDS`) supplies ONE
/// per-project adapter implementing this; the base interface stays unchanged, so `stage`-world bundles (csping) are untouched.
/// A capability the bundle's manifest did not request fails at call time with the host's `denied` -- never at link time.
/// </summary>
public interface IWaddleHostNext : IWaddleHost
{
    IEconomyClient Economy { get; }
    IIdentityClient Identity { get; }
    IToolClient Tool { get; }
    IOverlayClient Overlay { get; }
    IAiClient Ai { get; }
    IMediaClient Media { get; }
}
