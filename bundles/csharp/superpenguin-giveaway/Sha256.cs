using System.Text;

namespace WaddleBundleSuperpenguinGiveaway;

/// <summary>
/// Pure managed SHA-256 (FIPS 180-4). The giveaway's fairness protocol needs a real cryptographic hash that a viewer can
/// recompute with any standard tool, but `System.Security.Cryptography` support inside a WASI NativeAOT component is not
/// reliable, so the bundle carries its own implementation, verified against the platform one in the test suite.
/// </summary>
public static class Sha256
{
    private static readonly uint[] K =
    [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ];

    /// <summary>The SHA-256 digest of <paramref name="data"/>.</summary>
    public static byte[] Hash(ReadOnlySpan<byte> data)
    {
        uint[] h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
        var padded = Pad(data);
        var w = new uint[64];
        for (var offset = 0; offset < padded.Length; offset += 64)
        {
            for (var i = 0; i < 16; i++)
            {
                var j = offset + i * 4;
                w[i] = (uint)(padded[j] << 24 | padded[j + 1] << 16 | padded[j + 2] << 8 | padded[j + 3]);
            }

            for (var i = 16; i < 64; i++)
            {
                var s0 = Rotr(w[i - 15], 7) ^ Rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
                var s1 = Rotr(w[i - 2], 17) ^ Rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
                w[i] = w[i - 16] + s0 + w[i - 7] + s1;
            }

            var a = h[0]; var b = h[1]; var c = h[2]; var d = h[3];
            var e = h[4]; var f = h[5]; var g = h[6]; var hh = h[7];
            for (var i = 0; i < 64; i++)
            {
                var t1 = hh + (Rotr(e, 6) ^ Rotr(e, 11) ^ Rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
                var t2 = (Rotr(a, 2) ^ Rotr(a, 13) ^ Rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
                hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
            }

            h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
        }

        var digest = new byte[32];
        for (var i = 0; i < 8; i++)
        {
            digest[i * 4] = (byte)(h[i] >> 24);
            digest[i * 4 + 1] = (byte)(h[i] >> 16);
            digest[i * 4 + 2] = (byte)(h[i] >> 8);
            digest[i * 4 + 3] = (byte)h[i];
        }

        return digest;
    }

    /// <summary>The digest of the UTF-8 bytes of <paramref name="text"/>.</summary>
    public static byte[] Hash(string text) => Hash(Encoding.UTF8.GetBytes(text));

    /// <summary>Lower-case hex of <paramref name="bytes"/>.</summary>
    public static string Hex(ReadOnlySpan<byte> bytes)
    {
        var chars = new char[bytes.Length * 2];
        for (var i = 0; i < bytes.Length; i++)
        {
            chars[i * 2] = HexDigit(bytes[i] >> 4);
            chars[i * 2 + 1] = HexDigit(bytes[i] & 0xf);
        }

        return new string(chars);
    }

    /// <summary>Parses lower/upper-case hex; returns null for odd length or a non-hex character.</summary>
    public static byte[]? FromHex(string hex)
    {
        if (hex.Length % 2 != 0)
        {
            return null;
        }

        var bytes = new byte[hex.Length / 2];
        for (var i = 0; i < bytes.Length; i++)
        {
            var hi = Nibble(hex[i * 2]);
            var lo = Nibble(hex[i * 2 + 1]);
            if (hi < 0 || lo < 0)
            {
                return null;
            }

            bytes[i] = (byte)(hi << 4 | lo);
        }

        return bytes;
    }

    private static char HexDigit(int value) => (char)(value < 10 ? '0' + value : 'a' + value - 10);

    private static int Nibble(char c) => c switch
    {
        >= '0' and <= '9' => c - '0',
        >= 'a' and <= 'f' => c - 'a' + 10,
        >= 'A' and <= 'F' => c - 'A' + 10,
        _ => -1,
    };

    private static uint Rotr(uint value, int bits) => value >> bits | value << (32 - bits);

    private static byte[] Pad(ReadOnlySpan<byte> data)
    {
        var total = (data.Length + 9 + 63) / 64 * 64;
        var padded = new byte[total];
        data.CopyTo(padded);
        padded[data.Length] = 0x80;
        var bitLength = (ulong)data.Length * 8;
        for (var i = 0; i < 8; i++)
        {
            padded[total - 1 - i] = (byte)(bitLength >> (8 * i));
        }

        return padded;
    }
}
