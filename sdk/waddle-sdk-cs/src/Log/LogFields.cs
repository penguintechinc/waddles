using System.Text;
using System.Text.Json;

namespace WaddleSdk.Log;

/// <summary>
/// Builds a canonical JSON object string for <see cref="ILogClient.Write"/>'s
/// `fieldsJson` parameter using <see cref="Utf8JsonWriter"/> directly --
/// reflection-free (no `JsonSerializer.Serialize(object)`), unlike a generic
/// dictionary-of-`object` serialization would require.
/// </summary>
public sealed class LogFields
{
    private readonly MemoryStream _buffer = new();
    private readonly Utf8JsonWriter _writer;
    private bool _finished;

    public LogFields()
    {
        _writer = new Utf8JsonWriter(_buffer);
        _writer.WriteStartObject();
    }

    public LogFields With(string key, string value)
    {
        _writer.WriteString(key, value);
        return this;
    }

    public LogFields With(string key, long value)
    {
        _writer.WriteNumber(key, value);
        return this;
    }

    public LogFields With(string key, double value)
    {
        _writer.WriteNumber(key, value);
        return this;
    }

    public LogFields With(string key, bool value)
    {
        _writer.WriteBoolean(key, value);
        return this;
    }

    /// <summary>Finalizes and returns the canonical JSON object text. Idempotent.</summary>
    public string Build()
    {
        if (!_finished)
        {
            _writer.WriteEndObject();
            _writer.Flush();
            _finished = true;
        }

        return Encoding.UTF8.GetString(_buffer.ToArray());
    }
}
