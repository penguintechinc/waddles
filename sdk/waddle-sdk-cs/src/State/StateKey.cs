using System.Text;

namespace WaddleSdk.State;

/// <summary>
/// Builds host-`kv`-safe keys: the allowed charset is `[A-Za-z0-9_.-]` (a `:` is a namespace-escape syntax error)
/// and the host caps a key at 256 bytes. Raw identifiers (actor ids, event ids) are hashed so a key is never
/// PII and never malformed, whatever shape the platform id has.
/// </summary>
public static class StateKey
{
    /// <summary>Host-enforced maximum guest key length in bytes.</summary>
    public const int MaxLength = 256;

    /// <summary>Joins already-safe literal <paramref name="parts"/> with `.`; throws if the result is not host-safe.</summary>
    public static string Of(params string[] parts)
    {
        var key = string.Join('.', parts);
        if (key.Length == 0 || key.Length > MaxLength)
        {
            throw new ArgumentException($"state key length {key.Length} outside 1..{MaxLength}", nameof(parts));
        }

        foreach (var c in key)
        {
            if (!(c is >= 'a' and <= 'z' or >= 'A' and <= 'Z' or >= '0' and <= '9' or '_' or '-' or '.'))
            {
                throw new ArgumentException($"state key contains '{c}', allowed: [A-Za-z0-9_.-]", nameof(parts));
            }
        }

        return key;
    }

    /// <summary>
    /// A stable, key-safe 32-hex-char pseudonym of <paramref name="raw"/>: two independent 64-bit FNV-1a passes. Pure managed
    /// code on purpose -- `System.Security.Cryptography` throws PlatformNotSupportedException on `wasi-wasm` (found by the
    /// real-executor e2e). NOT cryptographic: it keeps raw ids out of keys, it is not a secret; keys live in the bundle's own
    /// (tenant, community, app)-scoped kv namespace.
    /// </summary>
    public static string Hash(string raw)
    {
        const ulong prime = 0x100000001b3;
        var h1 = 0xcbf29ce484222325UL;
        var h2 = 0x84222325cbf29ce4UL;
        foreach (var b in Encoding.UTF8.GetBytes(raw))
        {
            h1 = (h1 ^ b) * prime;
            h2 = (h2 ^ (byte)(b + 0x5b)) * prime;
            h2 = (h2 << 13) | (h2 >> 51);
        }

        return h1.ToString("x16") + h2.ToString("x16");
    }
}
