using System.Globalization;
using System.Text;

namespace SuperPenguin.DropGameCore;

/// <summary>
/// Derives the per-invocation game seed. The seed binds the invocation identity (host message id, platform, actor uuid, command)
/// to ONE host-RNG entropy value; that entropy is recorded in the replay record BEFORE the money call, so a redelivery re-derives
/// the very same seed and any settled result can be audited by re-running the simulation from the recorded seed and config.
/// The identity hash is a pure-managed FNV-1a (System.Security.Cryptography is unavailable on wasi-wasm); unpredictability comes
/// from the host entropy, the identity part only guarantees two different invocations never share a seed.
/// </summary>
public static class SeedDerivation
{
    /// <summary>Derives the seed from the invocation identity and the recorded <paramref name="entropy"/>.</summary>
    public static ulong Derive(string messageId, string platform, string actor, string command, ulong entropy)
    {
        var h = 0xcbf29ce484222325UL;
        foreach (var field in new[] { messageId, platform, actor, command })
        {
            var bytes = Encoding.UTF8.GetBytes(field);
            h = Fold(h, (ulong)bytes.Length);
            foreach (var b in bytes)
            {
                h = Fold(h, b);
            }
        }

        return SplitMix64.Mix(h ^ SplitMix64.Mix(entropy));
    }

    /// <summary>Builds one 64-bit entropy value from four 16-bit draws of <paramref name="nextInt"/> (inclusive range; the host RNG by default).</summary>
    public static ulong NewEntropy(Func<int, int, int>? nextInt = null)
    {
        var next = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;
        ulong value = 0;
        for (var i = 0; i < 4; i++)
        {
            value = (value << 16) | (ulong)(uint)(next(0, 0xFFFF) & 0xFFFF);
        }

        return value;
    }

    /// <summary>The fixed-width lower-case hex form of a seed (16 chars), as recorded and published.</summary>
    public static string ToHex(ulong seed) => seed.ToString("x16", CultureInfo.InvariantCulture);

    /// <summary>Parses <see cref="ToHex"/> output; false for anything that is not exactly 16 hex digits.</summary>
    public static bool TryParseHex(string? text, out ulong seed)
    {
        seed = 0;
        return text is { Length: 16 } && ulong.TryParse(text, NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture, out seed);
    }

    private static ulong Fold(ulong h, ulong value) => (h ^ value) * 0x100000001b3UL;
}
